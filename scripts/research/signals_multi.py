"""多信号库：把本地已有的数据都做成"行情信号"，逐个喂给参数调度器。

信号（每日一个值，全部只用当日及之前数据）：
  1 trend20     20 日趋势效率（|20日收益| / 波动率×√20）
  2 autocorr60  近 60 日收益一阶自相关
  3 mom60       60 日动量
  4 vol20       20 日已实现波动率
  5 breadth     市场宽度 = 池内收盘价 > MA20 的股票占比     ← 240 只日线
  6 pe_med      池内 PE_TTM 中位数                        ← Tushare 估值缓存
  7 turn_med    池内换手率中位数                          ← Tushare 估值缓存
  8 event_dens  近 30 日负面事件密度（减持+预亏+解禁）      ← 事件缓存

映射（先验方向，都是"强/热 → 持有更短"）：
  h_target = 30 − 15 × z(signal)，z 为 250 日滚动 z-score
  vol20 取反（波动越大 → 持有越短，即 z 越大 h 越小，同样减号）

严格协议：只用训练集挑信号，验证集/压力窗口当样本外。

用法：python scripts/research/signals_multi.py [--lr 0.01]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from adaptive_market import rolling_z  # noqa: E402
from adaptive_online import HI, HOLDS, LO, START, nearest, replay_by_hold  # noqa: E402
from study import STRESS, TRAIN, VALID, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
FUND_DIR = os.path.join(RESEARCH, "fund")
EVENT_DIR = os.path.join(RESEARCH, "events")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def build_signals(bars_map: dict, days: list, closes: list) -> dict:
    out: dict[str, dict] = {}

    # --- 指数类
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    trend, autocorr, mom, vol = {}, {}, {}, {}
    for i, d in enumerate(days):
        if i >= 20:
            seg = rets[i - 19:i + 1]
            sd = statistics.pstdev(seg) if len(seg) > 2 else 0.0
            if sd > 0:
                trend[d] = abs(closes[i] / closes[i - 20] - 1) / (sd * (20 ** 0.5))
            vol[d] = sd
        if i >= 61:
            a, b = rets[i - 59:i + 1], rets[i - 60:i]
            ma, mb = statistics.fmean(a), statistics.fmean(b)
            num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False))
            da = sum((x - ma) ** 2 for x in a) ** 0.5
            db = sum((y - mb) ** 2 for y in b) ** 0.5
            if da > 0 and db > 0:
                autocorr[d] = num / (da * db)
            mom[d] = closes[i] / closes[i - 60] - 1
    out["trend20"] = trend
    out["autocorr60"] = autocorr
    out["mom60"] = mom
    out["vol20"] = vol

    # --- 市场宽度（池内 > MA20 的比例）
    per_code = {}
    for code, bars in bars_map.items():
        closes_c = [b.close for b in bars]
        above = {}
        for i, b in enumerate(bars):
            if i >= 19:
                ma = sum(closes_c[i - 19:i + 1]) / 20
                above[b.date] = 1 if b.close > ma else 0
        per_code[code] = above
    breadth = {}
    for d in days:
        vals = [m[d] for m in per_code.values() if d in m]
        if len(vals) >= 50:
            breadth[d] = sum(vals) / len(vals)
    out["breadth"] = breadth

    # --- 估值 / 换手（Tushare 缓存）
    pe: dict = {}
    turn: dict = {}
    if os.path.isdir(FUND_DIR):
        for name in sorted(os.listdir(FUND_DIR)):
            if not name.endswith(".csv"):
                continue
            with open(os.path.join(FUND_DIR, name), encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        d = dt.date.fromisoformat(row["date"])
                    except (ValueError, KeyError):
                        continue
                    try:
                        p = float(row["pe_ttm"]) if row["pe_ttm"] else None
                    except (TypeError, ValueError):
                        p = None
                    try:
                        t = float(row["turnover_rate"]) if row["turnover_rate"] else None
                    except (TypeError, ValueError):
                        t = None
                    if p is not None and p > 0:
                        pe.setdefault(d, []).append(p)
                    if t is not None:
                        turn.setdefault(d, []).append(t)
    out["pe_med"] = {d: statistics.median(v) for d, v in pe.items() if len(v) >= 30}
    out["turn_med"] = {d: statistics.median(v) for d, v in turn.items() if len(v) >= 30}

    # --- 事件密度（近 30 日负面事件数）
    daily: dict = {}
    if os.path.isdir(EVENT_DIR):
        for name in sorted(os.listdir(EVENT_DIR)):
            if not name.endswith(".csv"):
                continue
            with open(os.path.join(EVENT_DIR, name), encoding="utf-8") as fh:
                next(fh)
                for line in fh:
                    parts = line.rstrip("\n").split(",", 4)
                    if len(parts) < 5:
                        continue
                    kind, ann = parts[0], parts[1]
                    try:
                        d = dt.date(int(ann[:4]), int(ann[4:6]), int(ann[6:8]))
                    except (ValueError, IndexError):
                        continue
                    if kind == "trade" or (kind == "forecast" and parts[4].split(" ")[0] in
                                           {"预减", "略减", "首亏", "续亏", "增亏"}):
                        daily[d] = daily.get(d, 0) + 1
    dens = {}
    for i, d in enumerate(days):
        if i < 30:
            continue
        dens[d] = sum(daily.get(days[j], 0) for j in range(i - 29, i + 1))
    out["event_dens"] = dens
    return out


def simulate(per_hold, window, sig: dict, days: list, lr: float,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0) -> dict:
    start, end = window
    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for h in HOLDS for t in per_hold[h]}
                       | {dt.date.fromisoformat(t.exit_date) for h in HOLDS for t in per_hold[h]})
    all_dates = [d for d in all_dates if start <= d <= end]

    h = START
    cash = cash0
    pos: list = []
    curve: list[float] = []

    for day in all_dates:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still

        z = rolling_z(sig, day, days, 250) if sig else None
        if z is not None and lr > 0:
            h = min(HI, max(LO, h + lr * (-15.0 * z)))

        active = nearest(h)
        for t in per_hold[active]:
            if t.entry_date != day.isoformat():
                continue
            if len(pos) >= slots:
                break
            equity = cash + sum(q["value"] for q in pos)
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
    ap.add_argument("--lr", type=float, default=0.01)
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    days = [d for d, _ in sh]
    closes = [c for _, c in sh]

    print("构建信号库…")
    sigs = build_signals(bars_map, days, closes)
    for k, v in sigs.items():
        print(f"  {k:12s} {len(v):5d} 天覆盖")

    print("重放各持有期…")
    per_hold = replay_by_hold(bars_map, sh, cost)

    base = {name: simulate(per_hold, w, {}, days, 0.0) for name, w in WINDOWS.items()}
    print("\n基准（不动参数）：" + " | ".join(
        f"{k} {v['total']:+.1f}%/{v['dd']:+.1f}%" for k, v in base.items()))

    rows = {}
    for name, w in WINDOWS.items():
        print(f"\n===== {name} · lr={args.lr} =====")
        print(f"{'信号':14s}{'总收益':>9s}{'回撤':>9s}{'vs基准':>9s}")
        for sname, sig in sigs.items():
            r = simulate(per_hold, w, sig, days, args.lr)
            rows[(name, sname)] = r
            print(f"{sname:14s}{r['total']:>+9.1f}{r['dd']:>+9.1f}"
                  f"{r['total'] - base[name]['total']:>+9.1f}")

    print("\n===== 严格协议（仅用训练集挑信号） =====")
    best = max(sigs, key=lambda s: rows[("TRAIN", s)]["total"])
    print(f"训练集选出：{best}（训练 {rows[('TRAIN', best)]['total']:+.1f}%）")
    for name in ("VALID", "STRESS"):
        r = rows[(name, best)]
        print(f"  {name}：{r['total']:+.1f}% / 回撤 {r['dd']:+.1f}%"
              f"（基准 {base[name]['total']:+.1f}%）")


if __name__ == "__main__":
    main()
