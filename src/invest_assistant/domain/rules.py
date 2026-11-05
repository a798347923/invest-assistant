"""规则引擎：实盘与回测共用（同一条评估代码，语义不分叉）。

约定：
- `evaluate()` 为纯函数，不触库；状态（state_json）与状态位（status）由调用方持久化；
- 触发后进入锁存（fixed_take_profit / fixed_stop_loss / trailing_stop / max_holding），
  daily_move 按交易日自动复位；人工处理后可调用 `reset_state()` 复位；
- 回测语义：止盈用 high、止损用 low 判定；trailing 先以 high 更新峰值再以 low 判定；
  "同日先止损后止盈" 属于组合模拟（B3-04）的约定，不在单条规则内处理。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

LATCHED_TYPES = ("fixed_take_profit", "fixed_stop_loss", "trailing_stop", "max_holding")


@dataclass(frozen=True)
class RuleContext:
    price: float
    cost: float
    now: dt.datetime
    opened_at: dt.datetime
    prev_close: float | None = None
    high: float | None = None  # 回测/收盘场景可提供
    low: float | None = None


@dataclass(frozen=True)
class Trigger:
    rule_type: str
    trigger_price: float
    reason: str


def _ref_high(ctx: RuleContext) -> float:
    return ctx.high if (ctx.high is not None and ctx.high > 0) else ctx.price


def _ref_low(ctx: RuleContext) -> float:
    return ctx.low if (ctx.low is not None and ctx.low > 0) else ctx.price


def _stamp(ctx: RuleContext) -> str:
    return ctx.now.isoformat(timespec="seconds")


def evaluate(
    rule_type: str, params: dict, state: dict | None, ctx: RuleContext
) -> tuple[dict, Trigger | None]:
    """评估单条规则。返回 (新状态, 触发或 None)。"""
    state = dict(state or {})

    if rule_type == "fixed_take_profit":
        if state.get("triggered"):
            return state, None
        pct = float(params["pct"])
        target = ctx.cost * (1 + pct / 100)
        ref = _ref_high(ctx)
        if ref >= target:
            state.update({"triggered": True, "last_triggered_at": _stamp(ctx)})
            return state, Trigger(rule_type, round(ref, 4), f"达到止盈 +{pct:g}%（目标 {target:.4f}）")
        return state, None

    if rule_type == "fixed_stop_loss":
        if state.get("triggered"):
            return state, None
        pct = float(params["pct"])
        target = ctx.cost * (1 - pct / 100)
        ref = _ref_low(ctx)
        if ref <= target:
            state.update({"triggered": True, "last_triggered_at": _stamp(ctx)})
            return state, Trigger(rule_type, round(ref, 4), f"触发止损 -{pct:g}%（防线 {target:.4f}）")
        return state, None

    if rule_type == "trailing_stop":
        if state.get("triggered"):
            return state, None
        drawdown = float(params["drawdown_pct"])
        peak = float(state.get("peak") or ctx.cost)
        ref_high = _ref_high(ctx)
        if ref_high > peak:
            peak = ref_high
        state["peak"] = round(peak, 4)
        line = peak * (1 - drawdown / 100)
        ref_low = _ref_low(ctx)
        if ref_low <= line:
            state.update({"triggered": True, "last_triggered_at": _stamp(ctx)})
            return state, Trigger(
                rule_type, round(ref_low, 4),
                f"自峰值 {peak:.4f} 回撤 {drawdown:g}%（止盈线 {line:.4f}）",
            )
        return state, None

    if rule_type == "daily_move":
        if not ctx.prev_close:
            return state, None
        today = ctx.now.date().isoformat()
        if state.get("fired_date") == today:
            return state, None
        pct = float(params["pct"])
        change = (ctx.price / ctx.prev_close - 1) * 100
        if abs(change) >= pct:
            state.update({"fired_date": today, "last_triggered_at": _stamp(ctx)})
            direction = "上涨" if change > 0 else "下跌"
            return state, Trigger(
                rule_type, round(ctx.price, 4), f"日内{direction} {change:+.2f}%（阈值 ±{pct:g}%）"
            )
        return state, None

    if rule_type == "max_holding":
        if state.get("triggered"):
            return state, None
        days = int(params["days"])
        held = (ctx.now - ctx.opened_at).days
        if held >= days:
            state.update({"triggered": True, "last_triggered_at": _stamp(ctx)})
            return state, Trigger(rule_type, round(ctx.price, 4), f"持有 {held} 天 ≥ {days} 天，请复核决策")
        return state, None

    raise ValueError(f"未知规则类型: {rule_type}")


def reset_state(rule_type: str, state: dict | None, ctx: RuleContext) -> dict:
    """人工处理后复位：trailing 峰值重置为当前价，其余清空。"""
    if rule_type == "trailing_stop":
        return {"peak": round(ctx.price, 4)}
    return {}


def describe(rule_type: str, params: dict) -> str:
    if rule_type == "fixed_take_profit":
        return f"止盈 +{float(params['pct']):g}%"
    if rule_type == "fixed_stop_loss":
        return f"止损 -{float(params['pct']):g}%"
    if rule_type == "trailing_stop":
        return f"移动止盈 回撤{float(params['drawdown_pct']):g}%"
    if rule_type == "daily_move":
        return f"日内异动 ±{float(params['pct']):g}%"
    if rule_type == "max_holding":
        return f"持有提醒 {int(params['days'])}天"
    return rule_type
