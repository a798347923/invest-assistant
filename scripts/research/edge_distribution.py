"""策略的真实 edge 分布：在随机股票子池上跑基准，看 +20% 是常态还是好运。

做法（不需要新数据）：
- 现有 480 只（原池 240 + 陌生池 240，去重后约 470 只），先整体重放一次（hold 30）；
- 从这 480 只里随机抽 120 只组成子池，重复 12 次；
- 每个子池在三个窗口上做组合模拟，得到收益/回撤的分布；
- 对照：原池（seed=42）、陌生池（seed=7）作为两个"真实抽到的池子"。

这样回答两个问题：
1. 基准策略的收益分布有多宽？（如果 p10~p90 跨几十个点，说明单池结果没有意义）
2. 原池的 +74% 在分布里处于什么位置？

用法：python scripts/research/edge_distribution.py [--pools 12] [--size 120]
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

from dd_control import simulate  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource import Bar  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
MAIN = os.path.join(RESEARCH, "bars")
HOLDOUT = os.path.join(RESEARCH, "bars_holdout")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def load_dir(folder: str) -> dict:
    out = {}
    if not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        bars = []
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    bars.append(Bar(code=code, date=dt.date.fromisoformat(row["date"]),
                                    open=float(row["open"]), high=float(row["high"]),
                                    low=float(row["low"]), close=float(row["close"]),
                                    volume=float(row["volume"] or 0)))
                except (TypeError, ValueError):
                    continue
        bars.sort(key=lambda b: b.date)
        if len(bars) >= 120:
            out[code] = bars
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", type=int, default=12)
    ap.add_argument("--size", type=int, default=120)
    ap.add_argument("--seed", type=int, default=2026)
    args = ap.parse_args()

    main_bars = load_dir(MAIN)
    hold_bars = load_dir(HOLDOUT)
    merged = dict(main_bars)
    merged.update({k: v for k, v in hold_bars.items() if k not in merged})
    print(f"主池 {len(main_bars)} / 陌生池 {len(hold_bars)} / 合并 {len(merged)} 只")

    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    opts = base_opts(max_hold=30)

    start_all = dt.date(2015, 1, 1)
    end_all = dt.date(2026, 10, 31)
    print("整体重放（一次，约 3 分钟）…")
    trades_by_code: dict = {}
    for i, (code, bars) in enumerate(merged.items(), 1):
        sliced = [b for b in bars if start_all <= b.date <= end_all]
        if len(sliced) < 120:
            continue
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades_by_code[code] = [t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31"]
        if i % 120 == 0:
            print(f"  {i}/{len(merged)}", flush=True)

    def run_pool(codes: set[str], label: str) -> dict:
        trades = [t for c in codes for t in trades_by_code.get(c, [])]
        out = {}
        for wname, win in WINDOWS.items():
            out[wname] = simulate(trades, win)
        return out

    rng = random.Random(args.seed)
    codes_all = sorted(trades_by_code)
    results = []
    for k in range(args.pools):
        pick = set(rng.sample(codes_all, min(args.size, len(codes_all))))
        res = run_pool(pick, f"pool{k}")
        results.append(res)
        print(f"  子池 {k + 1}/{args.pools}: "
              + " | ".join(f"{w} {res[w]['total']:+.1f}%/{res[w]['dd']:+.1f}%"
                           for w in WINDOWS), flush=True)

    print("\n===== 随机子池分布 =====")
    print(f"{'窗口':8s}{'均值':>9s}{'中位':>9s}{'p10':>8s}{'p90':>8s}{'最差':>8s}{'最好':>8s}")
    for w in WINDOWS:
        vals = sorted(r[w]["total"] for r in results)
        print(f"{w:8s}{statistics.fmean(vals):>+9.1f}{statistics.median(vals):>+9.1f}"
              f"{vals[int(0.1 * len(vals))]:>+8.1f}{vals[int(0.9 * len(vals))]:>+8.1f}"
              f"{vals[0]:>+8.1f}{vals[-1]:>+8.1f}")

    print("\n===== 两个真实池的位置 =====")
    for label, pool in (("原池 seed=42", set(main_bars)), ("陌生池 seed=7", set(hold_bars))):
        res = run_pool(pool, label)
        line = " | ".join(f"{w} {res[w]['total']:+.1f}%/{res[w]['dd']:+.1f}%" for w in WINDOWS)
        pct = []
        for w in WINDOWS:
            vals = sorted(r[w]["total"] for r in results)
            p = sum(1 for v in vals if v < res[w]["total"]) / len(vals) * 100
            pct.append(f"{w} {p:.0f}%分位")
        print(f"  {label}: {line}")
        print("    相对子池分布: " + "，".join(pct))


if __name__ == "__main__":
    main()
