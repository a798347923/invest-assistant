"""东方财富扩展数据（best-effort，均带超时，失败抛异常由调用方降级）：

- 个股资金流（当日/5 日主力净额、超大单，日线口径）；
- 融资融券（融资余额与环比变化、占流通市值比例）；
- 个股公告（标题 + 分类，重要事件关键词标记）。

解析函数独立为纯函数，便于测试。
"""

from __future__ import annotations

import datetime as dt
import logging

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
}
_TIMEOUT = 8.0

_FLOW_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
_FLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

_MARGIN_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"

IMPORTANT_KEYWORDS = (
    "业绩预告", "预增", "预减", "扭亏", "减持", "增持", "回购", "解禁", "停牌",
    "问询", "关注函", "立案", "诉讼", "中标", "分红", "重大合同", "风险提示",
)


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential_jitter(multiplier=0.6, max=3.0),
    retry=retry_if_exception_type(requests.RequestException),
)
def _http_get(url: str, params: dict) -> requests.Response:
    """带一次退避重试（东财主机间歇性瞬断，best-effort）。"""
    resp = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp


def _secid(code: str) -> str:
    market = "1" if code.startswith(("6", "5", "9")) else "0"
    return f"{market}.{code}"


def _parse_flow_klines(klines: list[str]) -> dict | None:
    """fflow/daykline 行格式：日期,主力净额,小单,中单,大单,超大单,占比×5。"""
    rows: list[tuple[str, float, float]] = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        try:
            rows.append((parts[0], float(parts[1]), float(parts[5])))
        except ValueError:
            continue
    if not rows:
        return None
    rows.sort(key=lambda item: item[0])
    recent = rows[-5:]
    last = recent[-1]
    return {
        "最新日期": last[0],
        "当日主力净额(亿)": round(last[1] / 1e8, 2),
        "5日主力净额(亿)": round(sum(r[1] for r in recent) / 1e8, 2),
        "当日超大单净额(亿)": round(last[2] / 1e8, 2),
    }


def _parse_margin_rows(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    latest = rows[0]
    diff = None
    if len(rows) >= 2:
        try:
            diff = (float(latest["RZYE"]) - float(rows[1]["RZYE"])) / 1e8
        except (KeyError, TypeError, ValueError):
            diff = None
    try:
        balance = float(latest["RZYE"]) / 1e8
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "日期": str(latest.get("DATE") or "")[:10],
        "融资余额(亿)": round(balance, 2),
        "较前日(亿)": round(diff, 2) if diff is not None else None,
        "占流通市值%": round(float(latest.get("RZYEZB") or 0), 2),
    }


def _parse_announcements(items: list[dict], limit: int = 8) -> list[str]:
    out: list[str] = []
    for item in items[:limit]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        day = str(item.get("notice_date") or "")[5:10]
        columns = "、".join(
            str(c.get("column_name") or "") for c in (item.get("columns") or []) if c
        )[:16]
        flag = "【重要】" if any(k in title for k in IMPORTANT_KEYWORDS) else ""
        text = f"{day} {flag}{title[:80]}"
        if columns:
            text += f"（{columns}）"
        out.append(text)
    return out


def fetch_stock_flow(code: str) -> dict | None:
    """当日/5 日主力净额（亿元）；失败抛异常。"""
    resp = _http_get(
        _FLOW_URL,
        {
            "lmt": "0",
            "klt": "101",
            "secid": _secid(code),
            "fields1": "f1,f2,f3,f7",
            "fields2": _FLOW_FIELDS2,
        },
    )
    klines = (resp.json().get("data") or {}).get("klines") or []
    result = _parse_flow_klines(klines)
    if result is None:
        raise ValueError("资金流返回为空")
    return result


def fetch_margin(code: str) -> dict | None:
    """融资余额（最近两个交易日）；失败抛异常。"""
    resp = _http_get(
        _MARGIN_URL,
        {
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "ALL",
            "filter": f'(scode="{code}")',
            "sortColumns": "date",
            "sortTypes": "-1",
            "pageSize": "2",
            "source": "WEB",
            "client": "WEB",
        },
    )
    rows = ((resp.json().get("result") or {}).get("data")) or []
    result = _parse_margin_rows(rows)
    if result is None:
        raise ValueError("两融返回为空")
    return result


