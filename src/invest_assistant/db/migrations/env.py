"""Alembic 环境：从应用配置注入数据库 URL，支持 autogenerate（SQLite batch 模式）。"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# 兜底：确保 invest_assistant 可导入（alembic.ini 的 prepend_sys_path 之外）
SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from invest_assistant.config import get_settings  # noqa: E402
from invest_assistant.db import models  # noqa: E402,F401  确保全部模型注册到 metadata
from invest_assistant.db.models import Base  # noqa: E402
from invest_assistant.db.session import make_engine  # noqa: E402

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    settings = get_settings()
    settings.ensure_dirs()
    return f"sqlite:///{settings.data_dir / 'invest.db'}"


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = make_engine(_url())
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
