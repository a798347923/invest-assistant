"""弱市 × 市值交互：弱市时只允许买大市值（其余仍禁）。

机制假设：大市值在熊市抗跌（压力窗口 +78.4% vs 中小盘 −14.7%/−25.6%），
所以弱市不该"一刀切全禁"，而是"只做大盘"。

实现要点：
- 市值分档用**窗口开始日**的市值（不含窗口内未来信息）；
- 弱市放开通过 `rules=base_rules(weak_block="none")` 实现（只给大盘股这类 code 用），
  中小盘仍用 `weak_block="all"`。
- 三窗口对比；若原池通过，再在陌生池上复验。

用法：python scripts/research/weak_largecap.py [--holdout]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from dd_control import simulate  # noqa: E402
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


def load_mv() -> dict:
    """{code: [(date, total_mv)]}，升序。"""
    out: dict = {}
    if not os.path.isdir(FUND):
        return out
    for name in sorted(os.listdir(FUND)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        rows = []
        with open(os.path.join(FUND, name), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    d = dt.date.fromisoformat(row["date"])
                    mv = float(row["total_mv"]) if row["total_mv"] else None
                except (ValueError, TypeError, KeyError):
                    continue
                if mv:
                    rows.append((d, mv))
        rows.sort()
        if rows:
            out[code] = rows
    return out


def mv_at(rows: list, day: dt.date) -> float | None:
    best = None
    for d, mv in rows:
        if d <= day:
            best = mv
        else:
            break
    return best


def terciles(bars_map: dict, mv: dict, day: dt.date) -> tuple[set[str], set[str]]:
    """返回（大市值 1/3, 中市值 1/3）。"""
    pairs = []
    for code in bars_map:
        rows = mv.get(code)
        if not rows:
            continue
        v = mv_at(rows, day)
        if v:
            pairs.append((v, code))
    pairs.sort()
    n = len(pairs)
    mid = {c for _, c in pairs[n // 3: 2 * n // 3]}
    big = {c for _, c in pairs[2 * n // 3:]}
    return big, mid


def replay_both(bars_map, regime, cost, sh) -> tuple[dict, dict]:
    """每个 code 只重放两次：弱市拦截版 / 弱市放开版。"""
    blocked: dict = {}
    free: dict = {}
    opts = base_opts(max_hold=30)
    for i, (code, bars) in enumerate(bars_map.items(), 1):
        sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
        if len(sliced) < 120:
            continue
        for target, rules in ((blocked, base_rules()), (free, base_rules(weak_block="none"))):
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            target[code] = [t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31"]
        if i % 120 == 0:
            print(f"  重放 {i}/{len(bars_map)}", flush=True)
    return blocked, free


def compose(blocked: dict, free: dict, allow: set[str]) -> list:
    """弱市里 allow 集合用"放开版"的交易，其余用"拦截版"。"""
    out = []
    for code, ts in blocked.items():
        out.extend(free.get(code, []) if code in allow else ts)
    return out

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()

    main_bars = load_dir(MAIN)
    hold_bars = load_dir(HOLDOUT)
    if args.holdout:
        bars_map = hold_bars
        print(f"【陌生池复验】{len(bars_map)} 只")
    else:
        bars_map = dict(main_bars)
        bars_map.update({k: v for k, v in hold_bars.items() if k not in bars_map})
        print(f"【合并池】{len(bars_map)} 只")

    mv = load_mv()
    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)

    print("重放（拦截版 / 放开版各一次）…")
    blocked, free = replay_both(bars_map, regime, cost, sh)
    print(f"  完成：{len(blocked)} 只")

    for wname, (start, end) in WINDOWS.items():
        big, mid = terciles(bars_map, mv, start)
        variants = {
            "A 现行（弱市全禁）": set(),
            "B 弱市只放大盘": big,
            "C 弱市放大盘+中盘": big | mid,
            "D 弱市全放开（对照）": set(bars_map),
        }
        print(f"\n===== {wname} {start}..{end} · 大市值 {len(big)} 只 =====")
        for label, allow in variants.items():
            trades = compose(blocked, free, allow)
            res = simulate(trades, (start, end))
            if not res:
                print(f"  {label:20s} （无成交）")
                continue
            print(f"  {label:20s} 交易 {len(trades):6d}  {res['total']:>+7.1f}% / {res['dd']:>+7.1f}%")


if __name__ == "__main__":
    main()
