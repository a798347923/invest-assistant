"""清仓复盘：用「高级模型」总结每笔已清仓交易的经验教训 → 经验库（trading_lesson）。

触发时机：每日收盘体检（15:30，覆盖实盘）与模拟盘日报（15:40，覆盖模拟盘）
自动扫描「已清仓但未复盘」的持仓，逐笔生成（每次最多 limit 笔）；
高级模型（config: llm.deep_model，默认 deepseek-reasoner）不可用时自动回退主模型。
"""

from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel
from sqlalchemy import select

from ..config import get_settings
from ..db.models import PaperPosition, PaperTrade, Position, Trade, TradeJournal, TradingLesson
from ..db.session import get_session_factory
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import LESSON_V1_SYSTEM, lesson_user
from ..llm.usage import record_usage

log = logging.getLogger(__name__)

DEFAULT_DEEP_MODEL = "deepseek-reasoner"


class LessonRead(BaseModel):
    summary: str
    lessons: list[str] = []
    rating: str = ""


def _entries_from_journal(session, source: str, ref_id: int) -> list[dict]:
    rows = session.scalars(
        select(TradeJournal)
        .where(TradeJournal.source == source, TradeJournal.ref_id == ref_id)
        .order_by(TradeJournal.ts)
    ).all()
    return [
        {
            "时间": row.ts.strftime("%m-%d %H:%M"),
            "操作": row.action,
            "数量": row.qty,
            "价格": row.price,
            "金额": row.amount,
            "费用": row.fee,
            "盈亏": row.realized_pnl,
            "原因": row.reason,
            "情境": row.context_json or {},
        }
        for row in rows
    ]


def _entries_fallback(session, source: str, position) -> list[dict]:
    """旧数据兜底：无日志时从成交表拼操作记录。"""
    entries: list[dict] = []
    if source == "paper":
        trades = session.scalars(
            select(PaperTrade).where(PaperTrade.code == position.code).order_by(PaperTrade.ts)
        ).all()
        for t in trades:
            if t.ts < position.opened_at:
                continue
            if position.closed_at and t.ts > position.closed_at + dt.timedelta(minutes=5):
                continue
            entries.append(
                {
                    "时间": t.ts.strftime("%m-%d %H:%M"),
                    "操作": "买入" if t.side == "buy" else "卖出",
                    "数量": t.qty, "价格": t.price, "金额": t.amount, "费用": t.fee,
                    "盈亏": t.realized_pnl if t.side == "sell" else None,
                    "原因": t.reason,
                }
            )
    else:
        trades = session.scalars(
            select(Trade).where(Trade.position_id == position.id).order_by(Trade.traded_at)
        ).all()
        for t in trades:
            entries.append(
                {
                    "时间": t.traded_at.strftime("%m-%d %H:%M"),
                    "操作": "买入" if t.side == "buy" else "卖出",
                    "数量": t.qty, "价格": t.price, "金额": round(t.price * t.qty, 2),
                    "费用": t.fee, "盈亏": None, "原因": t.note or "",
                }
            )
    return entries


def build_lesson_payload(session_factory, source: str, position) -> dict:
    """汇总一笔完整交易的「所有数据」：操作记录 + 当时情境 + 盈亏。"""
    closed_at = position.closed_at or dt.datetime.now()
    with session_factory() as session:
        entries = _entries_from_journal(session, source, position.id)
        if not entries:
            entries = _entries_fallback(session, source, position)
    buy_amount = sum(
        float(e["金额"]) + float(e.get("费用") or 0)
        for e in entries
        if e["操作"] in ("建仓", "加仓", "买入")
    )
    sell_amount = sum(
        float(e["金额"]) - float(e.get("费用") or 0)
        for e in entries
        if e["操作"] in ("减仓", "清仓", "卖出")
    )
    net = sell_amount - buy_amount
    pnl_pct = (net / buy_amount * 100) if buy_amount else 0.0
    return {
        "来源": "模拟盘" if source == "paper" else "实盘持仓",
        "标的代码": position.code,
        # PaperPosition 有 name；实盘 Position 只有 code（名字在 instrument 表），不能硬取
        "标的名": getattr(position, "name", "") or position.code,
        "开仓时间": position.opened_at.strftime("%Y-%m-%d %H:%M"),
        "清仓时间": closed_at.strftime("%Y-%m-%d %H:%M"),
        "持有天数": (closed_at - position.opened_at).days,
        "投入金额": round(buy_amount, 2),
        "净盈亏": round(net, 2),
        "净收益率%": round(pnl_pct, 2),
        "操作记录": entries,
    }


