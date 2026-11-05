"""B3-03/04/05 回测引擎测试（构造行情，无网络）。"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.backtest import Fees, compute_stats, run_backtest
from invest_assistant.backtest.engine import DEFAULT_SIGNAL, _build_signals
from invest_assistant.backtest.runner import execute_backtest
from invest_assistant.datasource import Bar
from invest_assistant.db.models import BacktestRun, Base
from invest_assistant.db.session import make_engine

START = dt.date(2026, 9, 1)


def bars_from_closes(code: str, closes: list[float], start: dt.date = START) -> list[Bar]:
    bars = []
    prev = closes[0]
    for i, close in enumerate(closes):
        day = start + dt.timedelta(days=i)
        open_ = prev
        bars.append(
            Bar(code=code, date=day, open=open_, high=max(open_, close) * 1.001,
                low=min(open_, close) * 0.999, close=close, volume=1000)
        )
        prev = close
    return bars


FLAT_20 = [100.0] * 20
PARAMS = {
    "start": START.isoformat(),
    "end": (START + dt.timedelta(days=40)).isoformat(),
    "slots": 5,
    "initial_cash": 200_000,
}


# ---------------------------------------------------------------- 费用

def test_fees():
    fees = Fees()
    assert fees.buy_fee(10_000) == 5.0  # 低于最低佣金
    assert fees.buy_fee(1_000_000) == 250.0  # 万 2.5
    assert fees.sell_fee(1_000_000) == 250.0 + 500.0  # 佣金 + 印花税
    custom = Fees.from_config({"commission_rate": 0.0003, "commission_min": 0, "stamp_tax_sell": 0.001})
    assert custom.buy_fee(10_000) == pytest.approx(3.0)


# ---------------------------------------------------------------- 统计

def test_stats():
    equity = [
        (dt.date(2026, 1, 1), 100_000.0),
        (dt.date(2026, 2, 1), 110_000.0),
        (dt.date(2026, 3, 1), 99_000.0),
        (dt.date(2026, 4, 1), 120_000.0),
    ]
    trades = [
        {"entry_date": "2026-01-02", "exit_date": "2026-01-12", "pnl": 100.0},
        {"entry_date": "2026-02-02", "exit_date": "2026-02-08", "pnl": -50.0},
        {"entry_date": "2026-03-02", "exit_date": "2026-03-22", "pnl": 20.0},
    ]
    stats = compute_stats(equity, trades)
    assert stats["total_return_pct"] == 20.0
    assert stats["max_drawdown_pct"] == -10.0
    assert stats["win_rate_pct"] == 66.67
    assert stats["avg_holding_days"] == 12.0  # (10 + 6 + 20) / 3
    assert stats["profit_factor"] == 2.4  # 120 / 50


# ---------------------------------------------------------------- 信号

def test_build_signals_no_future_and_threshold():
    short = bars_from_closes("600519", [100.0] * 10)
    assert _build_signals(short, DEFAULT_SIGNAL) == {}  # 历史不足

    flat = bars_from_closes("600519", [100.0] * 25)
    assert _build_signals(flat, DEFAULT_SIGNAL) == {}  # 平盘：不满足涨幅阈值

    rising = bars_from_closes("600519", FLAT_20 + [104.0])
    signals = _build_signals(rising, DEFAULT_SIGNAL)
    assert list(signals) == [rising[-1].date]  # 仅触发当日出现信号


# ---------------------------------------------------------------- 引擎

def test_engine_take_profit_trade():
    closes = FLAT_20 + [104.0, 106.0, 110.0, 122.0, 130.0]
    bars = {"600519": bars_from_closes("600519", closes)}
    result = run_backtest(PARAMS, bars)
    assert "error" not in result
    assert result["trades_count"] == 1
    trade = result["trades"][0]
    assert "止盈" in trade["reason"]
    assert trade["pnl"] > 0
    assert trade["qty"] == 300  # 等权 4 万 / 104 元 ≈ 384 股 → 300 股整手
    assert result["stats"]["trades"] == 1
    assert result["stats"]["win_rate_pct"] == 100.0


def test_engine_stop_loss_priority():
    closes = FLAT_20 + [104.0, 106.0, 90.0, 90.0, 91.0]
    bars = {"600519": bars_from_closes("600519", closes)}
    bars["600519"][22] = Bar(  # 买入后第 2 天：低开低走触发止损（同日 trailing 也会命中，止损优先）
        code="600519", date=bars["600519"][22].date, open=106.0, high=106.1,
        low=85.0, close=90.0, volume=1000,
    )
    result = run_backtest(PARAMS, bars)
    assert result["trades_count"] == 1
    trade = result["trades"][0]
    assert "止损" in trade["reason"]
    assert trade["pnl"] < 0


def test_engine_slots_and_no_entry_without_signal():
    bars = {"600519": bars_from_closes("600519", [100.0] * 30)}
    result = run_backtest(PARAMS, bars)
    assert result["trades_count"] == 0
    assert result["stats"]["total_return_pct"] == 0.0


# ---------------------------------------------------------------- 执行器

class FakeData:
    def __init__(self, bars_map=None, fail=False):
        self._bars = bars_map or {}
        self.fail = fail

    def get_daily_bars(self, code, start, end, adjust="qfq"):
        if self.fail:
            raise RuntimeError("no data")
        return [b for b in self._bars.get(code, []) if start <= b.date <= end]


def _make_env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'backtest.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_execute_backtest_success(tmp_path):
    sf = _make_env(tmp_path)
    with sf() as session:
        run = BacktestRun(
            params_json={**PARAMS, "codes": ["600519"]},
            status="pending",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    bars_map = {
        "600519": bars_from_closes("600519", FLAT_20 + [104.0, 106.0, 110.0, 122.0, 130.0]),
        "000300": bars_from_closes("000300", [4000.0 + i for i in range(25)]),
    }
    execute_backtest(run_id, session_factory=sf, data=FakeData(bars_map))
    with sf() as session:
        row = session.get(BacktestRun, run_id)
        assert row.status == "done"
        assert row.progress == 100.0
        assert row.result_json["stats"]["trades"] == 1
        assert row.result_json["benchmark"]  # 基准曲线存在
        assert row.params_json["universe_codes"] == 1


def test_execute_backtest_error_path(tmp_path):
    sf = _make_env(tmp_path)
    with sf() as session:
        run = BacktestRun(params_json={**PARAMS, "codes": ["999999"]}, status="pending")
        session.add(run)
        session.commit()
        run_id = run.id

    execute_backtest(run_id, session_factory=sf, data=FakeData(fail=True))
    with sf() as session:
        row = session.get(BacktestRun, run_id)
        assert row.status == "error"
        assert "区间内无可用行情数据" in row.error
