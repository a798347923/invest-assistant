"""B1-07 通知层测试（假 SMTP / 假渠道，无网络）。"""

from __future__ import annotations

import smtplib

from invest_assistant.config import Settings
from invest_assistant.notify import (
    DeliveryResult,
    Dispatcher,
    EmailNotifier,
    Message,
    build_dispatcher,
    system_message,
    trigger_message,
)
from invest_assistant.notify.email import EmailNotifier as EmailNotifierCls


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.login_args = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, email):
        self.sent.append(email)


class FailSMTP(FakeSMTP):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"auth failed")


def _blank_smtp_settings() -> Settings:
    """显式清空 SMTP（避免读到本机 .env 的真实配置，保证测试与环境无关）。"""
    return Settings(
        smtp_host="", smtp_user="", smtp_password="", notify_email_to="",
        serverchan_sendkey="",
    )


def test_email_send_ok(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    notifier = EmailNotifier(
        host="smtp.test", port=465, user="u@test.com", password="x", to=["to@test.com"]
    )
    result = notifier.send(Message(title="标题", body="正文内容"))
    assert result.ok is True
    assert result.channel == "email"
    fake = FakeSMTP.instances[0]
    assert fake.login_args == ("u@test.com", "x")
    email = fake.sent[0]
    assert email["Subject"] == "标题"
    content = email.get_content()
    assert "正文内容" in content
    assert "不构成投资建议" in content  # 免责尾注自动附加


def test_email_send_failure(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", FailSMTP)
    notifier = EmailNotifier(
        host="smtp.test", port=465, user="u@test.com", password="bad", to=["to@test.com"]
    )
    result = notifier.send(Message(title="T", body="B"))
    assert result.ok is False
    assert "SMTPAuthenticationError" in result.error


def test_email_from_settings():
    settings = Settings(
        smtp_host="smtp.qq.com", smtp_user="u@qq.com", smtp_password="p",
        notify_email_to="a@b.c, d@e.f",
    )
    notifier = EmailNotifierCls.from_settings(settings)
    assert notifier is not None
    assert notifier._to == ["a@b.c", "d@e.f"]  # noqa: SLF001

    assert EmailNotifierCls.from_settings(_blank_smtp_settings()) is None


class FlakyNotifier:
    name = "flaky"

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def send(self, message: Message) -> DeliveryResult:
        self.calls += 1
        if self.calls <= self.failures:
            return DeliveryResult(channel=self.name, ok=False, ms=1, error="boom")
        return DeliveryResult(channel=self.name, ok=True, ms=1)


def test_dispatcher_retries_then_succeeds():
    notifier = FlakyNotifier(failures=2)
    dispatcher = Dispatcher([notifier], retries=2, backoff=0.01)
    results = dispatcher.send(Message(title="T", body="B"))
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].attempts == 3
    assert notifier.calls == 3


def test_dispatcher_all_fail():
    notifier = FlakyNotifier(failures=99)
    dispatcher = Dispatcher([notifier], retries=2, backoff=0.01)
    results = dispatcher.send(Message(title="T", body="B"))
    assert results[0].ok is False
    assert results[0].attempts == 3


def test_dispatcher_multi_channel_partial():
    ok_notifier = FlakyNotifier(failures=0)
    ok_notifier.name = "ok"
    bad_notifier = FlakyNotifier(failures=99)
    dispatcher = Dispatcher([ok_notifier, bad_notifier], retries=0, backoff=0.01)
    results = dispatcher.send(Message(title="T", body="B"))
    assert [r.ok for r in results] == [True, False]


def test_build_dispatcher_empty_when_unconfigured():
    dispatcher = build_dispatcher(_blank_smtp_settings())
    assert dispatcher.configured is False
    assert dispatcher.send(Message(title="T", body="B")) == []


def test_trigger_message_template():
    message = trigger_message(
        {
            "code": "600519",
            "name": "贵州茅台",
            "reason": "自峰值 1263.00 回撤 8%（止盈线 1161.96）",
            "price": 1160.0,
            "cost": 1200.05,
            "qty": 100,
        }
    )
    assert "600519" in message.title
    assert "回撤 8%" in message.body
    assert "浮动盈亏" in message.body
    assert "不构成投资建议" in message.render()


def test_trigger_message_catchup_prefix_and_system_template():
    message = trigger_message({"code": "600519", "reason": "x"}, catchup=True, expired=True)
    assert message.title.startswith("【已过期，请复核】")
    warning = system_message("行情连续 3 轮失败")
    assert "系统异常" in warning.title
    assert "连续 3 轮" in warning.body
