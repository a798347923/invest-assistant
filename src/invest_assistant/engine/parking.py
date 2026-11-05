"""弱市停泊：把闲置现金停到「债 7 : 金 3」，强市退出。

## 为什么单独一层

- **不占 5 个股票槽位**：回测口径就是"只停泊闲置现金"（`scripts/research/defensive.py`）；
- **不参与 AI 建议 / 止损 / 复盘**：它不是交易信号，是资金管理；
- 用 `paper_trade` 流水记账（`reason` 以「弱市停泊」开头），因此**不加表**，`paper-reset` 时自动清空。

## 验证依据（2026-11，见 docs/weak-market-alternatives.md）

同一批股票交易、只改闲置现金去向，5 槽组合、训练/验证双窗口：

| 方案 | 训练集 | 验证集 |
|---|---|---|
| 空仓（现行） | +77.8% / 回撤 −15.1% | −11.4% / 回撤 −34.6% |
| 十年国债 | +91.5% / −14.8% | −6.4% / −34.0% |
| **债7:金3** | **+96.3% / −14.5%** | **−0.0% / −33.0%** |

前视偏差已修正：用**前一日收盘**的状态决定今日是否停泊。

## 口径

- 买入：按权重把「可用现金」分成两份，各买对应 ETF（含滑点与佣金，模拟盘口径）；
- 卖出：状态转强时全部卖出（ETF 不收印花税，但这里沿用股票费率模型，偏保守）；
- 幂等：已有持仓则不再买；无持仓则不再卖。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from ..backtest.fees import Fees
from ..config import get_settings
from ..db.models import KvSetting, PaperTrade
from ..db.session import get_session_factory

PARK_REASON = "弱市停泊"
PRICE_KEY = "paper_parking_prices"


@dataclass
class ParkingSettings:
    enabled: bool = False
    bond: str = "511260"      # 十年国债 ETF
    gold: str = "518880"      # 黄金 ETF
    bond_weight: float = 0.7
    gold_weight: float = 0.3
    min_amount: float = 1000.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> ParkingSettings:
        cfg = cfg or {}
        return cls(
            enabled=bool(cfg.get("parking_enabled", False)),
            bond=str(cfg.get("parking_bond", "511260") or "511260"),
            gold=str(cfg.get("parking_gold", "518880") or "518880"),
            bond_weight=float(cfg.get("parking_bond_weight", 0.7) or 0.7),
            gold_weight=float(cfg.get("parking_gold_weight", 0.3) or 0.3),
            min_amount=float(cfg.get("parking_min_amount", 1000) or 0),
        )


class ParkingService:
    """弱市停泊账户（纸面）：按交易流水记账，价格缓存于 kv。"""

    def __init__(self, session_factory=None, settings=None):
        self._sf = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        app_cfg = self._settings.app_config() or {}
        self.cfg = ParkingSettings.from_config(app_cfg.get("paper") or {})
        fees_cfg = (app_cfg.get("backtest") or {}).get("fees") or {}
        self.fees = Fees.from_config(fees_cfg)

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    @property
    def codes(self) -> list[str]:
        return [self.cfg.bond, self.cfg.gold]

    # ---------------------------------------------------------------- 持仓

    def holdings(self, session) -> dict[str, dict]:
        """从流水还原停泊持仓：{code: {"qty": 股数, "cost": 成本金额}}。"""
        rows = session.scalars(
            select(PaperTrade)
            .where(PaperTrade.reason.like(f"{PARK_REASON}%"))
            .order_by(PaperTrade.id)
        ).all()
        book: dict[str, dict] = {}
        for trade in rows:
            slot = book.setdefault(trade.code, {"qty": 0.0, "cost": 0.0})
            if trade.side == "buy":
                slot["qty"] += trade.qty
                slot["cost"] += trade.amount
            elif slot["qty"] > 0:
                # 按剩余持仓的单位成本结转，避免部分卖出后成本漂移
                unit_cost = slot["cost"] / slot["qty"]
                slot["qty"] -= trade.qty
                slot["cost"] -= trade.qty * unit_cost
            else:
                slot["qty"] -= trade.qty
                slot["cost"] -= trade.amount
        return {code: v for code, v in book.items() if v["qty"] > 1e-9}

    def _cached_prices(self, session) -> dict[str, float]:
        row = session.get(KvSetting, PRICE_KEY)
        if row is None:
            return {}
        raw = row.value_json or {}
        return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}

    def value(self, session, prices: dict | None = None) -> float:
        """停泊仓位市值（无价格时按成本计，保守）。"""
        book = self.holdings(session)
        if not book:
            return 0.0
        prices = prices or self._cached_prices(session)
        total = 0.0
        for code, slot in book.items():
            price = prices.get(code)
            if price and price > 0:
                total += slot["qty"] * price
            else:
                total += slot["cost"]
        return round(total, 2)

    def cache_prices(self, session, quotes: dict) -> None:
        """把最新 ETF 价格写入 kv，供 portfolio_stats 估值。"""
        raw = self._cached_prices(session)
        changed = False
        for code in self.codes:
            quote = quotes.get(code)
            if quote is not None and getattr(quote, "price", 0) > 0:
                raw[code] = float(quote.price)
                changed = True
        if not changed:
            return
        row = session.get(KvSetting, PRICE_KEY)
        if row is None:
            row = KvSetting(key=PRICE_KEY)
            session.add(row)
        row.value_json = raw
        session.add(row)

    # ---------------------------------------------------------------- 调仓

    def rebalance(
        self, session, *, weak: bool, quotes: dict, now: dt.datetime,
        available_cash: float,
    ) -> dict:
        """状态翻转时买入/卖出停泊仓位；其余情况不动（幂等）。"""
        if not self.enabled:
            return {"action": "disabled"}
        book = self.holdings(session)
        if weak and not book:
            return self._buy(session, quotes, now, available_cash)
        if (not weak) and book:
            return self._sell(session, quotes, now)
        return {"action": "hold" if book else "flat"}

    def _buy(self, session, quotes: dict, now: dt.datetime, cash: float) -> dict:
        budget = min(float(cash), max(0.0, float(cash)))
        if budget < self.cfg.min_amount * 2:
            return {"action": "skip", "reason": f"可用现金 {budget:.0f} 不足"}
        legs = [
            (self.cfg.bond, self.cfg.bond_weight),
            (self.cfg.gold, self.cfg.gold_weight),
        ]
        bought = []
        for code, weight in legs:
            quote = quotes.get(code)
            if quote is None or quote.price <= 0:
                continue
            amount = budget * weight
            if amount < self.cfg.min_amount:
                continue
            fill = round(quote.price * (1 + self.fees.slippage), 4)
            qty = int(amount / fill / 100) * 100
            if qty < 100:
                continue
            gross = round(fill * qty, 2)
            fee = round(self.fees.buy_fee(gross), 2)
            session.add(PaperTrade(
                ts=now, code=code, name=getattr(quote, "name", "") or code,
                side="buy", qty=qty, price=fill, amount=gross, fee=fee,
                reason=f"{PARK_REASON}：弱市闲置现金（{weight:.0%}）",
            ))
            bought.append({"code": code, "qty": qty, "price": fill})
        return {"action": "buy", "legs": bought} if bought else {"action": "skip", "reason": "行情缺失"}

    def _sell(self, session, quotes: dict, now: dt.datetime) -> dict:
        sold = []
        for code, slot in self.holdings(session).items():
            quote = quotes.get(code)
            price = quote.price if quote is not None and quote.price > 0 else None
            if price is None:
                continue
            fill = round(price * (1 - self.fees.slippage), 4)
            gross = round(fill * slot["qty"], 2)
            fee = round(self.fees.sell_fee(gross), 2)
            session.add(PaperTrade(
                ts=now, code=code, name=(getattr(quote, "name", "") or code),
                side="sell", qty=slot["qty"], price=fill, amount=gross, fee=fee,
                reason=f"{PARK_REASON}：转强退出",
                realized_pnl=round(gross - slot["cost"] - fee, 2),
            ))
            sold.append({"code": code, "qty": slot["qty"], "price": fill})
        return {"action": "sell", "legs": sold} if sold else {"action": "skip", "reason": "行情缺失"}

    # ---------------------------------------------------------------- 展示

    def status(self, session) -> dict:
        book = self.holdings(session)
        return {
            "enabled": self.enabled,
            "positions": [
                {"code": code, "qty": slot["qty"], "cost": round(slot["cost"], 2)}
                for code, slot in book.items()
            ],
            "value": self.value(session),
            "config": {
                "bond": self.cfg.bond, "gold": self.cfg.gold,
                "bond_weight": self.cfg.bond_weight, "gold_weight": self.cfg.gold_weight,
            },
        }
