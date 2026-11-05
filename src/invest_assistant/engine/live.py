"""实盘指令单与风控（本模块只管「意图 + 风控 + 审计」，不直接触碰券商界面）。

执行器由 `config.live.executor` 选择（具体实现见 `engine/live_app.py`）：
- `manual`（默认）：只生成指令单，人工在东财下单后 `live fill` 回填；
- `ui_app`：ADB 驱动 MuMu 里的东财 App 自动填单/提交，默认 `live.dry_run=true`（只填不提交）。

安全默认（config.live）：
- enabled=false：总开关，关闭时不产生可执行指令；
- semi_auto_enabled=true：半自动模式开关；
- dry_run=true：自动执行时只填单校验、不点提交；
- 不保存交易密码，只驱动已登录的 App。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import get_settings
from ..db.models import OrderIntent, WatchlistItem
from ..db.session import get_session_factory


@dataclass
class LiveSettings:
    enabled: bool = False
    semi_auto_enabled: bool = True
    mirror_paper_entries: bool = True
    max_order_amount: float = 5000.0
    max_position_weight: float = 0.2
    max_daily_orders: int = 4
    max_daily_buy_amount: float = 20000.0
    price_deviation_pct: float = 1.0
    whitelist: str = "watchlist"
    trading_window: tuple[str, ...] = ("09:35-11:25", "13:05-14:55")
    circuit_breaker_index_pct: float = -1.5
    default_take_profit_pct: float = 12.0
    partial_take_profit_pct: float = 10.0
    partial_fraction: float = 0.5
    default_stop_loss_pct: float = 8.0
    default_max_holding_days: int = 30
    announcement_filter: bool = True
    announcement_limit: int = 20
    announcement_block_keywords: tuple[str, ...] = (
        "减持", "解禁", "立案", "调查", "问询", "预亏", "下修", "风险警示", "退市", "处罚",
    )

    @classmethod
    def from_config(cls, cfg: dict | None) -> LiveSettings:
        cfg = cfg or {}
        windows = cfg.get("trading_window") or ("09:35-11:25", "13:05-14:55")
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            semi_auto_enabled=bool(cfg.get("semi_auto_enabled", True)),
            mirror_paper_entries=bool(cfg.get("mirror_paper_entries", True)),
            max_order_amount=float(cfg.get("max_order_amount", 5000) or 0),
            max_position_weight=float(cfg.get("max_position_weight", 0.2) or 0),
            max_daily_orders=int(cfg.get("max_daily_orders", 4) or 0),
            max_daily_buy_amount=float(cfg.get("max_daily_buy_amount", 20000) or 0),
            price_deviation_pct=float(cfg.get("price_deviation_pct", 1.0) or 0),
            whitelist=str(cfg.get("whitelist", "watchlist")),
            trading_window=tuple(str(x) for x in windows),
            circuit_breaker_index_pct=float(cfg.get("circuit_breaker_index_pct", -1.5) or 0),
            default_take_profit_pct=float(cfg.get("default_take_profit_pct", 12.0) or 0),
            partial_take_profit_pct=float(cfg.get("partial_take_profit_pct", 10.0) or 0),
            partial_fraction=float(cfg.get("partial_fraction", 0.5) or 0.5),
            default_stop_loss_pct=float(cfg.get("default_stop_loss_pct", 8.0) or 0),
            default_max_holding_days=int(cfg.get("default_max_holding_days", 30) or 0),
            announcement_filter=bool(cfg.get("announcement_filter", True)),
            announcement_limit=int(cfg.get("announcement_limit", 20) or 20),
            announcement_block_keywords=tuple(
                str(x) for x in (
                    cfg.get("announcement_block_keywords")
                    or (
                        "减持", "解禁", "立案", "调查", "问询", "预亏", "下修", "风险警示", "退市", "处罚",
                    )
                )
            ),
        )


def _in_window(now: dt.datetime, windows: tuple[str, ...]) -> bool:
    t = now.time()
    for window in windows:
        try:
            start_s, end_s = window.split("-")
            start = dt.time.fromisoformat(start_s.strip())
            end = dt.time.fromisoformat(end_s.strip())
        except ValueError:
            continue
        if start <= t <= end:
            return True
    return False


def _price_limit_pct(code: str, name: str) -> float:
    upper = (name or "").upper()
    if "ST" in upper:
        return 5.0
    if code.startswith(("300", "301", "688")):
        return 20.0
    return 10.0


@dataclass
class RiskResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def check_risk(
    session,
    *,
    settings: LiveSettings,
    code: str,
    side: str,
    qty: int,
    price: float,
    name: str = "",
    ref_price: float | None = None,
    now: dt.datetime,
) -> RiskResult:
    """指令单风控校验（硬约束，默认从严）。"""
    reasons: list[str] = []
    detail: dict = {}

    if not settings.enabled:
        reasons.append("实盘总开关未开启（live.enabled=false）")
    if not settings.semi_auto_enabled:
        reasons.append("半自动开关未开启（live.semi_auto_enabled=false）")

    if side not in ("buy", "sell"):
        reasons.append("方向无效（仅 buy/sell）")
    if qty <= 0 or price <= 0:
        reasons.append("数量或价格无效")

    amount = qty * price
    detail["amount"] = round(amount, 2)
    if side == "buy" and settings.max_order_amount and amount > settings.max_order_amount:
        reasons.append(f"单笔金额 {amount:.0f} 超上限 {settings.max_order_amount:.0f}")

    if ref_price and ref_price > 0 and settings.price_deviation_pct:
        dev = abs(price / ref_price - 1) * 100
        detail["price_deviation_pct"] = round(dev, 2)
        if dev > settings.price_deviation_pct:
            reasons.append(f"限价偏离参考价 {dev:.2f}% 超上限 {settings.price_deviation_pct:g}%")

    if not _in_window(now, settings.trading_window) and side == "buy":
        reasons.append("非允许交易时段（买入）")

    if "ST" in (name or "").upper():
        reasons.append("ST 标的禁止")

    if ref_price and ref_price > 0:
        limit = _price_limit_pct(code, name)
        up = ref_price * (1 + limit / 100)
        down = ref_price * (1 - limit / 100)
        if price > up or price < down:
            reasons.append(f"价格超出涨跌停区间（±{limit:g}%）")

    if side == "buy" and settings.whitelist in ("watchlist", "both"):
        if session.get(WatchlistItem, code) is None:
            reasons.append("标的不在自选池")

    today = now.date()
    intents = session.scalars(
        select(OrderIntent).where(OrderIntent.trade_date == today)
    ).all()
    active = [x for x in intents if x.status in ("pending", "confirmed", "filled")]
    if settings.max_daily_orders and len(active) >= settings.max_daily_orders:
        reasons.append(f"当日指令数已达上限 {settings.max_daily_orders}")
    if side == "buy" and settings.max_daily_buy_amount:
        bought = sum(x.qty * x.price for x in active if x.side == "buy")
        detail["daily_buy_amount"] = round(bought + amount, 2)
        if bought + amount > settings.max_daily_buy_amount:
            reasons.append(f"当日买入金额超上限 {settings.max_daily_buy_amount:.0f}")

    return RiskResult(ok=not reasons, reasons=reasons, detail=detail)


def _to_dict(row: OrderIntent) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else "",
        "trade_date": row.trade_date.isoformat() if row.trade_date else "",
        "code": row.code,
        "name": row.name,
        "side": row.side,
        "qty": row.qty,
        "price": round(row.price, 4),
        "amount": round(row.qty * row.price, 2),
        "source": row.source,
        "mode": row.mode,
        "ref_position_id": row.ref_position_id,
        "stop_price": row.stop_price,
        "reason": row.reason,
        "status": row.status,
        "risk": row.risk_json,
        "idempotency_key": row.idempotency_key,
        "filled_price": row.filled_price,
        "filled_qty": row.filled_qty,
        "filled_at": row.filled_at.isoformat(timespec="seconds") if row.filled_at else None,
        "executor": row.executor,
        "note": row.note,
    }


class LiveService:
    """指令单服务：生成（含风控）→ 确认 → 成交回填 / 取消。"""

    def __init__(self, session_factory=None, settings=None):
        self._sf = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        cfg = (self._settings.app_config() or {}).get("live") or {}
        self.live = LiveSettings.from_config(cfg)

    def create_intent(
        self,
        code: str,
        side: str,
        qty: int,
        price: float,
        *,
        name: str = "",
        ref_price: float | None = None,
        source: str = "manual",
        reason: str = "",
        now: dt.datetime | None = None,
        idempotency_key: str | None = None,
        ref_position_id: int | None = None,
        stop_price: float | None = None,
        session=None,
    ) -> dict:
        """创建指令单；传入 session 时复用它（调用方负责提交，避免 SQLite 写锁冲突）。"""
        if session is not None:
            return self._create_intent_in(
                session, code, side, qty, price,
                name=name, ref_price=ref_price, source=source, reason=reason,
                now=now, idempotency_key=idempotency_key,
                ref_position_id=ref_position_id, stop_price=stop_price,
            )
        with self._sf() as own:
            result = self._create_intent_in(
                own, code, side, qty, price,
                name=name, ref_price=ref_price, source=source, reason=reason,
                now=now, idempotency_key=idempotency_key,
                ref_position_id=ref_position_id, stop_price=stop_price,
            )
            own.commit()
            return result

    def _create_intent_in(
        self, session, code: str, side: str, qty: int, price: float,
        *, name: str, ref_price: float | None, source: str, reason: str,
        now: dt.datetime | None, idempotency_key: str | None,
        ref_position_id: int | None, stop_price: float | None,
    ) -> dict:
        now = now or dt.datetime.now()
        key = idempotency_key or f"{now.date().isoformat()}-{code}-{side}-{qty}-{price:.3f}"
        existing = session.scalar(
            select(OrderIntent).where(OrderIntent.idempotency_key == key)
        )
        if existing is not None:
            return {**_to_dict(existing), "created": False}
        risk = check_risk(
            session,
            settings=self.live,
            code=code, side=side, qty=int(qty), price=float(price),
            name=name, ref_price=ref_price, now=now,
        )
        row = OrderIntent(
            trade_date=now.date(),
            code=code,
            name=name or "",
            side=side,
            qty=int(qty),
            price=float(price),
            source=source,
            mode="semi",
            ref_position_id=ref_position_id,
            stop_price=stop_price,
            reason=reason,
            status="pending" if risk.ok else "blocked",
            risk_json={"ok": risk.ok, "reasons": risk.reasons, **risk.detail},
            idempotency_key=key,
            executor="manual",
        )
        session.add(row)
        session.flush()
        return {**_to_dict(row), "created": True}

    def list_intents(self, status: str = "", limit: int = 50) -> list[dict]:
        with self._sf() as session:
            stmt = select(OrderIntent).order_by(OrderIntent.id.desc()).limit(limit)
            if status:
                stmt = stmt.where(OrderIntent.status == status)
            return [_to_dict(x) for x in session.scalars(stmt)]

    def confirm(self, intent_id: int, now: dt.datetime | None = None) -> dict:
        with self._sf() as session:
            row = session.get(OrderIntent, intent_id)
            if row is None:
                raise ValueError(f"指令单 #{intent_id} 不存在")
            if row.status != "pending":
                raise ValueError(f"指令单状态为 {row.status}，不能确认")
            row.status = "confirmed"
            session.commit()
            return _to_dict(row)

    def fill(
        self,
        intent_id: int,
        price: float,
        qty: int | None = None,
        now: dt.datetime | None = None,
    ) -> dict:
        now = now or dt.datetime.now()
        with self._sf() as session:
            row = session.get(OrderIntent, intent_id)
            if row is None:
                raise ValueError(f"指令单 #{intent_id} 不存在")
            if row.status not in ("pending", "confirmed"):
                raise ValueError(f"指令单状态为 {row.status}，不能回填")
            row.status = "filled"
            row.filled_price = float(price)
            row.filled_qty = int(qty if qty is not None else row.qty)
            row.filled_at = now
            session.commit()
            result = _to_dict(row)
        note = self._apply_account(row, float(price), int(row.filled_qty or row.qty), now)
        if note:
            with self._sf() as session:
                stored = session.get(OrderIntent, intent_id)
                if stored is not None:
                    stored.note = f"{stored.note};{note}".strip(";")
                    session.commit()
                    result = _to_dict(stored)
        return result

    def _apply_account(self, row: OrderIntent, price: float, qty: int, now: dt.datetime) -> str:
        """成交后写实盘账本（持仓 + 交易日志；清仓会自动进入复盘经验库）。"""
        try:
            from sqlalchemy import select as _select

            from ..backtest.fees import Fees
            from ..config import get_settings
            from ..db.models import Position
            from ..domain.positions import ExitRules, PositionService

            ps = PositionService(self._sf)
            fees_cfg = (get_settings().app_config().get("backtest") or {}).get("fees") or {}
            fees = Fees.from_config(fees_cfg)
            amount = price * qty
            if row.side == "buy":
                fee = fees.buy_fee(amount)
                stop_pct = None
                if row.stop_price and row.stop_price < price:
                    stop_pct = max(1.0, min(30.0, (1 - row.stop_price / price) * 100))
                with self._sf() as session:
                    existing = session.scalar(
                        _select(Position)
                        .where(Position.code == row.code, Position.status == "open")
                        .order_by(Position.id.asc())
                    )
                if existing is not None:
                    ps.increase_position(
                        existing.id, qty=qty, price=price, fee=fee, traded_at=now,
                        note=f"实盘指令#{row.id}：{row.reason}",
                    )
                else:
                    rules = ExitRules(
                        take_profit_pct=self.live.default_take_profit_pct or None,
                        stop_loss_pct=stop_pct or self.live.default_stop_loss_pct or None,
                        max_holding_days=self.live.default_max_holding_days or None,
                    )
                    ps.add_position(
                        code=row.code, qty=qty, price=price, strategy="swing",
                        rules=rules, name=row.name, fee=fee, opened_at=now,
                        note=f"实盘指令#{row.id}：{row.reason}",
                    )
                return "已记入实盘持仓"
            fee = fees.sell_fee(amount)
            with self._sf() as session:
                pos = session.get(Position, row.ref_position_id) if row.ref_position_id else None
                if pos is None:
                    pos = session.scalar(
                        _select(Position)
                        .where(Position.code == row.code, Position.status == "open")
                        .order_by(Position.id.asc())
                    )
            if pos is None:
                return "账本未更新：未找到对应实盘持仓"
            sell_qty = min(float(qty), float(pos.qty))
            ps.sell(pos.id, qty=sell_qty, price=price, fee=fee, sold_at=now)
            return "已记入实盘卖出"
        except Exception as exc:  # noqa: BLE001 — 账本失败不阻塞成交事实
            return f"账本更新失败：{exc}"

    def cancel(self, intent_id: int, note: str = "") -> dict:
        with self._sf() as session:
            row = session.get(OrderIntent, intent_id)
            if row is None:
                raise ValueError(f"指令单 #{intent_id} 不存在")
            if row.status in ("filled", "cancelled"):
                raise ValueError(f"指令单状态为 {row.status}，不能取消")
            row.status = "cancelled"
            if note:
                row.note = f"{row.note};{note}".strip(";")
            session.commit()
            return _to_dict(row)
