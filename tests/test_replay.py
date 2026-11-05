"""历史回放（机械定式 playbook）测试：入场/确认/止损/止盈/超时/经验落库。"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.datasource.base import Bar
from invest_assistant.db.models import Base, TradingLesson
from invest_assistant.db.session import make_engine
from invest_assistant.domain.journal import lessons_text
from invest_assistant.engine.playbook_replay import run_playbook_replay

START = dt.date(2026, 8, 1)
NOW = dt.datetime(2026, 10, 16, 12, 0, 0)


@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'replay.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakeData:
    def __init__(self, bars_by_code, index_bars=None):
        self.bars_by_code = bars_by_code
        self.tencent = SimpleNamespace(
            get_daily_bars=lambda code, start, end, adjust="raw": [
                b for b in (index_bars or []) if start <= b.date <= end
            ]
        )

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        return [b for b in self.bars_by_code.get(code, []) if start <= b.date <= end]


def _flat(count, start, close=10.0, high=10.05, low=9.95, volume=10000.0):
    return [
        Bar(code="600519", date=start + dt.timedelta(days=i), open=close, high=high,
            low=low, close=close, volume=volume, amount=0.0, adjust="qfq")
        for i in range(count)
    ]


def _scenario_bars(signal_volume=5000.0, signal_close=10.10, exit_low=10.10, exit_high=10.35):
    """预热 65 根 + 信号日（缩量阳线、收复前日高点、贴近均线）+ 入场日 + 出场日 + 尾段。"""
    bars = _flat(65, START)
    bars.append(
        Bar(code="600519", date=START + dt.timedelta(days=65), open=9.95,
            high=max(10.12, signal_close + 0.02), low=9.90, close=signal_close,
            volume=signal_volume, amount=0.0, adjust="qfq")
    )
    bars.append(
        Bar(code="600519", date=START + dt.timedelta(days=66), open=10.10, high=10.20,
            low=10.00, close=10.15, volume=12000.0, amount=0.0, adjust="qfq")
    )
    bars.append(
        Bar(code="600519", date=START + dt.timedelta(days=67), open=10.20, high=exit_high,
            low=exit_low, close=10.20, volume=12000.0, amount=0.0, adjust="qfq")
    )
    bars.extend(_flat(10, START + dt.timedelta(days=68), close=10.2, high=10.15, low=10.1, volume=15000.0))
    return bars


def _index_bars():
    return [
        Bar(code="sh000001", date=START + dt.timedelta(days=i), open=100.0, high=100.0,
            low=100.0, close=100.0, volume=0.0, amount=0.0, adjust="raw")
        for i in range(80)
    ]


def test_replay_pullback_target_exit_and_lesson(sf):
    data = FakeData({"600519": _scenario_bars()}, index_bars=_index_bars())
    report = run_playbook_replay(
        session_factory=sf, data=data, codes=["600519"], now=NOW
    )
    assert report["baseline"]["trades"] == 1
    stat = report["baseline"]["stats"]["回踩支撑"]
    assert stat["n"] == 1 and stat["target_rate"] == 100 and stat["win_rate"] == 100
    assert stat["avg_ret"] > 0
    # 经验版（v2）在同窗同样成交（指数≥20日线、站上MA5、放宽止损未触及 → 目标离场）
    assert report["v2"]["trades"] == 1
    assert report["v2"]["stats"]["回踩支撑"]["win_rate"] == 100
    # v6（双指数+滞回判定）：指数平稳视为强市 → 同窗同样成交
    assert report["v6"]["trades"] == 1
    assert report["lesson_saved"] is True
    assert any(
        "经验版对比" in line
        for line in report[report["deployed"]]["findings"]
    )
    with sf() as session:
        lesson = session.scalars(select(TradingLesson)).one()
        assert lesson.source == "replay"
        assert "经验版" in lesson.name
        assert lesson.summary.startswith("历史模拟（回放 ")  # 压缩成一行
        assert len(lesson.summary) < 280
        assert lesson.lessons_json  # 完整明细仍保留（复盘页展示）
    text = lessons_text(sf)
    replay_lines = [line for line in text if line.startswith("[历史模拟]")]
    assert len(replay_lines) == 1  # 只注入一行
    assert replay_lines[0].startswith("[历史模拟] 历史模拟（回放 ")

    # 新回放替换旧版本：再次运行后 replay 经验仍只有一条
    data2 = FakeData({"600519": _scenario_bars()}, index_bars=_index_bars())
    report2 = run_playbook_replay(
        session_factory=sf, data=data2, codes=["600519"], now=NOW + dt.timedelta(hours=2)
    )
    assert report2["lesson_saved"] is True
    with sf() as session:
        assert len(session.scalars(select(TradingLesson)).all()) == 1


def test_replay_stop_exit(sf):
    data = FakeData({"600519": _scenario_bars(exit_low=9.5)}, index_bars=_index_bars())
    report = run_playbook_replay(session_factory=sf, data=data, codes=["600519"], now=NOW)
    assert report["baseline"]["trades"] == 1
    stat = report["baseline"]["stats"]["回踩支撑"]
    assert stat["stop_rate"] == 100 and stat["win_rate"] == 0
    assert stat["avg_ret"] < 0
    # 经验版放宽止损到 -5%：9.5 > 9.405 未被扫损 → 反而命中目标（演示经验的作用）
    v2_stat = report["v2"]["stats"]["回踩支撑"]
    assert v2_stat["stop_rate"] == 0 and v2_stat["target_rate"] == 100
    # v3：回踩保持 -2% 止损（经验：放宽止损对回踩不利）→ 与基线一致被止损
    v3_stat = report["v3"]["stats"]["回踩支撑"]
    assert v3_stat["stop_rate"] == 100


def test_replay_no_signal_when_confirmation_fails(sf):
    # 信号日放量且未收复前日高点、也未突破 → 无入场
    data = FakeData(
        {"600519": _scenario_bars(signal_volume=15000.0, signal_close=10.0)},
        index_bars=_index_bars(),
    )
    report = run_playbook_replay(session_factory=sf, data=data, codes=["600519"], now=NOW)
    assert report["baseline"]["trades"] == 0
    assert report["v2"]["trades"] == 0
    assert report["lesson_saved"] is False
    with sf() as session:
        assert session.scalars(select(TradingLesson)).all() == []


def test_replay_timeout_exit(sf):
    # 出场日既不触止损也不触目标 → 超时（20 日后收盘）平仓
    data = FakeData(
        {"600519": _scenario_bars(exit_low=10.05, exit_high=10.15)}, index_bars=_index_bars()
    )
    report = run_playbook_replay(session_factory=sf, data=data, codes=["600519"], now=NOW)
    assert report["baseline"]["trades"] == 1
    stat = report["baseline"]["stats"]["回踩支撑"]
    assert stat["stop_rate"] == 0 and stat["target_rate"] == 0
    v2_stat = report["v2"]["stats"]["回踩支撑"]
    assert v2_stat["stop_rate"] == 0 and v2_stat["target_rate"] == 0


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


def test_sample_codes_filters_and_excludes(fresh_env):
    """扩展样本：排除自选、ST/新股与 <50 亿市值，数量符合请求且可复现。"""
    import json

    from invest_assistant.config import get_settings
    from invest_assistant.db.models import WatchlistItem
    from invest_assistant.db.session import ensure_schema, get_session_factory
    from invest_assistant.engine.playbook_replay import _sample_codes

    ensure_schema()
    sf = get_session_factory()
    cache_dir = get_settings().data_dir / "snapshot"
    cache_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"code": "600001", "name": "平稳股份", "market_cap": 200.0},
        {"code": "600002", "name": "ST风险", "market_cap": 100.0},
        {"code": "600003", "name": "退市边缘", "market_cap": 100.0},
        {"code": "600004", "name": "N新股", "market_cap": 100.0},
        {"code": "600005", "name": "微盘股", "market_cap": 20.0},
        {"code": "600006", "name": "自选股", "market_cap": 300.0},
        {"code": "600007", "name": "候选甲", "market_cap": 80.0},
        {"code": "600008", "name": "候选乙", "market_cap": 60.0},
    ]
    (cache_dir / "20261016.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
    with sf() as session:
        session.add(WatchlistItem(code="600006"))
        session.commit()
    picked = _sample_codes(sf, size=10)
    assert set(picked) == {"600001", "600007", "600008"}
    assert _sample_codes(sf, size=2, seed=42) == picked[:2]


def test_enhanced_regime_map_hysteresis():
    """加强判定：双指数+滞回带——明显破位才转弱、双双明显修复才转强，减少均线附近翻转。"""
    from invest_assistant.engine.playbook_replay import _enhanced_regime_map

    days = [START + dt.timedelta(days=i) for i in range(30)]
    closes = [100.0] * 20 + [98.0] * 3 + [100.5] * 4 + [102.0] * 3
    series = list(zip(days, closes, strict=True))
    m = _enhanced_regime_map(series, [], band_pct=1.0)
    assert m[days[19]] is True    # 初始：站上 MA20
    assert m[days[20]] is False   # 98 明显低于 MA20（约 -1.9% < -1%）→ 转弱
    assert m[days[25]] is False   # 回到 100.5（带内约 +0.7%）→ 维持弱（不翻转）
    assert m[days[29]] is True    # 102（约 +2%，超带）→ 转强

    # 双指数：任一明显破位即转弱；两者都明显修复才恢复
    sh = list(zip(days, [100.0] * 30, strict=True))
    cyber = list(zip(days, [100.0] * 20 + [97.0] * 3 + [101.0] * 7, strict=True))
    m2 = _enhanced_regime_map(sh, cyber, band_pct=1.0)
    assert m2[days[19]] is True
    assert m2[days[21]] is False  # 创业板明显破位（约 -2.7%）→ 转弱
    assert m2[days[29]] is False  # 创业板已修复（约 +1.4%）但上证仅在线上（0%）→ 未双双超带 → 维持弱


def test_replay_option_breakeven_dynamic_stop():
    """实验开关验证：保本止损（浮盈 +2% 后移到成本）出场记为「移动止损」。"""
    from invest_assistant.engine.playbook_replay import V7_RULES, _replay_code

    bars = _flat(80, START)
    bars[60] = Bar(code="600519", date=START + dt.timedelta(days=60), open=9.9, high=10.12,
                   low=9.88, close=10.10, volume=5000.0, amount=0.0, adjust="qfq")
    bars[61] = Bar(code="600519", date=START + dt.timedelta(days=61), open=10.00, high=10.30,
                   low=9.99, close=10.25, volume=12000.0, amount=0.0, adjust="qfq")
    bars[62] = Bar(code="600519", date=START + dt.timedelta(days=62), open=9.95, high=10.00,
                   low=9.90, close=9.92, volume=10000.0, amount=0.0, adjust="qfq")
    trades = _replay_code("600519", bars, [], 0.0, V7_RULES, 0.98, 0.95, opts={"breakeven": 2.0})
    assert len(trades) == 1
    assert trades[0].exit_reason == "移动止损"  # 保本止损触发（非原始止损）
    assert -2.0 < trades[0].ret_pct < 0.5


def test_replay_option_confirm_day_entry():
    """B5 开关：二次确认日——信号次日收盘站上信号日高点后，再次日开盘进场。"""
    from invest_assistant.engine.playbook_replay import V7_RULES, _replay_code

    bars = _scenario_bars()  # 信号日 i=65；i=66 收盘 10.15 > 信号日高 10.12 → 确认；i=67 开盘进场
    trades = _replay_code("600519", bars, [], 0.0, V7_RULES, 0.98, 0.95, opts={"confirm_days": 3})
    assert len(trades) == 1
    assert trades[0].entry_date == bars[67].date.isoformat()
    assert abs(trades[0].entry_price - round(10.20 * 1.001, 4)) < 1e-6


def test_replay_option_partial_profit():
    """A6 开关：+8% 先卖一半、剩余继续持有到目标 → 收益按半仓混合计算。"""
    from invest_assistant.engine.playbook_replay import V7_RULES, _replay_code

    bars = _scenario_bars(exit_high=11.5)
    base = _replay_code("600519", bars, [], 0.0, V7_RULES, 0.98, 0.95)
    part = _replay_code(
        "600519", bars, [], 0.0, V7_RULES, 0.98, 0.95,
        opts={"partial_at": 0.08, "partial_frac": 0.5},
    )
    assert len(base) == len(part) == 1
    assert part[0].ret_pct > base[0].ret_pct  # 高位先落袋一半 → 混合收益更高
    assert part[0].exit_reason == "目标"
