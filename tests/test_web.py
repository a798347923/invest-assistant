"""B2-01/B2-02 Web 面板测试。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from invest_assistant import config as config_mod
from invest_assistant.config import Settings
from invest_assistant.db import session as db_session
from invest_assistant.db.models import Alert
from invest_assistant.db.session import ensure_schema, get_session_factory
from invest_assistant.web.app import create_app
from invest_assistant.web.templating import WEB_DIR


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


@pytest.fixture()
def no_quotes(monkeypatch):
    """页面测试不发真实行情请求。"""
    monkeypatch.setattr(
        "invest_assistant.datasource.MarketDataService.get_quotes",
        lambda self, codes: {},
    )


def test_index_and_status(fresh_env):
    ensure_schema()
    client = TestClient(create_app())
    resp = client.get("/")
    assert resp.status_code == 200
    assert "投资助手" in resp.text
    status = client.get("/api/status").json()
    assert status["open_positions"] == 0
    assert "version" in status


def test_password_flow(fresh_env):
    ensure_schema()
    client = TestClient(create_app(Settings(panel_password="secret")))
    assert client.get("/api/status").status_code == 401
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 307
    assert page.headers["location"] == "/login"
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    good = client.post("/login", data={"password": "secret"}, follow_redirects=False)
    assert good.status_code == 303
    assert client.get("/api/status").status_code == 200


def test_health_and_vendor_assets(fresh_env):
    client = TestClient(create_app())
    assert client.get("/health").json()["ok"] is True
    vendor = WEB_DIR / "static" / "vendor"
    assert (vendor / "htmx.min.js").stat().st_size > 10_000
    assert (vendor / "echarts.min.js").stat().st_size > 100_000


def test_positions_api_and_page(fresh_env, no_quotes):
    ensure_schema()
    client = TestClient(create_app())

    resp = client.post(
        "/api/positions",
        json={
            "code": "600519", "qty": 100, "price": 1200, "strategy": "value",
            "take_profit_pct": 20, "trailing_drawdown_pct": 8, "max_holding_days": 90,
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["position_id"] >= 1

    listing = client.get("/api/positions").json()
    assert len(listing) == 1
    assert listing[0]["code"] == "600519"
    assert any(r["type"] == "trailing_stop" for r in listing[0]["rules"])

    bad = client.post(
        "/api/positions",
        json={"code": "600519", "qty": 1, "price": 1, "strategy": "value",
              "take_profit_pct": 20, "max_holding_days": 90},
    )
    assert bad.status_code == 422  # 缺止损/移动止盈

    page = client.get("/positions")
    assert page.status_code == 200
    assert "600519" in page.text
    assert "移动止盈" in page.text

    sell = client.post(
        "/positions/1/sell", data={"qty": "50", "price": "1300"}, follow_redirects=False
    )
    assert sell.status_code == 303
    assert client.get("/api/positions").json()[0]["qty"] == 50


def test_positions_page_form_create(fresh_env, no_quotes):
    ensure_schema()
    client = TestClient(create_app())
    ok = client.post(
        "/positions",
        data={
            "code": "510300", "qty": "1000", "price": "4.05", "strategy": "drip",
            "take_profit": "30", "stop_loss": "", "trailing": "10", "max_holding": "365",
            "fee": "5", "name": "", "note": "",
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    bad = client.post(
        "/positions",
        data={"code": "510300", "qty": "1000", "price": "4.05", "strategy": "drip",
              "take_profit": "", "stop_loss": "", "trailing": "", "max_holding": "",
              "fee": "0", "name": "", "note": ""},
    )
    assert bad.status_code == 422
    assert "拒绝" in bad.text or "缺失" in bad.text


def test_alerts_page_and_mark(fresh_env):
    ensure_schema()
    sf = get_session_factory()
    with sf() as session:
        alert = Alert(
            kind="trigger", status="sent", triggered_at=dt.datetime.now(),
            context_json={"code": "600519", "name": "贵州茅台", "reason": "触发止损 -10%",
                          "price": 1080.0, "cost": 1200.0, "qty": 100},
        )
        session.add(alert)
        session.commit()
        alert_id = alert.id

    client = TestClient(create_app())
    page = client.get("/alerts")
    assert page.status_code == 200
    assert "贵州茅台" not in page.text or "600519" in page.text
    assert "触发止损" in page.text

    mark = client.post(f"/alerts/{alert_id}/mark", data={"status": "handled"}, follow_redirects=False)
    assert mark.status_code == 303
    api = client.get("/api/alerts?status=handled").json()
    assert api and api[0]["status"] == "handled"

    # htmx 局部刷新路径
    with sf() as session:
        alert2 = Alert(kind="trigger", status="sent", triggered_at=dt.datetime.now(),
                       context_json={"code": "510300", "reason": "移动止盈"})
        session.add(alert2)
        session.commit()
        alert2_id = alert2.id
    partial = client.post(
        f"/alerts/{alert2_id}/mark", data={"status": "ignored"}, headers={"HX-Request": "true"}
    )
    assert partial.status_code == 200
    assert "<tr" in partial.text
    assert "已忽略" in partial.text


def test_placeholder_pages(fresh_env):
    ensure_schema()
    client = TestClient(create_app())
    for path in ("/candidates", "/cards", "/backtests", "/usage", "/settings"):
        assert client.get(path).status_code == 200, path


def test_positions_page_with_funds(fresh_env, monkeypatch):
    ensure_schema()
    import datetime as _dt

    from invest_assistant.datasource import FundNav
    from invest_assistant.db.session import get_session_factory
    from invest_assistant.domain.positions import ExitRules, PositionService

    sf = get_session_factory()
    PositionService(session_factory=sf).add_position(
        code="011186", qty=100000, price=1.0, type="otc_fund", strategy="drip",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    monkeypatch.setattr(
        "invest_assistant.datasource.MarketDataService.get_fund_nav",
        lambda self, code, days=2: [
            FundNav(code=code, date=_dt.date(2026, 10, 12), nav=0.6908)
        ],
    )
    client = TestClient(create_app())
    page = client.get("/positions")
    assert page.status_code == 200
    assert "净值(2026-10-12)" in page.text
    assert "行情获取失败" not in page.text  # 基金不再触发行情失败横幅
    assert "0.6908" in page.text


def test_alert_ai_judgement_button(fresh_env, monkeypatch):
    ensure_schema()
    sf = get_session_factory()
    with sf() as session:
        alert = Alert(
            kind="watch", status="sent", triggered_at=dt.datetime.now(),
            context_json={"code": "600519", "name": "贵州茅台",
                          "reason": "自选异动：日内上涨 +6.00%", "price": 106.0, "pct": 6.0},
        )
        session.add(alert)
        session.commit()
        alert_id = alert.id

    calls = {"n": 0}

    def fake_interpret(self, context, kind="trigger"):
        calls["n"] += 1
        return {
            "what_happened": "放量上攻", "position_status": "接近前高",
            "options": ["列入买入计划", "继续观察"], "suggestion": "继续观察",
            "confidence": 0.66,
        }

    monkeypatch.setattr(
        "invest_assistant.llm.analyzer.TriggerInterpreter.interpret", fake_interpret
    )

    client = TestClient(create_app())
    page = client.get("/alerts")
    assert page.status_code == 200
    assert "AI 判断" in page.text

    partial = client.post(f"/alerts/{alert_id}/interpret", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "放量上攻" in partial.text
    assert "✔ 已判断" in partial.text
    assert "关注要点" in partial.text  # watch 提醒用「关注要点」标签
    assert calls["n"] == 1

    # 已生成：再次点击不再调用 LLM（不重复花钱）
    partial2 = client.post(f"/alerts/{alert_id}/interpret", headers={"HX-Request": "true"})
    assert calls["n"] == 1
    assert "放量上攻" in partial2.text

    # LLM 不可用路径：显示提示、不崩溃
    def failing_interpret(self, context, kind="trigger"):
        return None

    monkeypatch.setattr(
        "invest_assistant.llm.analyzer.TriggerInterpreter.interpret", failing_interpret
    )
    with sf() as session:
        alert2 = Alert(
            kind="watch", status="sent", triggered_at=dt.datetime.now(),
            context_json={"code": "300750", "reason": "跌破下方提醒价 95"},
        )
        session.add(alert2)
        session.commit()
        alert2_id = alert2.id
    partial3 = client.post(f"/alerts/{alert2_id}/interpret", headers={"HX-Request": "true"})
    assert partial3.status_code == 200
    assert "暂不可用" in partial3.text
