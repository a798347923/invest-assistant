"""弱市盈利研究台：诊断 + 候选方法在「训练集 / 验证集」上的分离验证。

窗口定义（用户口径）：
- 训练集 TRAIN：2023-11-01 .. 2026-10-31（近 3 年，用于开发方法）
- 验证集 VALID：2019-11-01 .. 2023-10-31（3 年前，仅用于验收；开发期不看）

纪律：
1. 复用线上同一份 `_replay_code`（确定性骨架：次日开盘成交、机械止损/止盈/超时），
   不为研究另写一套撮合，避免"研究赚钱、实盘不同"；
2. 每个窗口都向前多取 120 个自然日做 MA60 预热，只统计入口日期落在窗口内的交易；
3. 候选方法只有在**验证集**上同时满足「期望>0、胜率≥50%、样本≥40、t≥2」才建议采纳；
4. 强市子集不得因改进而变差（期望下降 >0.05pp 或胜率下降 >1pp 即否决）。

用法：
    python scripts/research/study.py --set diagnose   # 弱市到底什么样
    python scripts/research/study.py --set full       # 弱市 + 强市候选
    python scripts/research/study.py --set reversal   # 弱市反转（均值回归）
    python scripts/research/study.py --set gates      # 指数 MA20 斜率闸门
    python scripts/research/study.py --set weak60     # 弱市按指数 MA60 拆分
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource import Bar  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    DEPLOYED_OPTS,
    V7_RULES,
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RESEARCH = os.path.join(ROOT, "data", "research")
BAR_DIR = os.path.join(RESEARCH, "bars")
INDEX_DIR = os.path.join(RESEARCH, "index")

TRAIN = (dt.date(2023, 11, 1), dt.date(2026, 10, 31))
VALID = (dt.date(2019, 11, 1), dt.date(2023, 10, 31))
STRESS = (dt.date(2015, 4, 1), dt.date(2019, 10, 31))  # 压力窗口：2015 股灾 + 2018 熊市
WARMUP = dt.timedelta(days=120)

STRONG = "双指数强市"
WEAK = "双指数弱市"


# ---------------------------------------------------------------- 数据

def load_bars() -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for name in sorted(os.listdir(BAR_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        bars: list[Bar] = []
        with open(os.path.join(BAR_DIR, name), encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 6:
                    continue
                try:
                    bars.append(Bar(
                        code=code,
                        date=dt.date.fromisoformat(parts[0]),
                        open=float(parts[1]), high=float(parts[2]),
                        low=float(parts[3]), close=float(parts[4]),
                        volume=float(parts[5] or 0), amount=0.0, adjust="qfq",
                    ))
                except ValueError:
                    continue
        if len(bars) >= 120:
            out[code] = bars
    return out


def load_index(symbol: str) -> list[tuple[dt.date, float]]:
    path = os.path.join(INDEX_DIR, f"{symbol}.csv")
    series: list[tuple[dt.date, float]] = []
    with open(path, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            day, close = line.strip().split(",")[:2]
            series.append((dt.date.fromisoformat(day), float(close)))
    return series


# ---------------------------------------------------------------- 变体

def base_rules(**over) -> dict:
    rules = dict(V7_RULES)
    rules.update(over)
    return rules


def base_opts(**over) -> dict:
    opts = dict(DEPLOYED_OPTS)
    opts.update(over)
    return opts


def variants_diagnose() -> dict[str, dict]:
    """诊断：现行（弱市不买） vs 弱市放开，用来看弱市到底什么样。"""
    return {
        "v12_现行(弱市不买)": {"rules": base_rules(), "opts": base_opts()},
        "d1_弱市全放开": {"rules": base_rules(weak_block="none"), "opts": base_opts()},
        "d2_弱市只做回踩": {"rules": base_rules(weak_block="breakout_only"), "opts": base_opts()},
        "d3_弱市只做突破": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="breakout"),
        },
    }


def variants_reversal() -> dict[str, dict]:
    """弱市反转（均值回归）候选：与追动量相反的方向，专治弱市期望为负。"""
    def rev(**over) -> dict:
        base = dict(allow_setup="reversal", enable_reversal=True)
        base.update(over)
        return base

    return {
        "v12_现行(弱市不买)": {"rules": base_rules(), "opts": base_opts()},
        "d1_弱市全放开": {"rules": base_rules(weak_block="none"), "opts": base_opts()},
        # 弱市专用反转（强市保持现行）
        "R1_弱市反转5-4-6-8": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": rev(rev_drop_pct=0.05, rev_stop_pct=0.04, rev_target=0.06,
                             max_hold_reversal=8),
        },
        "R2_弱市反转8-4-8-10": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": rev(rev_drop_pct=0.08, rev_stop_pct=0.04, rev_target=0.08,
                             max_hold_reversal=10),
        },
        "R3_弱市反转5-3-5-5": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": rev(rev_drop_pct=0.05, rev_stop_pct=0.03, rev_target=0.05,
                             max_hold_reversal=5),
        },
        "R4_弱市反转3-4-6-8": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": rev(rev_drop_pct=0.03, rev_stop_pct=0.04, rev_target=0.06,
                             max_hold_reversal=8),
        },
        # 全市场反转（检验形态本身有没有 edge）
        "R5_全市场反转5-4-6-8": {
            "rules": base_rules(weak_block="none"),
            "opts": rev(rev_drop_pct=0.05, rev_stop_pct=0.04, rev_target=0.06,
                       max_hold_reversal=8),
        },
    }


def variants_gates() -> dict[str, dict]:
    """结构性闸门：用指数 MA20 斜率区分「弱市下跌中继」与「弱市筑底/修复」。"""
    rev = dict(allow_setup="reversal", enable_reversal=True, rev_drop_pct=0.05,
               rev_stop_pct=0.04, rev_target=0.06, max_hold_reversal=8)
    return {
        "v12_现行(弱市不买)": {"rules": base_rules(), "opts": base_opts()},
        "d1_弱市全放开": {"rules": base_rules(weak_block="none"), "opts": base_opts()},
        "G1_弱市突破+斜率≥0": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="breakout", index_slope_min=0.0),
        },
        "G2_弱市突破+斜率≥0.5": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="breakout", index_slope_min=0.5),
        },
        "G3_弱市反转+斜率≥0": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": {**rev, "index_slope_min": 0.0},
        },
        "G4_弱市反转+斜率≥0.5": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": {**rev, "index_slope_min": 0.5},
        },
        "G5_全市场反转+斜率≥0.5": {
            "rules": base_rules(weak_block="none"),
            "opts": {**rev, "index_slope_min": 0.5},
        },
    }


def variants_weak60() -> dict[str, dict]:
    """把弱市拆成「牛市回调（指数仍在 MA60 上）」与「熊市中继（MA60 下）」。"""
    rev = dict(allow_setup="reversal", enable_reversal=True, rev_drop_pct=0.05,
               rev_stop_pct=0.04, rev_target=0.06, max_hold_reversal=8)
    return {
        "v12_现行(弱市不买)": {"rules": base_rules(), "opts": base_opts()},
        "d1_弱市全放开": {"rules": base_rules(weak_block="none"), "opts": base_opts()},
        "M1_弱市+指数在MA60上": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="both", index_ma60="above"),
        },
        "M2_弱市+指数在MA60下": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="both", index_ma60="below"),
        },
        "M3_弱市+MA60上+只突破": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="breakout", index_ma60="above"),
        },
        "M4_弱市+MA60上+反转": {
            "rules": base_rules(weak_block="none"), "opts": base_opts(),
            "opts_weak": {**rev, "index_ma60": "above"},
        },
    }


def variants_full() -> dict[str, dict]:
    """候选方法：弱市（W*）与强市（S*）改进，全部只在训练集上挑选。"""
    weak_fast = base_opts(fixed_stop_pct=5.0, target_mult=0.06, max_hold=10)
    return {
        # ---- 基线
        "v12_现行(弱市不买)": {"rules": base_rules(), "opts": base_opts()},
        "d1_弱市全放开": {"rules": base_rules(weak_block="none"), "opts": base_opts()},
        "d2_弱市只做回踩": {"rules": base_rules(weak_block="breakout_only"), "opts": base_opts()},
        # ---- 弱市候选
        "W1_回踩+相对强度": {
            "rules": base_rules(weak_block="breakout_only"),
            "opts": base_opts(), "opts_weak": base_opts(rs_min=0.05),
        },
        "W2_回踩+快进快出": {
            "rules": base_rules(weak_block="breakout_only"),
            "opts": base_opts(), "opts_weak": weak_fast,
        },
        "W3_回踩+RS+快出": {
            "rules": base_rules(weak_block="breakout_only"),
            "opts": base_opts(), "opts_weak": base_opts(rs_min=0.05, fixed_stop_pct=5.0,
                                                         target_mult=0.06, max_hold=10),
        },
        "W4_弱市收紧止损": {
            "rules": base_rules(weak_block="none"),
            "opts": base_opts(), "opts_weak": base_opts(fixed_stop_pct=4.0),
        },
        "W5_弱市低吸(深回踩)": {
            "rules": base_rules(weak_block="none"),
            "opts": base_opts(), "opts_weak": base_opts(fixed_stop_pct=5.0, target_mult=0.08,
                                                         max_hold=15, rs_min=0.0),
        },
        "W6_弱市只做突破+RS": {
            "rules": base_rules(weak_block="none"),
            "opts": base_opts(),
            "opts_weak": base_opts(allow_setup="breakout", rs_min=0.08),
        },
        # ---- 强市候选
        "S1_目标15%": {"rules": base_rules(), "opts": base_opts(target_mult=0.15)},
        "S2_目标20%": {"rules": base_rules(), "opts": base_opts(target_mult=0.20)},
        "S3_减半点8%": {"rules": base_rules(), "opts": base_opts(partial_at=0.08, target_mult=0.15)},
        "S4_持有40日": {"rules": base_rules(), "opts": base_opts(max_hold=40)},
        "S5_ATR移动止盈": {
            "rules": base_rules(), "opts": base_opts(trail_atr=2.0, activate_pct=2.0),
        },
    }


# ---------------------------------------------------------------- 统计

def stat(trades: list) -> dict | None:
    if not trades:
        return None
    rets = [t.ret_pct for t in trades]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    std = var ** 0.5
    wins = sum(1 for r in rets if r > 0)
    gross_win = sum(r for r in rets if r > 0)
    gross_loss = abs(sum(r for r in rets if r < 0))
    # 交易序列的累计收益与最大回撤（等权、每笔相同仓位，便于跨变体比较）
    cum, peak, mdd = 0.0, 0.0, 0.0
    for trade in sorted(trades, key=lambda t: t.entry_date):
        cum += trade.ret_pct
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_ret": round(mean, 3),
        "median_ret": round(sorted(rets)[n // 2], 3),
        "total_ret": round(sum(rets), 1),
        "std": round(std, 3),
        "t_stat": round(mean / (std / n ** 0.5), 2) if std > 0 and n > 1 else 0.0,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0,
        "stop_rate": round(sum(1 for t in trades if t.exit_reason == "止损") / n * 100, 1),
        "target_rate": round(sum(1 for t in trades if t.exit_reason == "目标") / n * 100, 1),
        "timeout_rate": round(sum(1 for t in trades if t.exit_reason == "超时") / n * 100, 1),
        "avg_hold": round(sum(t.holding_days for t in trades) / n, 1),
        "mdd_sum": round(mdd, 1),
    }


def split_bucket(trades: list) -> dict:
    return {
        "全部": trades,
        STRONG: [t for t in trades if t.regime == STRONG],
        WEAK: [t for t in trades if t.regime == WEAK],
    }


# ---------------------------------------------------------------- 主流程

def run_variant(name: str, spec: dict, bars_map: dict, index_series: list,
                enhanced: dict, cost: float, window: tuple[dt.date, dt.date]) -> list:
    start, end = window
    warm_start = start - WARMUP
    out = []
    for code, bars in bars_map.items():
        sliced = [b for b in bars if warm_start <= b.date <= end]
        if len(sliced) < 120:
            continue
        trades = _replay_code(
            code, sliced, index_series, cost, spec["rules"],
            spec.get("stop_pullback", 0.98), spec.get("stop_breakout", 0.95),
            enhanced_regime=enhanced,
            opts=spec.get("opts"), opts_weak=spec.get("opts_weak"),
        )
        out.extend(
            t for t in trades
            if start.isoformat() <= t.entry_date <= end.isoformat()
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="diagnose",
                    choices=["diagnose", "full", "reversal", "gates", "weak60"])
    ap.add_argument("--limit", type=int, default=0, help="只用前 N 只（调试）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    band = float((settings.app_config().get("market") or {}).get("regime_band_pct", 1.0) or 1.0)

    bars_map = load_bars()
    if args.limit:
        bars_map = dict(list(bars_map.items())[: args.limit])
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    enhanced = _enhanced_regime_map(sh, cyber, band)
    print(f"样本 {len(bars_map)} 只 · 单笔成本 {cost}% · 强弱带 ±{band}% · "
          f"指数 {len(sh)}/{len(cyber)} 天", flush=True)

    variants = (
        variants_diagnose() if args.set == "diagnose"
        else variants_reversal() if args.set == "reversal"
        else variants_gates() if args.set == "gates"
        else variants_weak60() if args.set == "weak60"
        else variants_full()
    )
    results: dict = {"meta": {"codes": len(bars_map), "cost_pct": cost, "band": band,
                              "train": [d.isoformat() for d in TRAIN],
                              "valid": [d.isoformat() for d in VALID]},
                     "windows": {}, "variants": {}}

    for label, window in (("TRAIN", TRAIN), ("VALID", VALID)):
        print(f"\n===== {label} {window[0]} .. {window[1]} =====", flush=True)
        results["windows"][label] = [d.isoformat() for d in window]
        header = f"{'变体':22s}{'子集':10s}{'笔数':>6s}{'胜率':>7s}{'期望':>8s}{'中位':>8s}" \
                 f"{'合计':>9s}{'t':>7s}{'PF':>6s}{'止损':>7s}{'超时':>7s}{'持仓':>7s}{'回撤':>8s}"
        print(header)
        for name, spec in variants.items():
            began = time.time()
            trades = run_variant(name, spec, bars_map, sh, enhanced, cost, window)
            buckets = split_bucket(trades)
            payload = {}
            for label2, subset in buckets.items():
                st = stat(subset)
                payload[label2] = st
                if st is None:
                    print(f"{name:22s}{label2:10s}{'-':>6s}")
                    continue
                print(f"{name:22s}{label2:10s}{st['n']:>6d}{st['win_rate']:>7.1f}{st['avg_ret']:>+8.2f}"
                      f"{st['median_ret']:>+8.2f}{st['total_ret']:>+9.1f}{st['t_stat']:>7.2f}"
                      f"{st['pf']:>6.2f}{st['stop_rate']:>7.1f}{st['timeout_rate']:>7.1f}"
                      f"{st['avg_hold']:>7.1f}{st['mdd_sum']:>+8.1f}")
            results["variants"].setdefault(name, {})[label] = payload
            print(f"{'':22s}  ({time.time() - began:.1f}s)", flush=True)

    out_path = args.out or os.path.join(
        RESEARCH, f"study_{args.set}_{dt.datetime.now():%Y%m%d_%H%M}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print(f"\n结果已写入 {out_path}")


if __name__ == "__main__":
    main()
