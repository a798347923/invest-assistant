"""B2-08 分析卡测试（假 LLM/假行情/假新闻 + API 注入）。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant import config as config_mod
from invest_assistant.datasource import Bar, FundNav
from invest_assistant.db import session as db_session
from invest_assistant.db.models import AnalysisCard, Base, LLMUsage
from invest_assistant.db.session import ensure_schema, make_engine
from invest_assistant.llm.analyzer import CardAnalyzer
from invest_assistant.llm.client import LLMError, LLMResult
from invest_assistant.web.app import create_app


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


class FakeData:
    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [
            Bar(code=code, date=dt.date(2026, 10, 9), open=1200, high=1270, low=1190,
                close=1250, volume=1000)
        ]


class FakeLLM:
    configured = True

    def __init__(self, error=None):
        self._error = error
        self.last_user = ""

    def complete_json(self, **kwargs):
        self.last_user = str(kwargs.get("user", ""))
        if self._error:
            raise self._error
        return LLMResult(
            content={
                "summary": "现价站上均线但量能不足，等待回踩 21.5~22.3 区间确认。",
                "technicals": "MA20=22.1，近20日区间 19.9~24.0",
                "news_sentiment": "中性",
                "bull_points": ["业绩稳"],
                "bear_points": ["估值偏高"],
                "risks": ["回撤风险"],
                "action_bias": "等待回踩",
                "plan": {
                    "entry_type": "回踩",
                    "entry_low": 21.5,
                    "entry_high": 22.3,
                    "breakout_price": 24.0,
                    "stop_price": 20.8,
                    "entry_zone": "21.5~22.3",
                    "triggers": ["回踩 21.8 附近缩量收稳"],
                    "confirm": ["volume_shrink", "bullish"],
                    "stop_level": "跌破 20.8",
                    "targets": ["24.0", "25.6"],
                    "position_hint": "首仓不超过 3 成，分两批",
                },
                "confidence": 0.6,
                "data_caveats": "样本有限",
            },
            raw="",
            model="deepseek-chat",
            tokens_in=300,
            tokens_out=200,
            cached_in=0,
            ms=200,
        )


def _make_sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'cards.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_card_analyzer_persists_and_records_usage(tmp_path):
    sf = _make_sf(tmp_path)
    llm = FakeLLM()
    analyzer = CardAnalyzer(
        llm=llm,
        data=FakeData(),
        news_fetcher=lambda code: ["2026-10-09 新闻A"],
        session_factory=sf,
    )
    card = analyzer.analyze("600519", name="贵州茅台")
    assert card["code"] == "600519"
    assert card["content"]["action_bias"] == "等待回踩"
    assert card["content"]["plan"]["entry_zone"] == "21.5~22.3"
    assert any("新闻A" in n for n in card["sources"]["news"])
    assert "日线(前复权)" in card["sources"]["prices"]
    assert "指标摘要" in llm.last_user  # 具体价位有数据依据
    with sf() as session:
        row = session.scalars(select(AnalysisCard)).one()
        assert row.prompt_ver == "card_v7"
        assert row.content_json["plan"]["stop_level"] == "跌破 20.8"
        assert row.content_json["plan"]["stop_price"] == 20.8
        assert row.content_json["plan"]["confirm"] == ["volume_shrink", "bullish"]
        assert len(session.scalars(select(LLMUsage)).all()) == 1


def test_card_analyzer_llm_error_raises(tmp_path):
    sf = _make_sf(tmp_path)
    analyzer = CardAnalyzer(
        llm=FakeLLM(error=LLMError("down")),
        data=FakeData(),
        news_fetcher=lambda code: [],
        session_factory=sf,
    )
    with pytest.raises(LLMError):
        analyzer.analyze("600519")
    with sf() as session:
        assert session.scalars(select(AnalysisCard)).all() == []


def test_card_schema_tolerates_loose_types():
    """模型偶尔把文字字段输出为数字/单值——schema 需容错而不是拒收。"""
    from invest_assistant.llm.analyzer import CardRead

    parsed = CardRead.model_validate(
        {
            "summary": "x",
            "plan": {
                "entry_zone": "21.5~22.3",
                "targets": [24.0, 25.6],
                "triggers": "放量站上 24 突破确认",
                "confirm": "volume_expand",
                "position_hint": 3,
            },
        }
    )
    assert parsed.plan.targets == ["24.0", "25.6"]
    assert parsed.plan.triggers == ["放量站上 24 突破确认"]
    assert parsed.plan.confirm == ["volume_expand"]
    assert parsed.plan.position_hint == "3"


def test_card_analyzer_fund_uses_nav(tmp_path):
    sf = _make_sf(tmp_path)

    class FundData:
        def get_fund_nav(self, code, days=60):
            return [FundNav(code=code, date=dt.date(2026, 10, 10), nav=0.8039)]

    analyzer = CardAnalyzer(
        llm=FakeLLM(), data=FundData(), news_fetcher=lambda code: [], session_factory=sf
    )
    card = analyzer.analyze("006327", type="otc_fund")
    assert card["kind"] == "fund"
    assert "基金净值" in card["sources"]["prices"]


def test_cards_api_and_page(fresh_env, monkeypatch):
    ensure_schema()

    def fake_analyze(self, code, name="", type=""):
        from invest_assistant.db.models import AnalysisCard as AC
        from invest_assistant.db.session import get_session_factory

        sf = get_session_factory()
        with sf() as session:
            card = AC(
                code=code, kind="stock", model="fake", prompt_ver="card_v2",
                content_json={
                    "summary": "模拟分析结论", "technicals": "t", "news_sentiment": "n",
                    "bull_points": ["多头A"], "bear_points": ["空头A"], "risks": ["风险A"],
                    "action_bias": "等待回踩",
                    "plan": {
                        "entry_zone": "21.5~22.3",
                        "triggers": ["回踩 21.8 附近缩量收稳"],
                        "stop_level": "跌破 20.8",
                        "targets": ["24.0"],
                        "position_hint": "首仓不超过 3 成",
                    },
                    "confidence": 0.5, "data_caveats": "d",
                },
                sources_json={"prices": "fake", "news": []},
            )
            session.add(card)
            session.commit()
            return {"id": card.id, "code": code, "content": card.content_json,
                    "sources": card.sources_json}

    monkeypatch.setattr("invest_assistant.llm.analyzer.CardAnalyzer.analyze", fake_analyze)

    client = TestClient(create_app())
    resp = client.post("/api/analyze", json={"code": "600519"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"]["summary"] == "模拟分析结论"

    cards = client.get("/api/cards").json()
    assert cards and cards[0]["code"] == "600519"

    page = client.get("/cards")
    assert page.status_code == 200
    assert "模拟分析结论" in page.text
    assert "多头A" in page.text
    assert "【操作计划】" in page.text
    assert "21.5~22.3" in page.text
    assert "跌破 20.8" in page.text

    form = client.post(
        "/cards/analyze", data={"code": "510300", "type": "etf"}, follow_redirects=False
    )
    assert form.status_code == 303
    assert len(client.get("/api/cards").json()) == 2


def test_card_quick_add_to_watchlist(fresh_env, monkeypatch):
    ensure_schema()

    def fake_analyze(self, code, name="", type=""):
        from invest_assistant.db.models import AnalysisCard as AC
        from invest_assistant.db.session import get_session_factory

        sf = get_session_factory()
        with sf() as session:
            card = AC(
                code=code, kind="stock", model="fake", prompt_ver="card_v1",
                content_json={
                    "summary": "模拟分析", "technicals": "", "news_sentiment": "",
                    "bull_points": [], "bear_points": [], "risks": [],
                    "action_bias": "观察", "confidence": 0.5, "data_caveats": "",
                },
                sources_json={"prices": "fake", "news": []},
            )
            session.add(card)
            session.commit()
            return {"id": card.id, "code": code}

    monkeypatch.setattr("invest_assistant.llm.analyzer.CardAnalyzer.analyze", fake_analyze)
    client = TestClient(create_app())
    client.post("/api/analyze", json={"code": "600519"})

    page = client.get("/cards")
    assert "加入自选" in page.text

    # 普通表单路径：重定向
    added = client.post("/cards/watch", data={"code": "600519"}, follow_redirects=False)
    assert added.status_code == 303
    assert [w["code"] for w in client.get("/api/watchlist").json()] == ["600519"]

    # 页面变为“已在自选池”，按钮消失
    page2 = client.get("/cards")
    assert "已在自选池" in page2.text
    assert "加入自选" not in page2.text

    # 幂等：重复提交不产生重复记录
    client.post("/cards/watch", data={"code": "600519"})
    assert len(client.get("/api/watchlist").json()) == 1

    # htmx 局部刷新路径
    client.delete("/api/watchlist/600519")
    partial = client.post(
        "/cards/watch", data={"code": "600519"}, headers={"HX-Request": "true"}
    )
    assert partial.status_code == 200
    assert "已在自选池" in partial.text
