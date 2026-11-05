"""B3-02 候选批量分析测试（注入快照/假分析器/假通知）。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, Candidate
from invest_assistant.db.session import make_engine
from invest_assistant.notify import DeliveryResult, Dispatcher
from invest_assistant.screening.runner import analyze_candidates, screen_and_analyze
from invest_assistant.screening.screener import run_screen

ROWS = [
    {"code": "601398", "name": "工商银行", "price": 5.9, "pct": 0.5, "amount": 2e9,
     "turnover": 0.2, "pe": 5.1, "pb": 0.6, "market_cap": 21000.0, "source": "test"},
    {"code": "000001", "name": "平安银行", "price": 11.5, "pct": -1.0, "amount": 1.5e9,
     "turnover": 0.9, "pe": 6.0, "pb": 0.8, "market_cap": 2230.0, "source": "test"},
    {"code": "300750", "name": "宁德时代", "price": 250.0, "pct": 4.2, "amount": 8e9,
     "turnover": 3.5, "pe": 25.0, "pb": 4.5, "market_cap": 11000.0, "source": "test"},
]


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


def _make_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'runner.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakeAnalyzer:
    """失败名单中的代码抛错；其余返回固定结论。"""

    def __init__(self, fail_codes=()):
        self.fail_codes = set(fail_codes)
        self.calls: list[str] = []

    def analyze(self, code, name="", type=""):
        self.calls.append(code)
        if code in self.fail_codes:
            raise RuntimeError("fake llm down")
        return {"id": 1, "code": code, "content": {"summary": "ok", "action_bias": "观察"}}


def test_analyze_candidates_updates_status(tmp_path):
    sf = _make_env(tmp_path)
    run_screen(session_factory=sf, snapshot=ROWS, top_n=10)
    fake = FakeAnalyzer(fail_codes={"000001"})
    report = analyze_candidates(
        session_factory=sf, analyzer_factory=lambda: fake, delay=0
    )
    assert report["analyzed"] >= 2
    assert report["failed"] == 1
    assert len(fake.calls) == report["analyzed"] + report["failed"]
    with sf() as session:
        statuses = {}
        for candidate in session.scalars(select(Candidate)):
            statuses.setdefault(candidate.status, 0)
            statuses[candidate.status] += 1
        assert statuses.get("analysis_failed", 0) == 1
        assert statuses.get("analyzed", 0) >= 2

    # 幂等：再跑不会重复分析（status 已不是 new）
    report2 = analyze_candidates(session_factory=sf, analyzer_factory=lambda: fake, delay=0)
    assert report2["analyzed"] == 0


def test_screen_and_analyze_end_to_end(tmp_path):
    sf = _make_env(tmp_path)
    notifier = RecordingNotifier()
    flow = type("Flow", (), {"dispatcher": Dispatcher([notifier], retries=0, backoff=0)})()
    fake = FakeAnalyzer()
    summary = screen_and_analyze(
        session_factory=sf,
        flow=flow,
        snapshot=ROWS,
        analyzer_factory=lambda: fake,
        top_n=10,
        delay=0,
    )
    assert summary["screen"]["created"] >= 3
    assert summary["analysis"]["analyzed"] >= 3
    assert summary["notified"] is True
    assert len(notifier.messages) == 1
    assert "候选池" in notifier.messages[0].title

    # 再跑一遍验证幂等（同日不重复创建、已分析不重复分析）
    summary2 = screen_and_analyze(
        session_factory=sf,
        flow=flow,
        snapshot=ROWS,
        analyzer_factory=lambda: fake,
        top_n=10,
        delay=0,
    )
    assert summary2["screen"]["created"] == 0  # 同日幂等
    assert summary2["analysis"]["analyzed"] == 0  # 已分析不重复
