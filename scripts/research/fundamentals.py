"""拉取 Tushare 估值/换手数据（研究用，缓存到 data/research/fund/）。

只拉 `daily_basic`：PE_TTM / PB / 换手率 / 总市值，按股票缓存为 CSV。
频次：5000 积分 = 500 次/分钟；单只一次调用，236 只约 1 分钟。

用法：
    python scripts/research/fundamentals.py            # 增量拉取（跳过已有文件）
    python scripts/research/fundamentals.py --force    # 全部重拉
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
FUND_DIR = os.path.join(RESEARCH, "fund")
START, END = "20050101", "20261231"


def ts_code(code: str) -> str:
    return f"{code}.{exchange_of(code).upper()}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    os.makedirs(FUND_DIR, exist_ok=True)

    settings = get_settings()
    client = TushareClient.from_settings(settings)
    if not client.configured:
        print("未配置 TUSHARE_TOKEN")
        return

    codes = sorted(load_bars().keys())
    if args.limit:
        codes = codes[: args.limit]
    print(f"待拉取 {len(codes)} 只 -> {FUND_DIR}")

    ok = skipped = failed = 0
    began = time.time()
    for i, code in enumerate(codes, 1):
        path = os.path.join(FUND_DIR, f"{code}.csv")
        if os.path.exists(path) and not args.force:
            skipped += 1
            continue
        try:
            rows = client.daily_basic(ts_code(code), START, END)
            if not rows:
                skipped += 1
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("date,pe_ttm,pb,turnover_rate,total_mv\n")
                for row in rows:
                    fh.write(
                        f"{row['trade_date']},{row.get('pe_ttm') or ''},{row.get('pb') or ''},"
                        f"{row.get('turnover_rate') or ''},{row.get('total_mv') or ''}\n"
                    )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {code}: {type(exc).__name__}: {str(exc)[:80]}")
        if i % 25 == 0:
            print(f"  {i}/{len(codes)} ok={ok} skip={skipped} fail={failed} "
                  f"{time.time() - began:.0f}s", flush=True)
    print(f"完成 ok={ok} skip={skipped} fail={failed}，用时 {time.time() - began:.0f}s")


if __name__ == "__main__":
    main()
