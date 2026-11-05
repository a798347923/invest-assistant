"""组合级资金曲线模拟测试：整手/费用/槽位/空仓。"""

from __future__ import annotations

import datetime as dt

from invest_assistant.backtest.fees import Fees
from invest_assistant.datasource.base import Bar
from invest_assistant.engine.playbook_replay import ReplayTrade
from invest_assistant.engine.portfolio_sim import PortfolioParams, simulate_portfolio


def _bars(code: str, start: dt.date, days: int = 12, close: float = 10.0) -> list[Bar]:
    return [
        Bar(
            code=code,
            date=start + dt.timedelta(days=i),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=0.0,
            amount=0.0,
            adjust="qfq",
        )
        for i in range(days)
    ]


def _trade(
    code: str,
    entry: dt.date,
    exit_: dt.date,
    entry_price: float = 10.0,
    exit_price: float = 11.0,
    setup: str = "突破",
    reason: str = "目标",
) -> ReplayTrade:
    return ReplayTrade(
        code=code,
        setup=setup,
        regime="双指数强市",
        entry_date=entry.isoformat(),
        entry_price=entry_price,
        exit_date=exit_.isoformat(),
        exit_price=exit_price,
        exit_reason=reason,
        ret_pct=round((exit_price / entry_price - 1) * 100, 2),
        holding_days=(exit_ - entry).days,
        mfe_pct=0.0,
        mae_pct=0.0,
    )


def test_portfolio_sim_single_trade_fees():
    """整手 + 双边费用：2 万仓位、10→11 元，账户收益应按 20% 仓位折算。"""
    start = dt.date(2026, 1, 5)
    bars = {"600519": _bars("600519", start)}
    trade = _trade("600519", start, start + dt.timedelta(days=5))
    result = simulate_portfolio(
        [trade], bars,
        PortfolioParams(initial_cash=100_000, slots=5, position_fraction=0.2),
        Fees(),
    )
    stats = result["stats"]
    assert result["executed"] == 1
    assert stats["final"] == 101_978.5  # 100000 − 20000 − 5 + 22000 − 16.5
    assert stats["win_rate_pct"] == 100.0
    assert result["avg_positions"] > 0


def test_portfolio_sim_slot_limit():
    """同一天 3 个信号、只有 2 个槽位 → 成交 2 笔、跳过 1 笔（槽位）。"""
    start = dt.date(2026, 1, 5)
    codes = ["000001", "000002", "000003"]
    bars = {c: _bars(c, start) for c in codes}
    trades = [_trade(c, start, start + dt.timedelta(days=3), exit_price=10.5) for c in codes]
    result = simulate_portfolio(
        trades, bars,
        PortfolioParams(initial_cash=100_000, slots=2, position_fraction=0.2),
        Fees(),
    )
    assert result["executed"] == 2
    assert result["skipped_slots"] == 1


def test_portfolio_sim_no_trades_flat():
    """无成交 → 权益保持初始资金、收益 0。"""
    start = dt.date(2026, 1, 5)
    bars = {"600519": _bars("600519", start)}
    result = simulate_portfolio([], bars, PortfolioParams(initial_cash=50_000), Fees())
    assert result["executed"] == 0
    assert result["stats"]["total_return_pct"] == 0.0
    assert result["stats"]["final"] == 50_000.0


def test_portfolio_sim_same_day_round_trip():
    """当日进出（回放允许）：立即结算、不得卡仓。"""
    start = dt.date(2026, 1, 5)
    bars = {"600519": _bars("600519", start)}
    trade = _trade("600519", start, start, exit_price=10.5)
    result = simulate_portfolio(
        [trade], bars,
        PortfolioParams(initial_cash=100_000, slots=5, position_fraction=0.2),
        Fees(),
    )
    assert result["executed"] == 1
    assert result["avg_positions"] == 0.0
    assert result["stats"]["final"] == 100_979.25  # 买 20000+5；卖 21000−15.75


def test_portfolio_sim_rank_by_momentum():
    """同日多信号、槽位不足时，按动量强度取更强的一只。"""
    start = dt.date(2026, 1, 5)
    codes = ["000001", "000002"]
    bars = {c: _bars(c, start) for c in codes}
    exit_ = start + dt.timedelta(days=3)
    trades = [
        _trade("000001", start, exit_, exit_price=10.5),
        _trade("000002", start, exit_, exit_price=10.5),
    ]
    features = {
        ("000001", start.isoformat()): {"mom": 0.02, "vol": 1.0, "rs": 0.0},
        ("000002", start.isoformat()): {"mom": 0.10, "vol": 1.0, "rs": 0.0},
    }
    result = simulate_portfolio(
        trades, bars,
        PortfolioParams(initial_cash=100_000, slots=1, position_fraction=0.2),
        Fees(), rank_mode="momentum", features=features,
    )
    assert result["executed"] == 1
    assert result["trades"][0]["code"] == "000002"  # 动量更强
    assert result["skipped_slots"] == 1


def test_portfolio_sim_drawdown_breaker():
    """组合回撤超过阈值后暂停新开仓（已有仓位照常退出）。"""
    start = dt.date(2026, 1, 5)
    bars = {"600519": _bars("600519", start, days=15)}
    t1 = _trade("600519", start, start + dt.timedelta(days=2), entry_price=10.0, exit_price=8.0, reason="止损")
    t2 = _trade("600519", start + dt.timedelta(days=3), start + dt.timedelta(days=6), entry_price=8.0, exit_price=9.0)
    result = simulate_portfolio(
        [t1, t2], bars,
        PortfolioParams(initial_cash=10_000, slots=1, position_fraction=1.0),
        Fees(), drawdown_pause_pct=10.0, drawdown_resume_pct=5.0,
    )
    assert result["executed"] == 1  # 第一笔亏损触发熔断
    assert result["skipped_breaker"] == 1  # 第二笔被暂停拦截
    assert result["stats"]["max_drawdown_pct"] < -10


def test_portfolio_sim_weighted_sizing_by_momentum():
    """信号强度加权：同日两个信号，动量强的仓位更大。"""
    start = dt.date(2026, 1, 5)
    codes = ["000001", "000002"]
    bars = {c: _bars(c, start, days=15) for c in codes}
    exit_ = start + dt.timedelta(days=5)
    trades = [_trade(c, start, exit_, exit_price=10.5) for c in codes]
    features = {
        ("000001", start.isoformat()): {"mom": 0.02},
        ("000002", start.isoformat()): {"mom": 0.10},
    }
    result = simulate_portfolio(
        trades, bars,
        PortfolioParams(initial_cash=100_000, slots=5, position_fraction=0.2),
        Fees(), weight_mode="momentum", features=features,
    )
    by_code = {t["code"]: t["qty"] for t in result["trades"]}
    assert by_code["000002"] > by_code["000001"]
