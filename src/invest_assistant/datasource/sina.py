"""新浪财经直连客户端（批量实时行情 + 日线兜底；日线为不复权价）。"""

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
    "Referer": "https://finance.sina.com.cn",
}
_CHUNK = 400


def _normalize_sina_row(item: dict) -> dict | None:
    code = str(item.get("code") or "")
    name = str(item.get("name") or "")
    symbol = str(item.get("symbol") or "")
    if not code or not name:
        return None
    if symbol.startswith("bj"):  # v1 初筛不含北交所
        return None
    cap_wan = to_float(item.get("mktcap"))  # 万元
    return {
        "code": code,
        "name": name,
        "price": to_float(item.get("trade")),
        "pct": to_float(item.get("changepercent")),
        "amount": to_float(item.get("amount")),
        "turnover": to_float(item.get("turnoverratio")),
        "pe": to_float(item.get("per")),
        "pb": to_float(item.get("pb")),
        "market_cap": cap_wan / 1e4 if cap_wan else 0.0,  # 万元 → 亿元
        "source": "sina",
    }


class SinaClient:
    name = "sina"

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(multiplier=0.8, max=8.0),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _get_text(self, url: str, encoding: str = "gbk") -> str:
        resp = self._session.get(url, headers=_HEADERS, timeout=self._timeout)
        resp.raise_for_status()
        resp.encoding = encoding
        return resp.text

    @staticmethod
    def parse_hq_line(line: str) -> Quote | None:
        """解析 `var hq_str_sh600519="..."` 行。字段：0名称 1今开 2昨收 3现价 4最高 5最低 8量(股) 9额(元) 30日期 31时间。"""
        line = line.strip()
        if not line.startswith("var hq_str_"):
            return None
        try:
            symbol = line.split("hq_str_", 1)[1].split("=", 1)[0]
            payload = line.split('"')[1]
        except IndexError:
            return None
        if not payload:
            return None
        parts = payload.split(",")
        if len(parts) < 32:
            return None

        code = symbol[2:]
        price = to_float(parts[3])
        prev = to_float(parts[2])
        pct = ((price - prev) / prev * 100.0) if prev else 0.0
        ts = dt.datetime.now()
        try:
            ts = dt.datetime.fromisoformat(f"{parts[30]} {parts[31]}")
        except ValueError:
            pass
        return Quote(
            code=code,
            name=parts[0],
            price=price,
            prev_close=prev,
            open=to_float(parts[1]),
            high=to_float(parts[4]),
            low=to_float(parts[5]),
            pct_change=round(pct, 3),
            volume=to_float(parts[8]),
            amount=to_float(parts[9]),
            ts=ts,
            source="sina",
        )

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        codes = [normalize_code(c) for c in codes]
        if not codes:
            return {}
        out: dict[str, Quote] = {}
        errors: list[str] = []
        for i in range(0, len(codes), _CHUNK):
            chunk = codes[i : i + _CHUNK]
            url = "https://hq.sinajs.cn/list=" + ",".join(sina_symbol(c) for c in chunk)
            try:
                text = self._get_text(url)
            except Exception as exc:
                errors.append(str(exc))
                continue
            for line in text.strip().splitlines():
                quote = self.parse_hq_line(line)
                if quote:
                    out[quote.code] = quote
        if not out:
            detail = "; ".join(errors) or "无有效返回"
            raise MarketDataError(
                f"新浪行情失败: {detail}（请求 {len(codes)} 个代码；"
                "场外基金不提供盘中行情，请将持仓类型设为 otc_fund）"
            )
        if errors:
            log.warning("新浪行情部分分片失败: %s", errors)
        return out

    def get_daily_bars(self, code: str, start: dt.date, end: dt.date) -> list[Bar]:
        """日线兜底源（不复权）。datalen 上限约 1000 根。

        支持显式交易所前缀（如 sh000300 指数、sh600519），也接受 6 位代码。
        """
        raw = str(code).strip().lower()
        if len(raw) == 8 and raw[:2] in ("sh", "sz", "bj") and raw[2:].isdigit():
            symbol = raw
            code6 = raw[2:]
        else:
            code6 = normalize_code(raw)
            symbol = sina_symbol(code6)
        span = max((end - start).days + 30, 60)
        datalen = min(span, 1000)
        url = (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
        )
        try:
            text = self._get_text(url, encoding="utf-8")
        except Exception as exc:
            raise MarketDataError(f"新浪日线失败: {exc}") from exc
        import json

        try:
            rows = json.loads(text)
        except ValueError as exc:
            raise MarketDataError(f"新浪日线解析失败: {exc}") from exc
        if not isinstance(rows, list) or not rows:
            raise MarketDataError(f"新浪日线为空: {code}")
        bars: list[Bar] = []
        for row in rows:
            try:
                d = dt.date.fromisoformat(str(row["day"])[:10])
            except (KeyError, ValueError):
                continue
            if d < start or d > end:
                continue
            bars.append(
                Bar(
                    code=code6,
                    date=d,
                    open=float(row.get("open", 0) or 0),
                    high=float(row.get("high", 0) or 0),
                    low=float(row.get("low", 0) or 0),
                    close=float(row.get("close", 0) or 0),
                    volume=float(row.get("volume", 0) or 0),
                    amount=0.0,
                    adjust="raw",
                )
            )
        if not bars:
            raise MarketDataError(f"新浪日线区间为空: {symbol}")
        return bars

    # -------------------------------------------------- 全市场快照（初筛兜底源）
    def get_market_snapshot(self, max_pages: int = 60, page_size: int = 100) -> list[dict]:
        """新浪市场中心全市场快照（node=hs_a，按换手率降序分页）。"""
        import json

        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            url = (
                "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"Market_Center.getHQNodeData?page={page}&num={page_size}"
                "&sort=turnoverratio&asc=0&node=hs_a"
            )
            try:
                text = self._get_text(url, encoding="utf-8")
            except Exception as exc:
                raise MarketDataError(f"新浪快照失败(page {page}): {exc}") from exc
            text = text.strip()
            if not text or text in ("null", "[]"):
                break
            try:
                items = json.loads(text)
            except ValueError as exc:
                raise MarketDataError(f"新浪快照解析失败: {exc}") from exc
            if not isinstance(items, list) or not items:
                break
            for item in items:
                row = _normalize_sina_row(item)
                if row:
                    rows.append(row)
            if len(items) < page_size:
                break
        if not rows:
            raise MarketDataError("新浪快照为空")
        return rows
