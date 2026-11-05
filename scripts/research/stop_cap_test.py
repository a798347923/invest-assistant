"""止损距离封顶：回撤归因指向的下一步（用引擎真正执行，不是事后截断）。

归因发现：止损平均亏 −15.69%，18.1% 的交易走止损——止损位挂在"近 10 日低 ×0.98/0.95"，
对一只已经涨离低点的股票，这个位置可能离入场价 15%~20%。

本脚本用引擎真实的 `stop_cap`（止损位不低于 入场价×(1−cap)）重放，因此会**真实改变出场时点、
持有期与槽位占用**，与"事后把收益截到 −8%"完全不同。

对比：无封顶（现行）/ 封顶 10% / 8% / 6%。

用法：python scripts/research/stop_cap_test.py
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from dd_control import simulate  # noqa: E402
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
CAPS = [None, 0.10, 0.08, 0.06]


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

    for cap in CAPS:
        label = "无封顶（现行）" if cap is None else f"封顶 {cap * 100:.0f}%"
        opts = base_opts(max_hold=30)
        if cap is not None:
            opts["stop_cap"] = cap
        trades = []
        stop_rets = []
        for code, bars in merged.items():
            sliced = [b for b in bars if dt.date(2015, 1, 1) <= b.date <= dt.date(2026, 10, 31)]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            got = [t for t in got if "2015-04-01" <= t.entry_date <= "2026-10-31"]
            trades.extend(got)
            stop_rets.extend(t.ret_pct for t in got if t.exit_reason == "止损")
        cells = []
        for _wname, win in WINDOWS.items():
            r = simulate(trades, win)
            cells.append(f"{r['total']:+.1f}%/{r['dd']:+.1f}%")
        stop_avg = statistics.fmean(stop_rets) if stop_rets else 0.0
        stop_pct = len(stop_rets) / len(trades) * 100 if trades else 0
        print(f"{label:16s} 交易 {len(trades):6d}  止损率 {stop_pct:5.1f}%  "
              f"止损均亏 {stop_avg:+6.2f}%  |  " + "  |  ".join(
                  f"{w} {c}" for w, c in zip(WINDOWS, cells, strict=True)))


if __name__ == "__main__":
    main()
