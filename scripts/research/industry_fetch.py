"""抓取申万一级行业成分，缓存到 data/research/industry.json。

映射：{股票代码: 行业名}。用于方向④的行业分散约束。
注意：`index_member` 返回的是**当前**成分，历史成分无法还原（幸存者偏差，需在报告里披露）。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.datasource.tushare import TushareClient  # noqa: E402

RESEARCH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "research")
OUT = os.path.join(RESEARCH, "industry.json")


def main() -> None:
    client = TushareClient.from_settings(get_settings())
    if not client.configured:
        print("未配置 TUSHARE_TOKEN")
        return
    industries = client.sw_industries()
    print(f"申万一级行业 {len(industries)} 个")

    mapping: dict[str, str] = {}
    index_of: dict[str, str] = {}
    for row in industries:
        name = str(row.get("industry_name") or "")
        code = str(row.get("index_code") or "")
        if not name or not code:
            continue
        try:
            members = client.sw_members(code)
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} 失败: {exc}")
            continue
        for m in members:
            con = str(m.get("con_code") or "")
            if not con:
                continue
            code6 = con.split(".")[0]
            mapping[code6] = name
            index_of[code6] = code
        print(f"  {name:8s} {len(members):5d} 只")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"industry_of": mapping, "index_of": index_of}, fh,
                  ensure_ascii=False, indent=1)
    print(f"写入 {OUT}：{len(mapping)} 只")


if __name__ == "__main__":
    main()
