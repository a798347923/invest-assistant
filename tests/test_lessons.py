"""复盘经验：实盘 Position 没有 name 字段，不能直接取（2026-11-04 线上报错）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, Instrument, Position
from invest_assistant.db.session import make_engine
from invest_assistant.engine.lessons import build_lesson_payload


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lesson.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_real_position_without_name_uses_code(sf):
    """实盘持仓表没有 name 列，payload 必须回退到 code 而不是抛 AttributeError。"""
    with sf() as session:
        session.add(Instrument(code="600519", name="贵州茅台", type="a_share", exchange="sh"))
        pos = Position(
            code="600519",
            strategy="swing",
            qty=100,
            avg_cost=1500.0,
            opened_at=dt.datetime(2026, 10, 1, 9, 30),
            closed_at=dt.datetime(2026, 10, 20, 14, 0),
            status="closed",
        )
        session.add(pos)
        session.commit()
        session.refresh(pos)

    payload = build_lesson_payload(sf, "real", pos)
    assert payload["标的代码"] == "600519"
    assert payload["标的名"] == "600519"
    assert payload["持有天数"] == 19


def test_paper_position_keeps_name(sf):
    """模拟盘持仓有 name，应原样保留。"""
    from invest_assistant.db.models import PaperPosition

    with sf() as session:
        pos = PaperPosition(
            code="601328", name="交通银行", qty=300, avg_cost=8.0,
            opened_at=dt.datetime(2026, 10, 1, 9, 30),
            closed_at=dt.datetime(2026, 10, 10, 14, 0), status="closed",
        )
        session.add(pos)
        session.commit()
        session.refresh(pos)

    payload = build_lesson_payload(sf, "paper", pos)
    assert payload["标的名"] == "交通银行"
