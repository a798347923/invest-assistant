"""规则级回测核心（纯函数，无 IO）。

与实盘的一致性：退出条件判定复用 `domain.rules.evaluate`（同一评估器）。

执行模型（文字约定，会随报告展示）：
- 信号：T 日收盘计算（收盘价 > MA20 且 20 日涨幅 ≥ 阈值），**T+1 开盘买入**（含滑点）；
- 退出：当日 bar 的 high/low 判定；同日同时触发按**止损优先**（保守）；
  成交价近似：止损/移动止盈 = min(开盘价, 线价)、止盈 = max(开盘价, 目标价)、
  持有到期 = 当日收盘价；卖出含滑点；
- 槽位等权（默认 5），A 股 100 股整手；佣金/印花税按 Fees 计；
- 不使用未来数据：信号与判定只用截至当日的行情。
"""

from __future__ import annotations

import datetime as dt
import logging

from ..datasource import Bar
from ..domain.rules import RuleContext, evaluate
from .fees import Fees
from .stats import compute_stats

log = logging.getLogger(__name__)

DEFAULT_SIGNAL = {"ma_days": 20, "pct_days": 20, "pct_min": 3.0}
DEFAULT_EXIT = {
    "take_profit_pct": 20.0,
    "stop_loss_pct": 10.0,
    "trailing_drawdown_pct": 8.0,
    "max_holding_days": 60,
}

NOTES = [
    "信号与退出判定均只使用截至当日的数据（无未来函数）。",
    "v1 入场信号为动量代理（MA20 + 20日涨幅）；基本面时点模板属二期。",
    "退出成交价近似：止损/移动止盈取 min(开盘, 线价)，止盈取 max(开盘, 目标价)，跳空按开盘价。",
    "标的池默认取当前快照中成交额靠前的股票，存在幸存者偏差；结论仅作方法验证。",
]


class _Position:
    __slots__ = ("code", "qty", "cost", "entry_price", "entry_date", "peak", "plan")

    def __init__(self, code: str, qty: int, cost: float, entry_price: float,
                 entry_date: dt.date, plan: dict):
        self.code = code
        self.qty = qty
        self.cost = cost  # 含买入费用的每股成本
        self.entry_price = entry_price
        self.entry_date = entry_date
        self.peak = entry_price
        self.plan = plan


def _ctx(pos: _Position, bar: Bar, day: dt.date) -> RuleContext:
    return RuleContext(
        price=bar.close,
        cost=pos.cost,
        now=dt.datetime.combine(day, dt.time(15, 0)),
        opened_at=dt.datetime.combine(pos.entry_date, dt.time(9, 30)),
        prev_close=None,
        high=bar.high,
        low=bar.low,
    )


def _detect_exit(pos: _Position, bar: Bar, day: dt.date, fees: Fees):
    """返回 (成交价, 原因) 或 None；判定顺序：止损 → 移动止盈 → 止盈 → 持有到期。"""
    plan = pos.plan

    pct_sl = float(plan.get("stop_loss_pct") or 0)
    if pct_sl > 0:
        _, trigger = evaluate("fixed_stop_loss", {"pct": pct_sl}, {}, _ctx(pos, bar, day))
        if trigger:
            line = pos.cost * (1 - pct_sl / 100)
            fill = min(bar.open, line) if bar.open > 0 else line
            return fill * (1 - fees.slippage), f"止损 -{pct_sl:g}%"

    pct_tr = float(plan.get("trailing_drawdown_pct") or 0)
    if pct_tr > 0:
        _, trigger = evaluate(
            "trailing_stop", {"drawdown_pct": pct_tr}, {"peak": pos.peak}, _ctx(pos, bar, day)
        )
        pos.peak = max(pos.peak, bar.high)
        if trigger:
            line = pos.peak * (1 - pct_tr / 100)
            fill = min(bar.open, line) if bar.open > 0 else line
            return fill * (1 - fees.slippage), f"移动止盈(峰值{pos.peak:.2f} 回撤{pct_tr:g}%)"

    pct_tp = float(plan.get("take_profit_pct") or 0)
    if pct_tp > 0:
        _, trigger = evaluate("fixed_take_profit", {"pct": pct_tp}, {}, _ctx(pos, bar, day))
        if trigger:
            target = pos.cost * (1 + pct_tp / 100)
            fill = max(bar.open, target) if bar.open > 0 else target
            return fill * (1 - fees.slippage), f"止盈 +{pct_tp:g}%"

    max_days = int(plan.get("max_holding_days") or 0)
    if max_days > 0:
        _, trigger = evaluate("max_holding", {"days": max_days}, {}, _ctx(pos, bar, day))
        if trigger:
            return bar.close * (1 - fees.slippage), f"持有到期 {max_days} 天"

    return None


def _build_signals(bars: list[Bar], signal_cfg: dict) -> dict[dt.date, float]:
    """单只股票每日信号（value = 20 日涨幅），仅用截至当日数据。"""
    ma_days = int(signal_cfg["ma_days"])
    pct_days = int(signal_cfg["pct_days"])
    pct_min = float(signal_cfg["pct_min"])
    closes = [b.close for b in bars]
    signals: dict[dt.date, float] = {}
    for i, bar in enumerate(bars):
        if i + 1 < max(ma_days, pct_days + 1):
            continue
        ma = sum(closes[i + 1 - ma_days : i + 1]) / ma_days
        ref = closes[i - pct_days]
        if not ref:
            continue
        pct_change = (closes[i] / ref - 1) * 100
        if bar.close > ma and pct_change >= pct_min:
            signals[bar.date] = round(pct_change, 2)
    return signals


