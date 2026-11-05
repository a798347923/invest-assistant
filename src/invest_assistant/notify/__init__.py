"""通知层：消息类型、渠道（邮件/微信）、模板与分发器。"""

from .base import DISCLAIMER, DeliveryResult, Message, Notifier
from .dispatcher import Dispatcher, build_dispatcher
from .email import EmailNotifier
from .templates import system_message, trigger_message, watch_message
from .wechat import ServerChanNotifier

__all__ = [
    "DISCLAIMER",
    "DeliveryResult",
    "Dispatcher",
    "EmailNotifier",
    "Message",
    "Notifier",
    "ServerChanNotifier",
    "build_dispatcher",
    "system_message",
    "trigger_message",
    "watch_message",
]
