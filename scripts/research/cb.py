"""方向⑥：弱市用「低价可转债」替代停泊（对比现金 / 十年国债 ETF）。

逻辑与停泊一致：只在**弱市**投放，强市回现金；状态用**前一日收盘**判定（无前视）。
可转债侧：每 5 个交易日调仓，等权持有「价格最低的 N 只」（过滤 90~130 元、上市/未退市）。

重要限制（必须在结论里披露）：
- 只有价格，**没有转股溢价率**（需要正股价格 + 历史转股价，当前只有最新转股价），
  因此这是「低价轮动」而非严格「双低轮动」；
- 不含 T+0 与临停规则，按收盘价成交；
- `cb_basic` 的 `conv_price` 是当前值，历史下修/送转无法还原。

用法：python scripts/research/cb.py --n 5 --rebalance 5
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import STRESS, TRAIN, VALID, load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource.tushare import TushareClient  # noqa: E402
from invest_assistant.engine.playbook_replay import _enhanced_regime_map  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
CB_DIR = os.path.join(RESEARCH, "cb")
BASIC = os.path.join(CB_DIR, "basic.json")
ETF = os.path.join(RESEARCH, "etf", "sh511260.csv")


def load_basic() -> dict:
    if os.path.exists(BASIC):
        with open(BASIC, encoding="utf-8") as fh:
            return json.load(fh)
    client = TushareClient.from_settings(get_settings())
    rows = client.call(
        "cb_basic", {},
        "ts_code,bond_short_name,stk_code,list_date,delist_date,conv_price,remain_size,par",
    ).rows
    book = {}
    for row in rows:
        code = str(row.get("ts_code") or "")
        if not code:
            continue
        book[code] = {
            "name": row.get("bond_short_name") or "",
            "list": str(row.get("list_date") or ""),
            "delist": str(row.get("delist_date") or ""),
            "conv_price": row.get("conv_price") or 0,
            "par": row.get("par") or 100,
        }
    os.makedirs(CB_DIR, exist_ok=True)
    with open(BASIC, "w", encoding="utf-8") as fh:
        json.dump(book, fh, ensure_ascii=False, indent=1)
    return book


def load_prices() -> dict:
    """{date_str: {cb_code: close}}"""
    panel: dict = {}
    path = os.path.join(CB_DIR, "daily.csv")
    if not os.path.exists(path):
        return panel
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            day, code = row["date"], row["code"]
            try:
                close = float(row["close"])
            except (TypeError, ValueError):
                continue
            try:
                iso = dt.date.fromisoformat(day).isoformat()
            except ValueError:
                continue
            panel.setdefault(iso, {})[code] = close
    return panel


def load_etf() -> dict:
    out = {}
    if not os.path.exists(ETF):
        return out
    with open(ETF, encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            day, close = line.strip().split(",")[:2]
            out[day] = float(close)
    return out


def eligible(basic: dict, code: str, day: str) -> bool:
    meta = basic.get(code)
    if not meta:
        return False
    if meta["list"] and day < meta["list"]:
        return False
    if meta["delist"] and day >= meta["delist"]:
        return False
    return True


def backtest(window, panel, basic, etf, regime, days_all, n_bonds: int, rebalance: int,
             cost: float = 0.0004) -> dict:
    start, end = window
    days = [d for d in days_all if start <= d <= end]
    if not days:
        return {}

    cash = 10000.0
    units: dict[str, float] = {}      # code -> 份额
    last_px: dict[str, float] = {}
    curve: list[float] = []
    switches = 0
    weak_days = 0

    for i, day in enumerate(days):
        dstr = day.isoformat()
        prices = panel.get(dstr) or {}
        for code, px in prices.items():
            last_px[code] = px

        # 前一交易日的状态（无前视）
        prev = days[i - 1] if i > 0 else day
        weak = regime.get(prev) is False
        if weak:
            weak_days += 1

        # 调仓：弱市持有 N 只低价券，否则清仓
        if i % rebalance == 0:
            if weak:
                pool = [
                    (px, code) for code, px in prices.items()
                    if 90 <= px <= 130 and eligible(basic, code, dstr)
                ]
                pool.sort()
                picked = [code for _, code in pool[:n_bonds]]
                if picked:
                    nav = cash + sum(units.get(c, 0) * (last_px.get(c) or 0) for c in units)
                    per = nav / len(picked)
                    for code in list(units):
                        if code not in picked:
                            px = last_px.get(code) or 0
                            cash += units[code] * px * (1 - cost)
                            del units[code]
                            switches += 1
                    for code in picked:
                        px = prices.get(code)
                        if not px:
                            continue
                        cur = units.get(code, 0) * px
                        if cur < per * 0.9:
                            add_value = per - cur
                            if add_value > 0 and cash > add_value * 0.5:
                                units[code] = units.get(code, 0) + (add_value * (1 - cost)) / px
                                cash -= add_value
                                switches += 1
            else:
                for code in list(units):
                    px = last_px.get(code) or 0
                    cash += units[code] * px * (1 - cost)
                    del units[code]
                    switches += 1

        curve.append(cash + sum(u * (last_px.get(c) or 0) for c, u in units.items()))

    if not curve:
        return {}
    total = curve[-1] / curve[0] - 1
    peak = curve[0]
    dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {
        "total": total * 100,
        "dd": dd * 100,
        "weak_days": weak_days,
        "switches": switches,
    }


def static_curve(window, series: dict, days_all) -> dict:
    start, end = window
    days = [d for d in days_all if start <= d <= end]
    pts = [series[d.isoformat()] for d in days if d.isoformat() in series]
    if len(pts) < 2:
        return {}
    total = pts[-1] / pts[0] - 1
    peak, dd = pts[0], 0.0
    for v in pts:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {"total": total * 100, "dd": dd * 100}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="持仓只数")
    ap.add_argument("--rebalance", type=int, default=5, help="调仓间隔（交易日）")
    args = ap.parse_args()

    basic = load_basic()
    panel = load_prices()
    if not panel:
        print("没有可转债行情缓存，请先运行 cb_fetch.py")
        return
    days_all = sorted(dt.date.fromisoformat(d) for d in panel)
    etf = load_etf()
    sh, cyber = load_index("sh000001"), load_index("sz399006")
    regime = _enhanced_regime_map(sh, cyber, 1.0)
    print(f"可转债行情 {len(panel)} 个交易日 / {len(basic)} 只债")

    print(f"\n{'窗口':8s}{'方案':18s}{'总收益':>9s}{'回撤':>9s}{'弱市天':>7s}{'调仓':>6s}")
    for name, window in (("TRAIN", TRAIN), ("VALID", VALID), ("STRESS", STRESS)):
        cb5 = backtest(window, panel, basic, etf, regime, days_all, args.n, args.rebalance)
        cb10 = backtest(window, panel, basic, etf, regime, days_all, args.n * 2, args.rebalance)
        bond = static_curve(window, etf, days_all)
        if cb5:
            print(f"{name:8s}{'低价转债' + str(args.n) + '只':18s}{cb5['total']:>+9.1f}"
                  f"{cb5['dd']:>+9.1f}{cb5['weak_days']:>7d}{cb5['switches']:>6d}")
        if cb10:
            print(f"{'':8s}{'低价转债' + str(args.n * 2) + '只':18s}{cb10['total']:>+9.1f}"
                  f"{cb10['dd']:>+9.1f}{cb10['weak_days']:>7d}{cb10['switches']:>6d}")
        if bond:
            print(f"{'':8s}{'十年国债ETF(全期)':18s}{bond['total']:>+9.1f}{bond['dd']:>+9.1f}")
        print(f"{'':8s}{'现金':18s}{0.0:>+9.1f}{0.0:>+9.1f}")


if __name__ == "__main__":
    main()
