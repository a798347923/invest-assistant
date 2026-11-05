"""B3-06 周报测试（假行情/假 LLM/假通知）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import Quote
from invest_assistant.db.models import Alert, Base, LLMUsage
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.llm.client import LLMResult
from invest_assistant.notify import DeliveryResult, Dispatcher
from invest_assistant.reports.weekly import weekly_report

NOW = dt.datetime(2026, 10, 11, 10, 0)


class FakeData:
    def __init__(self, price=110.0, fail=False):
        self.price = price
        self.fail = fail

    def get_quotes(self, codes):
        if self.fail:
            raise RuntimeError("quote down")
        return {
            code: Quote(
                code=code, name="测试", price=self.price, prev_close=self.price,
                open=self.price, high=self.price, low=self.price, pct_change=0.0,
                volume=0, amount=0, ts=NOW, source="fake",
            )
            for code in codes
        }


class FakeLLM:
    configured = True

    def complete_json(self, **kwargs):
        return LLMResult(
            content={"summary": "本周纪律执行良好，注意落袋为安。", "actions": ["继续按规则持有"]},
            raw="",
            model="deepseek-chat",
            tokens_in=100,
            tokens_out=40,
            cached_in=0,
            ms=100,
        )


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


def _env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'weekly.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_weekly_report_full(tmp_path):
    sf = _env(tmp_path)
    positions = PositionService(session_factory=sf)
    created = positions.add_position(
        code="600519", qty=100, price=100.0, strategy="value",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=90),
    )
    positions.sell(created["position_id"], qty=50, price=105.0, sold_at=NOW - dt.timedelta(days=2))
    with sf() as session:
        session.add(
            Alert(
                kind="trigger", status="sent", triggered_at=NOW - dt.timedelta(days=3),
                trigger_price=120.0,
                context_json={"code": "600519", "rule_type": "fixed_take_profit", "reason": "达到止盈 +20%"},
            )
        )
        session.add(
            Alert(
                kind="trigger", status="handled", triggered_at=NOW - dt.timedelta(days=1),
                context_json={"code": "600519", "rule_type": "fixed_stop_loss", "reason": "止损"},
            )
        )
        session.commit()

    notifier = RecordingNotifier()
    report = weekly_report(
        data=FakeData(price=110.0),
        session_factory=sf,
        llm=FakeLLM(),
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        now=NOW,
    )
    assert report["sent"] is True
    assert report["trades"] == 2  # 建仓与卖出各一笔，都在 7 天内
    assert report["missed"] == 1  # 止盈提醒价 120 → 现价 110
    assert report["ai"] is True
    body = notifier.messages[0].body
    assert "错过的卖点" in body
    assert "600519" in body
    assert "AI 总结" in body
    assert "继续按规则持有" in body
    with sf() as session:
        assert len(session.scalars(select(LLMUsage)).all()) == 1


def test_weekly_report_survives_quote_failure(tmp_path):
    sf = _env(tmp_path)
    with sf() as session:
        session.add(
            Alert(
                kind="trigger", status="sent", triggered_at=NOW - dt.timedelta(days=2),
                trigger_price=120.0,
                context_json={"code": "600519", "rule_type": "fixed_take_profit"},
            )
        )
        session.commit()

    notifier = RecordingNotifier()
    report = weekly_report(
        data=FakeData(fail=True),
        session_factory=sf,
        llm=FakeLLM(),
        dispatcher=Dispatcher([notifier], retries=0, backoff=0),
        now=NOW,
    )
    assert report["sent"] is True
    assert report["missed"] == 0
    assert "错过的卖点" in notifier.messages[0].body  # 板块保留，说明无数据结论
