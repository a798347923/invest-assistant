"""serve 冒烟测试（不实际启动阻塞循环）。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from invest_assistant import config as config_mod
from invest_assistant.__main__ import app
from invest_assistant.db import session as db_session


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def test_serve_help(fresh_env):
    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "面板" in result.output


def test_build_scheduler_registers_jobs(fresh_env):
    from invest_assistant.engine.scheduler import build_scheduler

    scheduler = build_scheduler(run_catchup_on_start=False)
    job_ids = {job.id for job in scheduler.get_jobs()}
    assert {"poll_quotes", "notify_pending", "eod_close", "paper_review", "screen_candidates", "weekly_report", "calibration", "housekeeping", "backup"} <= job_ids
    # 投递作业独立且每分钟运行（轮询不再承担 LLM/投递）
    notify = scheduler.get_job("notify_pending")
    minute_field = {f.name: str(f) for f in notify.trigger.fields}["minute"]
    assert minute_field == "*"
