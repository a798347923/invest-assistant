"""B1-09 备份与部署脚本测试。"""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from typer.testing import CliRunner

from invest_assistant import config as config_mod
from invest_assistant.__main__ import app
from invest_assistant.config import get_settings
from invest_assistant.db import session as db_session
from invest_assistant.db.backup import backup_database


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def test_backup_and_rotation(fresh_env):
    settings = get_settings()
    settings.ensure_dirs()
    db = settings.data_dir / "invest.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("create table t(x)")
        conn.execute("insert into t values (1)")
        conn.commit()

    dest1 = backup_database(keep=2)
    assert dest1.exists()
    assert dest1.parent.name == "backups"
    with closing(sqlite3.connect(dest1)) as conn:
        assert conn.execute("select count(*) from t").fetchone()[0] == 1

    backup_database(keep=2)
    dest3 = backup_database(keep=2)
    files = sorted((settings.data_dir / "backups").glob("invest-*.db"))
    assert len(files) == 2  # 轮转保留 2 份
    assert dest3 in files


def test_once_backup(fresh_env):
    runner = CliRunner()
    result = runner.invoke(app, ["once", "backup"])
    assert result.exit_code == 0, result.output
    assert "已备份" in result.output


def test_backup_command(fresh_env):
    runner = CliRunner()
    result = runner.invoke(app, ["backup"])
    assert result.exit_code == 0, result.output
    assert "已备份" in result.output
