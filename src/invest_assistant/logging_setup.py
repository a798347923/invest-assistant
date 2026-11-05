"""日志初始化：队列 → 滚动文件（logs/app.log，5MB × 5）。

实现要点：根 logger 只挂 QueueHandler，由独立监听线程写文件——
终端被选中"冻结"（Windows 快速编辑）/ 磁盘卡顿都不会阻塞业务线程。
历史事故（2026-10-15）：终端冻结导致所有写日志的线程挂死在控制台 handler，
自选池「AI 分析」等一切带日志输出的请求永不返回，调度器心跳也停摆。
故 serve 模式不再向终端输出日志（启动横幅会提示日志文件路径）。
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
from pathlib import Path

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_listener: logging.handlers.QueueListener | None = None


def setup_logging(logs_dir: Path, level: str = "INFO") -> Path:
    """幂等初始化；返回日志文件路径。"""
    global _listener
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"

    root = logging.getLogger()
    root.setLevel(level.upper())
    if _listener is not None:
        return log_file

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FMT))

    log_queue: queue.SimpleQueue = queue.SimpleQueue()
    root.addHandler(logging.handlers.QueueHandler(log_queue))
    _listener = logging.handlers.QueueListener(log_queue, file_handler)
    _listener.start()
    return log_file
