"""交易日历：akshare（新浪源）→ 缓存到 trading_calendar 表。"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..db.models import TradingCalendar
from ..db.session import get_session_factory
from .base import MarketDataError

log = logging.getLogger(__name__)


class TradingCalendarSource:
    name = "calendar"

    def __init__(self, session_factory=None):
        self._session_factory = session_factory or get_session_factory()

    def refresh(self) -> int:
        """全量拉取并补齐缺失日期；返回新增条数。"""
        try:
            import akshare as ak

            df = ak.tool_trade_date_hist_sina()
        except Exception as exc:
            raise MarketDataError(f"交易日历获取失败: {exc}") from exc

        dates: set[dt.date] = set()
        for value in df["trade_date"]:
            if isinstance(value, dt.datetime):
                dates.add(value.date())
            elif isinstance(value, dt.date):
                dates.add(value)
            else:
                try:
                    dates.add(dt.date.fromisoformat(str(value)[:10]))
                except ValueError:
                    continue
        if not dates:
            raise MarketDataError("交易日历为空")

        with self._session_factory() as session:
            existing = {row for (row,) in session.execute(select(TradingCalendar.date))}
            new_rows = [TradingCalendar(date=d, is_open=True) for d in sorted(dates - existing)]
            session.add_all(new_rows)
            session.commit()
        return len(new_rows)

    def is_trading_day(self, day: dt.date | None = None) -> bool:
        day = day or dt.date.today()
        with self._session_factory() as session:
            row = session.get(TradingCalendar, day)
        if row is not None:
            return bool(row.is_open)
        # 缓存未覆盖（日历未刷新）：按工作日粗略回退，并提示
        log.warning("交易日历缺少 %s，暂按工作日判断（请运行日历刷新）", day)
        return day.weekday() < 5
