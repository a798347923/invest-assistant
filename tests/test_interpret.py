"""B2-05 触发解读接入测试（假 LLM/假行情/假通知）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import Bar
from invest_assistant.db.models import Alert, Base, LLMUsage
from invest_assistant.db.session import make_engine
from invest_assistant.engine.notify_flow import NotificationFlow
from invest_assistant.llm.analyzer import TriggerInterpreter
from invest_assistant.llm.client import LLMError, LLMResult
from invest_assistant.notify import DeliveryResult, Dispatcher


class FakeLLM:
    configured = True

    def __init__(self, result=None, error=None):
        self.calls = 0
        self._result = result
        self._error = error

    def complete_json(self, **kwargs):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result or LLMResult(
            content={
                "what_happened": "触发止盈规则",
                "position_status": "浮盈约 20%",
                "options": ["执行卖出", "继续持有观察"],
                "suggestion": "执行卖出",
                "confidence": 0.8,
            },
            raw="",
            model="deepseek-chat",
            tokens_in=100,
            tokens_out=50,
            cached_in=10,
            ms=120,
        )


class FakeData:
    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [
            Bar(code=code, date=dt.date(2026, 10, 9), open=1, high=1, low=1, close=1250.0, volume=1)
        ]


class RecordingNotifier:
    name = "rec"

    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, self.ok, 1, "" if self.ok else "fail")


def make_env(tmp_path, llm):
    engine = make_engine(f"sqlite:///{tmp_path / 'interpret.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    notifier = RecordingNotifier()
    interpreter = TriggerInterpreter(llm=llm, data=FakeData(), session_factory=sf)
    flow = NotificationFlow(
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        session_factory=sf,
        interpreter=interpreter,
    )
    return sf, flow, notifier


def seed_alert(sf, **overrides):
    context = {
        "code": "600519", "name": "贵州茅台", "reason": "达到止盈 +20%",
        "price": 1250.0, "cost": 1040.0, "qty": 100,
    }
    context.update(overrides)
    with sf() as session:
        alert = Alert(
            kind="trigger", status="pending", triggered_at=dt.datetime.now(), context_json=context
        )
        session.add(alert)
        session.commit()
        return alert.id


def test_interpretation_attached_and_message_includes_it(tmp_path):
    llm = FakeLLM()
    sf, flow, notifier = make_env(tmp_path, llm)
    alert_id = seed_alert(sf)
    report = flow.process_pending()
    assert report["sent"] == 1
    assert report["interpreted"] == 1

    with sf() as session:
        alert = session.get(Alert, alert_id)
        assert alert.interpretation_json["suggestion"] == "执行卖出"
        assert len(session.scalars(select(LLMUsage)).all()) == 1  # 用量已记录

    body = notifier.messages[0].body
    assert "【AI 解读】" in body
    assert "执行卖出" in body
    assert "不构成投资建议" in notifier.messages[0].render()


def test_llm_failure_falls_back_to_template(tmp_path):
    llm = FakeLLM(error=LLMError("llm down"))
    sf, flow, notifier = make_env(tmp_path, llm)
    seed_alert(sf)
    report = flow.process_pending()
    assert report["sent"] == 1
    assert report["interpreted"] == 0
    body = notifier.messages[0].body
    assert "【AI 解读】" not in body
    assert "达到止盈" in body  # 模板兜底仍完整


def test_interpretation_cached_on_retry(tmp_path):
    llm = FakeLLM()
    sf, flow, _ = make_env(tmp_path, llm)
    alert_id = seed_alert(sf)
    flow.process_pending()
    assert llm.calls == 1

    # 模拟投递失败 → 重试：解读不重复生成
    with sf() as session:
        alert = session.get(Alert, alert_id)
        alert.status = "failed"
        session.commit()
    flow.process_pending()
    assert llm.calls == 1


def test_watch_alert_skips_interpretation(tmp_path):
    llm = FakeLLM()
    sf, flow, notifier = make_env(tmp_path, llm)
    with sf() as session:
        session.add(
            Alert(
                kind="watch",
                status="pending",
                triggered_at=dt.datetime.now(),
                context_json={
                    "code": "600519", "name": "贵州茅台",
                    "reason": "自选异动：日内上涨 +6.00%（阈值 ±5%）",
                    "price": 106.0, "pct": 6.0,
                },
            )
        )
        session.commit()

    report = flow.process_pending()
    assert report["sent"] == 1
    assert report["interpreted"] == 0
    assert llm.calls == 0  # 自选提醒不调用 LLM
    message = notifier.messages[0]
    assert message.title.startswith("【自选提醒】")
    assert "【AI 解读】" not in message.body
    assert "自选池监控提醒" in message.body


def test_watch_judgement_uses_watch_prompt(tmp_path):
    from invest_assistant.llm.prompts import WATCH_V1_SYSTEM

    engine = make_engine(f"sqlite:///{tmp_path / 'wj.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)

    captured: dict = {}

    class CapturingLLM:
        configured = True

        def complete_json(self, **kwargs):
            captured.update(kwargs)
            return LLMResult(
                content={
                    "what_happened": "放量上攻", "position_status": "接近前高",
                    "options": ["列入买入计划"], "suggestion": "继续观察", "confidence": 0.6,
                },
                raw="", model="deepseek-chat",
                tokens_in=10, tokens_out=5, cached_in=0, ms=5,
            )

    interpreter = TriggerInterpreter(llm=CapturingLLM(), data=FakeData(), session_factory=sf)
    result = interpreter.interpret({"code": "600519", "reason": "自选异动"}, kind="watch")
    assert result is not None and result["suggestion"] == "继续观察"
    assert captured["system"] == WATCH_V1_SYSTEM
    assert captured["purpose"] == "watch"
    with sf() as session:
        assert len(session.scalars(select(LLMUsage)).all()) == 1
