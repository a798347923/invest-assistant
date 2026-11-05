"""B3-07 数据清理测试。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Alert, BacktestRun, Base, JobRun, NewsItem
from invest_assistant.db.session import make_engine
from invest_assistant.engine.housekeeping import housekeeping

NOW = dt.datetime(2026, 10, 11, 23, 0)


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'hk.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_housekeeping_removes_old_keeps_recent_and_protected(tmp_path):
    sf = _env(tmp_path)
    old = NOW - dt.timedelta(days=100)
    recent = NOW - dt.timedelta(days=5)
    with sf() as session:
        session.add(NewsItem(code="600519", ts=old, hash="h-old", title="old"))
        session.add(NewsItem(code="600519", ts=recent, hash="h-new", title="new"))
        session.add(Alert(kind="trigger", status="sent", triggered_at=old))
        session.add(Alert(kind="trigger", status="pending", triggered_at=old))  # 保护
        session.add(Alert(kind="trigger", status="sent", triggered_at=recent))
        session.add(JobRun(job="poll_quotes", started_at=old, status="ok"))
        session.add(JobRun(job="poll_quotes", started_at=recent, status="ok"))
        for _ in range(3):
            session.add(BacktestRun(params_json={}, status="done"))
        session.commit()

    removed = housekeeping(
        sf,
        now=NOW,
        retention={"news_days": 30, "alerts_days": 30, "job_run_days": 7, "backtest_keep": 2},
    )
    assert removed["news_item"] == 1
    assert removed["alert"] == 1  # pending 被保护
    assert removed["job_run"] == 1
    assert removed["backtest_run"] == 1

    with sf() as session:
        assert len(session.scalars(select(NewsItem)).all()) == 1
        statuses = sorted(a.status for a in session.scalars(select(Alert)))
        assert statuses == ["pending", "sent"]
        assert len(session.scalars(select(JobRun)).all()) == 1
        assert len(session.scalars(select(BacktestRun)).all()) == 2
