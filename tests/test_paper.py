"""模拟盘（paper）测试：自动买入 / 日报建议 / 次日执行 / 页面（假数据，无网络）。"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import Quote
from invest_assistant.db.models import (
    Alert,
    AnalysisCard,
    Base,
    OrderIntent,
    PaperDaily,
    PaperPosition,
    PaperReport,
    PaperTrade,
    TradeJournal,
    WatchlistItem,
)
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import PositionService
from invest_assistant.engine.paper import PaperService, run_daily_review
from invest_assistant.engine.poller import Poller
from invest_assistant.llm.client import LLMResult
from invest_assistant.notify import DeliveryResult, Dispatcher

MONDAY_10AM = dt.datetime(2026, 10, 12, 10, 0, 0)
TUESDAY_931 = dt.datetime(2026, 10, 13, 9, 31, 0)


class FakeSettings:
    """只实现 paper/backtest/live/watchlist 配置的最小设置对象。"""

    def __init__(self, paper=None, live=None, watchlist=None):
        self._paper = {
            "enabled": True,
            "initial_cash": 50000,
            "position_fraction": 0.2,
            "max_positions": 5,
            "max_position_weight": 0.3,
            "ai_veto": False,  # 测试默认关闭 AI 复核（专项测试单独开启）
            "apply_replay_v3": False,  # 测试默认关闭 v3 固化（专项测试单独开启）
        }
        self._paper.update(paper or {})
        self._live = live or {}
        self._watchlist = watchlist or {}

    def app_config(self):
        return {
            "paper": self._paper,
            "backtest": {"fees": {}},
            "live": self._live,
            "watchlist": self._watchlist,
        }


class FakeVeto:
    """测试用复核员：按设定返回同意/反对/不可用，并记录调用。"""

    def __init__(self, decision=True, reason="方向与数据一致", fail=False):
        self.decision = decision
        self.reason = reason
        self.fail = fail
        self.calls = 0
        self.payloads = []

    @property
    def configured(self):
        return True

    def review(self, payload):
        self.calls += 1
        self.payloads.append(payload)
        if self.fail:
            return None
        return {"approve": self.decision, "reason": self.reason}


class FakeData:
    def __init__(self, quotes=None, daily_bars=None, market_env=None, regime_state="", announcements=None):
        self.quotes = quotes or {}
        self.daily_bars = daily_bars or {}
        self.market_env = market_env or {}
        self.regime_state = regime_state
        self.announcements = announcements or {}
        self.quote_calls = []

    def is_trading_day(self, day=None):
        return True

    def get_quotes(self, codes):
        self.quote_calls.append(list(codes))
        return {c: q for c, q in self.quotes.items() if c in codes}

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [b for b in self.daily_bars.get(code, []) if start <= b.date <= end]

    def get_market_env(self):
        return dict(self.market_env)

    def market_regime_state(self):
        return self.regime_state

    def get_announcements(self, code, limit=8):
        return list(self.announcements.get(code, []))[:limit]


def quote(code, price, prev_close=None, open_price=None, volume=0.0, source="fake"):
    prev = prev_close or price
    return Quote(
        code=code, name="测试", price=price, prev_close=prev,
        open=open_price if open_price is not None else prev, high=price, low=price,
        pct_change=(price / prev - 1) * 100 if prev else 0, volume=volume, amount=0,
        ts=dt.datetime.now(), source=source,
    )


class FakeLLM:
    configured = True

    def __init__(self, content):
        self._content = content
        self.last_user = ""

    def complete_json(self, **kwargs):
        self.last_user = str(kwargs.get("user", ""))
        return LLMResult(
            content=self._content, raw="", model="fake",
            tokens_in=10, tokens_out=10, cached_in=0, ms=1,
        )


class RecordingNotifier:
    name = "rec"

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return DeliveryResult(self.name, True, 1)


def make_flow():
    notifier = RecordingNotifier()
    dispatcher = Dispatcher([notifier], retries=0, backoff=0)
    return type("Flow", (), {"dispatcher": dispatcher})(), notifier


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ---------------------------------------------------------------- 成交与统计


def test_auto_entry_sizing_and_cash(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        entry = service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="进入买入区间", now=MONDAY_10AM
        )
        session.commit()
    assert entry is not None
    assert entry["qty"] == 900  # 50000×20% ÷ (10×1.001) → 900 股
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.qty == 900 and position.status == "open"
        assert abs(position.avg_cost - 10.01) < 1e-6
        assert abs(service.cash(session) - (50000 - 9009.0 - 5.0)) < 0.01
        # 已有持仓 → 不再自动买入
        again = service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="再次触发", now=MONDAY_10AM
        )
        assert again is None


def test_auto_entry_guards(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings({"max_positions": 1}))
    with sf() as session:
        first = service.try_auto_entry(
            session=session, code="600001", name="甲", price=10.0, reason="买点", now=MONDAY_10AM
        )
        assert first is not None
        second = service.try_auto_entry(
            session=session, code="600002", name="乙", price=10.0, reason="买点", now=MONDAY_10AM
        )
        assert second is None  # 超过持仓上限
        session.commit()
    # 关闭开关
    disabled = PaperService(session_factory=sf, settings=FakeSettings({"enabled": False}))
    with sf() as session:
        assert disabled.try_auto_entry(
            session=session, code="600003", name="丙", price=10.0, reason="买点", now=MONDAY_10AM
        ) is None
    # 资金不足（预算买不起 100 股）
    poor = PaperService(session_factory=sf, settings=FakeSettings({"initial_cash": 1500}))
    with sf() as session:
        assert poor.try_auto_entry(
            session=session, code="600004", name="丁", price=10.0, reason="买点", now=MONDAY_10AM
        ) is None


def test_sell_partial_and_close_realized_pnl(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        trade = service._sell(session, position, 12.0, 400, "测试减仓", MONDAY_10AM)
        session.commit()
        assert trade.side == "sell"
        # 成交价 = 12×0.999 = 11.988；费用 = max(佣金,5) + 印花税
        assert abs(trade.price - 11.988) < 1e-6
        assert abs(trade.fee - (5.0 + round(11.988 * 400 * 0.0005, 4))) < 0.01
        assert position.qty == 500 and position.status == "open"
        service._sell(session, position, 12.0, 500, "清仓", MONDAY_10AM)
        session.commit()
        assert position.status == "closed" and position.qty == 0
        fee2 = round(max(11.988 * 500 * 0.00025, 5.0) + 11.988 * 500 * 0.0005, 2)
        expected = (11.988 - 10.01) * 900 - trade.fee - fee2
        assert abs(position.realized_pnl - expected) < 0.02


# ---------------------------------------------------------------- 执行建议


def _add_pending_report(sf, advice: list[dict], day: dt.date = dt.date(2026, 10, 12)):
    with sf() as session:
        session.add(
            PaperReport(
                date=day, created_at=MONDAY_10AM,
                content_json={"summary": "x", "positions": advice}, status="pending",
            )
        )
        session.commit()


def test_execute_pending_on_next_day(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    _add_pending_report(sf, [{"code": "600519", "action": "减仓", "qty_pct": 50, "reason": "到目标位"}])
    # 当日不执行
    assert service.execute_pending({"600519": 12.0}, now=MONDAY_10AM) == []
    # 次日开盘首次轮询执行：900×50% → 450 → 整百 400
    executed = service.execute_pending({"600519": 12.0}, now=TUESDAY_931)
    assert len(executed) == 1 and executed[0]["action"] == "减仓" and executed[0]["qty"] == 400
    with sf() as session:
        report = session.scalars(select(PaperReport)).one()
        assert report.status == "executed"
        assert report.execution_json["executed"][0]["qty"] == 400
        assert session.scalars(select(PaperPosition)).one().qty == 500


def test_execute_pending_expires_stale(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings())
    _add_pending_report(sf, [], day=dt.date(2026, 10, 1))
    result = service.execute_pending({}, now=dt.datetime(2026, 10, 15, 9, 31))
    assert result and result[0]["action"] == "过期作废"
    with sf() as session:
        assert session.scalars(select(PaperReport)).one().status == "expired"


def test_no_reentry_after_same_day_sell(sf):
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        service._sell(session, position, 11.0, position.qty, "测试卖出", MONDAY_10AM)
        session.commit()
    with sf() as session:
        entry = service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.2, reason="再次触发", now=MONDAY_10AM
        )
        assert entry is None  # 当日已卖出，防反手重买


# ---------------------------------------------------------------- 每日日报


def _seed_open_position(sf, service, price=10.0):
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=price, reason="买点", now=MONDAY_10AM
        )
        session.commit()


def test_run_daily_review_with_llm(sf):
    settings = FakeSettings()
    service = PaperService(session_factory=sf, settings=settings)
    _seed_open_position(sf, service)
    llm = FakeLLM(
        content={
            "summary": "组合稳健，注意回撤。",
            "positions": [
                {"code": "600519", "action": "减仓", "qty_pct": 150, "price_hint": "12 附近", "reason": "已达目标位 12"}
            ],
            "risk_notes": ["回撤风险"],
            "confidence": 0.6,
        }
    )
    flow, notifier = make_flow()
    report = run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 11.0)}),
        now=dt.datetime(2026, 10, 12, 15, 40),
    )
    assert report["positions"] == 1 and report["notified"] is True
    assert notifier.messages and "【模拟盘日报】" in notifier.messages[0].title
    with sf() as session:
        saved = session.scalars(select(PaperReport)).one()
        assert saved.status == "pending"
        advice = saved.content_json["positions"][0]
        assert advice["action"] == "减仓"
        assert advice["qty_pct"] == 100  # 150 被收敛到上限 100
        assert session.scalars(select(PaperPosition)).one().last_price == 11.0
        daily = session.scalars(select(PaperDaily)).one()
        assert daily.equity > 0 and daily.positions == 1
    # 同日重跑（非 force）→ 跳过
    again = run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 11.0)}),
        now=dt.datetime(2026, 10, 12, 15, 41),
    )
    assert again.get("skipped")


def test_review_stop_rule_forces_clear(sf):
    settings = FakeSettings()
    service = PaperService(session_factory=sf, settings=settings)
    _seed_open_position(sf, service, price=9.3)
    with sf() as session:
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v3",
                content_json={
                    "summary": "s",
                    "plan": {
                        "entry_type": "回踩", "entry_low": 8.5, "entry_high": 9.2,
                        "stop_price": 9.0, "entry_zone": "8.5~9.2", "stop_level": "跌破 9.0",
                    },
                },
                sources_json={},
            )
        )
        session.commit()
    llm = FakeLLM(
        content={
            "summary": "横盘观察",
            "positions": [{"code": "600519", "action": "持有", "qty_pct": 0, "reason": "趋势未坏"}],
            "risk_notes": [], "confidence": 0.5,
        }
    )
    flow, _ = make_flow()
    run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 8.8)}),  # 跌破失效价 9.0
        now=dt.datetime(2026, 10, 12, 15, 40),
    )
    with sf() as session:
        advice = session.scalars(select(PaperReport)).one().content_json["positions"][0]
        assert advice["action"] == "清仓"
        assert "【规则】" in advice["reason"]


def test_review_missing_advice_defaults_hold(sf):
    settings = FakeSettings()
    service = PaperService(session_factory=sf, settings=settings)
    _seed_open_position(sf, service)
    llm = FakeLLM(content={"summary": "ok", "positions": [], "risk_notes": [], "confidence": 0.4})
    flow, _ = make_flow()
    run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 10.5)}),
        now=dt.datetime(2026, 10, 12, 15, 40),
    )
    with sf() as session:
        advice = session.scalars(select(PaperReport)).one().content_json["positions"][0]
        assert advice["action"] == "持有"


def test_poller_refreshes_paper_prices(sf):
    """模拟盘持仓现价/名称应随盘中轮询刷新（面板与统计即时可见）。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 10.8)})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    poller.run_once(now=dt.datetime(2026, 10, 12, 10, 30), skip_time_check=True)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.last_price == 10.8  # 现价已随轮询刷新
        assert position.name == "测试"  # 名称随行情更新
        stats = paper.portfolio_stats(session)
        assert abs(stats["market_value"] - 900 * 10.8) < 0.01


