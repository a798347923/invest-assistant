"""技术指标纯函数测试（第二批）。"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from invest_assistant.domain.indicators import (
    atr,
    boll,
    macd,
    percentile_rank,
    rsi,
    sma,
    weekly_trend,
)


def _bars(closes, start=dt.date(2026, 1, 1)):
    return [
        SimpleNamespace(
            date=start + dt.timedelta(days=i),
            high=close * 1.01,
            low=close * 0.99,
            close=close,
        )
        for i, close in enumerate(closes)
    ]


def test_sma_boll_rsi():
    rising = [10 + i * 0.1 for i in range(40)]
    assert sma(rising, 5) is not None
    assert sma([1, 2], 5) is None

    b = boll(rising)
    assert b is not None and b["pos_pct"] >= 90  # 上升趋势贴近上轨

    r = rsi(rising)
    assert r is not None and r > 90
    assert rsi([1, 1, 1, 1], 14) is None
    assert rsi([5.0] * 20, 14) == 50.0  # 完全横盘


def test_macd_trend():
    rising = [10 + i * 0.2 for i in range(60)]
    m = macd(rising)
    assert m is not None and m["dif"] > m["dea"] and m["trend"] == "金叉/多头"
    falling = [30 - i * 0.2 for i in range(60)]
    m2 = macd(falling)
    assert m2 is not None and m2["dif"] < m2["dea"] and m2["trend"] == "死叉/空头"
    assert macd([1, 2, 3]) is None


def test_atr_and_percentile():
    bars = _bars([10.0] * 16)  # 高低各 ±1% → ATR ≈ 0.2
    value = atr(bars, 14)
    assert value is not None and 0.15 < value < 0.25
    assert atr(bars[:5], 14) is None

    values = [1, 2, 3, 4, 5]
    assert percentile_rank(values, 5) == 100.0
    assert percentile_rank(values, 3) == 60.0
    assert percentile_rank([], 1) is None


def test_weekly_trend():
    closes = [10 + i * 0.05 for i in range(160)]  # 约 23 周
    w = weekly_trend(_bars(closes))
    assert w is not None
    assert w["above"] is True
    assert w["pct5"] is not None
    assert weekly_trend(_bars(closes[:20])) is None
