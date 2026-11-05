"""回测统计：收益/回撤/胜率/持有期 + 基准对比。"""

from __future__ import annotations

import datetime as dt


def compute_stats(
    equity: list[tuple[dt.date, float]],
    trades: list[dict],
    benchmark: list[tuple[dt.date, float]] | None = None,
    initial_cash: float | None = None,
) -> dict:
    if not equity:
        return {}
    initial = initial_cash or equity[0][1]
    final = equity[-1][1]
    days = max((equity[-1][0] - equity[0][0]).days, 1)

    total_return = final / initial - 1 if initial else 0.0
    annualized = (final / initial) ** (365 / days) - 1 if initial > 0 and final > 0 else -1.0

    peak = equity[0][1]
    max_drawdown = 0.0
    max_drawdown_date = equity[0][0]
    for day, value in equity:
        peak = max(peak, value)
        drawdown = value / peak - 1 if peak else 0.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = day

    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_hold = (
        sum((dt.date.fromisoformat(t["exit_date"]) - dt.date.fromisoformat(t["entry_date"])).days
            for t in trades) / len(trades)
        if trades
        else 0.0
    )
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    benchmark_return = None
    if benchmark:
        base = benchmark[0][1] or 1
        benchmark_return = benchmark[-1][1] / base - 1

    return {
        "initial": round(initial, 2),
        "final": round(final, 2),
        "total_return_pct": round(total_return * 100, 2),
        "annualized_pct": round(annualized * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "max_drawdown_date": max_drawdown_date.isoformat(),
        "trades": len(trades),
        "win_rate_pct": round(win_rate * 100, 2),
        "avg_holding_days": round(avg_hold, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "benchmark_return_pct": round(benchmark_return * 100, 2) if benchmark_return is not None else None,
        "days": days,
    }
