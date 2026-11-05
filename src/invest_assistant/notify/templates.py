"""消息模板（纯文本）。"""

from __future__ import annotations

from .base import Message


def trigger_message(
    context: dict,
    catchup: bool = False,
    expired: bool = False,
    interpretation: dict | None = None,
) -> Message:
    code = str(context.get("code", ""))
    name = context.get("name") or code
    reason = str(context.get("reason", ""))
    price = context.get("price")
    cost = context.get("cost")
    qty = context.get("qty")

    prefix = ""
    if expired:
        prefix = "【已过期，请复核】"
    elif catchup:
        prefix = "【补报】"

    lines = [f"标的：{name}（{code}）", f"规则触发：{reason}"]
    if price is not None:
        lines.append(f"当前价：{float(price):.4f}")
    if cost:
        lines.append(f"成本价：{float(cost):.4f}")
    if price is not None and cost and qty:
        pnl = (float(price) - float(cost)) * float(qty)
        pct = (float(price) / float(cost) - 1) * 100
        lines.append(f"浮动盈亏：{pnl:+.2f}（{pct:+.2f}%）")
    if qty:
        lines.append(f"持仓数量：{float(qty):g}")
    lines.append("")
    lines.append("建议动作：请依纪律决策——卖出 / 继续持有观察 / 复核退出规则。")

    if interpretation:
        lines.append("")
        lines.append("【AI 解读】")
        if interpretation.get("what_happened"):
            lines.append(f"发生了什么：{interpretation['what_happened']}")
        if interpretation.get("position_status"):
            lines.append(f"持仓状态：{interpretation['position_status']}")
        if interpretation.get("suggestion"):
            lines.append(f"建议：{interpretation['suggestion']}")
        options = interpretation.get("options") or []
        if options:
            lines.append("备选动作：" + "；".join(str(o) for o in options))
        confidence = interpretation.get("confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"置信度：{confidence:.2f}（仅供参考）")

    return Message(title=f"{prefix}【规则触发】{name} {code}", body="\n".join(lines))


def system_message(warning: str) -> Message:
    return Message(
        title="【系统异常】投资助手数据源连续失败",
        body=f"{warning}\n\n处理建议：检查网络与数据源状态；服务恢复后会自动补报错过的触发。",
    )


_ALERT_TITLES = {
    "plan_buy": "【AI 买入机会】",
    "plan_stop": "【AI 计划失效】",
    "paper_buy": "【模拟盘·自动买入】",
    "paper_exec": "【模拟盘·执行建议】",
    "paper_stop": "【模拟盘·止损清仓】",
}


def watch_message(context: dict, expired: bool = False) -> Message:
    code = str(context.get("code", ""))
    name = context.get("name") or code
    reason = str(context.get("reason", ""))
    price = context.get("price")
    pct = context.get("pct")
    alert_type = str(context.get("alert_type") or "")
    plan = context.get("plan") or {}

    prefix = "【已过期，请复核】" if expired else ""
    label = _ALERT_TITLES.get(alert_type, "【自选提醒】")
    lines = [f"标的：{name}（{code}）", f"提醒原因：{reason}"]
    if price is not None:
        lines.append(f"现价：{float(price):.4f}")
    if isinstance(pct, (int, float)):
        lines.append(f"日内涨跌：{pct:+.2f}%")

    if plan:
        lines.append("")
        lines.append("【AI 买入计划】")
        if plan.get("zone_text"):
            lines.append(f"建议买入区间：{plan['zone_text']}")
        if plan.get("breakout"):
            lines.append(f"备选突破确认价：{float(plan['breakout']):g}")
        if plan.get("stop_text"):
            lines.append(f"失效价：{plan['stop_text']}")
        if plan.get("triggers_text"):
            lines.append(f"确认条件：{plan['triggers_text']}")
        if plan.get("position_hint"):
            lines.append(f"仓位建议：{plan['position_hint']}")
        lines.append("提示：到价 ≠ 立即买入，请按确认条件（量价）复核后再操作。")

    lines.append("")
    lines.append(
        "说明：这是自选池监控提醒（非持仓规则触发），是否需要操作请自行决策；"
        "需要 AI 辅助判断时，可在电脑面板「提醒」页点击该记录的「AI 判断」按钮。"
    )
    return Message(title=f"{prefix}{label}{name} {code}", body="\n".join(lines))


def paper_message(content: dict, stats: dict | None = None) -> Message:
    """模拟盘日报消息（AI 建议正文由调用方保证已归一化）。"""
    stats = stats or {}
    lines: list[str] = []
    summary = str(content.get("summary") or "")
    if summary:
        lines.append(summary)
    positions = content.get("positions") or []
    if positions:
        lines.append("")
        for item in positions:
            action = str(item.get("action") or "持有")
            qty_pct = item.get("qty_pct") or 0
            hint = item.get("price_hint") or ""
            qty_text = f" {qty_pct:g}%" if action in ("加仓", "减仓") and qty_pct else ""
            hint_text = f"（{hint}）" if hint else ""
            lines.append(
                f"· {item.get('name') or item.get('code')} {item.get('code')}："
                f"建议{action}{qty_text}{hint_text}——{item.get('reason') or ''}"
            )
    risks = content.get("risk_notes") or []
    if risks:
        lines.append("")
        lines.append("风险提示：" + "；".join(str(r) for r in risks))
    lines.append("")
    lines.append("建议将于下一交易日开盘自动执行（模拟盘）。仅供参考，不构成投资建议。")

    pct = stats.get("total_pnl_pct")
    day = stats.get("day_pnl")
    if isinstance(pct, (int, float)):
        title = f"【模拟盘日报】累计 {pct:+.2f}%"
        if isinstance(day, (int, float)):
            title += f"（今日 {day:+.0f} 元）"
    else:
        title = "【模拟盘日报】"
    return Message(title=title, body="\n".join(lines))
