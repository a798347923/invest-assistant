"""组合级资金曲线模拟：把 playbook 的逐笔信号放进真实资金约束（槽位 / 整手 / 费用 / 现金）。

与 `engine/playbook_replay.py` 的关系：
- 逐笔交易（入场/出场日期与价格、止损/目标/超时）仍由 playbook 回放生成；
- 本模块只决定“做不做、做多大”：同一天先卖出回款、再买入；仓位预算 = min(组合市值×仓位比例, 现金)；
- A 股 100 股整手；滑点已包含在回放成交价中，这里只计佣金与印花税；
- 逐日收盘价盯市 → 权益曲线 → 复用 `backtest.stats.compute_stats`（总收益/年化/最大回撤/胜率/PF）。

诚实说明（局限）：
- 被跳过的信号不会“补发”同一标的当时可能存在的其他信号（逐笔序列是独立回放生成的，近似）；
- 同日多个买入信号按代码排序（无强弱信息）；
- 当日进出（回放口径允许，A 股实盘 T+1 不可卖）按当日结算近似，会略微高估交易频率；
- 不建模涨跌停无法成交、停牌、分红再投资等细节。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select

from ..backtest.fees import Fees
from ..backtest.stats import compute_stats
from ..datasource import Bar, MarketDataService
from .playbook_replay import (
    DEPLOYED_OPTS,
    V7_RULES,
    ReplayTrade,
    _cost_pct,
    _enhanced_regime_map,
    _replay_code,
)


@dataclass
class PortfolioParams:
    initial_cash: float = 50_000.0
    slots: int = 5
    position_fraction: float = 0.2


def compute_signal_features(
    trades: list[ReplayTrade],
    bars_by_code: dict[str, list[Bar]],
    index_series: list | None = None,
) -> dict[tuple[str, str], dict]:
    """为每笔交易计算信号日特征（用于同日多个信号时的排序）。

    - mom: 信号日收盘 / 近 10 日最低 − 1（动量/延伸度）
    - vol: 信号日成交量 / 前 5 日均量（放量比）
    - rs: 个股 20 日收益 − 指数 20 日收益（相对强度；指数数据缺失时为 None）
    """
    from .playbook_replay import _index_return20

    feats: dict[tuple[str, str], dict] = {}
    for t in trades:
        bars = bars_by_code.get(t.code) or []
        entry = dt.date.fromisoformat(t.entry_date)
        sig_idx = None
        for k, b in enumerate(bars):
            if b.date >= entry:
                break
            sig_idx = k
        if sig_idx is None or sig_idx < 20:
            continue
        hist = bars[: sig_idx + 1]
        close = hist[-1].close
        lows = [b.low for b in hist[-10:]]
        low10 = min(lows) if lows else 0.0
        mom = (close / low10 - 1) if low10 else 0.0
        vols = [b.volume for b in hist[-6:-1]]
        avg_vol = sum(vols) / len(vols) if vols else 0.0
        volr = (hist[-1].volume / avg_vol) if avg_vol else 0.0
        rs = None
        if index_series and hist[-21].close:
            idx_ret = _index_return20(index_series, hist[-1].date)
            if idx_ret is not None:
                rs = (close / hist[-21].close - 1) - idx_ret
        feats[(t.code, t.entry_date)] = {"mom": mom, "vol": volr, "rs": rs}
    return feats


def simulate_portfolio(
    trades: list[ReplayTrade],
    bars_by_code: dict[str, list[Bar]],
    params: PortfolioParams | None = None,
    fees: Fees | None = None,
    benchmark_bars: list[Bar] | None = None,
    rank_mode: str | None = None,
    features: dict | None = None,
    index_series: list | None = None,
    drawdown_pause_pct: float | None = None,
    drawdown_resume_pct: float | None = None,
    weight_mode: str | None = None,
    weight_low: float = 0.6,
    weight_high: float = 1.4,
) -> dict:
    """把逐笔交易装进资金约束，产出权益曲线与账户级统计。

    rank_mode: None=按代码顺序；"momentum" / "volume" / "rs" / "composite"=同日信号按强度排序。
    weight_mode: 同日信号按强度加权仓位（线性权重 weight_low..weight_high，均值≈1）。
    drawdown_pause_pct: 组合回撤达到该比例后暂停新开仓（恢复线 drawdown_resume_pct，默认暂停线的一半）。
    """
    p = params or PortfolioParams()
    fees = fees or Fees()
    bar_map = {code: {b.date: b for b in bars} for code, bars in bars_by_code.items() if bars}
    if not bar_map:
        return {"stats": {}, "signals": len(trades), "executed": 0, "skipped_slots": 0,
                "skipped_cash": 0, "skipped_dup": 0, "skipped_breaker": 0,
                "avg_positions": 0.0, "avg_exposure_pct": 0.0, "curve": [], "trades": []}

    if rank_mode and features is None:
        features = compute_signal_features(trades, bars_by_code, index_series)

    all_dates = sorted({d for mapping in bar_map.values() for d in mapping})
    entries_by_day: dict[dt.date, list[ReplayTrade]] = {}
    for t in trades:
        entries_by_day.setdefault(dt.date.fromisoformat(t.entry_date), []).append(t)

    def _order_day(day_list: list[ReplayTrade]) -> list[ReplayTrade]:
        if not rank_mode or not features:
            return sorted(day_list, key=lambda t: t.code)
        # 同一标的同一天只可能有一个信号；按特征强度降序
        if rank_mode == "composite":
            n = len(day_list)
            score = [0.0] * n
            for key in ("mom", "vol", "rs"):
                vals = [
                    (float(f.get(key)), i)
                    for i, t in enumerate(day_list)
                    for f in [(features.get((t.code, t.entry_date)) or {})]
                    if f.get(key) is not None
                ]
                if len(vals) < 2:
                    continue
                vals.sort(key=lambda x: x[0])
                for rank, (_, i) in enumerate(vals):
                    score[i] += rank / (len(vals) - 1)
            return [
                day_list[i]
                for i in sorted(range(n), key=lambda i: (-score[i], day_list[i].code))
            ]
        key = {"momentum": "mom", "volume": "vol", "rs": "rs"}.get(rank_mode)
        if key is None:
            return sorted(day_list, key=lambda t: t.code)
        return sorted(
            day_list,
            key=lambda t: (
                -float((features.get((t.code, t.entry_date)) or {}).get(key) or 0.0),
                t.code,
            ),
        )

    cash = float(p.initial_cash)
    positions: dict[str, dict] = {}
    last_price: dict[str, float] = {}
    executed: list[dict] = []
    skipped_slots = skipped_cash = skipped_dup = skipped_breaker = 0
    curve: list[tuple[dt.date, float]] = []
    position_days = 0
    exposure_sum = 0.0
    peak_equity = float(p.initial_cash)
    paused = False

    def market_value() -> float:
        return sum(
            pos["qty"] * last_price.get(code, pos["entry_price"])
            for code, pos in positions.items()
        )

    def settle(code: str, pos: dict) -> None:
        nonlocal cash
        proceeds = pos["qty"] * pos["exit_price"]
        fee = fees.sell_fee(proceeds)
        cash += proceeds - fee
        pnl = proceeds - fee - (pos["amount"] + pos["buy_fee"])
        executed.append(
            {
                "code": code,
                "setup": pos["setup"],
                "entry_date": pos["entry_date"].isoformat(),
                "exit_date": pos["exit_date"].isoformat(),
                "entry": round(pos["entry_price"], 4),
                "exit": round(pos["exit_price"], 4),
                "qty": pos["qty"],
                "pnl": round(pnl, 2),
                "reason": pos["reason"],
            }
        )

    for day in all_dates:
        # 1) 卖出（回放给定的出场日/价；含“当日进出”在次日开始的兜底结算）
        for code in list(positions.keys()):
            if positions[code]["exit_date"] <= day:
                settle(code, positions[code])
                del positions[code]

        # 2) 买入（当日到价信号）
        equity_now = cash + market_value()
        if drawdown_pause_pct is not None:
            peak_equity = max(peak_equity, equity_now)
            dd = equity_now / peak_equity - 1 if peak_equity else 0.0
            pause_line = -abs(float(drawdown_pause_pct)) / 100.0
            resume_line = -abs(
                float(
                    drawdown_resume_pct
                    if drawdown_resume_pct is not None
                    else drawdown_pause_pct / 2
                )
            ) / 100.0
            if not paused and dd <= pause_line:
                paused = True
            elif paused and dd >= resume_line:
                paused = False
        day_entries = _order_day(entries_by_day.get(day, []))
        if paused:
            skipped_breaker += len(day_entries)
            day_entries = []
        day_weights: dict[str, float] = {}
        if weight_mode and len(day_entries) >= 2 and features:
            key = {"momentum": "mom", "volume": "vol", "rs": "rs"}.get(weight_mode)
            if key:
                pairs = []
                for t in day_entries:
                    value = (features.get((t.code, t.entry_date)) or {}).get(key)
                    if value is not None:
                        pairs.append((float(value), t.code))
                if len(pairs) >= 2:
                    pairs.sort()
                    n = len(pairs)
                    for rank, (_, code) in enumerate(pairs):
                        day_weights[code] = weight_low + (weight_high - weight_low) * rank / (n - 1)
        for t in day_entries:
            if t.code in positions:
                skipped_dup += 1
                continue
            if len(positions) >= p.slots:
                skipped_slots += 1
                continue
            price = t.entry_price
            if price <= 0:
                continue
            budget = min(
                equity_now * p.position_fraction * day_weights.get(t.code, 1.0), cash
            )
            qty = int(budget / (price * 100)) * 100
            while qty >= 100 and qty * price + fees.buy_fee(qty * price) > cash:
                qty -= 100
            if qty < 100:
                skipped_cash += 1
                continue
            amount = qty * price
            buy_fee = fees.buy_fee(amount)
            cash -= amount + buy_fee
            pos = {
                "qty": qty,
                "entry_price": price,
                "amount": amount,
                "buy_fee": buy_fee,
                "entry_date": day,
                "exit_date": dt.date.fromisoformat(t.exit_date),
                "exit_price": t.exit_price,
                "setup": t.setup,
                "reason": t.exit_reason,
            }
            if pos["exit_date"] <= day:
                settle(t.code, pos)  # 当日进出（回放口径允许；实盘 T+1 近似）
            else:
                positions[t.code] = pos

        # 3) 收盘盯市
        for code in positions:
            bar = bar_map.get(code, {}).get(day)
            if bar is not None:
                last_price[code] = bar.close
        mv = market_value()
        equity = cash + mv
        curve.append((day, round(equity, 2)))
        position_days += len(positions)
        exposure_sum += (mv / equity) if equity > 0 else 0.0

    # 去掉首次成交前的“空仓等待期”，让年化更有意义
    if executed:
        first_day = dt.date.fromisoformat(executed[0]["entry_date"])
        curve = [(d, v) for d, v in curve if d >= first_day]

    benchmark_curve: list[tuple[dt.date, float]] = []
    if benchmark_bars and curve:
        first_day, last_day = curve[0][0], curve[-1][0]
        bench = [b for b in benchmark_bars if first_day <= b.date <= last_day]
        if bench:
            base = bench[0].close or 1
            benchmark_curve = [
                (b.date, round(b.close / base * p.initial_cash, 2)) for b in bench
            ]

    stats = compute_stats(curve, executed, benchmark_curve, initial_cash=p.initial_cash)
    n_days = max(len(curve), 1)
    return {
        "params": {
            "initial_cash": p.initial_cash,
            "slots": p.slots,
            "position_fraction": p.position_fraction,
        },
        "signals": len(trades),
        "executed": len(executed),
        "skipped_slots": skipped_slots,
        "skipped_cash": skipped_cash,
        "skipped_dup": skipped_dup,
        "skipped_breaker": skipped_breaker,
        "avg_positions": round(position_days / n_days, 2),
        "avg_exposure_pct": round(exposure_sum / n_days * 100, 1),
        "stats": stats,
        "curve": [(d.isoformat(), v) for d, v in curve],
        "trades": executed[-100:],
    }


def run_portfolio_replay(
    session_factory=None,
    data=None,
    codes: list[str] | None = None,
    months: int = 60,
    now: dt.datetime | None = None,
    rules: dict | None = None,
    opts: dict | None = None,
    opts_weak: dict | None = None,
    regime_band: float | None = None,
    params: PortfolioParams | None = None,
    benchmark_symbol: str = "sh000001",
    rank_mode: str | None = "momentum",
    drawdown_pause_pct: float | None = None,
    drawdown_resume_pct: float | None = None,
    weight_mode: str | None = None,
) -> dict:
    """在指定股票池上跑 playbook 信号 → 组合资金曲线模拟。

    rank_mode 默认 "momentum"：同日多个信号竞争槽位时，优先动量更强的（固定子集 MC 两批样本均为正）。
    drawdown_pause_pct：可选组合回撤熔断（默认关闭；实测 −15% 熔断对回撤/胜率略优、收益两批一正一负）。
    """
    from ..config import get_settings
    from ..db.models import PaperPosition, Position, WatchlistItem
    from ..db.session import get_session_factory

    sf = session_factory or get_session_factory()
    settings = get_settings()
    data = data or MarketDataService(session_factory=sf)
    now = now or dt.datetime.now()
    if opts is None:
        opts = dict(DEPLOYED_OPTS)  # 默认使用当前部署策略（v11）

    if codes is None:
        with sf() as session:
            codes = sorted(
                {w.code for w in session.scalars(select(WatchlistItem))}
                | {
                    p.code
                    for p in session.scalars(
                        select(PaperPosition).where(PaperPosition.status == "open")
                    )
                }
                | {
                    p.code
                    for p in session.scalars(select(Position).where(Position.status == "open"))
                }
            )
    if not codes:
        return {
            "skipped": "无自选/持仓标的可模拟",
            "codes": 0,
            "signals": 0,
            "executed": 0,
            "stats": {},
        }

    start = now.date() - dt.timedelta(days=months * 30 + 120)
    index_bars: list[Bar] = []
    try:
        index_bars = data.tencent.get_daily_bars(
            benchmark_symbol, start, now.date(), adjust="raw"
        )
    except Exception:  # noqa: BLE001 — 基准不可用则不做基准对比
        index_bars = []
    index_series = [(b.date, b.close) for b in index_bars]
    cyber_series: list[tuple[dt.date, float]] = []
    try:
        cyber_bars = data.tencent.get_daily_bars("sz399006", start, now.date(), adjust="raw")
        cyber_series = [(b.date, b.close) for b in cyber_bars]
    except Exception:  # noqa: BLE001 — 退化为单指数滞回
        pass
    if regime_band is None:
        market_cfg = settings.app_config().get("market") or {}
        try:
            regime_band = float(market_cfg.get("regime_band_pct", 1.0) or 1.0)
        except (TypeError, ValueError):
            regime_band = 1.0
    enhanced = _enhanced_regime_map(index_series, cyber_series, regime_band)
    cost = _cost_pct(settings)
    final_rules = {**V7_RULES, **(rules or {})}

    bars_by_code: dict[str, list[Bar]] = {}
    trades: list[ReplayTrade] = []
    failures = 0
    for code in codes:
        try:
            bars = data.get_daily_bars(code, start, now.date(), adjust="qfq")
        except Exception:  # noqa: BLE001 — 单只失败跳过
            failures += 1
            continue
        if len(bars) < 70:
            continue
        bars_by_code[code] = bars
        trades.extend(
            _replay_code(
                code, bars, index_series, cost, final_rules, 0.98, 0.95,
                enhanced_regime=enhanced, opts=opts, opts_weak=opts_weak,
            )
        )

    paper_cfg = settings.app_config().get("paper") or {}
    p = params or PortfolioParams(
        initial_cash=float(paper_cfg.get("initial_cash", 50_000) or 50_000),
        slots=int(paper_cfg.get("max_positions", 5) or 5),
        position_fraction=float(paper_cfg.get("position_fraction", 0.2) or 0.2),
    )
    fees_cfg = (settings.app_config().get("backtest") or {}).get("fees") or {}
    report = simulate_portfolio(
        trades,
        bars_by_code,
        params=p,
        fees=Fees.from_config(fees_cfg),
        benchmark_bars=index_bars,
        rank_mode=rank_mode,
        index_series=index_series,
        drawdown_pause_pct=drawdown_pause_pct,
        drawdown_resume_pct=drawdown_resume_pct,
        weight_mode=weight_mode,
    )
    report["codes"] = len(bars_by_code)
    report["failures"] = failures
    return report
