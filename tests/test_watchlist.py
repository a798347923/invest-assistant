"""B2-07 自选池测试。"""

from __future__ import annotations

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


def test_watchlist_api_and_page(fresh_env):
    ensure_schema()
    client = TestClient(create_app())

    created = client.post(
        "/api/watchlist", json={"code": "sh600519", "group": "core", "note": "茅台"}
    )
    assert created.status_code == 201
    assert created.json()["code"] == "600519"

    listing = client.get("/api/watchlist").json()
    assert listing[0]["group"] == "core"

    # 重复加入 = 更新（幂等）
    again = client.post("/api/watchlist", json={"code": "600519", "note": "更新备注"})
    assert again.status_code == 201
    assert len(client.get("/api/watchlist").json()) == 1

    # 非法代码
    bad = client.post("/api/watchlist", json={"code": "abc"})
    assert bad.status_code == 422

    # 页面渲染 + 表单添加
    page = client.get("/watchlist")
    assert page.status_code == 200
    assert "600519" in page.text
    added = client.post(
        "/watchlist", data={"code": "510300", "group": "", "note": ""}, follow_redirects=False
    )
    assert added.status_code == 303
    assert "510300" in client.get("/watchlist").text

    # 页面删除
    deleted = client.post("/watchlist/600519/delete", follow_redirects=False)
    assert deleted.status_code == 303
    codes = {item["code"] for item in client.get("/api/watchlist").json()}
    assert codes == {"510300"}
    assert "510300" in client.get("/watchlist").text

    # API 删除不存在 → 404
    assert client.delete("/api/watchlist/000001").status_code == 404


def test_watchlist_alert_fields_api_and_page(fresh_env):
    ensure_schema()
    client = TestClient(create_app())

    created = client.post(
        "/api/watchlist",
        json={
            "code": "300750", "group": "core", "note": "宁德",
            "alert_pct": 4.0, "alert_price_above": 260.0, "alert_price_below": 210.0,
        },
    )
    assert created.status_code == 201
    item = client.get("/api/watchlist").json()[0]
    assert item["alert_pct"] == 4.0
    assert item["alert_price_above"] == 260.0
    assert item["alert_price_below"] == 210.0

    page = client.get("/watchlist")
    assert "异动±4%" in page.text
    assert "≥260" in page.text
    assert "≤210" in page.text

    # 表单重加 = 更新设置（关闭异动、清空上方价、改下方价）
    updated = client.post(
        "/watchlist",
        data={
            "code": "300750", "group": "core", "note": "更新",
            "alert_pct": "0", "price_above": "", "price_below": "200",
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    item2 = client.get("/api/watchlist").json()[0]
    assert item2["alert_pct"] == 0.0
    assert item2["alert_price_above"] is None
    assert item2["alert_price_below"] == 200.0
    assert "异动关闭" in client.get("/watchlist").text


def test_watchlist_ai_insight_button(fresh_env, monkeypatch):
    ensure_schema()
    client = TestClient(create_app())
    client.post("/api/watchlist", json={"code": "600519", "note": "茅台"})

    page = client.get("/watchlist")
    assert "AI 分析" in page.text
    assert "查看完整卡" not in page.text  # 还没有分析过

    def fake_analyze(self, code, name="", type=""):
        from invest_assistant.db.models import AnalysisCard as AC
        from invest_assistant.db.session import get_session_factory

        sf = get_session_factory()
        with sf() as session:
            card = AC(
                code=code, kind="stock", model="fake", prompt_ver="card_v1",
                content_json={
                    "summary": "当前处于震荡上行", "technicals": "t", "news_sentiment": "n",
                    "bull_points": [], "bear_points": [], "risks": [],
                    "action_bias": "观察", "confidence": 0.6, "data_caveats": "d",
                },
                sources_json={"prices": "fake", "news": []},
            )
            session.add(card)
            session.commit()
            return {"id": card.id, "code": code, "content": card.content_json}

    monkeypatch.setattr("invest_assistant.llm.analyzer.CardAnalyzer.analyze", fake_analyze)

    partial = client.post("/watchlist/600519/insight", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "当前处于震荡上行" in partial.text
    assert "查看完整卡" in partial.text
    assert "倾向 观察" in partial.text

    # 再次打开页面：上次分析自动展示
    page2 = client.get("/watchlist")
    assert "当前处于震荡上行" in page2.text

    # 失败路径：显示错误提示且保留上次结果
    def failing(self, code, name="", type=""):
        from invest_assistant.llm.client import LLMError

        raise LLMError("llm down")

    monkeypatch.setattr("invest_assistant.llm.analyzer.CardAnalyzer.analyze", failing)
    partial2 = client.post("/watchlist/600519/insight", headers={"HX-Request": "true"})
    assert "AI 分析失败" in partial2.text
    assert "当前处于震荡上行" in partial2.text

    # 非 htmx 回退：整页重定向
    monkeypatch.setattr("invest_assistant.llm.analyzer.CardAnalyzer.analyze", fake_analyze)
    redirected = client.post("/watchlist/600519/insight", follow_redirects=False)
    assert redirected.status_code == 303

    # 不在自选 → 404
    assert client.post("/watchlist/000001/insight").status_code == 404


def test_watchlist_plan_monitoring_toggle(fresh_env):
    """AI 买入计划：卡片计划展示 + 每行监控开关。"""
    ensure_schema()
    client = TestClient(create_app())
    client.post("/api/watchlist", json={"code": "300031", "alert_pct": 0})
    item = client.get("/api/watchlist").json()[0]
    assert item["alert_plan"] is True  # 默认开启计划监控

    from invest_assistant.db.models import AnalysisCard
    from invest_assistant.db.session import get_session_factory

    sf = get_session_factory()
    with sf() as session:
        session.add(
            AnalysisCard(
                code="300031", kind="stock", model="fake", prompt_ver="card_v3",
                content_json={
                    "summary": "等待回踩", "action_bias": "等待回踩",
                    "plan": {
                        "entry_type": "回踩", "entry_low": 21.6, "entry_high": 22.2,
                        "stop_price": 20.8, "entry_zone": "21.6~22.2",
                        "stop_level": "跌破 20.8",
                    },
                },
                sources_json={},
            )
        )
        session.commit()

    page = client.get("/watchlist")
    assert "AI 买入" in page.text
    assert "21.6~22.2" in page.text
    assert "计划监控：开" in page.text

    # htmx 局部刷新切换
    partial = client.post("/watchlist/300031/toggle-plan", headers={"HX-Request": "true"})
    assert partial.status_code == 200
    assert "计划监控：关" in partial.text
    assert "AI 买入（已关）" in partial.text
    assert client.get("/api/watchlist").json()[0]["alert_plan"] is False

    # 非 htmx 回退 + 不在自选
    assert client.post("/watchlist/300031/toggle-plan", follow_redirects=False).status_code == 303
    assert client.post("/watchlist/000001/toggle-plan").status_code == 404
