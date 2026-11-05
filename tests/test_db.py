"""B1-02 数据库层测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from invest_assistant.config import get_settings
from invest_assistant.db.models import Alert, Base, ExitRule, Instrument, Position
from invest_assistant.db.session import make_engine

EXPECTED_TABLES = {
    "instrument",
    "position",
    "trade",
    "dividend",
    "corporate_action",
    "exit_rule",
    "watchlist_item",
    "alert",
    "analysis_card",
    "llm_usage",
    "screen_template",
    "candidate",
    "daily_bar",
    "fund_nav",
    "financial_snapshot",
    "news_item",
    "trading_calendar",
    "job_run",
    "backtest_run",
    "kv_setting",
}


def _engine(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return engine


def test_create_all_tables(tmp_path):
    engine = _engine(tmp_path)
    tables = set(inspect(engine).get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"缺少表: {missing}"


def test_position_rule_roundtrip(tmp_path):
    engine = _engine(tmp_path)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        s.add(Instrument(code="600519", name="贵州茅台", type="a_share", exchange="sh"))
        pos = Position(code="600519", strategy="value", qty=100, avg_cost=1200.0)
        s.add(pos)
        s.flush()
        s.add(
            ExitRule(
                position_id=pos.id,
                type="trailing_stop",
                params_json={"drawdown_pct": 8},
                state_json={"peak": 1300.0},
            )
        )
        s.commit()

    with Session() as s:
        rule = s.scalars(select(ExitRule)).one()
        assert rule.params_json["drawdown_pct"] == 8
        assert rule.state_json["peak"] == 1300.0
        assert rule.status == "armed"


def test_alert_dedup_key_unique(tmp_path):
    engine = _engine(tmp_path)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        s.add(Alert(kind="trigger", dedup_key="k1"))
        s.commit()

    with Session() as s:
        s.add(Alert(kind="trigger", dedup_key="k1"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()


def test_alembic_upgrade_head(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(cfg, "head")

        db_file = tmp_path / "data" / "invest.db"
        assert db_file.exists()
        engine = make_engine(f"sqlite:///{db_file}")
        tables = set(inspect(engine).get_table_names())
        assert EXPECTED_TABLES <= tables
    finally:
        get_settings.cache_clear()