def test_execute_add_deferred_until_confirmations_met(sf):
    """日报「加仓」建议：次日开盘确认未过 → 推迟；当日盘中满足 → 执行；次日过期作废。"""
    settings = FakeSettings()
    service = PaperService(
        session_factory=sf, settings=settings,
        data=FakeData(daily_bars={"600519": _hist_bars()}),
    )
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.add(
            PaperReport(
                date=dt.date(2026, 10, 12), created_at=MONDAY_10AM,
                content_json={"summary": "x", "positions": [{
                    "code": "600519", "action": "加仓", "qty_pct": 30,
                    "confirm": ["bullish"], "reason": "趋势延续",
                }]},
                status="pending",
            )
        )
        session.commit()
    # 次日开盘：现价 9.8 < 今开 10.0 → 确认未过 → 推迟，不执行
    q1 = quote("600519", 9.8, prev_close=10.0, open_price=10.0, source="em")
    assert service.execute_pending({"600519": q1}, now=dt.datetime(2026, 10, 13, 9, 31)) == []
    with sf() as session:
        report = session.scalars(select(PaperReport)).one()
        deferred = report.execution_json["deferred_adds"]
        assert len(deferred) == 1 and deferred[0]["code"] == "600519"
        assert session.scalars(select(PaperPosition)).one().qty == 900
    # 当日盘中：现价 10.1 ≥ 今开 → 确认通过 → 执行加仓
    q2 = quote("600519", 10.1, prev_close=10.0, open_price=10.0, source="em")
    executed = service.retry_deferred_adds({"600519": q2}, now=dt.datetime(2026, 10, 13, 10, 20))
    assert len(executed) == 1 and executed[0]["action"] == "加仓"
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().qty > 900
        report = session.scalars(select(PaperReport)).one()
        assert report.execution_json["deferred_adds"] == []
    # 次日再重试：无待办（当日有效，过期作废）
    q3 = quote("600519", 10.2, prev_close=10.1, open_price=10.0, source="em")
    assert service.retry_deferred_adds({"600519": q3}, now=dt.datetime(2026, 10, 14, 9, 40)) == []


