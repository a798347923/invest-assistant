"""在线参数学习（每笔交易更新一步）：真正的"持续更新"，没有周期。

思路（用户）：像训练一样，根据最近的反馈算出一个方向，在现有参数上加减一点点。

实现：
- 参数 h（持有天数，浮点，范围 15~45），初始 30；实际使用时取最近的 5 的倍数（15/20/.../45）。
- **每平掉一笔**，看这只票在平仓后 5 个交易日的走势：
    涨 > +2% → 说明"拿太短了"，h += lr
    跌 < −2% → 说明"拿太久了"，h -= lr
    其余不更新
- lr（学习率）很小：默认 0.5，意味着约 10 个有效信号才移动 5 天。

自检：lr=0 时，本模拟器必须复现固定 30 天的已知结果（训练 +74.1%）。不通过就不报结果。

对照组：
- fixed：h 恒为 30
- online：上述在线更新
- random：同样的步长但方向随机（检验"方向"是否有用）

用法：python scripts/research/adaptive_online.py [--lr 0.5]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
FULL = (dt.date(2015, 4, 1), dt.date(2026, 10, 31))
HOLDS = [15, 20, 25, 30, 35, 40, 45]
START, LO, HI = 30.0, 15.0, 45.0
POST_DAYS = 5
THRESH = 0.02


def nearest(h: float) -> int:
    return min(HOLDS, key=lambda x: (abs(x - h), x))


def replay_by_hold(bars_map, sh, cost) -> dict:
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    out = {}
    for hold in HOLDS:
        opts = base_opts(max_hold=hold)
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if FULL[0] - dt.timedelta(days=120) <= b.date <= FULL[1]]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got
                          if FULL[0].isoformat() <= t.entry_date <= FULL[1].isoformat())
        out[hold] = trades
        print(f"  hold={hold:2d}  {len(trades):5d} 笔", flush=True)
    return out


def post_drift(bars_by_day: dict, exit_day: dt.date) -> float | None:
    """平仓后 POST_DAYS 个交易日的涨跌幅。"""
    days = bars_by_day["days"]
    if exit_day not in bars_by_day["idx"]:
        return None
    i = bars_by_day["idx"][exit_day]
    j = i + POST_DAYS
    if j >= len(days):
        return None
    c0 = bars_by_day["close"].get(exit_day)
    c1 = bars_by_day["close"].get(days[j])
    if not c0 or not c1:
        return None
    return c1 / c0 - 1


def simulate(per_hold: dict, bars_map: dict, window, lr: float, mode: str,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0,
             rng=None) -> dict:
    start, end = window
    bars_by_code = {}
    for code, bars in bars_map.items():
        days = [b.date for b in bars]
        bars_by_code[code] = {
            "days": days,
            "idx": {d: i for i, d in enumerate(days)},
            "close": {b.date: b.close for b in bars},
        }

    all_dates = sorted({dt.date.fromisoformat(t.entry_date) for h in HOLDS for t in per_hold[h]}
                       | {dt.date.fromisoformat(t.exit_date) for h in HOLDS for t in per_hold[h]})
    all_dates = [d for d in all_dates if start <= d <= end]

    h = START
    cash = cash0
    pos: list = []
    curve: list[float] = []
    h_path: list[tuple[dt.date, float]] = []
    taken = 0

    # 按"当前 h"决定用哪张交易表：这里逐日推进，进入信号以 active hold 的表为准
    for day in all_dates:
        # 1) 到期平仓 + 在线更新
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                cash += p["value"] * (1 + p["trade"].ret_pct / 100.0)
                drift = post_drift(bars_by_code.get(p["trade"].code, {}), p["exit_day"])
                if drift is not None:
                    if mode == "online" and lr > 0:
                        step = lr if drift > THRESH else (-lr if drift < -THRESH else 0.0)
                        h = min(HI, max(LO, h + step))
                    elif mode == "random" and lr > 0:
                        h = min(HI, max(LO, h + (lr if (rng or random).random() > 0.5 else -lr)))
            else:
                still.append(p)
        pos = still

        # 2) 取当天信号（来自 active hold 的表）
        active = nearest(h)
        entries = [t for t in per_hold[active] if t.entry_date == day.isoformat()]
        for t in entries:
            if len(pos) >= slots:
                break
            equity = cash + sum(p["value"] for p in pos)
            size = min(equity * fraction, cash)
            if size < 1000:
                break
            cash -= size
            pos.append({"trade": t, "value": size,
                        "exit_day": dt.date.fromisoformat(t.exit_date)})
            taken += 1

        curve.append(cash + sum(p["value"] for p in pos))
        h_path.append((day, h))

    if not curve:
        return {}
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {
        "trades": taken,
        "total": (curve[-1] / cash0 - 1) * 100,
        "dd": mdd * 100,
        "h_end": round(h, 2),
        "h_min": round(min(v for _, v in h_path), 1),
        "h_max": round(max(v for _, v in h_path), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lr", type=float, default=0.5)
    ap.add_argument("--window", default="all")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    print("重放各持有期…")
    per_hold = replay_by_hold(bars_map, sh, cost)

    # 自检：lr=0 必须复现固定 30 天
    check = simulate(per_hold, bars_map, WINDOWS["TRAIN"], 0.0, "fixed")
    print(f"\n自检（lr=0, TRAIN）：{check['trades']} 笔，收益 {check['total']:+.1f}%，"
          f"回撤 {check['dd']:+.1f}%（应为 +74.1% / −17.4%）")
    if abs(check["total"] - 74.1) > 3:
        print("自检未通过，停止——模拟器有问题，结果不可信")
        return

    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        print(f"\n===== {name} {window[0]}..{window[1]} · 每笔更新 lr={args.lr} =====")
        print(f"{'方案':14s}{'成交':>6s}{'总收益':>9s}{'回撤':>9s}{'h终值':>7s}{'h范围':>12s}")
        for mode, label, lr in (("fixed", "固定 30", 0.0),
                                ("online", "在线学习", args.lr),
                                ("random", "随机方向", args.lr)):
            res = simulate(per_hold, bars_map, window, lr, mode, rng=random.Random(11))
            if not res:
                continue
            span = f"{res['h_min']:.0f}~{res['h_max']:.0f}"
            print(f"{label:14s}{res['trades']:>6d}{res['total']:>+9.1f}{res['dd']:>+9.1f}"
                  f"{res['h_end']:>7.1f}{span:>12s}")


if __name__ == "__main__":
    main()
