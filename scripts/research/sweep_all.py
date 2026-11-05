"""大规模组合扫描：参数 × 信号 × 学习率 × 方向。

参数网格（决定交易本身，需重放）：
- max_hold: 15/20/25/30/35/40/45（7）
- 止损乘数: (0.98,0.95) / (0.97,0.94) / (0.96,0.93) / (0.95,0.92)（4）
=> 28 次重放（约 30-40 分钟）

调度网格（复用上面的交易表，很快）：
- 信号: trend20 / autocorr60 / mom60 / vol20 / breadth / pe_med / turn_med / event_dens（8）
- 学习率: 0.01 / 0.02（2）
- 方向: 反向(强→短) / 正向(强→久)（2）
=> 32 种调度 × 28 个参数 = 896 种组合 × 3 窗口

评估（防止"样本内最好"的幻觉）：
1. 训练集最优（最容易自欺的榜单）
2. 三窗口全为正的组合
3. 稳健榜：按"最差窗口的改进"排序（maximin）

用法：python scripts/research/sweep_all.py [--lr 0.01 0.02]
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

from signals_multi import build_signals  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
HOLDS = [15, 20, 25, 30, 35, 40, 45]
STOPS = [(0.98, 0.95), (0.97, 0.94), (0.96, 0.93), (0.95, 0.92)]
START, LO, HI = 30.0, 15.0, 45.0
Z_WIN = 250


def nearest(h: float, holds: list[int]) -> int:
    return min(holds, key=lambda x: (abs(x - h), x))


def zscore_series(values: dict, days: list) -> dict:
    """250 日滚动 z-score（只用当日及之前）。"""
    out = {}
    buf: list[float] = []
    for d in days:
        v = values.get(d)
        if v is None:
            continue
        buf.append(v)
        if len(buf) > Z_WIN:
            buf.pop(0)
        if len(buf) >= 60:
            mu = statistics.fmean(buf)
            sd = statistics.pstdev(buf)
            if sd > 0:
                out[d] = (v - mu) / sd
    return out


def simulate(per_hold, window, z: dict | None, lr: float, invert: bool, fixed_hold: int | None = None,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0) -> dict:
    start, end = window
    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for ts in per_hold.values() for t in ts}
                       | {dt.date.fromisoformat(t.exit_date) for ts in per_hold.values() for t in ts})
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
        if z is not None and lr > 0:
            zv = z.get(day)
            if zv is not None:
                g = (-15.0 if invert else 15.0) * zv
                h = min(HI, max(LO, h + lr * g))
        active = fixed_hold if fixed_hold else nearest(h, HOLDS)
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


CACHE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research", "sweep_grid.pkl")


def load_or_build_grid(bars_map, sh, cost) -> dict:
    """重放结果缓存到磁盘——一次 30 分钟，后续迭代都复用。"""
    import pickle

    if os.path.exists(CACHE):
        with open(CACHE, "rb") as fh:
            data = pickle.load(fh)
        if len(data) == len(HOLDS) * len(STOPS):
            print(f"  复用缓存 {CACHE}（{len(data)} 组）")
            return data
    grid = replay_grid(bars_map, sh, cost)
    with open(CACHE, "wb") as fh:
        pickle.dump(grid, fh)
    return grid

def replay_grid(bars_map, sh, cost) -> dict:
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    grid = {}
    total = len(HOLDS) * len(STOPS)
    i = 0
    for stop_p, stop_b in STOPS:
        for hold in HOLDS:
            i += 1
            opts = base_opts(max_hold=hold)
            trades = []
            for code, bars in bars_map.items():
                sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
                if len(sliced) < 120:
                    continue
                got = _replay_code(code, sliced, sh, cost, rules, stop_p, stop_b,
                                   enhanced_regime=regime, opts=opts)
                trades.extend(t for t in got
                              if "2015-04-01" <= t.entry_date <= "2026-10-31")
            grid[(hold, (stop_p, stop_b))] = trades
            print(f"  [{i:2d}/{total}] hold={hold:2d} stop=({stop_p},{stop_b}) "
                  f"{len(trades):5d} 笔", flush=True)
    return grid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    days = [d for d, _ in sh]

    print("构建信号库…")
    sigs = build_signals(bars_map, days, [c for _, c in sh])
    zseries = {name: zscore_series(vals, days) for name, vals in sigs.items()}
    print("  完成：" + "，".join(f"{k}({len(v)})" for k, v in zseries.items()))

    print("\n重放参数网格（28 组，约 30 分钟）…")
    grid = load_or_build_grid(bars_map, sh, cost)

    # 按止损档分组：每档都要有全部 7 个持有期，信号才有"别的持有期可调"
    by_stop = {stop: {hold: grid[(hold, stop)] for hold in HOLDS} for stop in STOPS}

    rows = []
    for stop in STOPS:
        table = by_stop[stop]
        for sname, z in zseries.items():
            for invert in (True, False):
                for lr in (0.01, 0.02):
                    per_window = {w: simulate(table, win, z, lr, invert)
                                  for w, win in WINDOWS.items()}
                    rows.append({
                        "hold": "auto", "stop": f"{stop[0]:.2f}/{stop[1]:.2f}",
                        "signal": sname, "dir": "逆" if invert else "正", "lr": lr,
                        "TRAIN": per_window["TRAIN"]["total"],
                        "VALID": per_window["VALID"]["total"],
                        "STRESS": per_window["STRESS"]["total"],
                        "dd_TRAIN": per_window["TRAIN"]["dd"],
                        "dd_VALID": per_window["VALID"]["dd"],
                        "dd_STRESS": per_window["STRESS"]["dd"],
                    })
        # 纯参数组合（不做自适应），用于对比"调参数 vs 调信号"
        for hold in HOLDS:
            per_window = {w: simulate(table, win, None, 0.0, True, fixed_hold=hold)
                          for w, win in WINDOWS.items()}
            rows.append({
                "hold": hold, "stop": f"{stop[0]:.2f}/{stop[1]:.2f}",
                "signal": "-", "dir": "-", "lr": 0.0,
                "TRAIN": per_window["TRAIN"]["total"],
                "VALID": per_window["VALID"]["total"],
                "STRESS": per_window["STRESS"]["total"],
                "dd_TRAIN": per_window["TRAIN"]["dd"],
                "dd_VALID": per_window["VALID"]["dd"],
                "dd_STRESS": per_window["STRESS"]["dd"],
            })

    # 基准：hold30 + 默认止损 + 不动参数
    base = {}
    for wname, win in WINDOWS.items():
        base[wname] = simulate(by_stop[STOPS[0]], win, None, 0.0, True, fixed_hold=30)
    print("\n基准（hold30 / 默认止损 / 不动参数）：" + " | ".join(
        f"{k} {v['total']:+.1f}%/{v['dd']:+.1f}%" for k, v in base.items()))

    for r in rows:
        r["worst"] = min(r["TRAIN"] - base["TRAIN"]["total"],
                         r["VALID"] - base["VALID"]["total"],
                         r["STRESS"] - base["STRESS"]["total"])
        r["all_pos"] = all(r[w] > base[w]["total"] for w in WINDOWS)

    def show(title, data, n=12):
        print(f"\n===== {title} =====")
        print(f"{'hold':>5s}{'stop':>12s}{'signal':>12s}{'dir':>4s}{'lr':>6s}"
              f"{'TRAIN':>8s}{'VALID':>8s}{'STRESS':>8s}{'最差窗口':>9s}")
        for r in data[:n]:
            print(f"{str(r['hold']):>5s}{r['stop']:>12s}{r['signal']:>12s}{r['dir']:>4s}"
                  f"{r['lr']:>6.2f}{r['TRAIN']:>+8.1f}{r['VALID']:>+8.1f}{r['STRESS']:>+8.1f}"
                  f"{r['worst']:>+9.1f}")

    show("按训练集收益排序（最容易自欺）", sorted(rows, key=lambda r: -r["TRAIN"]))
    allpos = [r for r in rows if r["all_pos"]]
    show("三窗口全部优于基准", sorted(allpos, key=lambda r: -r["worst"]), n=15)
    show("稳健榜（按最差窗口的改进排序）", sorted(rows, key=lambda r: -r["worst"]), n=15)
    print(f"\n共 {len(rows)} 种组合；三窗口全为正的有 {len(allpos)} 种")

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"明细写入 {args.out}")


if __name__ == "__main__":
    main()
