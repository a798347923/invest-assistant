"""回测执行器：构建标的池与数据 → 跑引擎 → 落库（支持后台线程 + 进度）。"""

from __future__ import annotations

import datetime as dt
import logging

from ..db.models import BacktestRun
from ..db.session import get_session_factory
from .engine import run_backtest

log = logging.getLogger(__name__)


def pick_universe(snapshot_rows: list[dict], top_n: int = 150) -> list[str]:
    """按成交额取前 N 作为回测标的池（当前快照子集，存在幸存者偏差）。"""
    rows = sorted(snapshot_rows, key=lambda r: r.get("amount") or 0, reverse=True)
    return [str(r["code"]) for r in rows[:top_n]]


def load_bars(data, codes: list[str], start: dt.date, end: dt.date) -> dict[str, list]:
    bars: dict[str, list] = {}
    for code in codes:
        try:
            bars[code] = data.get_daily_bars(code, start, end, adjust="qfq")
        except Exception as exc:  # noqa: BLE001 — 单只无数据跳过
            log.debug("回测取行情失败 %s: %s", code, exc)
    return bars


def execute_backtest(run_id: int, session_factory=None, data=None, snapshot=None) -> None:
    """执行一次回测并更新 BacktestRun。异常路径写 status=error。"""
    sf = session_factory or get_session_factory()
    with sf() as session:
        run_row = session.get(BacktestRun, run_id)
        if run_row is None:
            return
        params = dict(run_row.params_json or {})
        run_row.status = "running"
        run_row.progress = 0.0
        session.commit()

    try:
        start = dt.date.fromisoformat(params["start"])
        end = dt.date.fromisoformat(params["end"])

        if data is None:
            from ..datasource import MarketDataService

            data = MarketDataService(session_factory=sf)

        codes = params.get("codes") or None
        if not codes:
            if snapshot is not None:
                rows, source = snapshot, "inline"
            else:
                rows, source = data.get_market_snapshot()
            codes = pick_universe(rows, top_n=int(params.get("universe_top", 150)))
            params["universe_source"] = source
        params["universe_codes"] = len(codes)

        bars_by_code = load_bars(data, codes, start, end)
        benchmark_bars = None
        try:
            benchmark_bars = data.get_daily_bars("000300", start, end, adjust="raw")
        except Exception:  # noqa: BLE001 — 基准可选
            try:
                benchmark_bars = data.sina.get_daily_bars("sh000300", start, end)
            except Exception as exc:  # noqa: BLE001
                log.warning("基准行情获取失败: %s", exc)

        last_progress = {"pct": -5.0}

        def progress(pct: float) -> None:
            value = round(pct * 100, 1)
            if value - last_progress["pct"] < 5:  # 每 5% 落一次库
                return
            last_progress["pct"] = value
            with sf() as session:
                row = session.get(BacktestRun, run_id)
                if row is not None:
                    row.progress = value
                    session.commit()

        result = run_backtest(params, bars_by_code, benchmark_bars, progress_cb=progress)
        if result.get("error"):
            raise RuntimeError(result["error"])

        with sf() as session:
            row = session.get(BacktestRun, run_id)
            if row is not None:
                row.result_json = result
                row.params_json = params
                row.status = "done"
                row.progress = 100.0
                session.commit()
        log.info("回测 #%s 完成: %s", run_id, result.get("stats"))
    except Exception as exc:  # noqa: BLE001
        log.exception("回测 #%s 失败", run_id)
        with sf() as session:
            row = session.get(BacktestRun, run_id)
            if row is not None:
                row.status = "error"
                row.error = f"{type(exc).__name__}: {exc}"[:500]
                session.commit()
