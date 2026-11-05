"""数据清理（周日 23:00）：按保留策略删除过期数据。

保留策略（可在 config.yaml 的 retention 覆盖）：
- 新闻 90 天；提醒 365 天（pending/failed 保护，不清理）；作业记录 30 天；
- 回测记录保留最近 20 次（按 id 倒序）。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import delete, select

from ..db.models import Alert, BacktestRun, JobRun, NewsItem
from ..db.session import get_session_factory

log = logging.getLogger(__name__)

DEFAULT_RETENTION = {
    "news_days": 90,
    "alerts_days": 365,
    "job_run_days": 30,
    "backtest_keep": 20,
}
PROTECTED_ALERT_STATUS = ("pending", "failed")


def housekeeping(session_factory=None, now: dt.datetime | None = None, retention: dict | None = None) -> dict:
    sf = session_factory or get_session_factory()
    now = now or dt.datetime.now()
    policy = dict(DEFAULT_RETENTION)
    policy.update(retention or {})
    try:
        from ..config import get_settings

        policy.update(
            {
                k: v
                for k, v in (get_settings().app_config().get("retention") or {}).items()
                if v is not None
            }
        )
    except Exception:  # noqa: BLE001 — 配置不可用则用默认值
        pass

    removed: dict = {}
    with sf() as session:
        removed["news_item"] = (
            session.execute(
                delete(NewsItem).where(
                    NewsItem.ts < now - dt.timedelta(days=int(policy["news_days"]))
                )
            ).rowcount
            or 0
        )
        removed["alert"] = (
            session.execute(
                delete(Alert).where(
                    Alert.triggered_at < now - dt.timedelta(days=int(policy["alerts_days"])),
                    Alert.status.notin_(PROTECTED_ALERT_STATUS),
                )
            ).rowcount
            or 0
        )
        removed["job_run"] = (
            session.execute(
                delete(JobRun).where(
                    JobRun.started_at < now - dt.timedelta(days=int(policy["job_run_days"]))
                )
            ).rowcount
            or 0
        )
        keep = int(policy["backtest_keep"])
        old_ids = session.scalars(
            select(BacktestRun.id).order_by(BacktestRun.id.desc()).offset(keep)
        ).all()
        removed["backtest_run"] = (
            session.execute(delete(BacktestRun).where(BacktestRun.id.in_(list(old_ids)))).rowcount
            or 0
            if old_ids
            else 0
        )
        session.commit()
    log.info("数据清理完成: %s", removed)
    return removed
