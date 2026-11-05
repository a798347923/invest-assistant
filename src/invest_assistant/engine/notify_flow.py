"""通知流：pending 提醒的「解读 → 投递」闭环（含过期标注与失败记录）。

- 持仓规则触发（kind=trigger）：生成 AI 解读（持久化缓存），LLM 失败降级为模板消息；
- 自选池提醒（kind=watch）：轻量模板消息，不调用 LLM；
- **与轮询解耦**：轮询作业只负责落库（本地判断），本模块由独立的 `notify_job` 作业驱动，
  一次慢 LLM / 慢 SMTP 不会推迟下一分钟的纪律监控。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..db.models import Alert
from ..db.session import get_session_factory
from ..llm.analyzer import TriggerInterpreter
from ..notify import Dispatcher, build_dispatcher, system_message, trigger_message, watch_message

log = logging.getLogger(__name__)

EXPIRY_HOURS = 24


class NotificationFlow:
    def __init__(self, dispatcher: Dispatcher | None = None, session_factory=None, interpreter=None):
        self._sf = session_factory or get_session_factory()
        self.dispatcher = dispatcher or build_dispatcher()
        self.interpreter = interpreter or TriggerInterpreter(session_factory=self._sf)

    def process_pending(self, limit: int = 50) -> dict:
        """解读并发送所有 pending/failed 的提醒；更新状态与投递记录。"""
        report = {"sent": 0, "failed": 0, "expired": 0, "skipped": 0, "interpreted": 0}
        if not self.dispatcher.configured:
            report["skipped"] = -1
            return report
        now = dt.datetime.now()
        with self._sf() as session:
            alerts = session.scalars(
                select(Alert)
                .where(Alert.status.in_(("pending", "failed")))
                .order_by(Alert.triggered_at)
                .limit(limit)
            ).all()
            for alert in alerts:
                if alert.interpretation_json is None and alert.kind != "watch":
                    interpretation = self.interpreter.interpret(alert.context_json or {})
                    if interpretation is not None:
                        alert.interpretation_json = interpretation
                        report["interpreted"] += 1

                expired = (now - alert.triggered_at) > dt.timedelta(hours=EXPIRY_HOURS)
                if alert.kind == "watch":
                    message = watch_message(alert.context_json or {}, expired=expired)
                else:
                    message = trigger_message(
                        alert.context_json or {},
                        catchup=bool(alert.is_catchup),
                        expired=expired,
                        interpretation=alert.interpretation_json,
                    )
                results = self.dispatcher.send(message)
                ok = any(r.ok for r in results)
                alert.delivery_json = {
                    "sent_at": now.isoformat(timespec="seconds"),
                    "results": [vars(r) for r in results],
                }
                alert.status = "sent" if ok else "failed"
                report["sent" if ok else "failed"] += 1
                if expired:
                    report["expired"] += 1
            session.commit()
        if report["sent"] or report["failed"]:
            log.info("通知投递: %s", report)
        return report

    def send_system_warning(self, warning: str) -> bool:
        if not self.dispatcher.configured:
            return False
        results = self.dispatcher.send(system_message(warning))
        return any(r.ok for r in results)


def poll_job(poller, flow: NotificationFlow):
    """轮询作业：本地规则判定 + 提醒落库；仅「系统告警」在此即时发送。

    刻意**不做** LLM 解读与常规投递：那部分由 `notify_job` 独立作业消费 pending 提醒。
    轮询对本机的承诺是「纯本地判断、快速返回」，不能被模型或 SMTP 的延迟拖住。
    """

    def _run():
        result = poller.run_once()
        if result.system_warning:
            flow.send_system_warning(result.system_warning)
        return result.summary()

    return _run


def notify_job(flow: NotificationFlow):
    """投递作业：解读并发送 pending/failed 提醒（LLM + 邮件/微信）。"""

    def _run():
        report = flow.process_pending()
        return f"通知投递：{report}"

    return _run
