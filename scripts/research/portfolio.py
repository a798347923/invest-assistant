"""组合级（5 槽位）模拟：槽位有限时，按不同横截面因子排序，看谁进谁出。

为什么需要它：`study.py` 统计的是**逐笔期望**，默认每笔都能成交、互不挤占。
真实账户只有 5 个槽，同一天出现 6 个信号时，**选谁**比"目标价定 12% 还是 15%"更能决定利润率。

文献依据（2026-11 检索）：
- A 股 2015–2025 实证：短期（3 月）呈**反转**而非动量，IC=−0.032（t=−2.66）；
  低动量组年化 9.43% vs 高动量组 2.04%，且高动量组回撤最深（BigQuant 因子研究）。
- 低换手波动选股（换手率 20 日标准差最小）：年化 19.53%、夏普 1.03、回撤 11.76%。
- 残差动量（剥离市场/行业 beta）方向为正 —— 我们无行业数据，暂不实现。

因此本脚本测试的排序因子：
  chrono      先到先得（现状基线）
  mom_60_asc  60 日动量最低优先（反转）
  mom_60_desc 60 日动量最高优先（动量）
  vol_20_asc  20 日波动率最低优先（低波）
  vol_20_desc 高波优先
  atr_asc     ATR% 最低优先
  rs_20_desc  相对指数 20 日强度最高优先
  volstab_asc 成交量 20 日稳定性最高优先（低换手波动的代理）
  composite   反转 + 低波 + 相对强度的 z 分加总

用法：
    python scripts/research/portfolio.py --window both
    python scripts/research/portfolio.py --window valid --variant v12
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import STRESS, TRAIN, VALID, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import (  # noqa: E402
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)

WARMUP = dt.timedelta(days=120)
RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
FUND_DIR = os.path.join(RESEARCH, "fund")
FINA_DIR = os.path.join(RESEARCH, "fina")


# ---------------------------------------------------------------- 基本面特征


def _num(raw: str) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def load_fund_features() -> dict:
    """Tushare daily_basic 缓存：{(code, date): (pe_ttm, pb, turnover_rate)}。"""
    out: dict = {}
    if not os.path.isdir(FUND_DIR):
        return out
    for name in sorted(os.listdir(FUND_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        with open(os.path.join(FUND_DIR, name), encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 5 or not parts[0]:
                    continue
                try:
                    day = dt.date.fromisoformat(parts[0])
                except ValueError:
                    continue
                out[(code, day)] = (_num(parts[1]), _num(parts[2]), _num(parts[3]))
    return out


def load_fina_features() -> dict:
    """时点财务：{code: [(ann_date, roe, netprofit_yoy), ...]}，按公告日排序。"""
    out: dict = {}
    if not os.path.isdir(FINA_DIR):
        return out
    for name in sorted(os.listdir(FINA_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        rows: list = []
        with open(os.path.join(FINA_DIR, name), encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.strip().split(",")
                if len(parts) < 5 or not parts[0]:
                    continue
                try:
                    ann = dt.date.fromisoformat(parts[0])
                except ValueError:
                    continue
                rows.append((ann, _num(parts[2]), _num(parts[3])))
        if rows:
            rows.sort(key=lambda r: r[0])
            out[code] = rows
    return out


def latest_at(rows: list, day: dt.date) -> tuple[float | None, float | None] | None:
    """取公告日 ≤ day 的最后一期（时点，杜绝未来函数）。"""
    picked = None
    for ann, roe, yoy in rows:
        if ann <= day:
            picked = (roe, yoy)
        else:
            break
    return picked


# ---------------------------------------------------------------- 因子


def features(bars: list, day: dt.date) -> dict | None:
    """信号日收盘后的横截面特征（只用当日及之前数据）。"""
    prior = [b for b in bars if b.date <= day]
    if len(prior) < 61:
        return None
    closes = [b.close for b in prior]
    if not closes[-61]:
        return None
    mom_60 = closes[-1] / closes[-61] - 1
    rets = [
        closes[i] / closes[i - 1] - 1
        for i in range(len(closes) - 20, len(closes))
        if closes[i - 1]
    ]
    vol_20 = statistics.pstdev(rets) if len(rets) >= 5 else None
    vols = [b.volume for b in prior[-20:]]
    vol_mean = statistics.fmean(vols) if vols else 0.0
    vol_std = statistics.pstdev(vols) if len(vols) >= 5 else 0.0
    volstab = (vol_std / vol_mean) if vol_mean else None
    # ATR%
    trs = []
    for i in range(len(prior) - 14, len(prior)):
        high, low, prev_close = prior[i].high, prior[i].low, prior[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = statistics.fmean(trs) if trs else 0.0
    return {
        "mom_60": mom_60,
        "vol_20": vol_20,
        "atr_pct": (atr / closes[-1]) if closes[-1] else None,
        "volstab": volstab,
    }


BOARD_ORDER = {
    "000": 0, "001": 0, "002": 1, "003": 1,
    "600": 2, "601": 2, "603": 2, "605": 2,
    "300": 3, "301": 3, "688": 4,
}


def rank_key(mode: str, feat: dict, rs: float) -> float:
    """越小越优先。"""
    if mode == "chrono":
        return 0.0
    if mode == "board_asc":
        return feat.get("board", 9)
    if mode == "mom_60_asc":
        return feat["mom_60"]
    if mode == "mom_60_desc":
        return -feat["mom_60"]
    if mode == "vol_20_asc":
        return feat["vol_20"] if feat["vol_20"] is not None else 9.9
    if mode == "vol_20_desc":
        return -(feat["vol_20"] or 0)
    if mode == "atr_asc":
        return feat["atr_pct"] if feat["atr_pct"] is not None else 9.9
    if mode == "rs_20_desc":
        return -rs
    if mode == "volstab_asc":
        return feat["volstab"] if feat["volstab"] is not None else 9.9
    if mode == "composite":
        vol = feat["vol_20"] or 0
        return feat["mom_60"] + vol * 8 - rs  # 反转 + 低波 + 强 RS
    # ---- 基本面（Tushare daily_basic）：缺数据一律排到最后
    if mode == "pe_asc":
        pe = feat.get("pe_ttm")
        return pe if (pe is not None and pe > 0) else 1e9
    if mode == "pb_asc":
        pb = feat.get("pb")
        return pb if (pb is not None and pb > 0) else 1e9
    if mode == "turnover_asc":
        turn = feat.get("turnover")
        return turn if turn is not None else 1e9
    if mode == "value2":
        pe = feat.get("pe_ttm")
        turn = feat.get("turnover")
        # 低估值 + 低换手（量纲粗对齐：PE/30、换手/3）
        return (pe / 30 if (pe is not None and pe > 0) else 33.0) + (
            turn / 3 if turn is not None else 33.0
        )
    # ---- 质量/成长（时点财务）：缺数据排最后
    if mode == "roe_desc":
        roe = feat.get("roe")
        return -roe if roe is not None else 1e9
    if mode == "growth_desc":
        yoy = feat.get("yoy")
        return -yoy if yoy is not None else 1e9
    return 0.0


# ---------------------------------------------------------------- 组合模拟


def simulate(trades: list, features_map: dict, mode: str,
             slots: int = 5, fraction: float = 0.2, cash0: float = 50000.0,
             rng=None, sizing: str = "equal", industry_map: dict | None = None, per_day: int = 0,
             industry_limit: int = 0) -> dict:
    """按入场日推进：先平仓、再按排序填槽，逐日按收盘价盯市。

    mode="random" 时用 rng 打乱（用于建立"随便选"的基准分布）。
    sizing="atr" 时按 1/ATR 分配仓位（波动率平价），让每笔的风险预算接近。
    """
    if not trades:
        return {"trades": 0}
    atr_values = sorted(
        f["atr_pct"] for f in features_map.values()
        if f.get("atr_pct")
    )
    median_atr = atr_values[len(atr_values) // 2] if atr_values else 0.03
    by_entry: dict[dt.date, list] = {}
    for t in trades:
        by_entry.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)
    all_days = sorted({dt.date.fromisoformat(t.entry_date) for t in trades}
                      | {dt.date.fromisoformat(t.exit_date) for t in trades})

    cash = cash0
    open_pos: list[dict] = []
    equity_curve: list[tuple[dt.date, float]] = []
    taken = skipped = 0
    wins = 0
    taken_rets: list[float] = []
    realized = 0.0

    for day in all_days:
        # 1) 到期平仓
        still_open = []
        for pos in open_pos:
            if pos["exit_day"] <= day:
                pnl = pos["value"] * (pos["trade"].ret_pct / 100.0)
                cash += pos["value"] + pnl
                realized += pnl
                if pos["trade"].ret_pct > 0:
                    wins += 1
            else:
                still_open.append(pos)
        open_pos = still_open

        # 2) 填槽
        candidates = by_entry.get(day, [])
        if candidates and len(open_pos) < slots:
            ranked = []
            for trade in candidates:
                feat = features_map.get((trade.code, trade.entry_date))
                if feat is None:
                    # chrono / random 不依赖特征；其余排序因子缺特征则跳过该信号
                    if mode not in ("chrono", "random"):
                        continue
                    feat = {}
                ranked.append((rank_key(mode, feat, feat.get("rs_20", 0.0)), trade))
            ranked.sort(key=lambda x: x[0])
            if mode == "random" and rng is not None:
                rng.shuffle(ranked)
            equity = cash + sum(
                p["value"] * (_mark(p["trade"], day) or 1.0) for p in open_pos
            )
            ind_counts: dict = {}
            taken_today = 0
            if industry_limit and industry_map:
                for p in open_pos:
                    ind0 = industry_map.get(p["trade"].code, "")
                    ind_counts[ind0] = ind_counts.get(ind0, 0) + 1
            for _, trade in ranked:
                if len(open_pos) >= slots:
                    skipped += 1
                    continue
                if per_day and taken_today >= per_day:
                    skipped += 1
                    continue
                if industry_limit and industry_map:
                    ind = industry_map.get(trade.code, "")
                    if ind and ind_counts.get(ind, 0) >= industry_limit:
                        skipped += 1
                        continue
                weight = fraction
                if sizing == "atr":
                    feat = features_map.get((trade.code, trade.entry_date)) or {}
                    atr = feat.get("atr_pct") or median_atr
                    # 波动越小仓位越大，限制在 0.5~2 倍，避免极端杠杆
                    weight = fraction * max(0.5, min(2.0, median_atr / atr if atr else 1.0))
                size = min(equity * weight, cash)
                if size < 1000:
                    skipped += 1
                    continue
                cash -= size
                open_pos.append({
                    "trade": trade, "value": size,
                    "exit_day": dt.date.fromisoformat(trade.exit_date),
                })
                if industry_limit and industry_map:
                    ind = industry_map.get(trade.code, "")
                    ind_counts[ind] = ind_counts.get(ind, 0) + 1
                taken += 1
                taken_today += 1
                taken_rets.append(trade.ret_pct)

        # 3) 盯市
        mv = sum(
            p["value"] * (_mark(p["trade"], day) or 1.0)
            for p in open_pos
        )
        equity_curve.append((day, cash + mv))

    if not equity_curve:
        return {"trades": 0}
    final = equity_curve[-1][1]
    peak, mdd = equity_curve[0][1], 0.0
    for _, value in equity_curve:
        peak = max(peak, value)
        mdd = min(mdd, value / peak - 1)
    days = (equity_curve[-1][0] - equity_curve[0][0]).days or 1
    annual = (final / cash0) ** (365 / days) - 1
    return {
        "trades": taken,
        "skipped": skipped,
        "win_rate": round(wins / taken * 100, 1) if taken else 0.0,
        "avg_ret_taken": round(statistics.fmean(taken_rets), 2) if taken_rets else 0.0,
        "realized_pnl": round(realized, 0),
        "open_at_end": len(open_pos),
        "final": round(final, 0),
        "total_ret_pct": round((final / cash0 - 1) * 100, 1),
        "annual_pct": round(annual * 100, 1),
        "max_drawdown_pct": round(mdd * 100, 1),
        "days": days,
    }


def _mark(trade, day: dt.date) -> float | None:
    """持仓期内按当日收盘价盯市（相对入场价的倍数）。"""
    ratio_bars = trade.__dict__.get("_mark_cache")
    if ratio_bars is None:
        return None
    return ratio_bars.get(day)


# ---------------------------------------------------------------- 主流程


MODES = [
    "chrono", "board_asc", "mom_60_asc", "mom_60_desc", "vol_20_asc", "vol_20_desc",
    "atr_asc", "rs_20_desc", "volstab_asc", "composite",
    "pe_asc", "pb_asc", "turnover_asc", "value2", "roe_desc", "growth_desc",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="both", choices=["train", "valid", "stress", "both", "all"])
    ap.add_argument("--variant", default="v12", choices=["v12", "weak_open"])
    ap.add_argument("--slots", type=int, default=5)
    ap.add_argument("--fraction", type=float, default=0.2)
    ap.add_argument("--seeds", type=int, default=40, help="随机基准的抽样次数")
    ap.add_argument("--hold", type=int, default=30, help="持有上限（交易日）；槽位有限时它会改变可成交笔数")
    ap.add_argument("--sizing", default="equal", choices=["equal", "atr"],
                    help="equal=等权（每笔 20%%）；atr=波动率平价")
    ap.add_argument("--per-day", type=int, default=0, help="每天最多新开几笔（0=不限）")
    ap.add_argument("--industry-limit", type=int, default=0,
                    help="同行业最多几只（0=不限制）；>0 时额外打印行业分散对照")
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    band = float((settings.app_config().get("market") or {}).get("regime_band_pct", 1.0) or 1.0)
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    enhanced = _enhanced_regime_map(sh, cyber, band)
    fund_map = load_fund_features()
    fina_map = load_fina_features()
    industry_map = load_industry_map()
    print(
        f"基本面缓存：估值 {len(fund_map)} 条 / 财务 {len(fina_map)} 只 / 行业 {len(industry_map)} 只"
        if fund_map else "基本面缓存：无（只跑价量因子）"
    )

    rules = base_rules() if args.variant == "v12" else base_rules(weak_block="none")
    opts = base_opts(max_hold=args.hold)

    out: dict = {"meta": {"variant": args.variant, "slots": args.slots,
                          "fraction": args.fraction, "codes": len(bars_map),
                          "hold": args.hold, "sizing": args.sizing},
                 "results": {}}

    windows = {"train": TRAIN, "valid": VALID, "stress": STRESS}
    for name, window in windows.items():
        if args.window not in ("both", "all") and args.window != name:
            continue
        start, end = window
        warm_start = start - WARMUP
        trades = []
        feat_map: dict = {}
        for code, bars in bars_map.items():
            sliced = [b for b in bars if warm_start <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(
                code, sliced, sh, cost, rules,
                0.98, 0.95, enhanced_regime=enhanced, opts=opts,
            )
            for t in got:
                if not (start.isoformat() <= t.entry_date <= end.isoformat()):
                    continue
                signal_day = dt.date.fromisoformat(t.entry_date)
                # 特征用信号日前一日（入场是次日开盘）
                prior_days = [b.date for b in sliced if b.date < signal_day]
                if not prior_days:
                    continue
                feat = features(sliced, prior_days[-1])
                if feat is None:
                    continue
                idx20 = None
                pts = [c for d, c in sh if d <= prior_days[-1]]
                if len(pts) >= 21 and pts[-21]:
                    idx20 = pts[-1] / pts[-21] - 1
                closes = [b.close for b in sliced if b.date <= prior_days[-1]]
                rs = (closes[-1] / closes[-21] - 1 - idx20) if idx20 is not None and len(closes) >= 21 else 0.0
                feat["rs_20"] = rs
                feat["board"] = BOARD_ORDER.get(code[:3], 9)
                fund = fund_map.get((code, prior_days[-1]))
                if fund:
                    feat["pe_ttm"], feat["pb"], feat["turnover"] = fund
                fina_rows = fina_map.get(code)
                if fina_rows:
                    hit = latest_at(fina_rows, prior_days[-1])
                    if hit:
                        feat["roe"], feat["yoy"] = hit
                feat_map[(code, t.entry_date)] = feat
                trades.append(t)

        print(f"\n===== {name.upper()} {start}..{end} · {len(trades)} 笔信号 · "
              f"{args.slots} 槽 / 单笔 {args.fraction:.0%} =====")

        # 先建立「随便选」的基准分布：只看排序因子是否显著偏离随机
        rand_rets: list[float] = []
        rng_master = random.Random(20261103)
        for _ in range(args.seeds):
            res = _simulate_with_marks(trades, feat_map, "random", bars_map,
                                       args.slots, args.fraction,
                                       rng=random.Random(rng_master.random()), per_day=args.per_day,
                                       sizing=args.sizing)
            rand_rets.append(res["total_ret_pct"])
        rand_rets.sort()
        mean_r = statistics.fmean(rand_rets)
        p10, p90 = rand_rets[int(0.1 * len(rand_rets))], rand_rets[int(0.9 * len(rand_rets))]
        print(f"【随机基准】{args.seeds} 次：均值 {mean_r:+.1f}%，"
              f"p10 {p10:+.1f}%，p90 {p90:+.1f}%，最好 {rand_rets[-1]:+.1f}%")

        header = (f"{'排序因子':14s}{'成交':>6s}{'胜率':>7s}{'总收益':>9s}{'年化':>8s}"
                  f"{'回撤':>8s}{'分位':>7s}")
        print(header)
        out["results"][name] = {"random": {"mean": round(mean_r, 1), "p10": p10,
                                           "p90": p90, "all": rand_rets}}
        for mode in MODES:
            res = _simulate_with_marks(trades, feat_map, mode, bars_map, args.slots,
                                       args.fraction, sizing=args.sizing, per_day=args.per_day)
            percentile = sum(1 for r in rand_rets if r < res["total_ret_pct"]) / len(rand_rets) * 100
            res["percentile_vs_random"] = round(percentile, 0)
            out["results"][name][mode] = res
            print(f"{mode:14s}{res.get('trades', 0):>6d}{res.get('win_rate', 0):>7.1f}"
                  f"{res.get('total_ret_pct', 0):>+9.1f}{res.get('annual_pct', 0):>+8.1f}"
                  f"{res.get('max_drawdown_pct', 0):>+8.1f}{percentile:>6.0f}%")

        # 行业分散约束：同行业最多 N 只（chrono 为基准，随机基准同步加约束才可比）
        if industry_map:
            print("【行业分散】同行业上限 1/2/3 只（chrono 排序）")
            for limit in (1, 2, 3):
                res = _simulate_with_marks(trades, feat_map, "chrono", bars_map, args.slots,
                                           args.fraction, sizing=args.sizing,
                                           industry_map=industry_map, industry_limit=limit)
                rand_ind = statistics.fmean(
                    _simulate_with_marks(
                        trades, feat_map, "random", bars_map, args.slots, args.fraction,
                        rng=random.Random(6000 + i), sizing=args.sizing,
                        industry_map=industry_map, industry_limit=limit,
                    )["total_ret_pct"]
                    for i in range(max(10, args.seeds // 2))
                )
                out["results"][name][f"ind{limit}_chrono"] = res
                print(f"{'ind' + str(limit) + '_chrono':14s}{res.get('trades', 0):>6d}"
                      f"{res.get('win_rate', 0):>7.1f}{res.get('total_ret_pct', 0):>+9.1f}"
                      f"{res.get('annual_pct', 0):>+8.1f}"
                      f"{res.get('max_drawdown_pct', 0):>+8.1f}"
                      f"{'随机' + format(rand_ind, '+.1f'):>10s}")

    path = os.path.join(RESEARCH, f"portfolio_{args.variant}_{dt.datetime.now():%Y%m%d_%H%M}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n结果已写入 {path}")


def load_industry_map() -> dict:
    """申万一级行业映射（当前成分，历史成分不可还原）。"""
    path = os.path.join(RESEARCH, "industry.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("industry_of") or {}
    except Exception:  # noqa: BLE001
        return {}


def _simulate_with_marks(trades, feat_map, mode, bars_map, slots, fraction,
                         rng=None, sizing: str = "equal", industry_map: dict | None = None,
                         per_day: int = 0, industry_limit: int = 0) -> dict:
    """给每笔交易准备盯市价格序列后再模拟。"""
    enriched = []
    for t in trades:
        code = t.code
        bars = bars_map.get(code) or []
        entry = dt.date.fromisoformat(t.entry_date)
        exit_day = dt.date.fromisoformat(t.exit_date)
        ep = t.entry_price
        marks = {}
        if ep:
            for b in bars:
                if entry <= b.date <= exit_day:
                    marks[b.date] = b.close / ep
        # 复用 dataclass 字段不方便，塞进副本的 __dict__
        import copy

        dup = copy.copy(t)
        dup.__dict__["_mark_cache"] = marks
        enriched.append(dup)
    return simulate(enriched, feat_map, mode, slots, fraction, rng=rng, sizing=sizing, per_day=per_day,
                    industry_map=industry_map, industry_limit=industry_limit)


if __name__ == "__main__":
    main()
