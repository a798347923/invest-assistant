"""弱市时对**已有持仓**的处理：清仓 / 换宽基 / 换国债。

背景：验证集 −33% 的回撤发生在 2022-07~2023-09，当时平均股票暴露 66%——
现行规则只管"弱市不新开仓"，不管已有持仓。这个实验补上那一半。

实现方式（关键）：**不重写撮合**。把"换仓"表达为收益替换，再交给已审计的
`portfolio._simulate_with_marks`：

- 对每笔交易，找持有期内第一个"前一日判定为弱"的交易日 d；
- 股票段收益 = 从入场价到 d 日收盘（扣卖出成本）；
- 剩余段（d → 原定 exit_date）按所选标的的收益替代：
    B 清仓 → 0
    C 换宽基 510300 → ETF 区间收益
    D 换国债 511260 → ETF 区间收益
- 合成 ret = (1+r_stock)*(1+r_rest) - 1

保守处：槽位占用仍按原 exit_date（换仓后仍占坑），对 B/C/D 都不利，因此若它们仍更好，
结论更可信。

用法：python scripts/research/weak_swap.py [--window all]
"""

from __future__ import annotations

import argparse
import copy
import csv
import datetime as dt
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _simulate_with_marks  # noqa: E402
from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
ETF_DIR = os.path.join(RESEARCH, "etf_bars")
WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
STOCK_SELL_COST = 0.0007  # 卖出：佣金+印花税+滑点（约）
ETF_COST = 0.0002          # ETF 往返


def load_etf(code: str) -> dict:
    path = os.path.join(ETF_DIR, f"{code}.csv")
    out: dict = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                out[dt.date.fromisoformat(row["date"])] = float(row["close"])
            except (TypeError, ValueError):
                continue
    return out


def etf_return(etf: dict, d0: dt.date, d1: dt.date) -> float:
    """ETF 在 [d0, d1] 的收益（两端取最近可得收盘）。"""
    if not etf:
        return 0.0
    p0 = etf.get(d0)
    p1 = etf.get(d1)
    if p0 is None or p1 is None:
        return 0.0
    return p1 / p0 - 1 - ETF_COST


def retime(trades, bars_map: dict, regime: dict, etf: dict, mode: str):
    """把"持有期内转弱"的段落替换掉，返回新交易列表（原对象不改）。"""
    out = []
    for t in trades:
        entry = dt.date.fromisoformat(t.entry_date)
        exit_day = dt.date.fromisoformat(t.exit_date)
        if mode == "none":
            out.append(t)
            continue

        bars = bars_map.get(t.code) or []
        held = [b for b in bars if entry <= b.date <= exit_day]
        # 前一日判定为弱 → 当天才可动作（无前视）
        switch_day = None
        prev = None
        for b in held:
            if prev is not None and regime.get(prev) is False:
                switch_day = b
                break
            prev = b.date
        if switch_day is None:
            out.append(t)
            continue

        ep = t.entry_price
        if not ep or ep <= 0:
            out.append(t)
            continue
        r_stock = switch_day.close / ep - 1 - STOCK_SELL_COST
        r_rest = 0.0
        if mode == "swap_300":
            r_rest = etf_return(etf, switch_day.date, exit_day)
        elif mode == "swap_bond":
            r_rest = etf_return(etf, switch_day.date, exit_day)

        dup = copy.copy(t)
        dup.ret_pct = round(((1 + r_stock) * (1 + r_rest) - 1) * 100, 4)
        out.append(dup)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="all", choices=["all", "train", "valid", "stress"])
    ap.add_argument("--seeds", type=int, default=20)
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh, cyber = load_index("sh000001"), load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules, opts = base_rules(), base_opts()
    etf300, etf_bond = load_etf("510300"), load_etf("511260")
    if not etf300 or not etf_bond:
        print("缺少 ETF 行情（data/research/etf_bars），请先跑 etf_study.py --seeds 1")
        return

    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        start, end = window
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if start - WARMUP <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got if start.isoformat() <= t.entry_date <= end.isoformat())

        print(f"\n===== {name} {start}..{end} · 信号 {len(trades)} 笔 =====")
        print(f"{'方案':16s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'回撤':>9s}{'随机均值':>10s}")
        for mode, label, etf in (
            ("none", "A 现行", {}),
            ("liquidate", "B 弱市清仓", {}),
            ("swap_300", "C 换宽基510300", etf300),
            ("swap_bond", "D 换国债511260", etf_bond),
        ):
            mod = retime(trades, bars_map, regime, etf, mode)
            res = _simulate_with_marks(mod, {}, "chrono", bars_map, 5, 0.2)
            if not res.get("trades"):
                print(f"{label:16s}  （无成交）")
                continue
            rand = statistics.fmean(
                _simulate_with_marks(mod, {}, "random", bars_map, 5, 0.2,
                                     rng=random.Random(900 + i))["total_ret_pct"]
                for i in range(args.seeds)
            )
            print(f"{label:16s}{res['trades']:>6d}{res['win_rate']:>7.1f}"
                  f"{res['total_ret_pct']:>+9.1f}{res['max_drawdown_pct']:>+9.1f}"
                  f"{rand:>+10.1f}")


if __name__ == "__main__":
    main()
