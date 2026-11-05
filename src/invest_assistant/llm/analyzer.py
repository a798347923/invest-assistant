"""分析器：触发解读（B2-05）+ 标的分析卡（含具体操作计划）。

分析卡（B3 升级）：
- 上下文含「指标摘要」（MA5/10/20/60、近 20/60 日区间、近 5/20 日涨跌、量比），
  让模型的价位判断有数据依据；
- 输出结构化 `plan`：买入区间 / 确认条件 / 失效价 / 参考目标 / 仓位建议；
- 提示词硬约束：禁止空泛表述（「建议观察」等），不适合买入必须给出等待条件。
"""

from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel, field_validator

from ..db.session import get_session_factory
from .client import LLMClient, LLMError
from .prompts import CARD_V3_SYSTEM, TRIGGER_V1_SYSTEM, WATCH_V1_SYSTEM, card_user, trigger_user
from .usage import record_usage

log = logging.getLogger(__name__)


class TriggerRead(BaseModel):
    what_happened: str
    position_status: str = ""
    options: list[str] = []
    suggestion: str = ""
    confidence: float = 0.5


class TriggerInterpreter:
    def __init__(self, llm=None, data=None, session_factory=None):
        self._sf = session_factory or get_session_factory()
        self.llm = llm or LLMClient()
        self._data = data

    def _data_service(self):
        if self._data is None:
            from ..datasource import MarketDataService

            self._data = MarketDataService(session_factory=self._sf)
        return self._data

    def interpret(self, context: dict, kind: str = "trigger") -> dict | None:
        """生成解读（kind=trigger 持仓触发 / kind=watch 自选提醒）；失败返回 None。"""
        if not self.llm.configured:
            return None
        bars = self._recent_bars(context.get("code"))
        is_watch = kind == "watch"
        system = WATCH_V1_SYSTEM if is_watch else TRIGGER_V1_SYSTEM
        purpose = "watch" if is_watch else "trigger"
        try:
            result = self.llm.complete_json(
                purpose=purpose,
                system=system,
                user=trigger_user(context, bars),
                schema=TriggerRead,
                max_tokens=600,
            )
        except LLMError as exc:
            log.warning("解读失败（kind=%s，降级模板消息）: %s", kind, exc)
            return None
        record_usage(
            self._sf,
            purpose=purpose,
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cached_in=result.cached_in,
        )
        return result.content

    def _recent_bars(self, code: str | None) -> list[str]:
        if not code:
            return []
        try:
            end = dt.date.today()
            start = end - dt.timedelta(days=20)
            bars = self._data_service().get_daily_bars(code, start, end)
            return [f"{b.date} 收{b.close}" for b in bars[-5:]]
        except Exception as exc:  # noqa: BLE001 — 行情失败不阻塞解读
            log.warning("解读取行情失败: %s", exc)
            return []


