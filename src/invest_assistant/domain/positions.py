"""持仓/交易/分红/拆股服务。

关键业务规则（需求 US-1.1 / US-1.5）：
- 建仓必须携带完整退出规则（止盈 + 止损或移动止盈 + 最长持有期），否则拒绝保存；
- 卖出支持部分/全部，自动结算已实现盈亏；全部卖出时关闭持仓并停用规则；
- 分红现金流自动摊薄成本；拆股手动修正数量与成本并留痕。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import select

from ..db.models import CorporateAction, Dividend, ExitRule, Instrument, Position, Trade
from ..db.session import get_session_factory

log = logging.getLogger(__name__)

STRATEGIES = ("value", "growth", "swing", "drip")
STRATEGY_LABELS = {"value": "价值", "growth": "成长", "swing": "波段", "drip": "定投"}


class ValidationError(ValueError):
    """业务校验失败（拒绝保存）。"""


class NotFoundError(LookupError):
    """对象不存在。"""


@dataclass
class ExitRules:
    """建仓时强制录入的退出规则（百分比均为正数）。"""

    take_profit_pct: float | None = None  # 固定止盈（相对成本价，%）
    stop_loss_pct: float | None = None  # 固定止损（相对成本价，%）
    trailing_drawdown_pct: float | None = None  # 移动止盈：自峰值回撤（%）
    max_holding_days: int | None = None  # 最长持有期（天）
    daily_move_pct: float | None = None  # 日内异动提醒（%，可选）

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.take_profit_pct or self.take_profit_pct <= 0:
            missing.append("止盈线（--take-profit）")
        if not self.stop_loss_pct and not self.trailing_drawdown_pct:
            missing.append("止损线（--stop-loss）或 移动止盈回撤（--trailing）至少一个")
        if not self.max_holding_days or self.max_holding_days <= 0:
            missing.append("最长持有期（--max-holding）")
        return missing

    def validate(self) -> None:
        missing = self.missing()
        if missing:
            raise ValidationError("退出规则不完整，缺失：" + "；".join(missing))


def infer_type(code: str) -> str:
    """按代码粗略推断类型（otc_fund 建议显式指定）。"""
    if code.startswith(("51", "56", "58", "15", "16")):
        return "etf"
    if code[0] in "0369":
        return "a_share"
    return "otc_fund"


class PositionService:
    """会话工厂需为 expire_on_commit=False（默认生产工厂已满足）。"""

    def __init__(self, session_factory=None):
        self._sf = session_factory or get_session_factory()

    # -------------------------------------------------- 建仓
    def add_position(
        self,
        *,
        code: str,
        qty: float,
        price: float,
        strategy: str,
        rules: ExitRules,
        name: str = "",
        type: str = "",
        fee: float = 0.0,
        opened_at: dt.datetime | None = None,
        note: str = "",
    ) -> dict:
        from ..datasource.base import exchange_of, normalize_code

        code = normalize_code(code)
        if strategy not in STRATEGIES:
            raise ValidationError(f"未知策略 {strategy!r}（可选: {'/'.join(STRATEGIES)}）")
        if qty <= 0 or price <= 0:
            raise ValidationError("数量与价格必须大于 0")
        if fee < 0:
            raise ValidationError("手续费不能为负")
        rules.validate()

        opened_at = opened_at or dt.datetime.now()
        avg_cost = (price * qty + fee) / qty
        with self._sf() as session:
            instrument = session.get(Instrument, code)
            if instrument is None:
                instrument = Instrument(
                    code=code,
                    name=name or code,
                    type=type or infer_type(code),
                    exchange=exchange_of(code),
                )
                session.add(instrument)
            elif name:
                instrument.name = name

            position = Position(
                code=code, strategy=strategy, qty=qty, avg_cost=avg_cost,
                opened_at=opened_at, status="open", note=note,
            )
            session.add(position)
            session.flush()

            session.add(
                Trade(position_id=position.id, side="buy", qty=qty, price=price,
                      fee=fee, traded_at=opened_at, note="建仓")
            )
            created: list[str] = []

            def _add_rule(rule_type: str, params: dict) -> None:
                session.add(
                    ExitRule(position_id=position.id, type=rule_type, params_json=params)
                )
                created.append(rule_type)

            if rules.take_profit_pct:
                _add_rule("fixed_take_profit", {"pct": rules.take_profit_pct})
            if rules.stop_loss_pct:
                _add_rule("fixed_stop_loss", {"pct": rules.stop_loss_pct})
            if rules.trailing_drawdown_pct:
                _add_rule("trailing_stop", {"drawdown_pct": rules.trailing_drawdown_pct})
            if rules.max_holding_days:
                _add_rule("max_holding", {"days": rules.max_holding_days})
            if rules.daily_move_pct:
                _add_rule("daily_move", {"pct": rules.daily_move_pct})

            from .journal import plan_context, record_trade

            record_trade(
                session,
                source="position",
                ref_id=position.id,
                code=code,
                name=name or code,
                action="建仓",
                qty=qty,
                price=price,
                amount=round(price * qty, 2),
                fee=fee,
                reason=note or f"手动建仓（{STRATEGY_LABELS.get(strategy, strategy)}）",
                ts=opened_at,
                context={
                    "策略": STRATEGY_LABELS.get(strategy, strategy),
                    "退出规则": {
                        "止盈%": rules.take_profit_pct,
                        "止损%": rules.stop_loss_pct,
                        "移动止盈回撤%": rules.trailing_drawdown_pct,
                        "最长持有天": rules.max_holding_days,
                    },
                    "计划": plan_context(session, code),
                },
            )

            session.commit()
            result = {"position_id": position.id, "avg_cost": round(avg_cost, 4), "rules": created}
        log.info("建仓 #%s %s strategy=%s qty=%s", result["position_id"], code, strategy, qty)
        return result

    def increase_position(
        self,
        position_id: int,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        traded_at: dt.datetime | None = None,
        note: str = "",
    ) -> dict:
        """加仓：更新数量与摊薄成本，写交易与日志（用于实盘指令回填）。"""
        if qty <= 0 or price <= 0:
            raise ValidationError("数量与价格必须大于 0")
        if fee < 0:
            raise ValidationError("手续费不能为负")
        traded_at = traded_at or dt.datetime.now()
        with self._sf() as session:
            position = session.get(Position, position_id)
            if position is None:
                raise NotFoundError(f"持仓不存在: #{position_id}")
            if position.status != "open":
                raise ValidationError("持仓已平仓，无法加仓")
            old_amount = position.avg_cost * position.qty
            new_qty = position.qty + qty
            position.qty = round(new_qty, 6)
            position.avg_cost = round((old_amount + price * qty + fee) / new_qty, 6)
            session.add(
                Trade(
                    position_id=position.id, side="buy", qty=qty, price=price,
                    fee=fee, traded_at=traded_at, note="加仓",
                )
            )
            from .journal import plan_context, record_trade

            instrument = session.get(Instrument, position.code)
            record_trade(
                session,
                source="position",
                ref_id=position.id,
                code=position.code,
                name=(instrument.name if instrument else position.code),
                action="加仓",
                qty=qty,
                price=price,
                amount=round(price * qty, 2),
                fee=fee,
                reason=note or "实盘指令加仓",
                ts=traded_at,
                context={
                    "成本": round(position.avg_cost, 4),
                    "持仓数量": position.qty,
                    "计划": plan_context(session, position.code),
                },
            )
            session.commit()
            result = {
                "position_id": position.id,
                "qty": position.qty,
                "avg_cost": round(position.avg_cost, 4),
            }
        log.info("加仓 #%s qty=%s cost=%s", position_id, qty, result["avg_cost"])
        return result

    # -------------------------------------------------- 卖出
    def sell(
        self,
        position_id: int,
        *,
        qty: float,
        price: float,
        fee: float = 0.0,
        sold_at: dt.datetime | None = None,
    ) -> dict:
        if qty <= 0 or price <= 0:
            raise ValidationError("数量与价格必须大于 0")
        if fee < 0:
            raise ValidationError("手续费不能为负")
        sold_at = sold_at or dt.datetime.now()
        with self._sf() as session:
            position = session.get(Position, position_id)
            if position is None:
                raise NotFoundError(f"持仓不存在: #{position_id}")
            if position.status != "open":
                raise ValidationError("持仓已平仓，无法再卖出")
            if qty > position.qty + 1e-9:
                raise ValidationError(f"卖出数量 {qty:g} 超过持仓 {position.qty:g}")

            realized = (price - position.avg_cost) * qty - fee
            session.add(
                Trade(position_id=position.id, side="sell", qty=qty, price=price,
                      fee=fee, traded_at=sold_at)
            )
            position.qty = round(position.qty - qty, 6)
            if position.qty <= 1e-9:
                position.qty = 0.0
                position.status = "closed"
                position.closed_at = sold_at
                for rule in session.scalars(
                    select(ExitRule).where(ExitRule.position_id == position.id)
                ):
                    rule.status = "disabled"

            from .journal import plan_context, record_trade

            instrument = session.get(Instrument, position.code)
            fully_closed = position.status == "closed"
            pnl_pct = (price / position.avg_cost - 1) * 100 if position.avg_cost else 0.0
            record_trade(
                session,
                source="position",
                ref_id=position.id,
                code=position.code,
                name=(instrument.name if instrument else position.code),
                action="清仓" if fully_closed else "减仓",
                qty=qty,
                price=price,
                amount=round(price * qty, 2),
                fee=fee,
                realized_pnl=round(realized, 2),
                reason=f"手动卖出（{'全部' if fully_closed else '部分'}）",
                ts=sold_at,
                context={
                    "成本": round(position.avg_cost, 4),
                    "浮动盈亏%": round(pnl_pct, 2),
                    "持有天数": (sold_at - position.opened_at).days,
                    "剩余数量": position.qty,
                    "计划": plan_context(session, position.code),
                },
            )
            session.commit()
            result = {
                "realized_pnl": round(realized, 2),
                "remaining_qty": position.qty,
                "status": position.status,
            }
        log.info("卖出 #%s qty=%s 盈亏=%s", position_id, qty, result["realized_pnl"])
        return result

    # -------------------------------------------------- 分红（摊薄成本）
    def add_dividend(
        self,
        position_id: int,
        *,
        cash_amount: float,
        ex_date: dt.date | None = None,
        note: str = "",
    ) -> dict:
        if cash_amount <= 0:
            raise ValidationError("分红金额必须大于 0")
        ex_date = ex_date or dt.date.today()
        with self._sf() as session:
            position = session.get(Position, position_id)
            if position is None:
                raise NotFoundError(f"持仓不存在: #{position_id}")
            session.add(
                Dividend(position_id=position.id, ex_date=ex_date,
                         cash_amount=cash_amount, note=note)
            )
            if position.qty > 0:
                new_cost = max(0.0, (position.avg_cost * position.qty - cash_amount) / position.qty)
                position.avg_cost = round(new_cost, 6)
            from .journal import record_trade

            instrument = session.get(Instrument, position.code)
            record_trade(
                session,
                source="position",
                ref_id=position.id,
                code=position.code,
                name=(instrument.name if instrument else position.code),
                action="分红",
                amount=round(cash_amount, 2),
                reason=note or "现金分红（摊薄成本）",
                context={"除息日": ex_date.isoformat(), "除息后成本": position.avg_cost},
            )
            session.commit()
            result = {"new_avg_cost": position.avg_cost}
        log.info("分红 #%s amount=%s 新成本=%s", position_id, cash_amount, result["new_avg_cost"])
        return result

    # -------------------------------------------------- 拆股（手动修正）
    def apply_split(self, position_id: int, *, factor: float, note: str = "") -> dict:
        if factor <= 0:
            raise ValidationError("拆股比例必须大于 0")
        with self._sf() as session:
            position = session.get(Position, position_id)
            if position is None:
                raise NotFoundError(f"持仓不存在: #{position_id}")
            position.qty = round(position.qty * factor, 6)
            position.avg_cost = round(position.avg_cost / factor, 6)
            session.add(
                CorporateAction(position_id=position.id, type="split",
                               detail_json={"factor": factor, "note": note})
            )
            from .journal import record_trade

            instrument = session.get(Instrument, position.code)
            record_trade(
                session,
                source="position",
                ref_id=position.id,
                code=position.code,
                name=(instrument.name if instrument else position.code),
                action="拆股",
                qty=position.qty,
                price=position.avg_cost,
                reason=note or f"拆股修正（1 拆 {factor:g}）",
                context={"比例": factor, "修正后数量": position.qty, "修正后成本": position.avg_cost},
            )
            session.commit()
            result = {"qty": position.qty, "avg_cost": position.avg_cost}
        log.info("拆股 #%s factor=%s", position_id, factor)
        return result

    # -------------------------------------------------- 查询
    def get_open_positions(self) -> list[Position]:
        with self._sf() as session:
            return list(
                session.scalars(
                    select(Position).where(Position.status == "open").order_by(Position.opened_at)
                )
            )

    def get_position(self, position_id: int) -> Position | None:
        with self._sf() as session:
            return session.get(Position, position_id)

    def list_positions(self, include_closed: bool = False) -> list[Position]:
        with self._sf() as session:
            stmt = select(Position).order_by(Position.opened_at)
            if not include_closed:
                stmt = stmt.where(Position.status == "open")
            return list(session.scalars(stmt))

    def list_rules(self, position_id: int) -> list[ExitRule]:
        with self._sf() as session:
            return list(
                session.scalars(
                    select(ExitRule)
                    .where(ExitRule.position_id == position_id)
                    .order_by(ExitRule.id)
                )
            )
