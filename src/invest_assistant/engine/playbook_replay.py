"""历史回放：机械定式 playbook 在历史日线上的模拟 → 统计经验（写入经验库并注入提示词）。

同时回放多个版本做同窗对比：
- baseline（原始骨架）：回踩支撑 / 突破入场 + 基础确认（缩量/放量 + 收阳）；
- v2~v5：经验迭代（弱市不做、回踩站上MA5/收复前日高点/MA20上行/站稳MA60、ATR 止损等）；
- v6：强弱判定加强（上证+创业板双指数 + ±1% 滞回带）；
- v7：v6 基础上，回踩型额外要求上证在 MA20 上方（突破不受此限）；
- v8：v7 + 持有上限 30 日、突破动量门槛、不追高开（后两条在总利润口径下已移除）；
- v9：v7 + 持有上限 30 日、突破目标 +15%；
- v10：v9 基础上突破目标 +20%（纯利润口径，胜率降至 56%，不满足 ≥60% 约束）；
- v11：持有上限 30 日、突破目标 +10%（胜率 ≥60% 下的利润最优；弱市 0 仓位）；
- v12（当前固化版）：v11 + 到 +10% 卖一半、余仓目标 +12%（P2，两批样本利润提升、胜率不变）。

回放口径（诚实说明）：
- 回放的是**确定性骨架**（入场形态 + 确认条件 + 止损/止盈/超时），**不含 AI 当日建议层**
  （加仓/减仓依赖每日 LLM 判断，无法廉价重演）；
- 全部用「当日收盘确认 → 次日开盘成交」，杜绝未来函数；
- 成本：滑点 0.1%（买卖各一次）+ 按 1 万元名义金额的佣金/印花税近似（与回测口径一致）；
- 样本为自选/持仓标的，存在选股偏差，统计仅供参考。

入场定式：
- A 回踩支撑：收盘价贴近 MA20/MA60（-1%~+3%）且 MA20≥MA60；确认：缩量 + 收阳（v2 追加 站上 MA5）→ 次日开盘买入
- B 突破：收盘价 ≥ 前 20 日最高 ×0.999；确认：放量 + 收阳 → 次日开盘买入
离场：止损 = min(最近 10 日最低)×stop_ratio（盘中触及按 min(止损, 开盘) 成交）；
止盈 = A: 前 20 日最高×1.02，B: 收盘×1.08（触达按 max(目标, 开盘)）；
超时 = 20 个交易日后收盘平仓。同一标的同时只持一仓。
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select

from ..config import get_settings
from ..datasource import MarketDataService
from ..db.models import PaperPosition, Position, TradingLesson, WatchlistItem
from ..db.session import get_session_factory
from ..domain.conditions import evaluate_confirmations
from ..domain.indicators import atr

log = logging.getLogger(__name__)

SETUP_PULLBACK = "回踩支撑"
SETUP_BREAKOUT = "突破"
SETUP_REVERSAL = "超跌反转"
ALL_SETUPS = (SETUP_PULLBACK, SETUP_BREAKOUT, SETUP_REVERSAL)
MAX_HOLD_DAYS = 20

# v7 规则集：实验变体默认在此之上叠加（opts），避免按变体名匹配规则
V7_RULES = {
    "weak_block": "all",        # "all" / "breakout_only" / "none"
    "above_ma5": True,          # 回踩确认追加「站上 MA5」
    "pullback_enhanced": True,  # 收复前日高点 + MA20 上行 + 站稳 MA60
    "atr_stop": True,           # 回踩止损 = 近10日低 − 1×ATR14
    "raw_gate": True,           # 回踩要求上证在 MA20 上方
    "enhanced": True,           # 强弱判定用双指数+滞回映射
}

# 经验固化目标：lesson/提示词以该变体为准（v12=P2：目标12% + 到10%减半）
DEPLOYED_VARIANT = "v12"

# v12 已采纳规则（P2：到 +10% 卖一半、余仓目标 +12%；20% 仓位，回撤基本持平）
DEPLOYED_OPTS = {
    "max_hold": 30,
    "target_mult": 0.12,
    "partial_at": 0.10,
    "partial_frac": 0.5,
}


@dataclass
class ReplayTrade:
    code: str
    setup: str
    regime: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    exit_reason: str
    ret_pct: float
    holding_days: int
    mfe_pct: float
    mae_pct: float


def _cost_pct(settings) -> float:
    """单边滑点在成交价中体现，此处仅计佣金/印花税（按 1 万元名义金额近似）。"""
    fees_cfg = (settings.app_config().get("backtest") or {}).get("fees") or {}
    nominal = 10000.0
    commission_rate = float(fees_cfg.get("commission_rate", 0.00025))
    commission_min = float(fees_cfg.get("commission_min", 5.0))
    stamp = float(fees_cfg.get("stamp_tax_sell", 0.0005))
    buy_fee = max(nominal * commission_rate, commission_min) / nominal
    sell_fee = (max(nominal * commission_rate, commission_min) + nominal * stamp) / nominal
    return round((buy_fee + sell_fee) * 100, 4)


def _pullback_enhanced_ok(bars: list, i: int, closes: list, highs: list) -> bool:
    """v4/v5 回踩增强过滤：收复前一日高点 + MA20 上行 + 站稳 MA60。"""
    if i >= 1 and not (bars[i].close > bars[i - 1].high):
        return False  # 未收复前一日高点（企稳证据不足）
    if len(closes) >= 25:
        ma20 = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-25:-5]) / 20
        if ma20 < ma20_prev:
            return False  # MA20 未上行
    if len(closes) >= 60:
        ma60 = sum(closes[-60:]) / 60
        if ma60 and bars[i].close < ma60 * 0.995:
            return False  # 未站稳 MA60（下行趋势中接刀）
    return True


def _index_return20(index_series: list, day: dt.date) -> float | None:
    """指数近 20 个交易日收益（只用当日及之前数据；不足 21 个点返回 None）。"""
    points = [c for d, c in index_series if d <= day]
    if len(points) < 21 or not points[-21]:
        return None
    return points[-1] / points[-21] - 1


def _index_ma20_slope_pct(index_series: list, day: dt.date, lookback: int = 5) -> float | None:
    """指数 MA20 的斜率（今日 MA20 相对 lookback 个交易日前，%）：判断"弱市是否还在走坏"。"""
    points = [c for d, c in index_series if d <= day]
    if len(points) < 20 + lookback:
        return None
    ma_now = sum(points[-20:]) / 20
    ma_prev = sum(points[-(20 + lookback) : -lookback]) / 20
    if not ma_prev:
        return None
    return (ma_now / ma_prev - 1) * 100


def _index_ma60_relation(index_series: list, day: dt.date) -> str | None:
    """指数相对自身 MA60 的位置：'above' / 'below'（数据不足返回 None）。

    用途：把"弱市"拆成「牛市中的回调（指数仍在 MA60 上方）」与「熊市中继（已在 MA60 下方）」，
    这两类的赔率完全不同，混在一起统计会互相抵消。
    """
    points = [c for d, c in index_series if d <= day]
    if len(points) < 60:
        return None
    ma60 = sum(points[-60:]) / 60
    if not ma60:
        return None
    return "above" if points[-1] >= ma60 else "below"


def _signal(
    bars: list, i: int, *, index_ok: bool, rules: dict,
    stop_pullback: float, stop_breakout: float,
    raw_index_ok: bool = True, opts: dict | None = None,
    index_series: list | None = None,
) -> tuple[str, float, float] | None:
    """在第 i 日（收盘后）评估入场信号；返回 (形态, 止损, 目标) 或 None。

    rules 由变体定义（不按名字匹配规则）：
    - weak_block: "all"（弱市不做）/ "breakout_only"（弱市仅过滤突破）/ "none"（不过滤）
    - above_ma5: 回踩确认追加「站上 MA5」
    - pullback_enhanced: 回踩额外要求「收复前一日高点 + MA20 上行 + 站稳 MA60」
    - atr_stop: 回踩止损改为 ATR 自适应（近10日低 − atr_mult×ATR14）
    - raw_gate: 回踩型额外要求上证在 MA20 上方（raw_index_ok）

    opts（实验开关，默认全部关闭）：
    - rs_min: 相对强度门槛（个股20日收益 − 指数20日收益 ≥ rs_min；两形态适用）
    - chase_day / chase_5d: 防追高（突破型；信号日涨幅% / 5日累计涨幅% 上限）
    - min_ext: 动量门槛（突破型；收盘须 ≥ 10日低 ×(1+min_ext)）
    - anti_ext: 防延伸（突破型；收盘须 ≤ 10日低 ×(1+anti_ext)）
    - target_mult: 突破目标倍数（默认 0.08）
    - stop_breakout_mult: 突破止损系数覆盖（默认用变体设定）
    - atr_mult: 回踩 ATR 缓冲倍数（默认 1.0）
    - allow_setup: "both"（默认）/ "breakout"（只在弱市做突破）/ "pullback"（只在弱市做回踩）
    - index_slope_min: 指数 MA20 五日斜率门槛（%）；低于门槛不做（区分"弱市下跌中继"与"弱市筑底"）
    - index_ma60: "above" / "below" —— 要求指数在自身 MA60 上方/下方（把弱市拆成"牛市回调"与"熊市中继"）
    - 反转形态（allow_setup="reversal" 时）：rev_drop_pct / rev_stop_pct / rev_target /
      rev_freefall_pct / max_hold_reversal
    """
    opts = opts or {}
    if i < 60 or i >= len(bars) - 1:  # 需 MA60 预热 + 次日开盘可成交
        return None
    if rules.get("weak_block", "all") == "all" and not index_ok:
        return None  # 经验：弱市不做入场（breakout_only 变体例外：弱市仅过滤突破）
    closes = [b.close for b in bars[: i + 1]]
    highs = [b.high for b in bars[: i + 1]]
    lows = [b.low for b in bars[: i + 1]]
    close = closes[-1]
    if not close:
        return None
    now = dt.datetime.combine(bars[i].date, dt.time(15, 0))
    history = bars[: i + 1]

    # 弱市「还在走坏」闸门：指数 MA20 斜率低于门槛时不做（区分下跌中继 vs 筑底/修复）
    slope_min = opts.get("index_slope_min")
    if slope_min is not None and index_series:
        slope = _index_ma20_slope_pct(index_series, bars[i].date, 5)
        if slope is not None and slope < float(slope_min):
            return None

    # 指数相对 MA60 的位置闸门：把「牛市回调」与「熊市中继」分开
    ma60_rel = opts.get("index_ma60")
    if ma60_rel and index_series:
        rel = _index_ma60_relation(index_series, bars[i].date)
        if rel is not None and rel != str(ma60_rel):
            return None

    rs_min = opts.get("rs_min")
    if rs_min is not None and index_series:
        idx_ret = _index_return20(index_series, bars[i].date)
        if idx_ret is not None and len(closes) >= 21 and closes[-21]:
            if close / closes[-21] - 1 - idx_ret < float(rs_min):
                return None  # B3：个股 20 日未跑赢指数 → 不买

    high20_prev = max(highs[i - 20 : i]) if i >= 20 else 0.0
    skip_weak_breakout = rules.get("weak_block") == "breakout_only" and not index_ok
    allow_setup = opts.get("allow_setup")
    if (
        high20_prev
        and close >= high20_prev * 0.999
        and not skip_weak_breakout
        and allow_setup != "pullback"
    ):
        chase_ok = True
        chase_day = opts.get("chase_day")
        if chase_day is not None and len(closes) >= 2 and closes[-2]:
            if (close / closes[-2] - 1) * 100 > float(chase_day):
                chase_ok = False  # B4：信号日涨幅过大（脉冲衰竭）
        chase_5d = opts.get("chase_5d")
        if chase_ok and chase_5d is not None and len(closes) >= 6 and closes[-6]:
            if (close / closes[-6] - 1) * 100 > float(chase_5d):
                chase_ok = False  # B4：5 日累计涨幅过大
        if chase_ok:
            ok, _ = evaluate_confirmations(
                ["volume_expand", "bullish"], price=close, open_price=bars[i].open,
                volume=bars[i].volume, source="em", bars=history, now=now,
            )
            if ok:
                low10 = min(lows[i - 9 : i + 1])
                min_ext = opts.get("min_ext")
                if min_ext is not None and low10 and close < low10 * (1 + float(min_ext)):
                    ok = False  # R3：动量门槛（仅买已延伸的突破，动量延续）
                anti_ext = opts.get("anti_ext")
                if ok and anti_ext is not None and low10 and close > low10 * (1 + float(anti_ext)):
                    ok = False  # 防延伸：离 10 日低过远（起跑太远的深止损人群）
                if ok:
                    sb = float(opts.get("stop_breakout_mult") or stop_breakout)
                    tm = float(opts.get("target_mult", 0.08) or 0.08)
                    return SETUP_BREAKOUT, low10 * sb, close * (1 + tm)

    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60

    # 超跌反转（实验形态，默认关闭）：弱势里做均值回归，而不是追动量
    if allow_setup == "reversal":
        if opts.get("enable_reversal") is None:
            return None
        drop = float(opts.get("rev_drop_pct", 0.05) or 0.05)
        if ma20 and close <= ma20 * (1 - drop):
            bounced = close > bars[i].open and (i >= 1 and close > bars[i - 1].close)
            high10 = max(highs[i - 9 : i + 1])
            # 不接飞刀：若已自近 10 日高点回撤超过 rev_freefall_pct（默认 15%）则跳过
            not_freefall = bool(high10) and close > high10 * (
                1 - float(opts.get("rev_freefall_pct", 0.15) or 0.15)
            )
            if bounced and not_freefall:
                stop_pct = float(opts.get("rev_stop_pct", 0.04) or 0.04)
                target_mult = float(opts.get("rev_target", 0.06) or 0.06)
                return SETUP_REVERSAL, close * (1 - stop_pct), close * (1 + target_mult)
        return None

    if allow_setup != "breakout" and ma60 and ma20 >= ma60:
        dist20 = (close / ma20 - 1) * 100 if ma20 else 999.0
        dist60 = (close / ma60 - 1) * 100 if ma60 else 999.0
        if -1.0 <= min(dist20, dist60) <= 3.0:
            confirm = ["volume_shrink", "bullish"]
            if rules.get("above_ma5"):
                confirm.append("above_ma5")  # 经验：回踩后须站上 5 日线
            ok, _ = evaluate_confirmations(
                confirm, price=close, open_price=bars[i].open,
                volume=bars[i].volume, source="em", bars=history, now=now,
            )
            if ok:
                if rules.get("pullback_enhanced") and not _pullback_enhanced_ok(bars, i, closes, highs):
                    return None
                if rules.get("raw_gate") and not raw_index_ok:
                    return None  # v7：上证未站上 MA20 时不做回踩（v5p 实验：弱市回踩≈打平，机会成本高）
                stop = min(lows[i - 9 : i + 1]) * stop_pullback
                if rules.get("atr_stop"):
                    a = atr(bars[: i + 1], 14)
                    if a and a > 0:
                        mult = float(opts.get("atr_mult", 1.0) or 1.0)
                        stop = min(lows[i - 9 : i + 1]) - mult * a  # ATR 自适应缓冲（atr_mult 可调）
                return SETUP_PULLBACK, stop, max(highs[i - 19 : i + 1]) * 1.02
    return None


def _replay_code(
    code: str, bars: list, index_series: list, cost_pct: float,
    rules: dict, stop_pullback: float, stop_breakout: float,
    enhanced_regime: dict | None = None, opts: dict | None = None,
    opts_weak: dict | None = None,
) -> list[ReplayTrade]:
    opts = opts or {}
    opts_weak = opts_weak or {}
    trades: list[ReplayTrade] = []
    i = 60
    n = len(bars)
    while i < n - 1:
        raw_index_ok = True
        if rules.get("enhanced"):
            # 强弱判定加强版（上证+创业板双指数 + 滞回带）
            index_ok = bool((enhanced_regime or {}).get(bars[i].date, True))
            regime = "双指数强市" if index_ok else "双指数弱市"
            raw_regime = _regime_at(index_series, bars[i].date)
            raw_index_ok = raw_regime != "指数<20日线"
        else:
            regime = _regime_at(index_series, bars[i].date)
            index_ok = regime != "指数<20日线"
        day_opts = opts_weak if (not index_ok and opts_weak) else opts
        signal = _signal(
            bars, i, index_ok=index_ok, rules=rules,
            stop_pullback=stop_pullback, stop_breakout=stop_breakout,
            raw_index_ok=raw_index_ok, opts=day_opts, index_series=index_series,
        )
        if signal is None:
            i += 1
            continue
        setup, stop, target = signal
        confirm_window = int(day_opts.get("confirm_days", 0) or 0)
        entry_idx = i + 1
        if confirm_window > 0:
            entry_idx = 0
            for k in range(i + 1, min(i + 1 + confirm_window, n - 1)):
                if bars[k].close > bars[i].high:
                    entry_idx = k + 1  # B5：二次确认日（收盘站上信号日高点）→ 次日开盘进场
                    break
            if not entry_idx:
                i += 1  # 未等到确认日 → 放弃该信号
                continue
        entry_bar = bars[entry_idx]
        entry_price = entry_bar.open * 1.001  # 买入滑点
        if entry_price <= 0:
            i += 1
            continue
        gap_cap = day_opts.get("gap_cap")
        if gap_cap is not None and entry_bar.open > bars[i].close * (1 + float(gap_cap)):
            i += 1  # 跳空过大：放弃该信号（不追高）
            continue
        stop_cap = day_opts.get("stop_cap")
        if stop_cap is not None:
            stop = max(stop, entry_price * (1 - float(stop_cap)))  # 单笔风险距离封顶
        fixed_stop_pct = day_opts.get("fixed_stop_pct")
        if fixed_stop_pct is not None:
            stop = entry_price * (1 - float(fixed_stop_pct) / 100.0)  # 固定百分比止损（覆盖机械止损）
        breakeven = day_opts.get("breakeven")
        trail_atr = day_opts.get("trail_atr")
        activate_pct = float(day_opts.get("activate_pct", 2.0) or 2.0)
        time_stop = day_opts.get("time_stop")
        partial_at = None if setup == SETUP_REVERSAL else day_opts.get("partial_at")
        partial_frac = float(day_opts.get("partial_frac", 0.5) or 0.5)
        partial_px: float | None = None
        atr_entry: float | None = None
        if trail_atr is not None:
            a = atr(bars[: i + 1], 14)
            atr_entry = float(a) if a and a > 0 else None
        exit_price: float | None = None
        exit_reason = ""
        exit_date = entry_bar.date
        mfe, mae = -999.0, 999.0
        hold_key = "max_hold_reversal" if setup == SETUP_REVERSAL else "max_hold"
        hold_days = int(day_opts.get(hold_key, MAX_HOLD_DAYS) or MAX_HOLD_DAYS)
        last_j = min(entry_idx + hold_days, n - 1)
        stop_eff = stop
        activated = False
        highest_close = entry_price
        j = entry_idx
        while j <= last_j:
            bar = bars[j]
            mfe = max(mfe, (bar.high / entry_price - 1) * 100)
            mae = min(mae, (bar.low / entry_price - 1) * 100)
            if bar.low <= stop_eff:
                exit_price = min(stop_eff, bar.open) * 0.999  # 卖出滑点
                exit_reason = "移动止损" if stop_eff > stop else "止损"
                exit_date = bar.date
                break
            if partial_at is not None and partial_px is None:
                level = entry_price * (1 + float(partial_at))
                if bar.high >= level:
                    partial_px = max(level, bar.open) * 0.999  # 分批止盈：先卖一半
                    if opts.get("partial_breakeven"):
                        stop_eff = max(stop_eff, entry_price)  # 余仓保本（降回撤）
            if bar.high >= target:
                exit_price = max(target, bar.open) * 0.999
                exit_reason = "目标"
                exit_date = bar.date
                break
            if time_stop and (j - (i + 1)) >= int(time_stop[0]):
                if bar.close < entry_price * (1 + float(time_stop[1]) / 100.0):
                    exit_price = bar.close * 0.999
                    exit_reason = "时间止损"
                    exit_date = bar.date
                    break
            # 动态止损：以当日收盘更新、次日起生效（保守，无同日假设）
            if breakeven is not None or atr_entry is not None:
                if not activated and bar.close >= entry_price * (1 + activate_pct / 100.0):
                    activated = True
                if activated:
                    if breakeven is not None:
                        stop_eff = max(stop_eff, entry_price)
                    if atr_entry is not None:
                        highest_close = max(highest_close, bar.close)
                        stop_eff = max(stop_eff, highest_close - float(trail_atr) * atr_entry)
            j += 1
        if exit_price is None:
            bar = bars[last_j]
            exit_price = bar.close * 0.999
            exit_reason = "超时"
            exit_date = bar.date
        if partial_px is not None:
            # 半仓已在 +partial_at 止盈，剩余仓位按最终出场价结算（费用按一次近似）
            exit_price = partial_frac * partial_px + (1 - partial_frac) * exit_price
        ret = (exit_price / entry_price - 1) * 100 - cost_pct
        trades.append(
            ReplayTrade(
                code=code,
                setup=setup,
                regime=regime,
                entry_date=entry_bar.date.isoformat(),
                entry_price=round(entry_price, 4),
                exit_date=exit_date.isoformat(),
                exit_price=round(exit_price, 4),
                exit_reason=exit_reason,
                ret_pct=round(ret, 2),
                holding_days=(exit_date - entry_bar.date).days,
                mfe_pct=round(mfe if mfe > -998 else 0.0, 2),
                mae_pct=round(mae if mae < 998 else 0.0, 2),
            )
        )
        i = j + 1
    return trades


def _regime_at(index_series: list, day: dt.date) -> str:
    points = [c for d, c in index_series if d <= day and (day - d).days <= 40]
    if len(points) < 20:
        return "未知"
    ma20 = sum(points[-20:]) / 20
    return "指数≥20日线" if points[-1] >= ma20 else "指数<20日线"


def _enhanced_regime_map(
    sh_series: list, cyber_series: list, band_pct: float = 1.0
) -> dict:
    """双指数 + 滞回带的逐日强弱映射（回放用；与实盘 market_regime_state 同一口径）。

    - strong→weak：任一指数收盘跌破 其MA20×(1−band)；
    - weak→strong：两指数均收于 MA20×(1+band) 之上；
    只用当日及之前数据（无未来函数）；缺少某指数时退化为单指数滞回。
    """

    def _points(series: list) -> dict:
        out: dict = {}
        closes: list[float] = []
        for day, close in series:
            closes.append(close)
            if len(closes) >= 20:
                out[day] = (close, sum(closes[-20:]) / 20)
        return out

    sh = _points(sh_series)
    cyber = _points(cyber_series)
    result: dict = {}
    state: str | None = None
    for day in sorted(sh.keys()):
        rels: list[float] = []
        sh_pt = sh.get(day)
        if sh_pt and sh_pt[1]:
            rels.append((sh_pt[0] / sh_pt[1] - 1) * 100)
        cyber_pt = cyber.get(day)
        if cyber_pt and cyber_pt[1]:
            rels.append((cyber_pt[0] / cyber_pt[1] - 1) * 100)
        if not rels:
            continue
        if state is None:
            state = "strong" if all(r >= 0 for r in rels) else "weak"
        elif state == "strong":
            if any(r < -band_pct for r in rels):
                state = "weak"
        else:
            if all(r > band_pct for r in rels):
                state = "strong"
        result[day] = state == "strong"
    return result


def _stat(items: list[ReplayTrade]) -> dict | None:
    if not items:
        return None
    rets = [t.ret_pct for t in items]
    return {
        "n": len(items),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100),
        "avg_ret": round(sum(rets) / len(rets), 2),
        "total_ret": round(sum(rets), 1),
        "pf": (
            round(sum(r for r in rets if r > 0) / abs(sum(r for r in rets if r < 0)), 2)
            if any(r < 0 for r in rets)
            else 99.0
        ),
        "median_hold": sorted(t.holding_days for t in items)[len(items) // 2],
        "stop_rate": round(sum(1 for t in items if t.exit_reason == "止损") / len(items) * 100),
        "dynamic_stop_rate": round(sum(1 for t in items if t.exit_reason == "移动止损") / len(items) * 100),
        "target_rate": round(sum(1 for t in items if t.exit_reason == "目标") / len(items) * 100),
        "timeout_rate": round(sum(1 for t in items if t.exit_reason == "超时") / len(items) * 100),
        "time_stop_rate": round(sum(1 for t in items if t.exit_reason == "时间止损") / len(items) * 100),
        "avg_mfe": round(sum(t.mfe_pct for t in items) / len(items), 2),
        "avg_mae": round(sum(t.mae_pct for t in items) / len(items), 2),
    }


def _aggregate(trades: list[ReplayTrade]) -> dict:
    buckets: dict[str, dict] = {}
    overall = _stat(trades)
    if overall:
        buckets["全部"] = overall
    for setup in ALL_SETUPS:
        subset = [t for t in trades if t.setup == setup]
        if subset:
            buckets[setup] = _stat(subset)
        for regime in ("指数≥20日线", "指数<20日线"):
            combo = [t for t in trades if t.setup == setup and t.regime == regime]
            if len(combo) >= 3:
                buckets[f"{setup}｜{regime}"] = _stat(combo)
    return buckets


def _findings(buckets: dict) -> list[str]:
    out: list[str] = []
    overall = buckets.get("全部")
    if overall:
        out.append(
            f"历史模拟（机械骨架，次日开盘成交）：{overall['n']} 笔，胜率 {overall['win_rate']}%，"
            f"平均 {overall['avg_ret']:+.2f}%，止损率 {overall['stop_rate']}%，"
            f"平均持有 {overall['median_hold']} 天（中位）"
        )
    for setup in ALL_SETUPS:
        stat = buckets.get(setup)
        if stat and stat["n"] >= 5:
            out.append(
                f"{setup}型：{stat['n']} 笔，胜率 {stat['win_rate']}%，平均 {stat['avg_ret']:+.2f}%"
                f"（平均最大浮盈 {stat['avg_mfe']:+.1f}%、最大浮亏 {stat['avg_mae']:+.1f}%）"
            )
    for key, stat in buckets.items():
        if stat["n"] < 5:
            continue
        if "｜指数<20日线" in key and stat["avg_ret"] < 0:
            out.append(f"→ {key} 历史期望为负（{stat['avg_ret']:+.2f}%/{stat['n']} 笔）：弱势环境谨慎执行该型入场")
        if "｜指数≥20日线" in key and stat["avg_ret"] > 0.5:
            out.append(f"→ {key} 历史期望为正（{stat['avg_ret']:+.2f}%/{stat['n']} 笔）：可作为常规入场形态")
    pull = buckets.get(SETUP_PULLBACK)
    if pull and pull["n"] >= 5 and pull["stop_rate"] >= 40:
        out.append(
            f"→ 回踩型止损率 {pull['stop_rate']}% 偏高：区间与止损距离过近或确认不足，建议缩小首仓或放宽止损"
        )
    return out[:8]


def _condense_summary(overall: dict, buckets: dict, variant_label: str = "v3") -> str:
    """把回放结论压缩成一行（用于 AI 提示词注入；完整明细仍存 lessons_json 供复盘页查看）。"""
    if not overall:
        return f"历史模拟（回放 {variant_label}）：样本不足，暂无可注入结论"
    parts = [
        f"历史模拟（回放 {variant_label}）：整体 {overall.get('n', 0)} 笔 胜率 {overall.get('win_rate', 0)}% "
        f"平均 {overall.get('avg_ret', 0):+.2f}%"
    ]
    breakout = buckets.get(SETUP_BREAKOUT) or {}
    strong_breakout = buckets.get(f"{SETUP_BREAKOUT}｜指数≥20日线") or breakout
    if breakout and strong_breakout:
        parts.append(
            f"突破型仅在强市有效（强市 {strong_breakout.get('avg_ret', 0):+.2f}%/{strong_breakout.get('n', 0)} 笔，弱市剔除）"
        )
    pullback = buckets.get(SETUP_PULLBACK) or {}
    if pullback:
        parts.append(
            f"回踩型 {pullback.get('avg_ret', 0):+.2f}%/{pullback.get('n', 0)} 笔"
            f"（止损率 {pullback.get('stop_rate', 0)}%、胜率 {pullback.get('win_rate', 0)}%）"
        )
    parts.append("已固化为规则：弱市（双指数+滞回）暂停买入、回踩须上证站上20日线、持有上限30日、突破目标+12%（到+10%减半）、形态差异化机械止损（回踩用 ATR 缓冲）")
    return "；".join(parts)


def _sample_codes(session_factory, size: int, seed: int = 42) -> list[str]:
    """从最近的全市场快照中抽样代码（排除自选/持仓与 ST/退市/新股，市值≥50亿），用于扩展样本回放。"""
    import json
    import random

    from ..config import get_settings

    cache_dir = get_settings().data_dir / "snapshot"
    rows: list[dict] = []
    if cache_dir.exists():
        for path in sorted(cache_dir.glob("*.json"), reverse=True)[:3]:
            try:
                rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
            except Exception:  # noqa: BLE001 — 缓存损坏则试更早的
                rows = []
            if rows:
                break
    with session_factory() as session:
        exclude = {w.code for w in session.scalars(select(WatchlistItem))}
        exclude |= {
            p.code
            for p in session.scalars(select(PaperPosition).where(PaperPosition.status == "open"))
        }
        exclude |= {
            p.code
            for p in session.scalars(select(Position).where(Position.status == "open"))
        }
    candidates: list[str] = []
    for row in rows:
        code = str(row.get("code") or "")
        name = str(row.get("name") or "")
        upper = name.upper()
        if len(code) != 6 or code in exclude:
            continue
        if "ST" in upper or "退" in name or upper.startswith("N"):
            continue
        if (row.get("market_cap") or 0) < 50:  # 剔除小市值，降低数据噪声
            continue
        candidates.append(code)
    random.Random(seed).shuffle(candidates)
    return candidates[: max(int(size), 0)]


def run_playbook_replay(
    session_factory=None, data=None, codes: list[str] | None = None,
    months: int = 60, now: dt.datetime | None = None, save_lesson: bool = True,
    sample_size: int = 0, regime_band: float | None = None,
    extra_variants: dict | None = None,
) -> dict:
    """在历史日线上同时回放「基线」与「经验版 v2/v3」，产出对比统计并写入经验库。

    codes 为 None 且 sample_size>0 时，从全市场快照随机抽样（排除自选/持仓）作为扩展样本。
    regime_band：强弱判定滞回带（%）；None 时取 config market.regime_band_pct。
    extra_variants：实验变体 {名称: {"opts": {...}}}（回踩增强规则逐条验证用）。
    """
    sf = session_factory or get_session_factory()
    settings = get_settings()
    data = data or MarketDataService(session_factory=sf)
    now = now or dt.datetime.now()

    if codes is None and sample_size > 0:
        codes = _sample_codes(sf, sample_size)
    if codes is None:
        with sf() as session:
            codes = sorted(
                {w.code for w in session.scalars(select(WatchlistItem))}
                | {p.code for p in session.scalars(select(PaperPosition).where(PaperPosition.status == "open"))}
                | {p.code for p in session.scalars(select(Position).where(Position.status == "open"))}
            )
    if not codes:
        return {"skipped": "无自选/持仓标的可回放", "trades": 0}

    start = now.date() - dt.timedelta(days=months * 30 + 120)
    index_series: list[tuple[dt.date, float]] = []
    try:
        index_bars = data.tencent.get_daily_bars("sh000001", start, now.date(), adjust="raw")
        index_series = [(b.date, b.close) for b in index_bars]
    except Exception as exc:  # noqa: BLE001 — 指数不可用则 regime 记未知
        log.debug("回放取指数失败: %s", exc)
    cyber_series: list[tuple[dt.date, float]] = []
    try:
        cyber_bars = data.tencent.get_daily_bars("sz399006", start, now.date(), adjust="raw")
        cyber_series = [(b.date, b.close) for b in cyber_bars]
    except Exception as exc:  # noqa: BLE001 — 创业板指不可用则退化为单指数滞回
        log.debug("回放取创业板指失败: %s", exc)
    if regime_band is None:
        market_cfg = settings.app_config().get("market") or {}
        try:
            regime_band = float(market_cfg.get("regime_band_pct", 1.0) or 1.0)
        except (TypeError, ValueError):
            regime_band = 1.0
    enhanced_regime = _enhanced_regime_map(index_series, cyber_series, regime_band)

    cost = _cost_pct(settings)
    variants = {
        "baseline": {
            "rules": {"weak_block": "none", "above_ma5": False, "pullback_enhanced": False,
                      "atr_stop": False, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.98, "stop_breakout": 0.98, "trades": [],
        },
        "v2": {
            "rules": {"weak_block": "all", "above_ma5": True, "pullback_enhanced": False,
                      "atr_stop": False, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.95, "stop_breakout": 0.95, "trades": [],
        },
        "v3": {
            "rules": {"weak_block": "all", "above_ma5": True, "pullback_enhanced": False,
                      "atr_stop": False, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v4": {
            "rules": {"weak_block": "all", "above_ma5": True, "pullback_enhanced": True,
                      "atr_stop": False, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v5": {
            "rules": {"weak_block": "all", "above_ma5": True, "pullback_enhanced": True,
                      "atr_stop": True, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v5p": {
            "rules": {"weak_block": "breakout_only", "above_ma5": True, "pullback_enhanced": True,
                      "atr_stop": True, "raw_gate": False, "enhanced": False},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v6": {
            "rules": {**V7_RULES, "raw_gate": False},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v7": {
            "rules": dict(V7_RULES),
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v8": {
            "rules": dict(V7_RULES),
            "opts": {"max_hold": 30, "min_ext": 0.08, "gap_cap": 0.02},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v9": {
            "rules": dict(V7_RULES),
            "opts": {"max_hold": 30, "target_mult": 0.15},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v10": {
            "rules": dict(V7_RULES),
            "opts": {"max_hold": 30, "target_mult": 0.20},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v11": {
            "rules": dict(V7_RULES),
            "opts": {"max_hold": 30, "target_mult": 0.10},
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
        "v12": {
            "rules": dict(V7_RULES),
            "opts": dict(DEPLOYED_OPTS),
            "stop_pullback": 0.98, "stop_breakout": 0.95, "trades": [],
        },
    }
    for name, spec in (extra_variants or {}).items():
        rules = dict(V7_RULES)
        rules.update(spec.get("rules") or {})
        variants[name] = {
            "rules": rules,
            "stop_pullback": float(spec.get("stop_pullback", 0.98)),
            "stop_breakout": float(spec.get("stop_breakout", 0.95)),
            "opts": dict(spec.get("opts") or {}),
            "opts_weak": dict(spec.get("opts_weak") or {}),
            "trades": [],
        }
    failures = 0
    first_days: list[dt.date] = []
    last_days: list[dt.date] = []
    for code in codes:
        try:
            bars = data.get_daily_bars(code, start, now.date(), adjust="qfq")
        except Exception as exc:  # noqa: BLE001 — 单只失败跳过
            failures += 1
            log.debug("回放取行情失败 %s: %s", code, exc)
            continue
        if len(bars) < 70:
            continue
        first_days.append(bars[0].date)
        last_days.append(bars[-1].date)
        for spec in variants.values():
            spec["trades"].extend(
                _replay_code(
                    code, bars, index_series, cost, spec["rules"],
                    spec["stop_pullback"], spec["stop_breakout"],
                    enhanced_regime=enhanced_regime,
                    opts=spec.get("opts"),
                    opts_weak=spec.get("opts_weak"),
                )
            )

    def _variant_payload(name: str) -> dict:
        trades = variants[name]["trades"]
        buckets = _aggregate(trades)
        return {"trades": len(trades), "stats": buckets, "findings": _findings(buckets)}

    payloads = {name: _variant_payload(name) for name in variants}
    comparison = "经验版对比（同窗）：" + " → ".join(
        f"{name} {payload['trades']} 笔 "
        f"{payload['stats'].get('全部', {}).get('win_rate', 0)}% "
        f"{payload['stats'].get('全部', {}).get('avg_ret', 0):+.2f}%"
        for name, payload in payloads.items()
    )
    deployed = payloads[DEPLOYED_VARIANT]
    deployed_all = deployed["stats"].get("全部") or {}
    findings = deployed["findings"]
    if findings:
        findings.insert(1, comparison)
    else:
        findings.append(comparison)

    saved = False
    deployed_trades = variants[DEPLOYED_VARIANT]["trades"]
    if save_lesson and deployed_trades:
        stamp = int(now.strftime("%Y%m%d%H%M"))
        entry_dates = sorted(t.entry_date for t in deployed_trades)
        exit_dates = sorted(t.exit_date for t in deployed_trades)
        condensed = _condense_summary(deployed_all, deployed["stats"], DEPLOYED_VARIANT)
        with sf() as session:
            exists = session.scalar(
                select(TradingLesson.id).where(
                    TradingLesson.source == "replay", TradingLesson.ref_id == stamp
                )
            )
            if exists is None:
                lesson = TradingLesson(
                    source="replay",
                    ref_id=stamp,
                    code="PLAYBOOK",
                    name=f"历史模拟（经验版 {DEPLOYED_VARIANT}）",
                    entry_date=dt.date.fromisoformat(entry_dates[0]),
                    exit_date=dt.date.fromisoformat(exit_dates[-1]),
                    holding_days=int(deployed_all.get("median_hold") or 0),
                    pnl=float(deployed_all.get("avg_ret") or 0.0),
                    pnl_pct=float(deployed_all.get("win_rate") or 0.0),
                    rating="",
                    summary=condensed,
                    lessons_json=findings,  # 完整明细（复盘页展示；注入提示词只用 summary 一行）
                    model="rule-replay",
                )
                session.add(lesson)
                session.flush()
                # 新回放替换全部旧版本（提示词只保留最新一行）
                session.execute(
                    delete(TradingLesson).where(
                        TradingLesson.source == "replay", TradingLesson.id != lesson.id
                    )
                )
                session.commit()
                saved = True

    report = {
        "codes": len(codes),
        "failures": failures,
        "regime_band": regime_band,
        "span": {
            "from": min(first_days).isoformat() if first_days else None,
            "to": max(last_days).isoformat() if last_days else None,
        },
        **payloads,
        "deployed": DEPLOYED_VARIANT,
        "lesson_saved": saved,
    }
    log.info(
        "历史回放完成: codes=%s " + " ".join(f"{name}={payloads[name]['trades']}" for name in variants) + " deployed=%s saved=%s",
        len(codes), DEPLOYED_VARIANT, saved,
    )
    return report
