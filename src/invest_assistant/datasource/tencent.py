"""腾讯行情直连（兜底源 + 指数）：批量实时行情 + 前复权日线（含指数）。

- 行情：https://qt.gtimg.cn/q=sh600519,sz000001（GBK 文本，~ 分隔）
- 日线：https://web.ifzq.gtimg.cn/appstock/app/fqkline/get（qfqday/hfqday/day）
- 支持显式前缀符号（sh000001 等指数），与新浪一致。

字段映射（行情，0 基）：[1]名称 [3]现价 [4]昨收 [5]今开 [6]量(手)
[30]时间 [31]涨跌额 [32]涨跌% [33]最高 [34]最低 [37]成交额(万)
"""

from __future__ import annotations

import datetime as dt
import logging

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import Bar, MarketDataError, Quote, normalize_code, sina_symbol, to_float

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://gu.qq.com/",
}
_CHUNK = 400
_FQ_KEY = {"qfq": "qfqday", "hfq": "hfqday", "raw": "day"}


def _symbol(code: str) -> str:
    """sh600519 / sz000001（显式前缀原样使用，6 位代码按交易所推断）。"""
    raw = str(code).strip().lower()
    if len(raw) == 8 and raw[:2] in ("sh", "sz", "bj") and raw[2:].isdigit():
        return raw
    return sina_symbol(normalize_code(raw))


def _parse_kline_payload(
    payload: dict, code6: str, start: dt.date, end: dt.date, adjust: str
) -> list[Bar]:
    """从 fqkline 响应中解析目标区间的日线（volume 单位：手）。"""
    node = (payload.get("data") or {}).get(_symbol(code6)) or {}
    key = _FQ_KEY.get(adjust, "qfqday")
    rows = node.get(key) or node.get("day") or []
    bars: list[Bar] = []
    for row in rows:
        try:
            day = dt.date.fromisoformat(str(row[0])[:10])
        except (IndexError, ValueError):
            continue
        if day < start or day > end:
            continue
        try:
            bars.append(
                Bar(
                    code=code6[2:] if len(code6) == 8 else code6,
                    date=day,
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 else 0.0,
                    amount=0.0,
                    adjust=adjust,
                )
            )
        except (IndexError, ValueError, TypeError):
            continue
    return bars


class TencentClient:
    name = "tencent"

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(multiplier=0.8, max=8.0),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _get(self, url: str) -> requests.Response:
        resp = self._session.get(url, headers=_HEADERS, timeout=self._timeout)
        resp.raise_for_status()
        return resp

    @staticmethod
    def parse_quote_line(line: str) -> Quote | None:
        """解析 `v_sh600519="1~贵州茅台~600519~..."` 行。"""
        line = line.strip()
        if not line.startswith("v_"):
            return None
        try:
            symbol = line.split("=", 1)[0][2:]
            fields = line.split('="', 1)[1].rstrip('";').split("~")
        except IndexError:
            return None
        if len(fields) < 38:
            return None
        code = symbol[2:]
        ts = dt.datetime.now()
        try:
            ts = dt.datetime.strptime(fields[30], "%Y%m%d%H%M%S")
        except (ValueError, IndexError):
            pass
        return Quote(
            code=code,
            name=fields[1],
            price=to_float(fields[3]),
            prev_close=to_float(fields[4]),
            open=to_float(fields[5]),
            high=to_float(fields[33]),
            low=to_float(fields[34]),
            pct_change=to_float(fields[32]),
            volume=to_float(fields[6]) * 100,  # 手 → 股（与新浪口径一致）
            amount=to_float(fields[37]) * 1e4,  # 万元 → 元
            ts=ts,
            source="tencent",
        )

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        """codes 支持 6 位代码或显式前缀符号（指数）。"""
        if not codes:
            return {}
        out: dict[str, Quote] = {}
        errors: list[str] = []
        for i in range(0, len(codes), _CHUNK):
            chunk = codes[i : i + _CHUNK]
            url = "https://qt.gtimg.cn/q=" + ",".join(_symbol(c) for c in chunk)
            try:
                resp = self._get(url)
                text = resp.content.decode("gbk", errors="replace")
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                continue
            for line in text.strip().split(";"):
                quote = self.parse_quote_line(line)
                if quote and quote.price > 0:
                    out[quote.code] = quote
        if not out:
            detail = "; ".join(errors) or "无有效返回"
            raise MarketDataError(f"腾讯行情失败: {detail}")
        return out

    def get_daily_bars(
        self, code: str, start: dt.date, end: dt.date, adjust: str = "qfq"
    ) -> list[Bar]:
        """日线（qfq 前复权 / hfq / raw 不复权；指数用 raw）。"""
        symbol = _symbol(code)
        span = max((end - start).days + 20, 60)
        fq = "" if adjust == "raw" else adjust
        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        try:
            resp = self._get(
                f"{url}?param={symbol},day,{start:%Y-%m-%d},{end:%Y-%m-%d},{span},{fq}"
            )
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"腾讯日线失败: {exc}") from exc
        bars = _parse_kline_payload(payload, symbol, start, end, adjust)
        if not bars:
            raise MarketDataError(f"腾讯日线为空: {symbol}")
        return bars
