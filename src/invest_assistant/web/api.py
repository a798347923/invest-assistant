"""JSON API 路由（/api/*）。"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import __version__
from ..config import get_settings
from ..db.models import Alert, ExitRule, Instrument, Position
from ..db.session import get_session_factory
from ..domain.positions import ExitRules, NotFoundError, PositionService, ValidationError
from ..engine.state import get_last_poll_at
from ..guards import local_offset_hours, timezone_warning

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _service() -> PositionService:
    return PositionService()


class PositionIn(BaseModel):
    code: str
    qty: float
    price: float
    strategy: str
    take_profit_pct: float
    stop_loss_pct: float | None = None
    trailing_drawdown_pct: float | None = None
    max_holding_days: int
    fee: float = 0.0
    name: str = ""
    type: str = ""
    note: str = ""


class SellIn(BaseModel):
    qty: float
    price: float
    fee: float = 0.0


class MarkIn(BaseModel):
    status: str


def status_snapshot() -> dict:
    data: dict = {
        "version": __version__,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "tz_offset_hours": local_offset_hours(),
        "tz_warning": timezone_warning(),
        "open_positions": 0,
        "pending_alerts": 0,
        "last_poll_at": None,
        "data_dir": str(get_settings().data_dir),
    }
    try:
        sf = get_session_factory()
        with sf() as session:
            data["open_positions"] = int(
                session.scalar(
                    select(func.count()).select_from(Position).where(Position.status == "open")
                )
                or 0
            )
            data["pending_alerts"] = int(
                session.scalar(
                    select(func.count())
                    .select_from(Alert)
                    .where(Alert.status.in_(("pending", "failed")))
                )
                or 0
            )
        last = get_last_poll_at(sf)
        data["last_poll_at"] = last.isoformat(timespec="seconds") if last else None
    except Exception as exc:  # noqa: BLE001 — 面板不应因数据库未初始化而 500
        data["db_error"] = str(exc)
    return data


@router.get("/status")
def api_status():
    return status_snapshot()


@router.get("/positions")
def list_positions(include_closed: bool = False):
    positions = _service().list_positions(include_closed=include_closed)
    sf = get_session_factory()
    with sf() as session:
        names = {i.code: i.name for i in session.scalars(select(Instrument))}
        rule_map: dict[int, list[dict]] = {}
        for rule in session.scalars(select(ExitRule)):
            rule_map.setdefault(rule.position_id, []).append(
                {"id": rule.id, "type": rule.type, "params": rule.params_json, "status": rule.status}
            )
    return [
        {
            "id": p.id,
            "code": p.code,
            "name": names.get(p.code, ""),
            "strategy": p.strategy,
            "qty": p.qty,
            "avg_cost": p.avg_cost,
            "opened_at": p.opened_at.isoformat(timespec="seconds"),
            "status": p.status,
            "rules": rule_map.get(p.id, []),
        }
        for p in positions
    ]


@router.post("/positions", status_code=201)
def create_position(body: PositionIn):
    rules = ExitRules(
        take_profit_pct=body.take_profit_pct,
        stop_loss_pct=body.stop_loss_pct,
        trailing_drawdown_pct=body.trailing_drawdown_pct,
        max_holding_days=body.max_holding_days,
    )
    try:
        return _service().add_position(
            code=body.code, qty=body.qty, price=body.price, strategy=body.strategy,
            rules=rules, name=body.name, type=body.type, fee=body.fee, note=body.note,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/positions/{position_id}/sell")
def sell_position(position_id: int, body: SellIn):
    try:
        return _service().sell(position_id, qty=body.qty, price=body.price, fee=body.fee)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/alerts")
def list_alerts(status: str = "active", limit: int = 200):
    sf = get_session_factory()
    with sf() as session:
        stmt = select(Alert).order_by(Alert.triggered_at.desc()).limit(min(limit, 500))
        if status == "active":
            stmt = stmt.where(Alert.status.in_(("pending", "failed", "sent")))
        elif status != "all":
            stmt = stmt.where(Alert.status == status)
        return [_alert_dict(a) for a in session.scalars(stmt).all()]


@router.post("/alerts/{alert_id}/mark")
def mark_alert(alert_id: int, body: MarkIn):
    if body.status not in ("handled", "ignored"):
        raise HTTPException(status_code=422, detail="status 只能是 handled / ignored")
    sf = get_session_factory()
    with sf() as session:
        alert = session.get(Alert, alert_id)
        if alert is None:
            raise HTTPException(status_code=404, detail="提醒不存在")
        alert.status = body.status
        session.commit()
        return _alert_dict(alert)


def _alert_dict(alert: Alert) -> dict:
    context = alert.context_json or {}
    return {
        "id": alert.id,
        "kind": alert.kind,
        "status": alert.status,
        "is_catchup": alert.is_catchup,
        "triggered_at": alert.triggered_at.isoformat(timespec="seconds"),
        "trigger_price": alert.trigger_price,
        "code": context.get("code"),
        "reason": context.get("reason"),
        "cost": context.get("cost"),
        "qty": context.get("qty"),
    }


# ---------------------------------------------------------------- 自选池

class WatchIn(BaseModel):
    code: str
    group: str = "default"
    note: str = ""
    alert_pct: float | None = None  # 异动阈值（%）；0=关闭；不传=保持/默认
    alert_price_above: float | None = None
    alert_price_below: float | None = None
    alert_plan: bool | None = None  # AI 买入计划监控开关；不传=保持/默认开


@router.get("/watchlist")
def list_watchlist():
    from ..db.models import WatchlistItem

    sf = get_session_factory()
    with sf() as session:
        items = session.scalars(
            select(WatchlistItem).order_by(WatchlistItem.added_at.desc())
        ).all()
        return [
            {
                "code": item.code,
                "group": item.group,
                "note": item.note,
                "added_at": item.added_at.isoformat(timespec="seconds"),
                "alert_pct": item.alert_pct,
                "alert_price_above": item.alert_price_above,
                "alert_price_below": item.alert_price_below,
                "alert_plan": item.alert_plan,
            }
            for item in items
        ]


@router.post("/watchlist", status_code=201)
def add_watchlist(body: WatchIn):
    from ..datasource.base import normalize_code
    from ..db.models import WatchlistItem

    try:
        code = normalize_code(body.code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is None:
            item = WatchlistItem(code=code)
            session.add(item)
        item.group = body.group or "default"
        item.note = body.note
        if body.alert_pct is not None:
            item.alert_pct = body.alert_pct
        if body.alert_price_above is not None:
            item.alert_price_above = body.alert_price_above
        if body.alert_price_below is not None:
            item.alert_price_below = body.alert_price_below
        if body.alert_plan is not None:
            item.alert_plan = body.alert_plan
        session.commit()
        return {
            "code": item.code,
            "group": item.group,
            "note": item.note,
            "alert_pct": item.alert_pct,
            "alert_price_above": item.alert_price_above,
            "alert_price_below": item.alert_price_below,
            "alert_plan": item.alert_plan,
        }


@router.delete("/watchlist/{code}")
def remove_watchlist(code: str):
    from ..db.models import WatchlistItem

    sf = get_session_factory()
    with sf() as session:
        item = session.get(WatchlistItem, code)
        if item is None:
            raise HTTPException(status_code=404, detail="不在自选中")
        session.delete(item)
        session.commit()
        return {"removed": code}


# ---------------------------------------------------------------- 分析卡

class AnalyzeIn(BaseModel):
    code: str
    name: str = ""
    type: str = ""


@router.get("/cards")
def list_cards(code: str = "", limit: int = 50):
    from ..db.models import AnalysisCard

    sf = get_session_factory()
    with sf() as session:
        stmt = (
            select(AnalysisCard)
            .order_by(AnalysisCard.created_at.desc())
            .limit(min(limit, 200))
        )
        if code:
            stmt = stmt.where(AnalysisCard.code == code)
        cards = session.scalars(stmt).all()
        return [
            {
                "id": card.id,
                "code": card.code,
                "kind": card.kind,
                "created_at": card.created_at.isoformat(timespec="seconds"),
                "model": card.model,
                "content": card.content_json,
                "sources": card.sources_json,
                "cost": card.cost,
            }
            for card in cards
        ]


@router.post("/analyze")
def analyze_code(body: AnalyzeIn):
    from ..llm.analyzer import CardAnalyzer
    from ..llm.client import LLMError

    try:
        return CardAnalyzer(session_factory=get_session_factory()).analyze(
            body.code, name=body.name, type=body.type
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"AI 分析失败: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------- 用量

@router.get("/usage")
def api_usage(days: int = 7):
    from ..llm.usage import daily_cost_summary

    return daily_cost_summary(get_session_factory(), days=max(1, min(days, 30)))


# ---------------------------------------------------------------- 候选池

@router.get("/candidates")
def list_candidates(date: str = "", template: str = "", limit: int = 100):
    from ..db.models import Candidate, ScreenTemplate

    sf = get_session_factory()
    with sf() as session:
        q_date = dt.date.fromisoformat(date) if date else session.scalar(select(func.max(Candidate.date)))
        if q_date is None:
            return []
        stmt = (
            select(Candidate)
            .where(Candidate.date == q_date)
            .order_by(Candidate.score.desc())
            .limit(min(limit, 500))
        )
        templates = {t.id: t.name for t in session.scalars(select(ScreenTemplate))}
        if template:
            tids = [tid for tid, tname in templates.items() if tname == template]
            if not tids:
                return []
            stmt = stmt.where(Candidate.template_id.in_(tids))
        rows = session.scalars(stmt).all()
        return [
            {
                "id": c.id,
                "date": c.date.isoformat(),
                "code": c.code,
                "template": templates.get(c.template_id, ""),
                "score": c.score,
                "status": c.status,
                "name": (c.reasons_json or {}).get("name", ""),
                "price": (c.reasons_json or {}).get("price"),
                "pct": (c.reasons_json or {}).get("pct"),
                "reasons": (c.reasons_json or {}).get("reasons", []),
                "setup": ((c.reasons_json or {}).get("setup") or {}).get("label", ""),
            }
            for c in rows
        ]


# ---------------------------------------------------------------- 回测

class BacktestIn(BaseModel):
    start: str
    end: str
    slots: int = 5
    initial_cash: float = 200_000
    codes: list[str] | None = None
    universe_top: int = 150
    signal: dict = {}
    exit: dict = {}
    fees: dict = {}
    sync: bool = False


def _backtest_brief(run) -> dict:
    result = run.result_json or {}
    params = run.params_json or {}
    return {
        "id": run.id,
        "created_at": run.created_at.isoformat(timespec="seconds"),
        "status": run.status,
        "progress": run.progress,
        "error": run.error,
        "stats": result.get("stats"),
        "params": {
            "start": params.get("start"),
            "end": params.get("end"),
            "slots": params.get("slots"),
            "universe_top": params.get("universe_top"),
        },
    }


@router.post("/backtests", status_code=201)
def create_backtest(body: BacktestIn):
    from ..backtest.runner import execute_backtest
    from ..db.models import BacktestRun

    sf = get_session_factory()
    params = body.model_dump(exclude={"sync"})
    with sf() as session:
        run = BacktestRun(params_json=params, status="pending", progress=0.0)
        session.add(run)
        session.commit()
        run_id = run.id

    if body.sync:
        execute_backtest(run_id, session_factory=sf)
    else:
        import threading

        threading.Thread(
            target=execute_backtest,
            args=(run_id,),
            kwargs={"session_factory": sf},
            daemon=True,
        ).start()

    with sf() as session:
        run = session.get(BacktestRun, run_id)
        return {"id": run.id, "status": run.status, "progress": run.progress}


@router.get("/backtests")
def list_backtests(limit: int = 20):
    from ..db.models import BacktestRun

    sf = get_session_factory()
    with sf() as session:
        runs = session.scalars(
            select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(min(limit, 100))
        ).all()
        return [_backtest_brief(r) for r in runs]


@router.get("/backtests/{run_id}")
def get_backtest(run_id: int):
    from ..db.models import BacktestRun

    sf = get_session_factory()
    with sf() as session:
        run = session.get(BacktestRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="回测不存在")
        data = _backtest_brief(run)
        data["result"] = run.result_json
        return data
