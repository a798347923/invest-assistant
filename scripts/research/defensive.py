"""方向二：弱市「换赛道」——空仓 vs 停泊到防守型 ETF。

现行规则：弱市 0 仓位（现金空转）。若把闲置现金在弱市停泊到低相关资产（国债/黄金/红利/货币），
组合收益与回撤会怎样？本脚本用**同一批股票交易**，只改"弱市里的现金做什么"。

口径：
- 股票侧完全沿用 v12（弱市暂停买入，只改停泊收益）；
- 停泊只作用于**闲置现金**：弱市日按该 ETF 的当日收盘涨幅计息，强市日不计（现金待命）；
- 每日推进，逐日盯市；停泊换仓成本忽略（3 年里切换约 10–20 次，ETF 佣金 ~0.02%，影响 <0.5pp）。

用法：python scripts/research/defensive.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from regime_multi import build_map, rel_series  # noqa: E402
from study import TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct, _replay_code  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
ETF_DIR = os.path.join(RESEARCH, "etf")

PARKING = {
    "空仓(现金)": "",
    "国债ETF": "sh511010",
    "十年国债ETF": "sh511260",
    "黄金ETF": "sh518880",
    "红利ETF": "sh510880",
    "货币ETF": "sh511990",
}


def load_etf(symbol: str) -> dict:
    """ETF 日收益率（收盘对收盘）。"""
    path = os.path.join(ETF_DIR, f"{symbol}.csv")
    if not os.path.exists(path):
        return {}
    closes: list[tuple[dt.date, float]] = []
    with open(path, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            day, close = line.strip().split(",")[:2]
            closes.append((dt.date.fromisoformat(day), float(close)))
    out: dict = {}
    for i in range(1, len(closes)):
        prev = closes[i - 1][1]
        if prev:
            out[closes[i][0]] = closes[i][1] / prev - 1
    return out


def simulate(trades: list, bars_map: dict, calendar: list, regime: dict,
             park_ret: dict, slots: int = 5, fraction: float = 0.2,
             cash0: float = 50000.0) -> dict:
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    closes: dict = {}
    for code, bars in bars_map.items():
        closes[code] = {b.date: b.close for b in bars}

    cash = cash0
    open_pos: list[dict] = []
    curve: list[tuple[dt.date, float]] = []
    taken = 0
    parked_days = 0
    prev_weak = False  # 前一日收盘时的市场状态（决定今日是否停泊）

    for day in calendar:
        # 1) 平仓
        still = []
        for pos in open_pos:
            if pos["exit_day"] <= day:
                cash += pos["value"] * (1 + pos["trade"].ret_pct / 100.0)
            else:
                still.append(pos)
        open_pos = still

        # 2) 开仓（仅强市日会有候选，因为弱市已被 weak_block=all 挡掉）
        for trade in by_entry.get(day, []):
            if len(open_pos) >= slots:
                break
            equity = cash + sum(
                p["value"] * (closes[p["trade"].code].get(day, p["entry"]) / p["entry"])
                for p in open_pos
            )
            size = min(equity * fraction, cash)
            if size < 1000:
                continue
            cash -= size
            open_pos.append({
                "trade": trade, "value": size,
                "entry": trade.entry_price,
                "exit_day": dt.date.fromisoformat(trade.exit_date),
            })
            taken += 1

        # 3) 弱市停泊：闲置现金按 ETF 当日涨幅计息
        #    关键：状态在「前一日收盘」才可知，今日才能停泊（避免用当日收盘状态赚当日涨幅的前视偏差）
        if prev_weak and park_ret:
            rate = park_ret.get(day, 0.0)
            cash *= (1 + rate)
            parked_days += 1
        prev_weak = not regime.get(day, True)

        # 4) 盯市
        mv = sum(
            p["value"] * (closes[p["trade"].code].get(day, p["entry"]) / p["entry"])
            for p in open_pos
        )
        curve.append((day, cash + mv))

    if not curve:
        return {"trades": taken}
    final = curve[-1][1]
    peak, mdd = curve[0][1], 0.0
    for _, value in curve:
        peak = max(peak, value)
        mdd = min(mdd, value / peak - 1)
    days = (curve[-1][0] - curve[0][0]).days or 1
    return {
        "trades": taken,
        "final": round(final, 0),
        "total_ret_pct": round((final / cash0 - 1) * 100, 1),
        "annual_pct": round(((final / cash0) ** (365 / days) - 1) * 100, 1),
        "max_drawdown_pct": round(mdd * 100, 1),
        "parked_days": parked_days,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slots", type=int, default=5)
    ap.add_argument("--fraction", type=float, default=0.2)
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = build_map([rel_series(sh), rel_series(cyber)], 1, 2)
    rules = base_rules()
    opts = base_opts()
    parks = {name: (load_etf(sym) if sym else {}) for name, sym in PARKING.items()}

    for window_name, window in (("TRAIN", TRAIN), ("VALID", VALID)):
        start, end = window
        warm_start = start - WARMUP
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if warm_start <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got
                          if start.isoformat() <= t.entry_date <= end.isoformat())

        calendar = [d for d, _ in sh if start <= d <= end]
        weak_days = sum(1 for d in calendar if not regime.get(d, True))
        print(f"\n===== {window_name} {start}..{end} · {len(trades)} 笔信号 · "
              f"弱市 {weak_days}/{len(calendar)} 天 ({weak_days / len(calendar) * 100:.0f}%) =====")
        print(f"{'弱市停泊标的':14s}{'总收益':>9s}{'年化':>8s}{'最大回撤':>9s}{'收益/回撤':>10s}")

        baseline = None
        for name, park_ret in parks.items():
            res = simulate(trades, bars_map, calendar, regime, park_ret,
                           args.slots, args.fraction)
            calmar = (res["annual_pct"] / abs(res["max_drawdown_pct"])
                      if res["max_drawdown_pct"] else 0.0)
            if baseline is None:
                baseline = res
            delta = res["total_ret_pct"] - baseline["total_ret_pct"]
            print(f"{name:14s}{res['total_ret_pct']:>+9.1f}{res['annual_pct']:>+8.1f}"
                  f"{res['max_drawdown_pct']:>+9.1f}{calmar:>10.2f}"
                  f"   (Δ {delta:+.1f}pp)")

        # 混合停泊：国债打底 + 黄金增强（按日再平衡近似）
        bond, gold = parks["十年国债ETF"], parks["黄金ETF"]
        for label, w_bond in (("债7:金3", 0.7), ("债5:金5", 0.5)):
            blended: dict = {}
            for day in set(bond) | set(gold):
                blended[day] = w_bond * bond.get(day, 0.0) + (1 - w_bond) * gold.get(day, 0.0)
            res = simulate(trades, bars_map, calendar, regime, blended,
                           args.slots, args.fraction)
            calmar = (res["annual_pct"] / abs(res["max_drawdown_pct"])
                      if res["max_drawdown_pct"] else 0.0)
            delta = res["total_ret_pct"] - baseline["total_ret_pct"]
            print(f"{label:14s}{res['total_ret_pct']:>+9.1f}{res['annual_pct']:>+8.1f}"
                  f"{res['max_drawdown_pct']:>+9.1f}{calmar:>10.2f}"
                  f"   (Δ {delta:+.1f}pp)")


if __name__ == "__main__":
    main()
