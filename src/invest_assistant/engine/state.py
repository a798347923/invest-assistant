"""引擎状态（kv_setting）读写辅助。"""

from __future__ import annotations

import datetime as dt

from ..db.models import KvSetting

LAST_POLL_KEY = "last_poll_at"


def get_value(session_factory, key: str) -> dict:
    with session_factory() as session:
        row = session.get(KvSetting, key)
        return dict(row.value_json or {}) if row else {}


def set_value(session_factory, key: str, value: dict) -> None:
    with session_factory() as session:
        row = session.get(KvSetting, key)
        if row is None:
            row = KvSetting(key=key, value_json=value)
            session.add(row)
        else:
            row.value_json = value
        session.commit()


def get_last_poll_at(session_factory) -> dt.datetime | None:
    raw = (get_value(session_factory, LAST_POLL_KEY) or {}).get("at")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def set_last_poll_at(session_factory, ts: dt.datetime) -> None:
    set_value(session_factory, LAST_POLL_KEY, {"at": ts.isoformat(timespec="seconds")})
