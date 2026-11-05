"""APScheduler 作业注册（进程内调度 + 作业记录 + 通知流 + 心跳/看门狗/补偿）。

作业清单：盘中轮询（默认每分钟）、**提醒投递（每分钟，独立于轮询）**、收盘体检、模拟盘日报、
候选初筛、周报、数据清理、启动补报、错过任务补偿、每日备份、调度心跳（每分钟）。

可靠性设计：
- 心跳作业每分钟写入 kv；SchedulerWatchdog 检测停摆（>5 分钟）自动重建调度器；
- 启动补偿：若当天已过截止时间且无成功记录，自动补跑收盘体检 / 模拟盘日报 / 候选初筛；
- **轮询与投递分离**：轮询只做本地规则判定与提醒落库；LLM 解读与邮件/微信投递由独立作业消费
  pending 提醒，避免慢模型/慢 SMTP 挤占下一分钟的监控窗口。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from ..config import get_settings
from ..db.backup import backup_database
from ..db.models import JobRun
from ..db.session import get_session_factory
from .catchup import CatchupService
from .eod import run_eod
from .notify_flow import NotificationFlow, notify_job, poll_job
from .poller import Poller

log = logging.getLogger(__name__)

TIMEZONE = "Asia/Shanghai"
HEARTBEAT_KEY = "scheduler_heartbeat"


class JobRunner:
    """包装作业函数：执行前后写入 job_run（可观测性）。"""

    def __init__(self, session_factory=None):
        self._sf = session_factory or get_session_factory()

    def wrap(self, name: str, fn):
        def _run():
            started = dt.datetime.now()
            with self._sf() as session:
                row = JobRun(job=name, started_at=started, status="running")
                session.add(row)
                session.commit()
                job_id = row.id
            status, detail = "ok", ""
            output = None
            try:
                output = fn()
                detail = str(output)[:500]
            except Exception as exc:  # noqa: BLE001
                status = "error"
                detail = f"{type(exc).__name__}: {exc}"[:500]
                log.exception("作业 %s 失败", name)
            with self._sf() as session:
                row = session.get(JobRun, job_id)
                if row is not None:
                    row.finished_at = dt.datetime.now()
                    row.status = status
                    row.detail = detail
                    session.commit()
            return output if status == "ok" else None

        _run.__name__ = f"job_{name}"
        return _run


def _missing_deadline_jobs(session_factory, now: dt.datetime) -> list[str]:
    """返回今天已过截止时间但尚无成功记录的作业（用于启动补偿）。"""
    day_start = dt.datetime.combine(now.date(), dt.time.min)
    with session_factory() as session:
        rows = session.execute(
            select(JobRun.job).where(JobRun.started_at >= day_start, JobRun.status == "ok")
        ).all()
    ok_jobs = {row[0] for row in rows}
    missing: list[str] = []
    if now.time() >= dt.time(15, 45) and "eod_close" not in ok_jobs:
        missing.append("eod_close")
    if now.time() >= dt.time(15, 55) and "paper_review" not in ok_jobs:
        missing.append("paper_review")
    if now.time() >= dt.time(18, 45) and "screen_candidates" not in ok_jobs:
        missing.append("screen_candidates")
    return missing


class SchedulerWatchdog(threading.Thread):
    """心跳看门狗：调度器停摆超过阈值则重建；重建失败则退出进程交由守护进程重启。"""

    def __init__(self, session_factory=None, interval_seconds: int = 60, stale_minutes: int = 5):
        super().__init__(name="scheduler-watchdog", daemon=True)
        self._sf = session_factory or get_session_factory()
        self._interval = interval_seconds
        self._stale = dt.timedelta(minutes=stale_minutes)
        self._scheduler = None
        self._started_at = dt.datetime.now()

    def attach(self, scheduler) -> None:
        self._scheduler = scheduler

    def run(self) -> None:
        while True:
            time.sleep(self._interval)
            try:
                self.check_once()
            except Exception:  # noqa: BLE001
                log.exception("看门狗检查异常")

    def check_once(self, now: dt.datetime | None = None) -> bool:
        """检查一次；执行了重建则返回 True。"""
        now = now or dt.datetime.now()
        last = self._last_heartbeat() or self._started_at
        if now - last <= self._stale:
            return False
        log.error("调度器心跳停摆（最后心跳 %s），尝试重建调度器", last)
        self._restart(now)
        return True

    def _last_heartbeat(self) -> dt.datetime | None:
        from .state import get_value

        raw = (get_value(self._sf, HEARTBEAT_KEY) or {}).get("at")
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def _restart(self, now: dt.datetime) -> None:
        try:
            if self._scheduler is not None:
                self._scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._scheduler = build_scheduler()
            self._scheduler.start()
            from .state import set_value

            set_value(self._sf, HEARTBEAT_KEY, {"at": now.isoformat(timespec="seconds")})
            log.info("调度器已重建并恢复运行")
        except Exception:
            log.exception("调度器重建失败，进程退出交由服务守护重启")
            os._exit(1)


def build_scheduler(
    settings=None,
    poller=None,
    session_factory=None,
    run_catchup_on_start: bool = True,
) -> BackgroundScheduler:
    settings = settings or get_settings()
    sf = session_factory or get_session_factory()
    poll_cfg = settings.app_config().get("poll") or {}
    backup_cfg = settings.app_config().get("backup") or {}
    interval = int(poll_cfg.get("interval_minutes", 1) or 1)
    keep = int(backup_cfg.get("keep", 30) or 30)

    poller = poller or Poller(session_factory=sf, settings=settings)
    flow = NotificationFlow(session_factory=sf)
    runner = JobRunner(sf)

    def _heartbeat_job():
        from .state import set_value

        set_value(
            sf, HEARTBEAT_KEY, {"at": dt.datetime.now().isoformat(timespec="seconds")}
        )
        return "heartbeat"

    def _regime_job():
        from ..datasource import MarketDataService

        state = MarketDataService(session_factory=sf).market_regime_state()
        return f"市场状态：{state or '未知'}"

    def _screen_job():
        from ..screening.runner import screen_and_analyze

        return screen_and_analyze(session_factory=sf, flow=flow)

    def _weekly_job():
        from ..reports.weekly import weekly_report

        return weekly_report(session_factory=sf)

    def _paper_job():
        from .paper import run_daily_review

        return run_daily_review(session_factory=sf, settings=settings, flow=flow)

    def _calibration_job():
        from .calibration import run_calibration

        return run_calibration(session_factory=sf, dispatcher=flow.dispatcher)

    def _cleanup_job():
        from .housekeeping import housekeeping

        return housekeeping(session_factory=sf)

    def _compensation_job():
        now = dt.datetime.now()
        try:
            from ..datasource import MarketDataService

            trading = MarketDataService(session_factory=sf).is_trading_day(now.date())
        except Exception:  # noqa: BLE001
            trading = now.date().weekday() < 5
        if not trading:
            return "非交易日"
        missing = _missing_deadline_jobs(sf, now)
        if not missing:
            return "无缺失任务"
        done: dict = {}
        if "eod_close" in missing:
            done["eod_close"] = run_eod(session_factory=sf, dispatcher=flow.dispatcher, force=True)
        if "paper_review" in missing:
            from .paper import run_daily_review

            done["paper_review"] = run_daily_review(
                session_factory=sf, settings=settings, flow=flow
            )
        if "screen_candidates" in missing:
            from ..screening.runner import screen_and_analyze

            done["screen_candidates"] = screen_and_analyze(session_factory=sf, flow=flow)
        return str({"missing": missing, "done": list(done)})

    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        runner.wrap("market_regime", _regime_job),
        trigger=CronTrigger(
            minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=TIMEZONE
        ),
        id="market_regime",
        name="强弱市刷新",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        runner.wrap("poll_quotes", poll_job(poller, flow)),
        trigger=CronTrigger(
            minute=f"*/{interval}", hour="9-15", day_of_week="mon-fri", timezone=TIMEZONE
        ),
        id="poll_quotes",
        name="盘中轮询",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=180,
    )
    # 投递与轮询解耦：每分钟消费 pending 提醒（LLM 解读 + 双通道发送）。
    # 全天运行而非只在盘中，因为补报/收盘体检也会产生 pending。
    scheduler.add_job(
        runner.wrap("notify_pending", notify_job(flow)),
        trigger=CronTrigger(minute="*", timezone=TIMEZONE),
        id="notify_pending",
        name="提醒投递",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        runner.wrap("eod_close", lambda: run_eod(session_factory=sf, dispatcher=flow.dispatcher)),
        trigger=CronTrigger(hour=15, minute=30, day_of_week="mon-fri", timezone=TIMEZONE),
        id="eod_close",
        name="收盘体检",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        runner.wrap("paper_review", _paper_job),
        trigger=CronTrigger(hour=15, minute=40, day_of_week="mon-fri", timezone=TIMEZONE),
        id="paper_review",
        name="模拟盘日报",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        runner.wrap("screen_candidates", _screen_job),
        trigger=CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=TIMEZONE),
        id="screen_candidates",
        name="候选初筛",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        runner.wrap("weekly_report", _weekly_job),
        trigger=CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=TIMEZONE),
        id="weekly_report",
        name="周报",
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        runner.wrap("calibration", _calibration_job),
        trigger=CronTrigger(day_of_week="sat", hour=10, minute=15, timezone=TIMEZONE),
        id="calibration",
        name="预测校准",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        runner.wrap("housekeeping", _cleanup_job),
        trigger=CronTrigger(day_of_week="sun", hour=23, minute=0, timezone=TIMEZONE),
        id="housekeeping",
        name="数据清理",
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        _heartbeat_job,
        trigger=CronTrigger(minute="*", timezone=TIMEZONE),
        id="heartbeat",
        name="调度心跳",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
    if run_catchup_on_start:
        scheduler.add_job(
            runner.wrap("catchup", CatchupService(session_factory=sf, flow=flow).run),
            trigger="date",
            run_date=dt.datetime.now() + dt.timedelta(seconds=10),
            id="catchup_on_start",
            name="启动补报",
        )
        scheduler.add_job(
            runner.wrap("startup_compensation", _compensation_job),
            trigger="date",
            run_date=dt.datetime.now() + dt.timedelta(seconds=25),
            id="startup_compensation",
            name="错过任务补偿",
        )
    scheduler.add_job(
        runner.wrap("backup", lambda: str(backup_database(keep=keep))),
        trigger=CronTrigger(hour=23, minute=30, timezone=TIMEZONE),
        id="backup",
        name="每日备份",
        misfire_grace_time=3600,
    )
    return scheduler
