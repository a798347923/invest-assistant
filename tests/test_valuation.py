"""估值助手测试（股票行情 + 基金净值混合，无网络）。"""

from __future__ import annotations

import datetime as dt

from invest_assistant.datasource import FundNav, Quote
from invest_assistant.domain.valuation import valuate_positions


class FakeData:
    def __init__(self, quotes=None, navs=None, quote_fail=False, nav_fail=False):
        self.quotes = quotes or {}
        self.navs = navs or {}
        self.quote_fail = quote_fail
        self.nav_fail = nav_fail

    def get_quotes(self, codes):
        if self.quote_fail:
            raise RuntimeError("quote down")
        return {c: q for c, q in self.quotes.items() if c in codes}

    def get_fund_nav(self, code, days=2):
        if self.nav_fail or code not in self.navs:
            raise RuntimeError("nav down")
        return self.navs[code]


def _quote(code, price):
    return Quote(
        code=code, name="测试", price=price, prev_close=price, open=price, high=price,
        low=price, pct_change=0.0, volume=0, amount=0, ts=dt.datetime.now(), source="fake",
    )


def test_mixed_valuation():
    data = FakeData(
        quotes={"600519": _quote("600519", 1280.0)},
        navs={"011186": [FundNav(code="011186", date=dt.date(2026, 10, 12), nav=0.6908)]},
    )
    out = valuate_positions(None, {"600519": "a_share", "011186": "otc_fund"}, data=data)
    assert out["prices"]["600519"] == 1280.0
    assert out["labels"]["600519"] == "现价"
    assert out["prices"]["011186"] == 0.6908
    assert out["labels"]["011186"] == "净值(2026-10-12)"
    assert out["errors"] == []


def test_stock_failure_recorded_fund_failure_tolerated():
    data = FakeData(quote_fail=True, nav_fail=True)
    out = valuate_positions(None, {"600519": "a_share", "011186": "otc_fund"}, data=data)
    assert out["prices"] == {}
    assert len(out["errors"]) == 1
    assert "股票行情不可用" in out["errors"][0]


def test_funds_only_produces_no_errors():
    data = FakeData(
        navs={"011186": [FundNav(code="011186", date=dt.date(2026, 10, 12), nav=0.6908)]}
    )
    out = valuate_positions(None, {"011186": "otc_fund"}, data=data)
    assert out["errors"] == []
    assert out["prices"]["011186"] == 0.6908
