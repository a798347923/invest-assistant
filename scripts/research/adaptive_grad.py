"""参数梯度微调（每年一步，±5 天）：像训练一样顺着梯度慢慢挪。

与上一版（adaptive.py 的"每季度挑一套最好配置"）的本质区别：
- 不换配置，只在一个连续参数（max_hold）上**小步移动**；
- 方向来自**局部梯度**：比较当前值左右邻居的近期表现，往好的方向挪一步；
- 频率每年一次，不是每季度。

参数：max_hold ∈ {15,20,25,30,35,40,45}，每步 ±5，范围夹在 [15,45]。

对照组：
- fixed：一直 30 天
- grad：梯度微调（每年一步）
- randomwalk：每年随机 ±5（同样是"慢"，但没有方向信息）
- oracle：每年用**当年**最优参数（事后诸葛，只作上界参考，不可实现）

严格性：每年年初打分只用 exit_date ≤ 年初 的交易（已实现）。

用法：python scripts/research/adaptive_grad.py [--window all] [--step 5]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _simulate_with_marks  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
FULL = (dt.date(2015, 4, 1), dt.date(2026, 10, 31))
HOLDS = [15, 20, 25, 30, 35, 40, 45]
START_HOLD = 30
MIN_SAMPLE = 20


def replay_by_hold(bars_map, sh, cost, band: float) -> dict:
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, band)
    rules = base_rules()
    out = {}
    for hold in HOLDS:
        opts = base_opts(max_hold=hold)
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if FULL[0] - dt.timedelta(days=120) <= b.date <= FULL[1]]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got
                          if FULL[0].isoformat() <= t.entry_date <= FULL[1].isoformat())
        out[hold] = trades
        print(f"  hold={hold:2d}  {len(trades):5d} 笔", flush=True)
    return out


def score(per_hold: dict, hold: int, upto: dt.date, lookback_days: int) -> float | None:
    floor = upto - dt.timedelta(days=lookback_days)
    done = [t for t in per_hold.get(hold, [])
            if t.exit_date <= upto.isoformat() and t.entry_date >= floor.isoformat()]
    if len(done) < MIN_SAMPLE:
        return None
    return statistics.fmean(t.ret_pct for t in done)


def gradient_step(per_hold, hold, upto, lookback, step) -> int:
    """局部梯度：比较 hold-step 与 hold+step 的近期表现，往好的方向挪一步。"""
    lo, hi = hold - step, hold + step
    s_lo = score(per_hold, lo, upto, lookback) if lo >= min(HOLDS) else None
    s_hi = score(per_hold, hi, upto, lookback) if hi <= max(HOLDS) else None
    if s_lo is None or s_hi is None:
        # 邻居样本不足：退化为比较当前与可用的那一侧
        s_cur = score(per_hold, hold, upto, lookback)
        if s_cur is None:
            return hold
        if s_hi is not None and s_hi > s_cur:
            return min(max(HOLDS), hold + step)
        if s_lo is not None and s_lo > s_cur:
            return max(min(HOLDS), hold - step)
        return hold
    if s_hi > s_lo:
        return min(max(HOLDS), hold + step)
    if s_lo > s_hi:
        return max(min(HOLDS), hold - step)
    return hold


def years(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    out = []
    cur = start
    while cur <= end:
        nxt = min(dt.date(cur.year + 1, cur.month, cur.day), end + dt.timedelta(days=1))
        out.append((cur, min(nxt - dt.timedelta(days=1), end)))
        cur = nxt
    return out


def build(per_hold, window, mode: str, lookback: int, step: int, rng=None):
    start, end = window
    hold = START_HOLD
    out, path = [], []
    for y0, y1 in years(start, end):
        if mode == "grad":
            hold = gradient_step(per_hold, hold, y0, lookback, step)
        elif mode == "randomwalk":
            hold = min(max(HOLDS), max(min(HOLDS), hold + (step if (rng or random).random() > 0.5 else -step)))
        elif mode == "oracle":
            best, best_s = START_HOLD, float("-inf")
            for h in HOLDS:
                s = score(per_hold, h, y1, 400)  # 用"这一年"的表现（不可实现，只作上界）
                if s is not None and s > best_s:
                    best, best_s = h, s
            hold = best
        path.append((y0, hold))
        out.extend(t for t in per_hold[hold] if y0.isoformat() <= t.entry_date <= y1.isoformat())
    return out, path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="all")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--lookback", type=int, default=365, help="梯度打分回看天数")
    ap.add_argument("--seeds", type=int, default=10)
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    print(f"重放 {len(HOLDS)} 个持有期（全历史）…")
    per_hold = replay_by_hold(bars_map, sh, cost, 1.0)

    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        print(f"\n===== {name} {window[0]}..{window[1]} · 每年一步 ±{args.step} 天 =====")
        print(f"{'方案':16s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'回撤':>9s}{'随机均值':>10s}{' 路径'}")
        for mode, label in (("fixed", "固定 30 天"), ("grad", "梯度微调"),
                            ("randomwalk", "随机游走"), ("oracle", "事后最优(上界)")):
            trades, path = build(per_hold, window, mode, args.lookback, args.step,
                                 rng=random.Random(2026) if mode == "randomwalk" else None)
            res = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
            if not res.get("trades"):
                print(f"{label:16s}  （无成交）")
                continue
            if mode == "randomwalk":
                rand = statistics.fmean(
                    _simulate_with_marks(
                        build(per_hold, window, "randomwalk", args.lookback, args.step,
                              rng=random.Random(700 + i))[0], {}, "chrono", bars_map, 5, 0.2
                    )["total_ret_pct"]
                    for i in range(args.seeds)
                )
                rand_s = f"{rand:+.1f}"
            else:
                rand_s = "—"
            trail = " → ".join(f"{h}" for _, h in path)
            print(f"{label:16s}{res['trades']:>6d}{res['win_rate']:>7.1f}"
                  f"{res['total_ret_pct']:>+9.1f}{res['max_drawdown_pct']:>+9.1f}"
                  f"{rand_s:>10s}  {trail}")


if __name__ == "__main__":
    main()
