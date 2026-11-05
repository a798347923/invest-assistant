"""B1-03 数据源层单元测试（无网络：全部使用假会话/假客户端）。"""

from __future__ import annotations

import datetime as dt

import pytest
import requests
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import (
    Bar,
    DataUnavailable,
    FundNav,
    MarketDataError,
    MarketDataService,
    Quote,
    em_secid,
    normalize_code,
    sina_symbol,
)
from invest_assistant.datasource.em import EastmoneyClient
from invest_assistant.datasource.sina import SinaClient
from invest_assistant.db.models import Base, TradingCalendar
from invest_assistant.db.session import make_engine

# ---------------------------------------------------------------- 假对象

class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self._payload = payload
        self.text = text
        self.status_code = status
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """按序返回预设响应；元素为 Exception 时抛出。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


EM_QUOTES = {
    "rc": 0,
    "data": {
        "total": 2,
        "diff": [
            {"f12": "600519", "f14": "贵州茅台", "f2": 1263.0, "f3": 0.58, "f4": 7.3,
             "f5": 35110, "f6": 4.45e9, "f15": 1282.0, "f16": 1255.4, "f17": 1255.4, "f18": 1255.79},
            {"f12": "000001", "f14": "平安银行", "f2": 11.5, "f3": -0.5, "f4": -0.06,
             "f5": 100, "f6": 1000, "f15": 11.6, "f16": 11.4, "f17": 11.55, "f18": 11.56},
        ],
    },
}

EM_KLINE = {
    "rc": 0,
    "data": {
        "code": "600519",
        "klines": [
            "2026-10-08,1250.00,1260.00,1265.00,1245.00,30000,3.7e9,1.6",
            "2026-10-09,1260.00,1263.00,1270.00,1255.00,35000,4.4e9,1.2",
        ],
    },
}

SINA_LINE = (
    'var hq_str_sh600519="贵州茅台,1255.420,1255.790,1263.000,1282.000,1255.420,1263.000,1263.060,'
    "3511051,4453223189.000,7344,1263.000,200,1262.990,300,1262.980,400,1262.970,500,1262.960,"
    '600,1262.950,700,1262.940,800,1262.930,900,1262.920,1000,1262.910,2026-10-09,15:00:00,00,";'
)


class FakeSource:
    def __init__(self, quotes=None, bars=None, fail=False):
        self.quotes = quotes or {}
        self.bars = bars or []
        self.fail = fail
        self.quote_calls = 0
        self.bar_calls = 0

    def get_quotes(self, codes):
        self.quote_calls += 1
        if self.fail:
            raise MarketDataError("source down")
        return {c: self.quotes[c] for c in codes if c in self.quotes}

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        self.bar_calls += 1
        if self.fail:
            raise MarketDataError("source down")
        return [b for b in self.bars if start <= b.date <= end]


class FakeFund:
    def __init__(self, navs=None, fail=False):
        self.navs = navs or []
        self.fail = fail
        self.calls = 0

    def get_nav_history(self, code):
        self.calls += 1
        if self.fail:
            raise MarketDataError("fund down")
        return list(self.navs)


def make_service(tmp_path, em=None, sina=None, tencent="default", fund=None):
    engine = make_engine(f"sqlite:///{tmp_path / 'svc.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    if tencent == "default":  # 默认注入失败的腾讯源，避免测试触网
        tencent = FakeSource(fail=True)
    service = MarketDataService(em=em, sina=sina, tencent=tencent, fund=fund, session_factory=sf)
    return service, sf


def _quote(code="600519", source="test"):
    return Quote(code=code, name="测试", price=1.0, prev_close=1.0, open=1.0, high=1.0,
                 low=1.0, pct_change=0.0, volume=0.0, amount=0.0,
                 ts=dt.datetime.now(), source=source)


# ---------------------------------------------------------------- 代码工具

def test_code_helpers():
    assert normalize_code("sh600519") == "600519"
    assert normalize_code(" 510300 ") == "510300"
    assert em_secid("600519") == "1.600519"
    assert em_secid("000001") == "0.000001"
    assert sina_symbol("300750") == "sz300750"
    assert sina_symbol("510300") == "sh510300"
    with pytest.raises(ValueError):
        normalize_code("abc")


# ---------------------------------------------------------------- 客户端解析

def test_em_quotes_parse():
    client = EastmoneyClient(session=FakeSession([FakeResponse(payload=EM_QUOTES)]))
    quotes = client.get_quotes(["600519", "000001"])
    assert quotes["600519"].price == 1263.0
    assert quotes["600519"].pct_change == 0.58
    assert quotes["000001"].name == "平安银行"


def test_em_daily_parse():
    client = EastmoneyClient(session=FakeSession([FakeResponse(payload=EM_KLINE)]))
    bars = client.get_daily_bars("600519", dt.date(2026, 10, 1), dt.date(2026, 10, 10))
    assert len(bars) == 2
    assert bars[-1].close == 1263.0
    assert bars[-1].adjust == "qfq"


def test_em_retry_recovers():
    flaky = FakeSession([requests.ConnectionError("boom"), FakeResponse(payload=EM_QUOTES)])
    client = EastmoneyClient(session=flaky)
    quotes = client.get_quotes(["600519"])
    assert "600519" in quotes
    assert len(flaky.calls) == 2  # 第一次失败后被 tenacity 重试


def test_sina_parse_line():
    quote = SinaClient.parse_hq_line(SINA_LINE)
    assert quote is not None
    assert quote.code == "600519"
    assert quote.price == 1263.0
    assert quote.prev_close == 1255.79
    assert quote.open == 1255.42
    assert abs(quote.pct_change - 0.574) < 0.01
    assert quote.ts == dt.datetime(2026, 10, 9, 15, 0, 0)


def test_sina_quotes_via_fake_session():
    line2 = SINA_LINE.replace("sh600519", "sh510300").replace("贵州茅台", "沪深300ETF")
    session = FakeSession([FakeResponse(text=SINA_LINE + "\n" + line2 + "\n")])
    client = SinaClient(session=session)
    quotes = client.get_quotes(["600519", "510300"])
    assert set(quotes) == {"600519", "510300"}


# ---------------------------------------------------------------- 服务回退与缓存

def test_service_quotes_fallback_to_sina(tmp_path):
    svc, _ = make_service(
        tmp_path, em=FakeSource(fail=True), sina=FakeSource(quotes={"600519": _quote(source="sina")})
    )
    out = svc.get_quotes(["600519"])
    assert out["600519"].source == "sina"


def test_service_quotes_all_fail(tmp_path):
    svc, _ = make_service(tmp_path, em=FakeSource(fail=True), sina=FakeSource(fail=True))
    with pytest.raises(DataUnavailable):
        svc.get_quotes(["600519"])


def test_service_bars_cached(tmp_path):
    bars = [
        Bar(code="600519", date=dt.date(2026, 10, 8), open=1, high=1, low=1, close=1260, volume=1),
        Bar(code="600519", date=dt.date(2026, 10, 9), open=1, high=1, low=1, close=1263, volume=1),
    ]
    em = FakeSource(bars=bars)
    svc, _ = make_service(tmp_path, em=em, sina=FakeSource(fail=True))
    r1 = svc.get_daily_bars("600519", dt.date(2026, 10, 1), dt.date(2026, 10, 9))
    r2 = svc.get_daily_bars("600519", dt.date(2026, 10, 1), dt.date(2026, 10, 9))
    assert len(r1) == 2 and len(r2) == 2
    assert em.bar_calls == 1  # 第二次命中缓存，不再请求
    assert r2[-1].close == 1263


def test_service_bars_fallback_raw(tmp_path):
    bars = [Bar(code="600519", date=dt.date(2026, 10, 9), open=1, high=1, low=1,
                close=1263, volume=1, adjust="raw")]
    svc, _ = make_service(tmp_path, em=FakeSource(fail=True), sina=FakeSource(bars=bars))
    out = svc.get_daily_bars("600519", dt.date(2026, 10, 1), dt.date(2026, 10, 9))
    assert out[-1].adjust == "raw"


def test_service_bars_backfill_when_history_short(tmp_path):
    """窗口扩大导致历史覆盖不足时应自动补齐，补齐后不再重复拉取。"""
    bars = [
        Bar(code="600519", date=dt.date(2026, 8, 3), open=1, high=1, low=1, close=1200, volume=1),
        Bar(code="600519", date=dt.date(2026, 10, 8), open=1, high=1, low=1, close=1260, volume=1),
        Bar(code="600519", date=dt.date(2026, 10, 9), open=1, high=1, low=1, close=1263, volume=1),
    ]
    em = FakeSource(bars=bars)
    svc, _ = make_service(tmp_path, em=em)
    shallow = svc.get_daily_bars("600519", dt.date(2026, 10, 8), dt.date(2026, 10, 9))
    assert len(shallow) == 2 and em.bar_calls == 1
    deep = svc.get_daily_bars("600519", dt.date(2026, 6, 1), dt.date(2026, 10, 9))
    assert len(deep) == 3 and em.bar_calls == 2  # 覆盖不足 → 补齐
    again = svc.get_daily_bars("600519", dt.date(2026, 6, 1), dt.date(2026, 10, 9))
    assert len(again) == 3 and em.bar_calls == 2  # 已覆盖 → 不再拉取


def test_calendar_fallback_and_cache(tmp_path):
    svc, sf = make_service(tmp_path, em=FakeSource(fail=True), sina=FakeSource(fail=True))
    # 无缓存：工作日回退判断（2026-10-11 为周日 → False；10-12 周一 → True）
    assert svc.is_trading_day(dt.date(2026, 10, 11)) is False
    assert svc.is_trading_day(dt.date(2026, 10, 12)) is True
    # 写入缓存后以缓存为准
    with sf() as s:
        s.add(TradingCalendar(date=dt.date(2026, 10, 12), is_open=False))
        s.commit()
    assert svc.is_trading_day(dt.date(2026, 10, 12)) is False


def test_fund_nav_cached(tmp_path):
    navs = [FundNav(code="011186", date=dt.date.today(), nav=0.69)]
    fund = FakeFund(navs=navs)
    svc, _ = make_service(tmp_path, em=FakeSource(fail=True), sina=FakeSource(fail=True), fund=fund)
    r1 = svc.get_fund_nav("011186", days=5)
    r2 = svc.get_fund_nav("011186", days=5)
    assert len(r1) == 1 and len(r2) == 1
    assert fund.calls == 1  # 第二遍命中缓存


def test_fund_nav_unavailable(tmp_path):
    svc, _ = make_service(
        tmp_path, em=FakeSource(fail=True), sina=FakeSource(fail=True), fund=FakeFund(fail=True)
    )
    with pytest.raises(DataUnavailable):
        svc.get_fund_nav("011186")


def test_sina_daily_bars_explicit_symbol():
    import json as _json

    rows = [
        {"day": "2026-10-09", "open": "4000", "high": "4050", "low": "3990",
         "close": "4020", "volume": "1000"}
    ]
    session = FakeSession([FakeResponse(text=_json.dumps(rows))])
    client = SinaClient(session=session)
    bars = client.get_daily_bars("sh000300", dt.date(2026, 10, 1), dt.date(2026, 10, 10))
    assert len(bars) == 1
    assert bars[0].code == "000300"
    assert "symbol=sh000300" in session.calls[0]


def test_sina_daily_bars_from_six_digit_code():
    import json as _json

    rows = [
        {"day": "2026-10-09", "open": "10", "high": "11", "low": "9", "close": "10.5", "volume": "1"}
    ]
    session = FakeSession([FakeResponse(text=_json.dumps(rows))])
    client = SinaClient(session=session)
    bars = client.get_daily_bars("600519", dt.date(2026, 10, 1), dt.date(2026, 10, 10))
    assert bars[0].code == "600519"
    assert "symbol=sh600519" in session.calls[0]


def _em_row(code: str) -> dict:
    return {"f12": code, "f14": "测试", "f2": 1.0, "f3": 0.1, "f6": 1.0,
            "f8": 1.0, "f9": 10.0, "f20": 1e8, "f23": 1.0}


def test_em_market_snapshot_pagination_until_total():
    page1 = {"data": {"total": 3, "diff": [_em_row("600519"), _em_row("000001")]}}
    page2 = {"data": {"total": 3, "diff": [_em_row("300750")]}}
    session = FakeSession([FakeResponse(payload=page1), FakeResponse(payload=page2)])
    client = EastmoneyClient(session=session)
    rows = client.get_market_snapshot(max_pages=5, page_size=100)
    assert [r["code"] for r in rows] == ["600519", "000001", "300750"]
    assert len(session.calls) == 2  # 达到 total 后停止
