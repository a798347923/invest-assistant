"""CLI 入口：serve / once / backup / version / position / dividend。"""

from __future__ import annotations

import datetime as dt

import typer

from . import __version__

app = typer.Typer(help="AI 投资分析助手（个人自用）", no_args_is_help=True)

position_app = typer.Typer(help="持仓管理（建仓/卖出/分红/拆股/列表）", no_args_is_help=True)
dividend_app = typer.Typer(help="分红管理", no_args_is_help=True)
live_app = typer.Typer(
    help="实盘：指令单 + 风控（人工下单回填 / ADB 驱动东财 App 自动执行，默认 dry-run）",
    no_args_is_help=True,
)
app.add_typer(position_app, name="position")
app.add_typer(dividend_app, name="dividend")
app.add_typer(live_app, name="live")


def _position_service():
    """CLI 用：确保库结构最新后返回服务实例。"""
    from .db.session import ensure_schema
    from .domain.positions import PositionService

    ensure_schema()
    return PositionService()


@app.command()
def version() -> None:
    """显示版本号。"""
    typer.echo(f"invest-assistant {__version__}")


@app.command()
def observe(
    days: int = typer.Option(30, help="最近 N 天"),
    snapshot_now: bool = typer.Option(False, "--snapshot", help="立即记录一次快照"),
) -> None:
    """纸面账户观察报表：总资产/现金/股票/停泊/暴露/均仓/停泊盈亏。"""
    from .db.session import get_session_factory
    from .engine.observe import load_rows, render, snapshot
    from .engine.paper import PaperService

    if snapshot_now:
        paper = PaperService()
        with get_session_factory()() as session:
            row = snapshot(session, paper)
            session.commit()
        if row:
            typer.echo(f"已记录 {row['date']}：总资产 {row['equity']:.0f}，停泊 {row['parking_value']:.0f}")
    typer.echo(render(load_rows(days)))


@app.command("data-check")
def data_check() -> None:
    """Tushare 连通性与权限自检（不打印 token）。"""
    from .config import get_settings
    from .datasource.tushare import TushareClient

    settings = get_settings()
    token = (settings.tushare_token or "").strip()
    if not token:
        typer.secho("未配置 TUSHARE_TOKEN：请在 .env 加一行再试", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"token 已读取：长度 {len(token)}，前 3 位 {token[:3]}… 后 3 位 …{token[-3:]}")
    info = TushareClient(token).doctor()
    for item in info.get("ok", []):
        typer.secho(f"  ✓ {item['api']:16s} {item['rows']:>6d} 行", fg=typer.colors.GREEN)
    for item in info.get("failed", []):
        typer.secho(f"  ✗ {item['api']:16s} {item['error']}", fg=typer.colors.RED)
    if info.get("failed"):
        typer.echo("提示：权限/积分不足的接口需单独开通（见 docs/data-sources.md）")
        raise typer.Exit(code=1)
    typer.secho(f"全部 {len(info.get('ok', []))} 个接口可用", fg=typer.colors.GREEN)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="绑定地址（默认仅本机）"),
    port: int = typer.Option(8787, help="端口"),
    no_scheduler: bool = typer.Option(False, help="不启动定时调度（仅 Web 面板）"),
) -> None:
    """启动常驻服务：Web 面板 + 调度器（盘中轮询/启动补报/每日备份）。"""
    import logging

    import uvicorn

    from .config import get_settings
    from .db.session import ensure_schema, get_session_factory
    from .engine.scheduler import SchedulerWatchdog, build_scheduler
    from .guards import binding_error, timezone_warning
    from .logging_setup import setup_logging
    from .web.app import create_app

    settings = get_settings()

    # 护栏一：非本机绑定必须带密码（面板含持仓/提醒/可写 API）
    guard_error = binding_error(host, bool(settings.panel_password))
    if guard_error:
        typer.secho(guard_error, fg=typer.colors.RED)
        raise typer.Exit(code=1)

    settings.ensure_dirs()
    log_file = setup_logging(settings.logs_dir)
    ensure_schema()

    # 护栏二：时区偏移不符时告警（否则交易时段/持有天数会静默偏移）
    tz_warning = timezone_warning()
    if tz_warning:
        logging.getLogger(__name__).warning(tz_warning)
        typer.secho(tz_warning, fg=typer.colors.YELLOW)

    scheduler = None
    watchdog = None
    if not no_scheduler:
        scheduler = build_scheduler(settings=settings)
        scheduler.start()
        watchdog = SchedulerWatchdog(session_factory=get_session_factory())
        watchdog.attach(scheduler)
        watchdog.start()

    app = create_app(settings)
    typer.echo(f"面板已启动：http://{host}:{port}（Ctrl+C 退出）")
    typer.echo(f"运行日志：{log_file}（实时查看：Get-Content -Wait -Tail 50 该文件）")
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning", log_config=None)
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        typer.echo("服务已停止")


