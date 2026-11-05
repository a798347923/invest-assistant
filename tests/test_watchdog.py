"""看门狗与错过任务补偿测试。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, JobRun
from invest_assistant.db.session import make_engine
from invest_assistant.engine import scheduler as sched_mod
from invest_assistant.engine.scheduler import SchedulerWatchdog, _missing_deadline_jobs


class FakeScheduler:
    def __init__(self):
        self.shutdown_called = False
        self.started = False

    def shutdown(self, wait=False):
        self.shutdown_called = True

    def start(self):
        self.started = True


def _env(path):
    engine = make_engine(f"sqlite:///{path / 'wd.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_missing_deadline_jobs(tmp_path):
    sf = _env(tmp_path)
    morning = dt.datetime(2026, 10, 13, 10, 0)
    evening = dt.datetime(2026, 10, 13, 19, 0)

    assert _missing_deadline_jobs(sf, morning) == []
    assert _missing_deadline_jobs(sf, evening) == ["eod_close", "paper_review", "screen_candidates"]

    with sf() as session:
        session.add(
            JobRun(job="eod_close", started_at=dt.datetime(2026, 10, 13, 15, 30), status="ok")
        )
        session.commit()
    assert _missing_deadline_jobs(sf, evening) == ["paper_review", "screen_candidates"]

    with sf() as session:
        session.add(
            JobRun(
                job="screen_candidates",
                started_at=dt.datetime(2026, 10, 13, 18, 30),
                status="ok",
            )
        )
        session.add(
            JobRun(
                job="paper_review",
                started_at=dt.datetime(2026, 10, 13, 15, 40),
                status="ok",
            )
        )
        session.commit()
    assert _missing_deadline_jobs(sf, evening) == []


def test_failed_job_still_needs_compensation(tmp_path):
    err_dir = tmp_path / "err"
    err_dir.mkdir(exist_ok=True)
    sf = _env(err_dir)
    with sf() as session:
        session.add(
            JobRun(job="eod_close", started_at=dt.datetime(2026, 10, 13, 15, 30), status="error")
        )
        session.commit()
    evening = dt.datetime(2026, 10, 13, 19, 0)
    assert "eod_close" in _missing_deadline_jobs(sf, evening)


def test_watchdog_restarts_when_heartbeat_stale(tmp_path, monkeypatch):
    sf = _env(tmp_path)
    watchdog = SchedulerWatchdog(session_factory=sf, interval_seconds=999)
    old = FakeScheduler()
    watchdog.attach(old)
    created: list[FakeScheduler] = []

    def fake_build(**kwargs):
        scheduler = FakeScheduler()
        created.append(scheduler)
        return scheduler

    monkeypatch.setattr(sched_mod, "build_scheduler", fake_build)
    watchdog._started_at = dt.datetime.now() - dt.timedelta(minutes=10)
    assert watchdog.check_once() is True
    assert old.shutdown_called is True
    assert created and created[0].started is True


def test_watchdog_no_restart_when_fresh(tmp_path):
    sf = _env(tmp_path)
    watchdog = SchedulerWatchdog(session_factory=sf, interval_seconds=999)
    assert watchdog.check_once() is False  # 刚启动，未超阈值


def test_heartbeat_and_compensation_jobs_registered(tmp_path):
    sf = _env(tmp_path)
    scheduler = sched_mod.build_scheduler(session_factory=sf, run_catchup_on_start=True)
    ids = {job.id for job in scheduler.get_jobs()}
    assert {"heartbeat", "startup_compensation", "catchup_on_start"} <= ids