class CardPlan(BaseModel):
    entry_type: str = ""  # 回踩 / 突破 / 现价
    entry_low: float | None = None
    entry_high: float | None = None
    breakout_price: float | None = None
    stop_price: float | None = None
    entry_zone: str = ""
    triggers: list[str] = []
    confirm: list[str] = []  # 程序自动买入前逐条校验的确认条件（枚举，见 domain/conditions.py）
    stop_level: str = ""
    targets: list[str] = []
    position_hint: str = ""

    @field_validator("entry_type", "entry_zone", "stop_level", "position_hint", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)

    @field_validator("triggers", "confirm", "targets", mode="before")
    @classmethod
    def _coerce_str_list(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        return [str(value)]  # 单值（数字/字符串）容错


class CardRead(BaseModel):
    summary: str
    technicals: str = ""
    news_sentiment: str = ""
    bull_points: list[str] = []
    bear_points: list[str] = []
    risks: list[str] = []
    action_bias: str = "持有观察"
    plan: CardPlan | None = None
    confidence: float = 0.5
    data_caveats: str = ""


class CardAnalyzer:
    """标的分析卡：行情指标 + 新闻 → LLM → 入库（含具体操作计划）。"""

    def __init__(self, llm=None, data=None, news_fetcher=None, session_factory=None):
        self._sf = session_factory or get_session_factory()
        self.llm = llm or LLMClient()
        self._data = data
        self._news_fetcher = news_fetcher  # 测试注入；默认走 akshare

    def _data_service(self):
        if self._data is None:
            from ..datasource import MarketDataService

            self._data = MarketDataService(session_factory=self._sf)
        return self._data

    def _news(self, code: str, limit: int = 8) -> list[str]:
        """个股新闻（东财搜索接口，自带 10s 超时；失败返回空列表，不阻塞分析）。"""
        if self._news_fetcher is not None:
            return self._news_fetcher(code)[:limit]
        from ..datasource.news import fetch_stock_news

        return fetch_stock_news(code, limit)

    @staticmethod
    def _technical_summary(bars: list) -> str:
        closes = [b.close for b in bars]
        vols = [b.volume for b in bars]
        parts: list[str] = []
        for n in (5, 10, 20, 60):
            if len(closes) >= n:
                parts.append(f"MA{n}={sum(closes[-n:]) / n:.3f}")
        if len(closes) >= 21 and closes[-21]:
            parts.append(f"近20日涨跌 {(closes[-1] / closes[-21] - 1) * 100:+.2f}%")
        if len(closes) >= 6 and closes[-6]:
            parts.append(f"近5日涨跌 {(closes[-1] / closes[-6] - 1) * 100:+.2f}%")
        if bars:
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            parts.append(f"近20日区间 {min(lows[-20:]):.3f}~{max(highs[-20:]):.3f}")
            window = min(len(bars), 60)
            parts.append(f"近{window}日区间 {min(lows[-window:]):.3f}~{max(highs[-window:]):.3f}")
        if len(vols) >= 20 and sum(vols[-20:]):
            vol5 = sum(vols[-5:]) / 5
            vol20 = sum(vols[-20:]) / 20
            if vol20:
                parts.append(f"5日均量/20日均量={vol5 / vol20:.2f}")
        return "指标摘要：" + "；".join(parts)

    def _market_extras(self, code: str, type: str = "") -> dict:
        """大盘环境 / 资金面 / 两融 / 公告 / 基本面 / 估值 / 行业（best-effort，缺失不注入）。"""
        ds = self._data_service()
        extras: dict = {}
        try:
            env = ds.get_market_env()
            if env:
                extras["大盘环境"] = env
        except Exception:  # noqa: BLE001
            pass
        if type != "otc_fund":
            try:
                flow = ds.get_stock_flow(code)
                if flow:
                    extras["资金面"] = flow
            except Exception:  # noqa: BLE001
                pass
            try:
                margin = ds.get_margin(code)
                if margin:
                    extras["融资融券"] = margin
            except Exception:  # noqa: BLE001
                pass
            try:
                fundamentals = ds.get_fundamentals(code)
                if fundamentals:
                    extras["基本面"] = fundamentals
            except Exception:  # noqa: BLE001
                pass
            try:
                forecast = ds.get_profit_forecast(code)
                if forecast:
                    extras["业绩预告"] = forecast
            except Exception:  # noqa: BLE001
                pass
            try:
                industry = ds.get_industry_snapshot(code)
                if industry:
                    extras["行业"] = industry
            except Exception:  # noqa: BLE001
                pass
            try:
                valuation = ds.get_valuation(code)
                if valuation:
                    extras["估值"] = valuation
            except Exception:  # noqa: BLE001
                pass
        try:
            announcements = ds.get_announcements(code)
            if announcements:
                extras["公告"] = announcements
        except Exception:  # noqa: BLE001
            pass
        try:
            from ..engine.calibration import calibration_notes

            notes = calibration_notes(self._sf)
            if notes:
                extras["预测校准结论"] = notes
        except Exception:  # noqa: BLE001
            pass
        return extras

    def _record_prediction(self, session, card_id: int, code: str, type: str, content: dict, extras: dict) -> None:
        """卡生成时记录「预测」快照（供每周校准评估）。"""
        if type == "otc_fund":
            return
        end = dt.date.today()
        bars = self._data_service().get_daily_bars(
            code, end - dt.timedelta(days=1100), end, adjust="qfq"
        )
        closes = [b.close for b in bars]
        if not closes:
            return
        from ..db.models import Prediction
        from ..domain.indicators import percentile_rank
        from ..domain.plan import parse_plan_levels

        plan = parse_plan_levels(content) or {}
        regime = ""
        sh = ((extras or {}).get("大盘环境") or {}).get("指数", {}).get("上证指数") or {}
        rel = sh.get("相对20日线%")
        if rel is not None:
            regime = "指数≥20日线" if rel >= 0 else "指数<20日线"
        trend = ""
        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20
            trend = "多头(>MA20)" if closes[-1] >= ma20 else "空头(<MA20)"
        pct = percentile_rank(closes, closes[-1]) if len(closes) >= 60 else None
        session.add(
            Prediction(
                date=end,
                code=code,
                card_id=card_id,
                action=str((content or {}).get("action_bias") or ""),
                price=closes[-1],
                zone_low=plan.get("zone_low"),
                zone_high=plan.get("zone_high"),
                breakout=plan.get("breakout"),
                stop=plan.get("stop"),
                regime=regime,
                trend=trend,
                price_pct=pct,
            )
        )

    @staticmethod
    def _advanced_summary(bars: list) -> str:
        """MACD / RSI / 布林 / ATR / 价格分位 / 周线趋势（本地确定性计算）。"""
        from ..domain.indicators import atr, boll, macd, percentile_rank, rsi, weekly_trend

        closes = [b.close for b in bars]
        parts: list[str] = []
        m = macd(closes)
        if m:
            parts.append(f"MACD DIF={m['dif']} DEA={m['dea']}（{m['trend']}）")
        r = rsi(closes)
        if r is not None:
            parts.append(f"RSI14={r}")
        b = boll(closes)
        if b:
            parts.append(f"BOLL {b['low']}~{b['up']}（现价在带内 {b['pos_pct']:.0f}%）")
        a = atr(bars)
        if a and closes and closes[-1]:
            parts.append(f"ATR14≈{a / closes[-1] * 100:.2f}%")
        if len(closes) >= 60:
            rank = percentile_rank(closes, closes[-1])
            if rank is not None:
                parts.append(f"现价处近 {len(closes)} 个交易日区间 {rank:.0f}% 分位")
        w = weekly_trend(bars)
        if w:
            tail = f"，近5周 {w['pct5']:+.1f}%" if w.get("pct5") is not None else ""
            parts.append(f"周线{'站上' if w['above'] else '跌破'}20周线（{w['rel_pct']:+.1f}%{tail}）")
        return "进阶指标：" + "；".join(parts) if parts else ""

    def _price_lines(self, code: str, type: str = "") -> tuple[list[str], str]:
        end = dt.date.today()
        start = end - dt.timedelta(days=1100)  # ≈3 年（含价格分位/周线所需历史）
        if type == "otc_fund":
            try:
                navs = self._data_service().get_fund_nav(code, days=60)
            except Exception as exc:  # noqa: BLE001
                log.warning("分析取净值失败 %s: %s", code, exc)
                return [], f"净值获取失败: {exc}"
            values = [n.nav for n in navs]
            parts = [f"最新净值 {values[-1]:.4f}"]
            if len(values) >= 6 and values[-6]:
                parts.append(f"近5期涨跌 {(values[-1] / values[-6] - 1) * 100:+.2f}%")
            if len(values) >= 21 and values[-21]:
                parts.append(f"近20期涨跌 {(values[-1] / values[-21] - 1) * 100:+.2f}%")
            parts.append(f"区间 {min(values):.4f}~{max(values):.4f}")
            summary = "净值摘要：" + "；".join(parts)
            lines = [f"{n.date} 净值{n.nav}" for n in navs[-20:]]
            source = f"基金净值 {len(navs)} 条，截至 {navs[-1].date}"
            return [summary] + lines, source

        try:
            bars = self._data_service().get_daily_bars(code, start, end, adjust="qfq")
        except Exception as exc:  # noqa: BLE001
            log.warning("分析取行情失败 %s: %s", code, exc)
            return [], f"行情获取失败: {exc}"
        summary = self._technical_summary(bars)
        advanced = self._advanced_summary(bars)
        lines = [f"{b.date} 收{b.close} 高{b.high} 低{b.low} 量{b.volume:.0f}" for b in bars[-20:]]
        source = f"日线(前复权) {len(bars)} 条，截至 {bars[-1].date if bars else 'N/A'}"
        return ([summary] + ([advanced] if advanced else []) + lines), source

    def analyze(self, code: str, name: str = "", type: str = "") -> dict:
        """生成并入库分析卡（含操作计划）；LLM 不可用/失败抛 LLMError。"""
        from ..datasource.base import normalize_code
        from ..db.models import AnalysisCard

        code = normalize_code(code)
        prices, price_source = self._price_lines(code, type)
        news = self._news(code)
        target = {"code": code, "name": name or code, "type": type or ""}
        from ..domain.journal import lessons_text

        extras = self._market_extras(code, type)
        result = self.llm.complete_json(
            purpose="analysis",
            system=CARD_V3_SYSTEM,
            user=card_user(
                target,
                prices,
                news,
                lessons=lessons_text(self._sf),
                extras=extras,
            ),
            schema=CardRead,
            max_tokens=1600,
        )
        cost = record_usage(
            self._sf,
            purpose="analysis",
            model=result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cached_in=result.cached_in,
        )

        with self._sf() as session:
            card = AnalysisCard(
                code=code,
                kind="fund" if type == "otc_fund" else "stock",
                model=result.model,
                prompt_ver="card_v7",
                content_json=result.content,
                sources_json={
                    "prices": price_source,
                    "news": news,
                    "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
                },
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost=cost,
            )
            session.add(card)
            session.flush()
            try:
                self._record_prediction(session, card.id, code, type, result.content, extras)
            except Exception:  # noqa: BLE001 — 预测记录失败不影响出卡
                log.exception("预测记录失败 %s", code)
            session.commit()
            return {
                "id": card.id,
                "code": code,
                "kind": card.kind,
                "created_at": card.created_at.isoformat(timespec="seconds"),
                "content": card.content_json,
                "sources": card.sources_json,
                "cost": card.cost,
            }
