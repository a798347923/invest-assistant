"""数据源公共类型与代码工具。"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


class MarketDataError(Exception):
    """单一数据源失败。"""


class DataUnavailable(MarketDataError):
    """所有数据源均失败。"""


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    pct_change: float
    volume: float
    amount: float
    ts: dt.datetime
    source: str
    is_delayed: bool = False


@dataclass(frozen=True)
class Bar:
    code: str
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    adjust: str = "raw"  # raw / qfq / hfq


@dataclass(frozen=True)
class FundNav:
    code: str
    date: dt.date
    nav: float
    acc_nav: float | None = None


@dataclass(frozen=True)
class FundEstimate:
    code: str
    name: str
    est_nav: float
    est_pct: float
    ts: dt.datetime
    source: str = "fundgz"


_CODE_RE = re.compile(r"^(sh|sz|bj)?(\d{6})$", re.IGNORECASE)


def normalize_code(code: str) -> str:
    """`sh600519` / `SH600519` / `600519` → `600519`。"""
    m = _CODE_RE.match(str(code).strip().upper())
    if not m:
        raise ValueError(f"无法识别的标的代码: {code!r}")
    return m.group(2)


def exchange_of(code: str) -> str:
    """6 位代码 → sh / sz / bj。"""
    c = normalize_code(code)
    if c.startswith(("5", "6", "9")):
        return "sh"
    if c.startswith(("0", "1", "2", "3")):
        return "sz"
    if c.startswith(("4", "8")):
        return "bj"
    raise ValueError(f"无法判断交易所: {c}")


def sina_symbol(code: str) -> str:
    return f"{exchange_of(code)}{normalize_code(code)}"


def em_secid(code: str) -> str:
    market = {"sh": "1", "sz": "0", "bj": "0"}[exchange_of(code)]
    return f"{market}.{normalize_code(code)}"


def to_float(value: object) -> float:
    """宽容转换：None/`-`/空串 → 0.0。"""
    try:
        if value in (None, "-", ""):
            return 0.0
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