# ---------------------------------------------------------------- AI 复核（否决制）


def test_ai_veto_blocks_entry(sf):
    """AI 反对 → 买入取消；冷却期内复用结论；冷却期后重新询问（同意才买入）。"""
    settings = FakeSettings({"ai_veto": True})
    veto = FakeVeto(decision=False, reason="公告含减持计划且资金持续流出")
    paper = PaperService(session_factory=sf, settings=settings, veto=veto)
    with sf() as session:
        first = paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0,
            reason="进入买入区间", now=MONDAY_10AM,
        )
        session.commit()
    assert first is not None and first.get("vetoed") is True
    assert veto.calls == 1
    assert "反对" in first["reason"]
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        # 冷却期内：复用反对，不再询问
        again = paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="再次触发",
            now=MONDAY_10AM + dt.timedelta(minutes=5),
        )
        assert again is not None and again.get("vetoed") is True
        assert veto.calls == 1
        # 冷却期后重新询问 → 同意 → 买入
        veto.decision = True
        entry = paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="再次触发",
            now=MONDAY_10AM + dt.timedelta(minutes=20),
        )
        session.commit()
    assert entry is not None and not entry.get("vetoed")
    assert veto.calls == 2
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "open"


def test_ai_veto_error_policy(sf):
    """买入复核不可用 → 保守取消；卖出复核不可用 → 按原计划执行。"""
    settings = FakeSettings({"ai_veto": True})
    veto = FakeVeto(fail=True)
    paper = PaperService(session_factory=sf, settings=settings, veto=veto)
    with sf() as session:
        blocked = paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    assert blocked is not None and blocked.get("vetoed") is True
    assert "复核不可用" in blocked["reason"]

    with sf() as session:
        paper._buy(session, "600519", "茅台", 10.0, 900, "测试建仓", MONDAY_10AM)
        session.add(
            PaperReport(
                date=dt.date(2026, 10, 12), created_at=MONDAY_10AM,
                content_json={"summary": "x", "positions": [{
                    "code": "600519", "action": "清仓", "qty_pct": 0, "reason": "风险上升",
                }]},
                status="pending",
            )
        )
        session.commit()
    executed = paper.execute_pending(
        {"600519": quote("600519", 10.5, prev_close=10.0, open_price=10.0, source="em")},
        now=dt.datetime(2026, 10, 13, 9, 31),
    )
    assert len(executed) == 1 and executed[0]["action"] == "清仓"
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "closed"


