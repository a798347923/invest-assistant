"""收盘纪律体检（15:30 作业）：价格/净值 + 走势/新闻/仓位上下文 → AI 操作建议 → 汇总推送。

- A股/ETF：实时行情 + 近 15 日走势 + 最新新闻（best-effort）；
- 场外基金：净值序列（近 20 期）；
- AI 输出：总体总结 + 每只持仓的操作建议（加仓/减仓/持有观察/止盈卖出/止损卖出/清仓，含触发条件）
  + 留意事项 + 明日行动清单；
- LLM 失败时降级为纯模板报告（纪律信息不丢）。
"""

from __future__ import annotations

import datetime as dt
import logging

from pydantic import BaseModel, field_validator
from sqlalchemy import select

from ..datasource import MarketDataService
from ..db.models import ExitRule, Instrument
from ..db.session import get_session_factory
from ..domain.conditions import confirm_labels
from ..domain.positions import PositionService
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import EOD_V2_SYSTEM, eod_user
from ..llm.usage import record_usage
from ..notify import Message, build_dispatcher

log = logging.getLogger(__name__)


class EodPositionAdvice(BaseModel):
    code: str
    action: str = "持有观察"  # 加仓 / 减仓 / 持有观察 / 止盈卖出 / 止损卖出 / 清仓
    reason: str = ""
    condition: str = ""
    confirm: list[str] = []  # 建议加仓时的程序化确认条件（枚举）

    @field_validator("code", "action", "reason", "condition", mode="before")
    @classmethod
    def _coerce_str(cls, value):
        return "" if value is None else str(value)

    @field_validator("confirm", mode="before")
    @classmethod
    def _coerce_confirm(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        return [str(value)]


class EodReport(BaseModel):
    summary: str
    positions: list[EodPositionAdvice] = []
    watch_items: list[str] = []
    actions: list[str] = []

    @field_validator("summary", mode="before")
    @classmethod
    def _coerce_summary(cls, value):
        return "" if value is None else str(value)

    @field_validator("watch_items", "actions", mode="before")
    @classmethod
    def _coerce_str_list(cls, value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        return [str(value)]


def _distance_text(rule: ExitRule, position, price: float) -> str | None:
    params = rule.params_json or {}
    state = rule.state_json or {}
    if rule.type == "fixed_take_profit":
        target = position.avg_cost * (1 + float(params["pct"]) / 100)
        return f"止盈线 {target:.3f}（差 {(target - price) / price * 100:+.2f}%）"
    if rule.type == "fixed_stop_loss":
        target = position.avg_cost * (1 - float(params["pct"]) / 100)
        return f"止损线 {target:.3f}（差 {(price - target) / price * 100:+.2f}%）"
    if rule.type == "trailing_stop":
        peak = float(state.get("peak") or position.avg_cost)
        line = peak * (1 - float(params["drawdown_pct"]) / 100)
        return f"移动止盈线 {line:.3f}（峰值 {peak:.3f}，差 {(price - line) / price * 100:+.2f}%）"
    return None  # max_holding / daily_move 不在距离行展示


def _trend_text(values: list[float]) -> str:
    if len(values) < 2:
        return "数据不足"
    last = values[-1]
    parts: list[str] = []
    if len(values) >= 6 and values[-6]:
        parts.append(f"近5日 {(last / values[-6] - 1) * 100:+.2f}%")
    if values[0]:
        parts.append(f"近{len(values)}日 {(last / values[0] - 1) * 100:+.2f}%")
    parts.append(f"区间 {min(values):.3f}~{max(values):.3f}")
    return "，".join(parts)


def _fetch_news(code: str, limit: int = 3) -> list[str]:
    """最新新闻标题（best-effort，失败返回空；统一走带超时的东财直连）。"""
    from ..datasource.news import fetch_stock_news

    return [item[:60] for item in fetch_stock_news(code, limit)]


def _generate_lessons_safe(sf, llm) -> dict:
    """清仓复盘（best-effort，失败不影响主流程）。"""
    try:
        from .lessons import generate_pending_lessons

        return generate_pending_lessons(session_factory=sf, llm=llm)
    except Exception:  # noqa: BLE001
        log.exception("清仓复盘生成失败")
        return {"generated": 0}


def run_eod(
    data=None,
    positions=None,
    llm=None,
    dispatcher=None,
    session_factory=None,
    now: dt.datetime | None = None,
    force: bool = False,
    news_fetcher=None,
) -> dict:
    now = now or dt.datetime.now()
    sf = session_factory or get_session_factory()
    data = data or MarketDataService(session_factory=sf)
    positions = positions or PositionService(session_factory=sf)
    fetch_news = news_fetcher if news_fetcher is not None else _fetch_news

    if not force and not data.is_trading_day(now.date()):
        return {"skipped": "非交易日"}
    lesson_report = _generate_lessons_safe(sf, llm)
    env_fn = getattr(data, "get_market_env", None)
    market_env: dict = {}
    if callable(env_fn):
        try:
            market_env = env_fn() or {}
        except Exception:  # noqa: BLE001 — 环境数据 best-effort
            market_env = {}
    open_positions = positions.get_open_positions()
    if not open_positions:
        return {"skipped": "无持仓", "lessons": lesson_report.get("generated", 0)}

    with sf() as session:
        types = {i.code: i.type for i in session.scalars(select(Instrument))}
    stock_positions = [p for p in open_positions if types.get(p.code) != "otc_fund"]
    fund_positions = [p for p in open_positions if types.get(p.code) == "otc_fund"]

    # 上下文数据（best-effort）：股票近 30 自然日行情、基金近 20 期净值
    bars_map: dict[str, list] = {}
    for p in stock_positions:
        try:
            bars_map[p.code] = data.get_daily_bars(
                p.code, now.date() - dt.timedelta(days=30), now.date()
            )
        except Exception:  # noqa: BLE001
            log.debug("EOD 日线刷新失败: %s", p.code)

    quotes: dict = {}
    if stock_positions:
        try:
            quotes = data.get_quotes([p.code for p in stock_positions])
        except Exception as exc:  # noqa: BLE001
            return {"error": f"行情不可用: {exc}"}

    navs_map: dict[str, list] = {}
    for p in fund_positions:
        try:
            navs_map[p.code] = data.get_fund_nav(p.code, days=20)
        except Exception as exc:  # noqa: BLE001
            log.warning("场外基金净值获取失败 %s: %s", p.code, exc)
            navs_map[p.code] = []

    # 先计算价格与市值（用于组合占比）
    priced: dict[str, dict] = {}
    for p in open_positions:
        is_fund = types.get(p.code) == "otc_fund"
        if is_fund:
            navs = navs_map.get(p.code) or []
            priced[p.code] = {
                "is_fund": True,
                "price": navs[-1].nav if navs else None,
                "label": "净值",
                "trend": [n.nav for n in navs],
                "news": [],
                "suffix": f"（净值日期 {navs[-1].date}）" if navs else "",
            }
        else:
            quote = quotes.get(p.code)
            price = quote.price if (quote and quote.price > 0) else None
            priced[p.code] = {
                "is_fund": False,
                "price": price,
                "label": "现价",
                "trend": [b.close for b in (bars_map.get(p.code) or [])][-15:],
                "news": fetch_news(p.code) if price is not None else [],
                "suffix": "",
            }

    total_mv = sum(
        entry["price"] * p.qty
        for p in open_positions
        for entry in [priced[p.code]]
        if entry["price"]
    )

    ai_input: list[dict] = []
    lines: list[str] = []
    with sf() as session:
        for p in open_positions:
            entry = priced[p.code]
            price = entry["price"]
            if price is None:
                lines.append(f"{p.code}：{'净值' if entry['is_fund'] else '行情'}缺失，无法体检")
                continue
            pnl = (price - p.avg_cost) * p.qty
            pct = (price / p.avg_cost - 1) * 100 if p.avg_cost else 0.0
            held = (now - p.opened_at).days
            details = []
            rules = session.scalars(
                select(ExitRule).where(
                    ExitRule.position_id == p.id, ExitRule.status == "armed"
                )
            ).all()
            for rule in rules:
                text = _distance_text(rule, p, price)
                if text:
                    details.append(text)
            head = (
                f"{p.code}（{p.strategy}）{entry['label']} {price:.4f} 成本 {p.avg_cost:.4f} "
                f"浮盈亏 {pnl:+.2f}（{pct:+.2f}%）持有 {held} 天{entry['suffix']}"
            )
            lines.append(head + ("｜" + "；".join(details) if details else ""))
            flow = None
            if not entry["is_fund"]:
                flow_fn = getattr(data, "get_stock_flow", None)
                if callable(flow_fn):
                    try:
                        flow = flow_fn(p.code)
                    except Exception:  # noqa: BLE001 — best-effort
                        flow = None
            item = {
                "代码": p.code,
                "类型": "场外基金" if entry["is_fund"] else "股票/ETF",
                "策略": p.strategy,
                "数量": p.qty,
                "成本": round(p.avg_cost, 4),
                "现价或净值": round(price, 4),
                "盈亏百分比": round(pct, 2),
                "持有天数": held,
                "市值": round(price * p.qty, 2),
                "组合占比百分比": round(price * p.qty / total_mv * 100, 1) if total_mv else None,
                "纪律距离": details,
                "近期走势": _trend_text(entry["trend"]),
                "最新新闻": entry["news"],
            }
            if flow:
                item["资金面"] = flow
            ai_input.append(item)

    ai = None
    llm = llm or LLMClient()
    if llm.configured and ai_input:
        from ..domain.journal import lessons_text
        from .calibration import calibration_notes

        lessons = lessons_text(sf)
        try:
            result = llm.complete_json(
                purpose="eod_report",
                system=EOD_V2_SYSTEM,
                user=eod_user(
                    ai_input,
                    lessons=lessons,
                    market_env=market_env,
                    calibration=calibration_notes(sf),
                ),
                schema=EodReport,
                max_tokens=1100,
            )
            record_usage(
                sf,
                purpose="eod_report",
                model=result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cached_in=result.cached_in,
            )
            ai = result.content
        except LLMError as exc:
            log.warning("收盘体检 AI 生成失败（仍发送模板报告）: %s", exc)

    body = ["【持仓体检】", "", *lines]
    if ai:
        body += ["", "【AI 总结】", str(ai.get("summary", ""))]
        advices = ai.get("positions") or []
        if advices:
            body.append("【操作建议】（AI 生成，仅供参考，不构成投资建议）")
            for item in advices:
                if not isinstance(item, dict):
                    continue
                line = f"- {item.get('code', '?')}：{item.get('action', '')}"
                if item.get("reason"):
                    line += f"｜{item['reason']}"
                condition = str(item.get("condition") or "")
                if condition and condition not in ("无", "None"):
                    line += f"（条件：{condition}）"
                confirm = [str(x) for x in (item.get("confirm") or [])]
                if confirm:
                    line += f"（程序可验：{confirm_labels(confirm)}）"
                body.append(line)
        watch = ai.get("watch_items") or []
        if watch:
            body.append("留意：")
            body.extend(f"- {w}" for w in watch)
        actions = ai.get("actions") or []
        if actions:
            body.append("明日行动清单：")
            body.extend(f"{i}. {a}" for i, a in enumerate(actions, 1))

    dispatcher = dispatcher or build_dispatcher()
    sent = False
    if dispatcher.configured:
        results = dispatcher.send(
            Message(title=f"【收盘体检】{len(open_positions)} 只持仓", body="\n".join(body))
        )
        sent = any(r.ok for r in results)
    return {
        "positions": len(open_positions),
        "lines": len(lines),
        "advice": len(ai.get("positions") or []) if ai else 0,
        "ai": bool(ai),
        "sent": sent,
        "lessons": lesson_report.get("generated", 0),
    }
