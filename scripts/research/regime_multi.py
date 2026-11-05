"""弱市过滤器：多指数确认（2/3/4 个指数 × 任一/多数/全部破位）。

问题：现行只看上证 + 创业板两个指数。加入沪深300、中证500 能不能更早/更准地识别弱市？
规则统一：band=1%，任一指数 < MA20×0.99 → 弱；恢复需要「足够多」指数 > MA20×1.01。

  A2_any   2 指数（现行）：1 个破位即弱，2 个都站上才恢复
  A2_all   2 指数：2 个都破位才弱，1 个站上即恢复（更松）
  I3_any   3 指数（+沪深300）：1 个破位即弱，3 个都站上才恢复
  I3_maj   3 指数：2 个破位才弱，2 个站上才恢复
  I3_all   3 指数：3 个都破位才弱，1 个站上即恢复
  I4_maj   4 指数（+中证500）：3 个破位才弱，2 个站上才恢复
  I4_all   4 指数：4 个都破位才弱，1 个站上即恢复

判定：训练/验证双窗口的组合级随机基准（30 次）都不差于 A2_any。

用法：python scripts/research/regime_multi.py --seeds 30
"""

from __future__ import annotations

import argparse
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _simulate_with_marks  # noqa: E402
from study import TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct, _replay_code  # noqa: E402

SYMBOLS = {
    "sh000001": "上证",
    "sz399006": "创业板",
    "sh000300": "沪深300",
    "sh000905": "中证500",
}


def rel_series(series: list) -> dict:
    """每日：收盘相对自身 MA20 的百分比。"""
    out: dict = {}
    closes: list[float] = []
    for day, close in series:
        closes.append(close)
        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20
            if ma20:
                out[day] = (close / ma20 - 1) * 100
    return out


def build_map(per_index: list[dict], n_weak: int, n_strong: int, band: float = 1.0) -> dict:
    days = sorted(set().union(*[set(d) for d in per_index]))
    out: dict = {}
    state: str | None = None
    for day in days:
        rels = [d[day] for d in per_index if day in d]
        if len(rels) < len(per_index):
            continue  # 数据不齐的日子不判（回放侧默认按强市处理）
        weak_votes = sum(1 for r in rels if r < -band)
        strong_votes = sum(1 for r in rels if r > band)
        if state is None:
            state = "strong" if weak_votes == 0 else "weak"
        elif state == "strong":
            if weak_votes >= n_weak:
                state = "weak"
        elif strong_votes >= n_strong:
            state = "strong"
        out[day] = state == "strong"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh = load_index("sh000001")
    rels = {code: rel_series(load_index(code)) for code in SYMBOLS}

    maps = {
        "A2_any(现行)": build_map([rels["sh000001"], rels["sz399006"]], 1, 2),
        "A2_all": build_map([rels["sh000001"], rels["sz399006"]], 2, 1),
        "I3_any": build_map([rels["sh000001"], rels["sz399006"], rels["sh000300"]], 1, 3),
        "I3_maj": build_map([rels["sh000001"], rels["sz399006"], rels["sh000300"]], 2, 2),
        "I3_all": build_map([rels["sh000001"], rels["sz399006"], rels["sh000300"]], 3, 1),
        "I4_maj": build_map([rels["sh000001"], rels["sz399006"], rels["sh000300"],
                             rels["sh000905"]], 3, 2),
        "I4_all": build_map([rels["sh000001"], rels["sz399006"], rels["sh000300"],
                             rels["sh000905"]], 4, 1),
    }
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        maps = {k: v for k, v in maps.items() if k.split("_")[0] in wanted or k in wanted}

    rules = base_rules()
    opts = base_opts()
    results: dict = {}

    for window_name, window in (("TRAIN", TRAIN), ("VALID", VALID)):
        start, end = window
        warm_start = start - WARMUP
        print(f"\n===== {window_name} {start}..{end} =====")
        print(f"{'过滤器':16s}{'强市天':>7s}{'笔数':>6s}{'胜率':>7s}{'期望':>8s}"
              f"{'组合':>9s}{'随机均值':>9s}{'σ':>6s}{'回撤':>8s}")
        calendar = [d for d, _ in sh if start <= d <= end]
        for name, mapping in maps.items():
            trades = []
            for code, bars in bars_map.items():
                sliced = [b for b in bars if warm_start <= b.date <= end]
                if len(sliced) < 120:
                    continue
                got = _replay_code(code, sliced, sh, cost, rules,
                                   0.98, 0.95, enhanced_regime=mapping, opts=opts)
                trades.extend(t for t in got
                              if start.isoformat() <= t.entry_date <= end.isoformat())
            strong_share = sum(1 for d in calendar if mapping.get(d, True)) / len(calendar) * 100
            port = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
            rand = [_simulate_with_marks(trades, {}, "random", bars_map, 5, 0.2,
                                         rng=random.Random(2000 + i))["total_ret_pct"]
                    for i in range(args.seeds)]
            rets = [t.ret_pct for t in trades] or [0]
            results.setdefault(window_name, {})[name] = {
                "strong_share": round(strong_share, 1), "n": len(trades),
                "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
                "avg": round(statistics.fmean(rets), 3),
                "portfolio": port["total_ret_pct"], "dd": port["max_drawdown_pct"],
                "random_mean": round(statistics.fmean(rand), 1),
                "random_std": round(statistics.pstdev(rand), 1),
            }
            print(f"{name:16s}{strong_share:>6.0f}%{len(trades):>6d}"
                  f"{results[window_name][name]['win']:>7.1f}{statistics.fmean(rets):>+8.2f}"
                  f"{port['total_ret_pct']:>+9.1f}{statistics.fmean(rand):>+9.1f}"
                  f"{statistics.pstdev(rand):>6.1f}{port['max_drawdown_pct']:>+8.1f}")

    print("\n=== 相对 A2_any(现行) ===")
    for window_name in ("TRAIN", "VALID"):
        base = results[window_name].get("A2_any(现行)")
        if not base:
            continue
        print(f"\n{window_name} 基线：组合 {base['portfolio']:+.1f}% / 随机 {base['random_mean']:+.1f}% "
              f"(σ {base['random_std']:.1f}) / 回撤 {base['dd']:+.1f}%")
        for name, r in results[window_name].items():
            if name.startswith("A2_any"):
                continue
            print(f"  {name:14s} 组合 {r['portfolio'] - base['portfolio']:+7.1f}pp  "
                  f"随机 {r['random_mean'] - base['random_mean']:+7.1f}pp  "
                  f"回撤 {r['dd'] - base['dd']:+6.1f}pp  笔数 {r['n']:5d}")


if __name__ == "__main__":
    main()
