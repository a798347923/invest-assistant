"""数据库备份（SQLite backup API + 按份数轮转）。"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from ..config import get_settings

log = logging.getLogger(__name__)


def backup_database(keep: int = 30) -> Path:
    """备份到 data/backups/，保留最近 keep 份（按修改时间轮转）。"""
    settings = get_settings()
    settings.ensure_dirs()
    source_path = settings.data_dir / "invest.db"
    if not source_path.exists():
        raise FileNotFoundError(f"数据库不存在：{source_path}（先运行任意命令完成初始化）")

    dest_dir = settings.data_dir / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"invest-{stamp}.db"
    seq = 1
    while dest.exists():
        dest = dest_dir / f"invest-{stamp}-{seq}.db"
        seq += 1

    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(dest)) as target:
        source.backup(target)

    if keep > 0:
        files = sorted(dest_dir.glob("invest-*.db"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError as exc:  # 可能被其他进程占用（杀毒/同步盘），跳过即可
                log.warning("旧备份删除失败: %s (%s)", old, exc)
    log.info("数据库已备份: %s", dest)
    return dest
