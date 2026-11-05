"""候选批量分析（18:30 任务链）：初筛 → 逐只分析卡 → 汇总推送。"""

from __future__ import annotations

import datetime as dt
import logging
import time

from sqlalchemy import select

from ..db.models import Candidate
from ..db.session import get_session_factory
from .screener import run_screen

log = logging.getLogger(__name__)


def analyze_candidates(
    session_factory=None,
    analyzer_factory=None,
    delay: float = 1.0,
    limit: int = 30,
    trade_date: dt.date | None = None,
) -> dict:
    """为当日 status=new 的候选生成分析卡；单只失败不中断整批。"""
    sf = session_factory or get_session_factory()
    trade_date = trade_date or dt.date.today()

    if analyzer_factory is None:

        def analyzer_factory():
            from ..llm.analyzer import CardAnalyzer

            return CardAnalyzer(session_factory=sf)

    with sf() as session:
        rows = session.scalars(
            select(Candidate)
            .where(Candidate.date == trade_date, Candidate.status == "new")
            .order_by(Candidate.score.desc())
            .limit(limit)
        ).all()
        todo = [(c.id, c.code) for c in rows]

    report: dict = {"analyzed": 0, "failed": 0, "codes": []}
    analyzer = analyzer_factory()
    for index, (cand_id, code) in enumerate(todo):
        try:
            analyzer.analyze(code)
        except Exception as exc:  # noqa: BLE001 — 单只失败不影响其余
            log.warning("候选分析失败 %s: %s", code, exc)
            report["failed"] += 1
            with sf() as session:
                candidate = session.get(Candidate, cand_id)
                if candidate is not None:
                    candidate.status = "analysis_failed"
                    session.commit()
            continue
        report["analyzed"] += 1
        report["codes"].append(code)
        with sf() as session:
            candidate = session.get(Candidate, cand_id)
            if candidate is not None:
                candidate.status = "analyzed"
                session.commit()
        if delay and index < len(todo) - 1:
            time.sleep(delay)
    log.info("候选分析完成: %s", {k: v for k, v in report.items() if k != "codes"})
    return report


def screen_and_analyze(
    session_factory=None,
    flow=None,
    snapshot: list[dict] | None = None,
    analyzer_factory=None,
    top_n: int | None = None,
    delay: float = 1.0,
) -> dict:
    """完整任务链：初筛 → 分析 → 汇总推送。"""
    sf = session_factory or get_session_factory()
    if top_n is None:
        from ..config import get_settings

        top_n = int((get_settings().app_config().get("screen") or {}).get("daily_candidates", 5) or 5)

    screen_result = run_screen(session_factory=sf, snapshot=snapshot, top_n=top_n)
    analysis = analyze_candidates(session_factory=sf, analyzer_factory=analyzer_factory, delay=delay)
    summary: dict = {"screen": screen_result, "analysis": analysis, "notified": False}

    if flow is None:
        from ..engine.notify_flow import NotificationFlow

        flow = NotificationFlow(session_factory=sf)
    if flow.dispatcher.configured and (analysis["codes"] or analysis["failed"]):
        from ..notify import Message

        codes_text = "、".join(analysis["codes"][:15]) or "—"
        body = "\n".join(
            [
                f"候选池更新（{screen_result['date']}）：新增 {screen_result['created']} 条（快照 {screen_result['snapshot_rows']} 只，来源 {screen_result['source']}）。",
                f"完成 AI 分析 {analysis['analyzed']} 只，失败 {analysis['failed']} 只。",
                f"已分析：{codes_text}",
                "",
                "详情见面板 → 候选池 / 分析卡。",
            ]
        )
        try:
            flow.dispatcher.send(
                Message(
                    title=f"【候选池】完成 {analysis['analyzed']} 只分析",
                    body=body,
                )
            )
            summary["notified"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("候选汇总推送失败: %s", exc)
    return summary
