"""ETF 对照实验：同一套 v12 规则，把标的从个股换成 ETF。

问题：交易 ETF 是否比个股更赚钱？
- 收益层面：ETF 是一篮子，波动更低，12% 目标价可能达不到；
- 成本层面：ETF **免印花税**（个股卖出 0.05%），佣金也相同量级；
- 风险层面：没有个股暴雷/退市尾部风险。

做法：`fund_daily` 抓 40 只流动性好的 ETF（2015 起），用与个股完全相同的
`_replay_code` + 5 槽组合模拟，三窗口对比；再按两档成本（0.15% 同个股 / 0.05% 真实 ETF）各跑一次。

用法：python scripts/research/etf_study.py [--seeds 20]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import _simulate_with_marks  # noqa: E402
from study import STRESS, TRAIN, VALID, WARMUP, base_opts, base_rules, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource import Bar  # noqa: E402
from invest_assistant.datasource.tushare import TushareClient  # noqa: E402
from invest_assistant.engine.playbook_replay import _enhanced_regime_map, _replay_code  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
ETF_DIR = os.path.join(RESEARCH, "etf_bars")
START, END = "20150101", "20261104"

# 宽基 / 行业 / 主题 / 商品债券，覆盖不同风格
ETFS = [
    "510300.SH", "510500.SH", "510050.SH", "159915.SZ", "512100.SH", "588000.SH",
    "159949.SZ", "510880.SH", "512890.SH", "515180.SH",
    "512880.SH", "512000.SH", "512800.SH", "512010.SH", "512170.SH", "159928.SZ",
    "515170.SH", "512690.SH", "512480.SH", "512760.SH", "159995.SZ", "512720.SH",
    "515050.SH", "159869.SZ", "512980.SH", "515030.SH", "515790.SH", "516160.SH",
    "512660.SH", "512400.SH", "515220.SH", "159865.SZ", "512200.SH", "516950.SH",
    "513100.SH", "513500.SH", "518880.SH", "511260.SH", "511010.SH", "159766.SZ",
]


def ts_code_to_code(ts: str) -> str:
    return ts.split(".")[0]


def fetch_all() -> None:
    os.makedirs(ETF_DIR, exist_ok=True)
    client = TushareClient.from_settings(get_settings())
    done = skipped = 0
    for ts in ETFS:
        path = os.path.join(ETF_DIR, f"{ts_code_to_code(ts)}.csv")
        if os.path.exists(path):
            skipped += 1
            continue
        try:
            rows = client.call(
                "fund_daily", {"ts_code": ts, "start_date": START, "end_date": END},
                "ts_code,trade_date,open,high,low,close,vol,amount",
            ).rows
        except Exception as exc:  # noqa: BLE001
            print(f"  {ts} 失败: {str(exc)[:60]}")
            continue
        if not rows:
            continue
        with open(path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["date", "open", "high", "low", "close", "volume"])
            for row in rows:
                day = str(row.get("trade_date") or "")
                if len(day) != 8:
                    continue
                iso = f"{day[:4]}-{day[4:6]}-{day[6:8]}"
                writer.writerow([iso, row.get("open"), row.get("high"), row.get("low"),
                                 row.get("close"), row.get("vol") or 0])
        done += 1
    print(f"ETF 抓取完成：新增 {done}，已有 {skipped}")


def load_etf_bars() -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    if not os.path.isdir(ETF_DIR):
        return out
    for name in sorted(os.listdir(ETF_DIR)):
        if not name.endswith(".csv"):
            continue
        code = name[:-4]
        bars: list[Bar] = []
        with open(os.path.join(ETF_DIR, name), encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    bars.append(Bar(
                        code=code,
                        date=dt.date.fromisoformat(row["date"]),
                        open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=float(row["volume"] or 0),
                    ))
                except (TypeError, ValueError):
                    continue
        bars.sort(key=lambda b: b.date)
        if len(bars) >= 120:
            out[code] = bars
    return out


def replay(bars_map, regime, cost_pct: float):
    rules, opts = base_rules(), base_opts()
    sh = load_index("sh000001")
    per_window = {}
    for name, (start, end) in (("TRAIN", TRAIN), ("VALID", VALID), ("STRESS", STRESS)):
        trades = []
        for code, bars in bars_map.items():
            sliced = [b for b in bars if start - WARMUP <= b.date <= end]
            if len(sliced) < 120:
                continue
            got = _replay_code(code, sliced, sh, cost_pct, rules, 0.98, 0.95,
                               enhanced_regime=regime, opts=opts)
            trades.extend(t for t in got
                          if start.isoformat() <= t.entry_date <= end.isoformat())
        per_window[name] = trades
    return per_window


def stats(trades) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t.ret_pct for t in trades]
    return {
        "n": len(rets),
        "win": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "avg": round(statistics.fmean(rets), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--no-fetch", action="store_true")
    args = ap.parse_args()

    if not args.no_fetch:
        fetch_all()
    bars_map = load_etf_bars()
    if not bars_map:
        print("没有 ETF 行情，请先抓取")
        return
    print(f"可用 ETF {len(bars_map)} 只")

    sh, cyber = load_index("sh000001"), load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)

    for cost_pct in (0.15, 0.05):
        per_window = replay(bars_map, regime, cost_pct)
        print(f"\n===== 成本 {cost_pct:.2f}%/笔（ETF 免印花税，0.05 更接近真实） =====")
        print(f"{'窗口':8s}{'笔数':>6s}{'胜率':>7s}{'逐笔期望':>9s}{'组合':>9s}{'随机均值':>9s}{'回撤':>8s}")
        for name in ("TRAIN", "VALID", "STRESS"):
            trades = per_window[name]
            st = stats(trades)
            if not st["n"]:
                print(f"{name:8s}{0:>6d}  （无信号）")
                continue
            port = _simulate_with_marks(trades, {}, "chrono", bars_map, 5, 0.2)
            rand = statistics.fmean(
                _simulate_with_marks(trades, {}, "random", bars_map, 5, 0.2,
                                     rng=random.Random(700 + i))["total_ret_pct"]
                for i in range(args.seeds)
            )
            print(f"{name:8s}{st['n']:>6d}{st['win']:>7.1f}{st['avg']:>+9.2f}"
                  f"{port['total_ret_pct']:>+9.1f}{rand:>+9.1f}"
                  f"{port['max_drawdown_pct']:>+8.1f}")


if __name__ == "__main__":
    main()