def test_ai_veto_blocks_sell_and_stop_is_exempt(sf):
    """AI 反对卖出 → 减仓取消；纪律止损默认豁免（不询问、照常执行）。"""
    settings = FakeSettings({"ai_veto": True})
    veto = FakeVeto(decision=False, reason="与失效价无关，刚买入不久")
    paper = PaperService(session_factory=sf, settings=settings, veto=veto)
    with sf() as session:
        paper._buy(session, "600519", "茅台", 10.0, 900, "测试建仓", MONDAY_10AM)
        session.add(
            PaperReport(
                date=dt.date(2026, 10, 12), created_at=MONDAY_10AM,
                content_json={"summary": "x", "positions": [{
                    "code": "600519", "action": "减仓", "qty_pct": 50, "reason": "到目标位",
                }]},
                status="pending",
            )
        )
        session.commit()
    executed = paper.execute_pending(
        {"600519": quote("600519", 10.5, prev_close=10.0, open_price=10.0, source="em")},
        now=dt.datetime(2026, 10, 13, 9, 31),
    )
    assert executed == []
    with sf() as session:
        report = session.scalars(select(PaperReport)).one()
        skipped = report.execution_json["skipped"]
        assert skipped and "AI 反对" in skipped[0]["reason"]
        assert session.scalars(select(PaperPosition)).one().qty == 900  # 卖出被取消

    # 止损路径：默认豁免 → 不发起复核、照常清仓
    veto.calls = 0
    with sf() as session:
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v7",
                content_json={"summary": "s", "plan": {
                    "entry_type": "回踩", "entry_low": 9.0, "entry_high": 10.5,
                    "stop_price": 9.5, "entry_zone": "9.0~10.5", "stop_level": "跌破 9.5"}},
                sources_json={},
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 9.3, prev_close=9.5, open_price=9.6, source="em")})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 13, 10, 0), skip_time_check=True)
    assert any(a.get("action") == "止损清仓" for a in result.paper_actions)
    assert veto.calls == 0  # 止损豁免：不发起复核
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "closed"


def test_veto_schema_tolerance():
    from invest_assistant.llm.veto import VetoRead

    assert VetoRead.model_validate({"approve": "反对", "reason": "利空"}).approve is False
    assert VetoRead.model_validate({"approve": True}).approve is True
    assert VetoRead.model_validate({"approve": "同意", "reason": None}).reason == ""


# ---------------------------------------------------------------- v3 经验固化


def _v3_card(confirm=None, entry_type="回踩", breakout=None, stop=8.5):
    from invest_assistant.db.models import AnalysisCard as AC

    plan = {
        "entry_type": entry_type,
        "entry_low": 9.0,
        "entry_high": 10.5,
        "stop_price": stop,
        "entry_zone": "9.0~10.5",
        "stop_level": f"跌破 {stop}",
    }
    if breakout is not None:
        plan["breakout_price"] = breakout
    if confirm is not None:
        plan["confirm"] = confirm
    return AC(code="600519", kind="stock", model="fake", prompt_ver="card_v7",
              content_json={"summary": "s", "plan": plan}, sources_json={})


def test_v3_weak_market_blocks_entry(sf):
    """v3：弱市（双指数+滞回状态）暂停自动买入，但到价提醒照常发出。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
        market_env={"指数": {"上证指数": {"涨跌%": -1.2, "相对20日线%": -2.5}}},
        regime_state="weak",
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        # 弱市默认不推送「买入计划」到价提醒
        assert session.scalars(
            select(Alert).where(Alert.dedup_key.like("%plan_buy%"))
        ).all() == []


def test_weak_market_plan_alert_opt_in(sf):
    """弱市提醒开关：显式关闭 suppress 后，到价提醒照常推送。"""
    settings = FakeSettings(
        {"apply_replay_v3": True},
        watchlist={"suppress_plan_alerts_in_weak": False},
    )
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
        regime_state="weak",
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        assert session.scalars(
            select(Alert).where(Alert.dedup_key.like("%plan_buy%"))
        ).one() is not None


def test_v3_pullback_requires_above_ma5_and_records_stop(sf):
    """v3：回踩型必须站上 MA5；入场时固化机械止损（×0.98）。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.4, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    # 现价 9.4 < MA5(9.48) → above_ma5 不满足 → 不买
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
    # 现价 9.6 ≥ MA5(9.52) 且收复前日高点（9.55）→ 买入，止损 = 近10低 9.4 − ATR14(0.15) = 9.25
    fake.quotes["600519"] = quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 20), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result2.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert abs(position.stop_price - 9.25) < 1e-6  # ATR 自适应缓冲（v5）


