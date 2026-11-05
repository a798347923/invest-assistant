"""自适应暴露：回撤越深，新开仓的仓位越小。

背景：回撤归因显示"回撤与暴露线性相关"——固定降仓位会把收益一起减半（Calmar 不变）。
自适应暴露想回答：能不能**只在回撤期降暴露**，从而用较小的收益代价换较大的回撤改善。

基线（当前已验证配置）：弱市拦截 + 弱市停泊（债7:金3）+ 5 槽 × 20%。

变体：
- T1：回撤 >8% → 仓位×0.6；>16% → ×0.35；恢复后回满
- T2：回撤 >12% → 仓位×0.5
- T3：恒定半仓（对照，检验"自适应"是否真的优于"一直小"）

用法：python scripts/research/adaptive_exposure.py [--holdout]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from dd_analysis import load_dir  # noqa: E402
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
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def load_etf(code: str) -> dict:
    for name in (f"{code}.csv", f"sh{code}.csv"):
        path = os.path.join(RESEARCH, "etf", name)
        if os.path.exists(path):
            out = {}
            with open(path, encoding="utf-8") as fh:
                next(fh)
                for line in fh:
                    d, c = line.strip().split(",")[:2]
                    out[dt.date.fromisoformat(d)] = float(c)
            return out
    return {}


def simulate(trades, window, regime, bond, gold, tiers=None, flat_mult=1.0,
             slots=5, fraction=0.2, cash0=50000.0) -> dict:
    start, end = window
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                  | {dt.date.fromisoformat(t.exit_date) for t in trades})
    days = [d for d in days if start <= d <= end]

    cash, pos, curve, peak = cash0, [], [], cash0
    for i, day in enumerate(days):
        prev = days[i - 1] if i > 0 else day
        if regime.get(prev) is False:
            b, g = bond.get(day), gold.get(day)
            pb, pg = bond.get(prev), gold.get(prev)
            if b and g and pb and pg:
                cash *= 1 + 0.7 * (b / pb - 1) + 0.3 * (g / pg - 1)

        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still

        equity = cash + sum(p["value"] for p in pos)
        peak = max(peak, equity)
        dd = equity / peak - 1 if peak > 0 else 0.0
        mult = flat_mult
        if tiers:
            for floor, m in tiers:
                if dd <= floor:
                    mult = m
                    break

        for t in by_entry.get(day, []):
            if len(pos) >= slots:
                break
            size = min(equity * fraction * mult, cash)
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
    total = (curve[-1] / cash0 - 1) * 100
    years = max(1, (days[-1] - days[0]).days / 365)
    annual = ((1 + total / 100) ** (1 / years) - 1) * 100
    return {"total": total, "dd": mdd * 100, "annual": annual,
            "calmar": annual / abs(mdd * 100) if mdd < 0 else 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    args = ap.parse_args()

    main_bars = load_dir(MAIN)
    hold_bars = load_dir(HOLDOUT)
    bars_map = hold_bars if args.holdout else {**main_bars, **hold_bars}
    print(f"{'陌生池' if args.holdout else '合并池'}：{len(bars_map)} 只")

    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    opts = base_opts(max_hold=30)
    bond, gold = load_etf("511260"), load_etf("518880")

    trades = []
    for code, bars in bars_map.items():
        sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
        if len(sliced) < 120:
            continue
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades.extend(t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31")
    print(f"信号 {len(trades)} 笔")

    variants = {
        "基线（停泊，不节流）": dict(),
        "T1 回撤8%→0.6/16%→0.35": dict(tiers=[(-0.16, 0.35), (-0.08, 0.6)]),
        "T2 回撤12%→0.5": dict(tiers=[(-0.12, 0.5)]),
        "T3 恒定半仓": dict(flat_mult=0.5),
    }
    print(f"\n{'方案':24s}" + "".join(f"{w:>22s}" for w in WINDOWS))
    for label, kw in variants.items():
        cells = []
        for _w, win in WINDOWS.items():
            r = simulate(trades, win, regime, bond, gold, **kw)
            cells.append(f"{r['total']:+.1f}%/DD{r['dd']:+.1f}/C{r['calmar']:.2f}")
        print(f"{label:24s}" + "".join(f"{c:>22s}" for c in cells))


if __name__ == "__main__":
    main()
