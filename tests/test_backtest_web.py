"""回测 Web 层测试（打桩执行器，无网络）。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from invest_assistant import config as config_mod
from invest_assistant.db import session as db_session
from invest_assistant.db.session import ensure_schema
from invest_assistant.web.app import create_app


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def _fake_execute(run_id, session_factory=None, data=None, snapshot=None):
    from invest_assistant.db.models import BacktestRun
    from invest_assistant.db.session import get_session_factory

    sf = session_factory or get_session_factory()
    with sf() as session:
        row = session.get(BacktestRun, run_id)
        row.result_json = {
            "stats": {
                "total_return_pct": 12.5, "annualized_pct": 8.1, "max_drawdown_pct": -6.4,
                "trades": 3, "win_rate_pct": 66.67, "avg_holding_days": 10.0,
                "benchmark_return_pct": 4.0, "final": 225000.0,
            },
            "equity": [["2026-01-02", 200000], ["2026-02-02", 210000], ["2026-03-02", 225000]],
            "benchmark": [["2026-01-02", 200000], ["2026-02-02", 205000], ["2026-03-02", 208000]],
            "trades": [
                {"code": "600519", "entry_date": "2026-01-05", "exit_date": "2026-01-20",
                 "entry": 100.0, "exit": 110.0, "qty": 100, "pnl": 950.0, "reason": "止盈 +20%"}
            ],
            "notes": ["测试备注：无未来函数"],
            "params": row.params_json,
        }
        row.status = "done"
        row.progress = 100.0
        session.commit()


def test_backtest_api_and_page(fresh_env, monkeypatch):
    ensure_schema()
    monkeypatch.setattr("invest_assistant.backtest.runner.execute_backtest", _fake_execute)
    client = TestClient(create_app())

    resp = client.post(
        "/api/backtests",
        json={"start": "2026-01-01", "end": "2026-03-01", "sync": True},
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]

    listing = client.get("/api/backtests").json()
    assert listing and listing[0]["id"] == run_id
    assert listing[0]["status"] == "done"

    detail = client.get(f"/api/backtests/{run_id}").json()
    assert detail["result"]["stats"]["total_return_pct"] == 12.5

    page = client.get(f"/backtests?run_id={run_id}")
    assert page.status_code == 200
    assert "12.50" in page.text
    assert "equity-chart" in page.text
    assert "600519" in page.text
    assert "无未来函数" in page.text

    # 页面表单（后台线程调用被打桩的执行器）
    form = client.post(
        "/backtests/run",
        data={
            "start": "2026-01-01", "end": "2026-03-01", "slots": "5",
            "initial_cash": "200000", "take_profit": "20", "stop_loss": "10",
            "trailing": "8", "max_holding": "60", "universe_top": "50",
        },
        follow_redirects=False,
    )
    assert form.status_code == 303
    time.sleep(0.3)
    assert len(client.get("/api/backtests").json()) == 2

    missing = client.get("/api/backtests/9999")
    assert missing.status_code == 404