def _ask_lesson(client, model: str | None, payload: dict, session_factory) -> tuple[dict, str, float]:
    result = client.complete_json(
        purpose="lesson",
        system=LESSON_V1_SYSTEM,
        user=lesson_user(payload),
        schema=LessonRead,
        max_tokens=900,
        model=model,
    )
    cost = record_usage(
        session_factory,
        purpose="lesson",
        model=result.model,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cached_in=result.cached_in,
    )
    return result.content, result.model, cost


def generate_pending_lessons(
    session_factory=None, settings=None, llm=None, limit: int = 3
) -> dict:
    """扫描已清仓且未复盘的持仓（模拟盘 + 实盘），逐笔生成经验教训。"""
    sf = session_factory or get_session_factory()
    settings = settings or get_settings()
    llm_cfg = settings.app_config().get("llm") or {}
    deep_model = str(llm_cfg.get("deep_model") or DEFAULT_DEEP_MODEL)
    client = llm or LLMClient(settings=settings)
    if not client.configured:
        return {"skipped": "未配置 LLM", "generated": 0}

    with sf() as session:
        existing = {(row.source, row.ref_id) for row in session.scalars(select(TradingLesson))}
        candidates: list[tuple[str, object]] = []
        for p in session.scalars(select(PaperPosition).where(PaperPosition.status == "closed")):
            if ("paper", p.id) not in existing:
                candidates.append(("paper", p))
        for p in session.scalars(select(Position).where(Position.status == "closed")):
            if ("position", p.id) not in existing:
                candidates.append(("position", p))
    candidates.sort(key=lambda item: item[1].closed_at or dt.datetime.min, reverse=True)

    report: dict = {"generated": 0, "failed": 0, "codes": [], "model": deep_model}
    for source, position in candidates[: max(int(limit), 0)]:
        payload = build_lesson_payload(sf, source, position)
        try:
            content, model_used, cost = _ask_lesson(client, deep_model, payload, sf)
        except LLMError as exc:
            log.warning("高级模型复盘失败 %s（%s），回退主模型", position.code, exc)
            try:
                content, model_used, cost = _ask_lesson(client, None, payload, sf)
            except LLMError as exc2:
                log.warning("清仓复盘失败 %s: %s", position.code, exc2)
                report["failed"] += 1
                continue
        closed_at = position.closed_at or dt.datetime.now()
        with sf() as session:
            session.add(
                TradingLesson(
                    source=source,
                    ref_id=position.id,
                    code=position.code,
                    name=position.name or position.code,
                    entry_date=position.opened_at.date(),
                    exit_date=closed_at.date(),
                    holding_days=payload["持有天数"],
                    pnl=payload["净盈亏"],
                    pnl_pct=payload["净收益率%"],
                    rating=str(content.get("rating") or "")[:4],
                    summary=str(content.get("summary") or "")[:600],
                    lessons_json=[str(x)[:160] for x in (content.get("lessons") or [])[:4]],
                    model=model_used,
                    cost=cost,
                )
            )
            session.commit()
        report["generated"] += 1
        report["codes"].append(position.code)
    if report["generated"] or report["failed"]:
        log.info("清仓复盘: %s", report)
    return report
