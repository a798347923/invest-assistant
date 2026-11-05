"""AI「买入计划」解析：把分析卡的 plan 转化为可盘中监控的结构化价位。

监控语义（poller）：
- 回踩/现价型：现价 ≤ 区间上沿 → 「进入（或低于）建议买入区间」；
- 突破型：现价 ≥ 突破确认价 → 「站上突破价」；
- 失效价：现价 ≤ stop → 「计划作废」。

优先取卡里的数值字段（entry_low/entry_high/breakout_price/stop_price，
CARD_V3 起模型会直接输出数字）；旧卡或漏填时用正则从文本兜底解析。
"""

from __future__ import annotations

import re

_NUM = re.compile(r"\d+(?:\.\d+)?")
_BREAKOUT_FOLLOW = re.compile(r"(?:站上|突破|升破|越过|上破)[^0-9]{0,6}(\d+(?:\.\d+)?)")
_BREAKOUT_WORDS = ("站上", "突破", "升破", "越过", "上破")


def _numbers(text: str) -> list[float]:
    return [float(m) for m in _NUM.findall(text or "")]


def _as_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def parse_plan_levels(content: dict | None) -> dict | None:
    """解析分析卡内容 → 结构化监控价位；无法定位任何价位时返回 None。"""
    plan = (content or {}).get("plan") or {}
    if not isinstance(plan, dict):
        return None

    zone_text = str(plan.get("entry_zone") or "")
    stop_text = str(plan.get("stop_level") or "")
    triggers = [str(t) for t in (plan.get("triggers") or [])]
    entry_type = str(plan.get("entry_type") or "")

    zone_nums = _numbers(zone_text)
    low = _as_float(plan.get("entry_low"))
    high = _as_float(plan.get("entry_high"))
    breakout = _as_float(plan.get("breakout_price"))
    stop = _as_float(plan.get("stop_price"))

    # 文本兜底
    if low is None and zone_nums:
        low = min(zone_nums)
    if high is None and zone_nums:
        high = max(zone_nums)
    if stop is None:
        stop_nums = _numbers(stop_text)
        if stop_nums:
            stop = stop_nums[0]
    if breakout is None:
        for text in [zone_text, *triggers]:
            match = _BREAKOUT_FOLLOW.search(text)
            if match:
                breakout = float(match.group(1))
                break

    # 突破型计划的「区间」实际是突破确认价
    if entry_type == "突破":
        if breakout is None:
            breakout = low if low is not None else high
        low = high = None

    # 旧卡推断：文本含突破字眼且无区间（只有一个价位）→ 视为突破型
    if not entry_type and low is not None and low == high and any(
        w in zone_text for w in _BREAKOUT_WORDS
    ):
        breakout = breakout if breakout is not None else low
        low = high = None

    if low is not None and high is not None and low > high:
        low, high = high, low

    if low is None and high is None and breakout is None and stop is None:
        return None

    return {
        "zone_low": low,
        "zone_high": high,
        "breakout": breakout,
        "stop": stop,
        "zone_text": zone_text,
        "stop_text": stop_text,
        "triggers": triggers,
        "triggers_text": "；".join(triggers[:3]),
        "position_hint": str(plan.get("position_hint") or ""),
        "confirm": [str(code) for code in (plan.get("confirm") or []) if code],
    }
