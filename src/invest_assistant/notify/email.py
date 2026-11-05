"""邮件通知（SMTP SSL，如 QQ/163 邮箱授权码）。"""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage

from .base import DeliveryResult, Message

log = logging.getLogger(__name__)


class EmailNotifier:
    name = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        to: list[str],
        timeout: float = 15.0,
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._to = [addr for addr in to if addr]
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings) -> EmailNotifier | None:
        """从应用配置构建；未配置完整时返回 None。"""
        to = [a.strip() for a in (settings.notify_email_to or "").split(",") if a.strip()]
        if not (settings.smtp_host and settings.smtp_user and settings.smtp_password and to):
            return None
        return cls(
            host=settings.smtp_host,
            port=int(settings.smtp_port or 465),
            user=settings.smtp_user,
            password=settings.smtp_password,
            to=to,
        )

    def send(self, message: Message) -> DeliveryResult:
        started = time.perf_counter()
        try:
            email = EmailMessage()
            email["Subject"] = message.title
            email["From"] = self._user
            email["To"] = ", ".join(self._to)
            email.set_content(message.render(), charset="utf-8")
            with smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.login(self._user, self._password)
                smtp.send_message(email)
            ms = round((time.perf_counter() - started) * 1000)
            log.info("邮件已发送: %s", message.title)
            return DeliveryResult(channel=self.name, ok=True, ms=ms)
        except Exception as exc:  # noqa: BLE001
            ms = round((time.perf_counter() - started) * 1000)
            log.warning("邮件发送失败: %s", exc)
            return DeliveryResult(
                channel=self.name, ok=False, ms=ms, error=f"{type(exc).__name__}: {exc}"
            )
