"""在"高收益高回撤"的 MFE 目标之上，叠加控回撤算法。

背景：MFE×0.6 使训练集 +74→+112、胜率 66%→72%，但验证集回撤 −33→−45。
这里在**组合层**加控回撤机制（交易不变，只改仓位大小），看能否用较小的收益代价换回回撤。

三种机制：
1. `dd_throttle`：按当前回撤缩放仓位——回撤 <5% 满仓；>12% 半仓；中间线性。
2. `vol_target`：按市场 20 日波动率缩放——波动高时减仓（目标 = 历史中位波动）。
3. `both`：两者相乘。

自检：机制关闭时必须复现固定 12% 的 +74.1%（训练集）。

用法：python scripts/research/dd_control.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402
from target_mfe import mfe_quantile  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def market_vol(sh: list, win: int = 20) -> dict:
    """市场 20 日已实现波动率（年化前，日频标准差）。"""
    closes = [c for _, c in sh]
    days = [d for d, _ in sh]
    rets = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    out = {}
    for i, d in enumerate(days):
        if i >= win:
            out[d] = statistics.pstdev(rets[i - win + 1:i + 1])
    return out


def simulate(trades, window, control: str = "none", vol: dict | None = None,
             vol_target: float = 0.0, slots: int = 5, fraction: float = 0.2,
             cash0: float = 50000.0) -> dict:
    start, end = window
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                  | {dt.date.fromisoformat(t.exit_date) for t in trades})
    days = [d for d in days if start <= d <= end]

    cash = cash0
    pos: list = []
    curve: list[float] = []
    peak = cash0

    for day in days:
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

        mult = 1.0
        if control in ("dd_throttle", "both"):
            if dd <= -0.12:
                mult *= 0.5
            elif dd >= -0.05:
                mult *= 1.0
            else:
                mult *= 1.0 - 0.5 * (abs(dd) - 0.05) / 0.07
        if control in ("vol_target", "both") and vol and vol_target > 0:
            v = vol.get(day)
            if v and v > 0:
                mult *= min(1.5, max(0.5, vol_target / v))

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
    calmar = annual / abs(mdd * 100) if mdd < 0 else 0.0
    return {"total": total, "dd": mdd * 100, "annual": annual, "calmar": calmar}


def replay(window, bars_map, sh, regime, rules, cost, target_mode: str, hold: int):
    start, end = WINDOWS[window][0], WINDOWS[window][1]
    targets = {}
    for code, bars in bars_map.items():
        targets[code] = mfe_quantile(bars, start, hold, 0.5)
    trades = []
    for code, bars in bars_map.items():
        sliced = [b for b in bars if start - WARMUP <= b.date <= end]
        if len(sliced) < 120:
            continue
        if target_mode == "fixed":
            tm = 0.12
        else:
            k = float(target_mode)
            q = targets.get(code)
            tm = 0.12 if q is None else min(0.35, max(0.05, q * k))
        opts = base_opts(max_hold=hold, target_mult=tm)
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades.extend(t for t in got
                      if start.isoformat() <= t.entry_date <= end.isoformat())
    return trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=30)
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    vol = market_vol(sh)
    vtarget = statistics.median(vol.values())

    for name, (start, end) in WINDOWS.items():
        print(f"\n===== {name} {start}..{end} =====")
        print(f"{'目标':10s}{'控回撤':14s}{'总收益':>9s}{'回撤':>9s}{'年化':>8s}{'Calmar':>8s}")
        for tmode, tlabel in (("fixed", "固定12%"), ("0.6", "MFE×0.6"), ("0.8", "MFE×0.8")):
            trades = replay(name, bars_map, sh, regime, rules, cost, tmode, args.hold)
            for ctrl, clabel in (("none", "无"), ("dd_throttle", "回撤节流"),
                                 ("vol_target", "波动目标"), ("both", "两者")):
                res = simulate(trades, (start, end), ctrl, vol, vtarget)
                if not res:
                    continue
                print(f"{tlabel:10s}{clabel:14s}{res['total']:>+9.1f}{res['dd']:>+9.1f}"
                      f"{res['annual']:>+8.1f}{res['calmar']:>8.2f}")


if __name__ == "__main__":
    main()
