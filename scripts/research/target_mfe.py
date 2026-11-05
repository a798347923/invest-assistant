"""按个股自己的历史设定止盈目标（MFE 法）。

现行止盈：突破型 = 收盘 ×1.12，回踩型 = 前 20 日高 ×1.02 —— 全市场同一套。
问题（用户提出）：不同行业/波动性的股票，30 天内能涨的幅度差别很大；
12% 对低波动股可能永远够不到（白等一场），对高波动股又太容易达到（早卖飞）。

做法：
1. 对每只股票、每个交易日 t，算它在**未来 H 个交易日内**的最大有利偏移
   MFE_t = max(high[t+1..t+H]) / close[t] − 1；
2. 用**窗口开始之前**的历史（不看未来）取分位数 q（默认中位数），再乘折扣 k；
3. 目标 = clip(q × k, 5%, 35%)，作为该股票在本窗口的统一 target_mult；
4. 其余（入场、止损、持有上限）完全不变，只换目标。

对照：k = 0（等于现行 12%）、0.6、0.8、1.0。

用法：python scripts/research/target_mfe.py [--hold 30]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
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

WINDOWS = {"TRAIN": TRAIN, "VALID": VALID, "STRESS": STRESS}
LOOKBACK_DAYS = 500
MIN_SAMPLES = 30


def mfe_quantile(bars: list, before: dt.date, hold: int, q: float) -> float | None:
    """只用 before 之前的数据：未来 hold 日内的最大有利偏移分位数。"""
    past = [b for b in bars if b.date < before]
    if len(past) < hold + MIN_SAMPLES:
        return None
    vals = []
    for i in range(len(past) - hold - 1):
        base = past[i].close
        if not base or base <= 0:
            continue
        hi = max(b.high for b in past[i + 1:i + 1 + hold])
        vals.append(hi / base - 1)
    if len(vals) < MIN_SAMPLES:
        return None
    vals = vals[-LOOKBACK_DAYS:]
    vals.sort()
    idx = min(len(vals) - 1, max(0, int(q * (len(vals) - 1))))
    return vals[idx]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--quantile", type=float, default=0.5, help="取历史 MFE 的哪个分位")
    args = ap.parse_args()

    cost = _cost_pct(get_settings())
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    rules = base_rules()

    for name, (start, end) in WINDOWS.items():
        targets = {}
        for code, bars in bars_map.items():
            q = mfe_quantile(bars, start, args.hold, args.quantile)
            targets[code] = q

        valid = [v for v in targets.values() if v is not None]
        if valid:
            print(f"\n===== {name} · 历史 MFE 中位 {statistics.median(valid) * 100:.1f}%，"
                  f"覆盖 {len(valid)}/{len(targets)} 只 =====")
        print(f"{'方案':22s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'回撤':>9s}{'目标均值':>9s}")

        for label, k in (("现行 12% 固定", 0.0), ("MFE×0.6", 0.6), ("MFE×0.8", 0.8),
                         ("MFE×1.0", 1.0), ("MFE×1.2", 1.2)):
            trades = []
            for code, bars in bars_map.items():
                sliced = [b for b in bars if start - WARMUP <= b.date <= end]
                if len(sliced) < 120:
                    continue
                if k <= 0:
                    tm = 0.12
                else:
                    q = targets.get(code)
                    tm = 0.12 if q is None else min(0.35, max(0.05, q * k))
                opts = base_opts(max_hold=args.hold, target_mult=tm)
                got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                                   enhanced_regime=regime, opts=opts)
                trades.extend(t for t in got
                              if start.isoformat() <= t.entry_date <= end.isoformat())
            res = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
            if not res.get("trades"):
                continue
            used = [
                (0.12 if k <= 0 else min(0.35, max(0.05, targets[c] * k)))
                for c in targets if targets.get(c) is not None
            ]
            avg_t = statistics.fmean(used) * 100 if used else 12.0
            print(f"{label:22s}{res['trades']:>6d}{res['win_rate']:>7.1f}"
                  f"{res['total_ret_pct']:>+9.1f}{res['max_drawdown_pct']:>+9.1f}{avg_t:>+8.1f}%")


if __name__ == "__main__":
    main()
