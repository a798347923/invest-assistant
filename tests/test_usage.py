"""B2-09 用量看板测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from invest_assistant import config as config_mod
from invest_assistant.db import session as db_session
from invest_assistant.db.session import ensure_schema, get_session_factory
from invest_assistant.llm.usage import record_usage
from invest_assistant.web.app import create_app


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def test_usage_api_and_page(fresh_env):
    ensure_schema()
    sf = get_session_factory()
    record_usage(sf, purpose="analysis", model="deepseek-chat", tokens_in=1_000_000, tokens_out=0)
    record_usage(sf, purpose="trigger", model="deepseek-chat", tokens_in=0, tokens_out=125_000)

    client = TestClient(create_app())
    usage = client.get("/api/usage").json()
    assert usage["today_cost"] == 3.0  # 2.0 + 1.0
    assert usage["days"][0]["tokens"] == 1_125_000

    page = client.get("/usage")
    assert page.status_code == 200
    assert "LLM 用量" in page.text
    assert "analysis" in page.text


def test_usage_soft_threshold_banner(fresh_env):
    ensure_schema()
    sf = get_session_factory()
    record_usage(sf, purpose="analysis", model="deepseek-chat", tokens_in=3_000_000, tokens_out=0)

    client = TestClient(create_app())
    usage = client.get("/api/usage").json()
    assert usage["over_threshold"] is True

    page = client.get("/usage")
    assert "超过软阈值" in page.text
