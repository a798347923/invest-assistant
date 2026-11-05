"""B1-06 轮询与调度测试（假数据源，无网络）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource import DataUnavailable, FundEstimate, FundNav, MarketDataError, Quote
from invest_assistant.db.models import Alert, AnalysisCard, Base, ExitRule, Instrument, JobRun, KvSetting, WatchlistItem
from invest_assistant.db.session import make_engine
from invest_assistant.domain.positions import ExitRules, PositionService
from invest_assistant.engine.poller import Poller, in_windows
from invest_assistant.engine.scheduler import JobRunner

MONDAY_10AM = dt.datetime(2026, 10, 12, 10, 0, 0)  # 交易日 10:00（周一）


class FakeData:
    def __init__(self, quotes=None, trading_day=True, fail=False):
        self.quotes = quotes or {}
        self.trading_day = trading_day
        self.fail = fail
        self.quote_calls = 0

    def is_trading_day(self, day=None):
        return self.trading_day

    def get_quotes(self, codes):
        self.quote_calls += 1
        if self.fail:
            raise DataUnavailable("fake source down")
        return {c: q for c, q in self.quotes.items() if c in codes}


def quote(code, price, prev_close=100.0):
    return Quote(code=code, name="测试", price=price, prev_close=prev_close, open=prev_close,
                 high=price, low=price, pct_change=(price / prev_close - 1) * 100,
                 volume=0, amount=0, ts=dt.datetime.now(), source="fake")


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'poll.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    positions = PositionService(session_factory=sf)
    return sf, positions


def seed(sf, positions, code="600519", price=100.0, tp=20, trailing=8, max_days=365):
    result = positions.add_position(
        code=code, qty=100, price=price, strategy="value",
        rules=ExitRules(take_profit_pct=tp, trailing_drawdown_pct=trailing, max_holding_days=max_days),
    )
    return result


class FakeFundClient:
    """默认查无此基（探测失败）→ 不触发重分类。"""

    def __init__(self, known=(), nav_known=(), est_pct=0.1):
        self.known = set(known)
        self.nav_known = set(nav_known)
        self.est_pct = est_pct
        self.calls = 0

    def get_estimates(self, codes):
        self.calls += 1
        out = {}
        for code in codes:
            if code in self.known:
                out[code] = FundEstimate(
                    code=code, name="测试基金", est_nav=1.0, est_pct=self.est_pct,
                    ts=dt.datetime.now(),
                )
        if not out:
            raise MarketDataError("无基金估值")
        return out

    def get_nav_history(self, code):
        if code in self.nav_known:
            return [FundNav(code=code, date=dt.date(2026, 10, 12), nav=1.0)]
        raise MarketDataError("无净值")


class _NoParkingSettings:
    """把弱市停泊关掉：轮询测试不涉及停泊（停泊另有专项测试）。

    之所以要显式注入，是因为 config.yaml 是真实配置——一旦线上打开 parking，
    这些用例的"空仓直接跳过"语义就会被打破。
    """

    def __init__(self, inner):
        self._inner = inner

    def app_config(self):
        cfg = dict(self._inner.app_config() or {})
        paper = dict(cfg.get("paper") or {})
        paper["parking_enabled"] = False
        cfg["paper"] = paper
        return cfg

    def __getattr__(self, item):
        return getattr(self._inner, item)


def make_poller(env, fake, fund=None, paper=None, settings=None):
    sf, positions = env
    if paper is None:
        from invest_assistant.engine.paper import PaperService

        paper = PaperService(session_factory=sf)
        paper.enabled = False  # 自选提醒测试不涉及模拟盘（模拟盘另有专项测试）
    if settings is None:
        from invest_assistant.config import get_settings

        settings = _NoParkingSettings(get_settings())
    return (
        Poller(
            data=fake,
            positions=positions,
            session_factory=sf,
            fund=fund or FakeFundClient(),
            paper=paper,
            settings=settings,
        ),
        sf,
    )


class WatchSettings:
    """轮询设置桩：只控制自选提醒开关，模拟盘由注入的 PaperService 负责。"""

    def __init__(self, movement_alerts=True):
        self._movement_alerts = movement_alerts

    def app_config(self):
        return {
            "poll": {"windows": ["09:30-11:30", "13:00-15:00"]},
            "watchlist": {"movement_alerts": self._movement_alerts},
        }


# ---------------------------------------------------------------- 时段工具

def test_in_windows():
    windows = ["09:30-11:30", "13:00-15:00"]
    assert in_windows(dt.time(10, 0), windows) is True
    assert in_windows(dt.time(12, 0), windows) is False
    assert in_windows(dt.time(15, 0), windows) is True
    assert in_windows(dt.time(8, 0), windows) is False


# ---------------------------------------------------------------- 触发与锁存

def test_trigger_creates_alert_and_latches(env):
    sf, positions = env
    seed(sf, positions, price=100.0)
    poller, _ = make_poller(env, FakeData(quotes={"600519": quote("600519", 121.0)}))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.skipped_reason == ""
    assert len(result.triggered) == 1
    assert result.triggered[0]["rule_type"] == "fixed_take_profit"
    assert result.rules_checked == 3  # tp + trailing + max_holding

    with sf() as s:
        tp_rule = s.scalars(select(ExitRule).where(ExitRule.type == "fixed_take_profit")).one()
        assert tp_rule.status == "triggered"
        alerts = s.scalars(select(Alert)).all()
        assert len(alerts) == 1
        assert alerts[0].kind == "trigger"
        assert alerts[0].status == "pending"
        assert alerts[0].dedup_key

    # 第二轮：同一触发不再重复
    result2 = poller.run_once(now=MONDAY_10AM)
    assert len(result2.triggered) == 0
    with sf() as s:
        assert len(s.scalars(select(Alert)).all()) == 1


def test_no_trigger_keeps_armed(env):
    sf, positions = env
    seed(sf, positions, price=100.0)
    poller, _ = make_poller(env, FakeData(quotes={"600519": quote("600519", 105.0)}))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.triggered == []
    assert result.rules_checked == 3
    with sf() as s:
        statuses = {r.status for r in s.scalars(select(ExitRule))}
        assert statuses == {"armed"}


# ---------------------------------------------------------------- 跳过与缺失

def test_skip_non_trading_day_and_outside_window(env):
    sf, positions = env
    seed(sf, positions)
    poller, _ = make_poller(env, FakeData(quotes={}, trading_day=False))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.skipped_reason == "非交易日"

    poller2, _ = make_poller(env, FakeData(quotes={}))
    result2 = poller2.run_once(now=dt.datetime(2026, 10, 12, 8, 0, 0))
    assert result2.skipped_reason == "非监控时段"


def test_missing_quote_recorded(env):
    sf, positions = env
    seed(sf, positions, code="600519")
    poller, _ = make_poller(env, FakeData(quotes={}))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.missing_codes == ["600519"]
    assert result.rules_checked == 0


def test_no_positions_skips(env):
    poller, _ = make_poller(env, FakeData(quotes={}))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.skipped_reason == "无持仓与自选"


# ---------------------------------------------------------------- 连续失败告警

def test_fail_streak_warning_and_reset(env):
    sf, positions = env
    seed(sf, positions)
    poller, _ = make_poller(env, FakeData(fail=True))
    r1 = poller.run_once(now=MONDAY_10AM)
    poller.run_once(now=MONDAY_10AM)  # 第二轮：连续失败计数累加（结果不参与断言）
    r3 = poller.run_once(now=MONDAY_10AM)
    r4 = poller.run_once(now=MONDAY_10AM)
    assert r1.fail_streak == 1 and r3.fail_streak == 3
    assert r3.system_warning != ""
    assert r4.system_warning == ""  # 节流：首次跨越阈值告警，此后每 24 轮复提醒
    with sf() as s:
        streak = s.get(KvSetting, "poll_fail_streak")
        assert streak.value_json["count"] == 4

    # 恢复成功后计数清零
    ok_poller, _ = make_poller(env, FakeData(quotes={"600519": quote("600519", 101.0)}))
    ok_poller.run_once(now=MONDAY_10AM)
    with sf() as s:
        streak = s.get(KvSetting, "poll_fail_streak")
        assert streak.value_json["count"] == 0


# ---------------------------------------------------------------- JobRunner

def test_job_runner_records_job_run(env):
    sf, _ = env
    runner = JobRunner(sf)
    wrapped = runner.wrap("test_job", lambda: "done-detail")
    wrapped()
    with sf() as s:
        row = s.scalars(select(JobRun)).one()
        assert row.job == "test_job"
        assert row.status == "ok"
        assert row.detail == "done-detail"

    wrapped_err = runner.wrap("test_job_err", lambda: 1 / 0)
    wrapped_err()
    with sf() as s:
        rows = {r.job: r for r in s.scalars(select(JobRun))}
        assert rows["test_job_err"].status == "error"
        assert "ZeroDivisionError" in rows["test_job_err"].detail


# ---------------------------------------------------------------- 场外基金处理

def test_fund_positions_skipped_in_polling(env):
    sf, positions = env
    positions.add_position(
        code="011186", qty=100000, price=1.0, strategy="drip", type="otc_fund",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    fake = FakeData(quotes={})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert "场外基金" in result.skipped_reason
    assert result.fund_skipped == ["011186"]
    assert fake.quote_calls == 0  # 完全不请求盘中行情


def test_mixed_positions_poll_only_stocks(env):
    sf, positions = env
    seed(sf, positions, code="600519", price=100.0)
    positions.add_position(
        code="011186", qty=100000, price=1.0, strategy="drip", type="otc_fund",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    fake = FakeData(quotes={"600519": quote("600519", 105.0)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert result.rules_checked == 3  # 仅股票的三条规则
    assert result.fund_skipped == ["011186"]
    assert "011186" not in result.missing_codes
    assert fake.quote_calls == 1


def test_mistyped_fund_auto_reclassified(env):
    sf, positions = env
    positions.add_position(
        code="011186", qty=100000, price=1.0, strategy="drip",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )  # 未指定 type → 误标为 a_share
    with sf() as s:
        assert s.get(Instrument, "011186").type == "a_share"

    fund = FakeFundClient(known={"011186"})
    from invest_assistant.config import get_settings

    poller = Poller(
        data=FakeData(fail=True), positions=positions, session_factory=sf, fund=fund,
        settings=_NoParkingSettings(get_settings()),
    )
    result = poller.run_once(now=MONDAY_10AM)
    assert result.reclassified == ["011186"]
    assert result.fail_streak == 0
    assert result.system_warning == ""
    with sf() as s:
        assert s.get(Instrument, "011186").type == "otc_fund"
        streak = s.get(KvSetting, "poll_fail_streak")
        assert streak is None or (streak.value_json or {}).get("count", 0) == 0


def test_mistyped_fund_reclassified_via_nav_probe(env):
    """估值服务未收录时，净值接口也能识别出场外基金。"""
    sf, positions = env
    positions.add_position(
        code="006327", qty=10000, price=1.0, strategy="value",
        rules=ExitRules(take_profit_pct=20, trailing_drawdown_pct=8, max_holding_days=365),
    )
    fund = FakeFundClient(nav_known={"006327"})  # 估值查无 → 回退净值探测
    poller = Poller(
        data=FakeData(fail=True), positions=positions, session_factory=sf, fund=fund
    )
    result = poller.run_once(now=MONDAY_10AM)
    assert result.reclassified == ["006327"]
    with sf() as s:
        assert s.get(Instrument, "006327").type == "otc_fund"


# ---------------------------------------------------------------- 自选池盘中监控

def _add_watch(sf, code, **kwargs):
    with sf() as session:
        session.add(WatchlistItem(code=code, **kwargs))
        session.commit()


def _add_card(sf, code, plan):
    with sf() as session:
        session.add(
            AnalysisCard(
                code=code,
                kind="stock",
                model="fake",
                prompt_ver="card_v3",
                content_json={"summary": "计划测试", "plan": plan},
                sources_json={},
            )
        )
        session.commit()


def test_watchlist_plan_buy_zone_alert(env):
    """现价进入 AI 建议买入区间 → plan_buy 提醒（每日去重）。"""
    sf, positions = env
    _add_watch(sf, "300031", alert_pct=0)
    _add_card(
        sf,
        "300031",
        {
            "entry_type": "回踩", "entry_low": 21.6, "entry_high": 22.2,
            "stop_price": 20.8, "entry_zone": "21.6~22.2",
            "triggers": ["回踩缩量收稳"], "stop_level": "跌破 20.8",
            "position_hint": "首仓 3 成",
        },
    )
    fake = FakeData(quotes={"300031": quote("300031", 22.1)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert alert.context_json["alert_type"] == "plan_buy"
        assert "建议买入区间 21.6~22.2" in alert.context_json["reason"]
        assert alert.context_json["plan"]["stop"] == 20.8

    # 同日再次运行：不重复提醒
    result2 = poller.run_once(now=MONDAY_10AM)
    assert result2.watch_triggered == []


def test_watchlist_plan_stop_alert_and_message(env):
    """跌破失效价 → plan_stop 提醒，消息带计划要素。"""
    from invest_assistant.notify import watch_message

    sf, positions = env
    _add_watch(sf, "300031", alert_pct=0)
    _add_card(
        sf,
        "300031",
        {
            "entry_type": "回踩", "entry_low": 21.6, "entry_high": 22.2,
            "stop_price": 20.8, "entry_zone": "21.6~22.2",
            "triggers": ["回踩缩量收稳"], "stop_level": "跌破 20.8",
        },
    )
    fake = FakeData(quotes={"300031": quote("300031", 20.5)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert alert.context_json["alert_type"] == "plan_stop"
        assert "失效价" in alert.context_json["reason"]
        msg = watch_message(alert.context_json)
        assert "【AI 计划失效】" in msg.title
        assert "AI 买入计划" in msg.body


def test_watchlist_plan_breakout_alert(env):
    """突破型计划：站上突破确认价 → plan_buy。"""
    sf, positions = env
    _add_watch(sf, "300031", alert_pct=0)
    _add_card(sf, "300031", {"entry_type": "突破", "entry_low": 24.0, "entry_high": 24.0})
    fake = FakeData(quotes={"300031": quote("300031", 24.1)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert alert.context_json["alert_type"] == "plan_buy"
        assert "突破确认价 24" in alert.context_json["reason"]


def test_watchlist_plan_disabled_no_alert(env):
    sf, positions = env
    _add_watch(sf, "300031", alert_pct=0, alert_plan=False)
    _add_card(sf, "300031", {"entry_low": 21.6, "entry_high": 22.2, "stop_price": 20.8})
    fake = FakeData(quotes={"300031": quote("300031", 22.0)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert result.watch_triggered == []


def test_watchlist_pct_alert_and_daily_dedup(env):
    sf, positions = env
    _add_watch(sf, "600519", alert_pct=3.0)
    fake = FakeData(quotes={"600519": quote("600519", 106.0, prev_close=100.0)})  # 日内 +6%
    poller, _ = make_poller(env, fake, settings=WatchSettings(movement_alerts=True))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.watch_total == 1
    assert result.watch_checked == 1
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert alert.kind == "watch"
        assert "上涨" in alert.context_json["reason"]

    # 同日再次运行：不重复提醒（每日每条件一次）
    result2 = poller.run_once(now=MONDAY_10AM)
    assert result2.watch_triggered == []
    with sf() as s:
        assert len(s.scalars(select(Alert)).all()) == 1


def test_watchlist_pct_alert_global_switch_off(env):
    """异动提醒总开关关闭：阈值触发的 pct 提醒不生成；手动到价提醒仍保留。"""
    sf, positions = env
    _add_watch(sf, "600519", alert_pct=3.0)
    fake = FakeData(quotes={"600519": quote("600519", 106.0, prev_close=100.0)})  # 日内 +6%
    poller, _ = make_poller(env, fake, settings=WatchSettings(movement_alerts=False))
    result = poller.run_once(now=MONDAY_10AM)
    assert result.watch_checked == 1
    assert result.watch_triggered == []
    with sf() as s:
        assert s.scalars(select(Alert)).all() == []


def test_watchlist_below_price_alert(env):
    sf, positions = env
    _add_watch(sf, "300750", alert_pct=0, alert_price_below=95.0)
    fake = FakeData(quotes={"300750": quote("300750", 94.5)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert "跌破下方提醒价 95" in alert.context_json["reason"]


def test_watchlist_fund_uses_intraday_estimate(env):
    sf, positions = env
    _add_watch(sf, "011186", alert_pct=2.0)
    fund = FakeFundClient(known={"011186"}, est_pct=3.2)
    poller = Poller(
        data=FakeData(quotes={}), positions=positions, session_factory=sf, fund=fund,
        settings=WatchSettings(movement_alerts=True),
    )
    result = poller.run_once(now=MONDAY_10AM)
    assert result.watch_checked == 1
    assert len(result.watch_triggered) == 1
    with sf() as s:
        alert = s.scalars(select(Alert)).one()
        assert "+3.20%" in alert.context_json["reason"]


def test_watchlist_only_portfolio_polls(env):
    sf, positions = env
    _add_watch(sf, "600519", alert_pct=5.0)
    fake = FakeData(quotes={"600519": quote("600519", 103.0, prev_close=100.0)})
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert result.skipped_reason == ""  # 无持仓但有自选 → 正常轮询
    assert result.watch_checked == 1
    assert result.watch_triggered == []  # 3% < 5%


def test_watchlist_and_positions_share_one_quote_call(env):
    sf, positions = env
    seed(sf, positions, code="600519", price=100.0)
    _add_watch(sf, "300750", alert_pct=0)
    fake = FakeData(
        quotes={"600519": quote("600519", 105.0), "300750": quote("300750", 100.0)}
    )
    poller, _ = make_poller(env, fake)
    result = poller.run_once(now=MONDAY_10AM)
    assert fake.quote_calls == 1  # 持仓 + 自选合并为一次批量请求
    assert result.watch_total == 1
