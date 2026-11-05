"""选股机制检验：①信号 vs 随便买 ②初筛阈值（市值/流动性）的影响。

问题（用户提出）：随机抽股票池没有体现"我们有一套选股机制"。这个脚本把机制拆成两层分别测：

A. 信号层有没有 edge：在同一池子里，对比
   - `signal`：按规则挑（20 日新高突破 / MA20 回踩确认）
   - `random`：同样日期、从池子里随机挑同样多的票（30 次）
   如果两者接近，说明"选股机制"其实没有选股能力，只是"按时买入"。

B. 初筛层（universe filter）阈值的影响：把 446 只按市值/换手中位数分成三档，
   分别跑信号，看"大盘股 vs 小盘股"哪档更好——这是此前从未测过的维度。

用法：python scripts/research/selection_test.py [--seeds 30]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from edge_distribution import load_dir  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
MAIN = os.path.join(RESEARCH, "bars")
HOLDOUT = os.path.join(RESEARCH, "bars_holdout")
FUND = os.path.join(RESEARCH, "fund")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def load_stats() -> dict:
    """每只股票的市值/换手中位数（用于分档）。"""
    acc: dict = {}
    if not os.path.isdir(FUND):
        return {}
    for name in sorted(os.listdir(FUND)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        mv, turn = [], []
        with open(os.path.join(FUND, name), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    if row["total_mv"]:
                        mv.append(float(row["total_mv"]))
                    if row["turnover_rate"]:
                        turn.append(float(row["turnover_rate"]))
                except (TypeError, ValueError):
                    continue
        if mv:
            acc[code] = {"mv": statistics.median(mv), "turn": statistics.median(turn) if turn else 0.0}
    return acc


def simulate(trades, window, mode="signal", rng=None, slots=5, fraction=0.2, cash0=50000.0):
    start, end = window
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                  | {dt.date.fromisoformat(t.exit_date) for t in trades})
    days = [d for d in days if start <= d <= end]
    cash, pos, curve = cash0, [], []
    for day in days:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still
        cands = list(by_entry.get(day, []))
        if mode == "random" and rng is not None and cands:
            rng.shuffle(cands)
        equity = cash + sum(p["value"] for p in pos)
        for t in cands:
            if len(pos) >= slots:
                break
            size = min(equity * fraction, cash)
            if size < 1000:
                break
            cash -= size
            pos.append({"trade": t, "value": size,
                        "exit_day": dt.date.fromisoformat(t.exit_date)})
        curve.append(cash + sum(p["value"] for p in pos))
    if not curve:
        return {}
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": (curve[-1] / cash0 - 1) * 100, "dd": mdd * 100}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    args = ap.parse_args()

    main_bars = load_dir(MAIN)
    hold_bars = load_dir(HOLDOUT)
    merged = dict(main_bars)
    merged.update({k: v for k, v in hold_bars.items() if k not in merged})
    stats = load_stats()
    print(f"合并池 {len(merged)} 只，有市值数据 {len(stats)} 只")

    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    opts = base_opts(max_hold=30)

    trades = []
    for code, bars in merged.items():
        sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
        if len(sliced) < 120:
            continue
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades.extend(t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31")
    print(f"信号 {len(trades)} 笔")

    print("\n===== A. 信号 vs 随机挑（同一池子、同样日期） =====")
    print(f"{'窗口':8s}{'信号':>16s}{'随机均值':>12s}{'随机 p10':>11s}{'随机 p90':>11s}{'分位':>8s}")
    for wname, win in WINDOWS.items():
        sig = simulate(trades, win)
        rand = sorted(simulate(trades, win, "random", rng=random.Random(1000 + i))["total"]
                      for i in range(args.seeds))
        pct = sum(1 for r in rand if r < sig["total"]) / len(rand) * 100
        print(f"{wname:8s}{sig['total']:>+9.1f}/{sig['dd']:>+6.1f}"
              f"{statistics.fmean(rand):>+12.1f}{rand[int(0.1 * len(rand))]:>+11.1f}"
              f"{rand[int(0.9 * len(rand))]:>+11.1f}{pct:>7.0f}%")

    print("\n===== B. 初筛阈值：按市值分三档 =====")
    codes = [c for c in merged if c in stats]
    codes.sort(key=lambda c: stats[c]["mv"])
    n = len(codes)
    buckets = {
        "小市值 1/3": set(codes[: n // 3]),
        "中市值 1/3": set(codes[n // 3: 2 * n // 3]),
        "大市值 1/3": set(codes[2 * n // 3:]),
    }
    print(f"{'档位':14s}" + "".join(f"{w:>16s}" for w in WINDOWS))
    for label, pool in buckets.items():
        sub = [t for t in trades if t.code in pool]
        cells = []
        for _wname, win in WINDOWS.items():
            r = simulate(sub, win)
            cells.append(f"{r['total']:+.1f}/{r['dd']:+.1f}" if r else "—")
        print(f"{label:14s}" + "".join(f"{c:>16s}" for c in cells))

    print("\n===== C. 初筛阈值：按换手分三档 =====")
    codes.sort(key=lambda c: stats[c]["turn"])
    buckets = {
        "低换手 1/3": set(codes[: n // 3]),
        "中换手 1/3": set(codes[n // 3: 2 * n // 3]),
        "高换手 1/3": set(codes[2 * n // 3:]),
    }
    print(f"{'档位':14s}" + "".join(f"{w:>16s}" for w in WINDOWS))
    for label, pool in buckets.items():
        sub = [t for t in trades if t.code in pool]
        cells = []
        for _wname, win in WINDOWS.items():
            r = simulate(sub, win)
            cells.append(f"{r['total']:+.1f}/{r['dd']:+.1f}" if r else "—")
        print(f"{label:14s}" + "".join(f"{c:>16s}" for c in cells))


if __name__ == "__main__":
    main()
