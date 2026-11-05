"""两件事一次测：
A. 参数集成（不选，平分槽位）—— 与单套基准比
B. 行情信号**反向**（趋势强 → 持有更短），验证"强势后续更弱"这个我们自己数据里看到的现象

统一自检：单套 30 天必须复现训练集 +74.1%。

用法：python scripts/research/ensemble.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from adaptive_market import rolling_z, signals  # noqa: E402
from adaptive_online import HI, HOLDS, LO, START, nearest, replay_by_hold  # noqa: E402
from study import STRESS, TRAIN, VALID, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct  # noqa: E402

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def simulate(per_hold, window, quotas: dict[int, int] | None = None, signal=None,
             lr: float = 0.0, invert: bool = False,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0) -> dict:
    """quota 模式=参数集成；signal 模式=行情驱动（invert=True 时方向反向）。"""
    start, end = window
    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for h in HOLDS for t in per_hold[h]}
                       | {dt.date.fromisoformat(t.exit_date) for h in HOLDS for t in per_hold[h]})
    all_dates = [d for d in all_dates if start <= d <= end]

    h = START
    cash = cash0
    pos: list = []
    curve: list[float] = []
    hs: list[float] = []

    for day in all_dates:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
            else:
                still.append(p)
        pos = still

        if signal is not None and lr > 0:
            z = rolling_z(signal["values"], day, signal["days"], 250)
            if z is not None:
                target = START - 15 * z if invert else START + 15 * z
                target = min(HI, max(LO, target))
                h = h + lr * (target - h)
            hs.append(h)

        if quotas:
            used: dict[int, int] = {}
            for p in pos:
                used[p["hold"]] = used.get(p["hold"], 0) + 1
            for hold, cap in quotas.items():
                if used.get(hold, 0) >= cap:
                    continue
                for t in per_hold[hold]:
                    if t.entry_date != day.isoformat():
                        continue
                    if used.get(hold, 0) >= cap or len(pos) >= slots:
                        break
                    equity = cash + sum(q["value"] for q in pos)
                    size = min(equity * fraction, cash)
                    if size < 1000:
                        break
                    cash -= size
                    pos.append({"trade": t, "value": size, "hold": hold,
                                "exit_day": dt.date.fromisoformat(t.exit_date)})
                    used[hold] = used.get(hold, 0) + 1
        else:
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
                pos.append({"trade": t, "value": size, "hold": active,
                            "exit_day": dt.date.fromisoformat(t.exit_date)})

        curve.append(cash + sum(p["value"] for p in pos))

    if not curve:
        return {}
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": (curve[-1] / cash0 - 1) * 100, "dd": mdd * 100,
            "h_min": round(min(hs), 1) if hs else 30.0,
            "h_max": round(max(hs), 1) if hs else 30.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="all")
    ap.add_argument("--lr", type=float, default=0.02)
    ap.add_argument("--sweep", action="store_true", help="扫描 lr × 信号 × 方向")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    print("重放各持有期…")
    per_hold = replay_by_hold(bars_map, sh, cost)
    sig = signals(sh)
    trend = {"values": sig["signals"]["trend"], "days": sig["days"]}

    check = simulate(per_hold, WINDOWS["TRAIN"], quotas={30: 5})
    print(f"\n自检（单套 30, TRAIN）：{check['total']:+.1f}% / {check['dd']:+.1f}%"
          f"（应为 +74.1 / −17.4）")
    if abs(check["total"] - 74.1) > 3:
        print("自检未通过，停止")
        return

    if args.sweep:
        print("\n===== 敏感性扫描（全部用反向映射：强→短） =====")
        for name, window in WINDOWS.items():
            print(f"\n--- {name} ---")
            print(f"{'信号':10s}{'lr':>6s}{'总收益':>9s}{'回撤':>9s}")
            for sname in ("trend", "autocorr", "mom"):
                s = {"values": sig["signals"][sname], "days": sig["days"]}
                for lr in (0.01, 0.02, 0.05):
                    res = simulate(per_hold, window, signal=s, lr=lr, invert=True)
                    if res:
                        print(f"{sname:10s}{lr:>6.2f}{res['total']:>+9.1f}{res['dd']:>+9.1f}")
        return
    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        print(f"\n===== {name} {window[0]}..{window[1]} =====")
        print(f"{'方案':26s}{'总收益':>9s}{'回撤':>9s}{'h范围':>12s}")
        rows = [
            ("单套 30（基准）", dict(quotas={30: 5})),
            ("集成 20/30/40（2/2/1）", dict(quotas={20: 2, 30: 2, 40: 1})),
            ("集成 25/35（3/2）", dict(quotas={25: 3, 35: 2})),
            ("集成 15/25/35/45（2/1/1/1）", dict(quotas={15: 2, 25: 1, 35: 1, 45: 1})),
            ("行情信号·正向（强→久）", dict(signal=trend, lr=args.lr, invert=False)),
            ("行情信号·反向（强→短）", dict(signal=trend, lr=args.lr, invert=True)),
        ]
        for label, kwargs in rows:
            res = simulate(per_hold, window, **kwargs)
            if not res:
                continue
            span = f"{res['h_min']}~{res['h_max']}"
            print(f"{label:26s}{res['total']:>+9.1f}{res['dd']:>+9.1f}{span:>12s}")


if __name__ == "__main__":
    main()
