"""方向③：PEAD（业绩预告后的漂移）。

事件：Tushare `forecast` 的业绩预告（带 ann_date，公告日即已知）。
入场：公告日之后第一个交易日的开盘价（与线上口径一致：次日开盘成交）。
出场：持有 N 个交易日收盘；或触发 -8% 止损 / +12% 目标（盘中触发价成交）。
成本：沿用 `_cost_pct`（0.15%/笔）。

对照：
- 随机基准：同样本、同数量、随机日期随机标的的入场，看 PEAD 是否优于"随便买"。
- 三窗口：TRAIN / VALID / STRESS。

用法：python scripts/research/pead.py --hold 20 --seeds 200
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

from study import STRESS, TRAIN, VALID, load_bars  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
EVENT_DIR = os.path.join(RESEARCH, "events")

# 预告类型分组
POSITIVE = {"预增", "略增", "扭亏", "续盈"}
STRONG = {"预增", "扭亏"}


def load_forecasts(codes: set[str]) -> list[tuple[str, dt.date, str, float]]:
    """[(code, ann_date, type, p_change_max)]"""
    out: list[tuple[str, dt.date, str, float]] = []
    for name in sorted(os.listdir(EVENT_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        if code not in codes:
            continue
        with open(os.path.join(EVENT_DIR, name), encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.rstrip("\n").split(",", 4)
                if len(parts) < 5 or parts[0] != "forecast":
                    continue
                ann = parts[1]
                try:
                    ann_d = dt.date(int(ann[:4]), int(ann[4:6]), int(ann[6:8]))
                    hi = float(parts[3])
                except (ValueError, IndexError):
                    continue
                ftype = parts[4].split(" ")[0] if parts[4] else ""
                out.append((code, ann_d, ftype, hi))
    return out


def trade_one(bars: list, ann: dt.date, hold: int, stop: float, target: float,
              cost: float) -> dict | None:
    """公告日后第一个交易日开盘买入，持有 hold 日或触发止损/目标。"""
    idx = None
    for i, b in enumerate(bars):
        if b.date > ann:
            idx = i
            break
    if idx is None or idx + 1 >= len(bars):
        return None
    entry = bars[idx].open
    if not entry or entry <= 0:
        return None
    stop_px = entry * (1 - stop)
    target_px = entry * (1 + target)
    exit_px = None
    exit_reason = "超时"
    last = min(idx + hold, len(bars) - 1)
    for j in range(idx, last + 1):
        bar = bars[j]
        if j > idx and bar.low <= stop_px:
            exit_px, exit_reason = stop_px, "止损"
            break
        if j > idx and bar.high >= target_px:
            exit_px, exit_reason = target_px, "目标"
            break
    if exit_px is None:
        exit_px = bars[last].close
    ret = (exit_px / entry - 1 - cost) * 100
    return {
        "code": bars[idx].code if hasattr(bars[idx], "code") else "",
        "entry_date": bars[idx].date.isoformat(),
        "ret_pct": round(ret, 4),
        "exit_reason": exit_reason,
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    rets = [r["ret_pct"] for r in rows]
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    t = mean / (sd / len(rets) ** 0.5) if sd > 0 else 0.0
    return {
        "n": len(rets),
        "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg": round(mean, 2),
        "t": round(t, 2),
        "stop": round(sum(1 for r in rows if r["exit_reason"] == "止损") / len(rows) * 100, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--stop", type=float, default=0.08)
    ap.add_argument("--target", type=float, default=0.12)
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings) / 100  # _cost_pct 是百分数
    bars_map = load_bars()
    codes = set(bars_map)
    forecasts = load_forecasts(codes)
    print(f"冻结池 {len(codes)} 只，预告事件 {len(forecasts)} 条，成本 {cost * 100:.2f}%")

    for name, window in (("TRAIN", TRAIN), ("VALID", VALID), ("STRESS", STRESS)):
        start, end = window
        variants = {
            "预增≥30%": lambda t, hi: t in STRONG and hi >= 30,
            "预增≥50%": lambda t, hi: t in STRONG and hi >= 50,
            "正向(预增/略增/扭亏/续盈)": lambda t, hi: t in POSITIVE,
        }
        print(f"\n===== {name} {start}..{end} · 持有 {args.hold} 日 / 止损 "
              f"{args.stop:.0%} / 目标 {args.target:.0%} =====")
        rows_all = []
        for label, keep in variants.items():
            rows = []
            for code, ann, ftype, hi in forecasts:
                if not (start <= ann <= end) or not keep(ftype, hi):
                    continue
                got = trade_one(bars_map[code], ann, args.hold, args.stop, args.target, cost)
                if got:
                    got["code"] = code
                    rows.append(got)
            rows_all.extend(rows)
            st = summarize(rows)
            print(f"  {label:24s} 事件 {st['n']:5d}  胜率 {st.get('win', 0):5.1f}  "
                  f"期望 {st.get('avg', 0):+6.2f}%  t={st.get('t', 0):+5.2f}  "
                  f"止损率 {st.get('stop', 0):5.1f}%")

        # 随机基准：同样的持有期/止损/目标，随机日期随机标的
        rng = random.Random(42)
        rand_means = []
        n_target = len(rows_all)
        for _ in range(args.seeds):
            picks = []
            for _ in range(n_target):
                code = rng.choice(list(codes))
                bars = bars_map[code]
                if len(bars) < args.hold + 2:
                    continue
                i = rng.randrange(60, len(bars) - args.hold - 2)
                if not (start <= bars[i].date <= end):
                    continue
                got = trade_one(bars, bars[i].date - dt.timedelta(days=1),
                                args.hold, args.stop, args.target, cost)
                if got:
                    picks.append(got)
            st = summarize(picks)
            if st["n"] >= 20:
                rand_means.append(st["avg"])
        if rand_means:
            band = sorted(rand_means)
            lo = band[int(len(band) * 0.1)]
            hi = band[int(len(band) * 0.9)]
            print(f"  随机基准（{args.seeds} 次）：均值 {statistics.fmean(band):+.2f}%  "
                  f"p10 {lo:+.2f}%  p90 {hi:+.2f}%")


if __name__ == "__main__":
    main()
