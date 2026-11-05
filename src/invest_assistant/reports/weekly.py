"""周报（周六 10:00）：持仓变化、提醒执行统计、错过的卖点回看、AI 总结。"""

from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel
from sqlalchemy import select

from ..db.models import Alert, Position, Trade
from ..db.session import get_session_factory
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import WEEKLY_V1_SYSTEM, weekly_user
from ..llm.usage import record_usage
from ..notify import Message, build_dispatcher

log = logging.getLogger(__name__)


class WeeklySummary(BaseModel):
    summary: str
    actions: list[str] = []


def weekly_report(data=None, session_factory=None, llm=None, dispatcher=None, now=None) -> dict:
    now = now or dt.datetime.now()
    sf = session_factory or get_session_factory()
    week_ago = now - dt.timedelta(days=7)

    with sf() as session:
        open_positions = session.scalars(
            select(Position).where(Position.status == "open")
        ).all()
        closed_this_week = session.scalars(
            select(Position).where(Position.status == "closed", Position.closed_at >= week_ago)
        ).all()
        trades = session.scalars(select(Trade).where(Trade.traded_at >= week_ago)).all()
        alerts = session.scalars(select(Alert).where(Alert.triggered_at >= week_ago)).all()
        alert_stats: dict = {"total": len(alerts)}
        for alert in alerts:
            alert_stats[alert.status] = alert_stats.get(alert.status, 0) + 1
        watch_alerts = [
            {
                "code": (alert.context_json or {}).get("code"),
                "trigger_price": alert.trigger_price,
                "triggered_at": alert.triggered_at.isoformat(timespec="minutes"),
            }
            for alert in alerts
            if alert.kind == "trigger"
            and alert.status in ("sent", "handled")
            and "take_profit" in ((alert.context_json or {}).get("rule_type") or "")
        ]

    missed: list[str] = []
    quotes: dict = {}
    if watch_alerts:
        if data is None:
            from ..datasource import MarketDataService

            data = MarketDataService(session_factory=sf)
        try:
            quotes = data.get_quotes(sorted({w["code"] for w in watch_alerts if w["code"]}))
        except Exception as exc:  # noqa: BLE001 — 行情失败跳过该板块
            log.warning("周报行情获取失败，跳过错过的卖点: %s", exc)
    for w in watch_alerts:
        quote = quotes.get(w["code"]) if quotes else None
        if quote and w["trigger_price"] and quote.price < w["trigger_price"] * 0.98:
            drop = (quote.price / w["trigger_price"] - 1) * 100
            missed.append(
                f"{w['code']}：{w['triggered_at']} 曾提醒止盈（{w['trigger_price']:.2f}），"
                f"现价 {quote.price:.2f}（{drop:+.2f}%）"
            )

    lines = [
        f"开放持仓 {len(open_positions)} 只，本周平仓 {len(closed_this_week)} 只，交易 {len(trades)} 笔。",
        "本周提醒 "
        + f"{alert_stats['total']} 条（"
        + "、".join(f"{k} {v}" for k, v in alert_stats.items() if k != "total")
        + "）。",
    ]
    if missed:
        lines.append("错过的卖点：")
        lines.extend(f"- {m}" for m in missed)
    else:
        lines.append("错过的卖点：无（本周止盈类提醒后价格未明显回落，或没有此类提醒）。")

    ai = None
    llm = llm or LLMClient()
    if llm.configured:
        try:
            result = llm.complete_json(
                purpose="weekly",
                system=WEEKLY_V1_SYSTEM,
                user=weekly_user({"近一周统计": lines, "提醒状态分布": alert_stats}),
                schema=WeeklySummary,
                max_tokens=600,
            )
            record_usage(
                sf,
                purpose="weekly",
                model=result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cached_in=result.cached_in,
            )
            ai = result.content
        except LLMError as exc:
            log.warning("周报 AI 总结失败（仅发送统计）: %s", exc)

    body = ["【一周回顾】", "", *lines]
    if ai:
        body += ["", "【AI 总结】", str(ai.get("summary", ""))]
        actions = ai.get("actions") or []
        if actions:
            body.append("下周建议：")
            body.extend(f"{i}. {a}" for i, a in enumerate(actions, 1))

    dispatcher = dispatcher or build_dispatcher()
    sent = False
    if dispatcher.configured:
        results = dispatcher.send(
            Message(
                title=f"【周报】{len(open_positions)} 只持仓 · {alert_stats['total']} 条提醒",
                body="\n".join(body),
            )
        )
        sent = any(r.ok for r in results)
    return {
        "positions": len(open_positions),
        "trades": len(trades),
        "alerts": alert_stats["total"],
        "missed": len(missed),
        "ai": bool(ai),
        "sent": sent,
    }
