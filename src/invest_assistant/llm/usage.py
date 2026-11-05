"""LLM 用量与成本统计（llm_usage 表）。"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from ..config import get_settings
from ..db.models import LLMUsage

# 元/百万 tokens；以 config.yaml 的 llm.pricing 覆盖（按官网价维护）
PRICING_DEFAULT = {"input": 2.0, "output": 8.0}


def _pricing() -> dict:
    settings = get_settings()
    llm_cfg = settings.app_config().get("llm") or {}
    pricing = dict(PRICING_DEFAULT)
    for key, value in (llm_cfg.get("pricing") or {}).items():
        try:
            pricing[key] = float(value)
        except (TypeError, ValueError):
            continue
    return pricing


def estimate_cost(tokens_in: int, tokens_out: int, pricing: dict | None = None) -> float:
    price = pricing or _pricing()
    return round(
        tokens_in / 1_000_000 * float(price.get("input", 0))
        + tokens_out / 1_000_000 * float(price.get("output", 0)),
        6,
    )


def record_usage(
    session_factory,
    *,
    purpose: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cached_in: int = 0,
) -> float:
    """记录一次调用；返回成本估算（元）。"""
    cost = estimate_cost(tokens_in, tokens_out)
    with session_factory() as session:
        session.add(
            LLMUsage(
                ts=dt.datetime.now(),
                purpose=purpose,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_in=cached_in,
                cost_est=cost,
            )
        )
        session.commit()
    return cost


def daily_cost_summary(session_factory, days: int = 7) -> dict:
    """近 N 天用量；含今日成本与软阈值状态（只提示不降级）。"""
    with session_factory() as session:
        rows = session.execute(
            select(
                func.date(LLMUsage.ts),
                func.coalesce(func.sum(LLMUsage.cost_est), 0.0),
                func.coalesce(func.sum(LLMUsage.tokens_in + LLMUsage.tokens_out), 0),
            )
            .group_by(func.date(LLMUsage.ts))
            .order_by(func.date(LLMUsage.ts).desc())
            .limit(days)
        ).all()
    items = [
        {"date": str(row[0]), "cost": round(float(row[1]), 4), "tokens": int(row[2])}
        for row in rows
    ]
    today = dt.date.today().isoformat()
    today_cost = next((item["cost"] for item in items if item["date"] == today), 0.0)
    llm_cfg = get_settings().app_config().get("llm") or {}
    threshold = float(llm_cfg.get("soft_daily_cost_alert_cny", 5) or 5)
    return {
        "days": items,
        "today_cost": today_cost,
        "threshold": threshold,
        "over_threshold": today_cost > threshold,
    }
