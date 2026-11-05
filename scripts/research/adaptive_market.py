"""用「近期行情数据」连续微调参数（输入不是成交，是市场本身）。

用户的澄清：不按交易反馈更新，而是看最近一段时间的**数据**——算出一个方向（梯度/趋势），
在原参数上一点一点加减。没有周期概念，每天都在动一点。

实现：
- 参数 h（持有天数，15~45），初始 30，实际取最近的 5 的倍数。
- 三个"行情信号"（都只用当日及之前的数据）：
    autocorr  近 60 日收益一阶自相关（>0 趋势市，<0 震荡市）
    trend     20 日趋势效率 = |20日收益| / (20日波动率×√20)（越大越趋势）
    mom       60 日动量（正=上行趋势）
- 映射到目标持有期：h_target = 30 + 15 × z(信号)，z 为 250 日滚动 z-score，夹在 [15,45]
- 连续更新：h ← h + lr × (h_target − h)，每交易日一步（默认 lr=0.02，即约 50 天才走完一档）

自检：lr=0 必须复现固定 30 天的训练结果（+74.1%）。

用法：python scripts/research/adaptive_market.py [--lr 0.02]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from adaptive_online import HI, HOLDS, LO, START, nearest, replay_by_hold  # noqa: E402
from study import STRESS, TRAIN, VALID, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct  # noqa: E402

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def rolling_z(values: dict, day: dt.date, days: list, win: int) -> float | None:
    i = days.index(day) if day in days else None
    if i is None or i < win:
        return None
    window = [values[d] for d in days[i - win:i] if values.get(d) is not None]
    if len(window) < 30:
        return None
    mu = statistics.fmean(window)
    sd = statistics.pstdev(window)
    if sd <= 0:
        return None
    v = values.get(day)
    return None if v is None else (v - mu) / sd


def signals(sh_series: list) -> dict:
    """三个信号序列（全部只用当日及之前的数据）。"""
    days = [d for d, _ in sh_series]
    closes = [c for _, c in sh_series]
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    idx = {d: i for i, d in enumerate(days)}

    def autocorr(i: int, win: int = 60) -> float | None:
        if i < win:
            return None
        a = rets[i - win + 1:i]
        b = rets[i - win:i - 1]
        if len(a) < 30:
            return None
        ma, mb = statistics.fmean(a), statistics.fmean(b)
        num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=False))
        da = sum((x - ma) ** 2 for x in a) ** 0.5
        db = sum((y - mb) ** 2 for y in b) ** 0.5
        return None if da <= 0 or db <= 0 else num / (da * db)

    def trend(i: int, win: int = 20) -> float | None:
        if i < win:
            return None
        seg = rets[i - win + 1:i + 1]
        sd = statistics.pstdev(seg) if len(seg) > 2 else 0.0
        if sd <= 0:
            return None
        move = closes[i] / closes[i - win] - 1
        return abs(move) / (sd * (win ** 0.5))

    out = {"autocorr": {}, "trend": {}, "mom": {}}
    for d in days:
        i = idx[d]
        ac = autocorr(i)
        if ac is not None:
            out["autocorr"][d] = ac
        tr = trend(i)
        if tr is not None:
            out["trend"][d] = tr
        if i >= 60:
            out["mom"][d] = closes[i] / closes[i - 60] - 1
    return {"days": days, "signals": out}


def simulate(per_hold, bars_map, window, signal_name: str, sig: dict, lr: float,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0) -> dict:
    start, end = window
    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for h in HOLDS for t in per_hold[h]}
                       | {dt.date.fromisoformat(t.exit_date) for h in HOLDS for t in per_hold[h]})
    all_dates = [d for d in all_dates if start <= d <= end]
    days = sig["days"]
    values = sig["signals"].get(signal_name, {})

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

        # 行情驱动：把 h 往目标推一小步（每天）
        z = rolling_z(values, day, days, 250)
        if z is not None and lr > 0:
            target = min(HI, max(LO, START + 15 * z))
            h = h + lr * (target - h)
        hs.append(h)

        active = nearest(h)
        for t in [x for x in per_hold[active] if x.entry_date == day.isoformat()]:
            if len(pos) >= slots:
                break
            equity = cash + sum(p["value"] for p in pos)
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
    return {"total": (curve[-1] / cash0 - 1) * 100, "dd": mdd * 100,
            "h_min": round(min(hs), 1), "h_max": round(max(hs), 1),
            "h_end": round(hs[-1], 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lr", type=float, default=0.02)
    ap.add_argument("--window", default="all")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    print("重放各持有期…")
    per_hold = replay_by_hold(bars_map, sh, cost)
    sig = signals(sh)

    check = simulate(per_hold, bars_map, WINDOWS["TRAIN"], "trend", sig, 0.0)
    print(f"\n自检（lr=0, TRAIN）：{check['total']:+.1f}% / {check['dd']:+.1f}%"
          f"（应为 +74.1% / −17.4%）")
    if abs(check["total"] - 74.1) > 3:
        print("自检未通过，停止")
        return

    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        print(f"\n===== {name} {window[0]}..{window[1]} · 行情驱动 lr={args.lr} =====")
        print(f"{'信号':16s}{'总收益':>9s}{'回撤':>9s}{'h终值':>7s}{'h范围':>12s}")
        for sname, label in (("autocorr", "自相关(趋势/震荡)"), ("trend", "20日趋势效率"),
                             ("mom", "60日动量")):
            res = simulate(per_hold, bars_map, window, sname, sig, args.lr)
            if res:
                span = f"{res['h_min']}~{res['h_max']}"
                print(f"{label:16s}{res['total']:>+9.1f}{res['dd']:>+9.1f}"
                      f"{res['h_end']:>7.1f}{span:>12s}")


if __name__ == "__main__":
    main()
