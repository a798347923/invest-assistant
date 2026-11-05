"""数据库引擎与会话工厂（SQLite + WAL）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def make_engine(url: str) -> Engine:
    """创建引擎；SQLite 时启用 WAL、外键约束与忙等待。"""
    engine = create_engine(url, future=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = make_engine(f"sqlite:///{settings.data_dir / 'invest.db'}")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _session_factory


def reset_engine() -> None:
    """清空全局引擎/会话工厂（测试与配置切换时使用）。"""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


def ensure_schema() -> None:
    """确保数据库结构为最新（等价于 alembic upgrade head，幂等）。"""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    settings.ensure_dirs()
    cfg = Config(str(settings.root_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    cfg.attributes["configure_logger"] = False  # 不覆盖应用自身的日志配置
    command.upgrade(cfg, "head")


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话：正常提交，异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
