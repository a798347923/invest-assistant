"""B1-01 骨架冒烟测试。"""

from typer.testing import CliRunner

from invest_assistant import __version__
from invest_assistant.__main__ import app
from invest_assistant.config import get_settings


def test_version_command():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("serve", "once", "backup", "version"):
        assert cmd in result.stdout


def test_settings_defaults():
    settings = get_settings()
    assert settings.data_dir.name == "data"
    cfg = settings.app_config()
    assert cfg["poll"]["interval_minutes"] == 1
    assert cfg["screen"]["daily_candidates"] == 5
    assert cfg["screen"]["setup_required"] is True
    assert cfg["paper"]["enabled"] is True
    assert cfg["paper"]["initial_cash"] == 50000
    assert cfg["paper"]["ai_veto"] is True
    assert cfg["paper"]["ai_veto_exempt_stops"] is True
    assert cfg["paper"]["apply_replay_v3"] is True
