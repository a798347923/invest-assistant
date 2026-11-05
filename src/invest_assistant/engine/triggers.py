"""自选 / 模拟盘的**纯判定函数**：无 IO、无数据库、可独立单测。

从 `engine/poller.py` 抽出。这些函数决定「什么时候提醒用户」，是全系统最需要边界测试的逻辑，
但此前只能通过「假数据源 + 建库 + 完整一轮轮询」间接覆盖，边界值（等于止损价、恰好等于区间上沿）
很难单独表达。

抽出后 `poller.py` 只保留编排与 IO，判定逻辑集中在此。
"""

from __future__ import annotations

import datetime as dt

from ..domain.conditions import confirm_labels, infer_confirmations


def trading_days_held(opened_at: dt.datetime, now: dt.datetime) -> int:
    """建仓至今经过了几个交易日（周一~周五近似，不含法定节假日）。"""
    days = 0
    day = opened_at.date()
    while day < now.date():
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            days += 1
    return days


def plan_triggers(plan: dict, price: float) -> list[tuple[str, str]]:
    """AI 买入计划的到价条件。

    优先级：失效价 > 突破价 > 买入区间，只返回**第一个命中**的条件
    （失效与买入同时满足时，失效优先——避免"计划已作废却提示买入"）。
    """
    stop = plan.get("stop")
    if stop and price <= stop:
        return [
            (
                "plan_stop",
                f"跌破 AI 买入计划失效价 {stop:g}（现价 {price:.4f}），该计划已作废，请重新分析后再操作",
            )
        ]

    confirm = [str(x) for x in (plan.get("confirm") or [])] or infer_confirmations(plan)
    suffix = f"（自动买入需确认：{confirm_labels(confirm)}）" if confirm else ""

    breakout = plan.get("breakout")
    if breakout and price >= breakout:
        return [
            (
                "plan_buy",
                f"站上 AI 计划突破确认价 {breakout:g}（现价 {price:.4f}），请结合量能确认{suffix}",
            )
        ]

    low, high = plan.get("zone_low"), plan.get("zone_high")
    if high is not None and price <= high:
        if low is not None:
            zone = f"{low:g}~{high:g}"
            if price >= low:
                return [
                    ("plan_buy", f"进入 AI 建议买入区间 {zone}（现价 {price:.4f}）{suffix}")
                ]
            return [
                (
                    "plan_buy",
                    f"已低于 AI 建议买入区间 {zone} 下沿（现价 {price:.4f}），"
                    f"注意是否仍满足确认条件{suffix}",
                )
            ]
        return [("plan_buy", f"进入 AI 建议买入区间 {high:g}（现价 {price:.4f}）{suffix}")]
    return []


def pullback_enhanced_problem(bars: list, price: float, today: dt.date) -> str:
    """回踩增强过滤：返回未通过原因（空串 = 通过）。只用今日之前的 K 线 + 实时价，避免未来函数。"""
    prior = [b for b in bars if b.date < today]
    if not prior:
        return ""
    if not price > prior[-1].high:
        return "未收复前一日高点"
    closes = [b.close for b in prior]
    if len(closes) >= 25:
        ma20 = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-25:-5]) / 20
        if ma20 < ma20_prev:
            return "MA20 未上行"
    if len(closes) >= 60:
        ma60 = sum(closes[-60:]) / 60
        if ma60 and price < ma60 * 0.995:
            return "未站稳 MA60"
    return ""


def watch_triggers(
    item, default_pct: float, price: float, pct: float | None
) -> list[tuple[str, str]]:
    """自选到价 / 异动触发（可同时命中多条：异动 + 上方价 + 下方价）。"""
    triggers: list[tuple[str, str]] = []
    alert_pct = item.alert_pct if item.alert_pct is not None else default_pct
    if alert_pct and pct is not None and abs(pct) >= alert_pct:
        direction = "上涨" if pct > 0 else "下跌"
        triggers.append(("pct", f"自选异动：日内{direction} {pct:+.2f}%（阈值 ±{alert_pct:g}%）"))
    if item.alert_price_above and price >= item.alert_price_above:
        triggers.append(("above", f"到达上方提醒价 {item.alert_price_above:g}（现价 {price:.4f}）"))
    if item.alert_price_below and price <= item.alert_price_below:
        triggers.append(("below", f"跌破下方提醒价 {item.alert_price_below:g}（现价 {price:.4f}）"))
    return triggers
