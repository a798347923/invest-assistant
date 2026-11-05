"""模拟盘重置：归档旧数据 → 清空模拟盘 → 从初始资金重新开始。

保留：
- 自选池、分析卡、候选池、实盘持仓（Position）、回放经验（source=replay）、实盘复盘经验（source=position）、预测校准；
清空（先归档到 data/paper_archive/*.json）：
- paper_position / paper_trade / paper_report / paper_daily；
- source=paper 的交易日志与复盘经验；
- 模拟盘相关提醒（context_json.source=paper 或 dedup_key 含 paper）。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import delete, or_, select

from ..config import get_settings
from ..db.models import (
    Alert,
    PaperDaily,
    PaperPosition,
    PaperReport,
    PaperTrade,
    TradeJournal,
    TradingLesson,
)
from ..db.session import get_session_factory


def _row(obj) -> dict:
    return {col.name: getattr(obj, col.name) for col in obj.__table__.columns}


def reset_paper(
    session_factory=None,
    archive_dir: Path | None = None,
    archive: bool = True,
) -> dict:
    """归档并清空模拟盘，返回计数与归档路径。"""
    sf = session_factory or get_session_factory()
    settings = get_settings()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    with sf() as session:
        positions = session.scalars(select(PaperPosition)).all()
        trades = session.scalars(select(PaperTrade)).all()
        reports = session.scalars(select(PaperReport)).all()
        daily = session.scalars(select(PaperDaily)).all()
        journals = session.scalars(
            select(TradeJournal).where(TradeJournal.source == "paper")
        ).all()
        lessons = session.scalars(
            select(TradingLesson).where(TradingLesson.source == "paper")
        ).all()

        archive_path = ""
        if archive:
            out_dir = archive_dir or (settings.data_dir / "paper_archive")
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"paper_archive_{stamp}.json"
            payload = {
                "archived_at": dt.datetime.now().isoformat(timespec="seconds"),
                "positions": [_row(x) for x in positions],
                "trades": [_row(x) for x in trades],
                "reports": [_row(x) for x in reports],
                "daily": [_row(x) for x in daily],
                "journal": [_row(x) for x in journals],
                "lessons": [_row(x) for x in lessons],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            archive_path = str(path)

        session.execute(
            delete(Alert).where(
                or_(
                    Alert.context_json["source"].as_string() == "paper",
                    Alert.dedup_key.like("%paper%"),
                )
            )
        )
        session.execute(delete(TradeJournal).where(TradeJournal.source == "paper"))
        session.execute(delete(TradingLesson).where(TradingLesson.source == "paper"))
        session.execute(delete(PaperReport))
        session.execute(delete(PaperTrade))
        session.execute(delete(PaperDaily))
        session.execute(delete(PaperPosition))
        session.commit()

    paper_cfg = settings.app_config().get("paper") or {}
    return {
        "archive": archive_path,
        "positions": len(positions),
        "trades": len(trades),
        "reports": len(reports),
        "daily": len(daily),
        "journal": len(journals),
        "lessons": len(lessons),
        "initial_cash": float(paper_cfg.get("initial_cash", 50_000) or 50_000),
    }
