"""候选池经验校准：把历史回放/校准结论转成「大盘状态 × 买点类型」的权重与剔除规则。

来源：「历史模拟（经验版 v3）」回放结论（2026-10-16，复盘页可查）：
- 突破在即：强市（指数≥20日线）+0.56%/130 笔（正期望）→ 升权；弱市 -1.35~-1.85%（负期望）→ 剔除；
- 回踩支撑：两种环境均轻微负期望（-0.9%~-0.3%）、止损率 42%+ → 降权（小仓试错）；
- 超跌企稳：未单独回测 → 中性。

大盘状态未知（数据不可用）时不做任何调整——不静默改变行为。
后续新的回放/校准结论成熟后，在 SETUP_WEIGHTS / NOTES 中更新即可（并同步复盘页留痕）。
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 权重表：{买点标签: {"strong": 强市权重, "weak": 弱市权重}}；0 表示剔除
SETUP_WEIGHTS: dict[str, dict[str, float]] = {
    "突破在即": {"strong": 1.2, "weak": 0.0},
    "回踩支撑": {"strong": 0.8, "weak": 0.6},
}

_EXCLUDE_NOTE = "经验校准：弱市突破型历史期望为负（历史回放），已剔除"
_WEIGHT_NOTE = "经验校准：{label} 在{regime_label}历史期望偏弱，权重 ×{weight}"


def apply_experience(entries: list[dict], regime: str) -> tuple[list[dict], list[str]]:
    """就地调整候选买点加成与说明；返回（保留的条目, 生效的调整说明列表）。

    regime: "strong" / "weak" / 其他（未知，不调整）。
    """
    if regime not in ("strong", "weak"):
        return entries, ["经验校准：大盘状态未知，未做权重调整"]
    regime_label = "强市" if regime == "strong" else "弱市"
    kept: list[dict] = []
    excluded: list[str] = []
    weighted: list[str] = []
    for entry in entries:
        setup = entry.get("setup") or {}
        label = str(setup.get("label") or "")
        weight = SETUP_WEIGHTS.get(label, {}).get(regime, 1.0)
        if weight <= 0:
            excluded.append(label or entry.get("row", {}).get("code", "?"))
            continue
        if weight != 1.0 and setup:
            setup["bonus"] = round(float(setup.get("bonus", 0.0)) * weight, 2)
            setup["note"] = str(setup.get("note") or "") + f"；{_WEIGHT_NOTE.format(label=label, regime_label=regime_label, weight=weight)}"
            weighted.append(label)
        kept.append(entry)
    notes: list[str] = []
    if excluded:
        counts: dict[str, int] = {}
        for label in excluded:
            counts[label] = counts.get(label, 0) + 1
        summary = "、".join(f"{label}×{n}" for label, n in counts.items())
        notes.append(f"{_EXCLUDE_NOTE}（{summary}）")
    if weighted:
        notes.append(
            f"经验校准：{regime_label}对 " + "、".join(sorted(set(weighted))) + " 已降权（历史回放止损率偏高，宜小仓试错）"
        )
    if not notes:
        notes.append(f"经验校准：{regime_label}无需调整（各形态历史期望正常）")
    for note in notes:
        log.info("候选池经验校准: %s", note)
    return kept, notes
