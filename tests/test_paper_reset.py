"""模拟盘重置测试：归档 + 清空 + 保留非模拟盘数据。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import (
    Alert,
    Base,
    PaperDaily,
    PaperPosition,
    PaperReport,
    PaperTrade,
    TradeJournal,
    TradingLesson,
    WatchlistItem,
)
from invest_assistant.db.session import make_engine
from invest_assistant.engine.paper_reset import reset_paper


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'reset.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_reset_paper_archives_and_clears(sf, tmp_path):
    with sf() as session:
        session.add_all(
            [
                PaperPosition(code="600519", name="贵州茅台", qty=100, avg_cost=10.0, status="open"),
                PaperPosition(
                    code="601998", name="中信银行", qty=0, avg_cost=9.0,
                    status="closed", realized_pnl=-107.6,
                ),
                PaperTrade(
                    code="600519", name="贵州茅台", side="buy", qty=100, price=10.0, amount=1000.0
                ),
                PaperReport(
                    date=dt.date(2026, 10, 16), model="fake",
                    content_json={"a": 1}, execution_json={}, status="executed",
                ),
                PaperDaily(
                    date=dt.date(2026, 10, 16), cash=40000, market_value=10000, equity=50000
                ),
                TradeJournal(
                    source="paper", code="600519", action="建仓",
                    qty=100, price=10.0, amount=1000.0, reason="test",
                ),
                TradeJournal(
                    source="position", code="600519", action="建仓",
                    qty=100, price=10.0, amount=1000.0, reason="real",
                ),
                TradingLesson(
                    source="paper", ref_id=1, code="600519", name="贵州茅台",
                    entry_date=dt.date(2026, 10, 1), exit_date=dt.date(2026, 10, 10),
                    holding_days=9, pnl=100, pnl_pct=10, rating="", summary="paper lesson",
                    lessons_json=["x"], model="fake",
                ),
                TradingLesson(
                    source="position", ref_id=2, code="600519", name="贵州茅台",
                    entry_date=dt.date(2026, 10, 1), exit_date=dt.date(2026, 10, 10),
                    holding_days=9, pnl=100, pnl_pct=10, rating="", summary="position lesson",
                    lessons_json=["x"], model="fake",
                ),
                Alert(
                    kind="watch", context_json={"source": "paper", "code": "600519"},
                    status="pending", dedup_key="watch-600519-paper_buy-20261016",
                ),
                Alert(
                    kind="watch", context_json={"source": "watchlist", "code": "600519"},
                    status="pending", dedup_key="watch-600519-plan_buy-20261016",
                ),
                WatchlistItem(code="600519", alert_pct=0),
            ]
        )
        session.commit()

    result = reset_paper(sf, archive_dir=tmp_path / "archive")
    assert result["positions"] == 2
    assert result["trades"] == 1
    assert result["reports"] == 1
    assert result["daily"] == 1
    assert result["journal"] == 1
    assert result["lessons"] == 1
    archive = json.loads(open(result["archive"], encoding="utf-8").read())
    assert len(archive["positions"]) == 2
    assert archive["journal"][0]["source"] == "paper"

    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        assert session.scalars(select(PaperTrade)).all() == []
        assert session.scalars(select(PaperReport)).all() == []
        assert session.scalars(select(PaperDaily)).all() == []
        assert session.scalars(
            select(TradeJournal).where(TradeJournal.source == "paper")
        ).all() == []
        assert session.scalars(
            select(TradingLesson).where(TradingLesson.source == "paper")
        ).all() == []
        # 保留实盘日志/经验、自选与非模拟盘提醒
        assert session.scalars(
            select(TradeJournal).where(TradeJournal.source == "position")
        ).all()
        assert session.scalars(
            select(TradingLesson).where(TradingLesson.source == "position")
        ).all()
        assert session.get(WatchlistItem, "600519") is not None
        alerts = session.scalars(select(Alert)).all()
        assert len(alerts) == 1
        assert alerts[0].dedup_key.endswith("plan_buy-20261016")


def test_reset_paper_without_archive(sf):
    with sf() as session:
        session.add(PaperPosition(code="600519"))
        session.commit()
    result = reset_paper(sf, archive=False)
    assert result["archive"] == ""
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
