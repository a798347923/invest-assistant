"""抓可转债日线（方向⑥用），按交易日增量缓存到 data/research/cb/daily.csv。

cb_daily 支持区间查询但单次上限 2000 行，因此按**单个交易日**拉取（每天约 300 行）。
约 1700 个交易日 ≈ 8~10 分钟；已抓过的日期会跳过。
"""

from __future__ import annotations

import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from study import load_index  # noqa: E402

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource.tushare import TushareClient  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
CB_DIR = os.path.join(RESEARCH, "cb")
OUT = os.path.join(CB_DIR, "daily.csv")
START, END = "2019-10-01", "2026-11-04"


def main() -> None:
    os.makedirs(CB_DIR, exist_ok=True)
    client = TushareClient.from_settings(get_settings())
    if not client.configured:
        print("未配置 TUSHARE_TOKEN")
        return

    days = [d for d, _ in load_index("sh000001") if START <= d.isoformat() <= END]
    done: set[str] = set()
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as fh:
            next(fh)
            done = {line.split(",")[1] for line in fh if line.strip()}
    todo = [d for d in days if d.strftime("%Y%m%d") not in done]
    print(f"交易日 {len(days)}，已抓 {len(done)}，待抓 {len(todo)}")

    new_file = not os.path.exists(OUT)
    began = time.time()
    with open(OUT, "a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["code", "date", "close", "vol", "amount"])
        for i, day in enumerate(todo, 1):
            try:
                rows = client.cb_daily(day.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001
                print(f"  {day} 失败: {str(exc)[:60]}")
                continue
            for row in rows:
                code = str(row.get("ts_code") or "")
                close = row.get("close")
                if not code or close is None:
                    continue
                writer.writerow([code, day.strftime("%Y%m%d"), close,
                                 row.get("vol") or "", row.get("amount") or ""])
            if i % 100 == 0:
                fh.flush()
                print(f"  {i}/{len(todo)} {time.time() - began:.0f}s", flush=True)
    print(f"完成，用时 {time.time() - began:.0f}s")


if __name__ == "__main__":
    main()
