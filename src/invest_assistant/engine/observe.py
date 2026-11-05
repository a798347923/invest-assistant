"""观察报表：每天收盘后记录一次纸面账户快照，用于事后归因。

为什么要单独记：`paper_daily` 表只有 cash/market_value/equity，而 equity 现在**包含停泊市值**，
不拆开就无法回答"停泊贡献了多少、配仓让波动降了多少"。这里落到 CSV（不动表结构、可随时加字段）：

    data/observations/paper_daily.csv

字段：
- cash / market_value / parking_value / equity：账户构成
- gross_exposure：股票市值 / 总资产（仓位暴露）
- avg_weight：在仓个股的平均权重（配仓开关的直接观测量）
- parking_pnl：停泊的累计盈亏（已实现 + 浮动）
- trades_cum：累计成交笔数

用法：`invest-assistant observe --days 30`
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
import os

from sqlalchemy import func, select

from ..config import get_settings
from ..db.models import PaperTrade

log = logging.getLogger(__name__)

FIELDS = [
    "date", "cash", "market_value", "parking_value", "equity",
    "positions", "gross_exposure", "avg_weight", "parking_pnl", "trades_cum",
]


def _path(settings=None) -> str:
    settings = settings or get_settings()
    folder = os.path.join(str(settings.data_dir), "observations")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "paper_daily.csv")


def build_row(session, paper) -> dict:
    """汇总当天快照（只读）。"""
    stats = paper.portfolio_stats(session)
    equity = float(stats.get("equity") or 0.0)
    market_value = float(stats.get("market_value") or 0.0)

    positions = paper.open_positions(session)
    weights = [
        (p.last_price or p.avg_cost) * p.qty / equity
        for p in positions
        if equity > 0
    ]

    # 停泊盈亏 = 已实现 + 浮动（市值 - 成本）
    from .parking import PARK_REASON, ParkingService

    parking = ParkingService(session_factory=paper._sf, settings=paper._settings)  # noqa: SLF001
    book = parking.holdings(session)
    cost = sum(v["cost"] for v in book.values())
    realized = session.execute(
        select(func.coalesce(func.sum(PaperTrade.realized_pnl), 0.0)).where(
            PaperTrade.reason.like(f"{PARK_REASON}%")
        )
    ).scalar_one()
    parking_pnl = float(realized or 0.0) + (float(stats.get("parking_value") or 0.0) - cost)

    trades_cum = session.execute(
        select(func.count()).select_from(PaperTrade)
    ).scalar_one()

    return {
        "date": dt.date.today().isoformat(),
        "cash": round(float(stats.get("cash") or 0.0), 2),
        "market_value": round(market_value, 2),
        "parking_value": round(float(stats.get("parking_value") or 0.0), 2),
        "equity": round(equity, 2),
        "positions": len(positions),
        "gross_exposure": round(market_value / equity, 4) if equity > 0 else 0.0,
        "avg_weight": round(sum(weights) / len(weights), 4) if weights else 0.0,
        "parking_pnl": round(parking_pnl, 2),
        "trades_cum": int(trades_cum or 0),
    }


def append_row(row: dict, path: str | None = None) -> str:
    """写入当天一行；同一天重复调用则覆盖那一行（幂等）。"""
    path = path or _path()
    rows: list[dict] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("date") != row["date"]]
    rows.append(row)
    rows.sort(key=lambda r: r["date"])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in FIELDS})
    return path


def snapshot(session, paper, path: str | None = None) -> dict:
    """建快照并落盘；失败只记日志，不影响日报主流程。

    路径优先取该 PaperService 自己的 settings——测试或临时会话不会污染真实
    `data/observations/`（2026-11-04 踩过：临时库数据写进了生产报表）。
    """
    try:
        row = build_row(session, paper)
        append_row(row, path=path or _path(getattr(paper, "_settings", None)))
        return row
    except Exception as exc:  # noqa: BLE001 — 观察报表不应打断日报
        log.warning("观察快照失败: %s", exc)
        return {}


def load_rows(days: int = 30, path: str | None = None) -> list[dict]:
    path = path or _path()
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows[-days:] if days > 0 else rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "还没有观察数据（服务跑过收盘日报后会产生）"
    head = (f"{'日期':12s}{'总资产':>10s}{'现金':>10s}{'股票':>9s}{'停泊':>9s}"
            f"{'暴露':>7s}{'均仓':>7s}{'停泊盈亏':>10s}")
    lines = [head]
    for r in rows:
        lines.append(
            f"{r['date']:12s}{float(r['equity']):>10.0f}{float(r['cash']):>10.0f}"
            f"{float(r['market_value']):>9.0f}{float(r['parking_value']):>9.0f}"
            f"{float(r['gross_exposure']) * 100:>6.0f}%{float(r['avg_weight']) * 100:>6.0f}%"
            f"{float(r['parking_pnl']):>+10.0f}"
        )
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        delta = float(last["equity"]) - float(first["equity"])
        base = float(first["equity"]) or 1.0
        lines.append(
            f"区间：{first['date']} → {last['date']}，总资产 {delta:+.0f}（{delta / base * 100:+.2f}%），"
            f"停泊盈亏 {float(last['parking_pnl']) - float(first['parking_pnl']):+.0f}"
        )
    return "\n".join(lines)
