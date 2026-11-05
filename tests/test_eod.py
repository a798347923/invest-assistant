"""收盘体检测试（假行情/假 LLM/假通知；含操作建议与上下文验证）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import FundNav, Quote
from invest_assistant.db.models import Base, LLMUsage
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.engine.eod import run_eod
from invest_assistant.llm.client import LLMError, LLMResult
from invest_assistant.notify import DeliveryResult, Dispatcher

NOW = dt.datetime(2026, 10, 12, 15, 30)
_EMPTY_NEWS = lambda code: []  # noqa: E731


class FakeData:
    def __init__(self, quotes=None, trading=True, fail=False):
        self.quotes = quotes or {}
        self.trading = trading
        self.fail = fail

    def is_trading_day(self, day=None):
        return self.trading

    def get_quotes(self, codes):
        if self.fail:
            raise RuntimeError("quote down")
        return {c: q for c, q in self.quotes.items() if c in codes}

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return []

    def get_fund_nav(self, code, days=2):
        raise RuntimeError("not a fund")


class FakeLLM:
    configured = True

    def __init__(self, content=None, error=None):
        self.calls = 0
        self.last_user = ""
        self._content = content
        self._error = error

    def complete_json(self, **kwargs):
        self.calls += 1
        self.last_user = str(kwargs.get("user", ""))
        if self._error:
            raise self._error
        content = self._content or {
            "summary": "今日无触发，持仓平稳。",
            "positions": [
                {
                    "code": "600519",
                    "action": "持有观察",
                    "reason": "未接近纪律线，量价平稳",
                    "condition": "无",
                }
            ],
            "watch_items": ["600519 距止盈线约 9%"],
            "actions": ["继续按纪律持有"],
        }
        return LLMResult(
            content=content, raw="", model="deepseek-chat",
            tokens_in=200, tokens_out=80, cached_in=0, ms=150,
        )


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


def make_sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'eod.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _quote(code, price):
    return Quote(
        code=code, name="测试", price=price, prev_close=price, open=price, high=price,
        low=price, pct_change=0.0, volume=0, amount=0, ts=dt.datetime.now(), source="fake",
    )


def _seed(sf):
    positions = PositionService(session_factory=sf)
    positions.add_position(
        code="600519", qty=100, price=100.0, strategy="value",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    return positions


def test_eod_report_with_advice(tmp_path):
    sf = make_sf(tmp_path)
    positions = _seed(sf)
    llm = FakeLLM()
    notifier = RecordingNotifier()
    report = run_eod(
        data=FakeData(quotes={"600519": _quote("600519", 110.0)}),
        positions=positions,
        llm=llm,
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        now=NOW,
        news_fetcher=_EMPTY_NEWS,
    )
    assert report["sent"] is True
    assert report["ai"] is True
    assert report["advice"] == 1

    body = notifier.messages[0].body
    assert "持仓体检" in body
    assert "止盈线 120.000" in body  # 100×(1+20%)
    assert "AI 总结" in body
    assert "【操作建议】" in body
    assert "持有观察" in body
    assert "继续按纪律持有" in body

    # 发送给模型的上下文包含行情/仓位/走势等关键信息
    assert "组合占比百分比" in llm.last_user
    assert "近期走势" in llm.last_user
    assert "纪律距离" in llm.last_user

    with sf() as session:
        assert len(session.scalars(select(LLMUsage)).all()) == 1


def test_eod_skip_cases(tmp_path):
    sf = make_sf(tmp_path)
    positions = _seed(sf)
    r1 = run_eod(
        data=FakeData(trading=False), positions=positions, session_factory=sf,
        now=NOW, news_fetcher=_EMPTY_NEWS,
    )
    assert r1.get("skipped") == "非交易日"

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir(exist_ok=True)
    empty_sf = make_sf(empty_dir)
    empty_positions = PositionService(session_factory=empty_sf)
    r2 = run_eod(
        data=FakeData(trading=True), positions=empty_positions, session_factory=empty_sf,
        now=NOW, news_fetcher=_EMPTY_NEWS,
    )
    assert r2.get("skipped") == "无持仓"


def test_eod_llm_failure_still_sends(tmp_path):
    sf = make_sf(tmp_path)
    positions = _seed(sf)
    notifier = RecordingNotifier()
    report = run_eod(
        data=FakeData(quotes={"600519": _quote("600519", 110.0)}),
        positions=positions,
        llm=FakeLLM(error=LLMError("llm down")),
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        now=NOW,
        news_fetcher=_EMPTY_NEWS,
    )
    assert report["sent"] is True
    assert report["ai"] is False
    assert report["advice"] == 0
    body = notifier.messages[0].body
    assert "AI 总结" not in body
    assert "持仓体检" in body  # 模板兜底仍完整


def test_eod_quote_failure_returns_error(tmp_path):
    sf = make_sf(tmp_path)
    positions = _seed(sf)
    report = run_eod(
        data=FakeData(fail=True), positions=positions, session_factory=sf,
        now=NOW, news_fetcher=_EMPTY_NEWS,
    )
    assert "error" in report


class FakeFundData(FakeData):
    def get_fund_nav(self, code, days=20):
        return [FundNav(code=code, date=dt.date(2026, 10, 9), nav=1.234)]


def test_eod_includes_fund_nav_and_advice(tmp_path):
    sf = make_sf(tmp_path)
    positions = PositionService(session_factory=sf)
    positions.add_position(
        code="011186", qty=100000, price=1.0, strategy="drip", type="otc_fund",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    notifier = RecordingNotifier()
    report = run_eod(
        data=FakeFundData(),
        positions=positions,
        llm=FakeLLM(),
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        now=NOW,
        news_fetcher=_EMPTY_NEWS,
    )
    assert report["sent"] is True
    body = notifier.messages[0].body
    assert "净值 1.2340" in body
    assert "净值日期 2026-10-09" in body
    assert "【操作建议】" in body


def test_eod_fund_nav_missing_line(tmp_path):
    sf = make_sf(tmp_path)
    positions = PositionService(session_factory=sf)
    positions.add_position(
        code="011186", qty=100000, price=1.0, strategy="drip", type="otc_fund",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    notifier = RecordingNotifier()
    report = run_eod(
        data=FakeData(),  # get_fund_nav 抛错 → 净值缺失
        positions=positions,
        llm=FakeLLM(),
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        now=NOW,
        news_fetcher=_EMPTY_NEWS,
    )
    assert report["sent"] is True
    assert "净值缺失" in notifier.messages[0].body
