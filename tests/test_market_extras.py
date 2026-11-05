"""第一批数据增强测试：腾讯源 / 东财扩展（资金流/两融/公告）/ 大盘环境 / 提示词注入。"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.config import get_settings
from invest_assistant.datasource import MarketDataError, MarketDataService
from invest_assistant.datasource.base import Bar, Quote
from invest_assistant.datasource.em_ext import (
    _parse_announcements,
    _parse_flow_klines,
    _parse_forecast,
    _parse_fundamentals,
    _parse_margin_rows,
)
from invest_assistant.datasource.tencent import TencentClient, _parse_kline_payload
from invest_assistant.db.models import Base
from invest_assistant.db.session import make_engine
from invest_assistant.llm.analyzer import CardAnalyzer
from invest_assistant.llm.client import LLMResult
from invest_assistant.llm.prompts import eod_user


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'extras.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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


# ---------------------------------------------------------------- 腾讯源解析


def test_tencent_parse_quote_line():
    fields = [""] * 88
    fields[0], fields[1] = "1", "贵州茅台"
    fields[3], fields[4], fields[5], fields[6] = "1312.97", "1286.00", "1286.00", "57677"
    fields[30], fields[32] = "20261015161456", "2.10"
    fields[33], fields[34], fields[37] = "1317.76", "1282.00", "753771"
    line = 'v_sh600519="' + "~".join(fields) + '";'
    quote = TencentClient.parse_quote_line(line)
    assert quote is not None
    assert quote.code == "600519"
    assert quote.name == "贵州茅台"
    assert quote.price == 1312.97
    assert quote.prev_close == 1286.0
    assert abs(quote.pct_change - 2.10) < 0.01
    assert quote.volume == 57677 * 100  # 手 → 股
    assert quote.amount == 753771 * 1e4  # 万 → 元
    assert TencentClient.parse_quote_line('var x="1"') is None


def test_tencent_parse_kline_payload():
    payload = {
        "data": {
            "sh600519": {
                "qfqday": [
                    ["2026-10-14", "1280.0", "1286.0", "1290.0", "1275.0", "50000.0"],
                    ["2026-10-15", "1286.0", "1312.97", "1317.76", "1282.0", "57677.0"],
                ]
            }
        }
    }
    bars = _parse_kline_payload(
        payload, "sh600519", dt.date(2026, 10, 14), dt.date(2026, 10, 15), "qfq"
    )
    assert len(bars) == 2
    assert bars[-1].close == 1312.97
    assert bars[-1].adjust == "qfq"
    assert bars[-1].code == "600519"

    payload_raw = {
        "data": {
            "sh000001": {"day": [["2026-10-15", "3710.7", "3707.48", "3724.56", "3674.82", "510163189"]]}
        }
    }
    bars2 = _parse_kline_payload(
        payload_raw, "sh000001", dt.date(2026, 10, 1), dt.date(2026, 10, 15), "raw"
    )
    assert bars2 and bars2[0].close == 3707.48


# ---------------------------------------------------------------- 东财扩展解析


def test_em_ext_parsers():
    flow = _parse_flow_klines(
        [
            "2026-10-13,300000000.0,-1,-2,-3,100000000.0,1,1,1,1,1",
            "2026-10-15,552032128.0,-487901.0,-551544208.0,427641424.0,124390704.0,7.32,-0.01,-7.32,5.67,1.65",
        ]
    )
    assert flow is not None
    assert flow["当日主力净额(亿)"] == 5.52
    assert flow["5日主力净额(亿)"] == 8.52

    margin = _parse_margin_rows(
        [
            {"DATE": "2026-10-14 00:00:00", "RZYE": 16889178036, "RZYEZB": 1.05},
            {"DATE": "2026-10-13 00:00:00", "RZYE": 16759178036, "RZYEZB": 1.04},
        ]
    )
    assert margin["融资余额(亿)"] == 168.89
    assert margin["较前日(亿)"] == 1.3
    assert margin["占流通市值%"] == 1.05

    anns = _parse_announcements(
        [
            {"title": "关于股东减持计划的公告", "notice_date": "2026-10-10 00:00:00",
             "columns": [{"column_name": "其他"}]},
            {"title": "日常经营公告", "notice_date": "2026-10-09 00:00:00", "columns": []},
        ]
    )
    assert anns[0].startswith("10-10")
    assert "【重要】" in anns[0]  # 减持
    assert "【重要】" not in anns[1]


def test_em_ext_fundamentals_and_forecast_parsers():
    data = _parse_fundamentals(
        {
            "REPORTDATE": "2026-06-30 00:00:00",
            "UPDATE_DATE": "2026-08-28 00:00:00",
            "BASIC_EPS": 1.88,
            "TOTAL_OPERATE_INCOME": 110177000000,
            "YSTZ": -0.2544,
            "PARENT_NETPROFIT": 41131000000,
            "SJLTZ": -4.66,
            "WEIGHTAVG_ROE": 4.79,
            "XSMLL": None,
        }
    )
    assert data["报告期"] == "2026年中报"
    assert data["公告日期"] == "2026-08-28"
    assert data["营收(亿)"] == 1101.77
    assert data["净利同比%"] == -4.66
    assert "毛利率%" not in data  # 银行无毛利率

    assert (
        _parse_forecast({"NOTICE_DATE": "2008-07-10 00:00:00", "REPORT_DATE": "2008-06-30 00:00:00"})
        is None
    )  # 过期预告不返回
    fresh = _parse_forecast(
        {
            "NOTICE_DATE": "2026-10-01 00:00:00",
            "REPORT_DATE": "2026-09-30 00:00:00",
            "PREDICT_TYPE": "预增",
            "ADD_AMP_LOWER": 50,
            "ADD_AMP_UPPER": 80,
            "PREDICT_CONTENT": "预计前三季度净利润同比增长 50%~80%",
        }
    )
    assert fresh is not None
    assert fresh["预告类型"] == "预增"
    assert "50~80" in fresh["变动幅度%"]


def test_valuation_and_industry_from_snapshot(fresh_env):
    from invest_assistant.db.session import ensure_schema, get_session_factory

    ensure_schema()
    sf = get_session_factory()
    cache_dir = get_settings().data_dir / "snapshot"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"code": "600519", "name": "贵州茅台", "pct": -1.5, "industry": "酿酒行业",
         "pe": 21.0, "pb": 7.5, "turnover": 0.3, "market_cap": 15900.0, "amount": 4.4e9},
        {"code": "000858", "name": "五粮液", "pct": 0.5, "industry": "酿酒行业",
         "pe": 18.0, "pb": 4.0, "turnover": 0.8, "market_cap": 6000.0, "amount": 2.0e9},
        {"code": "601398", "name": "工商银行", "pct": 0.2, "industry": "银行",
         "pe": 5.0, "pb": 0.6, "turnover": 0.2, "market_cap": 21000.0, "amount": 2.0e9},
    ]
    (cache_dir / f"{dt.date.today():%Y%m%d}.json").write_text(
        json.dumps({"rows": rows}), encoding="utf-8"
    )
    service = MarketDataService(session_factory=sf)

    valuation = service.get_valuation("600519")
    assert valuation["PE"] == 21.0
    assert valuation["PB"] == 7.5
    assert valuation["总市值(亿)"] == 15900

    industry = service.get_industry_snapshot("600519")
    assert industry["行业"] == "酿酒行业"
    assert industry["当日行业均涨跌%"] == -0.5
    assert industry["行业上涨/下跌家数"] == "1/1"
    assert industry["近5个快照日累计%"] == -0.5  # 首日累计 = 当日
    assert (get_settings().data_dir / "industry_history.json").exists()

    assert not service.is_stock_code("510300")
    from invest_assistant.db.models import Instrument

    with sf() as session:
        session.add(Instrument(code="011186", name="测试基金", type="otc_fund", exchange="fund"))
        session.commit()
    assert not service.is_stock_code("011186")  # 已登记为场外基金
    assert service.is_stock_code("600519")


# ---------------------------------------------------------------- 服务层回退链


class _FailingEM:
    def get_quotes(self, codes):
        raise MarketDataError("em down")

    def get_daily_bars(self, *args, **kwargs):
        raise MarketDataError("em down")


class _FailingSina:
    def __init__(self):
        self.calls = 0

    def get_quotes(self, codes):
        self.calls += 1
        raise MarketDataError("sina down")

    def get_daily_bars(self, *args, **kwargs):
        self.calls += 1
        raise MarketDataError("sina down")


class _WorkingTencent:
    def get_quotes(self, codes):
        return {
            "600519": Quote(
                code="600519", name="茅台", price=10.0, prev_close=9.8, open=9.9,
                high=10.1, low=9.8, pct_change=2.04, volume=0, amount=0,
                ts=dt.datetime.now(), source="tencent",
            )
        }

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [
            Bar(code="600519", date=dt.date(2026, 10, 15), open=1.0, close=2.0,
                high=3.0, low=1.0, volume=1.0, amount=0.0, adjust="qfq")
        ]


def test_quotes_and_bars_fallback_to_tencent(sf):
    sina = _FailingSina()
    service = MarketDataService(
        em=_FailingEM(), sina=sina, tencent=_WorkingTencent(), session_factory=sf
    )
    quotes = service.get_quotes(["600519"])
    assert quotes["600519"].source == "tencent"
    bars = service.get_daily_bars("600519", dt.date(2026, 8, 1), dt.date(2026, 10, 15))
    assert bars and bars[-1].close == 2.0
    assert sina.calls == 1  # 日线路径没有落到新浪（腾讯已命中）


# ---------------------------------------------------------------- 大盘环境


class _EnvTencent:
    def get_quotes(self, codes):
        out = {}
        for symbol in codes:
            out[symbol[2:]] = Quote(
                code=symbol[2:], name=symbol, price=100.0, prev_close=99.0, open=99.0,
                high=101.0, low=98.0, pct_change=1.01, volume=0, amount=5e10,
                ts=dt.datetime.now(), source="tencent",
            )
        return out

    def get_daily_bars(self, code, start, end, adjust="raw"):
        return [
            Bar(code="000001", date=start + dt.timedelta(days=i), open=1.0,
                close=100.0 + (i % 5), high=1.0, low=1.0, volume=0.0, amount=0.0,
                adjust="raw")
            for i in range(30)
        ]


def test_market_env_with_breadth(fresh_env):
    from invest_assistant.db.session import get_session_factory

    sf = get_session_factory()
    cache_dir = get_settings().data_dir / "snapshot"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{dt.date.today():%Y%m%d}.json").write_text(
        json.dumps(
            {"rows": [{"code": "1", "pct": 3.0}, {"code": "2", "pct": -1.0}, {"code": "3", "pct": 10.0}]}
        ),
        encoding="utf-8",
    )
    service = MarketDataService(tencent=_EnvTencent(), session_factory=sf)
    env = service.get_market_env()
    assert "上证指数" in env["指数"]
    assert env["指数"]["上证指数"]["相对20日线%"] is not None
    assert env["情绪"]["上涨家数"] == 2
    assert env["情绪"]["涨停约"] == 1
    assert env["两市成交额(亿)"] > 0
    # TTL 缓存：二次调用不重取（相同对象值）
    assert service.get_market_env() == env


def test_market_regime_state_dual_index_hysteresis(fresh_env):
    """加强判定：双指数（上证+创业板）+ ±1% 滞回带；状态持久化，均线附近不来回翻转。"""
    from invest_assistant.db.session import ensure_schema, get_session_factory

    ensure_schema()
    service = MarketDataService(tencent=_EnvTencent(), session_factory=get_session_factory())

    def env_with(sh_rel, cyb_rel):
        return {
            "指数": {
                "上证指数": {"相对20日线%": sh_rel},
                "创业板指": {"相对20日线%": cyb_rel},
            }
        }

    seq = [
        env_with(0.5, 0.3),    # 初始化：双双在线上 → strong
        env_with(-0.5, -0.6),  # 双双小幅跌破（带内）→ 维持 strong（不翻转）
        env_with(-1.5, -0.2),  # 上证明显破位 → weak
        env_with(0.8, 0.9),    # 双双回到带内但未超 +1 → 维持 weak
        env_with(1.2, 1.1),    # 双双明显修复 → strong
    ]
    it = iter(seq)
    service.get_market_env = lambda **kw: next(it)
    assert service.market_regime_state() == "strong"
    assert service.market_regime_state() == "strong"
    assert service.market_regime_state() == "weak"
    assert service.market_regime_state() == "weak"
    assert service.market_regime_state() == "strong"


# ---------------------------------------------------------------- 提示词注入


def test_eod_user_includes_env_and_lessons():
    text = eod_user([{"代码": "600519"}], lessons=["L1"], market_env={"指数": {"上证指数": {}}})
    assert "大盘环境" in text
    assert "历史交易复盘经验" in text


def test_analyzer_injects_extras(sf):
    class FakeData:
        def get_daily_bars(self, code, start, end, adjust="qfq"):
            return [
                Bar(code=code, date=dt.date(2026, 9, 1) + dt.timedelta(days=i),
                    open=10.0, close=10.0 + i * 0.1, high=10.5, low=9.8,
                    volume=1000.0 + i, amount=0.0, adjust="qfq")
                for i in range(25)
            ]

        def get_fund_nav(self, code, days=60):
            return []

        def get_market_env(self):
            return {"指数": {"上证指数": {"涨跌%": -1.5, "相对20日线%": -2.1}}}

        def get_stock_flow(self, code):
            return {"当日主力净额(亿)": -3.2}

        def get_margin(self, code):
            return {"融资余额(亿)": 100.0}

        def get_fundamentals(self, code):
            return {"报告期": "2026年中报", "净利同比%": -4.66}

        def get_profit_forecast(self, code):
            return {"预告类型": "预增", "变动幅度%": "50~80"}

        def get_industry_snapshot(self, code):
            return {"行业": "银行", "当日行业均涨跌%": -0.5}

        def get_valuation(self, code):
            return {"PE": 5.1, "PB": 0.6}

        def get_announcements(self, code):
            return ["10-10 【重要】股东减持计划公告"]

    class FakeLLM:
        configured = True

        def __init__(self):
            self.last_user = ""

        def complete_json(self, **kwargs):
            self.last_user = str(kwargs.get("user", ""))
            return LLMResult(
                content={"summary": "x", "action_bias": "等待回踩", "confidence": 0.5,
                         "plan": {"entry_zone": "10~11"}},
                raw="", model="fake", tokens_in=1, tokens_out=1, cached_in=0, ms=1,
            )

    llm = FakeLLM()
    analyzer = CardAnalyzer(
        llm=llm, data=FakeData(), news_fetcher=lambda code: [], session_factory=sf
    )
    card = analyzer.analyze("600519")
    assert "大盘环境" in llm.last_user
    assert "资金面" in llm.last_user
    assert "融资融券" in llm.last_user
    assert "公告" in llm.last_user
    assert "【重要】" in llm.last_user
    assert "基本面" in llm.last_user
    assert "业绩预告" in llm.last_user
    assert "行业" in llm.last_user
    assert "估值" in llm.last_user
    assert card["content"]["summary"] == "x"
