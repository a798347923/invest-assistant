"""运行时护栏：把「靠约定」变成「启动即检查」。

两条约束来自实际风险：

1. **时区**：全项目使用 naive 本地时间（`datetime.now()`），调度器按 `Asia/Shanghai` 注册作业。
   若 Windows 时区被改动（出差、误设、UTC），交易时段判断、持有天数、提醒时间会**静默**漂移 8 小时——
   没有任何异常，只是所有结论都错。启动时比对 UTC 偏移并显式告警。
2. **绑定地址**：面板默认只监听 127.0.0.1，且密码可选。一旦 `--host 0.0.0.0`，持仓、提醒历史与
   `/api/*` 会暴露给同网段；默认无密码时等于裸奔。非回环地址必须显式配置密码。

这两个函数都是纯函数，便于单独测试。
"""

from __future__ import annotations

import datetime as dt

# 中国市场固定 UTC+8（无夏令时），与调度器 TIMEZONE 一致
EXPECTED_OFFSET_HOURS = 8.0

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def local_offset_hours() -> float:
    """本机当前相对 UTC 的偏移小时数（naive 时间按系统本地时区解释）。"""
    offset = dt.datetime.now().astimezone().utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 3600


def timezone_warning_for(offset_hours: float) -> str:
    """偏移不符预期时返回告警文案；符合返回空串。"""
    if abs(offset_hours - EXPECTED_OFFSET_HOURS) < 1e-9:
        return ""
    return (
        f"时区告警：本机相对 UTC 偏移为 UTC{offset_hours:+g}，与系统假定的 UTC+8（Asia/Shanghai）不一致。"
        "交易时段判定、持有天数与提醒时间会整体偏移，请在 Windows「设置 → 日期和时间」"
        "把时区改为 (UTC+08:00) 北京/重庆/香港。"
    )


def timezone_warning() -> str:
    return timezone_warning_for(local_offset_hours())


def is_loopback(host: str) -> bool:
    """是否仅本机可访问（含 127.0.0.0/8 任意地址）。"""
    candidate = (host or "").strip().lower()
    if candidate in _LOOPBACK_HOSTS:
        return True
    return candidate.startswith("127.")


def binding_error(host: str, password_configured: bool) -> str:
    """非回环绑定且未设密码时返回拒绝理由；允许则返回空串。

    注意 `""`/`0.0.0.0`/`::` 都按"对外暴露"处理——uvicorn 对空 host 会监听全部网卡。
    """
    if is_loopback(host) or password_configured:
        return ""
    shown = host.strip() or "(未指定，等价于全部网卡)"
    return (
        f"拒绝启动：--host {shown} 会监听非本机地址，但未设置 PANEL_PASSWORD。"
        "面板包含持仓、提醒历史与可写 API，无密码暴露到局域网有风险。"
        "请在 .env 设置 PANEL_PASSWORD 后重启，或改回 --host 127.0.0.1。"
    )