def test_v5_pullback_reclaim_required(sf):
    """v5：回踩型必须收复前一日高点（站上MA5 但未收复前高 → 不买）。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.53, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    # 9.53 ≥ MA5(9.506) 但未收复前日高点 9.55 → 增强过滤拦截
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
    # 9.60 > 9.55 → 通过并买入
    fake.quotes["600519"] = quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 20), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result2.paper_actions)


def test_v7_pullback_paused_below_ma20_even_if_regime_strong(sf):
    """v7：滞回状态为强但上证在 MA20 下方 → 回踩暂停自动买入；突破型不受此限。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
        market_env={"指数": {"上证指数": {"涨跌%": -0.3, "相对20日线%": -0.5}}},
        regime_state="strong",
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []  # 回踩被附加闸门拦下
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        # 到价提醒照常
        assert session.scalars(
            select(Alert).where(Alert.dedup_key.like("%plan_buy%"))
        ).one() is not None
    # 同环境下（滞回强市）突破型照常买入
    with sf() as session:
        session.add(_v3_card(confirm=["volume_expand", "bullish"], entry_type="突破", breakout=9.55, stop=9.2))
        session.commit()
    fake.quotes["600519"] = quote("600519", 9.6, prev_close=9.4, open_price=9.3, volume=15000.0, source="em")
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 20), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result2.paper_actions)


# （v8 的动量门槛/跳空限价已被 v9 撤销：总利润口径下它们减少总收益，见 docs/rule-experiments.md）

def test_v3_breakout_wider_stop_tolerance(sf):
    """v3：突破型机械止损 ×0.95（更宽）——卡片失效价先到也不清仓，跌穿机械止损才清仓。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_expand", "bullish"], entry_type="突破", breakout=9.55, stop=9.2))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.4, open_price=9.3, volume=15000.0, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert abs(position.stop_price - round(9.4 * 0.95, 4)) < 1e-6  # 突破型 -5%
        assert position.target_price == round(9.6 * 1.12, 4)  # v12 目标 +12%
        assert abs(position.partial_price - round(9.6 * 1.001 * 1.10, 4)) < 1e-4  # 到 +10% 减半
    # 9.0：跌破卡片失效价 9.2（触发计划失效提醒），但未破机械止损 8.93 → 不清仓
    fake.quotes["600519"] = quote("600519", 9.0, prev_close=9.6, open_price=9.5, source="em")
    poller.run_once(now=dt.datetime(2026, 10, 12, 13, 30), skip_time_check=True)
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "open"
    # 8.9：跌破机械止损 8.93 → 清仓（原因注明机械止损价）
    fake.quotes["600519"] = quote("600519", 8.9, prev_close=9.0, open_price=8.95, source="em")
    result3 = poller.run_once(now=dt.datetime(2026, 10, 12, 14, 0), skip_time_check=True)
    assert any(a.get("action") == "止损清仓" for a in result3.paper_actions)
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "closed"
        alert = session.scalars(select(Alert).where(Alert.dedup_key.like("paper-stop-%"))).one()
        assert "机械止损价" in alert.context_json["reason"]


def test_v3_review_uses_mechanical_stop(sf):
    """日报强制清仓以机械止损为准：现价介于机械止损与卡片失效价之间时不强制清仓。"""
    settings = FakeSettings()
    service = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        service._buy(session, "600519", "茅台", 10.0, 900, "测试建仓", MONDAY_10AM)
        position = session.scalars(select(PaperPosition)).one()
        position.stop_price = 8.9  # v3 机械止损
        session.add(_v3_card(confirm=[], stop=9.5))
        session.commit()
    llm = FakeLLM(content={"summary": "ok", "positions": [], "risk_notes": [], "confidence": 0.4})
    flow, _ = make_flow()
    # 现价 9.3：≤ 卡片失效价 9.5，但 > 机械止损 8.9 → 保持 AI 建议（默认持有）
    run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 9.3)}),
        now=dt.datetime(2026, 10, 12, 15, 40),
    )
    with sf() as session:
        advice = session.scalars(select(PaperReport)).one().content_json["positions"][0]
        assert advice["action"] == "持有"
    # 现价 8.8 ＜ 机械止损 → 强制清仓
    run_daily_review(
        session_factory=sf, settings=settings, flow=flow, llm=llm,
        data=FakeData({"600519": quote("600519", 8.8)}),
        now=dt.datetime(2026, 10, 13, 15, 40), force=True,
    )
    with sf() as session:
        report = session.scalars(
            select(PaperReport).order_by(PaperReport.date.desc())
        ).first()
        advice = report.content_json["positions"][0]
        assert advice["action"] == "清仓"
        assert "8.9" in advice["reason"]


# ---------------------------------------------------------------- 轮询集成


def _hist_bars(days: int = 20, volume: float = 10000.0):
    from invest_assistant.datasource.base import Bar

    return [
        Bar(
            code="600519", date=dt.date(2026, 9, 23) + dt.timedelta(days=i),
            open=9.5, high=9.55, low=9.4, close=9.5, volume=volume, amount=0.0, adjust="qfq",
        )
        for i in range(days)
    ]


def test_auto_entry_requires_confirmations(sf):
    """买点触发但计划确认条件（缩量+收阳）未满足时不买入；满足后同一交易日买入。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v6",
                content_json={
                    "summary": "s",
                    "plan": {
                        "entry_type": "回踩", "entry_low": 9.0, "entry_high": 10.5,
                        "stop_price": 8.5, "entry_zone": "9.0~10.5",
                        "triggers": ["回踩缩量收稳"],
                        "confirm": ["volume_shrink", "bullish"],
                    },
                },
                sources_json={},
            )
        )
        session.commit()
    daily = {"600519": _hist_bars()}
    # 第一次轮询：进区间但放量（量比 1.33）+ 现价 < 今开 → 不满足 → 不买入（到价提醒照发）
    fake = FakeData(
        {"600519": quote("600519", 9.3, prev_close=9.4, open_price=9.5, volume=5000.0, source="em")},
        daily_bars=daily,
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []
    with sf() as session:
        assert session.scalars(select(PaperPosition)).all() == []
        assert not session.scalars(
            select(Alert).where(Alert.dedup_key.like("%paper_buy%"))
        ).all()
        alert = session.scalars(select(Alert).where(Alert.dedup_key.like("%plan_buy%"))).one()
        assert "需确认" in alert.context_json["reason"]

    # 第二次轮询：缩量（量比 0.55）+ 收阳 → 全部满足 → 自动买入
    fake.quotes["600519"] = quote(
        "600519", 9.45, prev_close=9.4, open_price=9.4, volume=2500.0, source="em"
    )
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 20), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result2.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.status == "open"


