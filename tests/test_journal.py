"""交易日志 / 清仓复盘 / 经验注入测试（假 LLM，无网络）。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import (
    Base,
    LLMUsage,
    PaperPosition,
    Position,
    TradeJournal,
    TradingLesson,
)
from invest_assistant.db.session import make_engine
from invest_assistant.domain.journal import lessons_text
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.engine.lessons import generate_pending_lessons
from invest_assistant.engine.paper import PaperService
from invest_assistant.llm.client import LLMError, LLMResult
from invest_assistant.llm.prompts import card_user

MONDAY = dt.datetime(2026, 10, 12, 10, 0, 0)
FRIDAY = dt.datetime(2026, 10, 16, 14, 0, 0)


class FakeSettings:
    def __init__(self, paper=None):
        self._paper = {
            "enabled": True,
            "initial_cash": 50000,
            "position_fraction": 0.2,
            "max_positions": 5,
            "max_position_weight": 0.3,
        }
        self._paper.update(paper or {})

    def app_config(self):
        return {"paper": self._paper, "backtest": {"fees": {}}}


class FakeLLM:
    configured = True

    def __init__(self, content=None, fail_on_model=False):
        self._content = content or {
            "summary": "区间买入执行到位，止盈略早。",
            "lessons": ["教训A：跌破失效价必须先走", "教训B：区间上沿分批买"],
            "rating": "一般",
        }
        self._fail_on_model = fail_on_model
        self.last_user = ""
        self.last_model = None
        self.calls = 0

    def complete_json(self, **kwargs):
        self.calls += 1
        self.last_user = str(kwargs.get("user", ""))
        self.last_model = kwargs.get("model")
        if self._fail_on_model and self.last_model:
            raise LLMError("model not available")
        return LLMResult(
            content=self._content,
            raw="",
            model=kwargs.get("model") or "deepseek-chat",
            tokens_in=10,
            tokens_out=10,
            cached_in=0,
            ms=1,
        )


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'journal.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _close_paper_position(sf) -> int:
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        service.try_auto_entry(
            session=session,
            code="600519",
            name="茅台",
            price=10.0,
            reason="进入 AI 建议买入区间 9.5~10.5",
            now=MONDAY,
            plan={"stop": 9.0, "zone_low": 9.5, "zone_high": 10.5, "zone_text": "9.5~10.5"},
        )
        session.commit()
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        service._sell(session, position, 11.0, position.qty, "执行建议：清仓", FRIDAY)
        session.commit()
        return position.id


def test_journal_records_position_ops(sf):
    ps = PositionService(session_factory=sf)
    result = ps.add_position(
        code="600519",
        qty=100,
        price=100.0,
        strategy="value",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=90),
        name="茅台",
        opened_at=MONDAY,
    )
    ps.sell(result["position_id"], qty=40, price=110.0, sold_at=dt.datetime(2026, 10, 15, 14, 0))
    ps.sell(result["position_id"], qty=60, price=112.0, sold_at=FRIDAY)
    with sf() as session:
        rows = session.scalars(select(TradeJournal).order_by(TradeJournal.ts)).all()
        assert [r.action for r in rows] == ["建仓", "减仓", "清仓"]
        assert rows[0].context_json["退出规则"]["止盈%"] == 20
        assert rows[0].name == "茅台"
        assert rows[1].realized_pnl is not None
        assert rows[2].context_json["持有天数"] >= 4
        assert session.scalars(select(Position)).one().status == "closed"


def test_journal_records_paper_ops(sf):
    _close_paper_position(sf)
    with sf() as session:
        rows = session.scalars(select(TradeJournal).order_by(TradeJournal.ts)).all()
        assert [r.source for r in rows] == ["paper", "paper"]
        assert [r.action for r in rows] == ["建仓", "清仓"]
        assert rows[0].context_json["计划"]["stop"] == 9.0
        assert rows[0].context_json["组合"]["总资产"] == 50000.0
        assert rows[1].realized_pnl is not None


def test_lesson_generated_and_injected(sf):
    _close_paper_position(sf)
    llm = FakeLLM()
    report = generate_pending_lessons(session_factory=sf, llm=llm)
    assert report["generated"] == 1
    assert report["codes"] == ["600519"]
    assert llm.last_model == "deepseek-reasoner"  # config.yaml llm.deep_model
    assert "操作记录" in llm.last_user
    assert "进入 AI 建议买入区间" in llm.last_user  # 情境数据完整传入
    with sf() as session:
        lesson = session.scalars(select(TradingLesson)).one()
        assert lesson.rating == "一般"
        assert lesson.lessons_json == ["教训A：跌破失效价必须先走", "教训B：区间上沿分批买"]
        assert lesson.pnl_pct != 0
        assert any(u.purpose == "lesson" for u in session.scalars(select(LLMUsage)))
    # 幂等：不重复复盘
    assert generate_pending_lessons(session_factory=sf, llm=llm)["generated"] == 0

    # 经验文本可用并注入提示词载荷
    text = lessons_text(sf)
    assert text and "教训A" in text[0]
    user = card_user({"code": "600519"}, ["行情"], ["新闻"], lessons=text)
    assert "历史交易复盘经验" in user


def test_lesson_fallback_to_main_model(sf):
    _close_paper_position(sf)
    llm = FakeLLM(fail_on_model=True)
    report = generate_pending_lessons(session_factory=sf, llm=llm)
    assert report["generated"] == 1
    assert llm.calls == 2  # 高级模型失败 → 回退主模型
    with sf() as session:
        lesson = session.scalars(select(TradingLesson)).one()
        assert lesson.model == "deepseek-chat"


def test_journal_page(fresh_env):
    from invest_assistant.db.session import ensure_schema, get_session_factory
    from invest_assistant.web.app import create_app

    ensure_schema()
    sf = get_session_factory()
    with sf() as session:
        session.add(
            TradeJournal(
                ts=MONDAY, source="paper", ref_id=1, code="600519", name="茅台",
                action="建仓", qty=900, price=10.01, amount=9009.0, fee=5.0,
                reason="进入 AI 建议买入区间", context_json={},
            )
        )
        session.add(
            TradingLesson(
                source="paper", ref_id=1, code="600519", name="茅台",
                entry_date=MONDAY.date(), exit_date=FRIDAY.date(), holding_days=4,
                pnl=880.0, pnl_pct=9.7, rating="好", summary="区间买入执行到位",
                lessons_json=["回踩区间分批买入有效"], model="deepseek-reasoner",
            )
        )
        session.commit()
    client = TestClient(create_app())
    page = client.get("/journal")
    assert page.status_code == 200
    assert "经验教训" in page.text
    assert "区间买入执行到位" in page.text
    assert "操作流水" in page.text
    assert "茅台" in page.text


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