def run_backtest(
    params: dict,
    bars_by_code: dict[str, list[Bar]],
    benchmark_bars: list[Bar] | None = None,
    progress_cb=None,
) -> dict:
    """纯函数核心：给定参数与行情数据，产出统计、权益曲线与交易明细。"""
    start = params["start"]
    end = params["end"]
    if isinstance(start, str):
        start = dt.date.fromisoformat(start)
    if isinstance(end, str):
        end = dt.date.fromisoformat(end)
    slots = int(params.get("slots", 5))
    initial_cash = float(params.get("initial_cash", 200_000))
    signal_cfg = {**DEFAULT_SIGNAL, **(params.get("signal") or {})}
    exit_cfg = {**DEFAULT_EXIT, **(params.get("exit") or {})}
    fees = Fees.from_config(params.get("fees"))

    signals: dict[str, dict[dt.date, float]] = {}
    bar_lookup: dict[str, dict[dt.date, Bar]] = {}
    for code, bars in bars_by_code.items():
        window = [b for b in bars if start <= b.date <= end]
        if len(window) < max(int(signal_cfg["ma_days"]), int(signal_cfg["pct_days"])) + 5:
            continue
        bar_lookup[code] = {b.date: b for b in window}
        signals[code] = _build_signals(window, signal_cfg)

    all_dates = sorted({d for mapping in bar_lookup.values() for d in mapping})
    if not all_dates:
        return {"error": "区间内无可用行情数据"}

    cash = initial_cash
    positions: dict[str, _Position] = {}
    last_price: dict[str, float] = {}
    equity_curve: list[tuple[dt.date, float]] = []
    trades: list[dict] = []
    pending_buys: list[str] = []

    def portfolio_value() -> float:
        return cash + sum(
            p.qty * last_price.get(code, p.entry_price) for code, p in positions.items()
        )

    total_days = len(all_dates)
    for day_index, day in enumerate(all_dates):
        # 1) 开盘：执行昨日信号（T+1 开盘买入）
        for code in pending_buys:
            if code in positions or len(positions) >= slots:
                continue
            bar = bar_lookup.get(code, {}).get(day)
            if bar is None or bar.open <= 0:
                continue
            budget = min(portfolio_value() / slots, cash)
            lot_qty = int(budget / (bar.open * 100)) * 100
            if lot_qty < 100:
                continue
            fill = bar.open * (1 + fees.slippage)
            amount = fill * lot_qty
            fee = fees.buy_fee(amount)
            if amount + fee > cash:
                continue
            cash -= amount + fee
            positions[code] = _Position(
                code=code, qty=lot_qty, cost=(amount + fee) / lot_qty,
                entry_price=fill, entry_date=day, plan=exit_cfg,
            )
        pending_buys = []

        # 2) 当日：判定退出（止损优先）
        for code in list(positions.keys()):
            bar = bar_lookup.get(code, {}).get(day)
            if bar is None:
                continue
            exit_result = _detect_exit(positions[code], bar, day, fees)
            if exit_result is not None:
                fill, reason = exit_result
                amount = fill * positions[code].qty
                fee = fees.sell_fee(amount)
                cash += amount - fee
                trades.append(
                    {
                        "code": code,
                        "entry_date": positions[code].entry_date.isoformat(),
                        "exit_date": day.isoformat(),
                        "entry": round(positions[code].entry_price, 3),
                        "exit": round(fill, 3),
                        "qty": positions[code].qty,
                        "pnl": round((fill - positions[code].cost) * positions[code].qty - fee, 2),
                        "reason": reason,
                    }
                )
                del positions[code]
            else:
                last_price[code] = bar.close

        # 3) 收盘：生成明日买入信号（等权槽位，按信号强度排序）
        if len(positions) < slots:
            candidates = []
            for code, sig_map in signals.items():
                if code in positions:
                    continue
                value = sig_map.get(day)
                if value is not None:
                    candidates.append((value, code))
            candidates.sort(reverse=True)
            pending_buys = [code for _, code in candidates[: slots - len(positions)]]

        # 4) 记录当日净值
        equity_curve.append((day, round(portfolio_value(), 2)))
        if progress_cb and total_days:
            progress_cb((day_index + 1) / total_days)

    # 基准（沪深300）
    benchmark_curve: list[tuple[dt.date, float]] = []
    if benchmark_bars:
        bench = [b for b in benchmark_bars if start <= b.date <= end]
        if bench:
            base = bench[0].close or 1
            benchmark_curve = [
                (b.date, round(b.close / base * initial_cash, 2)) for b in bench
            ]

    stats = compute_stats(equity_curve, trades, benchmark_curve, initial_cash=initial_cash)
    return {
        "params": {
            **params,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "slots": slots,
            "initial_cash": initial_cash,
            "signal": signal_cfg,
            "exit": exit_cfg,
        },
        "universe_size": len(bar_lookup),
        "equity": [(d.isoformat(), v) for d, v in equity_curve],
        "benchmark": [(d.isoformat(), v) for d, v in benchmark_curve],
        "trades": trades[-200:],
        "trades_count": len(trades),
        "stats": stats,
        "notes": NOTES,
    }
