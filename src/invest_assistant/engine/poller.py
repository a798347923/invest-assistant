"""盘中轮询：持仓规则 + 自选池监控 → 提醒落库。

设计要点：
- 持仓（A股/ETF）走退出规则评估；**场外基金持仓不参与盘中**（走日频净值体检）；
- **自选池参与盘中监控**：
  - 异动提醒：日内涨跌幅超阈值（单项可覆盖，默认取 config.watchlist.default_alert_pct；设 0 关闭）；
  - 到价提醒：现价 ≥ 上方价 / ≤ 下方价；
  - **AI 买入计划提醒**：依据最新分析卡的 plan（domain.plan 解析）——现价进入建议买入区间
    →「AI 买入机会」；跌破失效价 →「AI 计划失效」；单项可关（alert_plan=false）；
  - **模拟盘**：买点自动买入；持仓现价/名称随轮询刷新（面板与收益统计即时可见）；
    **盘中跌破失效价立即纪律清仓**（1 分钟内，不再等日报次日执行）；
  - 场外基金自选使用盘中估值（best-effort，未收录则静默跳过）；
  - 每日每条件（异动/上方/下方/计划）最多提醒一次（dedup_key 幂等）；
- 纯本地判断（0 LLM 调用）；自选提醒不参与 AI 解读（轻量模板消息）；
- 数据源失败：影响持仓监控时按轮记录连续失败（≥3 轮首告，每 24 轮复提醒）；仅自选失败不升级为系统告警；
- 误标类型的场外基金（报价源全部无返回时）自动重分类。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select

from ..config import get_settings
from ..datasource import DataUnavailable, MarketDataService
from ..db.models import Alert, AnalysisCard, ExitRule, Instrument, KvSetting, OrderIntent, PaperPosition, WatchlistItem
from ..db.session import get_session_factory
from ..domain.indicators import atr
from ..domain.plan import parse_plan_levels
from ..domain.positions import PositionService
from ..domain.rules import LATCHED_TYPES, RuleContext, evaluate
from .paper import PaperService
from .state import set_last_poll_at
from .triggers import (
    plan_triggers,
    pullback_enhanced_problem,
    trading_days_held,
    watch_triggers,
)

log = logging.getLogger(__name__)

_STREAK_KEY = "poll_fail_streak"
_NOTIFY_AT = 3  # 首次告警阈值（连续失败轮数）
_NOTIFY_EVERY = 24  # 之后每 N 轮复提醒一次（≈4 小时）


@dataclass
class PollResult:
    started_at: dt.datetime
    skipped_reason: str = ""
    rules_checked: int = 0
    quotes_count: int = 0
    triggered: list[dict] = field(default_factory=list)
    missing_codes: list[str] = field(default_factory=list)
    fund_skipped: list[str] = field(default_factory=list)
    reclassified: list[str] = field(default_factory=list)
    watch_total: int = 0
    watch_checked: int = 0
    watch_triggered: list[dict] = field(default_factory=list)
    watch_missing: list[str] = field(default_factory=list)
    paper_actions: list[dict] = field(default_factory=list)
    error: str = ""
    fail_streak: int = 0
    system_warning: str = ""

    def summary(self) -> str:
        if self.skipped_reason:
            extra = f"（场外基金 {len(self.fund_skipped)} 只走日频净值）" if self.fund_skipped else ""
            return f"跳过（{self.skipped_reason}）{extra}"
        parts = [f"规则 {self.rules_checked} 条", f"行情 {self.quotes_count} 个"]
        if self.watch_total:
            parts.append(f"自选 {self.watch_checked}/{self.watch_total}")
        if self.triggered:
            parts.append(f"触发 {len(self.triggered)}")
        if self.watch_triggered:
            parts.append(f"自选触发 {len(self.watch_triggered)}")
        if self.paper_actions:
            parts.append(f"模拟盘 {len(self.paper_actions)}")
        if self.missing_codes:
            parts.append(f"缺行情 {len(self.missing_codes)}")
        if self.reclassified:
            parts.append(f"重分类为场外基金 {len(self.reclassified)}")
        if self.error:
            parts.append(f"错误: {self.error}")
        return "；".join(parts)


def in_windows(t: dt.time, windows: list[str]) -> bool:
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


class Poller:
    def __init__(
        self, data=None, positions=None, session_factory=None, settings=None, fund=None, paper=None
    ):
        self._sf = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        self.data = data or MarketDataService(session_factory=self._sf)
        self.positions = positions or PositionService(session_factory=self._sf)
        self._fund = fund  # 懒加载 FundClient（测试注入）
        self._paper = paper or PaperService(session_factory=self._sf, settings=self._settings)
        self._parking_service = None  # 弱市停泊服务（懒加载；默认关闭）
        poll_cfg = self._settings.app_config().get("poll") or {}
        self._windows: list[str] = poll_cfg.get("windows") or ["09:30-11:30", "13:00-15:00"]

    # -------------------------------------------------- 主流程
    def run_once(self, now: dt.datetime | None = None, skip_time_check: bool = False) -> PollResult:
        now = now or dt.datetime.now()
        result = PollResult(started_at=now)

        if not skip_time_check:
            if not self.data.is_trading_day(now.date()):
                result.skipped_reason = "非交易日"
                return result
            if not in_windows(now.time(), self._windows):
                result.skipped_reason = "非监控时段"
                return result

        set_last_poll_at(self._sf, now)  # 记录"服务已检查"（即使无盘中标的）

        open_positions = self.positions.get_open_positions()
        parking = self._parking()
        parking_codes = parking.codes if parking.enabled else []
        with self._sf() as session:
            watch_items = list(session.scalars(select(WatchlistItem)))
            paper_positions = self._paper.open_positions(session) if self._paper.enabled else []
        if (
            not open_positions and not watch_items and not paper_positions
            and not parking_codes
        ):
            result.skipped_reason = "无持仓与自选"
            return result

        types = self._instrument_types()
        funds = [p for p in open_positions if types.get(p.code) == "otc_fund"]
        intraday = [p for p in open_positions if types.get(p.code, "a_share") != "otc_fund"]
        result.fund_skipped = [p.code for p in funds]
        result.watch_total = len(watch_items)

        position_codes = [p.code for p in intraday]
        watch_codes = [w.code for w in watch_items]
        paper_codes = [p.code for p in paper_positions]
        stock_codes = sorted(
            set(position_codes) | set(watch_codes) | set(paper_codes) | set(parking_codes)
        )
        if not stock_codes:
            result.skipped_reason = "无盘中持仓（场外基金走日频净值检查）"
            return result

        quotes: dict = {}
        fetch_error = ""
        try:
            quotes = self.data.get_quotes(stock_codes)
        except DataUnavailable:
            # 仅对持仓尝试重分类（自选失败不升级为系统告警）
            with self._sf() as session:
                reclassified = []
                for code in position_codes:
                    if self._maybe_reclassify(session, code):
                        reclassified.append(code)
                if reclassified:
                    session.commit()
            if reclassified:
                result.reclassified = reclassified
                log.info("自动重分类为场外基金: %s", reclassified)
            remaining = [c for c in stock_codes if c not in set(reclassified)]
            if remaining:
                try:
                    quotes = self.data.get_quotes(remaining)
                except DataUnavailable as exc2:
                    fetch_error = str(exc2)
            if fetch_error:
                if intraday:
                    self._handle_data_failure(result, fetch_error)
                    return result
                result.error = fetch_error
                log.warning("自选行情暂不可用: %s", fetch_error)

        if not fetch_error:
            self._reset_fail_streak()
        result.quotes_count = len(quotes)
        reclassified_set = set(result.reclassified)

        # 模拟盘：执行上一交易日 AI 建议 + 持仓估值随轮询刷新 + 盘中纪律止损（面板/统计即时可见）
        if self._paper.enabled and quotes:
            self._execute_paper_actions(now, quotes, result)
            self._refresh_paper_prices(paper_positions, quotes)
            self._check_paper_stops(now, paper_positions, quotes, result)
            if self._paper.apply_replay_v3:
                self._check_paper_targets(now, paper_positions, quotes, result)

        # 弱市停泊（独立资金层，不占股票槽位；默认关闭）
        if quotes:
            self._rebalance_parking(now, quotes, result)

        with self._sf() as session:
            # 1) 持仓规则
            for position in intraday:
                if position.code in reclassified_set:
                    continue
                quote = quotes.get(position.code)
                if quote is None or quote.price <= 0:
                    if self._maybe_reclassify(session, position.code):
                        session.commit()
                        result.reclassified.append(position.code)
                        continue
                    result.missing_codes.append(position.code)
                    continue
                self._mirror_live_partial(session, now, position, quote.price)
                rules = session.scalars(
                    select(ExitRule).where(
                        ExitRule.position_id == position.id, ExitRule.status == "armed"
                    )
                ).all()
                for rule in rules:
                    ctx = RuleContext(
                        price=quote.price,
                        cost=position.avg_cost,
                        now=now,
                        opened_at=position.opened_at,
                        prev_close=quote.prev_close,
                    )
                    new_state, trigger = evaluate(rule.type, rule.params_json, rule.state_json, ctx)
                    rule.state_json = new_state
                    result.rules_checked += 1
                    if trigger is None:
                        continue
                    if rule.type in LATCHED_TYPES:
                        rule.status = "triggered"
                    session.add(
                        Alert(
                            position_id=position.id,
                            rule_id=rule.id,
                            kind="trigger",
                            triggered_at=now,
                            trigger_price=trigger.trigger_price,
                            context_json={
                                "code": position.code,
                                "name": None,
                                "strategy": position.strategy,
                                "rule_type": rule.type,
                                "reason": trigger.reason,
                                "price": quote.price,
                                "cost": position.avg_cost,
                                "qty": position.qty,
                                "source": quote.source,
                            },
                            status="pending",
                            dedup_key=f"rule-{rule.id}-{new_state.get('last_triggered_at')}",
                        )
                    )
                    result.triggered.append(
                        {
                            "position_id": position.id,
                            "code": position.code,
                            "rule_type": rule.type,
                            "reason": trigger.reason,
                        }
                    )
                    self._mirror_live_sell(
                        session, now, position, quote.price, trigger.reason, rule.type
                    )
            # 2) 自选池异动/到价监控
            if watch_items:
                self._evaluate_watchlist(session, now, watch_items, quotes, result)
            session.commit()

        if result.triggered:
            log.info("轮询触发 %s 条: %s", len(result.triggered), result.triggered)
        return result

    # -------------------------------------------------- 模拟盘
    def _live_service(self):
        """惰性获取实盘服务；未启用（默认）时返回 None，不影响轮询。"""
        try:
            from .live import LiveService

            service = LiveService(session_factory=self._sf, settings=self._settings)
        except Exception:  # noqa: BLE001 — 实盘配置异常不应影响轮询
            return None
        if not service.live.enabled or not service.live.semi_auto_enabled:
            return None
        return service

    def _mirror_live_buy(
        self, session, now, code: str, name: str, price: float, reason: str,
        stop_price: float | None = None,
    ) -> None:
        """模拟盘买入成功 → 生成实盘半自动买入指令（预算 = live.max_order_amount）。"""
        service = self._live_service()
        if service is None or not service.live.mirror_paper_entries:
            return
        if price <= 0:
            return
        if service.live.announcement_filter:
            try:
                announcements = self.data.get_announcements(
                    code, limit=service.live.announcement_limit
                )
            except Exception:  # noqa: BLE001 — 公告取不到则放行（不因数据故障错杀）
                announcements = []
            hits = [
                item
                for item in announcements
                if any(keyword in item for keyword in service.live.announcement_block_keywords)
            ]
            if hits:
                log.info("实盘买入跳过 %s：近期负面公告 %s", code, hits[:2])
                return
        qty = int(service.live.max_order_amount / (price * 100)) * 100
        if qty < 100:
            return
        try:
            service.create_intent(
                code, "buy", qty, price,
                name=name or "", ref_price=price, source="paper",
                reason=f"模拟盘买入：{reason}", now=now,
                idempotency_key=f"live-{now:%Y%m%d}-{code}-buy",
                stop_price=stop_price, session=session,
            )
        except Exception:  # noqa: BLE001 — 指令生成失败不影响轮询
            log.warning("实盘买入指令生成失败 %s", code, exc_info=True)

    def _mirror_live_sell(
        self, session, now, position, price: float, reason: str, rule_type: str
    ) -> None:
        """实盘持仓规则触发 → 生成实盘半自动卖出指令（整仓）。"""
        service = self._live_service()
        if service is None or price <= 0:
            return
        qty = int(round(position.qty))
        if qty < 1:
            return
        try:
            service.create_intent(
                position.code, "sell", qty, price,
                ref_price=price, source="position",
                reason=f"持仓规则触发（{rule_type}）：{reason}", now=now,
                ref_position_id=position.id,
                idempotency_key=(
                    f"live-{now:%Y%m%d}-{position.code}-sell-{position.id}-{rule_type}"
                ),
                session=session,
            )
        except Exception:  # noqa: BLE001
            log.warning("实盘卖出指令生成失败 %s", position.code, exc_info=True)

    def _mirror_live_partial(self, session, now, position, price: float) -> None:
        """P2：系统管理的实盘持仓到 +10% 时生成减半卖出指令（每个仓位仅一次）。"""
        service = self._live_service()
        if service is None or price <= 0:
            return
        pct = service.live.partial_take_profit_pct
        fraction = service.live.partial_fraction
        if pct <= 0 or fraction <= 0:
            return
        if not (position.note or "").startswith("实盘指令"):
            return
        if price < position.avg_cost * (1 + pct / 100):
            return
        key = f"live-partial-{position.id}"
        existing = session.scalar(
            select(OrderIntent.id).where(OrderIntent.idempotency_key == key)
        )
        if existing is not None:
            return
        qty = int(position.qty * fraction / 100) * 100
        if qty < 100:
            return
        try:
            service.create_intent(
                position.code, "sell", qty, price,
                ref_price=price, source="position",
                reason=(
                    f"分批止盈：+{pct:g}% 减半"
                    f"（余仓目标 +{service.live.default_take_profit_pct:g}%）"
                ),
                now=now, ref_position_id=position.id,
                idempotency_key=key, session=session,
            )
        except Exception:  # noqa: BLE001
            log.warning("实盘分批止盈指令生成失败 %s", position.code, exc_info=True)

    def _check_paper_targets(self, now, paper_positions, quotes, result: PollResult) -> None:
        """v12 机械止盈：到 +10% 减半；到达目标价清仓；持有满 30 交易日清仓。"""
        if not paper_positions:
            return
        with self._sf() as session:
            for p in paper_positions:
                quote = quotes.get(p.code)
                if quote is None or quote.price <= 0:
                    continue
                row = session.get(PaperPosition, p.id)
                if row is None or row.status != "open":
                    continue
                price = quote.price
                if row.partial_price and not row.partial_taken and price >= row.partial_price:
                    qty = int(row.qty * 0.5 / 100) * 100
                    if qty >= 100:
                        summary = self._paper.sell_mechanical(
                            session=session, position=row, price=price, qty=qty,
                            reason=f"分批止盈：到 +10% 减半（{row.partial_price:g}）",
                            now=now,
                            context={
                                "触发": "分批止盈", "价位": row.partial_price,
                                "剩余": row.qty,
                            },
                        )
                        row.partial_taken = True
                        result.paper_actions.append(
                            {
                                "code": row.code,
                                "action": "分批止盈",
                                "qty": summary["qty"],
                                "price": summary["price"],
                            }
                        )
                        self._add_paper_exit_alert(
                            session, row, "paper_partial", price,
                            f"到 +10% 减半 {summary['qty']:g} 股 @ {summary['price']:.4f}", now,
                        )
                if row.status == "open" and row.target_price and price >= row.target_price:
                    summary = self._paper.sell_mechanical(
                        session=session, position=row, price=price, qty=row.qty,
                        reason=f"目标止盈：到达 {row.target_price:g}",
                        now=now,
                        context={"触发": "目标止盈", "价位": row.target_price},
                    )
                    result.paper_actions.append(
                        {
                            "code": row.code,
                            "action": "目标止盈",
                            "qty": summary["qty"],
                            "price": summary["price"],
                        }
                    )
                    self._add_paper_exit_alert(
                        session, row, "paper_target", price,
                        f"到达目标止盈价 {row.target_price:g}，清仓 {summary['qty']:g} 股", now,
                    )
                if row.status == "open" and row.target_price and trading_days_held(row.opened_at, now) >= 30:
                    held = trading_days_held(row.opened_at, now)
                    summary = self._paper.sell_mechanical(
                        session=session, position=row, price=price, qty=row.qty,
                        reason="持有到期（30 交易日）", now=now,
                        context={"触发": "持有到期", "持有交易日": held},
                    )
                    result.paper_actions.append(
                        {
                            "code": row.code,
                            "action": "持有到期",
                            "qty": summary["qty"],
                            "price": summary["price"],
                        }
                    )
                    self._add_paper_exit_alert(
                        session, row, "paper_timeout", price,
                        f"持有 {held} 个交易日到期，清仓 {summary['qty']:g} 股", now,
                    )
            session.commit()

    @staticmethod
    def _add_paper_exit_alert(session, row, alert_type: str, price: float, reason: str, now) -> None:
        key = f"{alert_type}-{row.id}-{now:%Y%m%d%H%M}"
        if session.scalar(select(Alert.id).where(Alert.dedup_key == key)) is not None:
            return
        session.add(
            Alert(
                kind="watch",
                triggered_at=now,
                trigger_price=round(price, 4),
                context_json={
                    "code": row.code,
                    "name": row.name or row.code,
                    "reason": reason,
                    "price": price,
                    "source": "paper",
                    "alert_type": alert_type,
                },
                status="pending",
                dedup_key=key,
            )
        )

    def _check_paper_stops(self, now, paper_positions, quotes, result: PollResult) -> None:
        """模拟盘盘中纪律止损：跌破最新卡片失效价（或持仓期间已触发过计划失效）→ 立即清仓。"""
        if not paper_positions:
            return
        with self._sf() as session:
            cards = self._latest_plan_cards(session, [p.code for p in paper_positions])
            for p in paper_positions:
                quote = quotes.get(p.code)
                if quote is None or quote.price <= 0:
                    continue
                row = session.get(PaperPosition, p.id)
                if row is None or row.status != "open":
                    continue
                card = cards.get(row.code)
                plan = parse_plan_levels(card.content_json) if card is not None else None
                plan_stop = (plan or {}).get("stop")
                mechanical = bool(row.stop_price)  # v3：入场时固化的机械止损（形态差异化）
                effective = row.stop_price or plan_stop
                if not effective:
                    continue
                label = "机械止损价" if mechanical else "失效价"
                breached = quote.price <= effective
                breach_reason = f"现价 {quote.price:.4f} 跌破{label} {effective:g}"
                if not breached and not mechanical:
                    fired = session.scalar(
                        select(Alert.id)
                        .where(
                            Alert.context_json["code"].as_string() == row.code,
                            Alert.context_json["alert_type"].as_string() == "plan_stop",
                            Alert.triggered_at >= row.opened_at,
                        )
                        .limit(1)
                    )
                    if fired is None:
                        continue
                    breached = True
                    breach_reason = f"计划失效提醒已触发（失效价 {effective:g}，现价 {quote.price:.4f}）"
                if not breached:
                    continue
                summary = self._paper.stop_out(
                    session=session,
                    position=row,
                    price=quote.price,
                    reason=f"纪律止损清仓：{breach_reason}",
                    now=now,
                    context={"触发": "纪律止损", "依据": breach_reason, "计划": plan or {}},
                    quote=quote,
                )
                if summary is not None and summary.get("vetoed"):
                    result.paper_actions.append(
                        {
                            "code": row.code,
                            "action": "AI否决止损",
                            "reason": summary.get("reason", ""),
                        }
                    )
                    continue
                key = f"paper-stop-{row.id}-{now:%Y%m%d}"
                if not session.scalar(select(Alert.id).where(Alert.dedup_key == key)):
                    session.add(
                        Alert(
                            kind="watch",
                            triggered_at=now,
                            trigger_price=quote.price,
                            context_json={
                                "code": row.code,
                                "name": row.name or row.code,
                                "reason": (
                                    f"{breach_reason}，模拟盘纪律清仓 {summary['qty']:g} 股 "
                                    f"@ {summary['price']:.4f}，本笔盈亏 {summary['realized_pnl']:+.0f} 元"
                                ),
                                "price": quote.price,
                                "source": "paper",
                                "alert_type": "paper_stop",
                                "plan": plan or {},
                            },
                            status="pending",
                            dedup_key=key,
                        )
                    )
                result.paper_actions.append(
                    {
                        "code": row.code,
                        "action": "止损清仓",
                        "qty": summary["qty"],
                        "price": summary["price"],
                    }
                )
            session.commit()

    def _refresh_paper_prices(self, paper_positions, quotes) -> None:
        """模拟盘持仓现价/名称跟随盘中轮询刷新（供面板与组合统计即时显示）。"""
        with self._sf() as session:
            updated = False
            for p in paper_positions:
                quote = quotes.get(p.code)
                if quote is None or quote.price <= 0:
                    continue
                row = session.get(PaperPosition, p.id)
                if row is None or row.status != "open":
                    continue
                row.last_price = quote.price
                if quote.name:
                    row.name = quote.name
                updated = True
            if updated:
                session.commit()

    def _execute_paper_actions(self, now, quotes, result: PollResult) -> None:
        """执行上一交易日日报中的建议，并生成提醒（真实监控不受其失败影响）。"""
        try:
            executed = self._paper.execute_pending(quotes, now=now)
            executed += self._paper.retry_deferred_adds(quotes, now=now)
        except Exception:  # noqa: BLE001
            log.exception("模拟盘执行建议失败")
            return
        if not executed:
            return
        with self._sf() as session:
            for item in executed:
                action = str(item.get("action") or "")
                code = str(item.get("code") or "")
                if action == "过期作废":
                    result.paper_actions.append(item)
                    continue
                key = f"paper-exec-{item.get('report_date', '')}-{code}-{action}"
                if session.scalar(select(Alert.id).where(Alert.dedup_key == key)):
                    continue
                reason = (
                    f"模拟盘执行 AI 建议：{action} {item.get('qty', 0):g} 股 @ {item.get('price', 0):.4f}"
                )
                if item.get("realized_pnl") is not None:
                    reason += f"，本笔盈亏 {item['realized_pnl']:+.0f} 元"
                reason += f"（{item.get('report_date', '')} 日报）"
                session.add(
                    Alert(
                        kind="watch",
                        triggered_at=now,
                        trigger_price=item.get("price"),
                        context_json={
                            "code": code,
                            "name": item.get("name") or code,
                            "reason": reason,
                            "price": item.get("price"),
                            "source": "paper",
                            "alert_type": "paper_exec",
                        },
                        status="pending",
                        dedup_key=key,
                    )
                )
                result.paper_actions.append(item)
            session.commit()

    # -------------------------------------------------- 自选监控
    def _evaluate_watchlist(self, session, now, watch_items, quotes, result: PollResult) -> None:
        watch_cfg = self._settings.app_config().get("watchlist") or {}
        try:
            default_pct = float(watch_cfg.get("default_alert_pct", 5.0))
        except (TypeError, ValueError):
            default_pct = 5.0
        suppress_plan_alerts = bool(watch_cfg.get("suppress_plan_alerts_in_weak", True))
        movement_alerts = bool(watch_cfg.get("movement_alerts", True))
        weak_market = self._weak_market()

        missing_codes = [w.code for w in watch_items if w.code not in quotes]
        estimates: dict = {}
        if missing_codes:
            try:
                estimates = self._fund_client().get_estimates(missing_codes)
            except Exception:  # noqa: BLE001 — 估值 best-effort
                estimates = {}

        plan_cards = self._latest_plan_cards(session, [w.code for w in watch_items])

        for item in watch_items:
            quote = quotes.get(item.code)
            estimate = estimates.get(item.code)
            if quote is not None and quote.price > 0:
                price, pct = quote.price, quote.pct_change
                name = quote.name or item.code
            elif estimate is not None and estimate.est_nav > 0:
                price, pct = estimate.est_nav, estimate.est_pct
                name = estimate.name or item.code
            else:
                result.watch_missing.append(item.code)
                continue
            result.watch_checked += 1

            # 条件 → (kind, 原因, 附加上下文)
            triggers: list[tuple[str, str, dict]] = [
                (kind, reason, {})
                for kind, reason in watch_triggers(item, default_pct, price, pct)
                if movement_alerts or kind != "pct"
            ]
            plan = None
            if item.alert_plan:
                card = plan_cards.get(item.code)
                plan = parse_plan_levels(card.content_json) if card is not None else None
                if plan is not None:
                    triggers.extend(
                        (kind, reason, {"plan": plan})
                        for kind, reason in plan_triggers(plan, price)
                    )

            for kind, reason, extra in triggers:
                if weak_market and suppress_plan_alerts and kind == "plan_buy":
                    continue  # 弱市暂停买入：不再推送「买入计划」到价提醒
                key = f"watch-{item.code}-{kind}-{now:%Y%m%d}"
                if session.scalar(select(Alert.id).where(Alert.dedup_key == key)):
                    continue
                context = {
                    "code": item.code,
                    "name": name,
                    "reason": reason,
                    "price": price,
                    "pct": pct,
                    "source": "watchlist",
                    "alert_type": kind,
                }
                context.update(extra)
                session.add(
                    Alert(
                        kind="watch",
                        triggered_at=now,
                        trigger_price=round(price, 4),
                        context_json=context,
                        status="pending",
                        dedup_key=key,
                    )
                )
                result.watch_triggered.append(
                    {"code": item.code, "kind": kind, "reason": reason}
                )

            # 模拟盘：买点触发 + 确认条件满足（v3 经验：弱市暂停、回踩须站上MA5、机械止损）→ 自动买入
            plan_buy_reason = next((r for k, r, _ in triggers if k == "plan_buy"), "")
            if quote is not None and plan is not None and plan_buy_reason:
                stop_price: float | None = None
                target_price: float | None = None
                if self._paper.apply_replay_v3:
                    style = "breakout" if "突破确认价" in plan_buy_reason else "pullback"
                    if weak_market:
                        confirmed, detail = False, "v7：弱市（双指数+滞回）暂停自动买入"
                    elif style == "pullback" and not self._raw_index_ok():
                        confirmed, detail = False, "v7：上证在 20 日线下方，回踩暂停自动买入（突破不受限）"
                    else:
                        confirmed, detail, stop_price, target_price = self._entry_gate_v3(
                            item.code, plan, quote, now, style
                        )
                else:
                    confirmed, detail = self._confirmation_check(item.code, plan, quote, now)
                if not confirmed:
                    if detail:
                        log.info("模拟盘买入等待 %s：%s", item.code, detail)
                else:
                    entry = self._paper.try_auto_entry(
                        session=session,
                        code=item.code,
                        name=name,
                        price=price,
                        reason=plan_buy_reason,
                        now=now,
                        plan=plan,
                        quote=quote,
                        stop_price=stop_price,
                        target_price=target_price,
                    )
                    if entry is not None and entry.get("vetoed"):
                        result.paper_actions.append(
                            {
                                "code": item.code,
                                "action": "AI否决买入",
                                "reason": entry.get("reason", ""),
                            }
                        )
                    elif entry is not None:
                        key = f"watch-{item.code}-paper_buy-{now:%Y%m%d}"
                        if not session.scalar(select(Alert.id).where(Alert.dedup_key == key)):
                            session.add(
                                Alert(
                                    kind="watch",
                                    triggered_at=now,
                                    trigger_price=entry["price"],
                                    context_json={
                                        "code": item.code,
                                        "name": name,
                                        "reason": (
                                            f"模拟盘自动买入 {entry['qty']:g} 股 @ {entry['price']:.4f}"
                                            f"（触发：{plan_buy_reason}）"
                                        ),
                                        "price": price,
                                        "source": "paper",
                                        "alert_type": "paper_buy",
                                        "plan": plan,
                                    },
                                    status="pending",
                                    dedup_key=key,
                                )
                            )
                        result.paper_actions.append(
                            {
                                "code": item.code,
                                "action": "自动买入",
                                "qty": entry["qty"],
                                "price": entry["price"],
                            }
                        )
                        self._mirror_live_buy(
                            session, now, item.code, name, price, plan_buy_reason,
                            stop_price=stop_price,
                        )
        if result.watch_triggered:
            log.info("自选触发 %s 条: %s", len(result.watch_triggered), result.watch_triggered)

    @staticmethod
    def _latest_plan_cards(session, codes: list[str]) -> dict:
        """自选代码 → 最新分析卡（按 id 最大，即最新一次生成）。"""
        if not codes:
            return {}
        latest_ids = (
            select(func.max(AnalysisCard.id))
            .where(AnalysisCard.code.in_(codes))
            .group_by(AnalysisCard.code)
        )
        cards = session.scalars(
            select(AnalysisCard).where(AnalysisCard.id.in_(latest_ids))
        ).all()
        return {card.code: card for card in cards}

    def _confirmation_check(self, code: str, plan: dict, quote, now) -> tuple[bool, str]:
        """自动买入前的确认条件校验（缩量/放量/收阳/站上均线；无确认条件视为通过）。"""
        from ..domain.conditions import evaluate_confirmations, infer_confirmations

        codes = [str(x) for x in (plan.get("confirm") or [])] or infer_confirmations(plan)
        if not codes:
            return True, ""
        try:
            bars = self.data.get_daily_bars(
                code, now.date() - dt.timedelta(days=90), now.date(), adjust="qfq"
            )
        except Exception as exc:  # noqa: BLE001 — 拿不到K线则保守不买
            return False, f"日线不可用，无法校验确认条件：{exc}"
        return evaluate_confirmations(
            codes,
            price=quote.price,
            open_price=quote.open,
            volume=quote.volume,
            source=quote.source,
            bars=bars,
            now=now,
        )

    def _weak_market(self) -> bool:
        """市场强弱：双指数（上证+创业板）+ 滞回带，状态持久化；未知视为非弱市（不静默停摆）。"""
        try:
            return self.data.market_regime_state() == "weak"
        except Exception:  # noqa: BLE001 — 数据 best-effort
            return False

    # -------------------------------------------------- 弱市停泊

    def _parking(self):
        if self._parking_service is None:
            from .parking import ParkingService

            self._parking_service = ParkingService(
                session_factory=self._sf, settings=self._settings
            )
        return self._parking_service

    def _rebalance_parking(self, now, quotes: dict, result: PollResult) -> None:
        """弱市把闲置现金停到 债7:金3，转强退出；状态未知时不动（避免数据抖动误平仓）。"""
        parking = self._parking()
        if not parking.enabled:
            return
        try:
            state = self.data.market_regime_state() or ""
        except Exception:  # noqa: BLE001
            state = ""
        if state not in ("weak", "strong"):
            return
        with self._sf() as session:
            parking.cache_prices(session, quotes)
            stats = self._paper.portfolio_stats(session)
            action = parking.rebalance(
                session, weak=(state == "weak"), quotes=quotes, now=now,
                available_cash=stats.get("cash", 0.0),
            )
            session.commit()
        if action.get("action") in ("buy", "sell"):
            result.paper_actions.append({"parking": action})
            log.info("弱市停泊 %s：%s", action["action"], action.get("legs"))

    def _raw_index_ok(self) -> bool:
        """v7 回踩型附加闸门：上证指数是否在 MA20 上方（数据不可用视为通过）。"""
        try:
            env = self.data.get_market_env()
            rel = ((env.get("指数") or {}).get("上证指数") or {}).get("相对20日线%")
            return rel is None or rel >= 0
        except Exception:  # noqa: BLE001 — 数据 best-effort
            return True

    def _entry_gate_v3(
        self, code: str, plan: dict, quote, now, style: str
    ) -> tuple[bool, str, float | None, float | None]:
        """v12 经验固化：回踩多确认 + 机械止损 + 机械目标价；返回 (放行, 说明, 止损价, 目标价)。

        机械止损：入场时固化——回踩型 = min(近10日最低)×0.98；突破型 = ×0.95。
        """
        from ..domain.conditions import evaluate_confirmations, infer_confirmations

        confirms = [str(x) for x in (plan.get("confirm") or [])] or infer_confirmations(plan)
        if style == "pullback":
            if not confirms:
                confirms = ["volume_shrink", "bullish"]
            if "above_ma5" not in confirms:
                confirms.append("above_ma5")  # 经验：回踩后须站上 5 日线
        elif not confirms:
            confirms = ["volume_expand", "bullish"]
        try:
            bars = self.data.get_daily_bars(
                code, now.date() - dt.timedelta(days=90), now.date(), adjust="qfq"
            )
        except Exception as exc:  # noqa: BLE001 — 拿不到K线则保守不买
            return False, f"日线不可用，无法校验确认条件：{exc}", None, None
        ok, details = evaluate_confirmations(
            confirms,
            price=quote.price,
            open_price=quote.open,
            volume=quote.volume,
            source=quote.source,
            bars=bars,
            now=now,
        )
        if ok and style == "pullback":
            # v4/v5 经验：回踩增强过滤（收复前一日高点 + MA20 上行 + 站稳 MA60）
            problem = pullback_enhanced_problem(bars, quote.price, now.date())
            if problem:
                return False, f"回踩增强过滤未通过：{problem}", None, None
        stop: float | None = None
        lows = [b.low for b in bars[-10:]]
        if len(lows) >= 10:
            if style == "breakout":
                stop = round(min(lows) * 0.95, 4)
            else:
                buffer_atr = atr(bars, 14)  # v5 经验：ATR 自适应缓冲（回踩）
                stop = (
                    round(min(lows) - float(buffer_atr), 4)
                    if buffer_atr and buffer_atr > 0
                    else round(min(lows) * 0.98, 4)
                )
        target: float | None = None
        if bars:
            if style == "breakout":
                target = round(quote.price * 1.12, 4)  # v12：突破目标 +12%
            else:
                prior = [b for b in bars if b.date < now.date()]
                if len(prior) >= 20:
                    target = round(max(b.high for b in prior[-20:]) * 1.02, 4)  # 回踩：前 20 日高 ×1.02
        return ok, "；".join(details), stop, target

    # -------------------------------------------------- 失败处理与重分类
    def _handle_data_failure(self, result: PollResult, message: str) -> None:
        result.error = message
        result.fail_streak = self._bump_fail_streak()
        should_notify = result.fail_streak >= _NOTIFY_AT and (
            result.fail_streak == _NOTIFY_AT or result.fail_streak % _NOTIFY_EVERY == 0
        )
        if should_notify:
            result.system_warning = f"行情连续 {result.fail_streak} 轮获取失败：{message}"
            log.error(result.system_warning)
        else:
            log.warning("行情获取失败（连续 %s 轮）: %s", result.fail_streak, message)

    def _maybe_reclassify(self, session, code: str) -> bool:
        """报价源无返回时，用基金数据接口识别误标类型的场外基金（估值→净值双探测）。"""
        client = self._fund_client()
        is_fund = False
        try:
            is_fund = code in client.get_estimates([code])
        except Exception:  # noqa: BLE001 — 估值服务未收录/失败
            is_fund = False
        if not is_fund:
            try:
                is_fund = bool(client.get_nav_history(code))
            except Exception:  # noqa: BLE001 — 净值接口也失败
                is_fund = False
        if not is_fund:
            return False
        instrument = session.get(Instrument, code)
        if instrument is None:
            return False
        instrument.type = "otc_fund"
        return True

    def _fund_client(self):
        if self._fund is None:
            from ..datasource.fund import FundClient

            self._fund = FundClient()
        return self._fund

    def _instrument_types(self) -> dict[str, str]:
        with self._sf() as session:
            return {i.code: i.type for i in session.scalars(select(Instrument))}

    # -------------------------------------------------- 连续失败计数（kv）
    def _bump_fail_streak(self) -> int:
        with self._sf() as session:
            row = session.get(KvSetting, _STREAK_KEY)
            if row is None:
                row = KvSetting(key=_STREAK_KEY, value_json={})
                session.add(row)
            count = int((row.value_json or {}).get("count", 0)) + 1
            row.value_json = {
                "count": count,
                "last": dt.datetime.now().isoformat(timespec="seconds"),
            }
            session.commit()
            return count

    def _reset_fail_streak(self) -> None:
        with self._sf() as session:
            row = session.get(KvSetting, _STREAK_KEY)
            if row is not None and (row.value_json or {}).get("count"):
                row.value_json = {"count": 0}
                session.commit()