@app.command()
def once(
    job: str = typer.Argument(
        ..., help="poll / calendar / eod / screen / paper / calibration / replay / portfolio / catchup / backup"
    ),
    rank: str = typer.Option("", help="portfolio 专用：同日信号排序 momentum/volume/rs/composite（空=动量）"),
    breaker: float = typer.Option(0.0, help="portfolio 专用：组合回撤熔断线%（如 15；0=关闭）"),
) -> None:
    """手动执行一次作业（对应任务实现后生效）。"""
    from .db.session import ensure_schema

    ensure_schema()
    if job == "poll":
        from .engine.poller import Poller

        result = Poller().run_once(skip_time_check=True)
        typer.echo(f"轮询结果：{result.summary()}")
        for item in result.triggered:
            typer.echo(f"  触发 #{item['position_id']} {item['code']} {item['rule_type']}: {item['reason']}")
        if result.system_warning:
            typer.secho(result.system_warning, fg=typer.colors.RED)
    elif job == "calendar":
        from .datasource import MarketDataService

        added = MarketDataService().refresh_calendar()
        typer.echo(f"交易日历新增 {added} 天")
    elif job == "catchup":
        from .engine.catchup import CatchupService

        report = CatchupService().run()
        typer.echo(
            f"补报完成：漏触发 {report['missed_alerts']}，送达 {report['sent']}，"
            f"失败 {report['failed']}，过期 {report['expired']}"
        )
    elif job == "eod":
        from .engine.eod import run_eod

        report = run_eod(force=True)
        typer.echo(f"收盘体检：{report}")
    elif job == "screen":
        from .screening import run_screen

        result = run_screen()
        typer.echo(f"初筛完成：{result}")
    elif job == "paper":
        from .engine.paper import run_daily_review

        report = run_daily_review(force=True)
        typer.echo(f"模拟盘日报：{report}")
    elif job == "calibration":
        from .engine.calibration import run_calibration

        report = run_calibration()
        typer.echo(f"预测校准：{report}")
    elif job == "replay":
        from .engine.playbook_replay import run_playbook_replay

        report = run_playbook_replay()
        typer.echo(f"历史回放：{report}")
    elif job == "portfolio":
        from .engine.portfolio_sim import run_portfolio_replay

        report = run_portfolio_replay(
            rank_mode=(rank or "momentum"),
            drawdown_pause_pct=(breaker or None),
            drawdown_resume_pct=(breaker / 2 if breaker else None),
        )
        stats = report.get("stats") or {}
        typer.echo(
            f"组合回放（{report.get('codes', 0)} 只，排序={rank or 'momentum'}，"
            f"熔断={'关' if not breaker else f'-{breaker:g}%'}）：成交 {report.get('executed', 0)} 笔，"
            f"总收益 {stats.get('total_return_pct')}%（年化 {stats.get('annualized_pct')}%），"
            f"最大回撤 {stats.get('max_drawdown_pct')}%，胜率 {stats.get('win_rate_pct')}%，"
            f"PF {stats.get('profit_factor')}，跳过(槽位) {report.get('skipped_slots', 0)}"
        )
    elif job == "weekly":
        from .reports.weekly import weekly_report

        report = weekly_report()
        typer.echo(f"周报：{report}")
    elif job == "cleanup":
        from .engine.housekeeping import housekeeping

        removed = housekeeping()
        typer.echo(f"数据清理：{removed}")
    elif job == "backup":
        from .db.backup import backup_database

        dest = backup_database()
        typer.echo(f"已备份：{dest}")
    else:
        typer.echo(f"[stub] once {job}（对应任务实现后生效）")


