"""B1-03 数据源真实网络冒烟测试。

默认跳过；运行方式：`.venv\\Scripts\\python.exe -m pytest -m live -q`
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import MarketDataService
from invest_assistant.db.models import Base
from invest_assistant.db.session import make_engine

pytestmark = pytest.mark.live


@pytest.fixture()
def service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    return MarketDataService(session_factory=sf)


def test_live_quotes(service):
    quotes = service.get_quotes(["600519", "510300"])
    assert quotes["600519"].price > 0
    assert quotes["510300"].price > 0


def test_live_daily_bars(service):
    end = dt.date.today()
    bars = service.get_daily_bars("600519", end - dt.timedelta(days=60), end, force=True)
    assert len(bars) > 5
    assert bars[-1].close > 0


def test_live_calendar(service):
    added = service.refresh_calendar()
    assert added > 1000
    assert isinstance(service.is_trading_day(), bool)


def test_live_fund_nav(service):
    navs = service.get_fund_nav("011186", days=5, force=True)
    assert navs
    assert navs[-1].nav > 0
