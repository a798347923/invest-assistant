"""技术指标与统计（纯函数，本地确定性计算，供提示词摘要使用）。

不含任何网络访问；序列不足时返回 None，由调用方自行跳过。
"""

from __future__ import annotations

import datetime as dt


def sma(values: list[float], n: int) -> float | None:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def _ema_series(values: list[float], n: int) -> list[float]:
    if not values or n <= 0:
        return []
    alpha = 2 / (n + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD（DIF/DEA/柱 + 多空状态）。"""
    if len(closes) < slow + signal:
        return None
    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)
    dif = [f - s for f, s in zip(fast_ema, slow_ema, strict=True)]
    dea = _ema_series(dif, signal)
    return {
        "dif": round(dif[-1], 3),
        "dea": round(dea[-1], 3),
        "hist": round(dif[-1] - dea[-1], 3),
        "trend": "金叉/多头" if dif[-1] > dea[-1] else "死叉/空头",
    }


def rsi(closes: list[float], n: int = 14) -> float | None:
    """RSI（Wilder 平滑）；完全横盘返回 50。"""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(change, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-change, 0.0)) / n
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def boll(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    """布林带（下/中/上轨 + 现价在带内位置 %）。"""
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    std = (sum((v - mid) ** 2 for v in window) / n) ** 0.5
    up, low = mid + k * std, mid - k * std
    price = closes[-1]
    pos = (price - low) / (up - low) * 100 if up > low else 50.0
    return {"mid": round(mid, 3), "up": round(up, 3), "low": round(low, 3), "pos_pct": round(pos, 0)}


def atr(bars: list, n: int = 14) -> float | None:
    """平均真实波幅（元）；bars 需含 high/low/close。"""
    if len(bars) < n + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        trs.append(
            max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - prev_close),
                abs(bars[i].low - prev_close),
            )
        )
    if not trs:
        return None
    value = sum(trs[:n]) / min(n, len(trs))
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def percentile_rank(values: list[float], value: float) -> float | None:
    """value 在 values 中的分位（≤ 占比，%）。"""
    if not values:
        return None
    return round(sum(1 for v in values if v <= value) / len(values) * 100, 0)


def weekly_series(closes: list[float], dates: list[dt.date]) -> list[float]:
    """按 ISO 周聚合日线收盘（每周取最后一个交易日）。"""
    weeks: dict[tuple[int, int], float] = {}
    for day, close in zip(dates, closes, strict=True):
        iso = day.isocalendar()
        weeks[(iso[0], iso[1])] = close
    return [weeks[key] for key in sorted(weeks)]


def weekly_trend(bars: list) -> dict | None:
    """周线级别趋势：20 周线位置 + 近 5 周涨跌。"""
    if len(bars) < 30:
        return None
    weeks = weekly_series([b.close for b in bars], [b.date for b in bars])
    if len(weeks) < 21:
        return None
    ma20w = sum(weeks[-20:]) / 20
    last = weeks[-1]
    pct5 = (last / weeks[-6] - 1) * 100 if len(weeks) >= 6 and weeks[-6] else None
    return {
        "ma20w": round(ma20w, 3),
        "above": last >= ma20w,
        "rel_pct": round((last / ma20w - 1) * 100, 2),
        "pct5": round(pct5, 2) if pct5 is not None else None,
    }
