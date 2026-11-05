"""买入确认条件：本地确定性判定（供模拟盘自动买入前逐条校验）。

支持的枚举（分析卡 plan.confirm 输出，或从 triggers 文字兜底解析）：
- volume_shrink  缩量：按时间折算的预估全天量 ≤ 5 日均量 × 0.8
- volume_expand  放量：≥ 5 日均量 × 1.2
- bullish        收阳/收稳：现价 ≥ 今日开盘
- above_ma5 / above_ma10 / above_ma20 / above_ma60：现价站上对应均线（现价作为临时收盘）

设计原则：
- 全部条件必须同时满足才放行；任何一条"无法判定"一律按未满足处理（保守优先）；
- 量能按交易时间折算（240 分钟），开盘前 45 分钟量能噪声过大 → 量能类条件判为未满足；
- 量纲防护：折算后的量比超出 [0.05, 20] 视为数据口径异常（如 K 线源与行情源单位不一致）→ 保守不确认。
"""

from __future__ import annotations

import datetime as dt
import re

CONFIRM_LABELS = {
    "volume_shrink": "缩量",
    "volume_expand": "放量",
    "bullish": "收阳/收稳",
    "above_ma5": "站上MA5",
    "above_ma10": "站上MA10",
    "above_ma20": "站上MA20",
    "above_ma60": "站上MA60",
}

_SESSION_TOTAL_MINUTES = 240
_SHRINK_MAX_RATIO = 0.8
_EXPAND_MIN_RATIO = 1.2
_MIN_ELAPSED_MINUTES = 45


def confirm_labels(codes: list[str]) -> str:
    return "、".join(CONFIRM_LABELS.get(code, str(code)) for code in codes if code)


def session_elapsed_minutes(now_time: dt.time) -> int:
    """A 股连续竞价已进行的分钟数（上午 9:30-11:30 / 下午 13:00-15:00）。"""
    minutes = now_time.hour * 60 + now_time.minute
    morning_start, morning_end = 9 * 60 + 30, 11 * 60 + 30
    afternoon_start, afternoon_end = 13 * 60, 15 * 60
    if minutes <= morning_start:
        return 0
    if minutes <= morning_end:
        return minutes - morning_start
    if minutes < afternoon_start:
        return 120
    if minutes <= afternoon_end:
        return 120 + (minutes - afternoon_start)
    return _SESSION_TOTAL_MINUTES


def _quote_volume_hands(volume: float, source: str) -> float | None:
    """行情成交量统一折算为「手」；未知来源返回 None。"""
    if not volume or volume <= 0:
        return None
    if source == "em":
        return float(volume)
    if source in ("sina", "tencent"):
        return float(volume) / 100.0  # 两者的 Quote.volume 均按「股」存储
    return None


def infer_confirmations(plan: dict | None) -> list[str]:
    """旧卡兜底：从 triggers/区间文字中解析常见确认条件。"""
    plan = plan or {}
    texts = [str(plan.get("zone_text") or "")]
    texts.extend(str(t) for t in (plan.get("triggers") or []))
    text = "；".join(texts)
    out: list[str] = []
    if "缩量" in text:
        out.append("volume_shrink")
    if "放量" in text:
        out.append("volume_expand")
    if any(keyword in text for keyword in ("阳线", "收阳", "收稳", "企稳")):
        out.append("bullish")
    upper = text.upper()
    for window in (5, 10, 20, 60):
        if re.search(rf"MA\s*{window}(?!\d)", upper) or re.search(rf"{window}\s*日线", text):
            out.append(f"above_ma{window}")
    return list(dict.fromkeys(out))


def evaluate_confirmations(
    confirm: list[str],
    *,
    price: float,
    open_price: float,
    volume: float,
    source: str,
    bars: list,
    now: dt.datetime,
) -> tuple[bool, list[str]]:
    """逐条判定确认条件；返回（是否全部满足, 明细）。空条件视为满足。"""
    confirm = [str(code) for code in confirm if code]
    if not confirm:
        return True, []

    today = now.date()
    history = [b for b in bars if b.date < today and getattr(b, "volume", 0)]
    base_window = history[-5:]
    avg5 = (sum(b.volume for b in base_window) / len(base_window)) if base_window else 0.0

    hands = _quote_volume_hands(volume, source)
    elapsed = session_elapsed_minutes(now.time())
    volume_ratio: float | None = None
    volume_unknown = ""
    if hands is None:
        volume_unknown = "成交量数据缺失"
    elif elapsed < _MIN_ELAPSED_MINUTES:
        volume_unknown = f"开盘不足 {_MIN_ELAPSED_MINUTES} 分钟，量能条件暂不判定"
    elif avg5 <= 0:
        volume_unknown = "缺少近 5 日基准成交量"
    else:
        ratio = hands * _SESSION_TOTAL_MINUTES / elapsed / avg5
        if ratio < 0.05 or ratio > 20:
            volume_unknown = f"量能比异常（{ratio:.2f}），数据口径存疑"
        else:
            volume_ratio = ratio

    def _ma(window: int) -> float | None:
        closes = [b.close for b in bars if b.date < today] + [price]
        if len(closes) < window:
            return None
        return sum(closes[-window:]) / window

    ok_all = True
    details: list[str] = []
    for code in confirm:
        if code in ("volume_shrink", "volume_expand"):
            label = "缩量" if code == "volume_shrink" else "放量"
            if volume_ratio is None:
                ok_all = False
                details.append(f"{label}未确认（{volume_unknown}）")
                continue
            good = (
                volume_ratio <= _SHRINK_MAX_RATIO
                if code == "volume_shrink"
                else volume_ratio >= _EXPAND_MIN_RATIO
            )
            ok_all &= good
            details.append(f"{label}{'✓' if good else '✗'}（预估量比 {volume_ratio:.2f}）")
        elif code == "bullish":
            if not open_price or open_price <= 0:
                ok_all = False
                details.append("收阳/收稳未确认（无今开数据）")
                continue
            good = price >= open_price
            ok_all &= good
            details.append(
                f"收阳/收稳{'✓' if good else '✗'}（现价 {price:.4g} vs 今开 {open_price:.4g}）"
            )
        elif code.startswith("above_ma"):
            try:
                window = int(code.replace("above_ma", ""))
            except ValueError:
                window = 0
            ma = _ma(window) if window else None
            if ma is None:
                ok_all = False
                details.append(f"站上MA{window}未确认（K线不足）")
                continue
            good = price >= ma
            ok_all &= good
            details.append(
                f"站上MA{window}{'✓' if good else '✗'}（现价 {price:.4g} vs MA{window} {ma:.4g}）"
            )
        else:
            ok_all = False
            details.append(f"未知条件「{code}」按未满足处理")
    return bool(ok_all), details
