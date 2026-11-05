"""实盘指令单与风控测试（半自动闭环；App 自动执行见 test_live_app.py）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, ExitRule, Position
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.engine.live import LiveService


class FakeSettings:
    def __init__(self, live_cfg):
        self._cfg = {"live": live_cfg}

    def app_config(self):
        return self._cfg


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


NOW = dt.datetime(2026, 10, 12, 10, 0)  # 周一 10:00（允许时段内）

BASE = {
    "enabled": True,
    "semi_auto_enabled": True,
    "max_order_amount": 5000,
    "max_daily_orders": 4,
    "max_daily_buy_amount": 20000,
    "price_deviation_pct": 1.0,
    "whitelist": "off",
    "trading_window": ["09:35-11:25", "13:05-14:55"],
}


def svc(sf, **overrides):
    return LiveService(session_factory=sf, settings=FakeSettings({**BASE, **overrides}))


def test_live_disabled_blocks(sf):
    r = svc(sf, enabled=False).create_intent("600519", "buy", 100, 10.0, now=NOW)
    assert r["status"] == "blocked"
    assert any("总开关" in x for x in r["risk"]["reasons"])


def test_live_semi_creates_pending(sf):
    r = svc(sf).create_intent("600519", "buy", 100, 10.0, ref_price=10.0, now=NOW)
    assert r["status"] == "pending"
    assert r["mode"] == "semi"


def test_live_price_deviation_blocks(sf):
    r = svc(sf).create_intent("600519", "buy", 100, 10.5, ref_price=10.0, now=NOW)
    assert r["status"] == "blocked"
    assert any("偏离" in x for x in r["risk"]["reasons"])


def test_live_max_amount_blocks(sf):
    r = svc(sf, max_order_amount=500).create_intent("600519", "buy", 100, 10.0, now=NOW)
    assert r["status"] == "blocked"
    assert any("单笔金额" in x for x in r["risk"]["reasons"])


def test_live_window_blocks(sf):
    r = svc(sf).create_intent(
        "600519", "buy", 100, 10.0, now=dt.datetime(2026, 10, 12, 9, 0)
    )
    assert r["status"] == "blocked"
    assert any("交易时段" in x for x in r["risk"]["reasons"])


def test_live_whitelist_blocks(sf):
    r = svc(sf, whitelist="watchlist").create_intent("600519", "buy", 100, 10.0, now=NOW)
    assert r["status"] == "blocked"
    assert any("自选池" in x for x in r["risk"]["reasons"])


def test_live_idempotent(sf):
    service = svc(sf)
    r1 = service.create_intent("600519", "buy", 100, 10.0, now=NOW)
    r2 = service.create_intent("600519", "buy", 100, 10.0, now=NOW)
    assert r1["id"] == r2["id"]
    assert r2["created"] is False


def test_live_daily_limit(sf):
    service = svc(sf, max_daily_orders=2)
    service.create_intent("600519", "buy", 100, 10.0, now=NOW, idempotency_key="a")
    service.create_intent("600519", "buy", 100, 10.1, now=NOW, idempotency_key="b")
    r3 = service.create_intent("600519", "buy", 100, 10.2, now=NOW, idempotency_key="c")
    assert r3["status"] == "blocked"
    assert any("上限" in x for x in r3["risk"]["reasons"])


def test_live_confirm_fill(sf):
    service = svc(sf)
    r = service.create_intent("600519", "buy", 100, 10.0, now=NOW)
    service.confirm(r["id"])
    filled = service.fill(r["id"], 10.02, now=NOW)
    assert filled["status"] == "filled"
    assert filled["filled_price"] == 10.02


def test_live_sell_exempt_from_max_amount(sf):
    """卖出是风险释放动作：不受单笔买入金额上限约束。"""
    r = svc(sf, max_order_amount=500).create_intent(
        "600519", "sell", 1000, 10.0, now=NOW
    )
    assert r["status"] == "pending"


def test_live_fill_buy_updates_position(sf):
    """回填买入 → 自动写入实盘持仓（含默认退出规则）。"""
    service = svc(sf)
    r = service.create_intent("600519", "buy", 100, 10.0, now=NOW)
    filled = service.fill(r["id"], 10.02, now=NOW)
    assert filled["status"] == "filled"
    assert "已记入实盘持仓" in filled["note"]
    with sf() as session:
        position = session.scalars(select(Position)).one()
        assert position.qty == 100
        assert position.status == "open"
        stop_rule = session.scalars(
            select(ExitRule).where(ExitRule.type == "fixed_stop_loss")
        ).one()
        assert stop_rule.params_json["pct"] == 8.0  # 无机械价时用默认 8%


def test_live_fill_buy_uses_mechanical_stop_price(sf):
    """回填买入带机械止损价 → 止损规则按 (1 − 止损价/成交价) 换算。"""
    service = svc(sf)
    r = service.create_intent("600519", "buy", 100, 10.0, stop_price=9.0, now=NOW)
    service.fill(r["id"], 10.0, now=NOW)
    with sf() as session:
        rule = session.scalars(
            select(ExitRule).where(ExitRule.type == "fixed_stop_loss")
        ).one()
        assert 9.8 < rule.params_json["pct"] < 10.2


def test_live_fill_buy_twice_increases_position(sf):
    service = svc(sf)
    r1 = service.create_intent("600519", "buy", 100, 10.0, now=NOW, idempotency_key="b1")
    service.fill(r1["id"], 10.0, now=NOW)
    r2 = service.create_intent("600519", "buy", 100, 11.0, now=NOW, idempotency_key="b2")
    service.fill(r2["id"], 11.0, now=NOW)
    with sf() as session:
        positions = session.scalars(select(Position)).all()
        assert len(positions) == 1
        assert positions[0].qty == 200


def test_live_fill_sell_closes_position(sf):
    """回填卖出 → 自动减仓/清仓并进入日志闭环。"""
    ps = PositionService(session_factory=sf)
    created = ps.add_position(
        code="600519", qty=100, price=10.0, strategy="swing",
        rules=ExitRules(take_profit_pct=15, stop_loss_pct=8, max_holding_days=30),
        opened_at=NOW,
    )
    service = svc(sf)
    r = service.create_intent(
        "600519", "sell", 100, 10.5, now=NOW, ref_position_id=created["position_id"]
    )
    filled = service.fill(r["id"], 10.5, now=NOW)
    assert "已记入实盘卖出" in filled["note"]
    with sf() as session:
        position = session.get(Position, created["position_id"])
        assert position.status == "closed"
        assert position.qty == 0