def fetch_announcements(code: str, limit: int = 8) -> list[str]:
    """最近公告（接口按时间倒序返回）；失败抛异常。"""
    resp = _http_get(
        _ANN_URL,
        {
            "sr": "-1",
            "page_size": str(limit),
            "page_index": "1",
            "ann_type": "A",
            "client_source": "web",
            "stock_list": code,
            "f_node": "0",
            "s_node": "0",
        },
    )
    items = ((resp.json().get("data") or {}).get("list")) or []
    return _parse_announcements(items, limit)


# ---------------------------------------------------------------- 基本面 / 业绩预告


def _report_label(report_date) -> str:
    text = str(report_date or "")[:10]
    month = text[5:7]
    label = {"03": "一季报", "06": "中报", "09": "三季报", "12": "年报"}.get(month, text)
    year = text[:4]
    return f"{year}年{label}" if year else label


def _num(value, digits: int = 2) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _num_yi(value) -> float | None:
    number = _num(value)
    return round(number / 1e8, 2) if number is not None else None


def _parse_fundamentals(row: dict) -> dict:
    result = {
        "报告期": _report_label(row.get("REPORTDATE")),
        "公告日期": str(row.get("UPDATE_DATE") or "")[:10],
        "每股收益": _num(row.get("BASIC_EPS")),
        "营收(亿)": _num_yi(row.get("TOTAL_OPERATE_INCOME")),
        "营收同比%": _num(row.get("YSTZ")),
        "净利(亿)": _num_yi(row.get("PARENT_NETPROFIT")),
        "净利同比%": _num(row.get("SJLTZ")),
        "ROE(加权)%": _num(row.get("WEIGHTAVG_ROE")),
        "毛利率%": _num(row.get("XSMLL")),
    }
    return {key: value for key, value in result.items() if value is not None}


def _parse_forecast(row: dict) -> dict | None:
    """仅保留近 400 天内的业绩预告，避免过时数据误导。"""
    notice = str(row.get("NOTICE_DATE") or "")[:10]
    if not notice:
        return None
    try:
        if (dt.date.today() - dt.date.fromisoformat(notice)).days > 400:
            return None
    except ValueError:
        return None
    lower, upper = row.get("ADD_AMP_LOWER"), row.get("ADD_AMP_UPPER")
    amplitude = ""
    if lower is not None or upper is not None:
        amplitude = f"{lower if lower is not None else '—'}~{upper if upper is not None else '—'}"
    result = {
        "报告期": _report_label(row.get("REPORT_DATE")),
        "公告日期": notice,
        "预告类型": str(row.get("PREDICT_TYPE") or row.get("FORECAST_TYPE") or "见内容"),
        "变动幅度%": amplitude,
        "内容": str(row.get("PREDICT_CONTENT") or "")[:120],
    }
    return {key: value for key, value in result.items() if value}


def fetch_fundamentals(code: str, periods: int = 1) -> dict:
    """最近一期主要业绩指标（营收/净利/同比/ROE/毛利率 + 公告日期）；失败抛异常。"""
    resp = _http_get(
        _MARGIN_URL,
        {
            "reportName": "RPT_LICO_FN_CPD",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")',
            "sortColumns": "REPORTDATE",
            "sortTypes": "-1",
            "pageSize": str(max(periods, 1)),
            "source": "WEB",
            "client": "WEB",
        },
    )
    rows = ((resp.json().get("result") or {}).get("data")) or []
    if not rows:
        raise ValueError("业绩数据为空")
    return _parse_fundamentals(rows[0])


def fetch_profit_forecast(code: str) -> dict | None:
    """最近一期业绩预告（近 400 天内才返回）；失败/无数据抛异常或返回 None。"""
    resp = _http_get(
        _MARGIN_URL,
        {
            "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")',
            "sortColumns": "NOTICE_DATE",
            "sortTypes": "-1",
            "pageSize": "3",
            "source": "WEB",
            "client": "WEB",
        },
    )
    rows = ((resp.json().get("result") or {}).get("data")) or []
    for row in rows:
        parsed = _parse_forecast(row)
        if parsed:
            return parsed
    return None
