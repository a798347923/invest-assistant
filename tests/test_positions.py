"""B1-04 持仓服务测试。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, Dividend, ExitRule, Position, Trade
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import (
    ExitRules,
    NotFoundError,
    PositionService,
    ValidationError,
)


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'pos.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def svc(sf):
    return PositionService(session_factory=sf)


def _rules(**overrides):
    base = dict(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=90)
    base.update(overrides)
    return ExitRules(**base)


def test_validation_rejects_empty_rules(svc):
    with pytest.raises(ValidationError):
        svc.add_position(code="600519", qty=100, price=1200, strategy="value", rules=ExitRules())


def test_validation_rejects_unknown_strategy(svc):
    with pytest.raises(ValidationError):
        svc.add_position(code="600519", qty=100, price=1200, strategy="bogus", rules=_rules())


def test_validation_requires_sl_or_trailing():
    with pytest.raises(ValidationError) as exc:
        ExitRules(take_profit_pct=20, max_holding_days=90).validate()
    assert "止损" in str(exc.value)


def test_add_position_creates_trade_and_rules(svc, sf):
    result = svc.add_position(
        code="600519", qty=100, price=1200.0, fee=5.0, strategy="value",
        rules=_rules(stop_loss_pct=10),
    )
    assert result["position_id"] >= 1
    with sf() as s:
        pos = s.get(Position, result["position_id"])
        assert pos is not None
        assert pos.qty == 100
        assert abs(pos.avg_cost - 1200.05) < 1e-6  # (1200×100+5)/100
        trades = s.scalars(select(Trade)).all()
        assert len(trades) == 1 and trades[0].side == "buy"
        rule_types = sorted(r.type for r in s.scalars(select(ExitRule)))
        assert rule_types == [
            "fixed_stop_loss",
            "fixed_take_profit",
            "max_holding",
            "trailing_stop",
        ]


def test_sell_partial_pnl(svc):
    r = svc.add_position(code="600519", qty=100, price=100.0, strategy="swing", rules=_rules())
    out = svc.sell(r["position_id"], qty=40, price=110.0, fee=1.0)
    assert abs(out["realized_pnl"] - 399.0) < 1e-6  # (110-100)×40 - 1
    assert out["remaining_qty"] == 60
    assert out["status"] == "open"


def test_sell_full_closes_and_disables_rules(svc, sf):
    r = svc.add_position(code="600519", qty=10, price=100.0, strategy="swing", rules=_rules())
    out = svc.sell(r["position_id"], qty=10, price=90.0)
    assert out["status"] == "closed"
    with sf() as s:
        statuses = {x.status for x in s.scalars(select(ExitRule))}
        assert statuses == {"disabled"}


def test_sell_over_qty_rejected(svc):
    r = svc.add_position(code="600519", qty=10, price=100.0, strategy="swing", rules=_rules())
    with pytest.raises(ValidationError):
        svc.sell(r["position_id"], qty=99, price=100.0)
    with pytest.raises(NotFoundError):
        svc.sell(9999, qty=1, price=100.0)


def test_dividend_dilutes_cost(svc, sf):
    r = svc.add_position(code="600519", qty=100, price=10.0, strategy="drip", rules=_rules())
    out = svc.add_dividend(r["position_id"], cash_amount=50.0, ex_date=dt.date(2026, 10, 1))
    assert abs(out["new_avg_cost"] - 9.5) < 1e-6  # (10×100-50)/100
    with sf() as s:
        assert s.scalars(select(Dividend)).one().cash_amount == 50.0


def test_split_adjusts_qty_and_cost(svc):
    r = svc.add_position(code="600519", qty=100, price=30.0, strategy="value", rules=_rules())
    out = svc.apply_split(r["position_id"], factor=2.0)
    assert out["qty"] == 200
    assert abs(out["avg_cost"] - 15.0) < 1e-6
