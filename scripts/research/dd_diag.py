"""诊断：验证集（及训练集）的回撤到底发生在哪一段、当时仓位多少。

复刻 portfolio.simulate 的主循环，额外记录逐日 (equity, cash, 持仓数, 暴露) 与回撤区间。
用法：python scripts/research/dd_diag.py [--window valid]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _mark, _simulate_with_marks  # noqa: E402
from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"train": TRAIN, "valid": VALID, "stress": STRESS}


def trace(trades, slots=5, fraction=0.2, cash0=50000.0):
    """复刻 simulate，但返回逐日曲线与暴露。"""
    by_entry: dict = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    all_days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                      | {dt.date.fromisoformat(t.exit_date) for t in trades})

    cash = cash0
    open_pos: list = []
    series = []
    for day in all_days:
        still = []
        for pos in open_pos:
            if pos["exit_day"] <= day:
                cash += pos["value"] * (1 + pos["trade"].ret_pct / 100.0)
                pos["exit_value"] = pos["value"] * (1 + pos["trade"].ret_pct / 100.0)
            else:
                still.append(pos)
        open_pos = still

        for trade in by_entry.get(day, []):
            if len(open_pos) >= slots:
                continue
            equity = cash + sum(p["value"] for p in open_pos)
            size = min(equity * fraction, cash)
            if size < 1000:
                continue
            cash -= size
            open_pos.append({"trade": trade, "value": size,
                             "exit_day": dt.date.fromisoformat(trade.exit_date)})

        mv = 0.0
        for p in open_pos:
            ratio = _mark(p["trade"], day)
            mv += p["value"] * (ratio if ratio else 1.0)
        series.append({
            "day": day, "equity": cash + mv, "cash": cash, "mv": mv,
            "n": len(open_pos),
            "codes": [p["trade"].code for p in open_pos],
        })
    return series


def report(name, series) -> None:
    if not series:
        return
    peak = series[0]["equity"]
    peak_day = series[0]["day"]
    mdd = 0.0
    trough = series[0]
    for row in series:
        if row["equity"] > peak:
            peak, peak_day = row["equity"], row["day"]
        dd = row["equity"] / peak - 1
        if dd < mdd:
            mdd, trough = dd, row
    start = series[0]["equity"]
    end = series[-1]["equity"]
    print(f"\n===== {name} =====")
    print(f"  首日 {series[0]['day']} 权益 {start:.0f} → 末日 {series[-1]['day']} {end:.0f}"
          f"（{end / start - 1:+.1%}）")
    print(f"  峰值 {peak:.0f}（{peak_day}）→ 谷底 {trough['equity']:.0f}（{trough['day']}）"
          f"，回撤 {mdd:.1%}")

    # 回撤区间内的暴露
    inside = [r for r in series if peak_day <= r["day"] <= trough["day"]]
    if inside:
        avg_n = sum(r["n"] for r in inside) / len(inside)
        avg_exp = sum(r["mv"] / r["equity"] for r in inside) / len(inside)
        flat = sum(1 for r in inside if r["n"] == 0) / len(inside)
        print(f"  区间内：{len(inside)} 个交易日，平均持仓 {avg_n:.1f} 只，"
              f"平均股票暴露 {avg_exp:.0%}，完全空仓天数占 {flat:.0%}")

    # 回撤最深的 5 天
    worst = sorted(series, key=lambda r: r["equity"])[:1]
    print(f"  谷底当天持仓：{worst[0]['codes']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="valid", choices=list(WINDOWS))
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh, cyber = load_index("sh000001"), load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules, opts = base_rules(), base_opts()

    start, end = WINDOWS[args.window]
    trades = []
    for code, bars in bars_map.items():
        sliced = [b for b in bars if start - WARMUP <= b.date <= end]
        if len(sliced) < 120:
            continue
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades.extend(t for t in got if start.isoformat() <= t.entry_date <= end.isoformat())

    # 与 portfolio.py 一致：先给每笔交易准备盯市序列
    enriched = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
    print(f"{args.window.upper()}：信号 {len(trades)} 笔，模拟成交 {enriched.get('trades', 0)} 笔，"
          f"收益 {enriched.get('total_ret_pct', 0):+.1f}%，回撤 {enriched.get('max_drawdown_pct', 0):+.1f}%")

    series = trace(trades)
    report(args.window.upper(), series)


if __name__ == "__main__":
    main()
