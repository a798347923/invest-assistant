"""东方财富直连客户端。

S1 实测：akshare 的东财接口在本机网络不可用，直连（requests + 浏览器头）稳定，
因此行情类以本客户端为主源。
"""

from __future__ import annotations

import datetime as dt
import logging

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from .base import Bar, MarketDataError, Quote, em_secid, normalize_code, to_float

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}
_QUOTE_FIELDS = "f12,f13,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18"
_SNAPSHOT_FIELDS = "f12,f14,f2,f3,f6,f8,f9,f20,f23,f100"
_ADJUST = {"raw": "0", "qfq": "1", "hfq": "2"}


def _normalize_em_row(item: dict) -> dict | None:
    code = str(item.get("f12") or "")
    name = str(item.get("f14") or "")
    if not code or not name:
        return None
    cap = to_float(item.get("f20"))  # 元
    return {
        "code": code,
        "name": name,
        "price": to_float(item.get("f2")),
        "pct": to_float(item.get("f3")),
        "amount": to_float(item.get("f6")),
        "turnover": to_float(item.get("f8")),
        "pe": to_float(item.get("f9")),
        "pb": to_float(item.get("f23")),
        "market_cap": cap / 1e8 if cap else 0.0,  # 亿元
        "industry": str(item.get("f100") or ""),
        "source": "em",
    }


class EastmoneyClient:
    name = "em"

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._timeout = timeout

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(multiplier=0.8, max=8.0),
        retry=retry_if_exception_type(requests.RequestException),
    )
    def _get_json(self, url: str) -> dict:
        resp = self._session.get(url, headers=_HEADERS, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        codes = [normalize_code(c) for c in codes]
        if not codes:
            return {}
        secids = ",".join(em_secid(c) for c in codes)
        url = (
            f"https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
            f"&secids={secids}&fields={_QUOTE_FIELDS}"
        )
        try:
            data = self._get_json(url)
        except Exception as exc:
            raise MarketDataError(f"EM 行情失败: {exc}") from exc

        diff = (data.get("data") or {}).get("diff") or []
        if isinstance(diff, dict):  # 个别网关返回按索引的字典
            diff = list(diff.values())
        now = dt.datetime.now()
        out: dict[str, Quote] = {}
        for item in diff:
            code = str(item.get("f12") or "")
            if not code:
                continue
            out[code] = Quote(
                code=code,
                name=str(item.get("f14") or ""),
                price=to_float(item.get("f2")),
                prev_close=to_float(item.get("f18")),
                open=to_float(item.get("f17")),
                high=to_float(item.get("f15")),
                low=to_float(item.get("f16")),
                pct_change=to_float(item.get("f3")),
                volume=to_float(item.get("f5")),
                amount=to_float(item.get("f6")),
                ts=now,
                source=self.name,
            )
        if not out:
            raise MarketDataError("EM 行情返回空")
        return out

    def get_daily_bars(
        self, code: str, start: dt.date, end: dt.date, adjust: str = "qfq"
    ) -> list[Bar]:
        code = normalize_code(code)
        fqt = _ADJUST.get(adjust, "1")
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={em_secid(code)}&fields1=f1,f2,f3,f4,f5,f6"
            f"&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt={fqt}"
            f"&beg={start:%Y%m%d}&end={end:%Y%m%d}&lmt=1000"
        )
        try:
            data = self._get_json(url)
        except Exception as exc:
            raise MarketDataError(f"EM 日线失败: {exc}") from exc

        klines = (data.get("data") or {}).get("klines") or []
        if not klines:
            raise MarketDataError(f"EM 日线为空: {code}")
        bars: list[Bar] = []
        for line in klines:
            p = line.split(",")
            if len(p) < 7:
                continue
            bars.append(
                Bar(
                    code=code,
                    date=dt.date.fromisoformat(p[0]),
                    open=float(p[1]),
                    close=float(p[2]),
                    high=float(p[3]),
                    low=float(p[4]),
                    volume=float(p[5]),
                    amount=float(p[6]),
                    adjust=adjust,
                )
            )
        return bars

    # -------------------------------------------------- 全市场快照（初筛用）
    def get_market_snapshot(self, max_pages: int = 60, page_size: int = 100) -> list[dict]:
        """全市场 A 股快照（沪/深主板+创业板+科创板，按成交额降序分页）。

        注意：东财 API 单页实际返回约百条（即使 pz 更大），因此按 total 逐页拉取。
        """
        rows: list[dict] = []
        total = 0
        for page in range(1, max_pages + 1):
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get"
                f"?pn={page}&pz={page_size}&po=1&np=1&fltt=2&invt=2&fid=f6"
                "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                f"&fields={_SNAPSHOT_FIELDS}"
            )
            try:
                data = self._get_json(url)
            except Exception as exc:
                raise MarketDataError(f"EM 快照失败(page {page}): {exc}") from exc
            payload = data.get("data") or {}
            total = int(payload.get("total") or total or 0)
            diff = payload.get("diff") or []
            if isinstance(diff, dict):
                diff = list(diff.values())
            if not diff:
                break
            for item in diff:
                row = _normalize_em_row(item)
                if row:
                    rows.append(row)
            if total and len(rows) >= total:
                break
        if not rows:
            raise MarketDataError("EM 快照为空")
        return rows
