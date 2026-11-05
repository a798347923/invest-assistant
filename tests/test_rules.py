"""B1-05 规则引擎测试（纯函数，无 IO）。"""

from __future__ import annotations

import datetime as dt

import pytest

from invest_assistant.domain.rules import (
    RuleContext,
    describe,
    evaluate,
    reset_state,
)

NOW = dt.datetime(2026, 10, 11, 14, 0, 0)
OPENED = dt.datetime(2026, 9, 1, 9, 30, 0)


def ctx(price, cost=100.0, prev_close=None, high=None, low=None, now=NOW, opened=OPENED):
    return RuleContext(price=price, cost=cost, now=now, opened_at=opened,
                       prev_close=prev_close, high=high, low=low)


# ---------------------------------------------------------------- 固定止盈

def test_take_profit_triggers_at_or_above_target():
    _, trigger = evaluate("fixed_take_profit", {"pct": 20}, {}, ctx(119.99))
    assert trigger is None  # 未达到目标
    state, trigger = evaluate("fixed_take_profit", {"pct": 20}, {}, ctx(120.0))
    assert trigger is not None  # 达到即触发
    assert state["triggered"] is True
    assert "止盈" in trigger.reason


def test_take_profit_latches():
    state, _ = evaluate("fixed_take_profit", {"pct": 20}, {}, ctx(125.0))
    state2, trigger2 = evaluate("fixed_take_profit", {"pct": 20}, state, ctx(130.0))
    assert trigger2 is None
    assert state2["triggered"] is True


def test_take_profit_backtest_uses_high():
    _, trigger = evaluate("fixed_take_profit", {"pct": 20}, {}, ctx(118.0, high=121.0))
    assert trigger is not None


# ---------------------------------------------------------------- 固定止损

def test_stop_loss_triggers_at_or_below_target():
    _, trigger = evaluate("fixed_stop_loss", {"pct": 10}, {}, ctx(90.01))
    assert trigger is None  # 未跌破防线
    state, trigger = evaluate("fixed_stop_loss", {"pct": 10}, {}, ctx(90.0))
    assert trigger is not None  # 跌破即触发
    assert state["triggered"] is True


def test_stop_loss_backtest_uses_low():
    _, trigger = evaluate("fixed_stop_loss", {"pct": 10}, {}, ctx(92.0, low=89.0))
    assert trigger is not None


# ---------------------------------------------------------------- 移动止盈

def test_trailing_updates_peak_and_triggers_on_drawdown():
    state, trigger = evaluate("trailing_stop", {"drawdown_pct": 8}, {}, ctx(110.0))
    assert trigger is None
    assert state["peak"] == 110.0

    state, trigger = evaluate("trailing_stop", {"drawdown_pct": 8}, state, ctx(115.0))
    assert trigger is None
    assert state["peak"] == 115.0

    _, trigger = evaluate("trailing_stop", {"drawdown_pct": 8}, state, ctx(105.9))
    assert trigger is None  # 高于止盈线

    # 回撤 8% → 线 = 115×0.92 = 105.8，等于线即触发
    state, trigger = evaluate("trailing_stop", {"drawdown_pct": 8}, state, ctx(105.8))
    assert trigger is not None
    assert state["triggered"] is True
    assert "回撤" in trigger.reason


def test_trailing_initial_peak_is_cost():
    state, _ = evaluate("trailing_stop", {"drawdown_pct": 10}, {}, ctx(95.0, cost=100.0))
    assert state["peak"] == 100.0  # 由成本初始化
    assert "triggered" not in state


def test_trailing_reset_after_manual_handling():
    state, _ = evaluate("trailing_stop", {"drawdown_pct": 8}, {}, ctx(112.0))
    state, trigger = evaluate("trailing_stop", {"drawdown_pct": 8}, state, ctx(102.0))
    assert trigger is not None
    new_state = reset_state("trailing_stop", state, ctx(103.0))
    assert new_state == {"peak": 103.0}
    _, trigger2 = evaluate("trailing_stop", {"drawdown_pct": 8}, new_state, ctx(94.76, cost=103.0))
    assert trigger2 is not None  # 复位后可再次触发（103×0.92=94.76）


def test_trailing_backtest_peak_uses_high_low_check():
    # 第一根：high 120 抬升峰值，但 low 115 未跌破线（120×0.92=110.4）
    state, trigger = evaluate(
        "trailing_stop", {"drawdown_pct": 8}, {},
        ctx(price=118.0, high=120.0, low=115.0, cost=100.0),
    )
    assert trigger is None
    assert state["peak"] == 120.0
    # 第二根：峰值维持 120，low 110 跌破 110.4 → 触发
    _, trigger = evaluate(
        "trailing_stop", {"drawdown_pct": 8}, state,
        ctx(price=111.0, high=111.0, low=110.0),
    )
    assert trigger is not None


# ---------------------------------------------------------------- 日内异动

def test_daily_move_fires_once_per_day():
    _, trigger = evaluate("daily_move", {"pct": 5}, {}, ctx(106.0, prev_close=100.0))
    assert trigger is not None
    state, _ = evaluate("daily_move", {"pct": 5}, {}, ctx(106.0, prev_close=100.0))
    _, trigger2 = evaluate("daily_move", {"pct": 5}, state, ctx(95.0, prev_close=100.0))
    assert trigger2 is None  # 当日已触发
    # 次日可再触发
    tomorrow = NOW + dt.timedelta(days=1)
    _, trigger3 = evaluate(
        "daily_move", {"pct": 5}, state, ctx(94.0, prev_close=100.0, now=tomorrow)
    )
    assert trigger3 is not None
    assert "下跌" in trigger3.reason


def test_daily_move_needs_prev_close():
    _, trigger = evaluate("daily_move", {"pct": 5}, {}, ctx(106.0))
    assert trigger is None


# ---------------------------------------------------------------- 最长持有

def test_max_holding_triggers_after_days():
    early = OPENED + dt.timedelta(days=10)
    _, trigger = evaluate("max_holding", {"days": 30}, {}, ctx(100.0, now=early))
    assert trigger is None  # 未到期
    state, trigger = evaluate(
        "max_holding", {"days": 30}, {}, ctx(100.0, now=OPENED + dt.timedelta(days=30))
    )
    assert trigger is not None  # 到期提醒
    _, trigger2 = evaluate(
        "max_holding", {"days": 30}, state, ctx(100.0, now=OPENED + dt.timedelta(days=31))
    )
    assert trigger2 is None  # 锁存


# ---------------------------------------------------------------- 描述

def test_describe():
    assert describe("fixed_take_profit", {"pct": 20}) == "止盈 +20%"
    assert describe("fixed_stop_loss", {"pct": 10}) == "止损 -10%"
    assert describe("trailing_stop", {"drawdown_pct": 8}) == "移动止盈 回撤8%"
    assert describe("daily_move", {"pct": 5}) == "日内异动 ±5%"
    assert describe("max_holding", {"days": 90}) == "持有提醒 90天"


def test_unknown_rule_type_raises():
    with pytest.raises(ValueError):
        evaluate("nope", {}, {}, ctx(100.0))