def test_confirmation_from_legacy_text(sf):
    """旧卡无 confirm 字段，从 triggers 文字解析「缩量收稳」并校验。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v5",
                content_json={
                    "summary": "s",
                    "plan": {
                        "entry_type": "回踩", "entry_low": 9.0, "entry_high": 10.5,
                        "stop_price": 8.5, "entry_zone": "9.0~10.5",
                        "triggers": ["回踩 9.3 附近缩量收稳"],
                    },
                },
                sources_json={},
            )
        )
        session.commit()
    daily = {"600519": _hist_bars()}
    fake = FakeData(
        {"600519": quote("600519", 9.3, prev_close=9.4, open_price=9.4, volume=6000.0, source="em")},
        daily_bars=daily,
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert result.paper_actions == []  # 放量 → 缩量条件不满足
    fake.quotes["600519"] = quote(
        "600519", 9.42, prev_close=9.4, open_price=9.4, volume=2000.0, source="em"
    )
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 30), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result2.paper_actions)


def test_poller_stop_out_on_stop_breach(sf):
    """盘中跌破失效价 → 模拟盘立即纪律清仓 + 提醒 + 交易日志。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v5",
                content_json={
                    "summary": "s",
                    "plan": {"entry_type": "回踩", "entry_low": 9.8, "entry_high": 10.5,
                             "stop_price": 9.5, "entry_zone": "9.8~10.5", "stop_level": "跌破 9.5"},
                },
                sources_json={},
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 9.3)})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert any(a.get("action") == "止损清仓" for a in result.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.status == "closed" and position.qty == 0
        alert = session.scalars(select(Alert).where(Alert.dedup_key.like("paper-stop-%"))).one()
        assert "失效价" in alert.context_json["reason"]
        entry = session.scalars(select(TradeJournal).where(TradeJournal.action == "清仓")).one()
        assert "失效价" in entry.reason
    # 已清仓 → 之后轮询无动作
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 5), skip_time_check=True)
    assert result2.paper_actions == []


