"""通知分发器：多通道发送、失败重试、每通道结果记录。"""

from __future__ import annotations

import logging
import time

from ..config import get_settings
from .base import DeliveryResult, Message
from .email import EmailNotifier
from .wechat import ServerChanNotifier

log = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, notifiers: list | None = None, retries: int = 2, backoff: float = 1.5):
        self.notifiers = list(notifiers or [])
        self.retries = retries
        self.backoff = backoff

    @property
    def configured(self) -> bool:
        return bool(self.notifiers)

    def send(self, message: Message) -> list[DeliveryResult]:
        """发送到全部渠道；返回每通道的最终结果（含重试次数）。"""
        results: list[DeliveryResult] = []
        for notifier in self.notifiers:
            results.append(self._send_with_retry(notifier, message))
        return results

    def _send_with_retry(self, notifier, message: Message) -> DeliveryResult:
        last: DeliveryResult | None = None
        for attempt in range(1, self.retries + 2):
            result = notifier.send(message)
            result.attempts = attempt
            if result.ok:
                return result
            last = result
            if attempt <= self.retries:
                time.sleep(self.backoff * attempt)
        assert last is not None
        return last


def build_dispatcher(settings=None) -> Dispatcher:
    """按配置构建：邮件 + 微信（Server酱），已配置的通道都会启用。"""
    settings = settings or get_settings()
    notifiers = []
    email = EmailNotifier.from_settings(settings)
    if email is not None:
        notifiers.append(email)
    wechat = ServerChanNotifier.from_settings(settings)
    if wechat is not None:
        notifiers.append(wechat)
    if not notifiers:
        log.warning("未配置任何通知渠道（.env 中 SMTP_* / NOTIFY_EMAIL_TO / SERVERCHAN_SENDKEY）")
    return Dispatcher(notifiers)
