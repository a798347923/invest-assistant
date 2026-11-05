"""买点评估：对模板候选补做技术面「近期是否有买点」判断。

标签（优先级从高到低）：
- 回踩支撑：现价贴近 MA20/MA60（-1%~+3% 以内）且中期均线向上（MA20≥MA60）；
- 突破在即：现价距 20 日高点 ≤3%，量能不放空（5/20 量比 ≥1.0）；
- 超跌企稳：20 日跌幅 ≥10%，近 3 日未创新低且日线收平/收涨；
- 其余：无明确买点（setup_required 时会被过滤，不再选入候选池）。

仅为规则化初筛，不构成投资建议；最终买卖判断以分析卡与用户确认为准。
"""

from __future__ import annotations


def evaluate_buy_setup(bars: list) -> dict:
    """返回 {"label", "bonus", "note"}；K 线不足时抛 ValueError 由调用方兜底跳过。"""
    closes = [b.close for b in bars]
    if len(closes) < 20:
        raise ValueError("K线不足 20 根")

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume for b in bars]
    price = closes[-1]
    if not price:
        raise ValueError("最新价缺失")

    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    pct20 = (price / closes[-21] - 1) * 100 if len(closes) >= 21 and closes[-21] else 0.0
    vol_sum20 = sum(vols[-20:])
    vol_ratio = (sum(vols[-5:]) / 5) / (vol_sum20 / 20) if vol_sum20 else 1.0

    dist_ma20 = (price / ma20 - 1) * 100 if ma20 else 999.0
    dist_ma60 = (price / ma60 - 1) * 100 if ma60 else 999.0
    dist_high20 = (high20 / price - 1) * 100 if price else 999.0

    if ma60 is not None and ma20 >= ma60 and -1.0 <= min(dist_ma20, dist_ma60) <= 3.0:
        nearest = min(dist_ma20, dist_ma60)
        return {
            "label": "回踩支撑",
            "bonus": round(30 - abs(nearest), 2),
            "note": f"现价距 MA20 {dist_ma20:+.1f}%、距 MA60 {dist_ma60:+.1f}%，5/20 量比 {vol_ratio:.2f}",
        }

    if dist_high20 <= 3.0 and vol_ratio >= 1.0:
        return {
            "label": "突破在即",
            "bonus": 25.0,
            "note": f"距 20 日高点 {dist_high20:.1f}%，5/20 量比 {vol_ratio:.2f}",
        }

    if pct20 <= -10 and min(lows[-3:]) > low20 and closes[-1] >= closes[-2]:
        return {
            "label": "超跌企稳",
            "bonus": 22.0,
            "note": f"近 20 日 {pct20:+.1f}%，近 3 日未创新低（20 日低点 {low20:.2f}）",
        }

    return {
        "label": "无明确买点",
        "bonus": 0.0,
        "note": f"距 MA20 {dist_ma20:+.1f}%、距 20 日高点 {dist_high20:.1f}%",
    }
