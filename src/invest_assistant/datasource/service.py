"""数据源服务：统一入口 = 多源回退 + SQLite 缓存 + 重试。

调用点（引擎/报告）只与本类交互；数据源细节（EM/Sina/基金）均被封装。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import delete, select

from ..db.models import DailyBar
from ..db.models import FundNav as FundNavRow
from ..db.session import get_session_factory
from .base import Bar, DataUnavailable, FundEstimate, FundNav, MarketDataError, Quote, normalize_code
from .calendar import TradingCalendarSource
from .em import EastmoneyClient
from .em_ext import (
    fetch_announcements,
    fetch_fundamentals,
    fetch_margin,
    fetch_profit_forecast,
    fetch_stock_flow,
)
from .fund import FundClient
from .sina import SinaClient
from .tencent import TencentClient

log = logging.getLogger(__name__)


class MarketDataService:
    def __init__(
        self,
        em: EastmoneyClient | None = None,
        sina: SinaClient | None = None,
        tencent: TencentClient | None = None,
        fund: FundClient | None = None,
        calendar: TradingCalendarSource | None = None,
        session_factory=None,
    ):
        self._session_factory = session_factory or get_session_factory()
        self.em = em or EastmoneyClient()
        self.sina = sina or SinaClient()
        self.tencent = tencent or TencentClient()
        self.fund = fund or FundClient()
        self.calendar = calendar or TradingCalendarSource(self._session_factory)
        self._env_cache: tuple[dt.datetime, dict] | None = None  # 大盘环境 TTL 缓存

    # -------------------------------------------------- 实时行情
    def get_quotes(self, codes: list[str]) -> dict[str, Quote]:
        codes = [normalize_code(c) for c in codes]
        if not codes:
            return {}
        try:
            return self.em.get_quotes(codes)
        except MarketDataError as exc:
            log.warning("EM 行情失败，切换新浪: %s", exc)
        try:
            return self.sina.get_quotes(codes)
        except MarketDataError as exc:
            log.warning("新浪行情失败，切换腾讯: %s", exc)
        try:
            return self.tencent.get_quotes(codes)
        except MarketDataError as exc2:
            raise DataUnavailable(f"行情源全部失败: {exc2}") from exc2

    async def aget_quotes(self, codes: list[str]) -> dict[str, Quote]:
        return await asyncio.to_thread(self.get_quotes, codes)

    # -------------------------------------------------- 日线（缓存优先）
    def get_daily_bars(
        self,
        code: str,
        start: dt.date,
        end: dt.date,
        adjust: str = "qfq",
        force: bool = False,
    ) -> list[Bar]:
        code = normalize_code(code)
        cached = self._load_bars(code, start, end)
        end_eff = min(end, dt.date.today())
        need = force or not cached or cached[-1].date < end_eff
        if not need and cached and cached[0].date > start + dt.timedelta(days=20):
            need = self._backfill_allowed(code, start)  # 历史覆盖不足（窗口扩大）→ 补齐（同窗口 7 天内只补一次）
        if need:
            bars = self._fetch_bars(code, start, end, adjust)
            self._store_bars(code, bars, start, end)
            if bars and bars[0].date > start + dt.timedelta(days=20):
                self._mark_backfill(code, start)  # 数据源也到不了更早（新股/源上限）→ 记标记避免反复拉
            cached = self._load_bars(code, start, end)
        if not cached:
            raise DataUnavailable(f"日线不可用: {code}")
        return cached

    async def aget_daily_bars(self, *args, **kwargs) -> list[Bar]:
        return await asyncio.to_thread(self.get_daily_bars, *args, **kwargs)

    def _fetch_bars(self, code: str, start: dt.date, end: dt.date, adjust: str) -> list[Bar]:
        try:
            return self.em.get_daily_bars(code, start, end, adjust=adjust)
        except MarketDataError as exc:
            log.warning("EM 日线失败，切换腾讯（前复权）: %s", exc)
        try:
            return self.tencent.get_daily_bars(code, start, end, adjust=adjust)
        except MarketDataError as exc:
            log.warning("腾讯日线失败，切换新浪（不复权）: %s", exc)
        try:
            return self.sina.get_daily_bars(code, start, end)
        except MarketDataError as exc:
            raise DataUnavailable(f"日线源全部失败: {exc}") from exc

    def _backfill_allowed(self, code: str, start: dt.date) -> bool:
        """同代码同窗口的历史补齐 7 天内只尝试一次（避免新股/源上限导致反复拉取）。"""
        from ..db.models import KvSetting

        try:
            with self._session_factory() as session:
                row = session.get(KvSetting, f"bar_cover:{code}")
            data = dict(row.value_json or {}) if row else {}
            last_start = dt.date.fromisoformat(str(data.get("start"))) if data.get("start") else None
            last_at = dt.date.fromisoformat(str(data.get("at"))) if data.get("at") else None
        except Exception:  # noqa: BLE001
            return True
        if last_start and last_at and last_start <= start and (dt.date.today() - last_at).days < 7:
            return False
        return True

    def _mark_backfill(self, code: str, start: dt.date) -> None:
        from ..db.models import KvSetting

        try:
            with self._session_factory() as session:
                row = session.get(KvSetting, f"bar_cover:{code}")
                if row is None:
                    row = KvSetting(key=f"bar_cover:{code}")
                    session.add(row)
                row.value_json = {"start": start.isoformat(), "at": dt.date.today().isoformat()}
                session.commit()
        except Exception:  # noqa: BLE001
            pass

    def _load_bars(self, code: str, start: dt.date, end: dt.date) -> list[Bar]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(DailyBar)
                .where(DailyBar.code == code, DailyBar.date >= start, DailyBar.date <= end)
                .order_by(DailyBar.date)
            ).all()
        return [
            Bar(
                code=r.code,
                date=r.date,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                amount=r.amount,
                adjust=r.adjust,
            )
            for r in rows
        ]

    def _store_bars(self, code: str, bars: list[Bar], start: dt.date, end: dt.date) -> None:
        with self._session_factory() as session:
            session.execute(
                delete(DailyBar).where(
                    DailyBar.code == code, DailyBar.date >= start, DailyBar.date <= end
                )
            )
            session.add_all(
                [
                    DailyBar(
                        code=b.code,
                        date=b.date,
                        open=b.open,
                        high=b.high,
                        low=b.low,
                        close=b.close,
                        volume=b.volume,
                        amount=b.amount,
                        adjust=b.adjust,
                    )
                    for b in bars
                ]
            )
            session.commit()

    # -------------------------------------------------- 场外基金
    def get_fund_nav(self, code: str, days: int = 60, force: bool = False) -> list[FundNav]:
        code = normalize_code(code)
        cached = self._load_fund_nav(code)
        need = force or not cached or cached[-1].date < dt.date.today()
        if need:
            try:
                navs = self.fund.get_nav_history(code)
            except MarketDataError as exc:
                if not cached:
                    raise DataUnavailable(f"基金净值不可用: {code}") from exc
                log.warning("基金净值刷新失败，使用缓存: %s", code)
            else:
                self._replace_fund_nav(code, navs)
                cached = self._load_fund_nav(code)
        if not cached:
            raise DataUnavailable(f"基金净值不可用: {code}")
        return cached[-days:]

    def get_fund_estimates(self, codes: list[str]) -> dict[str, FundEstimate]:
        return self.fund.get_estimates([normalize_code(c) for c in codes])

    def _load_fund_nav(self, code: str) -> list[FundNav]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(FundNavRow).where(FundNavRow.code == code).order_by(FundNavRow.date)
            ).all()
        return [FundNav(code=r.code, date=r.date, nav=r.nav, acc_nav=r.acc_nav) for r in rows]

    def _replace_fund_nav(self, code: str, navs: list[FundNav]) -> None:
        with self._session_factory() as session:
            session.execute(delete(FundNavRow).where(FundNavRow.code == code))
            session.add_all(
                [FundNavRow(code=n.code, date=n.date, nav=n.nav, acc_nav=n.acc_nav) for n in navs]
            )
            session.commit()

    # -------------------------------------------------- 资金面 / 两融 / 公告 / 大盘环境（best-effort）

    def get_stock_flow(self, code: str) -> dict | None:
        """个股资金流（当日/5 日主力净额，亿元）；失败返回 None。"""
        try:
            return fetch_stock_flow(normalize_code(code))
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.debug("资金流获取失败 %s: %s", code, exc)
            return None

    def get_margin(self, code: str) -> dict | None:
        """融资融券余额（亿元）；失败返回 None。"""
        try:
            return fetch_margin(normalize_code(code))
        except Exception as exc:  # noqa: BLE001
            log.debug("两融获取失败 %s: %s", code, exc)
            return None

    def get_announcements(self, code: str, limit: int = 8) -> list[str]:
        """最近公告（重要事件带【重要】标记）；失败返回 []。"""
        try:
            return fetch_announcements(normalize_code(code), limit)
        except Exception as exc:  # noqa: BLE001
            log.debug("公告获取失败 %s: %s", code, exc)
            return []

    # ---- 基本面 / 估值 / 行业（第一批与第二批数据增强） ----

    def is_stock_code(self, code: str) -> bool:
        """A 股个股（优先查 Instrument 表；未知代码按代码段推断，ETF/基金段排除）。"""
        code = normalize_code(code)
        try:
            from ..db.models import Instrument

            with self._session_factory() as session:
                instrument = session.get(Instrument, code)
            if instrument is not None and instrument.type:
                return instrument.type == "a_share"
        except Exception:  # noqa: BLE001 — 表不可用时退回代码段推断
            pass
        if code[:1] not in ("0", "3", "6"):
            return False
        return not code.startswith(("51", "56", "58", "15", "16"))

    def get_fundamentals(self, code: str) -> dict | None:
        """最近一期业绩关键指标；失败返回 None。"""
        if not self.is_stock_code(code):
            return None
        try:
            return fetch_fundamentals(normalize_code(code))
        except Exception as exc:  # noqa: BLE001
            log.debug("基本面获取失败 %s: %s", code, exc)
            return None

    def get_profit_forecast(self, code: str) -> dict | None:
        """最近 400 天内的业绩预告；无/失败返回 None。"""
        if not self.is_stock_code(code):
            return None
        try:
            return fetch_profit_forecast(normalize_code(code))
        except Exception as exc:  # noqa: BLE001
            log.debug("业绩预告获取失败 %s: %s", code, exc)
            return None

    def _latest_snapshot(self) -> tuple[list[dict] | None, str]:
        """最近一份全市场快照缓存（不触发抓取）；返回 (行, YYYYMMDD)。"""
        import json

        from ..config import get_settings

        cache_dir = get_settings().data_dir / "snapshot"
        if not cache_dir.exists():
            return None, ""
        for path in sorted(cache_dir.glob("*.json"), reverse=True)[:5]:
            try:
                rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
            except Exception:  # noqa: BLE001
                continue
            if rows:
                return rows, path.stem
        return None, ""

    @staticmethod
    def _date_label(stamp: str) -> str:
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else stamp

    def get_valuation(self, code: str) -> dict | None:
        """估值（快照缓存：PE/PB/换手/总市值）；无缓存返回 None。"""
        rows, stamp = self._latest_snapshot()
        if not rows:
            return None
        code = normalize_code(code)
        target = next((r for r in rows if r.get("code") == code), None)
        if target is None:
            return None
        return {
            "数据日期": self._date_label(stamp),
            "PE": target.get("pe") or None,
            "PB": target.get("pb") or None,
            "换手%": round(target.get("turnover") or 0.0, 2),
            "总市值(亿)": round(target.get("market_cap") or 0.0, 0),
        }

    def _industry_history_path(self):
        from ..config import get_settings

        return get_settings().data_dir / "industry_history.json"

    def _industry_history(self) -> dict:
        import json

        try:
            return json.loads(self._industry_history_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _industry_update_history(self, history: dict, rows: list[dict], stamp: str) -> None:
        import json

        if not stamp or stamp in history:
            return
        buckets: dict[str, list[float]] = {}
        for row in rows:
            industry = row.get("industry") or ""
            if not industry:
                continue
            buckets.setdefault(industry, []).append(row.get("pct") or 0.0)
        history[stamp] = {
            name: round(sum(values) / len(values), 4)
            for name, values in buckets.items()
            if values
        }
        for key in sorted(history)[:-40]:  # 只留最近 40 个快照日
            history.pop(key, None)
        try:
            self._industry_history_path().write_text(
                json.dumps(history, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("行业历史写入失败: %s", exc)

    def get_industry_snapshot(self, code: str, window_days: tuple[int, ...] = (5, 20)) -> dict | None:
        """行业联动（基于全市场快照缓存）：行业名称、当日行业均涨跌、涨跌家数、成交额与近期累计。

        不触发网络抓取；行业历史随每次调用逐日积累（data/industry_history.json）。
        """
        rows, stamp = self._latest_snapshot()
        if not rows:
            return None
        code = normalize_code(code)
        target = next((r for r in rows if r.get("code") == code), None)
        if target is None:
            return None
        industry = str(target.get("industry") or "")
        if not industry:
            return None
        members = [r for r in rows if r.get("industry") == industry]
        if not members:
            return None
        pcts = [r.get("pct") or 0.0 for r in members]
        item = {
            "行业": industry,
            "数据日期": self._date_label(stamp),
            "当日行业均涨跌%": round(sum(pcts) / len(pcts), 2),
            "行业上涨/下跌家数": f"{sum(1 for p in pcts if p > 0)}/{sum(1 for p in pcts if p < 0)}",
            "行业内涨停家数": sum(1 for p in pcts if p >= 9.8),
            "行业成交额(亿)": round(sum(r.get("amount") or 0.0 for r in members) / 1e8, 0),
        }
        history = self._industry_history()
        self._industry_update_history(history, rows, stamp)
        ordered = sorted(history)
        for window in window_days:
            values = [
                history[key][industry]
                for key in ordered[-window:]
                if industry in (history.get(key) or {})
            ]
            if values:
                item[f"近{window}个快照日累计%"] = round(sum(values), 2)
        return item

    def get_market_env(self, ttl_minutes: int = 5, force: bool = False) -> dict:
        """大盘环境：四大指数（现价/涨跌/成交额 + 20 日趋势位置）+ 两市成交额 + 情绪（当日快照）。

        5 分钟内存缓存（批量分析只取一次）；全部 best-effort，返回可能为空 dict。
        """
        now = dt.datetime.now()
        if (
            not force
            and self._env_cache is not None
            and now - self._env_cache[0] < dt.timedelta(minutes=ttl_minutes)
        ):
            return dict(self._env_cache[1])

        env: dict = {}
        index_symbols = {
            "sh000001": "上证指数",
            "sz399001": "深证成指",
            "sz399006": "创业板指",
            "sh000688": "科创50",
        }
        indices: dict = {}
        try:
            quotes = self.tencent.get_quotes(list(index_symbols))
        except Exception as exc:  # noqa: BLE001
            log.debug("指数行情失败: %s", exc)
            quotes = {}
        amount_total = 0.0
        for symbol, label in index_symbols.items():
            code = symbol[2:]
            quote = quotes.get(code)
            if quote is None or quote.price <= 0:
                continue
            amount_total += quote.amount or 0.0
            item = {"点位": quote.price, "涨跌%": round(quote.pct_change or 0.0, 2)}
            try:
                end = now.date()
                bars = self.tencent.get_daily_bars(
                    symbol, end - dt.timedelta(days=60), end, adjust="raw"
                )
                closes = [b.close for b in bars]
                if len(closes) >= 20:
                    ma20 = sum(closes[-20:]) / 20
                    item["20日均线"] = round(ma20, 2)
                    item["相对20日线%"] = round((quote.price / ma20 - 1) * 100, 2)
                if len(closes) >= 21 and closes[-21]:
                    item["近20日%"] = round((closes[-1] / closes[-21] - 1) * 100, 2)
            except Exception:  # noqa: BLE001 — 趋势缺失不影响行情部分
                pass
            indices[label] = item
        if indices:
            env["指数"] = indices
        if amount_total:
            env["两市成交额(亿)"] = round(amount_total / 1e8, 0)

        # 情绪：若当日全市场快照已生成（18:30 任务），直接复用，不触发全量抓取
        try:
            from ..config import get_settings

            cache_file = get_settings().data_dir / "snapshot" / f"{now:%Y%m%d}.json"
            if cache_file.exists():
                import json

                rows = json.loads(cache_file.read_text(encoding="utf-8")).get("rows") or []
                if rows:
                    up = sum(1 for r in rows if (r.get("pct") or 0) > 0)
                    down = sum(1 for r in rows if (r.get("pct") or 0) < 0)
                    limit_up = sum(1 for r in rows if (r.get("pct") or 0) >= 9.8)
                    limit_down = sum(1 for r in rows if (r.get("pct") or 0) <= -9.8)
                    env["情绪"] = {
                        "快照日期": f"{now:%Y-%m-%d}",
                        "上涨家数": up,
                        "下跌家数": down,
                        "涨停约": limit_up,
                        "跌停约": limit_down,
                    }
        except Exception as exc:  # noqa: BLE001
            log.debug("情绪快照读取失败: %s", exc)

        if env:
            env["数据时间"] = now.strftime("%Y-%m-%d %H:%M")
        self._env_cache = (now, env)
        return dict(env)

    def market_regime_state(self, band_pct: float | None = None) -> str:
        """市场强弱状态（强化判定）：上证 + 创业板指各相对自身 MA20，±band 滞回带。

        - strong→weak：任一指数跌破 MA20×(1−band)；
        - weak→strong：两个指数都回到 MA20×(1+band) 之上；
        - 状态持久化于 kv（"market_regime"），避免均线附近来回翻转；
        - 数据缺失返回 ""（调用方按"未知"处理，不调整行为）。
        """
        from ..config import get_settings
        from ..db.models import KvSetting

        if band_pct is None:
            market_cfg = get_settings().app_config().get("market") or {}
            try:
                band_pct = float(market_cfg.get("regime_band_pct", 1.0) or 1.0)
            except (TypeError, ValueError):
                band_pct = 1.0
        try:
            env = self.get_market_env()
        except Exception:  # noqa: BLE001 — 环境数据 best-effort
            env = {}
        indices = env.get("指数") or {}
        rels: list[float] = []
        for name in ("上证指数", "创业板指"):
            rel = (indices.get(name) or {}).get("相对20日线%")
            if rel is not None:
                rels.append(float(rel))
        if not rels:
            return ""
        with self._session_factory() as session:
            row = session.get(KvSetting, "market_regime")
            state = str((row.value_json or {}).get("state") or "") if row else ""
        if state not in ("strong", "weak"):
            state = "strong" if all(r >= 0 for r in rels) else "weak"
        elif state == "strong":
            if any(r < -band_pct for r in rels):
                state = "weak"
        else:  # weak
            if all(r > band_pct for r in rels):
                state = "strong"
        with self._session_factory() as session:
            row = session.get(KvSetting, "market_regime")
            if row is None:
                row = KvSetting(key="market_regime")
                session.add(row)
            row.value_json = {
                "state": state,
                "at": dt.datetime.now().isoformat(timespec="seconds"),
                "rel": [round(r, 2) for r in rels],
            }
            session.commit()
        return state

    # -------------------------------------------------- 交易日历
    def is_trading_day(self, day: dt.date | None = None) -> bool:
        return self.calendar.is_trading_day(day)

    def refresh_calendar(self) -> int:
        return self.calendar.refresh()

    # -------------------------------------------------- 全市场快照（每日缓存）
    def get_market_snapshot(self, force: bool = False, max_pages: int = 60) -> tuple[list[dict], str]:
        """返回（归一化行, 数据源）；当日结果缓存到 data/snapshot/YYYYMMDD.json。"""
        import json

        from ..config import get_settings

        settings = get_settings()
        cache_dir = settings.data_dir / "snapshot"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{dt.date.today():%Y%m%d}.json"
        if cache_file.exists() and not force:
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                rows = payload.get("rows") or []
                if rows:
                    return rows, f"{payload.get('source', 'cache')}(cache)"
            except Exception:  # noqa: BLE001 — 缓存损坏则重新抓取
                pass

        source = "em"
        try:
            rows = self.em.get_market_snapshot(max_pages=max_pages)
        except MarketDataError as exc:
            log.warning("EM 快照失败，切换新浪: %s", exc)
            source = "sina"
            try:
                rows = self.sina.get_market_snapshot(max_pages=max_pages)
            except MarketDataError as exc2:
                raise DataUnavailable(f"快照源全部失败: {exc2}") from exc2

        cache_file.write_text(
            json.dumps({"rows": rows, "source": source}, ensure_ascii=False), encoding="utf-8"
        )
        return rows, source
