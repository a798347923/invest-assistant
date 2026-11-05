"""强/弱市状态的前端展示快照（只读 kv，不触发行情请求）。

状态由调度器/轮询/初筛写入 kv（`market_regime`），本模块只负责读取与展示；
页面渲染绝不会因为拉行情而变慢。
"""

from __future__ import annotations

import datetime as dt
import threading

from ..db.models import KvSetting
from ..db.session import get_session_factory

_lock = threading.Lock()
_cache: dict = {"at": None, "payload": None}
_TTL = dt.timedelta(seconds=60)
_STALE_AFTER = dt.timedelta(hours=2)


def clear_cache() -> None:
    with _lock:
        _cache["at"] = None
        _cache["payload"] = None


def _read_kv() -> dict:
    try:
        with get_session_factory()() as session:
            row = session.get(KvSetting, "market_regime")
            return dict(row.value_json or {}) if row else {}
    except Exception:  # noqa: BLE001 — 展示层 best-effort
        return {}


def _build(state: str, payload: dict, stale: bool = False) -> dict:
    rel = payload.get("rel") or []
    names = ("上证", "创业板")
    parts = []
    for index, value in enumerate(rel[:2]):
        try:
            parts.append(f"{names[index]} {float(value):+.2f}%")
        except (TypeError, ValueError):
            continue
    at = str(payload.get("at") or "")
    detail = " · ".join(parts)
    if at:
        detail = (detail + " · " if detail else "") + f"更新 {at[5:16].replace('T', ' ')}"
    if stale:
        detail += "（数据可能已过期）"
    return {
        "state": state,
        "label": {"strong": "强市", "weak": "弱市"}.get(state, "未知"),
        "detail": detail,
        "at": at,
        "rel": rel,
        "stale": stale,
    }


def regime_snapshot() -> dict:
    """当前市场强弱（读取调度器维护的 kv 状态，带 60 秒进程缓存）。"""
    now = dt.datetime.now()
    with _lock:
        cached = _cache["payload"]
        cached_at = _cache["at"]
        if cached is not None and cached_at is not None and now - cached_at < _TTL:
            return dict(cached)

    payload = _read_kv()
    state = str(payload.get("state") or "")
    at_raw = str(payload.get("at") or "")
    stale = False
    if at_raw:
        try:
            stale = (now - dt.datetime.fromisoformat(at_raw)) > _STALE_AFTER
        except ValueError:
            stale = True
    elif state:
        stale = True
    data = _build(state, payload, stale=stale)

    with _lock:
        _cache["at"] = now
        _cache["payload"] = data
    return dict(data)
