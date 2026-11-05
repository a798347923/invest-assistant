"""弱市过滤器本身的形状实验：把「什么时候算弱市、什么时候解除」当作变量。

背景：2026-11 的斜率闸门实验（G1–G5）证明"等指数 MA20 抬头再买"更差（−1.59%～−4.62%）。
但现行过滤器的**解除条件**比斜率闸门更晚——要求两个指数都 > MA20×(1+band) 才恢复买入。
如果"等均线走好"是买在半山腰，那"晚解除"同样值得怀疑。

本脚本只改「regime 映射」，不改任何入场/离场规则（统一 v12 + 弱市 0 仓位）：

  A 现行（进 −1% / 出 +1%，双指数）
  B 无滞回（进 0 / 出 0）
  C 窄滞回（进 −0.5% / 出 +0.5%）
  D 快释放（进 −1% / 出 0%）        ← 回到 MA20 就恢复
  E 更快释放（进 −1% / 出 −0.5%）   ← 还没站上 MA20 就恢复
  F 需同时破 MA60（指数在 MA60 上方时不判弱）
  G 宽度（样本内站上 MA20 的比例 < 40% 才算弱）
  H 指数弱 或 宽度弱（更严）

判定口径（沿用 AGENTS.md）：两个窗口的组合级随机基准都要不差于现行，且回撤不变差。

用法：python scripts/research/regime.py
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
from study import TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct, _replay_code  # noqa: E402

# ---------------------------------------------------------------- regime 映射


def _rels(sh: list, cyber: list) -> dict:
    """每个交易日：各指数相对自身 MA20 的百分比。"""
    out: dict[dt.date, list[float]] = {}
    for series in (sh, cyber):
        closes: list[float] = []
        for day, close in series:
            closes.append(close)
            if len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20
                if ma20:
                    out.setdefault(day, []).append((close / ma20 - 1) * 100)
    return out


def regime_map(rels: dict, enter: float, release: float) -> dict:
    """state 机：任一指数 < −enter → 弱；全部指数 > +release → 强。

    enter=1.0, release=1.0 即现行逻辑；release < enter 表示"更快解除"。
    """
    result: dict = {}
    state: str | None = None
    for day in sorted(rels):
        rels_today = rels[day]
        if not rels_today:
            continue
        if state is None:
            state = "strong" if all(r >= 0 for r in rels_today) else "weak"
        elif state == "strong":
            if any(r < -enter for r in rels_today):
                state = "weak"
        else:
            if all(r > release for r in rels_today):
                state = "strong"
        result[day] = state == "strong"
    return result


def ma60_conjunction(sh: list, cyber: list, base: dict, enter: float = 1.0) -> dict:
    """只在指数也跌破自身 MA60 时才判弱（MA20 弱但 MA60 上方 → 视为强）。"""
    above60: dict[dt.date, set[str]] = {}
    for name, series in (("sh", sh), ("cyber", cyber)):
        closes: list[float] = []
        for day, close in series:
            closes.append(close)
            if len(closes) >= 60:
                ma60 = sum(closes[-60:]) / 60
                if ma60 and close >= ma60:
                    above60.setdefault(day, set()).add(name)
    out: dict = {}
    for day, strong in base.items():
        names = above60.get(day, set())
        out[day] = strong or len(names) == 2  # 两个指数都在 MA60 上方 → 不判弱
    return out


def breadth_map(bars_map: dict, threshold: float = 0.40) -> dict:
    """宽度：样本内收盘站上自身 MA20 的比例低于 threshold 记为弱。"""
    ratio: dict[dt.date, list[int]] = {}
    for bars in bars_map.values():
        closes: list[float] = []
        for bar in bars:
            closes.append(bar.close)
            if len(closes) < 20:
                continue
            ma20 = sum(closes[-20:]) / 20
            if not ma20:
                continue
            bucket = ratio.setdefault(bar.date, [0, 0])
            bucket[1] += 1
            if bar.close > ma20:
                bucket[0] += 1
    out: dict = {}
    for day, (up, total) in ratio.items():
        if total >= 50:
            out[day] = (up / total) >= threshold
    return out


def combine_or(a: dict, b: dict) -> dict:
    """两者都强才算强（更严的过滤器）。"""
    days = set(a) | set(b)
    return {d: bool(a.get(d, True) and b.get(d, True)) for d in days}


# ---------------------------------------------------------------- 统计


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t.ret_pct for t in trades]
    return {
        "n": len(rets),
        "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg": round(statistics.fmean(rets), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--only", default="", help="只跑这些过滤器（英文前缀，逗号分隔，如 A,H）")
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    rels = _rels(sh, cyber)

    base = regime_map(rels, 1.0, 1.0)
    maps: dict[str, dict] = {
        "A 现行(进-1/出+1)": base,
        "B 无滞回(0/0)": regime_map(rels, 0.0, 0.0),
        "C 窄滞回(-0.5/+0.5)": regime_map(rels, 0.5, 0.5),
        "D 快释放(-1/0)": regime_map(rels, 1.0, 0.0),
        "E 更快释放(-1/-0.5)": regime_map(rels, 1.0, -0.5),
        "F 需同破MA60": ma60_conjunction(sh, cyber, base),
        "G 宽度<40%": breadth_map(bars_map, 0.40),
    }
    maps["H 指数弱或宽度弱"] = combine_or(base, maps["G 宽度<40%"])
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        maps = {k: v for k, v in maps.items() if k.split()[0] in wanted}

    rules = base_rules()  # weak_block="all"
    opts = base_opts()
    results: dict = {}

    for window_name, window in (("TRAIN", TRAIN), ("VALID", VALID)):
        start, end = window
        warm_start = start - WARMUP
        print(f"\n===== {window_name} {start}..{end} =====")
        print(f"{'过滤器':20s}{'强市天占比':>9s}{'笔数':>6s}{'胜率':>7s}{'期望':>8s}"
              f"{'组合收益':>9s}{'随机均值':>9s}{'回撤':>7s}")
        for name, mapping in maps.items():
            trades = []
            for code, bars in bars_map.items():
                sliced = [b for b in bars if warm_start <= b.date <= end]
                if len(sliced) < 120:
                    continue
                got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                                   enhanced_regime=mapping, opts=opts)
                trades.extend(t for t in got
                              if start.isoformat() <= t.entry_date <= end.isoformat())

            day_range = [b.date for b in list(bars_map.values())[0] if start <= b.date <= end]
            strong_share = (
                sum(1 for d in day_range if mapping.get(d, True)) / len(day_range) * 100
                if day_range else 0
            )

            port = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
            rand = [
                _simulate_with_marks(trades, {}, "random", bars_map, 5, 0.2,
                                     rng=__import__("random").Random(1000 + i))["total_ret_pct"]
                for i in range(args.seeds)
            ]
            st = stats(trades)
            rand_sorted = sorted(rand)
            results.setdefault(window_name, {})[name] = {
                "strong_share": round(strong_share, 1), **st,
                "portfolio": port["total_ret_pct"], "dd": port["max_drawdown_pct"],
                "random_mean": round(statistics.fmean(rand), 1),
                "random_std": round(statistics.pstdev(rand), 1),
                "random_p10": rand_sorted[int(0.1 * len(rand_sorted))],
                "random_p90": rand_sorted[int(0.9 * len(rand_sorted))],
            }
            print(f"{name:20s}{strong_share:>8.0f}%{st.get('n', 0):>6d}{st.get('win', 0):>7.1f}"
                  f"{st.get('avg', 0):>+8.2f}{port['total_ret_pct']:>+9.1f}"
                  f"{statistics.fmean(rand):>+9.1f}{port['max_drawdown_pct']:>+7.1f}"
                  f"   σ={statistics.pstdev(rand):>5.1f} p10={rand_sorted[int(0.1 * len(rand_sorted))]:>+6.1f}"
                  f" p90={rand_sorted[int(0.9 * len(rand_sorted))]:>+6.1f}")

    print("\n=== 判定（相对 A 现行）===")
    for window_name in ("TRAIN", "VALID"):
        base_r = results[window_name]["A 现行(进-1/出+1)"]
        print(f"\n{window_name}（A 基线：组合 {base_r['portfolio']:+.1f}% / "
              f"随机 {base_r['random_mean']:+.1f}% / 回撤 {base_r['dd']:+.1f}%）")
        for name, r in results[window_name].items():
            if name.startswith("A "):
                continue
            d_port = r["portfolio"] - base_r["portfolio"]
            d_rand = r["random_mean"] - base_r["random_mean"]
            d_dd = r["dd"] - base_r["dd"]
            print(f"  {name:20s} 组合 {d_port:+7.1f}pp  随机 {d_rand:+7.1f}pp  "
                  f"回撤 {d_dd:+6.1f}pp  笔数 {r['n']:5d}")


if __name__ == "__main__":
    main()
