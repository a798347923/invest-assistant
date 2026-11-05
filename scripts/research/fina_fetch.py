"""拉取 Tushare 时点财务（fina_indicator），按股票缓存到 data/research/fina/。

为什么用 ann_date：财务数据必须按**公告日**生效，用报告期（end_date）会引入未来函数。
每行：ann_date,end_date,roe,netprofit_yoy,grossprofit_margin

用法：python scripts/research/fundamentals.py --fina [--force]
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
FINA_DIR = os.path.join(RESEARCH, "fina")
START = "20100101"


def ts_code(code: str) -> str:
    return f"{code}.{exchange_of(code).upper()}"


def fetch_fina(force: bool = False, limit: int = 0) -> None:
    os.makedirs(FINA_DIR, exist_ok=True)
    client = TushareClient.from_settings(get_settings())
    if not client.configured:
        print("未配置 TUSHARE_TOKEN")
        return
    codes = sorted(load_bars().keys())
    if limit:
        codes = codes[:limit]
    print(f"待拉取财务 {len(codes)} 只 -> {FINA_DIR}")

    ok = skipped = failed = 0
    began = time.time()
    for i, code in enumerate(codes, 1):
        path = os.path.join(FINA_DIR, f"{code}.csv")
        if os.path.exists(path) and not force:
            skipped += 1
            continue
        try:
            rows = client.fina_indicator(ts_code(code), START)
            rows = [r for r in rows if r.get("ann_date")]
            if not rows:
                skipped += 1
                continue
            rows.sort(key=lambda r: str(r["ann_date"]))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("ann_date,end_date,roe,netprofit_yoy,grossprofit_margin\n")
                for row in rows:
                    fh.write(
                        f"{row['ann_date']},{row.get('end_date') or ''},"
                        f"{row.get('roe') or ''},{row.get('netprofit_yoy') or ''},"
                        f"{row.get('grossprofit_margin') or ''}\n"
                    )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {code}: {type(exc).__name__}: {str(exc)[:80]}")
        if i % 50 == 0:
            print(f"  {i}/{len(codes)} ok={ok} skip={skipped} fail={failed} "
                  f"{time.time() - began:.0f}s", flush=True)
    print(f"完成 ok={ok} skip={skipped} fail={failed}，用时 {time.time() - began:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fina", action="store_true", help="拉时点财务（fina_indicator）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    if args.fina:
        fetch_fina(args.force, args.limit)
        return
    print("请指定 --fina（估值/换手用脚本内的默认流程：直接运行本文件即拉 daily_basic）")


if __name__ == "__main__":
    main()
