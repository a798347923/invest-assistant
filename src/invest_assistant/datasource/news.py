"""个股新闻（东方财富搜索接口，自带 10s 超时）。

替代 akshare.stock_news_em：其内部 requests 调用无超时，东方财富抖动时会挂死请求线程
（2026-10-15 事故教训）。失败返回空列表，绝不抛异常、绝不阻塞。
"""

from __future__ import annotations

import json
import logging
import re

import requests

log = logging.getLogger(__name__)

NEWS_URL = "https://search-api-web.eastmoney.com/search/jsonp"


def fetch_stock_news(code: str, limit: int = 8) -> list[str]:
    """返回 [「时间 标题（媒体）」]（limit 条；失败返回 []）。"""
    param = {
        "uid": "",
        "keyword": code,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": 1,
                "pageSize": limit,
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    try:
        resp = requests.get(
            NEWS_URL,
            params={"cb": "jsonp", "param": json.dumps(param, ensure_ascii=False)},
            headers={"referer": "https://so.eastmoney.com/", "user-agent": "Mozilla/5.0"},
            timeout=10.0,
        )
        text = resp.text
        items = json.loads(text[text.index("(") + 1 : text.rindex(")")])["result"][
            "cmsArticleWebOld"
        ]
        out: list[str] = []
        for item in items[:limit]:
            title = re.sub(r"</?em>", "", str(item.get("title", "")))[:60]
            stamp = str(item.get("date", ""))[:16]
            media = str(item.get("mediaName", ""))
            suffix = f"（{media}）" if media else ""
            out.append(f"{stamp} {title}{suffix}")
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("新闻获取失败 %s: %s", code, exc)
        return []
