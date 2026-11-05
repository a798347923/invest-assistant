"""交易日志与复盘经验库（模拟盘 + 实盘统一记录）。

- `record_trade`：每笔操作连同「当时情况」（计划价位、组合状态、触发原因）入库；
- `plan_context`：取标的最近分析卡的计划价位摘要（作为操作情境的一部分）；
- `lessons_text`：把清仓复盘提炼的经验教训格式化，供后续 AI 提示词注入。
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select

from ..db.models import AnalysisCard, TradeJournal, TradingLesson
from .plan import parse_plan_levels

log = logging.getLogger(__name__)


def plan_context(session, code: str) -> dict:
    """最新分析卡的计划价位摘要（无卡返回空 dict）。"""
    card = session.scalars(
        select(AnalysisCard)
        .where(AnalysisCard.code == code)
        .order_by(AnalysisCard.created_at.desc())
        .limit(1)
    ).first()
    plan = parse_plan_levels(card.content_json) if card is not None else None
    if not plan:
        return {}
    return {
        "zone_low": plan.get("zone_low"),
        "zone_high": plan.get("zone_high"),
        "breakout": plan.get("breakout"),
        "stop": plan.get("stop"),
        "zone_text": plan.get("zone_text"),
        "stop_text": plan.get("stop_text"),
        "position_hint": plan.get("position_hint"),
        "action_bias": str((card.content_json or {}).get("action_bias") or ""),
    }


def record_trade(
    session,
    *,
    source: str,
    ref_id: int | None,
    code: str,
    name: str,
    action: str,
    qty: float = 0.0,
    price: float = 0.0,
    amount: float = 0.0,
    fee: float = 0.0,
    realized_pnl: float | None = None,
    reason: str = "",
    context: dict | None = None,
    ts: dt.datetime | None = None,
) -> TradeJournal:
    """写入一条操作日志（不 commit，由调用方事务统一提交）。"""
    row = TradeJournal(
        ts=ts or dt.datetime.now(),
        source=source,
        ref_id=ref_id,
        code=code,
        name=name or code,
        action=action,
        qty=qty,
        price=price,
        amount=amount,
        fee=fee,
        realized_pnl=realized_pnl,
        reason=reason,
        context_json=context or {},
    )
    session.add(row)
    return row


def lessons_text(session_factory, limit: int = 8) -> list[str]:
    """最近的经验教训（新的在前，供提示词注入）；无数据返回空列表。"""
    with session_factory() as session:
        rows = session.scalars(
            select(TradingLesson).order_by(TradingLesson.created_at.desc()).limit(limit)
        ).all()
    out: list[str] = []
    for row in rows:
        if row.source == "replay":
            # 回放经验只注入一行压缩结论（完整明细在复盘页查看）
            text = (row.summary or "").strip()
            if not text:
                findings = [str(x) for x in (row.lessons_json or [])[:2]]
                text = "；".join(findings) if findings else "样本不足"
            out.append("[历史模拟] " + text)
            continue
        lessons = [str(x) for x in (row.lessons_json or [])[:3]]
        detail = "；".join(lessons)
        label = {"paper": "模拟盘", "position": "实盘"}.get(row.source, row.source or "—")
        head = (
            f"[{row.rating or '—'}] {label}"
            f" {row.name or row.code}（{row.pnl_pct:+.1f}%，持有 {row.holding_days} 天）"
        )
        text = head + "：" + (row.summary or "")
        if detail:
            text += "；经验：" + detail
        out.append(text)
    return out


def journal_digest(row: TradeJournal) -> dict[str, Any]:
    """单条日志的紧凑摘要（供 UI / 复盘载荷复用）。"""
    return {
        "时间": row.ts.strftime("%Y-%m-%d %H:%M"),
        "操作": row.action,
        "数量": row.qty,
        "价格": row.price,
        "金额": row.amount,
        "费用": row.fee,
        "盈亏": row.realized_pnl,
        "原因": row.reason,
    }
