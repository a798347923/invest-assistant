"""场外基金数据：净值历史（akshare/天天基金）+ 盘中估值（fundgz 直连）。"""

from __future__ import annotations

import datetime as dt
import json
import logging

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import FundEstimate, FundNav, MarketDataError, normalize_code, to_float

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
}


class FundClient:
    name = "fund"

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(multiplier=0.8, max=8.0),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _get_text(self, url: str, encoding: str = "utf-8") -> str:
        resp = self._session.get(url, headers=_HEADERS, timeout=self._timeout)
        resp.raise_for_status()
        resp.encoding = encoding
        return resp.text

    def get_nav_history(self, code: str) -> list[FundNav]:
        code = normalize_code(code)
        try:
            import akshare as ak

            df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        except Exception as exc:
            raise MarketDataError(f"基金净值失败: {exc}") from exc
        out: list[FundNav] = []
        for _, row in df.iterrows():
            raw_date = row.iloc[0]
            if isinstance(raw_date, dt.datetime):
                d = raw_date.date()
            elif isinstance(raw_date, dt.date):
                d = raw_date
            else:
                try:
                    d = dt.date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    continue
            out.append(FundNav(code=code, date=d, nav=float(row.iloc[1]), acc_nav=None))
        if not out:
            raise MarketDataError(f"基金净值为空: {code}")
        out.sort(key=lambda n: n.date)
        return out

    def get_estimates(self, codes: list[str]) -> dict[str, FundEstimate]:
        """盘中估值（仅供参考；场外基金以每日净值为准）。个别失败跳过。"""
        codes = [normalize_code(c) for c in codes]
        out: dict[str, FundEstimate] = {}
        for code in codes:
            try:
                text = self._get_text(f"http://fundgz.1234567.com.cn/js/{code}.js")
                inner = text[text.index("(") + 1 : text.rindex(")")].strip()
            except Exception as exc:
                log.warning("基金估值失败 %s: %s", code, exc)
                continue
            if not inner:
                log.info("该基金无盘中估值（估值服务未收录）: %s", code)
                continue
            try:
                payload = json.loads(inner)
            except ValueError as exc:
                log.warning("基金估值解析失败 %s: %s", code, exc)
                continue
            ts = dt.datetime.now()
            try:
                ts = dt.datetime.strptime(str(payload.get("gztime", "")), "%Y-%m-%d %H:%M")
            except ValueError:
                pass
            out[code] = FundEstimate(
                code=code,
                name=str(payload.get("name", "")),
                est_nav=to_float(payload.get("gsz")),
                est_pct=to_float(payload.get("gszzl")),
                ts=ts,
            )
        if not out:
            raise MarketDataError(f"基金估值全部失败: {codes}")
        return out
