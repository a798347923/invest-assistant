"""研究用数据层：6 年 A 股前复权日线 + 指数，落盘缓存（data/research/bars）。

为什么不复用 `MarketDataService`：
- 腾讯前复权只给 640 根（约 2.5 年），够不到"3 年前"的验证集；
- 东财接口在本机网络时好时坏；
- akshare 走新浪通道能拿到 6.8 年前复权全历史（实测 1653 根/只）。

本模块只服务研究脚本，写入 `data/research/`（已被 .gitignore 的 data/ 覆盖），
不触碰生产库 `data/invest.db`。

用法：
    python scripts/research/data.py --n 240          # 抽样并缓存
    python scripts/research/data.py --n 240 --index  # 只补指数
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RESEARCH = os.path.join(ROOT, "data", "research")
BAR_DIR = os.path.join(RESEARCH, "bars")
INDEX_DIR = os.path.join(RESEARCH, "index")
ETF_DIR = os.path.join(RESEARCH, "etf")
START = dt.date(2015, 1, 1)
END = dt.date(2026, 10, 31)
INDEXES = {"sh000001": "上证指数", "sz399006": "创业板指"}


def _ensure_dirs() -> None:
    os.makedirs(BAR_DIR, exist_ok=True)
    os.makedirs(INDEX_DIR, exist_ok=True)
    os.makedirs(ETF_DIR, exist_ok=True)


def _prefix(code: str) -> str:
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return ""


def load_universe(n: int, seed: int = 42, snapshot: str = "", resample: bool = False) -> list[str]:
    """从全市场快照抽样：主板/创业板、市值≥50亿、非 ST/退市，排除自选与持仓。

    股票池一旦生成就**冻结在 universe.json**，后续默认复用（`--resample` 才重抽）——
    否则自选/持仓一变，排除集合变化会让 240 只整体换人，历史实验无法复现。
    """
    import random

    universe_path = os.path.join(RESEARCH, "universe.json")
    if not resample and os.path.exists(universe_path):
        try:
            with open(universe_path, encoding="utf-8") as fh:
                frozen = json.load(fh)
            codes = frozen.get("codes") or []
            if codes:
                return codes
        except Exception:  # noqa: BLE001 — 文件损坏则重抽
            pass

    snapshot_dir = os.path.join(ROOT, "data", "snapshot")
    files = sorted((f for f in os.listdir(snapshot_dir) if f.endswith(".json")), reverse=True)
    if snapshot:
        files = [f for f in files if f == snapshot] or files[:1]
    rows: list[dict] = []
    for name in files[:3]:
        try:
            with open(os.path.join(snapshot_dir, name), encoding="utf-8") as fh:
                rows = json.load(fh).get("rows") or []
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            break

    exclude: set[str] = set()
    db = os.path.join(ROOT, "data", "invest.db")
    if os.path.exists(db):
        import sqlite3

        con = sqlite3.connect(db)
        for table, where in (
            ("watchlist_item", "1=1"),
            ("position", "status='open'"),
            ("paper_position", "status='open'"),
        ):
            try:
                exclude |= {r[0] for r in con.execute(f"select code from {table} where {where}")}
            except sqlite3.Error:
                pass
        con.close()

    candidates: list[str] = []
    for row in rows:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        if len(code) != 6 or not _prefix(code) or code in exclude:
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        if (row.get("market_cap") or 0) < 50:
            continue
        candidates.append(code)
    # 关键：先排序再抽样。shuffle 后切片对"排除集合"极度敏感——
    # 自选/持仓变动会让整个 240 只全部换人，实验不可复现（2026-11-04 踩过）。
    candidates = sorted(set(candidates))
    picked = sorted(random.Random(seed).sample(candidates, min(n, len(candidates))))
    with open(os.path.join(RESEARCH, "universe.json"), "w", encoding="utf-8") as fh:
        json.dump({"seed": seed, "n": len(picked), "snapshot": snapshot or files[0] if files else "",
                   "codes": picked, "excluded": sorted(exclude)},
                  fh, ensure_ascii=False, indent=1)
    return picked


def fetch_stock(code: str, force: bool = False) -> int:
    """拉取单只前复权日线并写 CSV，返回行数（已缓存则直接返回）。"""
    import akshare as ak
    import pandas as pd

    path = os.path.join(BAR_DIR, f"{code}.csv")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for _ in fh) - 1
    symbol = _prefix(code)
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=START.strftime("%Y%m%d"),
                end_date=END.strftime("%Y%m%d"),
                adjust="qfq",
            )
            if df is None or df.empty:
                raise ValueError("empty")
            out = pd.DataFrame({
                "date": pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"),
                "open": df["open"], "high": df["high"], "low": df["low"],
                "close": df["close"], "volume": df.get("volume", 0),
                "amount": df.get("amount", 0),
            })
            out.to_csv(path, index=False)
            return len(out)
        except Exception as exc:  # noqa: BLE001
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
            print(f"    retry {code}: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    return 0


def fetch_index(symbol: str, force: bool = False) -> int:
    """指数日线（不复权即可），akshare 失败则用腾讯 raw。"""
    path = os.path.join(INDEX_DIR, f"{symbol}.csv")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for _ in fh) - 1
    try:
        import akshare as ak

        df = ak.stock_zh_index_daily(symbol=symbol)
        df = df[(df["date"].astype(str) >= START.isoformat()) & (df["date"].astype(str) <= END.isoformat())]
        lines = ["date,close"] + [
            f"{row['date']},{row['close']}" for _, row in df.iterrows()
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"  akshare 指数失败 {symbol}: {exc} — 退腾讯 raw", flush=True)
        import requests

        r = requests.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"{symbol},day,{START:%Y-%m-%d},{END:%Y-%m-%d},1900,"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            timeout=20,
        )
        rows = ((r.json().get("data") or {}).get(symbol) or {}).get("day") or []
        lines = ["date,close"] + [f"{row[0][:10]},{row[2]}" for row in rows
                                 if START.isoformat() <= row[0][:10] <= END.isoformat()]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines) - 1


EXTRA_INDEXES = {
    "sh000300": "沪深300",
    "sh000905": "中证500",
    "sh000016": "上证50",
}
ETFS = {
    "sh511010": "国债ETF",
    "sh511260": "十年国债ETF",
    "sh518880": "黄金ETF",
    "sh510880": "红利ETF",
    "sh511990": "货币ETF",
}


def fetch_etf(symbol: str, force: bool = False) -> int:
    """ETF 日线（收盘价即可，用于弱市「停泊」收益）。"""
    path = os.path.join(ETF_DIR, f"{symbol}.csv")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as fh:
            return sum(1 for _ in fh) - 1
    import akshare as ak

    df = ak.fund_etf_hist_sina(symbol=symbol)
    df = df[(df["date"].astype(str) >= START.isoformat())
            & (df["date"].astype(str) <= END.isoformat())]
    lines = ["date,close"] + [f"{row['date']},{row['close']}" for _, row in df.iterrows()]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(lines) - 1


def fetch_extra_index(symbol: str, force: bool = False) -> int:
    return fetch_index(symbol, force=force)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=240, help="抽样股票数")
    ap.add_argument("--snapshot", default="", help="固定快照文件名（如 20261030.json）以冻结股票池")
    ap.add_argument("--resample", action="store_true", help="重新抽样（默认复用 universe.json）")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    ap.add_argument("--index", action="store_true", help="只更新指数")
    ap.add_argument("--aux", action="store_true", help="只补多指数与防守 ETF")
    ap.add_argument("--force", action="store_true", help="忽略缓存重拉")
    args = ap.parse_args()
    _ensure_dirs()

    for symbol in INDEXES:
        n = fetch_index(symbol, force=args.force)
        print(f"index {symbol}: {n} bars", flush=True)

    if args.index or args.aux:
        for symbol, name in EXTRA_INDEXES.items():
            try:
                print(f"index {symbol} {name}: {fetch_extra_index(symbol, args.force)} bars", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"index {symbol} {name}: FAIL {exc}", flush=True)
        for symbol, name in ETFS.items():
            try:
                print(f"etf {symbol} {name}: {fetch_etf(symbol, args.force)} bars", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"etf {symbol} {name}: FAIL {exc}", flush=True)
    if args.index or args.aux:
        return

    codes = load_universe(args.n, seed=getattr(args, "seed", 42), snapshot=args.snapshot, resample=getattr(args, "resample", False))
    print(f"universe: {len(codes)} codes (seed=42) -> {RESEARCH}", flush=True)
    ok, failed = 0, []
    began = time.time()
    for i, code in enumerate(codes, 1):
        try:
            fetch_stock(code, force=args.force)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed.append(code)
            print(f"  FAIL {code}: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
        if i % 10 == 0 or i == len(codes):
            rate = (time.time() - began) / i
            print(f"  {i}/{len(codes)} ok={ok} fail={len(failed)} "
                  f"{rate:.2f}s/只 剩 {rate*(len(codes)-i)/60:.1f}min", flush=True)
    print(f"done ok={ok} fail={len(failed)} {failed[:10]}", flush=True)


if __name__ == "__main__":
    main()
