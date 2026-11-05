"""观察报表：快照字段、同日幂等、渲染。"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, PaperPosition, PaperTrade
from invest_assistant.db.session import make_engine
from invest_assistant.engine.observe import FIELDS, append_row, build_row, load_rows, render
from invest_assistant.engine.paper import PaperService


class FakeSettings:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def app_config(self):
        return {
            "paper": {"enabled": True, "initial_cash": 50000},
            "backtest": {"fees": {"commission_rate": 0.00025, "commission_min": 5,
                                  "stamp_tax_sell": 0.0005, "slippage": 0.001}},
        }


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'obs.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    settings = FakeSettings(tmp_path)
    return sf, PaperService(session_factory=sf, settings=settings), settings


def seed(sf) -> None:
    with sf() as session:
        session.add(PaperPosition(code="601328", name="交通银行", qty=300, avg_cost=8.0,
                                  last_price=8.25, status="open"))
        session.add(PaperTrade(code="511260", name="十年国债ETF", side="buy", qty=300,
                               price=110.0, amount=33000.0, fee=8.25, reason="弱市停泊：弱市闲置现金（70%）"))
        session.commit()


def test_build_row_has_parking_and_exposure(env):
    sf, paper, _ = env
    seed(sf)
    with sf() as session:
        row = build_row(session, paper)
    assert set(row) == set(FIELDS)
    assert row["positions"] == 1
    assert row["parking_value"] > 0        # 停泊有持仓（价格缺失时按成本计）
    assert row["gross_exposure"] > 0       # 有股票市值
    assert 0 < row["avg_weight"] <= 1
    assert row["trades_cum"] >= 1


def test_append_row_is_idempotent_per_day(env, tmp_path):
    path = str(tmp_path / "obs.csv")
    row = {k: "" for k in FIELDS}
    row.update({"date": "2026-11-04", "equity": "50000"})
    append_row(row, path=path)
    row2 = dict(row, equity="51000")
    append_row(row2, path=path)

    with open(path, encoding="utf-8") as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    assert len(lines) == 2  # 表头 + 1 行（同日被覆盖）
    rows = load_rows(days=10, path=path)
    assert len(rows) == 1 and rows[0]["equity"] == "51000"


def test_render_contains_summary(env, tmp_path):
    path = str(tmp_path / "obs2.csv")
    base = {k: "0" for k in FIELDS}
    a = dict(base, date="2026-11-01", equity="50000", cash="50000", market_value="0",
             parking_value="0", gross_exposure="0", avg_weight="0", parking_pnl="0")
    b = dict(base, date="2026-11-02", equity="50500", cash="45000", market_value="5000",
             parking_value="500", gross_exposure="0.0099", avg_weight="0.2", parking_pnl="12")
    append_row(a, path=path)
    append_row(b, path=path)
    text = render(load_rows(days=10, path=path))
    assert "2026-11-01" in text and "2026-11-02" in text
    assert "区间" in text
    assert "+500" in text


def test_append_row_creates_missing_dir(tmp_path):
    target = os.path.join(tmp_path, "nested", "dir", "obs.csv")
    row = {k: "" for k in FIELDS}
    row.update({"date": dt.date.today().isoformat(), "equity": "1"})
    os.makedirs(os.path.dirname(target), exist_ok=True)
    append_row(row, path=target)
    assert os.path.exists(target)


def test_snapshot_uses_service_data_dir(env, tmp_path):
    """回归：快照必须落在该服务自己的 data_dir，不能污染真实 data/observations。"""
    from invest_assistant.engine.observe import snapshot

    sf, paper, _ = env
    seed(sf)
    with sf() as session:
        row = snapshot(session, paper)
    expected = os.path.join(str(tmp_path), "observations", "paper_daily.csv")
    assert os.path.exists(expected)
    assert row["date"] == dt.date.today().isoformat()
