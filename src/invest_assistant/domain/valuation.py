"""持仓估值：股票/ETF 用实时行情；场外基金用最新净值（逐项降级，不互相拖累）。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def valuate_positions(session_factory, codes_by_type: dict[str, str], data=None) -> dict:
    """返回 {"prices": {code: float}, "labels": {code: str}, "errors": [str]}。

    - 股票/ETF：一次批量行情请求；整组失败时记入 errors（供页面/CLI 提示）；
    - 场外基金：逐只用最新净值；单只失败仅记录日志（展示时显示为缺失）。
    """
    if data is None:
        from ..datasource import MarketDataService

        data = MarketDataService(session_factory=session_factory)

    stocks = [code for code, t in codes_by_type.items() if t != "otc_fund"]
    funds = [code for code, t in codes_by_type.items() if t == "otc_fund"]
    prices: dict[str, float] = {}
    labels: dict[str, str] = {}
    errors: list[str] = []

    if stocks:
        try:
            quotes = data.get_quotes(stocks)
            for code, quote in quotes.items():
                if quote.price > 0:
                    prices[code] = quote.price
                    labels[code] = "现价"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"股票行情不可用: {exc}")

    for code in funds:
        try:
            navs = data.get_fund_nav(code, days=2)
        except Exception as exc:  # noqa: BLE001
            log.warning("场外基金净值获取失败 %s: %s", code, exc)
            continue
        if navs:
            prices[code] = navs[-1].nav
            labels[code] = f"净值({navs[-1].date})"

    return {"prices": prices, "labels": labels, "errors": errors}
