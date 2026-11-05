"""B3-01 初筛与候选池测试（注入快照，无网络）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant import config as config_mod
from invest_assistant.db import session as db_session
from invest_assistant.db.models import Base, Candidate, ScreenTemplate
from invest_assistant.db.session import ensure_schema, get_session_factory, make_engine
from invest_assistant.screening import run_screen
from invest_assistant.screening.templates import filter_row
from invest_assistant.web.app import create_app

ROWS = [
    {"code": "600519", "name": "贵州茅台", "price": 1263.0, "pct": 0.58, "amount": 4.4e9,
     "turnover": 0.3, "pe": 22.0, "pb": 7.5, "market_cap": 15900.0, "source": "test"},
    {"code": "601398", "name": "工商银行", "price": 5.9, "pct": 0.5, "amount": 2e9,
     "turnover": 0.2, "pe": 5.1, "pb": 0.6, "market_cap": 21000.0, "source": "test"},
    {"code": "000001", "name": "平安银行", "price": 11.5, "pct": -1.0, "amount": 1.5e9,
     "turnover": 0.9, "pe": 6.0, "pb": 0.8, "market_cap": 2230.0, "source": "test"},
    {"code": "300750", "name": "宁德时代", "price": 250.0, "pct": 4.2, "amount": 8e9,
     "turnover": 3.5, "pe": 25.0, "pb": 4.5, "market_cap": 11000.0, "source": "test"},
    {"code": "600000", "name": "ST测试", "price": 3.0, "pct": 5.0, "amount": 1e8,
     "turnover": 2.0, "pe": 10.0, "pb": 1.0, "market_cap": 200.0, "source": "test"},
    {"code": "600001", "name": "亏损股", "price": 10.0, "pct": 6.0, "amount": 5e8,
     "turnover": 6.0, "pe": -3.0, "pb": 3.0, "market_cap": 150.0, "source": "test"},
]

VALUE_PARAMS = {"pe_max": 25, "pb_max": 3, "min_cap": 50, "max_turnover": 10}
TREND_PARAMS = {"pct_min": 2, "min_cap": 80, "turnover_min": 2, "turnover_max": 20}


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()
    yield
    config_mod.get_settings.cache_clear()
    db_session.reset_engine()


def _make_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'screen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_filter_rules():
    ok, reasons, score = filter_row("value", VALUE_PARAMS, ROWS[1])
    assert ok and reasons and score > 0
    assert "PE 5.1" in reasons[1]

    assert filter_row("value", VALUE_PARAMS, ROWS[0])[0] is False  # PB 超限
    assert filter_row("value", VALUE_PARAMS, ROWS[3])[0] is False  # PB 超限
    assert filter_row("trend", TREND_PARAMS, ROWS[4])[0] is False  # ST 排除
    assert filter_row("trend", TREND_PARAMS, ROWS[5])[0] is False  # 亏损（PE<0）
    assert filter_row("trend", TREND_PARAMS, ROWS[3])[0] is True   # 宁德时代


def test_run_screen_creates_and_dedupes(tmp_path):
    sf = _make_env(tmp_path)
    result = run_screen(session_factory=sf, snapshot=ROWS, top_n=10)
    assert result["created"] == 4  # value×2 + growth×1 + trend×1
    assert result["by_template"]["value"] == 2
    assert result["source"] == "inline"

    again = run_screen(session_factory=sf, snapshot=ROWS, top_n=10)
    assert again["created"] == 0  # 同日幂等

    with sf() as session:
        candidates = session.scalars(select(Candidate)).all()
        assert len(candidates) == 4
        one = candidates[0]
        assert one.reasons_json["reasons"]
        assert one.status == "new"
        assert len(session.scalars(select(ScreenTemplate)).all()) == 3


def test_candidates_api_and_page(fresh_env):
    ensure_schema()
    run_screen(session_factory=get_session_factory(), snapshot=ROWS, top_n=10)
    client = TestClient(create_app())

    data = client.get("/api/candidates").json()
    assert data
    assert any(item["template"] == "value" for item in data)
    assert any(item["name"] == "工商银行" for item in data)

    page = client.get("/candidates")
    assert page.status_code == 200
    assert "工商银行" in page.text
    assert "PE 5.1" in page.text  # 入选理由中的具体条件


def test_run_screen_buy_setup_stage(tmp_path):
    """买点评估：setup_required 过滤无买点候选，只留有买点的（全局 Top N）。"""
    sf = _make_env(tmp_path)
    setups = {
        "600519": {"label": "无明确买点", "bonus": 0.0, "note": "距 MA20 +8.0%"},
        "601398": {"label": "回踩支撑", "bonus": 22.5, "note": "距 MA20 +1.2%"},
        "000001": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 1.0%"},
        "300750": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 2.0%"},
    }
    result = run_screen(
        session_factory=sf, snapshot=ROWS, top_n=5, setup_fn=lambda code: setups[code]
    )
    assert result["created"] == 3
    assert result["setup"] == {"回踩支撑": 1, "突破在即": 2}
    with sf() as session:
        rows = session.scalars(select(Candidate)).all()
        assert {r.code for r in rows} == {"601398", "000001", "300750"}
        joined = {r.code: "；".join(r.reasons_json["reasons"]) for r in rows}
        assert "买点：回踩支撑" in joined["601398"]
        assert rows[0].reasons_json["setup"]["label"] in ("回踩支撑", "突破在即")


def test_run_screen_top_n_cap_and_order(tmp_path):
    """按（买点加成，模板评分）排序取 Top N。"""
    sf = _make_env(tmp_path)
    setups = {
        "600519": {"label": "无明确买点", "bonus": 0.0, "note": "-"},
        "601398": {"label": "回踩支撑", "bonus": 22.5, "note": "-"},
        "000001": {"label": "突破在即", "bonus": 25.0, "note": "-"},
        "300750": {"label": "突破在即", "bonus": 25.0, "note": "-"},
    }
    result = run_screen(
        session_factory=sf, snapshot=ROWS, top_n=2, setup_fn=lambda code: setups[code]
    )
    assert result["created"] == 2
    with sf() as session:
        codes = {r.code for r in session.scalars(select(Candidate)).all()}
        # 等加成时按模板评分：000001（价值 107.1）> 300750（趋势 24.5）
        assert codes == {"000001", "300750"}


def test_run_screen_experience_weak_market(tmp_path):
    """弱市经验校准：突破在即被剔除、回踩降权、候选上限收紧到 3。"""
    sf = _make_env(tmp_path)
    setups = {
        "600519": {"label": "无明确买点", "bonus": 0.0, "note": "-"},
        "601398": {"label": "回踩支撑", "bonus": 22.5, "note": "距 MA20 +1.2%"},
        "000001": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 1.0%"},
        "300750": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 2.0%"},
    }
    result = run_screen(
        session_factory=sf, snapshot=ROWS, top_n=5,
        setup_fn=lambda code: setups[code], regime="weak",
    )
    assert result["created"] == 1  # 突破型全剔除，只剩回踩型
    assert any("剔除" in line for line in result["experience"])
    with sf() as session:
        candidates = session.scalars(select(Candidate)).all()
        assert {c.code for c in candidates} == {"601398"}
        joined = "；".join(candidates[0].reasons_json["reasons"])
        assert "权重 ×0.6" in joined  # 回踩降权标注进入理由


def test_run_screen_experience_strong_market_boost(tmp_path):
    """强市经验校准：突破升权 ×1.2、回踩降权 ×0.8，并写入买点说明。"""
    sf = _make_env(tmp_path)
    setups = {
        "600519": {"label": "无明确买点", "bonus": 0.0, "note": "-"},
        "601398": {"label": "回踩支撑", "bonus": 22.5, "note": "距 MA20 +1.2%"},
        "000001": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 1.0%"},
        "300750": {"label": "突破在即", "bonus": 25.0, "note": "距 20 日高点 2.0%"},
    }
    result = run_screen(
        session_factory=sf, snapshot=ROWS, top_n=5,
        setup_fn=lambda code: setups[code], regime="strong",
    )
    assert result["created"] == 3
    with sf() as session:
        rows = {c.code: c for c in session.scalars(select(Candidate)).all()}
        assert rows["000001"].reasons_json["setup"]["bonus"] == 30.0  # 25 × 1.2
        assert rows["601398"].reasons_json["setup"]["bonus"] == 18.0  # 22.5 × 0.8
        assert any("权重 ×1.2" in r for r in rows["000001"].reasons_json["reasons"])
        assert any("权重 ×0.8" in r for r in rows["601398"].reasons_json["reasons"])


def test_run_screen_experience_unknown_regime_noop(tmp_path):
    """大盘状态未知（注入快照且未给 regime）：不做权重调整。"""
    sf = _make_env(tmp_path)
    setups = {
        "600519": {"label": "无明确买点", "bonus": 0.0, "note": "-"},
        "601398": {"label": "回踩支撑", "bonus": 22.5, "note": "-"},
        "000001": {"label": "突破在即", "bonus": 25.0, "note": "-"},
        "300750": {"label": "突破在即", "bonus": 25.0, "note": "-"},
    }
    result = run_screen(
        session_factory=sf, snapshot=ROWS, top_n=5, setup_fn=lambda code: setups[code]
    )
    assert result["created"] == 3
    assert any("未知" in line for line in result["experience"])
    with sf() as session:
        rows = {c.code: c for c in session.scalars(select(Candidate)).all()}
        assert rows["000001"].reasons_json["setup"]["bonus"] == 25.0  # 未调整
        assert rows["601398"].reasons_json["setup"]["bonus"] == 22.5


def test_run_screen_setup_data_unavailable_falls_back(tmp_path):
    """买点数据全部不可用 → 退化为模板评分（旧语义），不再阻塞初筛。"""
    sf = _make_env(tmp_path)

    def broken(code):
        raise RuntimeError("no data")

    result = run_screen(session_factory=sf, snapshot=ROWS, top_n=10, setup_fn=broken)
    assert result["created"] == 4  # value×2 + growth×1 + trend×1
    assert result["setup"] == {}


def test_candidates_page_watch_button(fresh_env):
    """候选池每行：分析 + 一键加入自选（htmx 局部刷新，来源记为候选池）。"""
    ensure_schema()
    run_screen(session_factory=get_session_factory(), snapshot=ROWS, top_n=10)
    client = TestClient(create_app())

    page = client.get("/candidates")
    assert page.status_code == 200
    assert "加入自选" in page.text

    partial = client.post(
        "/cards/watch",
        data={"code": "601398", "source": "候选池"},
        headers={"HX-Request": "true"},
    )
    assert partial.status_code == 200
    assert "已在自选池" in partial.text

    from invest_assistant.db.models import WatchlistItem

    with get_session_factory()() as session:
        item = session.get(WatchlistItem, "601398")
        assert item is not None and item.note == "来自候选池"

    # 刷新后该行变为「已在自选池」
    page2 = client.get("/candidates")
    assert "已在自选池" in page2.text

    # 非 htmx 回退：重定向回候选池页
    redirected = client.post(
        "/cards/watch", data={"code": "000001", "source": "候选池"}, follow_redirects=False
    )
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/candidates"
