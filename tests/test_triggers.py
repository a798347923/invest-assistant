"""`engine.triggers` 纯判定函数的边界测试。

这些函数决定「什么时候提醒用户」，此前只能通过完整轮询间接覆盖；
抽出后可以精确表达边界：恰好等于止损价、恰好等于区间上沿、优先级冲突等。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from invest_assistant.engine.triggers import (
    plan_triggers,
    pullback_enhanced_problem,
    trading_days_held,
    watch_triggers,
)


def bar(day: dt.date, high: float, close: float):
    return SimpleNamespace(date=day, high=high, close=close)


# ---------------------------------------------------------------- 持有天数

def test_trading_days_held_skips_weekend():
    monday = dt.datetime(2026, 10, 12, 10, 0)   # 周一
    friday = dt.datetime(2026, 10, 16, 10, 0)   # 周五
    assert trading_days_held(monday, friday) == 4


def test_trading_days_held_across_weekend():
    friday = dt.datetime(2026, 10, 16, 10, 0)
    monday = dt.datetime(2026, 10, 19, 10, 0)
    assert trading_days_held(friday, monday) == 1


def test_trading_days_held_same_day_is_zero():
    now = dt.datetime(2026, 10, 12, 10, 0)
    assert trading_days_held(now, now) == 0


# ---------------------------------------------------------------- AI 计划到价

def test_plan_stop_fires_at_or_below_stop_price():
    plan = {"stop": 10.0}
    assert plan_triggers(plan, 10.0)[0][0] == "plan_stop"   # 恰好等于即触发
    assert plan_triggers(plan, 9.9)[0][0] == "plan_stop"
    assert plan_triggers(plan, 10.01) == []


def test_plan_stop_takes_priority_over_buy_zone():
    """计划已作废时不能同时提示买入。"""
    plan = {"stop": 10.0, "zone_low": 9.5, "zone_high": 12.0}
    triggers = plan_triggers(plan, 9.8)
    assert [kind for kind, _ in triggers] == ["plan_stop"]


def test_plan_breakout_fires_above_confirm_price():
    plan = {"breakout": 12.0, "zone_high": 11.0}
    assert plan_triggers(plan, 12.0)[0][0] == "plan_buy"
    assert "突破" in plan_triggers(plan, 12.0)[0][1]


def test_plan_zone_inside_and_boundaries():
    plan = {"zone_low": 10.0, "zone_high": 11.0}
    assert plan_triggers(plan, 10.5)[0][0] == "plan_buy"
    assert "买入区间" in plan_triggers(plan, 11.0)[0][1]   # 上沿仍算进入
    assert "下沿" in plan_triggers(plan, 9.9)[0][1]        # 低于下沿提醒复核
    assert plan_triggers(plan, 11.1) == []                 # 高于上沿不提醒


def test_plan_zone_without_lower_bound():
    plan = {"zone_high": 11.0}
    assert plan_triggers(plan, 11.0)[0][0] == "plan_buy"


def test_plan_without_levels_never_triggers():
    assert plan_triggers({}, 10.0) == []


# ---------------------------------------------------------------- 自选到价/异动

def watch_item(alert_pct=None, above=None, below=None):
    return SimpleNamespace(alert_pct=alert_pct, alert_price_above=above, alert_price_below=below)


def test_watch_pct_uses_default_threshold():
    item = watch_item(alert_pct=None)
    assert watch_triggers(item, 5.0, 10.0, -5.0)[0][0] == "pct"
    assert watch_triggers(item, 5.0, 10.0, -4.99) == []


def test_watch_pct_per_item_override():
    item = watch_item(alert_pct=2.0)
    assert watch_triggers(item, 5.0, 10.0, 2.5)[0][0] == "pct"


def test_watch_pct_zero_disables_check():
    item = watch_item(alert_pct=0)
    assert watch_triggers(item, 5.0, 10.0, 99.0) == []


def test_watch_price_levels_can_fire_together():
    item = watch_item(alert_pct=None, above=11.0, below=9.0)
    kinds = [kind for kind, _ in watch_triggers(item, 5.0, 11.0, None)]
    assert kinds == ["above"]
    # 极端行情下区间判断互相独立（下方价高于现价属于配置错误，这里只验证不抛异常）
    assert watch_triggers(item, 5.0, 8.0, None)[0][0] == "below"


# ---------------------------------------------------------------- 回踩增强

def test_pullback_without_prior_bars_passes():
    today = dt.date(2026, 10, 16)
    assert pullback_enhanced_problem([], 10.0, today) == ""


def test_pullback_requires_reclaiming_previous_high():
    today = dt.date(2026, 10, 16)
    bars = [bar(dt.date(2026, 10, 15), high=10.0, close=9.8)]
    assert pullback_enhanced_problem(bars, 9.9, today) == "未收复前一日高点"
    assert pullback_enhanced_problem(bars, 10.1, today) == ""


def test_pullback_rejects_falling_ma20():
    today = dt.date(2026, 10, 16)
    # 收盘逐日走低 → MA20 < 前一段 MA20
    bars = [
        bar(today - dt.timedelta(days=30 - i), high=100.0, close=100.0 - i)
        for i in range(30)
    ]
    assert pullback_enhanced_problem(bars, 200.0, today) == "MA20 未上行"


def test_pullback_rejects_price_under_ma60():
    today = dt.date(2026, 10, 16)
    # 前 35 日 200（抬高 MA60），后 25 日 100（MA20 走平不掉头）
    closes = [200.0] * 35 + [100.0] * 25
    bars = [
        bar(today - dt.timedelta(days=60 - i), high=c + 0.5, close=c)
        for i, c in enumerate(closes)
    ]
    # MA60 = (200*35 + 100*25)/60 ≈ 158.3 → 门槛 ≈ 157.5；现价须先高于前一日高点 100.5
    assert pullback_enhanced_problem(bars, 120.0, today) == "未站稳 MA60"
    assert pullback_enhanced_problem(bars, 160.0, today) == ""
