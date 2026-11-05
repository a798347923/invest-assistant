"""参数自适应（walk-forward）：用最近的数据挑参数，再往前跑一个季度。

想法（用户提出）：没有一个策略永远正确，那能不能按最近一段时间的表现更新参数？

严格性设计（这是这类实验最容易作弊的地方）：
1. **不看未来**：每个季度开始时，只用 exit_date ≤ 季度首日 的交易来打分（那些交易的
   结果那时已经实现）；
2. **季度内冻结**：选定参数后，本季度所有信号都用它，季度末才重新挑；
3. **对照组**：除了"自适应"，还跑"随机轮换参数"（同样每季度换，但随机挑）。
   自适应必须显著强于随机轮换，否则说明它只是噪声；

参数网格（小、离散、可解释）：
- max_hold: 20 / 30 / 45 交易日
- 弱市判定滞回带 band: 0.5 / 1.0 / 2.0
- 弱市是否开仓: 关闭 / 放开

选参指标：过去 252 天内该配置的**逐笔平均收益**（样本 < 20 笔则视为不可用）。
候选按指标排序，取第一；全部不可用则回退到基准配置。

用法：python scripts/research/adaptive.py [--lookback 252] [--step 63]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _simulate_with_marks  # noqa: E402
from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
FULL = (dt.date(2015, 4, 1), dt.date(2026, 10, 31))

# name -> (max_hold, band, weak_block)
CONFIGS = {
    "hold30_band1.0_block": (30, 1.0, "all"),
    "hold20_band1.0_block": (20, 1.0, "all"),
    "hold45_band1.0_block": (45, 1.0, "all"),
    "hold30_band0.5_block": (30, 0.5, "all"),
    "hold30_band2.0_block": (30, 2.0, "all"),
    "hold30_band1.0_open": (30, 1.0, "none"),  # 危险配置：留作对照，--no-open 可剔除
    "hold20_band2.0_open": (20, 2.0, "none"),
}
BASE = "hold30_band1.0_block"


def replay_all(bars_map, sh, cost: float) -> dict:
    """每个配置重放一次全历史，得到 {config: [trades]}。"""
    regimes = {}
    out = {}
    for name, (hold, band, block) in CONFIGS.items():
        if band not in regimes:
            cyber = load_index("sz399006")
            regimes[band] = _enhanced_regime_map(sh, cyber, band)
        rules = base_rules(weak_block=block)
        opts = base_opts(max_hold=hold)
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if FULL[0] - dt.timedelta(days=120) <= b.date <= FULL[1]]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regimes[band], opts=opts)
            trades.extend(t for t in got
                          if FULL[0].isoformat() <= t.entry_date <= FULL[1].isoformat())
        out[name] = trades
        print(f"  {name:22s} {len(trades):5d} 笔", flush=True)
    return out


def quarters(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """[(季度起, 季度止)]；第一段从窗口起点开始（否则会丢掉窗口头两个月）。"""
    out: list[tuple[dt.date, dt.date]] = []
    cur = start
    while cur <= end:
        month = cur.month + 3
        year = cur.year + (month - 1) // 12
        nxt = dt.date(year, (month - 1) % 12 + 1, 1)
        out.append((cur, min(nxt - dt.timedelta(days=1), end)))
        cur = nxt
    return out


def quarter_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    return [q for q, _ in quarters(start, end)]


def pick(per_config: dict, upto: dt.date, lookback_days: int, min_improve: float = 0.0) -> str:
    """只用 exit_date <= upto 且落在回看窗内的已实现交易打分。"""
    floor = upto - dt.timedelta(days=lookback_days)
    best, best_score = BASE, float("-inf")
    for name, trades in per_config.items():
        done = [t for t in trades
                if t.exit_date <= upto.isoformat() and t.entry_date >= floor.isoformat()]
        if len(done) < 20:
            continue
        score = statistics.fmean(t.ret_pct for t in done)
        if score > best_score:
            best, best_score = name, score
    # 只在明显优于基准时才切换（防止追高近期表现）
    if min_improve > 0 and best != BASE:
        floor_score = None
        for name, trades in per_config.items():
            if name != BASE:
                continue
            done = [t for t in trades
                    if t.exit_date <= upto.isoformat() and t.entry_date >= floor.isoformat()]
            if len(done) >= 20:
                floor_score = statistics.fmean(t.ret_pct for t in done)
        if floor_score is not None and best_score < floor_score * (1 + min_improve):
            best = BASE
    return best


def build_oos(per_config: dict, window, lookback_days: int, mode: str,
              rng=None, min_improve: float = 0.0) -> tuple[list, list]:
    """按季度拼出样本外交易序列，返回 (trades, 选择记录)。"""
    start, end = window
    picks = []
    out = []
    names = list(per_config)
    for q, q_end in quarters(start, end):
        if mode == "adaptive":
            name = pick(per_config, q, lookback_days, min_improve)
        elif mode == "random":
            name = rng.choice(names) if rng else random.choice(names)
        else:
            name = BASE
        picks.append((q, name))
        out.extend(t for t in per_config[name]
                   if q.isoformat() <= t.entry_date <= q_end.isoformat())
    return out, picks


def run(window, per_config, bars_map, lookback: int, seeds: int, min_improve: float = 0.0) -> None:
    name = next(k for k, v in WINDOWS.items() if v == window)
    start, end = window
    print(f"\n===== {name} {start}..{end} · 回看 {lookback} 天 / 季度调参 =====")
    print(f"{'方案':16s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'回撤':>9s}{'随机均值':>10s}")

    for mode, label in (("fixed", "固定基准参数"), ("adaptive", "自适应"), ("random", "随机轮换")):
        trades, picks = build_oos(per_config, window, lookback, mode, min_improve=min_improve,
                                  rng=random.Random(2026) if mode == "random" else None)
        res = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
        if not res.get("trades"):
            print(f"{label:16s}  （无成交）")
            continue
        if mode == "random":
            rand = statistics.fmean(
                _simulate_with_marks(
                    build_oos(per_config, window, lookback, "random", rng=random.Random(500 + i))[0],
                    {}, "chrono", bars_map, 5, 0.2)["total_ret_pct"]
                for i in range(seeds)
            )
        else:
            rand = 0.0
        print(f"{label:16s}{res['trades']:>6d}{res['win_rate']:>7.1f}"
              f"{res['total_ret_pct']:>+9.1f}{res['max_drawdown_pct']:>+9.1f}"
              f"{(f'{rand:+.1f}' if mode == 'random' else '—'):>10s}")

    # 自适应选了哪些参数
    _, picks = build_oos(per_config, window, lookback, "adaptive")
    from collections import Counter

    counts = Counter(n for _, n in picks)
    print("  自适应选参分布：" + "，".join(f"{k}×{v}" for k, v in counts.most_common()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lookback", type=int, default=252, help="回看天数")
    ap.add_argument("--seeds", type=int, default=15)
    ap.add_argument("--window", default="all")
    ap.add_argument("--no-open", action="store_true", help="剔除弱市放开配置")
    ap.add_argument("--min-improve", type=float, default=0.0, help="切换阈值（相对基准逐笔期望的改进比例）")
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh = load_index("sh000001")
    print("重放各配置（全历史）…")
    if args.no_open:
        for k in [k for k, v in CONFIGS.items() if v[2] == "none"]:
            CONFIGS.pop(k)
    per_config = replay_all(bars_map, sh, cost)

    for name, window in WINDOWS.items():
        if args.window != "all" and args.window.upper() != name:
            continue
        run(window, per_config, bars_map, args.lookback, args.seeds, args.min_improve)


if __name__ == "__main__":
    main()
