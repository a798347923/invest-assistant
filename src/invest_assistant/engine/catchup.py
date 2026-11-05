"""补报与追赶（US-4.5）：

1) 未送达重发：pending/failed 提醒由 NotificationFlow 统一处理（含过期标注）；
2) 离线漏触发回放：服务启动时，对已武装的规则，用「上次轮询后的日线 high/low」
   重放评估（与实盘同一 evaluate），生成 is_catchup=1 的补报提醒。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..datasource import MarketDataService
from ..db.models import Alert, ExitRule
from ..db.session import get_session_factory
from ..domain.positions import PositionService
from ..domain.rules import LATCHED_TYPES, RuleContext, evaluate
from .notify_flow import NotificationFlow
from .state import get_last_poll_at, set_last_poll_at

log = logging.getLogger(__name__)

REPLAY_TYPES = ("fixed_take_profit", "fixed_stop_loss", "trailing_stop", "daily_move", "max_holding")


class CatchupService:
    def __init__(self, data=None, positions=None, session_factory=None, flow=None):
        self._sf = session_factory or get_session_factory()
        self.data = data or MarketDataService(session_factory=self._sf)
        self.positions = positions or PositionService(session_factory=self._sf)
        self.flow = flow or NotificationFlow(session_factory=self._sf)

    def run(self, now: dt.datetime | None = None) -> dict:
        now = now or dt.datetime.now()
        report = {"missed_alerts": 0, "sent": 0, "failed": 0, "expired": 0, "errors": []}

        last_poll = get_last_poll_at(self._sf)
        if last_poll is not None:
            self._replay_missed(last_poll, now, report)
            set_last_poll_at(self._sf, now)

        dispatch = self.flow.process_pending()
        report["sent"] = dispatch.get("sent", 0)
        report["failed"] = dispatch.get("failed", 0)
        report["expired"] = dispatch.get("expired", 0)
        if report["missed_alerts"]:
            log.info("补报完成: %s", report)
        return report

    # -------------------------------------------------- 离线回放
    def _replay_missed(self, last_poll: dt.datetime, now: dt.datetime, report: dict) -> None:
        start_date = last_poll.date() + dt.timedelta(days=1)
        if start_date > now.date():
            return
        from ..db.models import Instrument

        with self._sf() as session:
            types = {i.code: i.type for i in session.scalars(select(Instrument))}
        for position in self.positions.get_open_positions():
            if types.get(position.code) == "otc_fund":
                continue  # 场外基金走日频净值（EOD 体检），不做盘中回放
            with self._sf() as session:
                rules = session.scalars(
                    select(ExitRule).where(
                        ExitRule.position_id == position.id,
                        ExitRule.status == "armed",
                        ExitRule.type.in_(REPLAY_TYPES),
                    )
                ).all()
                if not rules:
                    continue
                try:
                    bars = self.data.get_daily_bars(position.code, start_date, now.date(), adjust="qfq")
                except Exception as exc:  # noqa: BLE001
                    report["errors"].append(f"{position.code}: {exc}")
                    continue
                if not bars:
                    continue

                for rule in rules:
                    state, trigger, trigger_date = self._replay_rule(rule, position, bars, now)
                    rule.state_json = state
                    if trigger is None:
                        continue
                    if rule.type in LATCHED_TYPES:
                        rule.status = "triggered"
                    key = f"catchup-rule-{rule.id}-{trigger_date.isoformat()}"
                    exists = session.scalar(select(Alert).where(Alert.dedup_key == key))
                    if exists:
                        continue
                    triggered_at = dt.datetime.combine(trigger_date, dt.time(15, 0))
                    session.add(
                        Alert(
                            position_id=position.id,
                            rule_id=rule.id,
                            kind="trigger",
                            triggered_at=triggered_at,
                            trigger_price=trigger.trigger_price,
                            context_json={
                                "code": position.code,
                                "name": None,
                                "strategy": position.strategy,
                                "rule_type": rule.type,
                                "reason": f"[{trigger_date.isoformat()}] {trigger.reason}",
                                "price": trigger.trigger_price,
                                "cost": position.avg_cost,
                                "qty": position.qty,
                            },
                            status="pending",
                            is_catchup=True,
                            dedup_key=key,
                        )
                    )
                    report["missed_alerts"] += 1
                session.commit()

    @staticmethod
    def _replay_rule(rule: ExitRule, position, bars, now: dt.datetime):
        """逐日回放；返回 (新状态, 触发或 None, 触发日)。"""
        state = dict(rule.state_json or {})
        if rule.type == "max_holding":
            ctx = RuleContext(
                price=position.avg_cost, cost=position.avg_cost, now=now,
                opened_at=position.opened_at,
            )
            state, trigger = evaluate("max_holding", rule.params_json, state, ctx)
            return state, trigger, now.date()

        prev_close = None
        for bar in bars:
            ctx = RuleContext(
                price=bar.close,
                cost=position.avg_cost,
                now=dt.datetime.combine(bar.date, dt.time(15, 0)),
                opened_at=position.opened_at,
                prev_close=prev_close,
                high=bar.high,
                low=bar.low,
            )
            state, trigger = evaluate(rule.type, rule.params_json, state, ctx)
            prev_close = bar.close
            if trigger is not None:
                return state, trigger, bar.date
        return state, None, now.date()