def test_poller_stop_out_on_prior_invalidation_alert(sf):
    """持仓期间已出过「计划失效」提醒：即使现价回到失效价上方，也按作废处理并清仓。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        paper.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v5",
                content_json={
                    "summary": "s",
                    "plan": {"entry_type": "回踩", "entry_low": 9.8, "entry_high": 10.5,
                             "stop_price": 9.5, "entry_zone": "9.8~10.5", "stop_level": "跌破 9.5"},
                },
                sources_json={},
            )
        )
        session.add(
            Alert(
                kind="watch", triggered_at=dt.datetime(2026, 10, 12, 11, 0), trigger_price=9.4,
                context_json={"code": "600519", "alert_type": "plan_stop", "reason": "跌破失效价"},
                status="sent", dedup_key="watch-600519-plan_stop-20261012",
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 9.6)})  # 现价回到失效价上方
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 13, 30), skip_time_check=True)
    assert any(a.get("action") == "止损清仓" for a in result.paper_actions)
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "closed"


def test_poller_auto_entry_and_execute(sf):
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(
            AnalysisCard(
                code="600519", kind="stock", model="fake", prompt_ver="card_v3",
                content_json={
                    "summary": "s",
                    "plan": {
                        "entry_type": "回踩", "entry_low": 9.5, "entry_high": 10.5,
                        "stop_price": 9.0, "entry_zone": "9.5~10.5", "stop_level": "跌破 9.0",
                    },
                },
                sources_json={},
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 10.0)})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    # 现价落入买入区间 → 自动买入 + paper_buy 提醒
    result = poller.run_once(now=MONDAY_10AM, skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.qty == 900 and position.status == "open"
        alert = session.scalars(select(Alert).where(Alert.dedup_key.like("%paper_buy%"))).one()
        assert "模拟盘自动买入 900 股" in alert.context_json["reason"]

    # 日报建议清仓 → 次日首次轮询自动执行 + paper_exec 提醒 + 当日不再自动重买
    _add_pending_report(sf, [{"code": "600519", "action": "清仓", "qty_pct": 0, "reason": "止损"}])
    fake.quotes["600519"] = quote("600519", 9.6)
    result2 = poller.run_once(now=TUESDAY_931, skip_time_check=True)
    assert any(a.get("action") == "清仓" for a in result2.paper_actions)
    with sf() as session:
        assert session.scalars(select(PaperPosition)).one().status == "closed"
        alert2 = session.scalars(select(Alert).where(Alert.dedup_key.like("%paper-exec%"))).one()
        assert "模拟盘执行 AI 建议" in alert2.context_json["reason"]
        # 卖出反手防抖：当日不再自动重买
        assert len(session.scalars(select(PaperPosition)).all()) == 1


# ---------------------------------------------------------------- 页面


def test_paper_page(fresh_env):
    from invest_assistant.db.session import ensure_schema, get_session_factory
    from invest_assistant.web.app import create_app

    ensure_schema()
    sf = get_session_factory()
    service = PaperService(session_factory=sf, settings=FakeSettings())
    with sf() as session:
        service.try_auto_entry(
            session=session, code="600519", name="茅台", price=10.0, reason="买点", now=MONDAY_10AM
        )
        session.commit()
    client = TestClient(create_app())
    page = client.get("/paper")
    assert page.status_code == 200
    assert "模拟盘" in page.text
    assert "600519" in page.text
    assert "成交记录" in page.text


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


def test_paper_buy_mirrors_live_intent(sf):
    """半自动闭环：模拟盘自动买入成功 → 生成实盘买入指令单。"""
    settings = FakeSettings(
        {"apply_replay_v3": True},
        live={
            "enabled": True,
            "semi_auto_enabled": True,
            "mirror_paper_entries": True,
            "max_order_amount": 5000,
            "whitelist": "off",
            "trading_window": ["09:35-11:25", "13:05-14:55"],
        },
    )
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result.paper_actions)
    with sf() as session:
        intents = session.scalars(select(OrderIntent)).all()
        assert len(intents) == 1
        assert intents[0].side == "buy"
        assert intents[0].source == "paper"
        assert intents[0].status == "pending"
        assert intents[0].qty == 500  # 5000 元预算 / 9.6 → 500 股
        assert intents[0].stop_price == 9.25  # 机械止损（10日低 9.4 − ATR 0.15）


def test_live_partial_take_profit_intent(sf):
    """P2：系统管理的实盘持仓到 +10% → 生成减半卖出指令（仅一次）。"""
    from invest_assistant.domain.positions import ExitRules

    settings = FakeSettings(
        live={
            "enabled": True,
            "semi_auto_enabled": True,
            "max_order_amount": 5000,
            "whitelist": "off",
            "trading_window": ["09:35-11:25", "13:05-14:55"],
            "partial_take_profit_pct": 10,
            "partial_fraction": 0.5,
        },
    )
    ps = PositionService(session_factory=sf)
    created = ps.add_position(
        code="600519", qty=1000, price=10.0, strategy="swing",
        rules=ExitRules(take_profit_pct=12, stop_loss_pct=8, max_holding_days=30),
        opened_at=dt.datetime(2026, 10, 12, 9, 30), note="实盘指令#1：测试",
    )
    fake = FakeData(
        {"600519": quote("600519", 11.0, prev_close=10.5, open_price=10.6, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings,
        paper=PaperService(session_factory=sf, settings=settings),
    )
    poller.run_once(now=dt.datetime(2026, 10, 12, 10, 0), skip_time_check=True)
    with sf() as session:
        intents = session.scalars(select(OrderIntent)).all()
        assert len(intents) == 1
        assert intents[0].side == "sell"
        assert intents[0].qty == 500
        assert intents[0].ref_position_id == created["position_id"]
        assert "分批止盈" in intents[0].reason
    # 同仓位不重复生成
    poller.run_once(now=dt.datetime(2026, 10, 12, 10, 1), skip_time_check=True)
    with sf() as session:
        assert len(session.scalars(select(OrderIntent)).all()) == 1


def test_live_partial_skips_manual_position(sf):
    """手工录入的实盘持仓不套用系统的分批止盈规则。"""
    from invest_assistant.domain.positions import ExitRules

    settings = FakeSettings(
        live={
            "enabled": True,
            "semi_auto_enabled": True,
            "max_order_amount": 5000,
            "whitelist": "off",
            "partial_take_profit_pct": 10,
            "partial_fraction": 0.5,
        },
    )
    ps = PositionService(session_factory=sf)
    ps.add_position(
        code="600519", qty=1000, price=10.0, strategy="swing",
        rules=ExitRules(take_profit_pct=12, stop_loss_pct=8, max_holding_days=30),
        opened_at=dt.datetime(2026, 10, 12, 9, 30), note="",
    )
    fake = FakeData(
        {"600519": quote("600519", 11.0, prev_close=10.5, source="em")},
        daily_bars={"600519": _hist_bars()},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings,
        paper=PaperService(session_factory=sf, settings=settings),
    )
    poller.run_once(now=dt.datetime(2026, 10, 12, 10, 0), skip_time_check=True)
    with sf() as session:
        assert session.scalars(select(OrderIntent)).all() == []


def test_live_buy_skipped_on_negative_announcement(sf):
    """实盘买入前公告过滤：近期有减持等负面公告 → 不生成实盘指令（模拟盘照常）。"""
    settings = FakeSettings(
        {"apply_replay_v3": True},
        live={
            "enabled": True,
            "semi_auto_enabled": True,
            "mirror_paper_entries": True,
            "max_order_amount": 5000,
            "whitelist": "off",
            "trading_window": ["09:35-11:25", "13:05-14:55"],
            "announcement_filter": True,
            "announcement_block_keywords": ["减持"],
        },
    )
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(WatchlistItem(code="600519", alert_pct=0))
        session.add(_v3_card(confirm=["volume_shrink", "bullish"]))
        session.commit()
    fake = FakeData(
        {"600519": quote("600519", 9.6, prev_close=9.5, open_price=9.3, volume=2500.0, source="em")},
        daily_bars={"600519": _hist_bars()},
        announcements={"600519": ["10-10 【重要】股东减持计划公告"]},
    )
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 11, 0), skip_time_check=True)
    assert any(a.get("action") == "自动买入" for a in result.paper_actions)
    with sf() as session:
        assert session.scalars(select(OrderIntent)).all() == []


def test_paper_partial_then_target_exit(sf):
    """v12：到 +10% 减半，余仓到目标价清仓。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(
            PaperPosition(
                code="600519", name="贵州茅台", qty=1000, avg_cost=10.0, status="open",
                opened_at=dt.datetime(2026, 10, 9, 10, 0),
                target_price=11.2, partial_price=11.0,
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 11.05, prev_close=10.8, source="em")})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 10, 0), skip_time_check=True)
    assert any(a.get("action") == "分批止盈" for a in result.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.qty == 500
        assert position.partial_taken is True
        sells = session.scalars(select(PaperTrade).where(PaperTrade.side == "sell")).all()
        assert len(sells) == 1 and sells[0].qty == 500
    fake.quotes["600519"] = quote("600519", 11.25, prev_close=11.05, source="em")
    result2 = poller.run_once(now=dt.datetime(2026, 10, 12, 10, 1), skip_time_check=True)
    assert any(a.get("action") == "目标止盈" for a in result2.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.status == "closed"
        assert position.qty == 0


def test_paper_timeout_exit_after_30_trading_days(sf):
    """v12：持有满 30 个交易日仍未离场 → 当前价清仓。"""
    settings = FakeSettings({"apply_replay_v3": True})
    paper = PaperService(session_factory=sf, settings=settings)
    with sf() as session:
        session.add(
            PaperPosition(
                code="600519", name="贵州茅台", qty=1000, avg_cost=10.0, status="open",
                opened_at=dt.datetime(2026, 8, 3, 10, 0), target_price=99.0,
            )
        )
        session.commit()
    fake = FakeData({"600519": quote("600519", 10.2, prev_close=10.1, source="em")})
    poller = Poller(
        data=fake, positions=PositionService(session_factory=sf),
        session_factory=sf, settings=settings, paper=paper,
    )
    result = poller.run_once(now=dt.datetime(2026, 10, 12, 10, 0), skip_time_check=True)
    assert any(a.get("action") == "持有到期" for a in result.paper_actions)
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.status == "closed"


def test_paper_import_position(sf):
    """手动导入持仓：按给定成本入账、扣现金、写成交与日志；重复导入报错。"""
    settings = FakeSettings()
    paper = PaperService(session_factory=sf, settings=settings)
    result = paper.import_position(
        code="600812", qty=200, cost=6.655, name="华北制药", last_price=6.70
    )
    assert abs(result["avg_cost"] - 6.655) < 1e-6
    with sf() as session:
        position = session.scalars(select(PaperPosition)).one()
        assert position.qty == 200
        assert abs(position.avg_cost - 6.655) < 1e-6
        assert position.last_price == 6.70
        trade = session.scalars(select(PaperTrade)).one()
        assert trade.side == "buy"
        assert abs(trade.price - 6.655) < 1e-6
        assert abs(trade.amount - 1331.0) < 0.01
        stats = paper.portfolio_stats(session)
        assert abs(stats["cash"] - (50000 - 1331.0 - trade.fee)) < 0.01
    with pytest.raises(ValueError):
        paper.import_position(code="600812", qty=100, cost=6.70)