@app.command()
def backup() -> None:
    """备份数据库（保留最近 30 份，data/backups/）。"""
    from .db.backup import backup_database
    from .db.session import ensure_schema

    ensure_schema()
    dest = backup_database()
    typer.echo(f"已备份：{dest}")


@app.command("paper-reset")
def paper_reset(
    yes: bool = typer.Option(False, "--yes", help="确认清空模拟盘（必须显式指定）"),
    no_backup: bool = typer.Option(False, "--no-backup", help="跳过数据库备份"),
) -> None:
    """归档并清空模拟盘（保留自选/分析卡/实盘/回放经验），从初始资金重新开始。"""
    if not yes:
        typer.secho("拒绝执行：清空模拟盘需要显式加 --yes", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    from .db.session import ensure_schema

    ensure_schema()
    if not no_backup:
        from .db.backup import backup_database

        typer.echo(f"数据库备份：{backup_database()}")
    from .engine.paper_reset import reset_paper as _reset

    result = _reset()
    typer.echo(
        f"模拟盘已重置：归档 {result['archive']}；清空 持仓 {result['positions']} / 成交 {result['trades']} / "
        f"日报 {result['reports']} / 净值快照 {result['daily']} / 日志 {result['journal']} / 经验 {result['lessons']}；"
        f"初始资金 {result['initial_cash']:.0f} 元"
    )


@app.command("paper-add")
def paper_add(
    code: str = typer.Argument(..., help="代码，如 600519"),
    qty: float = typer.Option(..., "--qty", "-q", help="持仓数量"),
    cost: float = typer.Option(..., "--cost", "-c", help="持仓成本价"),
    name: str = typer.Option("", "--name", help="名称（默认自动获取）"),
    opened_at: str = typer.Option("", "--opened-at", help="建仓日期/时间（默认现在）"),
    note: str = typer.Option("", "--note", help="备注"),
    stop_price: float = typer.Option(0.0, "--stop-price", help="机械止损价（可选）"),
    target_price: float = typer.Option(0.0, "--target-price", help="目标止盈价（可选）"),
    partial_price: float = typer.Option(0.0, "--partial-price", help="分批止盈价（可选）"),
) -> None:
    """手动导入已有持仓到模拟盘（按给定成本入账，不触发 AI 复核）。"""
    from .datasource import MarketDataService
    from .db.session import ensure_schema, get_session_factory
    from .engine.paper import PaperService

    ensure_schema()
    quote = None
    try:
        quote = MarketDataService().get_quotes([code]).get(code)
    except Exception:  # noqa: BLE001 — 行情取不到则用成本价
        quote = None
    opened = dt.datetime.fromisoformat(opened_at) if opened_at else None
    service = PaperService()
    try:
        result = service.import_position(
            code=code, qty=qty, cost=cost,
            name=name or (getattr(quote, "name", "") or ""),
            last_price=(quote.price if quote is not None and quote.price > 0 else None),
            opened_at=opened,
            note=note or "导入持仓（手动）",
            stop_price=stop_price or None,
            target_price=target_price or None,
            partial_price=partial_price or None,
        )
    except ValueError as exc:
        typer.secho(f"导入失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    with get_session_factory()() as session:
        stats = service.portfolio_stats(session)
    typer.echo(
        f"已导入 #{result['position_id']} {result['code']} {result['name']} "
        f"{result['qty']:g} 股 @ 成本 {result['avg_cost']:.4f}（费用 {result['fee']:.2f}）；"
        f"现金 {stats['cash']:.2f} / 总资产 {stats['equity']:.2f}"
    )


# ---------------------------------------------------------------- position

@position_app.command("add")
def position_add(
    code: str = typer.Argument(..., help="标的代码，如 600519 / 510300 / 011186"),
    qty: float = typer.Option(..., "--qty", "-q", help="数量（股/份）"),
    price: float = typer.Option(..., "--price", "-p", help="买入价格"),
    strategy: str = typer.Option(..., "--strategy", "-s", help="value/growth/swing/drip"),
    take_profit: float = typer.Option(..., "--take-profit", help="止盈线（%）"),
    stop_loss: float | None = typer.Option(None, "--stop-loss", help="止损线（%）"),
    trailing: float | None = typer.Option(None, "--trailing", help="移动止盈回撤（%）"),
    max_holding: int = typer.Option(..., "--max-holding", help="最长持有期（天）"),
    fee: float = typer.Option(0.0, "--fee", help="手续费"),
    name: str = typer.Option("", "--name", help="标的名称"),
    type_: str = typer.Option("", "--type", help="a_share/etf/otc_fund（默认自动推断）"),
    note: str = typer.Option("", "--note"),
) -> None:
    """建仓：强制录入退出规则（止盈 + 止损或移动止盈 + 最长持有期）。"""
    from .domain.positions import ExitRules, ValidationError

    rules = ExitRules(
        take_profit_pct=take_profit,
        stop_loss_pct=stop_loss,
        trailing_drawdown_pct=trailing,
        max_holding_days=max_holding,
    )
    try:
        result = _position_service().add_position(
            code=code, qty=qty, price=price, strategy=strategy, rules=rules,
            name=name, type=type_, fee=fee, note=note,
        )
    except (ValidationError, ValueError) as exc:
        typer.secho(f"拒绝保存：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"已建仓 #{result['position_id']} {code} 成本 {result['avg_cost']:.4f}；"
        f"规则: {', '.join(result['rules'])}"
    )


@position_app.command("list")
def position_list(
    all_: bool = typer.Option(False, "--all", help="包含已平仓"),
    quotes: bool = typer.Option(False, "--quotes", help="附带价格与浮盈亏（股票实时价/基金净值）"),
) -> None:
    """列出持仓。"""
    positions = _position_service().list_positions(include_closed=all_)
    if not positions:
        typer.echo("暂无持仓")
        return
    valuation = {"prices": {}, "labels": {}, "errors": []}
    if quotes:
        from sqlalchemy import select as _select

        from .db.models import Instrument
        from .db.session import get_session_factory
        from .domain.valuation import valuate_positions

        sf = get_session_factory()
        with sf() as session:
            types = {i.code: i.type for i in session.scalars(_select(Instrument))}
        codes_by_type = {p.code: types.get(p.code, "a_share") for p in positions}
        valuation = valuate_positions(sf, codes_by_type)
        for error in valuation["errors"]:
            typer.secho(f"行情获取失败，仅显示成本信息: {error}", fg=typer.colors.YELLOW)
    for p in positions:
        line = (
            f"#{p.id} {p.code} [{p.strategy}] 数量={p.qty:g} 成本={p.avg_cost:.4f} "
            f"建仓={p.opened_at:%Y-%m-%d} {p.status}"
        )
        price = valuation["prices"].get(p.code)
        if price is not None and p.avg_cost > 0:
            label = valuation["labels"].get(p.code, "现价")
            pnl = (price - p.avg_cost) * p.qty
            pct = (price / p.avg_cost - 1) * 100
            line += f" | {label} {price} 浮盈亏 {pnl:+.2f} ({pct:+.2f}%)"
        typer.echo(line)


@position_app.command("sell")
def position_sell(
    position_id: int = typer.Argument(..., help="持仓编号（见 position list）"),
    qty: float = typer.Option(..., "--qty", "-q", help="卖出数量"),
    price: float = typer.Option(..., "--price", "-p", help="卖出价格"),
    fee: float = typer.Option(0.0, "--fee", help="手续费"),
) -> None:
    """卖出（支持部分卖出）。"""
    from .domain.positions import NotFoundError, ValidationError

    try:
        result = _position_service().sell(position_id, qty=qty, price=price, fee=fee)
    except (ValidationError, NotFoundError) as exc:
        typer.secho(f"卖出失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"已卖出 {qty:g}；已实现盈亏 {result['realized_pnl']:+.2f}；"
        f"剩余 {result['remaining_qty']:g}（{result['status']}）"
    )


@position_app.command("split")
def position_split(
    position_id: int = typer.Argument(..., help="持仓编号"),
    factor: float = typer.Option(..., "--factor", help="拆股比例，如 2 表示 1 拆 2"),
    note: str = typer.Option("", "--note"),
) -> None:
    """拆股修正：数量 × factor，成本 ÷ factor（留痕）。"""
    from .domain.positions import NotFoundError, ValidationError

    try:
        result = _position_service().apply_split(position_id, factor=factor, note=note)
    except (ValidationError, NotFoundError) as exc:
        typer.secho(f"修正失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"已修正：数量 {result['qty']:g}，成本 {result['avg_cost']:.4f}")


# ---------------------------------------------------------------- dividend

@dividend_app.command("add")
def dividend_add(
    position_id: int = typer.Argument(..., help="持仓编号"),
    amount: float = typer.Option(..., "--amount", "-a", help="现金分红金额（元）"),
    ex_date: str = typer.Option("", "--ex-date", help="除息日 YYYY-MM-DD（默认今天）"),
    note: str = typer.Option("", "--note"),
) -> None:
    """登记分红并自动摊薄成本。"""
    from .domain.positions import NotFoundError, ValidationError

    try:
        parsed_date = dt.date.fromisoformat(ex_date) if ex_date else None
    except ValueError as exc:
        typer.secho(f"日期格式错误: {ex_date}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    try:
        result = _position_service().add_dividend(
            position_id, cash_amount=amount, ex_date=parsed_date, note=note
        )
    except (ValidationError, NotFoundError) as exc:
        typer.secho(f"分红登记失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"已登记分红 {amount:.2f}；新成本 {result['new_avg_cost']:.4f}")


# ---------------------------------------------------------------- live

@live_app.command("create")
def live_create(
    code: str = typer.Argument(..., help="代码"),
    qty: int = typer.Argument(..., help="数量（股）"),
    price: float = typer.Argument(..., help="限价"),
    side: str = typer.Option("buy", help="buy / sell"),
    name: str = typer.Option("", help="名称（ST 判断）"),
    ref_price: float = typer.Option(0.0, help="参考价（现价，用于偏离/涨跌停校验）"),
    stop_price: float = typer.Option(0.0, help="机械止损价（回填买入时换算为持仓止损%）"),
    source: str = typer.Option("manual", help="watchlist / paper / manual"),
    reason: str = typer.Option("", help="理由"),
) -> None:
    """创建实盘指令单（经过风控；人工在东财客户端下单）。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService

    ensure_schema()
    result = LiveService().create_intent(
        code, side, qty, price,
        name=name, ref_price=ref_price or None, source=source, reason=reason,
        stop_price=stop_price or None,
    )
    color = typer.colors.GREEN if result["status"] in ("pending", "confirmed") else typer.colors.RED
    typer.secho(
        f"#{result['id']} {result['side']} {result['code']} "
        f"{result['qty']} 股 @ {result['price']} → {result['status']}",
        fg=color,
    )
    for reason_text in (result.get("risk") or {}).get("reasons") or []:
        typer.echo(f"  风控：{reason_text}")


@live_app.command("list")
def live_list(
    status: str = typer.Option("", help="过滤：pending/blocked/confirmed/filled/cancelled/failed"),
    limit: int = typer.Option(20, help="条数"),
) -> None:
    """列出实盘指令单。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService

    ensure_schema()
    rows = LiveService().list_intents(status=status, limit=limit)
    if not rows:
        typer.echo("暂无指令单")
        return
    for row in rows:
        typer.echo(
            f"#{row['id']} {row['trade_date']} {row['side']} {row['code']} "
            f"{row['qty']}股 @ {row['price']} → {row['status']}（{row['source']}）"
        )


@live_app.command("confirm")
def live_confirm(intent_id: int = typer.Argument(..., help="指令单 id")) -> None:
    """人工确认指令单（半自动流程）。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService

    ensure_schema()
    try:
        row = LiveService().confirm(intent_id)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"#{row['id']} 已确认")


@live_app.command("fill")
def live_fill(
    intent_id: int = typer.Argument(..., help="指令单 id"),
    price: float = typer.Argument(..., help="实际成交价"),
    qty: int = typer.Option(0, help="实际成交数量（0=按指令数量）"),
) -> None:
    """回填实际成交（半自动流程；写入审计，后续接实盘账本）。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService

    ensure_schema()
    try:
        row = LiveService().fill(intent_id, price, qty or None)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"#{row['id']} 已成交：{row['filled_qty']} 股 @ {row['filled_price']}")


@live_app.command("cancel")
def live_cancel(
    intent_id: int = typer.Argument(..., help="指令单 id"),
    note: str = typer.Option("", help="备注"),
) -> None:
    """取消指令单。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService

    ensure_schema()
    try:
        row = LiveService().cancel(intent_id, note=note)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"#{row['id']} 已取消")


# ---------------------------------------------------------------- live：App 自动执行


@live_app.command("doctor")
def live_doctor() -> None:
    """自检：adb / 模拟器 / 东财 App 是否就绪（只读）。"""
    from .engine.live_app import AdbError, LiveAppService

    try:
        info = LiveAppService().doctor()
    except AdbError as exc:
        typer.secho(f"adb 不可用：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"adb: {info.get('adb')}")
    devices = info.get("devices") or []
    typer.echo(f"设备: {', '.join(devices) if devices else '无'}")
    if devices:
        typer.echo(f"当前前台: {info.get('current_package') or '未知'}")
    typer.echo(f"目标 App: {info.get('package')} "
               f"（{'已安装' if info.get('package_installed') else '未安装'}）")
    typer.echo(f"dry_run: {info.get('dry_run')}　截图目录: {info.get('screenshot_dir')}")
    if not devices:
        typer.secho("请先启动 MuMu 实例并开启 ADB 调试。", fg=typer.colors.YELLOW)
    elif not info.get("package_installed"):
        typer.secho(
            "模拟器里没有该包名的 App：请在 MuMu 中安装并登录东财 App，"
            "或用 `adb shell pm list packages | findstr eastmoney` 查到真实包名后改 config.live.app.package",
            fg=typer.colors.YELLOW,
        )


@live_app.command("probe")
def live_probe(save: bool = typer.Option(True, help="保存控件树与截图到 data/live_probe/")):
    """导出当前界面控件树（校准选择器用，只读）。"""
    from .engine.live_app import AdbError, LiveAppService

    try:
        info = LiveAppService().probe(save=save)
    except (AdbError, Exception) as exc:  # noqa: BLE001 — CLI 入口统一报错
        typer.secho(f"探测失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"设备 {info['device']} · 前台 {info['current_package'] or '未知'} · "
               f"{info['nodes']} 个节点（输入框 {info['editables']}）· 尺寸 {info['size']}")
    for item in info["clickable"]:
        typer.echo(f"  可点：{item['text']!r} {item['bounds']}")
    for name, path in (info.get("saved") or {}).items():
        typer.echo(f"已保存 {name}：{path}")


@live_app.command("exec")
def live_exec(
    intent_id: int = typer.Argument(..., help="指令单 id"),
) -> None:
    """自动执行一条指令单（默认 dry_run 只填单不提交）。"""
    from .db.session import ensure_schema
    from .engine.live_app import LiveAppService

    ensure_schema()
    try:
        result = LiveAppService().execute_intent(intent_id)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        typer.secho(f"执行失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    color = typer.colors.GREEN if result["submitted"] else typer.colors.YELLOW
    typer.secho(result["summary"], fg=color)
    typer.echo(f"状态 -> {result['status']}（dry_run={result['dry_run']}）")
    for shot in result["screenshots"]:
        typer.echo(f"  截图：{shot}")


@live_app.command("run")
def live_run(
    limit: int = typer.Option(3, help="单次最多执行几条"),
) -> None:
    """执行所有待执行的实盘指令单（需 live.enabled=true）。"""
    from .db.session import ensure_schema
    from .engine.live import LiveService
    from .engine.live_app import LiveAppService

    ensure_schema()
    service = LiveAppService()
    if not service._settings.app_config().get("live", {}).get("enabled"):  # noqa: SLF001
        typer.secho("拒绝执行：live.enabled=false", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    pending = [row for row in LiveService().list_intents(status="pending", limit=50)]
    if not pending:
        typer.echo("没有待执行的指令单")
        return
    for row in pending[:limit]:
        result = service.execute_intent(row["id"])
        typer.echo(result["summary"])


if __name__ == "__main__":
    app()
