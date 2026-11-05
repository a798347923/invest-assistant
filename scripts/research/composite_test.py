"""多输入梯度：把 8 个信号合成为一个综合信号，在两个池上验证。

为什么合成：单个信号各自称王的窗口不同（breadth 赢训练、mom60 赢压力），
合成（z 分数等权平均）是"不选信号"的做法——不需要判断哪个对。

信号方向（先验，都是"市场越热 → 持有越短"）：
  trend20 / mom60 / breadth / turn_med / pe_med / autocorr60 取正（越热 z 越大）
  再合成 z_comp = 均值；持有期 h += lr × (−15 × z_comp)

对照：固定 30 天。两个池（原池 / 陌生池）各跑三窗口。

用法：python scripts/research/composite_test.py
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from holdout_adopted import load_bars_from  # noqa: E402
from signals_multi import build_signals  # noqa: E402
from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_index  # noqa: E402
from sweep_all import zscore_series  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
POOLS = {"原池": os.path.join(RESEARCH, "bars"),
         "陌生池": os.path.join(RESEARCH, "bars_holdout")}
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
HOLDS = [15, 20, 25, 30, 35, 40, 45]
HEAT = ("trend20", "mom60", "breadth", "turn_med", "pe_med", "autocorr60")


def run(bars_map, sh, regime, cost, tables, zcomp, lr):
    days = sorted({dt.date.fromisoformat(t.entry_date) for ts in tables.values() for t in ts}
                  | {dt.date.fromisoformat(t.exit_date) for ts in tables.values() for t in ts})
    h = 30.0
    cash = 50000.0
    pos: list = []
    curve: list[float] = []
    for day in days:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still
        z = zcomp.get(day)
        if z is not None and lr > 0:
            h = min(45.0, max(15.0, h + lr * (-15.0 * z)))
        active = min(HOLDS, key=lambda x: (abs(x - h), x))
        for t in tables[active]:
            if t.entry_date != day.isoformat():
                continue
            if len(pos) >= 5:
                break
            eq = cash + sum(q["value"] for q in pos)
            size = min(eq * 0.2, cash)
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
    return {"total": (curve[-1] / 50000 - 1) * 100, "dd": mdd * 100}


def main() -> None:
    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    days = [d for d, _ in sh]

    for pool_name, folder in POOLS.items():
        bars_map = load_bars_from(folder)
        print(f"\n########## {pool_name}（{len(bars_map)} 只） ##########")
        sigs = build_signals(bars_map, days, [c for _, c in sh])
        zs = {k: zscore_series(v, days) for k, v in sigs.items()}

        # 合成信号：可用信号在当日的 z 均值
        zcomp = {}
        for d in days:
            vals = [zs[k][d] for k in HEAT if k in zs and d in zs[k]]
            if len(vals) >= 3:
                zcomp[d] = statistics.fmean(vals)
        print(f"  综合信号覆盖 {len(zcomp)} 天")

        for wname, (start, end) in WINDOWS.items():
            tables = {h: [] for h in HOLDS}
            for h in HOLDS:
                for code, bars in bars_map.items():
                    sliced = [b for b in bars if start - WARMUP <= b.date <= end]
                    if len(sliced) < 120:
                        continue
                    got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                                       enhanced_regime=regime, opts=base_opts(max_hold=h))
                    tables[h].extend(t for t in got
                                     if start.isoformat() <= t.entry_date <= end.isoformat())
            win_tables = {h: [t for t in tables[h]
                              if start.isoformat() <= t.entry_date <= end.isoformat()]
                          for h in HOLDS}
            base = run(bars_map, sh, regime, cost, {h: win_tables[h] for h in HOLDS},
                       {}, 0.0)
            r1 = run(bars_map, sh, regime, cost, win_tables, zcomp, 0.01)
            r2 = run(bars_map, sh, regime, cost, win_tables, zcomp, 0.02)
            print(f"  {wname:7s} 固定30 {base['total']:>+7.1f}/{base['dd']:>+7.1f}"
                  f" | 合成 lr=.01 {r1['total']:>+7.1f}/{r1['dd']:>+7.1f}"
                  f"（{r1['total'] - base['total']:>+6.1f}pp）"
                  f" | lr=.02 {r2['total']:>+7.1f}/{r2['dd']:>+7.1f}"
                  f"（{r2['total'] - base['total']:>+6.1f}pp）")


if __name__ == "__main__":
    main()
