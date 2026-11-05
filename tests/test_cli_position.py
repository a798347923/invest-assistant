"""B1-04 CLI 集成测试（临时 DATA_DIR + 临时数据库）。"""

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


def test_cli_add_and_list(fresh_env):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "position", "add", "600519",
            "--qty", "100", "--price", "1200", "--strategy", "value",
            "--take-profit", "20", "--trailing", "8", "--max-holding", "90",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "已建仓" in result.output

    result2 = runner.invoke(app, ["position", "list"])
    assert result2.exit_code == 0
    assert "600519" in result2.output


def test_cli_add_rejected_when_rules_incomplete(fresh_env):
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "position", "add", "600519",
            "--qty", "100", "--price", "1200", "--strategy", "value",
            "--take-profit", "20", "--max-holding", "90",
        ],
    )
    assert result.exit_code == 1
    assert "拒绝保存" in result.output


def test_cli_sell_and_dividend(fresh_env):
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "position", "add", "600519",
            "--qty", "100", "--price", "100", "--strategy", "swing",
            "--take-profit", "20", "--stop-loss", "10", "--max-holding", "60",
        ],
    )
    result = runner.invoke(app, ["position", "sell", "1", "--qty", "50", "--price", "110"])
    assert result.exit_code == 0, result.output
    assert "已实现盈亏" in result.output

    result2 = runner.invoke(app, ["dividend", "add", "1", "--amount", "30", "--ex-date", "2026-10-01"])
    assert result2.exit_code == 0, result2.output
    assert "新成本" in result2.output
