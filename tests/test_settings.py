"""设置页测试：展示 / 保存覆盖 / 备份 / 测试通知（无网络）。"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from invest_assistant import config as config_mod
from invest_assistant.config import get_settings
from invest_assistant.db import session as db_session
from invest_assistant.db.session import ensure_schema
from invest_assistant.notify import DeliveryResult, Dispatcher
from invest_assistant.web.app import create_app


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


def test_settings_page_renders(fresh_env):
    ensure_schema()
    client = TestClient(create_app())
    page = client.get("/settings")
    assert page.status_code == 200
    for token in ("运行参数", "密钥与通知", "模拟盘", "保存设置", "最近作业", "调度心跳", "自选异动提醒"):
        assert token in page.text, token


def test_settings_save_writes_overrides(fresh_env):
    ensure_schema()
    client = TestClient(create_app())
    resp = client.post(
        "/settings",
        data={
            "poll_interval_minutes": "5",
            "poll_windows": "09:30-11:30, 13:00-15:00",
            "watchlist_default_alert_pct": "4.5",
            "watchlist_movement_alerts": "on",
            "notify_daily_push_limit": "40",
            "screen_daily_candidates": "6",
            "screen_setup_eval_limit": "30",
            "screen_setup_required": "on",
            "paper_enabled": "on",
            "paper_ai_veto": "on",
            "paper_ai_veto_exempt_stops": "on",
            "paper_apply_replay_v3": "on",
            "paper_ai_veto_cooldown_minutes": "20",
            "paper_ai_veto_max_per_day": "6",
            "paper_initial_cash": "80000",
            "paper_position_fraction": "0.25",
            "paper_max_positions": "4",
            "paper_max_position_weight": "0.3",
            "llm_model": "deepseek-chat",
            "llm_deep_model": "deepseek-reasoner",
            "llm_soft_daily_cost_alert_cny": "5",
            "backup_keep": "30",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    settings = get_settings()
    override = yaml.safe_load(settings.override_file.read_text(encoding="utf-8"))
    assert override["paper"]["initial_cash"] == 80000.0
    assert override["paper"]["ai_veto"] is True
    assert override["paper"]["apply_replay_v3"] is True
    assert override["paper"]["ai_veto_cooldown_minutes"] == 20
    assert override["poll"]["interval_minutes"] == 5
    assert override["poll"]["windows"] == ["09:30-11:30", "13:00-15:00"]
    assert override["watchlist"]["movement_alerts"] is True
    assert override["llm"]["deep_model"] == "deepseek-reasoner"
    merged = settings.app_config()
    assert merged["paper"]["initial_cash"] == 80000.0
    assert merged["poll"]["interval_minutes"] == 5

    page = client.get("/settings")
    assert "80000" in page.text  # 回显新值

    # 非法值：提示错误且不写入（旧覆盖完好）
    bad = client.post("/settings", data={"paper_initial_cash": "abc"})
    assert bad.status_code == 422
    assert "不是有效数值" in bad.text
    override2 = yaml.safe_load(settings.override_file.read_text(encoding="utf-8"))
    assert override2["paper"]["initial_cash"] == 80000.0
    assert override2["poll"]["interval_minutes"] == 5


def test_settings_backup_button(fresh_env):
    ensure_schema()
    client = TestClient(create_app())
    resp = client.post("/settings/backup", follow_redirects=False)
    assert resp.status_code == 303
    settings = get_settings()
    backups = list((settings.data_dir / "backups").glob("invest-*.db"))
    assert backups
    page = client.get(resp.headers["location"])
    assert "备份完成" in page.text


def test_settings_test_notify(fresh_env, monkeypatch):
    ensure_schema()
    notifier = RecordingNotifier()
    dispatcher = Dispatcher([notifier], retries=0, backoff=0)
    monkeypatch.setattr("invest_assistant.notify.build_dispatcher", lambda: dispatcher)
    client = TestClient(create_app())
    resp = client.post("/settings/test-notify", follow_redirects=False)
    assert resp.status_code == 303
    page = client.get(resp.headers["location"])
    assert "测试通知已发送" in page.text
    assert notifier.messages and "测试通知" in notifier.messages[0].title
