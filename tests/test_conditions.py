"""买入确认条件本地判定测试（缩量/放量/收阳/站上均线）。"""

from __future__ import annotations

import datetime as dt

from invest_assistant.datasource.base import Bar
from invest_assistant.domain.conditions import (
    confirm_labels,
    evaluate_confirmations,
    infer_confirmations,
    session_elapsed_minutes,
)

NOW_1100 = dt.datetime(2026, 10, 12, 11, 0)  # 已开市 90 分钟


def _bars(volumes=None, closes=None, days: int = 6):
    volumes = volumes or [10000.0] * days
    closes = closes or [10.0] * days
    return [
        Bar(
            code="600519",
            date=dt.date(2026, 10, 1) + dt.timedelta(days=i),
            open=closes[i], high=closes[i] * 1.01, low=closes[i] * 0.99,
            close=closes[i], volume=volumes[i], amount=0.0, adjust="qfq",
        )
        for i in range(days)
    ]


def test_session_elapsed():
    assert session_elapsed_minutes(dt.time(9, 40)) == 10
    assert session_elapsed_minutes(dt.time(11, 30)) == 120
    assert session_elapsed_minutes(dt.time(12, 0)) == 120
    assert session_elapsed_minutes(dt.time(14, 0)) == 180
    assert session_elapsed_minutes(dt.time(15, 30)) == 240


def test_volume_shrink_and_expand():
    bars = _bars()
    ok, details = evaluate_confirmations(
        ["volume_shrink"], price=10.0, open_price=10.0, volume=2500.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert ok and "缩量✓" in details[0]  # 2500×240/90≈6667 → 0.67 倍
    ok2, details2 = evaluate_confirmations(
        ["volume_shrink"], price=10.0, open_price=10.0, volume=5000.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert not ok2 and "缩量✗" in details2[0]
    ok3, _ = evaluate_confirmations(
        ["volume_expand"], price=10.0, open_price=10.0, volume=6000.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert ok3  # 1.6 倍


def test_volume_guards():
    bars = _bars()
    ok, details = evaluate_confirmations(
        ["volume_shrink"], price=10.0, open_price=10.0, volume=100.0,
        source="em", bars=bars, now=dt.datetime(2026, 10, 12, 9, 40),
    )
    assert not ok and "开盘不足" in details[0]
    ok2, details2 = evaluate_confirmations(
        ["volume_shrink"], price=10.0, open_price=10.0, volume=0.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert not ok2 and "缺失" in details2[0]
    # 量纲异常防护（如行情与K线单位不一致会差 100 倍）→ 保守不确认
    ok3, details3 = evaluate_confirmations(
        ["volume_expand"], price=10.0, open_price=10.0, volume=6000.0 * 100,
        source="em", bars=bars, now=NOW_1100,
    )
    assert not ok3 and "存疑" in details3[0]
    # 未知行情源
    ok4, details4 = evaluate_confirmations(
        ["volume_shrink"], price=10.0, open_price=10.0, volume=100.0,
        source="unknown", bars=bars, now=NOW_1100,
    )
    assert not ok4


def test_bullish_and_above_ma():
    bars = _bars(closes=[9.8] * 6)
    ok, details = evaluate_confirmations(
        ["bullish", "above_ma5"], price=10.0, open_price=9.9, volume=0.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert ok
    assert any("收阳/收稳✓" in d for d in details)
    assert any("站上MA5✓" in d for d in details)

    ok2, details2 = evaluate_confirmations(
        ["bullish"], price=9.7, open_price=9.9, volume=0.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert not ok2 and "✗" in details2[0]

    ok3, details3 = evaluate_confirmations(
        ["above_ma20"], price=9.9, open_price=9.8, volume=0.0,
        source="em", bars=_bars(days=10), now=NOW_1100,
    )
    assert not ok3 and "K线不足" in details3[0]

    # 未知条件按未满足处理
    ok4, details4 = evaluate_confirmations(
        ["mystery"], price=10.0, open_price=10.0, volume=0.0,
        source="em", bars=bars, now=NOW_1100,
    )
    assert not ok4 and "未知条件" in details4[0]


def test_infer_confirmations_and_labels():
    plan = {"zone_text": "9.3~9.8", "triggers": ["放量阳线收复 MA5", "站上 10 日线"]}
    assert infer_confirmations(plan) == ["volume_expand", "bullish", "above_ma5", "above_ma10"]
    legacy = {"zone_text": "", "triggers": ["回踩 21.8 附近缩量收稳"]}
    assert infer_confirmations(legacy) == ["volume_shrink", "bullish"]
    assert infer_confirmations({"triggers": []}) == []
    assert infer_confirmations(None) == []
    assert confirm_labels(["volume_shrink", "bullish"]) == "缩量、收阳/收稳"
