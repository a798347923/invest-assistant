"""B2-03 微信（Server酱）通道测试。"""

from __future__ import annotations

from invest_assistant.config import Settings
from invest_assistant.notify import Message, build_dispatcher
from invest_assistant.notify.wechat import ServerChanNotifier


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"content-type": "application/json"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _blank_settings(**overrides) -> Settings:
    base = dict(
        smtp_host="", smtp_user="", smtp_password="", notify_email_to="", serverchan_sendkey=""
    )
    base.update(overrides)
    return Settings(**base)


def test_serverchan_send_ok(monkeypatch):
    captured = {}

    def fake_post(url, data=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        return FakeResponse({"code": 0, "message": "ok"})

    monkeypatch.setattr("invest_assistant.notify.wechat.requests.post", fake_post)
    result = ServerChanNotifier("SCT_test_key").send(Message(title="【测试】标题", body="正文"))
    assert result.ok is True
    assert result.channel == "wechat"
    assert "SCT_test_key.send" in captured["url"]
    assert captured["data"]["title"].startswith("【测试】")
    assert "不构成投资建议" in captured["data"]["desp"]


def test_serverchan_send_failure(monkeypatch):
    monkeypatch.setattr(
        "invest_assistant.notify.wechat.requests.post",
        lambda url, data=None, timeout=None: FakeResponse({"code": 40001, "message": "bad key"}),
    )
    result = ServerChanNotifier("bad").send(Message(title="t", body="b"))
    assert result.ok is False
    assert "40001" in result.error


def test_from_settings_and_dispatcher_wiring():
    assert ServerChanNotifier.from_settings(_blank_settings()) is None
    assert build_dispatcher(_blank_settings()).configured is False

    only_wechat = build_dispatcher(_blank_settings(serverchan_sendkey="SCT_x"))
    assert only_wechat.configured is True
    assert [n.name for n in only_wechat.notifiers] == ["wechat"]

    both = build_dispatcher(
        _blank_settings(
            serverchan_sendkey="SCT_x",
            smtp_host="smtp.qq.com",
            smtp_user="u@qq.com",
            smtp_password="p",
            notify_email_to="u@qq.com",
        )
    )
    assert [n.name for n in both.notifiers] == ["email", "wechat"]
