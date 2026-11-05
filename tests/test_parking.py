"""弱市停泊测试：买入/卖出/幂等/估值/关闭。

口径（docs/weak-market-alternatives.md）：弱市把闲置现金按 债7:金3 停到 ETF，强市退出；
不占股票槽位、不参与 AI 建议，只在独立资金层记账。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, PaperTrade
from invest_assistant.db.session import make_engine
from invest_assistant.engine.parking import PARK_REASON, ParkingService

NOW = dt.datetime(2026, 11, 4, 10, 0)


class FakeSettings:
    def __init__(self, paper_cfg):
        self._cfg = {"paper": paper_cfg, "backtest": {"fees": {
            "commission_rate": 0.00025, "commission_min": 5,
            "stamp_tax_sell": 0.0005, "slippage": 0.001,
        }}}

    def app_config(self):
        return self._cfg


def quote(code: str, price: float, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(code=code, name=name or code, price=price)


QUOTES = {
    "511260": quote("511260", 110.0, "十年国债ETF"),
    "518880": quote("518880", 5.0, "黄金ETF"),
}


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'parking.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def service(sf, **over) -> ParkingService:
    cfg = {"enabled": True, "parking_enabled": True,
           "parking_bond": "511260", "parking_gold": "518880",
           "parking_bond_weight": 0.7, "parking_gold_weight": 0.3,
           "parking_min_amount": 1000}
    cfg.update(over)
    return ParkingService(session_factory=sf, settings=FakeSettings(cfg))


def trades(sf):
    with sf() as session:
        return session.scalars(
            select(PaperTrade).where(PaperTrade.reason.like(f"{PARK_REASON}%"))
        ).all()


def test_disabled_does_nothing(sf):
    svc = service(sf, parking_enabled=False)
    with sf() as session:
        action = svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
    assert action["action"] == "disabled"
    assert trades(sf) == []


def test_weak_market_buys_bond_and_gold_by_weight(sf):
    svc = service(sf)
    with sf() as session:
        action = svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
    assert action["action"] == "buy"
    rows = trades(sf)
    assert {r.code for r in rows} == {"511260", "518880"}
    bond = next(r for r in rows if r.code == "511260")
    gold = next(r for r in rows if r.code == "518880")
    # 70% / 30%（整手取整会带来几个百分点误差）
    assert bond.amount == pytest.approx(50000 * 0.7, rel=0.06)
    assert gold.amount == pytest.approx(50000 * 0.3, rel=0.06)
    assert bond.amount / gold.amount == pytest.approx(0.7 / 0.3, rel=0.15)


def test_idempotent_when_already_parked(sf):
    svc = service(sf)
    with sf() as session:
        svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
    before = len(trades(sf))
    with sf() as session:
        action = svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
    assert action["action"] == "hold"
    assert len(trades(sf)) == before


def test_strong_market_sells_everything(sf):
    svc = service(sf)
    with sf() as session:
        svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
    with sf() as session:
        action = svc.rebalance(session, weak=False, quotes=QUOTES, now=NOW, available_cash=0)
        session.commit()
    assert action["action"] == "sell"
    assert svc_holdings_empty(sf)
    sells = [r for r in trades(sf) if r.side == "sell"]
    assert len(sells) == 2


def svc_holdings_empty(sf) -> bool:
    svc = service(sf)
    with sf() as session:
        return not svc.holdings(session)


def test_value_uses_cached_prices(sf):
    svc = service(sf)
    with sf() as session:
        svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        svc.cache_prices(session, QUOTES)
        session.commit()
        value = svc.value(session)
    assert value > 30000  # 约 3.5 万，含滑点


def test_skip_when_cash_too_small(sf):
    svc = service(sf)
    with sf() as session:
        action = svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=500)
        session.commit()
    assert action["action"] == "skip"
    assert trades(sf) == []


def test_unknown_price_skips_that_leg(sf):
    svc = service(sf)
    only_bond = {"511260": QUOTES["511260"]}
    with sf() as session:
        action = svc.rebalance(session, weak=True, quotes=only_bond, now=NOW, available_cash=50000)
        session.commit()
    assert action["action"] == "buy"
    assert {r.code for r in trades(sf)} == {"511260"}


def test_status_reports_holdings(sf):
    svc = service(sf)
    with sf() as session:
        svc.rebalance(session, weak=True, quotes=QUOTES, now=NOW, available_cash=50000)
        session.commit()
        status = svc.status(session)
    assert status["enabled"] is True
    assert {p["code"] for p in status["positions"]} == {"511260", "518880"}
    assert status["config"]["bond_weight"] == 0.7
