"""在**陌生股票池**上重验：①已采纳的两个改动 ②梯度/自适应算法。

背景：跨池检验证明策略本身对样本高度敏感（训练集 +74% → +21%），
所以所有"在合并样本上验证通过"的改动都必须在新池上重验。

三块内容：
A. 波动率平价配仓（paper.risk_based_sizing）：atr 配仓 vs 等权
B. 弱市停泊（paper.parking_enabled）：弱市闲置现金买债7/金3 vs 拿现金
C. 梯度/自适应持有期：trend20 / autocorr60 / breadth 反向信号，lr=0.01/0.02 vs 固定 30

用法：python scripts/research/holdout_adopted.py
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import features  # noqa: E402
from signals_multi import build_signals  # noqa: E402
from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_index  # noqa: E402
from sweep_all import zscore_series  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource import Bar  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
HOLDOUT = os.path.join(RESEARCH, "bars_holdout")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
HOLDS = [15, 20, 25, 30, 35, 40, 45]


def load_bars_from(folder: str) -> dict:
    out = {}
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


def load_etf(code: str) -> dict:
    path = os.path.join(RESEARCH, "etf", f"{code}.csv")
    if not os.path.exists(path):
        path = os.path.join(RESEARCH, "etf", f"sh{code}.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            d, c = line.strip().split(",")[:2]
            out[dt.date.fromisoformat(d)] = float(c)
    return out


def simulate(trades, window, bars_map, regime, sizing="equal", park=False,
             etf_bond=None, etf_gold=None, slots=5, fraction=0.2, cash0=50000.0) -> dict:
    start, end = window
    by_entry = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                  | {dt.date.fromisoformat(t.exit_date) for t in trades})
    days = [d for d in days if start <= d <= end]

    feats = {}
    for code, bars in bars_map.items():
        for t in trades:
            if t.code == code:
                f = features(bars, dt.date.fromisoformat(t.entry_date))
                if f:
                    feats[(code, t.entry_date)] = f
    atrs = sorted(f["atr_pct"] for f in feats.values() if f.get("atr_pct"))
    med_atr = atrs[len(atrs) // 2] if atrs else 0.03

    cash = cash0
    pos = []
    curve = []
    for day in days:
        prev = days[days.index(day) - 1] if days.index(day) > 0 else day
        # 停泊：弱市的现金获得债/金组合收益
        if park and regime.get(prev) is False:
            b, g = etf_bond.get(day), etf_gold.get(day)
            pb, pg = etf_bond.get(prev), etf_gold.get(prev)
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
        for t in by_entry.get(day, []):
            if len(pos) >= slots:
                break
            weight = fraction
            if sizing == "atr":
                f = feats.get((t.code, t.entry_date)) or {}
                a = f.get("atr_pct") or med_atr
                weight = fraction * max(0.5, min(2.0, med_atr / a if a else 1.0))
            size = min(equity * weight, cash)
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
    return {"total": (curve[-1] / cash0 - 1) * 100, "dd": mdd * 100,
            "trades": len({id(t) for t in trades})}


def main() -> None:
    cost = _cost_pct(get_settings())
    bars_map = load_bars_from(HOLDOUT)
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    bond = load_etf("511260")
    gold = load_etf("518880")
    print(f"陌生池：{len(bars_map)} 只；ETF 数据 {len(bond)}/{len(gold)} 天")

    days = [d for d, _ in sh]
    sigs = build_signals(bars_map, days, [c for _, c in sh])
    zs = {k: zscore_series(v, days) for k, v in sigs.items()}

    # 每个窗口：hold30 的交易（A/B 用）+ 7 个持有期（C 用）
    for name, (start, end) in WINDOWS.items():
        print(f"\n===== {name} {start}..{end} =====")
        t30 = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if start - WARMUP <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=base_opts(max_hold=30))
            t30.extend(t for t in got if start.isoformat() <= t.entry_date <= end.isoformat())

        a = simulate(t30, (start, end), bars_map, regime, sizing="equal")
        b = simulate(t30, (start, end), bars_map, regime, sizing="atr")
        c = simulate(t30, (start, end), bars_map, regime, sizing="equal",
                     park=True, etf_bond=bond, etf_gold=gold)
        print(f"  A 配仓：等权 {a['total']:+.1f}/{a['dd']:+.1f}  →  atr {b['total']:+.1f}/{b['dd']:+.1f}"
              f"（收益 {b['total'] - a['total']:+.1f}pp，回撤 {b['dd'] - a['dd']:+.1f}pp）")
        print(f"  B 停泊：现金 {a['total']:+.1f}/{a['dd']:+.1f}  →  债7金3 {c['total']:+.1f}/{c['dd']:+.1f}"
              f"（收益 {c['total'] - a['total']:+.1f}pp，回撤 {c['dd'] - a['dd']:+.1f}pp）")

        # C：梯度/自适应
        tables = {h: [] for h in HOLDS}
        tables[30] = t30
        for h in HOLDS:
            if h == 30:
                continue
            for code, bars in bars_map.items():
                sliced = [b for b in bars if start - WARMUP <= b.date <= end]
                if len(sliced) < 120:
                    continue
                got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                                   enhanced_regime=regime, opts=base_opts(max_hold=h))
                tables[h].extend(t for t in got
                                 if start.isoformat() <= t.entry_date <= end.isoformat())

        base = simulate(t30, (start, end), bars_map, regime)
        print(f"  C 自适应（固定30 = {base['total']:+.1f}%）：")
        for sname in ("trend20", "autocorr60", "breadth"):
            z = zs[sname]
            for lr in (0.01, 0.02):
                # 逐日推进的简化版：用各持有期的交易表，按 h 切换
                all_dates = sorted({dt.date.fromisoformat(t.entry_date)
                                    for ts in tables.values() for t in ts}
                                   | {dt.date.fromisoformat(t.exit_date)
                                      for ts in tables.values() for t in ts})
                all_dates = [d for d in all_dates if start <= d <= end]
                h = 30.0
                cash = 50000.0
                pos = []
                curve = []
                for day in all_dates:
                    still = []
                    for p in pos:
                        if p["exit_day"] <= day:
                            cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
                        else:
                            still.append(p)
                    pos = still
                    zv = z.get(day)
                    if zv is not None:
                        h = min(45, max(15, h + lr * (-15.0 * zv)))
                    active = min(HOLDS, key=lambda x: abs(x - h))
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
                peak, mdd = curve[0], 0.0
                for v in curve:
                    peak = max(peak, v)
                    mdd = min(mdd, v / peak - 1)
                total = (curve[-1] / 50000 - 1) * 100
                print(f"     {sname:11s} lr={lr:.2f}  {total:+.1f}% / {mdd * 100:+.1f}%"
                      f"（vs 固定 {total - base['total']:+.1f}pp）")


if __name__ == "__main__":
    main()
