"""日志初始化测试：队列化 + 仅文件（不挂控制台 handler，终端冻结不阻塞业务）。"""

from __future__ import annotations

import logging
import logging.handlers

from invest_assistant import logging_setup


def test_setup_logging_file_only_and_queue(tmp_path):
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_listener = logging_setup._listener
    logging_setup._listener = None
    try:
        before_ids = {id(h) for h in root.handlers}
        log_file = logging_setup.setup_logging(tmp_path)
        assert log_file == tmp_path / "app.log"
        added = [h for h in root.handlers if id(h) not in before_ids]
        # 新增的 handler 只有队列（历史事故：终端冻结拖死全部写日志的线程）
        assert any(isinstance(h, logging.handlers.QueueHandler) for h in added)
        assert not any(type(h) is logging.StreamHandler for h in added)

        before = len(root.handlers)
        logging_setup.setup_logging(tmp_path)  # 幂等：重复调用不追加 handler
        assert len(root.handlers) == before

        logging.getLogger("test.logging").info("写一条测试日志")
        logging_setup._listener.stop()  # 停监听会先冲掉队列
        content = log_file.read_text(encoding="utf-8")
        assert "写一条测试日志" in content
    finally:
        if logging_setup._listener is not None and logging_setup._listener is not saved_listener:
            logging_setup._listener.stop()
        logging_setup._listener = saved_listener
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
