"""微信推送渠道：Server酱（sct.ftqq.com）。

说明：SendKey 需在 Server酱官网微信扫码获取；未配置时不启用该通道。
（PushPlus 等其它通道可按同协议在此扩展。）
"""

from __future__ import annotations

import logging
import time

import requests

from .base import DeliveryResult, Message

log = logging.getLogger(__name__)

_TITLE_MAX = 32  # Server酱标题长度限制


class ServerChanNotifier:
    name = "wechat"

    def __init__(self, sendkey: str, timeout: float = 15.0):
        self._sendkey = sendkey.strip()
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings) -> ServerChanNotifier | None:
        key = (getattr(settings, "serverchan_sendkey", "") or "").strip()
        return cls(key) if key else None

    def send(self, message: Message) -> DeliveryResult:
        started = time.perf_counter()
        try:
            url = f"https://sctapi.ftqq.com/{self._sendkey}.send"
            resp = requests.post(
                url,
                data={"title": message.title[:_TITLE_MAX], "desp": message.render()},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            payload = resp.json() if "json" in resp.headers.get("content-type", "") else {}
            code = payload.get("code", 0)
            if code not in (0, None):
                raise RuntimeError(f"ServerChan code={code} message={payload.get('message')}")
            ms = round((time.perf_counter() - started) * 1000)
            log.info("微信消息已发送: %s", message.title)
            return DeliveryResult(channel=self.name, ok=True, ms=ms)
        except Exception as exc:  # noqa: BLE001
            ms = round((time.perf_counter() - started) * 1000)
            log.warning("微信发送失败: %s", exc)
            return DeliveryResult(
                channel=self.name, ok=False, ms=ms, error=f"{type(exc).__name__}: {exc}"
            )
