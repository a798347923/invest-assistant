"""预测校准闭环测试（假数据，无网络）。"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, CalibrationReport, Prediction
from invest_assistant.db.session import make_engine
from invest_assistant.engine.calibration import (
    _build_findings,
    _score,
    backfill_predictions,
    calibration_notes,
    calibration_stats,
    evaluate_pending,
    run_calibration,
)
from invest_assistant.notify import DeliveryResult, Dispatcher

BASE_DAY = dt.date(2026, 9, 1)
NOW = dt.datetime(2026, 10, 15, 10, 0, 0)


def make_bars(rows, base_day: dt.date = BASE_DAY) -> list:
    """rows: [(close, low)]、单元素元组或纯数字（high 取 close*1.001）。"""
    bars = []
    for i, row in enumerate(rows):
        if isinstance(row, tuple):
            close = row[0]
            low = row[1] if len(row) > 1 else close * 0.999
        else:
            close, low = row, row * 0.999
        bars.append(
            SimpleNamespace(
                code="600519",
                date=base_day + dt.timedelta(days=i),
                open=close, close=close, high=close * 1.001, low=low,
                volume=1.0, amount=0.0, adjust="qfq",
            )
        )
    return bars


def make_prediction(**kwargs) -> Prediction:
    defaults = dict(
        date=BASE_DAY, code="600519", action="等待回踩", price=10.0,
        zone_low=None, zone_high=None, breakout=None, stop=None,
        regime="指数≥20日线", trend="多头(>MA20)",
    )
    defaults.update(kwargs)
    return Prediction(**defaults)


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'calib.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakeData:
    def __init__(self, bars_by_code=None, index_bars=None):
        self.bars_by_code = bars_by_code or {}
        self.tencent = SimpleNamespace(
            get_daily_bars=lambda code, start, end, adjust="raw": [
                b for b in (index_bars or []) if start <= b.date <= end
            ]
        )

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [b for b in self.bars_by_code.get(code, []) if start <= b.date <= end]


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


# ---------------------------------------------------------------- 单条评分


def test_score_pullback_triggered_and_strong():
    bars = make_bars([
        (10.0, 9.9), (9.9, 9.8), (9.85, 9.7), (9.95, 9.8), (10.1, 10.0),
        (10.3, 10.2), (10.5, 10.4), (10.6, 10.5), (10.7, 10.6), (10.8, 10.7),
        (10.9, 10.8), (11.0, 10.9), (11.1, 11.0), (11.2, 11.1), (11.3, 11.2),
        (11.4, 11.3),
    ])
    outcome = _score(make_prediction(zone_high=9.8, stop=9.4), bars)
    assert outcome["triggered"] is True
    assert outcome["trigger_day"] == 1  # 第 2 根（low 9.8 ≤ 9.8）
    assert outcome["post_entry_ret5"] == 7.14  # (10.5/9.8 - 1)
    assert outcome["verdict"] == "命中后走强"


def test_score_pullback_never_triggered_but_rallied():
    bars = make_bars([(10.0, 9.9), (10.1, 10.0), (10.3, 10.2), (10.5, 10.4),
                      (10.7, 10.6), (10.9, 10.8), (11.0, 10.9), (11.1, 11.0),
                      (11.2, 11.1), (11.3, 11.2), (11.4, 11.3), (11.5, 11.4),
                      (11.6, 11.5), (11.7, 11.6), (11.8, 11.7), (11.9, 11.8),
                      (12.0, 11.9), (12.1, 12.0), (12.2, 12.1), (12.3, 12.2),
                      (12.4, 12.3), (12.5, 12.4)])
    outcome = _score(make_prediction(zone_high=9.0), bars)
    assert outcome["triggered"] is False
    assert outcome["verdict"] == "未触发（同期上涨，偏保守）"


def test_score_breakout_stop_hit_and_weak():
    falling = make_bars([(10.0,), (10.6,), (10.5, 10.4), (10.2, 10.1), (10.0, 9.5),
                         (9.8, 9.6), (9.6, 9.5), (9.5, 9.4), (9.4, 9.3), (9.3, 9.2),
                         (9.2, 9.1), (9.1, 9.0), (9.0, 8.9), (8.9, 8.8), (8.8, 8.7)])
    pred = make_prediction(action="等待突破", breakout=10.5, stop=10.0, price=10.0)
    outcome = _score(pred, falling)
    assert outcome["triggered"] is True
    assert outcome["verdict"] == "触发后跌破失效价"

    weaker = make_bars([(10.0,), (10.6,), (10.4,), (10.3,), (10.1,), (9.9,),
                        (9.8,), (9.7,), (9.6,), (9.5,), (9.4,), (9.3,), (9.2,)])
    outcome2 = _score(make_prediction(action="等待突破", breakout=10.5, price=10.0), weaker)
    assert outcome2["triggered"] is True
    assert outcome2["verdict"] == "命中后走弱"


def test_score_avoid_correct():
    falling = make_bars([(10.0,), (9.9,), (9.8,), (9.7,), (9.6,), (9.5,),
                         (9.4,), (9.3,), (9.2,), (9.1,), (9.0,), (8.9,), (8.8,)])
    outcome = _score(make_prediction(action="回避"), falling)
    assert outcome["verdict"] == "回避正确"
    assert _score(make_prediction(action="回避"), make_bars([(10.0,)])) is None  # 数据不足


# ---------------------------------------------------------------- 评估与统计


def test_evaluate_pending_pipeline(sf):
    with sf() as session:
        session.add(make_prediction(date=dt.date(2026, 9, 1), zone_high=9.8, stop=9.4))
        session.commit()
    bars = make_bars([
        (10.0, 9.9), (9.9, 9.8), (9.85, 9.7), (9.95, 9.8), (10.1, 10.0),
        (10.3, 10.2), (10.5, 10.4), (10.6, 10.5), (10.7, 10.6), (10.8, 10.7),
        (10.9, 10.8), (11.0, 10.9), (11.1, 11.0), (11.2, 11.1), (11.3, 11.2),
        (11.4, 11.3), (11.5, 11.4), (11.6, 11.5),
    ])
    report = evaluate_pending(session_factory=sf, data=FakeData({"600519": bars}), now=NOW)
    assert report["evaluated"] == 1
    with sf() as session:
        row = session.scalars(select(Prediction)).one()
        assert row.status == "evaluated"
        assert row.outcome_json["verdict"] == "命中后走强"
    # 已评估不再重复
    assert evaluate_pending(session_factory=sf, data=FakeData({"600519": bars}), now=NOW)["evaluated"] == 0


def test_stats_and_findings_weak_breakout_negative(sf):
    with sf() as session:
        for i in range(6):
            session.add(
                make_prediction(
                    date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
                    action="等待突破", regime="指数<20日线", status="evaluated",
                    outcome_json={"ret10": -2.5, "ret5": -1.0, "ret20": -4.0,
                                  "triggered": True, "post_entry_ret5": -3.5,
                                  "verdict": "命中后走弱"},
                )
            )
        session.commit()
    stats = calibration_stats(session_factory=sf)
    bucket = stats["buckets"]["突破｜指数<20日线"]
    assert bucket["n"] == 6
    assert bucket["ret10"] == -2.5
    findings = _build_findings(stats)
    assert any("追突破" in line for line in findings)
    assert any("突破｜指数<20日线" in line for line in findings)


def test_run_calibration_stores_report_and_notifies(sf):
    with sf() as session:
        for i in range(5):
            session.add(
                make_prediction(
                    date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
                    action="等待回踩", regime="指数≥20日线", status="evaluated",
                    outcome_json={"ret10": 2.0, "triggered": True, "post_entry_ret5": 4.0,
                                  "verdict": "命中后走强"},
                )
            )
        session.commit()
    notifier = RecordingNotifier()
    dispatcher = Dispatcher([notifier], retries=0, backoff=0)
    report = run_calibration(
        session_factory=sf, data=FakeData(), dispatcher=dispatcher, now=NOW
    )
    assert report["samples"] == 5
    assert report["findings"] >= 1
    assert notifier.messages and "预测校准" in notifier.messages[0].title
    with sf() as session:
        saved = session.scalars(select(CalibrationReport)).one()
        assert saved.content_json["findings"]
    notes = calibration_notes(sf)
    assert notes and "回踩" in notes[0]


def test_backfill_predictions_from_cards(sf):
    from invest_assistant.db.models import AnalysisCard

    card_day = NOW - dt.timedelta(days=40)
    with sf() as session:
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v5",
                created_at=card_day,
                content_json={
                    "summary": "s", "action_bias": "等待回踩",
                    "plan": {"entry_low": 9.5, "entry_high": 9.8, "stop_price": 9.2,
                             "entry_zone": "9.5~9.8", "stop_level": "跌破 9.2"},
                },
                sources_json={},
            )
        )
        session.commit()
    bars = make_bars([(10.0 - i * 0.01) for i in range(80)], base_day=card_day.date() - dt.timedelta(days=79))
    index_bars = make_bars([(100.0,) for _ in range(60)], base_day=card_day.date() - dt.timedelta(days=59))
    created = backfill_predictions(
        session_factory=sf,
        data=FakeData({"600519": bars}, index_bars=index_bars),
        days=90,
    )
    assert created == 1
    with sf() as session:
        row = session.scalars(select(Prediction)).one()
        assert row.action == "等待回踩"
        assert row.zone_high == 9.8
        assert row.regime == "指数≥20日线"
        assert row.price is not None


def test_analyzer_records_prediction_and_injects_notes(sf):
    from invest_assistant.db.models import AnalysisCard
    from invest_assistant.llm.analyzer import CardAnalyzer
    from invest_assistant.llm.client import LLMResult

    with sf() as session:
        session.add(
            CalibrationReport(
                date=dt.date(2026, 10, 10),
                summary="校准结论",
                content_json={"findings": ["弱势环境追突破为负，弱市放弃突破买点"]},
            )
        )
        session.commit()

    bars = make_bars(
        [(10.0 + i * 0.05) for i in range(80)],
        base_day=dt.date.today() - dt.timedelta(days=79),
    )

    class FakeData:
        def get_daily_bars(self, code, start, end, adjust="qfq"):
            return [b for b in bars if start <= b.date <= end]

        def get_fund_nav(self, code, days=60):
            return []

        def get_market_env(self):
            return {"指数": {"上证指数": {"相对20日线%": 1.2}}}

    class FakeLLM:
        configured = True

        def __init__(self):
            self.last_user = ""

        def complete_json(self, **kwargs):
            self.last_user = str(kwargs.get("user", ""))
            return LLMResult(
                content={
                    "summary": "x", "action_bias": "等待回踩", "confidence": 0.5,
                    "plan": {"entry_type": "回踩", "entry_low": 11.0, "entry_high": 11.6,
                             "stop_price": 10.5, "entry_zone": "11.0~11.6",
                             "stop_level": "跌破 10.5"},
                },
                raw="", model="fake", tokens_in=1, tokens_out=1, cached_in=0, ms=1,
            )

    llm = FakeLLM()
    analyzer = CardAnalyzer(
        llm=llm, data=FakeData(), news_fetcher=lambda code: [], session_factory=sf
    )
    analyzer.analyze("600519")
    assert "预测校准结论" in llm.last_user
    with sf() as session:
        prediction = session.scalars(select(Prediction)).one()
        assert prediction.action == "等待回踩"
        assert prediction.zone_high == 11.6
        assert prediction.stop == 10.5
        assert prediction.regime == "指数≥20日线"
        assert prediction.trend.startswith("多头") or prediction.trend.startswith("空头")
        card = session.scalars(select(AnalysisCard)).one()
        assert prediction.card_id == card.id


def test_journal_page_shows_calibration(fresh_env):
    from fastapi.testclient import TestClient

    from invest_assistant.db.session import ensure_schema, get_session_factory
    from invest_assistant.web.app import create_app

    ensure_schema()
    sf = get_session_factory()
    with sf() as session:
        session.add(
            CalibrationReport(
                date=dt.date(2026, 10, 12), summary="校准摘要",
                content_json={"findings": ["弱市追突破为负"]},
            )
        )
        session.add(
            make_prediction(
                date=dt.date(2026, 9, 20), status="evaluated",
                outcome_json={"verdict": "命中后走强", "ret10": 3.2},
            )
        )
        session.commit()
    page = TestClient(create_app()).get("/journal")
    assert page.status_code == 200
    assert "预测校准" in page.text
    assert "弱市追突破为负" in page.text
    assert "命中后走强" in page.text


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    from invest_assistant import config as config_mod
    from invest_assistant.db import session as db_session

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
