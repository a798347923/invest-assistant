"""运行时护栏测试：时区偏移告警 + 非本机绑定强制密码。"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from invest_assistant import config as config_mod
from invest_assistant.__main__ import app
from invest_assistant.db import session as db_session
from invest_assistant.guards import (
    binding_error,
    is_loopback,
    timezone_warning_for,
)


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PANEL_PASSWORD", "")
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


# ---------------------------------------------------------------- 时区

def test_timezone_matches_expected():
    assert timezone_warning_for(8.0) == ""


def test_timezone_mismatch_warns_with_actual_offset():
    warning = timezone_warning_for(9.0)
    assert warning
    assert "UTC+9" in warning
    assert "UTC+8" in warning


def test_timezone_utc_detected():
    assert "UTC+0" in timezone_warning_for(0.0)


# ---------------------------------------------------------------- 绑定

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "localhost", "LOCALHOST", "::1"])
def test_loopback_detected(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", ""])
def test_non_loopback_detected(host):
    assert is_loopback(host) is False


def test_loopback_without_password_allowed():
    assert binding_error("127.0.0.1", password_configured=False) == ""


def test_public_bind_with_password_allowed():
    assert binding_error("0.0.0.0", password_configured=True) == ""


def test_public_bind_without_password_refused():
    error = binding_error("0.0.0.0", password_configured=False)
    assert "PANEL_PASSWORD" in error
    assert "拒绝启动" in error


def test_serve_refuses_public_bind_without_password(fresh_env):
    result = CliRunner().invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "PANEL_PASSWORD" in result.output
