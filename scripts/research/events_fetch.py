"""抓取负面事件数据（解禁 / 股东减持 / 业绩预告），按股票缓存到 data/research/events/。

三个接口都带 `ann_date`（公告日），因此可以做**时点正确**的闸门回测——
这正是实盘 `live.announcement_filter` 一直无法回测的原因（当时没有历史公告库）。

缓存格式（每只一个 CSV）：
    kind,ann_date,event_date,value,detail
    float,20250101,20250315,3.5,占流通比3.5%
    trade,20250210,20250210,-120000,减持12万股
    forecast,20250420,,0.85,预减 15%

用法：python scripts/research/events_fetch.py [--force]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import load_bars  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource.base import exchange_of  # noqa: E402
from invest_assistant.datasource.tushare import TushareClient  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
EVENT_DIR = os.path.join(RESEARCH, "events")
START = "20140101"


def ts_code(code: str) -> str:
    return f"{code}.{exchange_of(code).upper()}"


def collect(client: TushareClient, code: str) -> list[tuple]:
    """返回 (kind, ann_date, event_date, value, detail) 列表。"""
    out: list[tuple] = []
    sym = ts_code(code)

    try:
        raw = client.call(
            "share_float", {"ts_code": sym},
            "ts_code,ann_date,float_date,float_share,float_ratio,holder_name",
        ).rows
        # 接口按股东逐行返回：按解禁日聚合，否则单股东占比（0.0x%）没有意义
        agg: dict[str, dict] = {}
        for row in raw:
            ann = str(row.get("ann_date") or "")
            date = str(row.get("float_date") or "")
            if ann < START or not date:
                continue
            slot = agg.setdefault(date, {"ann": ann, "share": 0.0, "ratio": 0.0})
            slot["ann"] = min(slot["ann"], ann)
            slot["share"] += float(row.get("float_share") or 0)
            slot["ratio"] += float(row.get("float_ratio") or 0)
        for date, slot in agg.items():
            out.append((
                "float", slot["ann"], date, slot["ratio"],
                f"合计解禁 {slot['share']:.0f} 股/占流通 {slot['ratio']:.2f}%",
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"  float {code}: {exc}")

    try:
        for row in client.call(
            "stk_holdertrade", {"ts_code": sym},
            "ts_code,ann_date,holder_name,holder_type,in_de,change_vol,change_ratio,after_share",
        ).rows:
            ann = str(row.get("ann_date") or "")
            if ann < START:
                continue
            if str(row.get("in_de") or "").upper() not in ("DE", "减持"):
                continue
            vol = row.get("change_vol") or 0
            out.append((
                "trade", ann, ann,
                -float(vol) if vol else 0.0,
                f"减持 {row.get('change_vol')} 股（{row.get('holder_name') or ''}）",
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"  trade {code}: {exc}")

    try:
        for row in client.call(
            "forecast", {"ts_code": sym, "start_date": START},
            "ts_code,ann_date,end_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max",
        ).rows:
            ann = str(row.get("ann_date") or "")
            if ann < START:
                continue
            kind = str(row.get("type") or "")
            lo = row.get("p_change_min")
            hi = row.get("p_change_max")
            out.append((
                "forecast", ann, "",
                float(hi) if hi is not None else 0.0,
                f"{kind} [{lo}, {hi}]%",
            ))
    except Exception as exc:  # noqa: BLE001
        print(f"  forecast {code}: {exc}")

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    os.makedirs(EVENT_DIR, exist_ok=True)

    client = TushareClient.from_settings(get_settings())
    if not client.configured:
        print("未配置 TUSHARE_TOKEN")
        return
    codes = sorted(load_bars().keys())
    if args.limit:
        codes = codes[:args.limit]
    print(f"待抓取事件 {len(codes)} 只 -> {EVENT_DIR}")

    ok = skipped = failed = 0
    total_rows = 0
    began = time.time()
    for i, code in enumerate(codes, 1):
        path = os.path.join(EVENT_DIR, f"{code}.csv")
        if os.path.exists(path) and not args.force:
            skipped += 1
            continue
        try:
            rows = collect(client, code)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("kind,ann_date,event_date,value,detail\n")
                for kind, ann, event, value, detail in rows:
                    fh.write(f"{kind},{ann},{event},{value},{detail}\n")
            total_rows += len(rows)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {code}: {type(exc).__name__}: {str(exc)[:80]}")
        if i % 40 == 0:
            print(f"  {i}/{len(codes)} ok={ok} skip={skipped} fail={failed} "
                  f"rows={total_rows} {time.time() - began:.0f}s", flush=True)
    print(f"完成 ok={ok} skip={skipped} fail={failed} rows={total_rows} "
          f"用时 {time.time() - began:.0f}s")


if __name__ == "__main__":
    main()
