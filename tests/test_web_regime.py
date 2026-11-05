"""前端强弱市徽标测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    from invest_assistant import config as config_mod
    from invest_assistant.db import session as db_session

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def _patch_regime(monkeypatch, state: str, label: str) -> None:
    from invest_assistant.engine import regime_display

    monkeypatch.setattr(
        regime_display,
        "regime_snapshot",
        lambda: {
            "state": state,
            "label": label,
            "detail": f"{label} · 上证 +0.30% · 创业板 +0.10%",
            "at": "2026-10-19T10:00:00",
            "rel": [0.3, 0.1],
            "stale": False,
        },
    )


def test_dashboard_shows_strong_regime_badge(fresh_env, monkeypatch):
    _patch_regime(monkeypatch, "strong", "强市")
    from invest_assistant.db.session import ensure_schema
    from invest_assistant.web.app import create_app

    ensure_schema()
    client = TestClient(create_app())
    page = client.get("/")
    assert page.status_code == 200
    assert "强市" in page.text
    assert "regime-strong" in page.text
    partial = client.get("/partials/regime")
    assert partial.status_code == 200
    assert "强市" in partial.text


def test_regime_badge_weak_label(fresh_env, monkeypatch):
    _patch_regime(monkeypatch, "weak", "弱市")
    from invest_assistant.db.session import ensure_schema
    from invest_assistant.web.app import create_app

    ensure_schema()
    client = TestClient(create_app())
    partial = client.get("/partials/regime")
    assert partial.status_code == 200
    assert "弱市" in partial.text
    assert "regime-weak" in partial.text
