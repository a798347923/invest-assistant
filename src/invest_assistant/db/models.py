"""数据库模型（SQLAlchemy 2.0）：对应设计文档第 5 节全部 20 张表。

约定：
- 时间统一为本地时间（Asia/Shanghai，单机场景），价格/金额存储用 Float，
  结算计算层按需转 Decimal（个人工具精度足够，避免 SQLite 的 Decimal 警告）。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> dt.datetime:
    return dt.datetime.now()


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- 标的与持仓


class Instrument(Base):
    __tablename__ = "instrument"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)  # 600519 / 510300 / 011186
    name: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(16))  # a_share / etf / otc_fund
    exchange: Mapped[str] = mapped_column(String(8), default="")  # sh / sz / fund
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/suspended/delisted


class Position(Base):
    __tablename__ = "position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(ForeignKey("instrument.code"), index=True)
    strategy: Mapped[str] = mapped_column(String(16))  # value / growth / swing / drip
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)  # 摊薄成本（含分红调整）
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / closed
    note: Mapped[str] = mapped_column(Text, default="")


class Trade(Base):
    __tablename__ = "trade"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"), index=True)
    side: Mapped[str] = mapped_column(String(8))  # buy / sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    traded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    note: Mapped[str] = mapped_column(Text, default="")


class Dividend(Base):
    __tablename__ = "dividend"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"), index=True)
    ex_date: Mapped[dt.date] = mapped_column(Date)
    cash_amount: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(Text, default="")


class CorporateAction(Base):
    __tablename__ = "corporate_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))  # split / merge / manual
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


# ---------------------------------------------------------------- 规则与提醒


class ExitRule(Base):
    __tablename__ = "exit_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"), index=True)
    # fixed_take_profit / fixed_stop_loss / trailing_stop / daily_move / max_holding
    type: Mapped[str] = mapped_column(String(24))
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state_json: Mapped[dict] = mapped_column(JSON, default=dict)  # peak / last_triggered_at 等
    status: Mapped[str] = mapped_column(String(16), default="armed")  # armed / triggered / disabled


class WatchlistItem(Base):
    __tablename__ = "watchlist_item"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    group: Mapped[str] = mapped_column(String(32), default="default")
    note: Mapped[str] = mapped_column(Text, default="")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    # 盘中监控（自选异动/到价提醒）
    alert_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 空=用默认阈值；0=关闭异动提醒
    alert_price_above: Mapped[float | None] = mapped_column(Float, nullable=True)  # 现价 ≥ 该值提醒
    alert_price_below: Mapped[float | None] = mapped_column(Float, nullable=True)  # 现价 ≤ 该值提醒
    # AI 买入计划监控：现价进入建议买入区间 / 跌破失效价时提醒（依据最新分析卡的 plan）
    alert_plan: Mapped[bool] = mapped_column(default=True)


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("position.id"), nullable=True, index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("exit_rule.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="trigger")  # trigger / system / report
    triggered_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)
    interpretation_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending/sent/delivered/failed/expired/handled/ignored
    is_catchup: Mapped[bool] = mapped_column(default=False)
    delivery_json: Mapped[dict] = mapped_column(JSON, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)


# ---------------------------------------------------------------- LLM 与选股


class AnalysisCard(Base):
    __tablename__ = "analysis_card"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # stock / fund / trigger / close_report
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    model: Mapped[str] = mapped_column(String(32), default="")
    prompt_ver: Mapped[str] = mapped_column(String(16), default="v1")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    sources_json: Mapped[dict] = mapped_column(JSON, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    purpose: Mapped[str] = mapped_column(String(32))  # analysis / trigger / close_report / ...
    model: Mapped[str] = mapped_column(String(32))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cached_in: Mapped[int] = mapped_column(Integer, default=0)
    cost_est: Mapped[float] = mapped_column(Float, default=0.0)


class ScreenTemplate(Base):
    __tablename__ = "screen_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)


class Candidate(Base):
    __tablename__ = "candidate"
    __table_args__ = (UniqueConstraint("date", "code", "template_id", name="uq_candidate_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(16))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("screen_template.id"), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    reasons_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new / analyzed / watched / dismissed


# ---------------------------------------------------------------- 数据缓存


class DailyBar(Base):
    __tablename__ = "daily_bar"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    adjust: Mapped[str] = mapped_column(String(8), default="raw")  # raw / qfq


class FundNav(Base):
    __tablename__ = "fund_nav"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    nav: Mapped[float] = mapped_column(Float)
    acc_nav: Mapped[float | None] = mapped_column(Float, nullable=True)


class FinancialSnapshot(Base):
    __tablename__ = "financial_snapshot"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    report_period: Mapped[str] = mapped_column(String(16), primary_key=True)  # 2026Q2 等
    ann_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True, index=True)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)


class NewsItem(Base):
    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    source: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    hash: Mapped[str] = mapped_column(String(64), unique=True)


# ---------------------------------------------------------------- 基础与任务


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    is_open: Mapped[bool] = mapped_column(default=True)


class JobRun(Base):
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / ok / error
    detail: Mapped[str] = mapped_column(Text, default="")


class BacktestRun(Base):
    __tablename__ = "backtest_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    params_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / running / done / error
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class KvSetting(Base):
    __tablename__ = "kv_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------- 模拟盘（纸面交易）


class PaperPosition(Base):
    __tablename__ = "paper_position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)  # 加权成交价（不含费用）
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open / closed
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # 已实现盈亏（含费用）
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # v3 机械止损价（入场时固化；空=按卡片计划失效价）
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # v12 目标止盈价
    partial_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # v12 分批止盈价（+10%）
    partial_taken: Mapped[bool] = mapped_column(default=False)  # 是否已执行过分批止盈


class PaperTrade(Base):
    __tablename__ = "paper_trade"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8))  # buy / sell
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)  # 含滑点成交价
    amount: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # 仅卖出


class PaperReport(Base):
    __tablename__ = "paper_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    model: Mapped[str] = mapped_column(String(32), default="")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / executed / expired
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)


class PaperDaily(Base):
    __tablename__ = "paper_daily"

    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    cash: Mapped[float] = mapped_column(Float, default=0.0)
    market_value: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=0.0)
    day_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    positions: Mapped[int] = mapped_column(Integer, default=0)


class OrderIntent(Base):
    """实盘指令单：信号 → 风控 → 确认/执行 → 成交回填（全链路审计）。"""

    __tablename__ = "order_intent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    trade_date: Mapped[dt.date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8))  # buy / sell
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)  # 限价/参考价
    source: Mapped[str] = mapped_column(String(32), default="manual")  # watchlist/paper/manual
    mode: Mapped[str] = mapped_column(String(8), default="semi")  # semi=半自动
    ref_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 卖出指令关联实盘持仓
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)  # 信号日机械止损价
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending / blocked / confirmed / filled / cancelled / failed
    risk_json: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    filled_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    filled_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filled_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    executor: Mapped[str] = mapped_column(String(16), default="manual")  # manual/ui_pc
    note: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- 交易日志与复盘经验


class TradeJournal(Base):
    """统一操作日志：模拟盘 + 实盘，每笔操作连同「当时情况」入库。"""

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # paper / position
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # paper_position.id / position.id
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(16))  # 建仓/加仓/减仓/清仓/分红/拆股
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)  # 计划价位/组合状态/触发原因等


class TradingLesson(Base):
    """清仓复盘经验（高级模型生成），注入后续 AI 提示词。"""

    __tablename__ = "trading_lesson"
    __table_args__ = (UniqueConstraint("source", "ref_id", name="uq_lesson_ref"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # paper / position
    ref_id: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    entry_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    exit_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    holding_days: Mapped[int] = mapped_column(Integer, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    rating: Mapped[str] = mapped_column(String(8), default="")  # 好/一般/差
    summary: Mapped[str] = mapped_column(Text, default="")
    lessons_json: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(32), default="")
    cost: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------- 预测校准


class Prediction(Base):
    """分析卡产生的「预测」记录：建议类型 + 计划价位 + 当时环境，供事后校准。"""

    __tablename__ = "prediction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    date: Mapped[dt.date] = mapped_column(Date, index=True)  # 预测日（卡生成日）
    code: Mapped[str] = mapped_column(String(16), index=True)
    card_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(24), default="")  # 建议类型（action_bias 原文）
    price: Mapped[float] = mapped_column(Float, default=0.0)  # 预测日收盘/现价
    zone_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    zone_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    breakout: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime: Mapped[str] = mapped_column(String(24), default="")  # 指数≥20日线 / 指数<20日线
    trend: Mapped[str] = mapped_column(String(24), default="")  # 多头(>MA20) / 空头(<MA20)
    price_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 当时价格分位
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/evaluated/expired
    outcome_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CalibrationReport(Base):
    """每周预测校准报告（统计 + 结论，注入后续提示词）。"""

    __tablename__ = "calibration_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    summary: Mapped[str] = mapped_column(Text, default="")
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
