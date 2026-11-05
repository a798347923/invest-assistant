"""HTML 页面路由（Jinja2 + htmx）。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from ..db.models import Alert, ExitRule, Instrument
from ..db.session import get_session_factory
from ..domain.positions import ExitRules, NotFoundError, PositionService, ValidationError
from ..domain.rules import describe
from .api import status_snapshot
from .templating import TEMPLATES

log = logging.getLogger(__name__)

router = APIRouter()

ALERT_TABS = {"active": "待处理", "all": "全部", "handled": "已处理", "ignored": "已忽略"}
ALERT_STATUS_LABELS = {
    "pending": "待投递",
    "failed": "投递失败",
    "sent": "已送达",
    "handled": "已处理",
    "ignored": "已忽略",
    "expired": "已过期",
}


# ---------------------------------------------------------------- 基础页

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html", {"status": status_snapshot()})


@router.get("/partials/regime", response_class=HTMLResponse)
def regime_partial(request: Request):
    """顶栏强弱市徽标局部刷新（htmx 每 60 秒）。"""
    return TEMPLATES.TemplateResponse(request, "_regime_badge.html", {})


# ---------------------------------------------------------------- 持仓页

def _position_rows() -> tuple[list[dict], str]:
    from ..domain.valuation import valuate_positions

    positions = PositionService().list_positions()
    sf = get_session_factory()
    with sf() as session:
        instruments = {i.code: i for i in session.scalars(select(Instrument))}
        rule_map: dict[int, list[ExitRule]] = {}
        for rule in session.scalars(select(ExitRule)):
            rule_map.setdefault(rule.position_id, []).append(rule)

    valuation = {"prices": {}, "labels": {}, "errors": []}
    if positions:
        codes_by_type = {
            p.code: (instruments[p.code].type if p.code in instruments else "a_share")
            for p in positions
        }
        valuation = valuate_positions(sf, codes_by_type)
    quote_error = "；".join(valuation["errors"])

    now = dt.datetime.now()
    rows = []
    for p in positions:
        price = valuation["prices"].get(p.code)
        rows.append(
            {
                "p": p,
                "name": instruments[p.code].name if p.code in instruments else "",
                "price": price,
                "price_label": valuation["labels"].get(p.code, ""),
                "pnl": (price - p.avg_cost) * p.qty if price is not None else None,
                "pct": ((price / p.avg_cost - 1) * 100)
                if (price is not None and p.avg_cost)
                else None,
                "days": (now - p.opened_at).days,
                "rules": [
                    f"{describe(r.type, r.params_json)}·{r.status}" for r in rule_map.get(p.id, [])
                ],
            }
        )
    return rows, quote_error


@router.get("/positions", response_class=HTMLResponse)
def positions_page(request: Request, created: str = "", sold: str = "", error: str = ""):
    rows, quote_error = _position_rows()
    return TEMPLATES.TemplateResponse(
        request,
        "positions.html",
        {"rows": rows, "quote_error": quote_error, "created": created, "sold": sold, "error": error},
    )


def _parse_float(value: str) -> float | None:
    try:
        return float(value) if str(value).strip() else None
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    try:
        return int(float(value)) if str(value).strip() else None
    except ValueError:
        return None


@router.post("/positions")
def positions_create(
    request: Request,
    code: str = Form(...),
    qty: str = Form(...),
    price: str = Form(...),
    strategy: str = Form(...),
    take_profit: str = Form(""),
    stop_loss: str = Form(""),
    trailing: str = Form(""),
    max_holding: str = Form(""),
    fee: str = Form("0"),
    name: str = Form(""),
    note: str = Form(""),
):
    qty_v, price_v = _parse_float(qty), _parse_float(price)
    if not qty_v or not price_v:
        error = "数量与价格必须为有效数字"
    else:
        error = ""
        rules = ExitRules(
            take_profit_pct=_parse_float(take_profit),
            stop_loss_pct=_parse_float(stop_loss),
            trailing_drawdown_pct=_parse_float(trailing),
            max_holding_days=_parse_int(max_holding),
        )
        try:
            PositionService().add_position(
                code=code, qty=qty_v, price=price_v, strategy=strategy, rules=rules,
                name=name, fee=_parse_float(fee) or 0.0, note=note,
            )
            return RedirectResponse("/positions?created=1", status_code=303)
        except (ValidationError, ValueError) as exc:
            error = str(exc)
    rows, quote_error = _position_rows()
    return TEMPLATES.TemplateResponse(
        request,
        "positions.html",
        {"rows": rows, "quote_error": quote_error, "error": error, "created": "", "sold": ""},
        status_code=422,
    )


@router.post("/positions/{position_id}/sell")
def positions_sell(
    request: Request,
    position_id: int,
    qty: str = Form(...),
    price: str = Form(...),
    fee: str = Form("0"),
):
    qty_v, price_v = _parse_float(qty), _parse_float(price)
    try:
        if not qty_v or not price_v:
            raise ValidationError("卖出数量与价格必须为有效数字")
        PositionService().sell(position_id, qty=qty_v, price=price_v, fee=_parse_float(fee) or 0.0)
        return RedirectResponse("/positions?sold=1", status_code=303)
    except (ValidationError, NotFoundError) as exc:
        rows, quote_error = _position_rows()
        return TEMPLATES.TemplateResponse(
            request,
            "positions.html",
            {"rows": rows, "quote_error": quote_error, "error": str(exc), "created": "", "sold": ""},
            status_code=422,
        )


# ---------------------------------------------------------------- 提醒页

def _alert_row(alert: Alert) -> dict:
    context = alert.context_json or {}
    price = context.get("price")
    cost = context.get("cost")
    qty = context.get("qty")
    pnl = (price - cost) * qty if (price is not None and cost and qty) else None
    return {
        "a": alert,
        "code": context.get("code", ""),
        "reason": context.get("reason", ""),
        "price": price,
        "pnl": pnl,
        "status_label": ALERT_STATUS_LABELS.get(alert.status, alert.status),
        "interpretation": alert.interpretation_json,
        "ai_error": "",
    }


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, status: str = "active"):
    sf = get_session_factory()
    with sf() as session:
        stmt = select(Alert).order_by(Alert.triggered_at.desc()).limit(200)
        if status == "active":
            stmt = stmt.where(Alert.status.in_(("pending", "failed", "sent")))
        elif status in ("handled", "ignored"):
            stmt = stmt.where(Alert.status == status)
        rows = [_alert_row(a) for a in session.scalars(stmt).all()]
    return TEMPLATES.TemplateResponse(
        request, "alerts.html", {"rows": rows, "status": status, "tabs": ALERT_TABS}
    )


@router.post("/alerts/{alert_id}/mark", response_class=HTMLResponse)
def alert_mark(request: Request, alert_id: int, status: str = Form(...)):
    if status not in ("handled", "ignored"):
        raise HTTPException(status_code=422, detail="status 只能是 handled / ignored")
    sf = get_session_factory()
    with sf() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="提醒不存在")
        alert.status = status
        session.commit()
        row = _alert_row(alert)
    if request.headers.get("HX-Request"):
        return TEMPLATES.TemplateResponse(request, "alerts_row.html", {"row": row})
    return RedirectResponse("/alerts", status_code=303)


@router.post("/alerts/{alert_id}/interpret", response_class=HTMLResponse)
def alert_interpret(request: Request, alert_id: int):
    """按需生成 AI 辅助判断（已生成则直接返回，不重复调用 LLM）。"""
    from ..llm.analyzer import TriggerInterpreter

    sf = get_session_factory()
    with sf() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="提醒不存在")
        error = ""
        if alert.interpretation_json is None:
            interpreter = TriggerInterpreter(session_factory=sf)
            interpretation = interpreter.interpret(alert.context_json or {}, kind=alert.kind)
            if interpretation is not None:
                alert.interpretation_json = interpretation
                session.commit()
            else:
                error = "AI 判断暂不可用，请稍后重试"
        row = _alert_row(alert)
        row["ai_error"] = error
    if request.headers.get("HX-Request"):
        return TEMPLATES.TemplateResponse(request, "alerts_row.html", {"row": row})
    return RedirectResponse("/alerts", status_code=303)


# ---------------------------------------------------------------- 自选池

def _watch_row(session, item, insight_error: str = "") -> dict:
    """自选行渲染上下文（含最新分析卡与解析后的 AI 买入计划价位）。"""
    from ..db.models import AnalysisCard
    from ..domain.plan import parse_plan_levels

    last_card = session.scalars(
        select(AnalysisCard)
        .where(AnalysisCard.code == item.code)
        .order_by(AnalysisCard.created_at.desc())
        .limit(1)
    ).first()
    return {
        "i": item,
        "last_card": last_card,
        "insight_error": insight_error,
        "plan": parse_plan_levels(last_card.content_json) if last_card is not None else None,
    }


def _watch_rows(sf) -> list[dict]:
    from ..db.models import WatchlistItem

    with sf() as session:
        items = session.scalars(
            select(WatchlistItem).order_by(WatchlistItem.added_at.desc())
        ).all()
        return [_watch_row(session, item) for item in items]


def _watchlist_render(request: Request, error: str = ""):
    return TEMPLATES.TemplateResponse(
        request,
        "watchlist.html",
        {"rows": _watch_rows(get_session_factory()), "added": "", "removed": "", "error": error},
        status_code=422,
    )


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, added: str = "", removed: str = "", error: str = ""):
    return TEMPLATES.TemplateResponse(
        request,
        "watchlist.html",
        {
            "rows": _watch_rows(get_session_factory()),
            "added": added,
            "removed": removed,
            "error": error,
        },
    )


@router.post("/watchlist")
def watchlist_add(
    request: Request,
    code: str = Form(...),
    group: str = Form("default"),
    note: str = Form(""),
    alert_pct: str = Form(""),
    price_above: str = Form(""),
    price_below: str = Form(""),
):
    from ..datasource.base import normalize_code
    from ..db.models import WatchlistItem

    try:
        norm = normalize_code(code)
    except ValueError as exc:
        return _watchlist_render(request, error=str(exc))
    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, norm)
        if item is None:
            item = WatchlistItem(code=norm)
            session.add(item)
        item.group = group or "default"
        item.note = note
        item.alert_pct = _parse_float(alert_pct)
        item.alert_price_above = _parse_float(price_above)
        item.alert_price_below = _parse_float(price_below)
        session.commit()
    return RedirectResponse("/watchlist?added=1", status_code=303)


@router.post("/watchlist/{code}/delete")
def watchlist_delete(code: str):
    from ..db.models import WatchlistItem

    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is not None:
            session.delete(item)
            session.commit()
    return RedirectResponse("/watchlist?removed=1", status_code=303)


@router.post("/watchlist/{code}/toggle-plan", response_class=HTMLResponse)
def watchlist_toggle_plan(request: Request, code: str):
    """开关「AI 买入计划」盘中监控（进入建议买入区间 / 跌破失效价提醒）。"""
    from ..db.models import WatchlistItem

    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is None:
            raise HTTPException(status_code=404, detail="不在自选中")
        item.alert_plan = not item.alert_plan
        session.commit()
        row = _watch_row(session, item)
    if request.headers.get("HX-Request"):
        return TEMPLATES.TemplateResponse(request, "watchlist_row.html", {"row": row})
    return RedirectResponse("/watchlist", status_code=303)


@router.post("/watchlist/{code}/insight", response_class=HTMLResponse)
def watchlist_insight(request: Request, code: str):
    """按需对自选标的生成 AI 分析（完整分析卡；页面就地显示紧凑预览）。"""
    from ..db.models import Instrument, WatchlistItem
    from ..llm.analyzer import CardAnalyzer
    from ..llm.client import LLMError

    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is None:
            raise HTTPException(status_code=404, detail="不在自选中")
        instrument = session.get(Instrument, code)

    error = ""
    try:
        CardAnalyzer(session_factory=sf).analyze(
            code, type=(instrument.type if instrument else "")
        )
    except LLMError as exc:
        error = f"AI 分析失败：{exc}"
    except ValueError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — 兜底：避免 htmx 静默失败（页面无任何反馈）
        log.exception("自选 AI 分析异常 %s", code)
        error = f"AI 分析失败：{type(exc).__name__}: {exc}"

    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is None:
            raise HTTPException(status_code=404, detail="不在自选中")
        row = _watch_row(session, item, insight_error=error)

    if request.headers.get("HX-Request"):
        return TEMPLATES.TemplateResponse(request, "watchlist_row.html", {"row": row})
    return RedirectResponse("/watchlist", status_code=303)


# ---------------------------------------------------------------- 分析卡

def _cards_context(request: Request, error: str = "", done: str = ""):
    from ..db.models import AnalysisCard, WatchlistItem

    sf = get_session_factory()
    with sf() as session:
        cards = session.scalars(
            select(AnalysisCard).order_by(AnalysisCard.created_at.desc()).limit(50)
        ).all()
        watched = {item.code for item in session.scalars(select(WatchlistItem))}
        rows = [
            {
                "c": card,
                "content": card.content_json or {},
                "sources": card.sources_json or {},
                "in_watchlist": card.code in watched,
            }
            for card in cards
        ]
    return TEMPLATES.TemplateResponse(
        request, "cards.html", {"rows": rows, "error": error, "done": done}
    )


@router.get("/cards", response_class=HTMLResponse)
def cards_page(request: Request, done: str = "", error: str = ""):
    return _cards_context(request, error=error, done=done)


@router.post("/cards/analyze")
def cards_analyze(
    request: Request,
    code: str = Form(...),
    name: str = Form(""),
    type: str = Form(""),
):
    from ..llm.analyzer import CardAnalyzer
    from ..llm.client import LLMError

    try:
        CardAnalyzer(session_factory=get_session_factory()).analyze(code, name=name, type=type)
    except (LLMError, ValueError) as exc:
        return _cards_context(request, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — 兜底：页面回显错误而不是 500
        log.exception("分析卡生成异常 %s", code)
        return _cards_context(request, error=f"{type(exc).__name__}: {exc}")
    return RedirectResponse("/cards?done=1", status_code=303)


@router.post("/cards/watch", response_class=HTMLResponse)
def cards_watch(request: Request, code: str = Form(...), source: str = Form("分析卡")):
    """快捷加入自选池（分析卡/候选池共用；htmx 局部刷新，普通表单回退整页跳转）。"""
    from ..datasource.base import normalize_code
    from ..db.models import WatchlistItem

    try:
        norm = normalize_code(code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, norm)
        if item is None:
            session.add(WatchlistItem(code=norm, group="default", note=f"来自{source}"))
        session.commit()

    if request.headers.get("HX-Request"):
        return TEMPLATES.TemplateResponse(
            request, "card_watch_button.html", {"code": norm, "watched": True}
        )
    target = "/candidates" if source == "候选池" else "/cards"
    return RedirectResponse(target, status_code=303)


# ---------------------------------------------------------------- 用量

@router.get("/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    from ..db.models import LLMUsage
    from ..llm.usage import daily_cost_summary

    sf = get_session_factory()
    summary = daily_cost_summary(sf, days=7)
    with sf() as session:
        recent = session.scalars(
            select(LLMUsage).order_by(LLMUsage.ts.desc()).limit(20)
        ).all()
    return TEMPLATES.TemplateResponse(
        request, "usage.html", {"summary": summary, "recent": recent}
    )


# ---------------------------------------------------------------- 候选池

@router.get("/candidates", response_class=HTMLResponse)
def candidates_page(request: Request, date: str = ""):
    from sqlalchemy import func as sql_func

    from ..db.models import Candidate, ScreenTemplate, WatchlistItem

    sf = get_session_factory()
    q_date = None
    rows: list[dict] = []
    with sf() as session:
        q_date = (
            dt.date.fromisoformat(date)
            if date
            else session.scalar(sql_func.max(Candidate.date))
        )
        if q_date is not None:
            templates = {t.id: t.name for t in session.scalars(select(ScreenTemplate))}
            watched = {item.code for item in session.scalars(select(WatchlistItem))}
            candidates = session.scalars(
                select(Candidate)
                .where(Candidate.date == q_date)
                .order_by(Candidate.score.desc())
            ).all()
            rows = [
                {
                    "c": candidate,
                    "template": templates.get(candidate.template_id, ""),
                    "name": (candidate.reasons_json or {}).get("name", ""),
                    "reasons": (candidate.reasons_json or {}).get("reasons", []),
                    "setup": ((candidate.reasons_json or {}).get("setup") or {}).get("label", ""),
                    "in_watchlist": candidate.code in watched,
                }
                for candidate in candidates
            ]
    return TEMPLATES.TemplateResponse(
        request,
        "candidates.html",
        {"rows": rows, "date": q_date.isoformat() if q_date else None},
    )


# ---------------------------------------------------------------- 模拟盘

@router.get("/paper", response_class=HTMLResponse)
def paper_page(request: Request):
    from ..db.models import AnalysisCard, PaperDaily, PaperReport, PaperTrade
    from ..domain.plan import parse_plan_levels
    from ..engine.paper import PaperService

    sf = get_session_factory()
    service = PaperService(session_factory=sf)
    with sf() as session:
        positions = service.open_positions(session)
        stats = service.portfolio_stats(session)
        latest_report = session.scalars(
            select(PaperReport).order_by(PaperReport.date.desc()).limit(1)
        ).first()
        advice_map: dict[str, dict] = {}
        if latest_report is not None:
            for item in (latest_report.content_json or {}).get("positions", []):
                advice_map[str(item.get("code"))] = item
        rows = []
        today = dt.date.today()
        for position in positions:
            card = session.scalars(
                select(AnalysisCard)
                .where(AnalysisCard.code == position.code)
                .order_by(AnalysisCard.created_at.desc())
                .limit(1)
            ).first()
            plan = parse_plan_levels(card.content_json) if card is not None else None
            price = position.last_price or position.avg_cost
            pnl = (price - position.avg_cost) * position.qty if position.avg_cost else 0.0
            pnl_pct = (price / position.avg_cost - 1) * 100 if position.avg_cost else 0.0
            rows.append(
                {
                    "p": position,
                    "plan": plan,
                    "price": price,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "advice": advice_map.get(position.code),
                    "holding_days": (today - position.opened_at.date()).days,
                }
            )
        trades = session.scalars(
            select(PaperTrade).order_by(PaperTrade.ts.desc()).limit(50)
        ).all()
        dailies = session.scalars(
            select(PaperDaily).order_by(PaperDaily.date.desc()).limit(30)
        ).all()
        report_ctx = {
            "date": latest_report.date.isoformat() if latest_report else None,
            "status": latest_report.status if latest_report else "",
            "summary": (latest_report.content_json or {}).get("summary", "") if latest_report else "",
            "risk_notes": (latest_report.content_json or {}).get("risk_notes", []) if latest_report else [],
            "executed": (latest_report.execution_json or {}).get("executed", []) if latest_report else [],
            "skipped": (latest_report.execution_json or {}).get("skipped", []) if latest_report else [],
        }
    return TEMPLATES.TemplateResponse(
        request,
        "paper.html",
        {"stats": stats, "rows": rows, "trades": trades, "dailies": dailies, "report": report_ctx},
    )


# ---------------------------------------------------------------- 复盘（交易日志与经验）

@router.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request):
    from ..db.models import (
        CalibrationReport,
        PaperPosition,
        Position,
        Prediction,
        TradeJournal,
        TradingLesson,
    )

    sf = get_session_factory()
    with sf() as session:
        lessons = session.scalars(
            select(TradingLesson).order_by(TradingLesson.created_at.desc()).limit(20)
        ).all()
        entries = session.scalars(
            select(TradeJournal).order_by(TradeJournal.ts.desc()).limit(100)
        ).all()
        calibration = session.scalars(
            select(CalibrationReport).order_by(CalibrationReport.date.desc()).limit(1)
        ).first()
        predictions = session.scalars(
            select(Prediction)
            .where(Prediction.status == "evaluated")
            .order_by(Prediction.date.desc())
            .limit(20)
        ).all()
        done = {(row.source, row.ref_id) for row in session.scalars(select(TradingLesson))}
        pending = sum(
            1
            for p in session.scalars(
                select(PaperPosition).where(PaperPosition.status == "closed")
            )
            if ("paper", p.id) not in done
        ) + sum(
            1
            for p in session.scalars(select(Position).where(Position.status == "closed"))
            if ("position", p.id) not in done
        )
    return TEMPLATES.TemplateResponse(
        request,
        "journal.html",
        {
            "lessons": lessons,
            "entries": entries,
            "pending": pending,
            "calibration": calibration,
            "predictions": predictions,
        },
    )


# ---------------------------------------------------------------- 设置

def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 6:
        return "已配置"
    return f"已配置（…{value[-4:]}）"


def _redirect_settings(**params) -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse("/settings?" + urlencode(params), status_code=303)


def _settings_context(request: Request, saved: str = "", error: str = "", status_code: int = 200):
    from .. import __version__
    from ..config import get_settings
    from ..db.models import JobRun
    from ..engine.scheduler import HEARTBEAT_KEY
    from ..engine.state import get_last_poll_at, get_value

    settings = get_settings()
    sf = get_session_factory()
    cfg = settings.app_config()
    poll_cfg = cfg.get("poll") or {}
    watch_cfg = cfg.get("watchlist") or {}
    notify_cfg = cfg.get("notify") or {}
    screen_cfg = cfg.get("screen") or {}
    paper_cfg = cfg.get("paper") or {}
    llm_cfg = cfg.get("llm") or {}
    backup_cfg = cfg.get("backup") or {}

    db_path = settings.data_dir / "invest.db"
    backup_dir = settings.data_dir / "backups"
    backups = sorted(backup_dir.glob("invest-*.db")) if backup_dir.exists() else []
    with sf() as session:
        jobs = session.scalars(
            select(JobRun).order_by(JobRun.started_at.desc()).limit(15)
        ).all()
    heartbeat = str((get_value(sf, HEARTBEAT_KEY) or {}).get("at") or "—").replace("T", " ")
    last_poll = get_last_poll_at(sf)

    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "saved": saved,
            "error": error,
            "version": __version__,
            "heartbeat": heartbeat,
            "last_poll": last_poll.strftime("%Y-%m-%d %H:%M:%S") if last_poll else "—",
            "jobs": jobs,
            "db_size_mb": round(db_path.stat().st_size / 1024 / 1024, 2) if db_path.exists() else 0.0,
            "log_file": str(settings.logs_dir / "app.log"),
            "backup_dir": str(backup_dir),
            "backup_count": len(backups),
            "latest_backup": backups[-1].name if backups else "—",
            "secrets": [
                {
                    "name": "DeepSeek API Key",
                    "ok": bool(settings.deepseek_api_key),
                    "hint": _mask_secret(settings.deepseek_api_key),
                },
                {
                    "name": "SMTP 邮件",
                    "ok": bool(
                        settings.smtp_host
                        and settings.smtp_user
                        and settings.smtp_password
                        and settings.notify_email_to
                    ),
                    "hint": _mask_secret(settings.smtp_user) or (settings.smtp_host or ""),
                },
                {
                    "name": "微信（ServerChan）",
                    "ok": bool(settings.serverchan_sendkey),
                    "hint": _mask_secret(settings.serverchan_sendkey),
                },
                {
                    "name": "面板密码",
                    "ok": bool(settings.panel_password),
                    "hint": "已设置" if settings.panel_password else "",
                },
            ],
            "poll": {
                "interval_minutes": poll_cfg.get("interval_minutes", 1),
                "windows": ", ".join(poll_cfg.get("windows") or []),
            },
            "watchlist_cfg": {
                "default_alert_pct": watch_cfg.get("default_alert_pct", 5.0),
                "movement_alerts": bool(watch_cfg.get("movement_alerts", True)),
            },
            "notify_cfg": {"daily_push_limit": notify_cfg.get("daily_push_limit", 30)},
            "screen": {
                "daily_candidates": screen_cfg.get("daily_candidates", 5),
                "setup_eval_limit": screen_cfg.get("setup_eval_limit", 40),
                "setup_required": bool(screen_cfg.get("setup_required", True)),
            },
            "paper": {
                "enabled": bool(paper_cfg.get("enabled", True)),
                "initial_cash": paper_cfg.get("initial_cash", 50000),
                "position_fraction": paper_cfg.get("position_fraction", 0.2),
                "max_positions": paper_cfg.get("max_positions", 5),
                "max_position_weight": paper_cfg.get("max_position_weight", 0.3),
                "ai_veto": bool(paper_cfg.get("ai_veto", True)),
                "ai_veto_exempt_stops": bool(paper_cfg.get("ai_veto_exempt_stops", True)),
                "ai_veto_cooldown_minutes": paper_cfg.get("ai_veto_cooldown_minutes", 15),
                "ai_veto_max_per_day": paper_cfg.get("ai_veto_max_per_day", 8),
                "apply_replay_v3": bool(paper_cfg.get("apply_replay_v3", True)),
            },
            "llm": {
                "model": llm_cfg.get("model", "deepseek-chat"),
                "deep_model": llm_cfg.get("deep_model", "deepseek-reasoner"),
                "soft_daily_cost_alert_cny": llm_cfg.get("soft_daily_cost_alert_cny", 5),
            },
            "backup_cfg": {"keep": backup_cfg.get("keep", 30)},
        },
        status_code=status_code,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = "", error: str = ""):
    return _settings_context(request, saved=saved, error=error)


@router.post("/settings")
async def settings_save(request: Request):
    import yaml as _yaml

    from ..config import deep_merge, get_settings

    settings = get_settings()
    form = await request.form()
    errors: list[str] = []

    def num(field: str, label: str, cast, lo, hi):
        raw = str(form.get(field, "")).strip()
        if not raw:
            return None
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            errors.append(f"{label}「{raw}」不是有效数值")
            return None
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            errors.append(f"{label}需在 {lo}~{hi} 之间")
            return None
        return value

    overrides: dict = {}

    def put(section: str, key: str, value) -> None:
        if value is not None:
            overrides.setdefault(section, {})[key] = value

    put("poll", "interval_minutes", num("poll_interval_minutes", "轮询间隔（分钟）", int, 1, 60))
    windows_raw = str(form.get("poll_windows", "")).strip()
    if windows_raw:
        windows = [
            part.strip()
            for part in windows_raw.replace("，", ",").split(",")
            if part.strip()
        ]
        overrides.setdefault("poll", {})["windows"] = windows
    put(
        "watchlist", "default_alert_pct",
        num("watchlist_default_alert_pct", "自选异动阈值（%）", float, 0, 50),
    )
    overrides.setdefault("watchlist", {})["movement_alerts"] = (
        form.get("watchlist_movement_alerts") is not None
    )
    put("notify", "daily_push_limit", num("notify_daily_push_limit", "每日推送上限", int, 1, 1000))
    put("screen", "daily_candidates", num("screen_daily_candidates", "每日候选数", int, 1, 20))
    put("screen", "setup_eval_limit", num("screen_setup_eval_limit", "买点评估只数", int, 5, 200))
    overrides.setdefault("screen", {})["setup_required"] = (
        form.get("screen_setup_required") is not None
    )
    put("paper", "initial_cash", num("paper_initial_cash", "模拟盘初始资金", float, 1, 1e9))
    put("paper", "position_fraction", num("paper_position_fraction", "单笔仓位比例", float, 0.01, 1))
    put("paper", "max_positions", num("paper_max_positions", "模拟盘最大持仓数", int, 1, 20))
    put(
        "paper", "max_position_weight",
        num("paper_max_position_weight", "单标的上限比例", float, 0.05, 1),
    )
    overrides.setdefault("paper", {})["enabled"] = form.get("paper_enabled") is not None
    overrides.setdefault("paper", {})["ai_veto"] = form.get("paper_ai_veto") is not None
    overrides.setdefault("paper", {})["ai_veto_exempt_stops"] = (
        form.get("paper_ai_veto_exempt_stops") is not None
    )
    overrides.setdefault("paper", {})["apply_replay_v3"] = (
        form.get("paper_apply_replay_v3") is not None
    )
    put(
        "paper", "ai_veto_cooldown_minutes",
        num("paper_ai_veto_cooldown_minutes", "复核冷却（分钟）", int, 1, 240),
    )
    put(
        "paper", "ai_veto_max_per_day",
        num("paper_ai_veto_max_per_day", "复核每日上限", int, 1, 100),
    )
    model = str(form.get("llm_model", "")).strip()
    if model:
        overrides.setdefault("llm", {})["model"] = model
    deep_model = str(form.get("llm_deep_model", "")).strip()
    if deep_model:
        overrides.setdefault("llm", {})["deep_model"] = deep_model
    put(
        "llm", "soft_daily_cost_alert_cny",
        num("llm_soft_daily_cost_alert_cny", "成本告警阈值（元）", float, 0, 1e6),
    )
    put("backup", "keep", num("backup_keep", "备份保留份数", int, 1, 365))

    if errors:
        return _settings_context(request, error="；".join(errors), status_code=422)

    settings.ensure_dirs()
    old: dict = {}
    if settings.override_file.exists():
        try:
            old = _yaml.safe_load(settings.override_file.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — 旧覆盖损坏则重建
            old = {}
    merged = deep_merge(old, overrides)
    settings.override_file.write_text(
        _yaml.safe_dump(merged, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    return _redirect_settings(saved="设置已保存（data/overrides.yaml），重启服务后生效")


@router.post("/settings/backup")
def settings_backup():
    from ..config import get_settings
    from ..db.backup import backup_database

    settings = get_settings()
    keep = int((settings.app_config().get("backup") or {}).get("keep", 30) or 30)
    try:
        dest = backup_database(keep=keep)
    except Exception as exc:  # noqa: BLE001 — 页面提示即可
        log.warning("手动备份失败: %s", exc)
        return _redirect_settings(error=f"备份失败：{exc}")
    return _redirect_settings(saved=f"备份完成：{dest.name}")


@router.post("/settings/test-notify")
def settings_test_notify():
    from ..notify import Message, build_dispatcher

    dispatcher = build_dispatcher()
    if not dispatcher.configured:
        return _redirect_settings(
            error="未配置任何通知渠道：请在 .env 填写 SMTP_* / SERVERCHAN_SENDKEY 后重启服务"
        )
    try:
        results = dispatcher.send(
            Message(
                title="【投资助手】测试通知",
                body="如果你收到这条消息，说明通知渠道工作正常。",
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _redirect_settings(error=f"发送异常：{exc}")
    ok = any(r.ok for r in results)
    detail = "；".join(f"{r.channel} {'成功' if r.ok else '失败'}" for r in results)
    if ok:
        return _redirect_settings(saved=f"测试通知已发送（{detail}）")
    return _redirect_settings(error=f"发送失败（{detail}）")


# ---------------------------------------------------------------- 回测

@router.get("/backtests", response_class=HTMLResponse)
def backtests_page(request: Request, run_id: int = 0):
    from ..db.models import BacktestRun

    sf = get_session_factory()
    with sf() as session:
        runs = session.scalars(
            select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(20)
        ).all()
        run_list = [
            {
                "r": run,
                "stats": (run.result_json or {}).get("stats"),
                "params": run.params_json or {},
            }
            for run in runs
        ]
        current = None
        if run_list:
            if run_id:
                current = next((item for item in run_list if item["r"].id == run_id), None)
            if current is None and run_id == 0:
                current = next(
                    (item for item in run_list if item["r"].status == "done"), run_list[0]
                )
    today = dt.date.today()
    return TEMPLATES.TemplateResponse(
        request,
        "backtests.html",
        {
            "runs": run_list,
            "current": current,
            "today": today.isoformat(),
            "default_start": (today - dt.timedelta(days=365)).isoformat(),
        },
    )


@router.post("/backtests/run")
def backtests_run(
    start: str = Form(...),
    end: str = Form(...),
    slots: int = Form(5),
    initial_cash: float = Form(200_000),
    take_profit: float = Form(20),
    stop_loss: float = Form(10),
    trailing: float = Form(8),
    max_holding: int = Form(60),
    universe_top: int = Form(150),
):
    import threading

    from ..backtest.runner import execute_backtest
    from ..db.models import BacktestRun

    sf = get_session_factory()
    params = {
        "start": start,
        "end": end,
        "slots": slots,
        "initial_cash": initial_cash,
        "universe_top": universe_top,
        "exit": {
            "take_profit_pct": take_profit,
            "stop_loss_pct": stop_loss,
            "trailing_drawdown_pct": trailing,
            "max_holding_days": max_holding,
        },
    }
    with sf() as session:
        run = BacktestRun(params_json=params, status="pending", progress=0.0)
        session.add(run)
        session.commit()
        run_id = run.id

    threading.Thread(
        target=execute_backtest,
        args=(run_id,),
        kwargs={"session_factory": sf},
        daemon=True,
    ).start()
    return RedirectResponse(f"/backtests?run_id={run_id}", status_code=303)
