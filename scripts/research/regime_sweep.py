"""强弱市判定：参数扫描（不带回放，直接测"分类的预测力"）。

已测过的（AGENTS 记录，均否决）：单/双/三/四指数、任一/多数/全部、无滞回、快释放、放宽、
加宽度、MA60 上方、MA20 斜率。**没测过的是判定本身的三个参数**：

- 滞回带 band（0.5% / 1.0% / 1.5% / 2.0%）
- 判定均线 window（MA10 / MA20 / MA30 / MA60）
- 转弱确认天数 confirm（1 / 2 / 3 天）

指标（不看策略，只看分类有没有信息）：
- 弱市日占比、切换次数（换手成本代理）
- 弱市日之后 20 个交易日的指数收益 vs 强市日之后 → 差值越大越有用
- 该差值的 t 值

用法：python scripts/research/regime_sweep.py
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import STRESS, TRAIN, VALID, load_index  # noqa: E402

FORWARD = 20
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def build_map(series: list, band: float, window: int, confirm: int) -> dict:
    """双指数 + 滞回带 + 确认天数。series: [(date, close)]，返回 {date: True=强}。"""
    points: dict = {}
    closes: list[float] = []
    for day, close in series:
        closes.append(close)
        if len(closes) >= window:
            points[day] = (close, sum(closes[-window:]) / window)

    out: dict = {}
    state: bool | None = None
    pending = 0
    for day in sorted(points):
        close, ma = points[day]
        rel = (close / ma - 1) * 100
        if state is None:
            state = rel >= 0
            out[day] = state
            continue
        if state:  # 强市：跌破 -band 触发转弱（需连续 confirm 天）
            if rel < -band:
                pending += 1
                if pending >= confirm:
                    state, pending = False, 0
            else:
                pending = 0
        else:      # 弱市：站上 +band 才转强（单日即可，快速释放已被否决，这里保持 confirm）
            if rel > band:
                pending += 1
                if pending >= confirm:
                    state, pending = True, 0
            else:
                pending = 0
        out[day] = state
    return out


def evaluate(state: dict, series: list, window) -> dict:
    start, end = window
    closes = {d: c for d, c in series}
    days = sorted(d for d in state if start <= d <= end)
    fwd_weak: list[float] = []
    fwd_strong: list[float] = []
    switches = 0
    prev = None
    for i, day in enumerate(days):
        if prev is not None and state[day] != state[prev]:
            switches += 1
        prev = day
        if i + FORWARD >= len(days):
            continue
        f0 = closes.get(day)
        f1 = closes.get(days[i + FORWARD])
        if not f0 or not f1:
            continue
        ret = (f1 / f0 - 1) * 100
        (fwd_strong if state[day] else fwd_weak).append(ret)

    weak_days = sum(1 for d in days if not state[d])
    frac = weak_days / len(days) * 100 if days else 0
    if len(fwd_weak) < 10 or len(fwd_strong) < 10:
        return {"weak_frac": frac, "switches": switches, "diff": 0.0, "t": 0.0,
                "weak_ret": 0.0, "strong_ret": 0.0}
    mw, ms = statistics.fmean(fwd_weak), statistics.fmean(fwd_strong)
    vw = statistics.pvariance(fwd_weak) / len(fwd_weak)
    vs = statistics.pvariance(fwd_strong) / len(fwd_strong)
    se = (vw + vs) ** 0.5
    t = (ms - mw) / se if se > 0 else 0.0
    return {"weak_frac": frac, "switches": switches, "diff": ms - mw, "t": t,
            "weak_ret": mw, "strong_ret": ms}


def main() -> None:
    sh = load_index("sh000001")
    full = (dt.date(2015, 4, 1), dt.date(2026, 10, 31))
    rows = []
    for window in (10, 20, 30, 60):
        for band in (0.5, 1.0, 1.5, 2.0):
            for confirm in (1, 2, 3):
                state = build_map(sh, band, window, confirm)
                res = evaluate(state, sh, full)
                rows.append((window, band, confirm, res))

    rows.sort(key=lambda r: -r[3]["t"])
    print("全区间 2015-04 ~ 2026-10，按 t 值排序（前 15）")
    print(f"{'MA':>4s}{'band':>6s}{'confirm':>8s}{'弱市%':>7s}{'切换':>6s}"
          f"{'弱市后20日':>10s}{'强市后20日':>10s}{'差':>7s}{'t':>6s}")
    for window, band, confirm, r in rows[:15]:
        print(f"{window:>4d}{band:>6.1f}{confirm:>8d}{r['weak_frac']:>7.0f}"
              f"{r['switches']:>6d}{r['weak_ret']:>+10.2f}{r['strong_ret']:>+10.2f}"
              f"{r['diff']:>+7.2f}{r['t']:>+6.2f}")

    # 现行基线
    base = build_map(sh, 1.0, 20, 1)
    b = evaluate(base, sh, full)
    print(f"\n现行（MA20 / band 1.0 / confirm 1）：弱市 {b['weak_frac']:.0f}%，"
          f"切换 {b['switches']} 次，弱市后20日 {b['weak_ret']:+.2f}% vs 强市 {b['strong_ret']:+.2f}%，"
          f"差 {b['diff']:+.2f}%，t={b['t']:+.2f}")

    print("\n分窗口看最好的 3 组：")
    for window, band, confirm, _ in rows[:3]:
        state = build_map(sh, band, window, confirm)
        parts = []
        for name, win in WINDOWS.items():
            r = evaluate(state, sh, win)
            parts.append(f"{name} 弱市{r['weak_frac']:.0f}% 差{r['diff']:+.2f}(t={r['t']:+.2f})")
        print(f"  MA{window}/band{band}/confirm{confirm}: " + " | ".join(parts))


if __name__ == "__main__":
    main()
