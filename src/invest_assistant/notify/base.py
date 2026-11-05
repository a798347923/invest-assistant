"""通知层公共类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

DISCLAIMER = (
    "——\n"
    "本消息由个人工具自动生成，仅供决策参考，不构成投资建议。投资有风险，请自行判断。"
)


@dataclass
class Message:
    title: str
    body: str
    footer: str = DISCLAIMER

    def render(self) -> str:
        return f"{self.body}\n\n{self.footer}" if self.footer else self.body


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    ms: int
    error: str = ""
    attempts: int = 1


class Notifier(Protocol):
    name: str

    def send(self, message: Message) -> DeliveryResult: ...
