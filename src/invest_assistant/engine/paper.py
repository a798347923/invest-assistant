"""模拟盘（纸面交易）：条件自动买入 → 每日 AI 建议 → 次日自动执行 → 收益跟踪。

流程：
- 买入：盘中轮询发现自选标的触发「AI 买入计划」买点 → 以现价自动买入
  （金额 = 组合资产 × paper.position_fraction，受现金与 max_positions 限制）；
- 日报：每日 15:40 用收盘行情估值 + LLM 生成操作建议（加仓/减仓/清仓/持有，
  带具体理由），存 paper_report 并推送（邮件+微信）；
- 执行：建议在「下一交易日首次轮询」按现价自动执行；执行结果写 execution_json；
- 收益：paper_daily 每交易日存快照（现金/市值/总资产/当日盈亏/累计收益率）。

资金与仓位参数见 config.yaml 的 paper 段（初始资金可自由调整）；
费用与滑点沿用 backtest.fees；仅限 A股/ETF（场外基金不参与）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging

from pydantic import BaseModel, field_validator
from sqlalchemy import select

from ..backtest.fees import Fees
from ..config import get_settings
from ..db.models import AnalysisCard, PaperDaily, PaperPosition, PaperReport, PaperTrade
from ..db.session import get_session_factory
from ..domain.conditions import CONFIRM_LABELS
from ..domain.journal import lessons_text, plan_context, record_trade
from ..domain.plan import parse_plan_levels
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import PAPER_V1_SYSTEM, paper_user
from ..llm.usage import record_usage

log = logging.getLogger(__name__)

_ACTIONS = ("加仓", "减仓", "清仓", "持有")


def _clamp(value, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, number))


def load_paper_config(settings=None) -> dict:
    settings = settings or get_settings()
    return settings.app_config().get("paper") or {}


class PaperAdvice(BaseModel):
    code: str
    action: str = "持有"
    qty_pct: float = 0.0
    confirm: list[str] = []  # 加仓建议的程序化确认条件（枚举，次日盘中校验，全部满足才执行）
    price_hint: str = ""
    reason: str = ""

    @field_validator("code", "action", "price_hint", "reason", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)

    @field_validator("confirm", mode="before")
    @classmethod
    def _coerce_confirm(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        return [str(value)]

    @field_validator("qty_pct", mode="before")
    @classmethod
    def _coerce_qty(cls, value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0


class PaperRead(BaseModel):
    summary: str
    positions: list[PaperAdvice] = []
    risk_notes: list[str] = []
    confidence: float = 0.5

    @field_validator("summary", mode="before")
    @classmethod
    def _coerce_summary(cls, value):
        return "" if value is None else str(value)

    @field_validator("risk_notes", mode="before")
    @classmethod
    def _coerce_notes(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        return [str(value)]


class PaperService:
    """模拟盘账户：成交、统计、自动买入、执行建议。"""

    def __init__(self, session_factory=None, settings=None, data=None, veto=None):
        self._sf = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        self._data = data
        cfg = load_paper_config(self._settings)
        self.enabled = bool(cfg.get("enabled", True))
        self.initial_cash = float(cfg.get("initial_cash", 50000) or 50000)
        self.position_fraction = float(cfg.get("position_fraction", 0.2) or 0.2)
        self.max_positions = int(cfg.get("max_positions", 5) or 5)
        self.max_weight = float(cfg.get("max_position_weight", 0.3) or 0.3)
        # 波动率平价配仓（实验开关，默认关）：按止损距离反推仓位，使单笔风险预算接近
        self.risk_based_sizing = bool(cfg.get("risk_based_sizing", False))
        self.risk_ref_stop_pct = float(cfg.get("risk_ref_stop_pct", 6.0) or 6.0)
        # AI 复核（否决制）：买卖前的最后一道闸门
        self.ai_veto = bool(cfg.get("ai_veto", True))
        self.ai_veto_exempt_stops = bool(cfg.get("ai_veto_exempt_stops", True))
        self.ai_veto_cooldown_minutes = int(cfg.get("ai_veto_cooldown_minutes", 15) or 15)
        self.ai_veto_max_per_day = int(cfg.get("ai_veto_max_per_day", 8) or 8)
        # v3 经验固化：弱市暂停买入 / 回踩确认追加站上MA5 / 形态差异化机械止损
        self.apply_replay_v3 = bool(cfg.get("apply_replay_v3", True))
        self._veto = veto  # 可注入（测试）；默认懒加载 VetoReviewer
        self._veto_state: dict = {}
        fees_cfg = (self._settings.app_config().get("backtest") or {}).get("fees") or {}
        self.fees = Fees.from_config(fees_cfg)

    def _data_service(self):
        if self._data is None:
            from ..datasource import MarketDataService

            self._data = MarketDataService(session_factory=self._sf)
        return self._data

    def _veto_reviewer(self):
        if self._veto is None:
            from ..llm.veto import VetoReviewer

            self._veto = VetoReviewer(session_factory=self._sf)
        return self._veto

    def _veto_payload(
        self, *, action_label: str, code: str, name: str, price: float, quote,
        reason: str, plan: dict | None, position: PaperPosition | None, now: dt.datetime,
    ) -> dict:
        payload: dict = {
            "操作": action_label,
            "标的": {"code": code, "name": name},
            "现价": price,
            "触发依据": reason[:200],
            "计划": plan or {},
        }
        if quote is not None and hasattr(quote, "open"):
            payload["今日行情"] = {
                "今开": quote.open,
                "最高": quote.high,
                "最低": quote.low,
                "昨收": quote.prev_close,
                "涨跌%": round(quote.pct_change or 0.0, 2),
            }
        if position is not None:
            pnl_pct = (price / position.avg_cost - 1) * 100 if position.avg_cost else 0.0
            payload["当前持仓"] = {
                "数量": position.qty,
                "成本": round(position.avg_cost, 4),
                "盈亏%": round(pnl_pct, 2),
                "持有天数": (now - position.opened_at).days,
            }
        try:
            env = self._data_service().get_market_env()
            if env:
                payload["大盘"] = {
                    "两市成交额(亿)": env.get("两市成交额(亿)"),
                    "指数涨跌%": {
                        key: value.get("涨跌%")
                        for key, value in (env.get("指数") or {}).items()
                    },
                }
        except Exception:  # noqa: BLE001 — 环境数据 best-effort
            pass
        return payload

    def _ai_veto(
        self, *, side: str, action_label: str, code: str, name: str, price: float, quote,
        reason: str, plan: dict | None, position: PaperPosition | None, now: dt.datetime,
        on_error_cancel: bool,
    ) -> tuple[bool, str]:
        """执行前 AI 复核（带节流）。返回（是否放行, 说明）。side: buy / sell / stop。"""
        if not self.ai_veto:
            return True, ""
        if side == "stop" and self.ai_veto_exempt_stops:
            return True, ""  # 纪律止损豁免：保命优先
        reviewer = self._veto_reviewer()
        if reviewer is None or not getattr(reviewer, "configured", False):
            return True, ""  # 未配置 LLM：整个复核功能视为关闭
        key = (code, side)
        state = self._veto_state.get(key)
        if (
            state is not None
            and state.get("date") == now.date()
            and (now - state["at"]) < dt.timedelta(minutes=self.ai_veto_cooldown_minutes)
        ):
            if state.get("approve"):
                return True, "（复核同意，冷却期内复用）"
            return False, f"AI 反对：{state.get('reason') or '明确反对'}（冷却期内不重复询问）"
        if (
            state is not None
            and state.get("date") == now.date()
            and state.get("count", 0) >= self.ai_veto_max_per_day
        ):
            if side == "buy":
                return False, f"今日复核次数已用完（{self.ai_veto_max_per_day} 次），买入保守取消"
            return True, "（复核次数用完，卖出按原计划执行）"
        payload = self._veto_payload(
            action_label=action_label, code=code, name=name, price=price, quote=quote,
            reason=reason, plan=plan, position=position, now=now,
        )
        try:
            decision = reviewer.review(payload)
        except Exception as exc:  # noqa: BLE001 — 复核异常走失败策略
            log.warning("AI 复核调用异常 %s: %s", code, exc)
            decision = None
        count = (state.get("count", 0) if state and state.get("date") == now.date() else 0) + 1
        if decision is None:
            approve = not on_error_cancel
            self._veto_state[key] = {
                "at": now, "date": now.date(), "approve": approve,
                "count": count, "reason": "复核不可用",
            }
            if approve:
                return True, "（AI 复核不可用，按原计划执行）"
            return False, "AI 复核不可用，买入保守取消"
        approve = bool(decision.get("approve", True))
        veto_reason = str(decision.get("reason") or "")
        self._veto_state[key] = {
            "at": now, "date": now.date(), "approve": approve,
            "count": count, "reason": veto_reason,
        }
        if approve:
            log.info("AI 复核同意 %s（%s）：%s", code, action_label, veto_reason)
            return True, ""
        log.info("AI 复核反对 %s（%s）：%s", code, action_label, veto_reason)
        return False, f"AI 反对：{veto_reason or '明确反对'}"

    # -------------------------------------------------- 账户统计

    def cash(self, session) -> float:
        """现金 = 初始资金 − Σ(买入成交额+费) + Σ(卖出成交额−费)。"""
        from sqlalchemy import func as sql_func

        buys = session.execute(
            select(sql_func.coalesce(sql_func.sum(PaperTrade.amount + PaperTrade.fee), 0.0)).where(
                PaperTrade.side == "buy"
            )
        ).scalar_one()
        sells = session.execute(
            select(sql_func.coalesce(sql_func.sum(PaperTrade.amount - PaperTrade.fee), 0.0)).where(
                PaperTrade.side == "sell"
            )
        ).scalar_one()
        return round(self.initial_cash - float(buys) + float(sells), 2)

    def open_positions(self, session) -> list[PaperPosition]:
        return list(
            session.scalars(
                select(PaperPosition)
                .where(PaperPosition.status == "open")
                .order_by(PaperPosition.opened_at)
            )
        )

    def portfolio_stats(self, session) -> dict:
        positions = self.open_positions(session)
        cash = self.cash(session)
        market_value = sum((p.last_price or p.avg_cost) * p.qty for p in positions)
        # 弱市停泊（独立资金层）：单独估值并计入总资产，不占股票槽位
        parking_value = 0.0
        try:
            from .parking import ParkingService

            parking_value = ParkingService(
                session_factory=self._sf, settings=self._settings
            ).value(session)
        except Exception:  # noqa: BLE001 — 停泊估值失败不影响主账本
            parking_value = 0.0
        equity = cash + market_value + parking_value
        last_daily = session.scalars(
            select(PaperDaily).order_by(PaperDaily.date.desc()).limit(1)
        ).first()
        base = last_daily.equity if last_daily is not None else self.initial_cash
        day_pnl = equity - base
        return {
            "initial_cash": self.initial_cash,
            "cash": cash,
            "market_value": market_value,
            "parking_value": parking_value,
            "equity": equity,
            "day_pnl": day_pnl,
            "total_pnl": equity - self.initial_cash,
            "total_pnl_pct": (equity / self.initial_cash - 1) * 100 if self.initial_cash else 0.0,
            "position_count": len(positions),
        }

    # -------------------------------------------------- 成交

    def _buy(
        self, session, code: str, name: str, price: float, qty: float, reason: str, now,
        context: dict | None = None,
    ) -> PaperTrade:
        fill = round(price * (1 + self.fees.slippage), 4)
        amount = round(fill * qty, 2)
        fee = round(self.fees.buy_fee(amount), 2)
        position = session.scalars(
            select(PaperPosition).where(
                PaperPosition.code == code, PaperPosition.status == "open"
            )
        ).first()
        is_new = position is None
        if position is None:
            position = PaperPosition(code=code, name=name, qty=0.0, avg_cost=0.0, opened_at=now)
            session.add(position)
        total_qty = position.qty + qty
        position.avg_cost = round(
            (position.avg_cost * position.qty + fill * qty) / total_qty, 6
        )
        position.qty = round(total_qty, 4)
        position.name = name or position.name
        position.last_price = price
        trade = PaperTrade(
            ts=now, code=code, name=name, side="buy", qty=qty, price=fill,
            amount=amount, fee=fee, reason=reason,
        )
        session.add(trade)
        session.flush()
        record_trade(
            session,
            source="paper",
            ref_id=position.id,
            code=code,
            name=name or position.name,
            action="建仓" if is_new else "加仓",
            qty=qty,
            price=fill,
            amount=amount,
            fee=fee,
            reason=reason,
            context=context,
            ts=now,
        )
        return trade

    def _sell(
        self, session, position: PaperPosition, price: float, qty: float, reason: str, now,
        context: dict | None = None,
    ) -> PaperTrade:
        qty = min(qty, position.qty)
        fill = round(price * (1 - self.fees.slippage), 4)
        amount = round(fill * qty, 2)
        fee = round(self.fees.sell_fee(amount), 2)
        pnl = round((fill - position.avg_cost) * qty - fee, 2)
        position.qty = round(position.qty - qty, 4)
        position.last_price = price
        position.realized_pnl = round(position.realized_pnl + pnl, 2)
        if position.qty <= 1e-6:
            position.qty = 0.0
            position.status = "closed"
            position.closed_at = now
        trade = PaperTrade(
            ts=now, code=position.code, name=position.name, side="sell", qty=qty,
            price=fill, amount=amount, fee=fee, reason=reason, realized_pnl=pnl,
        )
        session.add(trade)
        session.flush()
        record_trade(
            session,
            source="paper",
            ref_id=position.id,
            code=position.code,
            name=position.name or position.code,
            action="清仓" if position.status == "closed" else "减仓",
            qty=qty,
            price=fill,
            amount=amount,
            fee=fee,
            realized_pnl=pnl,
            reason=reason,
            context=context,
            ts=now,
        )
        return trade

    # -------------------------------------------------- 自动买入（买点触发）

    def _entry_weight(self, price: float, stop_price: float | None) -> float:
        """单笔建仓占组合的比例。

        默认等权（position_fraction）。开启 risk_based_sizing 后按止损距离反推：
        止损越远（波动越大）仓位越小、止损越近仓位越大，权重限制在
        [0.5×, min(max_position_weight, 2×)] 区间内，避免极端。

        依据：2026-11 组合级验证——同一批交易下，等权训练集 +77.6%/回撤 −15.1%、
        验证集 −11.5%/回撤 −33.3%；波动率平价后训练集 +77.7%/回撤 −13.9%、
        验证集 +7.8%/回撤 −31.3%（见 docs/weak-market-study.md 与 portfolio.py）。
        """
        base = self.position_fraction
        if not self.risk_based_sizing or not stop_price or not (0 < stop_price < price):
            return base
        distance = (price - stop_price) / price
        ref = self.risk_ref_stop_pct / 100.0
        if distance <= 0 or ref <= 0:
            return base
        weight = base * (ref / distance)
        return max(base * 0.5, min(self.max_weight, base * 2, weight))

    def try_auto_entry(
        self, *, session, code: str, name: str, price: float, reason: str, now,
        plan: dict | None = None, quote=None, stop_price: float | None = None,
        target_price: float | None = None,
    ) -> dict | None:
        """自选买点触发时自动买入；不满足条件（已持仓/无现金/超上限）返回 None。"""
        if not self.enabled or price <= 0:
            return None
        existing = session.scalars(
            select(PaperPosition).where(
                PaperPosition.code == code, PaperPosition.status == "open"
            )
        ).first()
        if existing is not None:
            return None
        if len(self.open_positions(session)) >= self.max_positions:
            return None
        day_start = dt.datetime.combine(now.date(), dt.time.min)
        sold_today = session.scalar(
            select(PaperTrade.id)
            .where(PaperTrade.code == code, PaperTrade.side == "sell", PaperTrade.ts >= day_start)
            .limit(1)
        )
        if sold_today is not None:
            return None  # 反手防抖：当日已卖出该标的，不再自动重买
        stats = self.portfolio_stats(session)
        weight = self._entry_weight(price, stop_price)
        budget = min(stats["equity"] * weight, stats["cash"] * 0.995)
        qty = int(budget / (price * (1 + self.fees.slippage)) / 100) * 100
        if qty < 100:
            return None
        allowed, veto_note = self._ai_veto(
            side="buy", action_label="买入（新建仓，按 AI 买入计划）", code=code, name=name,
            price=price, quote=quote, reason=reason, plan=plan, position=None, now=now,
            on_error_cancel=True,
        )
        if not allowed:
            return {"vetoed": True, "code": code, "reason": veto_note}
        context = {
            "触发": reason,
            "组合": {
                "总资产": round(stats["equity"], 2),
                "现金": round(stats["cash"], 2),
                "持仓数": stats["position_count"],
            },
            "计划": plan or {},
        }
        trade = self._buy(
            session, code, name, price, qty, f"自动买入：{reason}", now, context=context
        )
        position = session.scalars(
            select(PaperPosition).where(
                PaperPosition.code == code, PaperPosition.status == "open"
            )
        ).first()
        if position is not None:
            if stop_price:
                position.stop_price = round(float(stop_price), 4)  # 机械止损（入场时固化）
            if target_price:
                position.target_price = round(float(target_price), 4)  # v12 目标止盈
                position.partial_price = round(float(trade.price) * 1.10, 4)  # v12 到 +10% 减半
        log.info("模拟盘自动买入 %s %s 股 @ %.4f（%s）", code, qty, trade.price, reason)
        return {
            "code": code,
            "name": name,
            "qty": qty,
            "price": float(trade.price),
            "amount": trade.amount,
            "fee": trade.fee,
            "reason": reason,
        }

    def import_position(
        self, *, code: str, qty: float, cost: float, name: str = "",
        last_price: float | None = None, opened_at: dt.datetime | None = None,
        note: str = "", stop_price: float | None = None,
        target_price: float | None = None, partial_price: float | None = None,
    ) -> dict:
        """手动导入已有持仓：按给定成本入账（不触发滑点与 AI 复核）。"""
        if qty <= 0 or cost <= 0:
            raise ValueError("数量与成本必须大于 0")
        now = opened_at or dt.datetime.now()
        with self._sf() as session:
            existing = session.scalars(
                select(PaperPosition).where(
                    PaperPosition.code == code, PaperPosition.status == "open"
                )
            ).first()
            if existing is not None:
                raise ValueError(f"{code} 已有未平仓模拟盘持仓")
            if len(self.open_positions(session)) >= self.max_positions:
                raise ValueError(f"模拟盘持仓数已达上限 {self.max_positions}")
            price = cost / (1 + self.fees.slippage)
            trade = self._buy(
                session, code, name, price, qty,
                note or "导入持仓（手动）", now,
                context={"触发": "导入持仓", "成本": cost},
            )
            position = session.scalars(
                select(PaperPosition).where(
                    PaperPosition.code == code, PaperPosition.status == "open"
                )
            ).first()
            if position is not None:
                position.last_price = round(float(last_price or cost), 4)
                if stop_price:
                    position.stop_price = round(float(stop_price), 4)
                if target_price:
                    position.target_price = round(float(target_price), 4)
                if partial_price:
                    position.partial_price = round(float(partial_price), 4)
            session.commit()
            return {
                "position_id": position.id if position else None,
                "code": code,
                "name": position.name if position else name,
                "qty": qty,
                "avg_cost": position.avg_cost if position else cost,
                "price": float(trade.price),
                "fee": trade.fee,
            }

    def stop_out(
        self, *, session, position: PaperPosition, price: float, reason: str, now,
        context: dict | None = None, quote=None,
    ) -> dict:
        """纪律止损：按失效价规则立即清仓（盘中触发，1 分钟内生效）。"""
        allowed, veto_note = self._ai_veto(
            side="stop", action_label="清仓（纪律止损）", code=position.code,
            name=position.name or position.code, price=price, quote=quote, reason=reason,
            plan=(context or {}).get("计划"), position=position, now=now, on_error_cancel=False,
        )
        if not allowed:
            return {"vetoed": True, "code": position.code, "reason": veto_note}
        trade = self._sell(session, position, price, position.qty, reason, now, context=context)
        return {
            "code": position.code,
            "name": position.name or position.code,
            "qty": trade.qty,
            "price": float(trade.price),
            "realized_pnl": trade.realized_pnl,
        }

    def sell_mechanical(
        self, *, session, position: PaperPosition, price: float, qty: float,
        reason: str, now, context: dict | None = None,
    ) -> dict:
        """机械止盈/减半（不经过 AI 复核，与纪律止损同级的确定性动作）。"""
        trade = self._sell(
            session, position, price, min(qty, position.qty), reason, now, context=context
        )
        return {
            "code": position.code,
            "name": position.name or position.code,
            "qty": trade.qty,
            "price": float(trade.price),
            "realized_pnl": trade.realized_pnl,
            "remaining": position.qty,
            "status": position.status,
        }

    # -------------------------------------------------- 执行上一交易日建议

    @staticmethod
    def _price_of(value) -> float | None:
        """兼容 Quote 对象 / 裸价格。"""
        if value is None:
            return None
        try:
            price = float(getattr(value, "price", value) or 0)
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    def _size_add(self, session, position: PaperPosition, price: float, pct: float) -> int:
        """加仓数量：相对现持仓 pct%，受单标的上限与现金约束。"""
        stats = self.portfolio_stats(session)
        room = stats["equity"] * self.max_weight - position.qty * price
        budget = min(position.qty * price * pct / 100, room, stats["cash"] * 0.995)
        return int(budget / (price * (1 + self.fees.slippage)) / 100) * 100

    def _confirmation_ok(self, code: str, confirm: list[str], quote, now) -> tuple[bool, str]:
        """确认条件校验（无 confirm 视为通过；无法判定一律不通过）。"""
        from ..domain.conditions import evaluate_confirmations

        codes = [str(x) for x in (confirm or []) if x]
        if not codes:
            return True, ""
        if quote is None or not hasattr(quote, "open"):
            return False, "缺少分时行情明细，无法校验确认条件"
        try:
            bars = self._data_service().get_daily_bars(
                code, now.date() - dt.timedelta(days=90), now.date(), adjust="qfq"
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"日线不可用：{exc}"
        return evaluate_confirmations(
            codes,
            price=quote.price,
            open_price=quote.open,
            volume=quote.volume,
            source=quote.source,
            bars=bars,
            now=now,
        )

    def execute_pending(self, quotes: dict, now: dt.datetime | None = None) -> list[dict]:
        """下一交易日首次轮询执行最新 pending 日报中的建议；加仓需确认条件满足，未满足当日盘中延迟重试。"""
        now = now or dt.datetime.now()
        if not self.enabled:
            return []
        with self._sf() as session:
            report = session.scalars(
                select(PaperReport)
                .where(PaperReport.status == "pending")
                .order_by(PaperReport.date.desc())
                .limit(1)
            ).first()
            if report is None or now.date() <= report.date:
                return []
            if (now.date() - report.date).days > 7:
                report.status = "expired"
                session.commit()
                return [{"code": "", "action": "过期作废", "report_date": report.date.isoformat()}]
            executed: list[dict] = []
            skipped: list[dict] = []
            deferred: list[dict] = []
            for advice in (report.content_json or {}).get("positions", []):
                code = str(advice.get("code") or "")
                action = str(advice.get("action") or "持有")
                if not code or action not in ("加仓", "减仓", "清仓"):
                    continue
                value = quotes.get(code)
                price = self._price_of(value)
                position = session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.code == code, PaperPosition.status == "open"
                    )
                ).first()
                if position is None or price is None:
                    skipped.append({"code": code, "action": action, "reason": "无持仓或无行情"})
                    continue
                confirm = [str(x) for x in (advice.get("confirm") or [])]
                if action == "加仓":
                    ok, detail = self._confirmation_ok(code, confirm, value, now)
                    if not ok:
                        deferred.append({
                            "code": code,
                            "qty_pct": _clamp(advice.get("qty_pct"), 10, 50),
                            "confirm": confirm,
                            "reason": str(advice.get("reason") or "")[:120],
                            "report_date": report.date.isoformat(),
                            "valid_date": now.date().isoformat(),
                            "note": detail,
                        })
                        continue
                allowed, veto_note = self._ai_veto(
                    side="buy" if action == "加仓" else "sell",
                    action_label={"加仓": "加仓", "减仓": "减仓", "清仓": "清仓"}[action],
                    code=code, name=position.name or code, price=price, quote=value,
                    reason=str(advice.get("reason") or ""), plan=plan_context(session, code),
                    position=position, now=now, on_error_cancel=(action == "加仓"),
                )
                if not allowed:
                    skipped.append({"code": code, "action": action, "reason": veto_note})
                    continue
                if action == "清仓":
                    qty = position.qty
                elif action == "减仓":
                    pct = _clamp(advice.get("qty_pct"), 20, 100)
                    qty = int(position.qty * pct / 100 / 100) * 100
                else:  # 加仓（确认已通过）
                    qty = self._size_add(session, position, price, _clamp(advice.get("qty_pct"), 10, 50))
                if qty < 100:
                    skipped.append({"code": code, "action": action, "reason": "数量不足 100 股或额度/资金不足"})
                    continue
                reason = f"执行 {report.date:%m-%d} AI 建议：{action}（{str(advice.get('reason') or '')[:60]}）"
                context = {
                    "执行": f"{report.date:%m-%d} AI 日报建议",
                    "建议理由": str(advice.get("reason") or "")[:160],
                    "价格提示": str(advice.get("price_hint") or ""),
                    "计划": plan_context(session, code),
                }
                if action == "加仓":
                    trade = self._buy(
                        session, code, position.name, price, qty, reason, now, context=context
                    )
                    executed.append({
                        "code": code, "name": position.name, "action": "加仓", "qty": qty,
                        "price": float(trade.price), "fee": trade.fee,
                        "report_date": report.date.isoformat(),
                    })
                else:
                    sell_qty = position.qty if action == "清仓" or qty >= position.qty else qty
                    trade = self._sell(
                        session, position, price, sell_qty, reason, now, context=context
                    )
                    executed.append({
                        "code": code, "name": trade.name, "action": action, "qty": trade.qty,
                        "price": float(trade.price), "fee": trade.fee,
                        "realized_pnl": trade.realized_pnl,
                        "report_date": report.date.isoformat(),
                    })
            report.execution_json = {
                "executed_at": now.isoformat(timespec="seconds"),
                "executed": executed,
                "skipped": skipped,
                "deferred_adds": deferred,
            }
            report.status = "executed"
            session.commit()
            if executed or deferred:
                log.info("模拟盘执行建议 %s 笔（延迟加仓 %s 笔）: %s", len(executed), len(deferred), executed)
            return executed

    def retry_deferred_adds(self, quotes: dict, now: dt.datetime | None = None) -> list[dict]:
        """当日盘中重试被推迟的「加仓」建议：确认条件满足即执行，当日过后自动作废。"""
        now = now or dt.datetime.now()
        if not self.enabled:
            return []
        executed: list[dict] = []
        with self._sf() as session:
            report = session.scalars(
                select(PaperReport)
                .where(PaperReport.status.in_(("pending", "executed")))
                .order_by(PaperReport.date.desc())
                .limit(1)
            ).first()
            if report is None:
                return []
            deferred = list((report.execution_json or {}).get("deferred_adds") or [])
            if not deferred:
                return []
            keep: list[dict] = []
            for entry in deferred:
                if str(entry.get("valid_date")) != now.date().isoformat():
                    continue  # 只限执行当日有效，过期作废
                code = str(entry.get("code") or "")
                value = quotes.get(code)
                price = self._price_of(value)
                position = session.scalars(
                    select(PaperPosition).where(
                        PaperPosition.code == code, PaperPosition.status == "open"
                    )
                ).first()
                if position is None:
                    continue
                if price is None:
                    keep.append(entry)
                    continue
                ok, detail = self._confirmation_ok(code, entry.get("confirm") or [], value, now)
                if not ok:
                    entry["note"] = detail
                    keep.append(entry)
                    continue
                allowed, veto_note = self._ai_veto(
                    side="buy", action_label="加仓（延迟确认）", code=code,
                    name=position.name or code, price=price, quote=value,
                    reason=str(entry.get("reason") or ""), plan=plan_context(session, code),
                    position=position, now=now, on_error_cancel=True,
                )
                if not allowed:
                    entry["note"] = veto_note
                    keep.append(entry)
                    continue
                qty = self._size_add(session, position, price, _clamp(entry.get("qty_pct"), 10, 50))
                if qty < 100:
                    continue  # 资金/额度不足，当日作废
                reason = f"执行 {entry.get('report_date', '')} AI 建议：加仓（盘中确认通过）"
                context = {
                    "执行": f"{entry.get('report_date', '')} AI 日报建议（延迟确认）",
                    "建议理由": str(entry.get("reason") or ""),
                    "确认": detail,
                    "计划": plan_context(session, code),
                }
                trade = self._buy(session, code, position.name, price, qty, reason, now, context=context)
                executed.append({
                    "code": code, "name": position.name, "action": "加仓", "qty": qty,
                    "price": float(trade.price), "fee": trade.fee,
                    "report_date": entry.get("report_date"),
                })
            report.execution_json = {**(report.execution_json or {}), "deferred_adds": keep}
            session.commit()
        if executed:
            log.info("模拟盘延迟加仓执行 %s 笔: %s", len(executed), executed)
        return executed


def _latest_plan(session, code: str) -> dict | None:
    card = session.scalars(
        select(AnalysisCard)
        .where(AnalysisCard.code == code)
        .order_by(AnalysisCard.created_at.desc())
        .limit(1)
    ).first()
    return parse_plan_levels(card.content_json) if card is not None else None


def _save_daily(session, day: dt.date, stats: dict, service=None) -> None:
    row = session.get(PaperDaily, day)
    if row is None:
        row = PaperDaily(date=day)
        session.add(row)
    row.cash = round(stats["cash"], 2)
    row.market_value = round(stats["market_value"], 2)
    row.equity = round(stats["equity"], 2)
    row.day_pnl = round(stats["day_pnl"], 2)
    row.total_pnl_pct = round(stats["total_pnl_pct"], 4)
    row.positions = stats["position_count"]
    # 观察报表：额外记录停泊/暴露/均仓，供事后归因（失败不影响日报）
    try:
        from .observe import snapshot as _observe_snapshot

        if service is not None:
            _observe_snapshot(session, service)
    except Exception:  # noqa: BLE001
        pass


def run_daily_review(
    session_factory=None,
    settings=None,
    flow=None,
    llm=None,
    data=None,
    now: dt.datetime | None = None,
    force: bool = False,
) -> dict:
    """每日 15:40：收盘估值 → AI 操作建议 → 日报（存 paper_report + 推送）。"""
    sf = session_factory or get_session_factory()
    settings = settings or get_settings()
    now = now or dt.datetime.now()
    today = now.date()
    service = PaperService(session_factory=sf, settings=settings, data=data)
    if not service.enabled:
        return {"skipped": "模拟盘已关闭"}

    with sf() as session:
        positions = service.open_positions(session)
        if not positions:
            stats = service.portfolio_stats(session)
            _save_daily(session, today, stats, service)
            session.commit()
            return {"positions": 0, "equity": stats["equity"], "note": "无持仓，仅记录快照"}
        existing_check = session.scalars(
            select(PaperReport).where(PaperReport.date == today)
        ).first()
        if existing_check is not None and not force:
            return {"skipped": "今日日报已生成", "date": today.isoformat()}
        codes = [p.code for p in positions]

    # 收盘价（行情失败时用日线收盘兜底，再退到上次估值）
    prices: dict[str, float] = {}
    try:
        quotes = service._data_service().get_quotes(codes)
        for code, q in quotes.items():
            if q.price > 0:
                prices[code] = q.price
    except Exception as exc:  # noqa: BLE001 — 估值尽力而为
        log.warning("模拟盘收盘取行情失败: %s", exc)
    for code in [c for c in codes if c not in prices]:
        try:
            end = today
            bars = service._data_service().get_daily_bars(code, end - dt.timedelta(days=15), end)
            if bars:
                prices[code] = bars[-1].close
        except Exception:  # noqa: BLE001
            continue

    with sf() as session:
        positions = service.open_positions(session)
        detail: list[dict] = []
        for position in positions:
            price = prices.get(position.code) or position.last_price or position.avg_cost
            position.last_price = price
            pnl_pct = (price / position.avg_cost - 1) * 100 if position.avg_cost else 0.0
            plan = _latest_plan(session, position.code)
            effective_stop = position.stop_price or ((plan or {}).get("stop"))
            detail.append({
                "code": position.code,
                "name": position.name or position.code,
                "qty": position.qty,
                "cost": round(position.avg_cost, 4),
                "price": round(price, 4),
                "pnl_pct": round(pnl_pct, 2),
                "holding_days": (today - position.opened_at.date()).days,
                "plan": plan,
                "止损参考": effective_stop,
            })
        stats = service.portfolio_stats(session)
        recent_trades = [
            {
                "ts": t.ts.strftime("%m-%d %H:%M"),
                "code": t.code,
                "side": "买入" if t.side == "buy" else "卖出",
                "qty": t.qty,
                "price": t.price,
                "reason": t.reason[:60],
            }
            for t in session.scalars(
                select(PaperTrade).order_by(PaperTrade.ts.desc()).limit(10)
            ).all()
        ]
        session.commit()

    payload = {
        "初始资金": service.initial_cash,
        "现金": round(stats["cash"], 2),
        "持仓市值": round(stats["market_value"], 2),
        "总资产": round(stats["equity"], 2),
        "累计收益率%": round(stats["total_pnl_pct"], 2),
        "持仓明细": detail,
        "最近成交": recent_trades,
    }
    lessons = lessons_text(sf)
    if lessons:
        payload["历史交易复盘经验"] = lessons
    try:
        from .calibration import calibration_notes

        notes = calibration_notes(sf)
        if notes:
            payload["预测校准结论"] = notes
    except Exception:  # noqa: BLE001 — best-effort
        pass
    try:
        env = service._data_service().get_market_env()
        if env:
            payload["大盘环境"] = env
    except Exception:  # noqa: BLE001 — best-effort
        pass

    llm_client = llm or LLMClient()
    content: dict | None = None
    model = ""
    tokens_in = tokens_out = 0
    cost = 0.0
    llm_error = ""
    if llm_client.configured:
        try:
            result = llm_client.complete_json(
                purpose="paper",
                system=PAPER_V1_SYSTEM,
                user=paper_user(payload),
                schema=PaperRead,
                max_tokens=900,
            )
            content = result.content
            model = result.model
            tokens_in, tokens_out = result.tokens_in, result.tokens_out
            cost = record_usage(
                sf, purpose="paper", model=result.model,
                tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                cached_in=result.cached_in,
            )
        except LLMError as exc:
            llm_error = str(exc)
            log.warning("模拟盘 AI 建议失败: %s", exc)

    # 归一化建议：补齐缺失持仓 + 校验动作/比例 + 失效价规则强制清仓
    advices: dict[str, dict] = {}
    for item in (content or {}).get("positions", []) or []:
        code = str(item.get("code") or "")
        action = str(item.get("action") or "持有")
        if action not in _ACTIONS:
            action = "持有"
        if code:
            advices[code] = {
                "action": action,
                "qty_pct": float(item.get("qty_pct") or 0),
                "confirm": [str(x) for x in (item.get("confirm") or []) if str(x) in CONFIRM_LABELS],
                "price_hint": str(item.get("price_hint") or ""),
                "reason": str(item.get("reason") or ""),
            }
    normalized: list[dict] = []
    for item in detail:
        code = item["code"]
        advice = advices.get(code) or {
            "action": "持有", "qty_pct": 0.0, "confirm": [], "price_hint": "",
            "reason": "未给出建议，默认持有",
        }
        plan = item.get("plan") or {}
        stop = item.get("止损参考") or plan.get("stop")
        if stop and item["price"] <= stop and advice["action"] != "清仓":
            advice = {
                "action": "清仓", "qty_pct": 0.0, "confirm": [], "price_hint": f"{stop:g} 下方",
                "reason": f"【规则】现价 {item['price']:g} 已跌破失效价 {stop:g}：" + advice["reason"],
            }
        if advice["action"] == "加仓":
            advice["qty_pct"] = _clamp(advice["qty_pct"], 10, 50)
        elif advice["action"] == "减仓":
            advice["qty_pct"] = _clamp(advice["qty_pct"], 20, 100)
        else:
            advice["qty_pct"] = 0.0
        normalized.append({"code": code, "name": item["name"], **advice})

    if content is None:
        content = {
            "summary": f"今日 AI 建议不可用（{llm_error or '未配置 LLM'}）；组合总资产 "
            f"{stats['equity']:.0f} 元（{stats['total_pnl_pct']:+.2f}%），持仓 {len(detail)} 只。",
            "positions": [],
            "risk_notes": [],
            "confidence": 0.0,
        }
    content = {**content, "positions": normalized, "stats": {
        "cash": round(stats["cash"], 2),
        "market_value": round(stats["market_value"], 2),
        "equity": round(stats["equity"], 2),
        "day_pnl": round(stats["day_pnl"], 2),
        "total_pnl_pct": round(stats["total_pnl_pct"], 2),
    }}

    with sf() as session:
        existing = session.scalars(
            select(PaperReport).where(PaperReport.date == today)
        ).first()
        if existing is not None:
            session.delete(existing)
            session.flush()
        session.add(
            PaperReport(
                date=today, model=model, content_json=content, status="pending",
                tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
            )
        )
        stats = service.portfolio_stats(session)  # last_price 已更新，重算
        _save_daily(session, today, stats, service)
        session.commit()

    from ..notify.templates import paper_message
    from .notify_flow import NotificationFlow

    sent = False
    flow = flow or NotificationFlow(session_factory=sf)
    if flow.dispatcher.configured:
        with contextlib.suppress(Exception):  # 推送失败不影响日报落库
            flow.dispatcher.send(paper_message(content, stats))
            sent = True

    from .lessons import generate_pending_lessons

    lesson_report: dict = {"generated": 0}
    try:
        lesson_report = generate_pending_lessons(
            session_factory=sf, settings=settings, llm=llm_client
        )
    except Exception:  # noqa: BLE001 — 复盘失败不影响日报
        log.exception("清仓复盘生成失败")

    report = {
        "date": today.isoformat(),
        "positions": len(detail),
        "equity": round(stats["equity"], 2),
        "total_pnl_pct": round(stats["total_pnl_pct"], 2),
        "llm": bool(model),
        "notified": sent,
        "lessons": lesson_report.get("generated", 0),
    }
    log.info("模拟盘日报完成: %s", report)
    return report
