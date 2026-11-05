"""方向①：负面事件闸门 A/B 回测。

假设：买入前剔除「近期有减持 / 预告预亏 / 未来 30 天大额解禁」的标的，能降低止损率与回撤
（不一定提高期望），从而在两个窗口都成立——这正是实盘 `live.announcement_filter` 一直无法回测的部分。

闸门口径（全部用 `ann_date` 时点对齐，事件在入场日之前已知）：
- float    ：未来 30 天内解禁，且当次解禁占流通比 ≥ 3%
- reduce   ：过去 30 天内出现股东减持公告
- forecast ：过去 30 天内出现预减/略减/首亏/续亏/增亏（或变动上限 < 0）
- all      ：以上任一

判定：与未过滤基线比，组合级随机均值与回撤。命门是「不显著变差 + 回撤改善」。

用法：python scripts/research/events_study.py --seeds 20
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
from study import TRAIN, VALID, WARMUP, base_opts, base_rules, load_bars, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.engine.playbook_replay import _cost_pct, _enhanced_regime_map, _replay_code  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
EVENT_DIR = os.path.join(RESEARCH, "events")

BAD_FORECAST = {"预减", "略减", "首亏", "续亏", "增亏", "不确定"}
FLOAT_LOOKAHEAD = 30
LOOKBACK = 30
FLOAT_MIN_RATIO = 3.0


def load_events() -> dict:
    """{code: {"float": [(ann, date, ratio)], "reduce": [ann], "forecast": [(ann, type, hi)]}}"""
    out: dict = {}
    if not os.path.isdir(EVENT_DIR):
        return out
    for name in sorted(os.listdir(EVENT_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        slot = {"float": [], "reduce": [], "forecast": []}
        with open(os.path.join(EVENT_DIR, name), encoding="utf-8") as fh:
            next(fh)
            for line in fh:
                parts = line.rstrip("\n").split(",", 4)
                if len(parts) < 5:
                    continue
                kind, ann, event, value, detail = parts
                try:
                    ann_d = dt.date.fromisoformat(f"{ann[:4]}-{ann[4:6]}-{ann[6:8]}")
                except ValueError:
                    continue
                try:
                    val = float(value)
                except ValueError:
                    val = 0.0
                if kind == "float":
                    try:
                        d = dt.date.fromisoformat(f"{event[:4]}-{event[4:6]}-{event[6:8]}")
                    except ValueError:
                        continue
                    slot["float"].append((ann_d, d, val))
                elif kind == "trade":
                    slot["reduce"].append(ann_d)
                elif kind == "forecast":
                    ftype = detail.split(" ")[0] if detail else ""
                    slot["forecast"].append((ann_d, ftype, val))
        out[code] = slot
    return out


def blocked(code: str, entry: dt.date, events: dict, gate: str) -> bool:
    slot = events.get(code) or {"float": [], "reduce": [], "forecast": []}
    if gate in ("float", "all"):
        for ann, fdate, ratio in slot["float"]:
            if ann <= entry and entry <= fdate <= entry + dt.timedelta(days=FLOAT_LOOKAHEAD):
                if ratio >= FLOAT_MIN_RATIO:
                    return True
    if gate in ("reduce", "all"):
        if any(entry - dt.timedelta(days=LOOKBACK) <= ann <= entry for ann in slot["reduce"]):
            return True
    if gate in ("forecast", "all"):
        for ann, ftype, hi in slot["forecast"]:
            if entry - dt.timedelta(days=LOOKBACK) <= ann <= entry and (
                ftype in BAD_FORECAST or hi < 0
            ):
                return True
    return False


def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t.ret_pct for t in trades]
    return {
        "n": len(rets),
        "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg": round(statistics.fmean(rets), 3),
        "stop": round(sum(1 for t in trades if t.exit_reason == "止损") / len(trades) * 100, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    args = ap.parse_args()

    settings = get_settings()
    cost = _cost_pct(settings)
    bars_map = load_bars()
    sh = load_index("sh000001")
    cyber = load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    events = load_events()
    print(f"事件库：{len(events)} 只")

    rules = base_rules()
    opts = base_opts()

    for window_name, window in (("TRAIN", TRAIN), ("VALID", VALID)):
        start, end = window
        warm_start = start - WARMUP
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if warm_start <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got
                          if start.isoformat() <= t.entry_date <= end.isoformat())

        print(f"\n===== {window_name} {start}..{end} · 基线 {len(trades)} 笔 =====")
        print(f"{'闸门':10s}{'剔除':>6s}{'保留':>6s}{'胜率':>7s}{'期望':>8s}{'止损率':>8s}"
              f"{'组合':>9s}{'随机均值':>9s}{'回撤':>8s}")
        base_port = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
        for gate in ("none", "float", "reduce", "forecast", "all"):
            kept = [t for t in trades
                    if gate == "none"
                    or not blocked(t.code, dt.date.fromisoformat(t.entry_date), events, gate)]
            st = stats(kept)
            port = _simulate_with_marks(kept, {}, "chrono", bars_map, 5, 0.2)
            rand = statistics.fmean(
                _simulate_with_marks(kept, {}, "random", bars_map, 5, 0.2,
                                     rng=random.Random(3000 + i))["total_ret_pct"]
                for i in range(args.seeds)
            )
            print(f"{gate:10s}{len(trades) - len(kept):>6d}{len(kept):>6d}{st.get('win', 0):>7.1f}"
                  f"{st.get('avg', 0):>+8.2f}{st.get('stop', 0):>8.1f}"
                  f"{port['total_ret_pct']:>+9.1f}{rand:>+9.1f}{port['max_drawdown_pct']:>+8.1f}")
        print(f"（基线组合 {base_port['total_ret_pct']:+.1f}% / 回撤 "
              f"{base_port['max_drawdown_pct']:+.1f}%）")


if __name__ == "__main__":
    main()
