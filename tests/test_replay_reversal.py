"""超跌反转形态（实验，默认关闭）的单元测试。

形态语义：收盘低于 MA20 达 rev_drop_pct、当日阳线且高于昨收（止跌）、
且不在自由落体中（近 10 日跌幅未超过 rev_freefall_pct）→ 次日开盘进场。
"""

from __future__ import annotations

import datetime as dt

import pytest

from invest_assistant.datasource import Bar
from invest_assistant.engine.playbook_replay import (
    SETUP_BREAKOUT,
    SETUP_REVERSAL,
    _index_ma20_slope_pct,
    _index_ma60_relation,
    _signal,
)

RULES = {
    "weak_block": "none", "above_ma5": True, "pullback_enhanced": True,
    "atr_stop": True, "raw_gate": True, "enhanced": True,
}
OPTS = {
    "allow_setup": "reversal", "enable_reversal": True,
    "rev_drop_pct": 0.05, "rev_stop_pct": 0.04, "rev_target": 0.06,
    "max_hold_reversal": 8,
}


def build_bars(tail: list[tuple[float, float, float, float]]) -> list[Bar]:
    """前 66 根横盘 100，接 tail（最后一个是信号日），再补一根「次日开盘」用于成交。"""
    start = dt.date(2025, 1, 1)
    bars: list[Bar] = []
    day = start
    for _ in range(66):
        bars.append(Bar(code="600000", date=day, open=100.0, high=101.0, low=99.0,
                        close=100.0, volume=1_000_000, amount=0.0, adjust="qfq"))
        day += dt.timedelta(days=1)
    for o, h, low, c in tail:
        bars.append(Bar(code="600000", date=day, open=o, high=h, low=low, close=c,
                        volume=1_000_000, amount=0.0, adjust="qfq"))
        day += dt.timedelta(days=1)
    last = bars[-1]
    bars.append(Bar(code="600000", date=day, open=last.close, high=last.close + 1,
                    low=last.close - 1, close=last.close,
                    volume=1_000_000, amount=0.0, adjust="qfq"))
    return bars


def signal_at(bars: list[Bar], opts: dict, index_ok: bool = False):
    # 信号在「倒数第二根」收盘评估，最后一根是次日开盘（引擎要求存在下一根）
    i = len(bars) - 2
    return _signal(
        bars, i, index_ok=index_ok, rules=RULES,
        stop_pullback=0.98, stop_breakout=0.95,
        raw_index_ok=False, opts=opts, index_series=None,
    )


def test_reversal_fires_on_oversold_bounce():
    bars = build_bars([
        (87.0, 88.0, 85.0, 88.0),   # 前一日大跌
        (89.0, 91.0, 88.5, 90.0),   # 信号日：阳线、高于昨收、低于 MA20×0.95
    ])
    sig = signal_at(bars, OPTS)
    assert sig is not None
    setup, stop, target = sig
    assert setup == SETUP_REVERSAL
    close = bars[-1].close
    assert target == pytest.approx(close * 1.06, rel=1e-9)
    assert stop == pytest.approx(close * 0.96, rel=1e-9)


def test_reversal_needs_bullish_bar():
    bars = build_bars([
        (100.0, 100.5, 99.5, 100.0),
        (98.0, 99.0, 94.0, 95.0),
        (91.5, 92.0, 88.5, 90.0),   # 阴线（close < open）→ 不算止跌
    ])
    assert signal_at(bars, OPTS) is None


def test_reversal_skips_freefall():
    bars = build_bars([
        (100.0, 100.5, 99.5, 100.0),
        (69.0, 71.0, 67.0, 68.0),
        (68.5, 71.0, 67.5, 70.0),   # 距 10 日高点已跌 30%，属于接飞刀
    ])
    assert signal_at(bars, OPTS) is None


def test_reversal_requires_oversold_depth():
    bars = build_bars([
        (100.0, 100.5, 99.5, 100.0),
        (99.0, 99.5, 98.5, 99.0),
        (98.0, 98.5, 97.5, 98.0),   # 仅低于 MA20 约 1.5%，不满足 5%
    ])
    assert signal_at(bars, OPTS) is None


def test_reversal_off_by_default():
    """不传 allow_setup 时行为不变（不会凭空出现反转信号）。"""
    bars = build_bars([
        (100.0, 100.5, 99.5, 100.0),
        (98.0, 99.0, 94.0, 95.0),
        (89.0, 91.0, 88.5, 90.0),
    ])
    sig = signal_at(bars, {"max_hold": 20})
    assert sig is None or sig[0] != SETUP_REVERSAL


def test_allow_setup_breakout_excludes_others():
    """allow_setup=breakout 时只保留突破形态。"""
    bars = build_bars([
        (100.0, 100.5, 99.5, 100.0),
        (98.0, 99.0, 94.0, 95.0),
        (89.0, 91.0, 88.5, 90.0),
    ])
    sig = signal_at(bars, {"allow_setup": "breakout"})
    assert sig is None or sig[0] == SETUP_BREAKOUT


# ---------------------------------------------------------------- 指数闸门

def test_index_ma60_relation_above_below_and_insufficient():
    base = dt.date(2025, 1, 1)
    series = [(base + dt.timedelta(days=i), 100.0) for i in range(80)]
    day = series[-1][0]
    assert _index_ma60_relation(series, day) == "above"
    crashed = series[:-1] + [(day, 60.0)]   # 末日暴跌 → 跌破 MA60
    assert _index_ma60_relation(crashed, day) == "below"
    assert _index_ma60_relation(series[:30], day) is None


def test_index_ma20_slope_flat_is_zero():
    base = dt.date(2025, 1, 1)
    series = [(base + dt.timedelta(days=i), 100.0) for i in range(40)]
    assert _index_ma20_slope_pct(series, series[-1][0], 5) == pytest.approx(0.0)
    assert _index_ma20_slope_pct(series[:10], series[-1][0], 5) is None
