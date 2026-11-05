"""B1-08 触发闭环与补报测试（假数据源/假通知）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import Bar, DataUnavailable
from invest_assistant.db.models import Alert, Base, ExitRule
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.engine.catchup import CatchupService
from invest_assistant.engine.notify_flow import NotificationFlow
from invest_assistant.engine.state import get_last_poll_at, set_last_poll_at
from invest_assistant.notify import DeliveryResult, Dispatcher

NOW = dt.datetime(2026, 10, 12, 9, 0, 0)


class RecordingNotifier:
    name = "rec"

    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(channel=self.name, ok=self.ok, ms=1, error="" if self.ok else "fail")


class FakeBars:
    def __init__(self, bars=None, fail=False):
        self._bars = bars or {}
        self.fail = fail

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        if self.fail:
            raise DataUnavailable("no bars")
        return [b for b in self._bars.get(code, []) if start <= b.date <= end]


def make_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'catchup.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    positions = PositionService(session_factory=sf)
    return sf, positions


def make_flow(sf, notifier):
    from invest_assistant.llm.analyzer import TriggerInterpreter

    class _NoLLM:
        configured = False

    interpreter = TriggerInterpreter(llm=_NoLLM(), data=None, session_factory=sf)
    return NotificationFlow(
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        interpreter=interpreter,
    )


# ---------------------------------------------------------------- 投递闭环

def test_process_pending_marks_sent(tmp_path):
    sf, _ = make_env(tmp_path)
    notifier = RecordingNotifier()
    flow = make_flow(sf, notifier)
    with sf() as s:
        s.add(Alert(kind="trigger", status="pending", triggered_at=dt.datetime.now(),
                    context_json={"code": "600519", "name": "贵州茅台", "reason": "止盈"}))
        s.commit()
    report = flow.process_pending()
    assert report["sent"] == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert alert.status == "sent"
        assert alert.delivery_json["results"][0]["ok"] is True
    assert len(notifier.messages) == 1
    assert "600519" in notifier.messages[0].title


def test_process_pending_marks_failed(tmp_path):
    sf, _ = make_env(tmp_path)
    notifier = RecordingNotifier(ok=False)
    flow = make_flow(sf, notifier)
    with sf() as s:
        s.add(Alert(kind="trigger", status="pending", triggered_at=dt.datetime.now(),
                    context_json={"code": "600519", "reason": "止盈"}))
        s.commit()
    report = flow.process_pending()
    assert report["failed"] == 1
    with sf() as s:
        assert s.scalars(select(Alert)).one().status == "failed"


def test_process_pending_expired_marked(tmp_path):
    sf, _ = make_env(tmp_path)
    notifier = RecordingNotifier()
    flow = make_flow(sf, notifier)
    with sf() as s:
        s.add(Alert(kind="trigger", status="pending",
                    triggered_at=dt.datetime.now() - dt.timedelta(hours=48),
                    context_json={"code": "600519", "reason": "止损"}))
        s.commit()
    report = flow.process_pending()
    assert report["expired"] == 1
    assert notifier.messages[0].title.startswith("【已过期，请复核】")


def test_process_pending_skips_when_unconfigured(tmp_path):
    sf, _ = make_env(tmp_path)
    flow = NotificationFlow(dispatcher=Dispatcher([]), session_factory=sf)
    with sf() as s:
        s.add(Alert(kind="trigger", status="pending", triggered_at=dt.datetime.now()))
        s.commit()
    report = flow.process_pending()
    assert report["skipped"] == -1
    with sf() as s:
        assert s.scalars(select(Alert)).one().status == "pending"  # 保持待发


# ---------------------------------------------------------------- 离线回放

def test_catchup_replays_missed_stop_loss(tmp_path):
    sf, positions = make_env(tmp_path)
    opened = NOW - dt.timedelta(days=6)
    positions.add_position(
        code="600519", qty=100, price=100.0, strategy="value", opened_at=opened,
        rules=ExitRules(take_profit_pct=20, stop_loss_pct=10, max_holding_days=365),
    )
    bars = [
        Bar(code="600519", date=dt.date(2026, 10, 9), open=95, high=96, low=88, close=89, volume=1),
        Bar(code="600519", date=dt.date(2026, 10, 10), open=89, high=92, low=88, close=91, volume=1),
    ]
    data = FakeBars({"600519": bars})
    notifier = RecordingNotifier()
    flow = make_flow(sf, notifier)
    service = CatchupService(data=data, positions=positions, session_factory=sf, flow=flow)

    set_last_poll_at(sf, NOW - dt.timedelta(days=4))  # 停机 4 天
    report = service.run(now=NOW)
    assert report["missed_alerts"] == 1  # 10-09 low=88 跌破 90 防线
    assert report["sent"] == 1
    with sf() as s:
        alert = s.scalars(select(Alert).where(Alert.is_catchup.is_(True))).one()
        assert "止损" in alert.context_json["reason"]
        assert "2026-10-09" in alert.context_json["reason"]
        sl_rule = s.scalars(select(ExitRule).where(ExitRule.type == "fixed_stop_loss")).one()
        assert sl_rule.status == "triggered"
    assert "止损" in notifier.messages[0].body

    # 第二次运行：last_poll 已更新，无重复补报
    report2 = service.run(now=NOW + dt.timedelta(minutes=5))
    assert report2["missed_alerts"] == 0


def test_catchup_no_gap_no_replay(tmp_path):
    sf, positions = make_env(tmp_path)
    positions.add_position(
        code="600519", qty=100, price=100.0, strategy="value", opened_at=NOW - dt.timedelta(days=30),
        rules=ExitRules(take_profit_pct=20, stop_loss_pct=10, max_holding_days=365),
    )
    data = FakeBars({"600519": []})
    flow = make_flow(sf, RecordingNotifier())
    service = CatchupService(data=data, positions=positions, session_factory=sf, flow=flow)
    set_last_poll_at(sf, NOW)
    report = service.run(now=NOW)
    assert report["missed_alerts"] == 0


def test_catchup_skips_without_baseline(tmp_path):
    sf, positions = make_env(tmp_path)
    positions.add_position(
        code="600519", qty=100, price=100.0, strategy="value",
        rules=ExitRules(take_profit_pct=20, stop_loss_pct=10, max_holding_days=365),
    )
    flow = make_flow(sf, RecordingNotifier())
    service = CatchupService(data=FakeBars(), positions=positions, session_factory=sf, flow=flow)
    report = service.run(now=NOW)
    assert report["missed_alerts"] == 0  # 无基准（首次运行）不做回放
    assert get_last_poll_at(sf) is None  # 仍由轮询成功时写入
