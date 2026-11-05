"""回撤归因：为什么这套策略的回撤是 30%~55%。

分析顺序：
1. 定位最大的几段回撤，看当时的**暴露**与**成交质量**；
2. 拆解亏损来源：是"很多次小亏累积"还是"少数几次大亏"；
3. 反事实：如果单笔亏损封顶 −8% / −5%、仓位减半、槽位减到 3 个，回撤能降到多少。

数据：446 只（原池 + 陌生池合并），这样结论不依赖单一样本。

用法：python scripts/research/dd_analysis.py
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from edge_distribution import load_dir  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
MAIN = os.path.join(RESEARCH, "bars")
HOLDOUT = os.path.join(RESEARCH, "bars_holdout")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}


def simulate(trades, window, fraction=0.2, slots=5, cap=None, cash0=50000.0):
    """返回 (曲线, 每日暴露, 成交列表)。cap 为单笔亏损封顶（百分数，如 8）。"""
    start, end = window
    by_entry = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                  | {dt.date.fromisoformat(t.exit_date) for t in trades})
    days = [d for d in days if start <= d <= end]

    cash = cash0
    pos, curve, exposure, taken = [], [], [], []
    for day in days:
        still = []
        for p in pos:
            if p["exit_day"] <= day:
                r = p["trade"].ret_pct
                if cap is not None:
                    r = max(r, -cap)
                cash += p["value"] * (1 + r / 100.0)
            else:
                still.append(p)
        pos = still
        equity = cash + sum(p["value"] for p in pos)
        for t in by_entry.get(day, []):
            if len(pos) >= slots:
                break
            size = min(equity * fraction, cash)
            if size < 1000:
                break
            cash -= size
            pos.append({"trade": t, "value": size,
                        "exit_day": dt.date.fromisoformat(t.exit_date)})
            taken.append(t)
        mv = sum(p["value"] for p in pos)
        curve.append((day, cash + mv))
        exposure.append((day, mv / (cash + mv) if (cash + mv) > 0 else 0.0))
    return curve, exposure, taken


def episodes(curve, top=3):
    """找出最深的几段回撤（峰 → 谷）。"""
    out = []
    peak, peak_day = curve[0][1], curve[0][0]
    cur = None
    for day, v in curve:
        if v > peak:
            if cur:
                out.append(cur)
                cur = None
            peak, peak_day = v, day
        dd = v / peak - 1 if peak else 0.0
        if dd < 0:
            if cur is None:
                cur = {"peak_day": peak_day, "peak": peak, "trough_day": day, "trough": v,
                       "dd": dd}
            elif dd < cur["dd"]:
                cur.update({"trough_day": day, "trough": v, "dd": dd})
    if cur:
        out.append(cur)
    out.sort(key=lambda e: e["dd"])
    return out[:top]


def main() -> None:
    main_bars = load_dir(MAIN)
    hold_bars = load_dir(HOLDOUT)
    merged = dict(main_bars)
    merged.update({k: v for k, v in hold_bars.items() if k not in merged})
    print(f"合并池 {len(merged)} 只")

    cost = _cost_pct(get_settings())
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()
    opts = base_opts(max_hold=30)

    trades = []
    for code, bars in merged.items():
        sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
        if len(sliced) < 120:
            continue
        got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                           enhanced_regime=regime, opts=opts)
        trades.extend(t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31")
    print(f"信号 {len(trades)} 笔")

    rets = [t.ret_pct for t in trades]
    losses = sorted(r for r in rets if r < 0)
    print("\n===== 逐笔分布 =====")
    print(f"  笔数 {len(rets)}，胜率 {sum(1 for r in rets if r > 0) / len(rets) * 100:.1f}%")
    print(f"  均值 {statistics.fmean(rets):+.2f}%  中位 {statistics.median(rets):+.2f}%")
    print(f"  最差 5% 分位 {statistics.quantiles(rets, n=20)[0]:+.1f}%  最好 5% "
          f"{statistics.quantiles(rets, n=20)[-1]:+.1f}%")
    stops = [t for t in trades if t.exit_reason == "止损"]
    print(f"  止损 {len(stops)} 笔（{len(stops) / len(trades) * 100:.1f}%），"
          f"平均 {statistics.fmean([t.ret_pct for t in stops]):+.2f}%")
    total_loss = -sum(losses)
    worst5 = -sum(losses[: int(len(losses) * 0.05)])
    worst10 = -sum(losses[: int(len(losses) * 0.10)])
    print(f"  亏损总额 {total_loss:.0f}%；最差 5% 交易贡献 {worst5 / total_loss * 100:.0f}%，"
          f"最差 10% 贡献 {worst10 / total_loss * 100:.0f}%")

    for _wname, win in WINDOWS.items():
        curve, exposure, taken = simulate(trades, win)
        eps = episodes(curve)
        print(f"\n===== {_wname} =====")
        for i, e in enumerate(eps, 1):
            inside = [d for d, _ in exposure if e["peak_day"] <= d <= e["trough_day"]]
            expo = [x for d, x in exposure if d in set(inside)]
            opened = [t for t in taken
                      if e["peak_day"].isoformat() <= t.entry_date <= e["trough_day"].isoformat()]
            win_rate = (sum(1 for t in opened if t.ret_pct > 0) / len(opened) * 100
                        if opened else 0)
            print(f"  #{i} {e['peak_day']} → {e['trough_day']}  回撤 {e['dd'] * 100:+.1f}%"
                  f"  时长 {(e['trough_day'] - e['peak_day']).days} 天"
                  f"  平均暴露 {statistics.fmean(expo) * 100 if expo else 0:.0f}%"
                  f"  期间开仓 {len(opened)} 笔（胜率 {win_rate:.0f}%）")

    print("\n===== 反事实：哪些手段真能压回撤 =====")
    print(f"{'方案':28s}{'训练集':>16s}{'验证集':>16s}{'压力窗口':>16s}")
    variants = [
        ("基准 20%×5槽", dict()),
        ("单笔亏损封顶 −8%", dict(cap=8)),
        ("单笔亏损封顶 −5%", dict(cap=5)),
        ("仓位减半 10%×5", dict(fraction=0.1)),
        ("槽位减到 3 个", dict(slots=3)),
        ("封顶−8% + 3 槽", dict(cap=8, slots=3)),
    ]
    for label, kw in variants:
        cells = []
        for _wname, win in WINDOWS.items():
            curve, _, _ = simulate(trades, win, **kw)
            peak, mdd = curve[0][1], 0.0
            for _, v in curve:
                peak = max(peak, v)
                mdd = min(mdd, v / peak - 1)
            total = (curve[-1][1] / 50000 - 1) * 100
            cells.append(f"{total:+.1f}%/{mdd * 100:+.1f}%")
        print(f"{label:28s}" + "".join(f"{c:>16s}" for c in cells))


if __name__ == "__main__":
    main()
