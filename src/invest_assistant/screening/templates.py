"""初筛模板：定义 + 过滤/评分。

v1 基于全市场快照指标（PE/PB/市值/换手/涨跌幅；EM 或新浪提供）。
基本面时点数据（营收/利润增速、ROE）需要公告日级数据，属二期。
"""

from __future__ import annotations

from sqlalchemy import select

from ..db.models import ScreenTemplate

DEFAULT_TEMPLATES = [
    {
        "name": "value",
        "description": "价值：低估值 + 规模门槛（0<PE≤25，0<PB≤3，市值≥50亿，换手≤10%）",
        "params": {"pe_max": 25, "pb_max": 3, "min_cap": 50, "max_turnover": 10},
    },
    {
        "name": "growth",
        "description": "成长：估值容忍 + 活跃度（PE≤60，PB≤8，市值≥100亿，换手1~15%，今日跌幅>-3%）",
        "params": {
            "pe_max": 60,
            "pb_max": 8,
            "min_cap": 100,
            "turnover_min": 1,
            "turnover_max": 15,
            "pct_min": -3,
        },
    },
    {
        "name": "trend",
        "description": "趋势：放量上攻（今日涨幅≥2%，换手2~20%，市值≥80亿，PE>0 非亏损）",
        "params": {"pct_min": 2, "min_cap": 80, "turnover_min": 2, "turnover_max": 20},
    },
]


def ensure_templates(session_factory) -> None:
    """种子模板（幂等；已存在同名模板则不动）。"""
    with session_factory() as session:
        existing = {t.name for t in session.scalars(select(ScreenTemplate))}
        for item in DEFAULT_TEMPLATES:
            if item["name"] not in existing:
                session.add(
                    ScreenTemplate(name=item["name"], params_json=item["params"], enabled=True)
                )
        session.commit()


def _excluded(name: str) -> bool:
    upper = name.upper()
    return "ST" in upper or upper.startswith("N") or "退" in name


def filter_row(template_name: str, params: dict, row: dict) -> tuple[bool, list[str], float]:
    """返回 (是否通过, 命中理由, 评分)。评分仅用于按模板排序取 Top N。"""
    name = str(row.get("name") or "")
    if _excluded(name):
        return False, [], 0.0
    price = row.get("price") or 0.0
    pe = row.get("pe") or 0.0
    pb = row.get("pb") or 0.0
    cap = row.get("market_cap") or 0.0
    turnover = row.get("turnover") or 0.0
    pct = row.get("pct") or 0.0

    if template_name == "value":
        pe_max = float(params.get("pe_max", 25))
        pb_max = float(params.get("pb_max", 3))
        min_cap = float(params.get("min_cap", 50))
        max_turnover = float(params.get("max_turnover", 10))
        if not (0 < pe <= pe_max):
            return False, [], 0.0
        if not (0 < pb <= pb_max):
            return False, [], 0.0
        if cap < min_cap:
            return False, [], 0.0
        if turnover > max_turnover:
            return False, [], 0.0
        reasons = [
            f"现价 {price:.2f}",
            f"PE {pe:.1f} ≤ {pe_max:g}",
            f"PB {pb:.2f} ≤ {pb_max:g}",
            f"市值 {cap:.0f} 亿 ≥ {min_cap:g} 亿",
            f"换手 {turnover:.1f}% ≤ {max_turnover:g}%",
        ]
        score = 100 - pe * 2 - pb * 4 + min(cap, 3000) / 100
        return True, reasons, round(max(score, 0.1), 4)

    if template_name == "growth":
        pe_max = float(params.get("pe_max", 60))
        pb_max = float(params.get("pb_max", 8))
        min_cap = float(params.get("min_cap", 100))
        t_min = float(params.get("turnover_min", 1))
        t_max = float(params.get("turnover_max", 15))
        pct_min = float(params.get("pct_min", -3))
        if not (0 < pe <= pe_max):
            return False, [], 0.0
        if not (0 < pb <= pb_max):
            return False, [], 0.0
        if cap < min_cap:
            return False, [], 0.0
        if not (t_min <= turnover <= t_max):
            return False, [], 0.0
        if pct < pct_min:
            return False, [], 0.0
        reasons = [
            f"现价 {price:.2f}",
            f"PE {pe:.1f} ≤ {pe_max:g}",
            f"PB {pb:.2f} ≤ {pb_max:g}",
            f"市值 {cap:.0f} 亿 ≥ {min_cap:g} 亿",
            f"换手 {turnover:.1f}% 在 {t_min:g}~{t_max:g}%",
            f"今日涨幅 {pct:+.2f}% ≥ {pct_min:g}%",
        ]
        score = min(turnover, 15) * 3 + min(cap, 3000) / 200
        return True, reasons, round(max(score, 0.1), 4)

    if template_name == "trend":
        pct_min = float(params.get("pct_min", 2))
        min_cap = float(params.get("min_cap", 80))
        t_min = float(params.get("turnover_min", 2))
        t_max = float(params.get("turnover_max", 20))
        if pct < pct_min:
            return False, [], 0.0
        if cap < min_cap:
            return False, [], 0.0
        if not (t_min <= turnover <= t_max):
            return False, [], 0.0
        if pe <= 0:
            return False, [], 0.0
        reasons = [
            f"现价 {price:.2f}",
            f"今日涨幅 {pct:+.2f}% ≥ {pct_min:g}%",
            f"市值 {cap:.0f} 亿 ≥ {min_cap:g} 亿",
            f"换手 {turnover:.1f}% 在 {t_min:g}~{t_max:g}%",
            "PE>0（非亏损）",
        ]
        score = pct * 5 + min(turnover, 20)
        return True, reasons, round(max(score, 0.1), 4)

    return False, [], 0.0
