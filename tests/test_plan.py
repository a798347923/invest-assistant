"""AI 买入计划解析（domain.plan）测试。"""

from __future__ import annotations

from invest_assistant.domain.plan import parse_plan_levels


def test_parse_explicit_numeric_fields():
    content = {
        "plan": {
            "entry_type": "回踩",
            "entry_low": 21.6,
            "entry_high": 22.2,
            "breakout_price": 24.0,
            "stop_price": 20.8,
            "entry_zone": "21.6~22.2",
            "triggers": ["回踩 21.8 附近缩量收稳"],
            "stop_level": "跌破 20.8",
            "position_hint": "首仓 3 成",
        }
    }
    plan = parse_plan_levels(content)
    assert plan is not None
    assert plan["zone_low"] == 21.6
    assert plan["zone_high"] == 22.2
    assert plan["breakout"] == 24.0
    assert plan["stop"] == 20.8
    assert plan["triggers_text"].startswith("回踩")
    assert plan["position_hint"] == "首仓 3 成"


def test_parse_text_fallback_range_and_stop():
    content = {"plan": {"entry_zone": "84.88 ~ 90.00", "stop_level": "跌破 76.01"}}
    plan = parse_plan_levels(content)
    assert plan["zone_low"] == 84.88
    assert plan["zone_high"] == 90.0
    assert plan["stop"] == 76.01
    assert plan["breakout"] is None


def test_parse_breakout_single_number():
    content = {
        "plan": {"entry_zone": "放量站上 24.00", "triggers": ["放量站上 24.00 突破确认"]}
    }
    plan = parse_plan_levels(content)
    assert plan["zone_low"] is None and plan["zone_high"] is None
    assert plan["breakout"] == 24.0


def test_parse_explicit_breakout_type_uses_low():
    content = {"plan": {"entry_type": "突破", "entry_low": 24.0, "entry_high": 24.0}}
    plan = parse_plan_levels(content)
    assert plan["breakout"] == 24.0
    assert plan["zone_low"] is None and plan["zone_high"] is None


def test_parse_no_numbers_returns_none():
    assert parse_plan_levels({"plan": {"entry_zone": "观望", "stop_level": ""}}) is None
    assert parse_plan_levels({}) is None
    assert parse_plan_levels(None) is None
