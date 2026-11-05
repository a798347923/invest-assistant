"""初筛执行：快照 → 逐模板过滤 → 买点评估 → 候选池入库（同日幂等）。

两条路径：
- 生产路径（snapshot=None）：三套模板过滤合并候选池 → 按模板配额抽查「买点评估」
  （回踩支撑/突破在即/超跌企稳，见 setup.py）→ 三模板轮转取全局 Top N
  （config.screen.daily_candidates，默认 5），保证候选多样性；
  setup_required=true 时只保留有明确买点的候选；
- 离线/测试路径（注入 snapshot 且未传 setup_fn）：保持旧语义（每模板 Top N），
  买点评估函数可通过 setup_fn 注入以便测试。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..db.models import Candidate, ScreenTemplate
from ..db.session import get_session_factory
from .experience import apply_experience
from .templates import ensure_templates, filter_row

log = logging.getLogger(__name__)


def _default_setup_fn(session_factory):
    """默认买点评估：拉取日线（自缓存）→ evaluate_buy_setup。"""
    from ..datasource import MarketDataService
    from .setup import evaluate_buy_setup

    service = MarketDataService(session_factory=session_factory)

    def evaluate(code: str) -> dict:
        end = dt.date.today()
        start = end - dt.timedelta(days=160)
        bars = service.get_daily_bars(code, start, end, adjust="qfq")
        return evaluate_buy_setup(bars)

    return evaluate


def _screen_cfg() -> dict:
    from ..config import get_settings

    return get_settings().app_config().get("screen") or {}


def run_screen(
    session_factory=None,
    snapshot: list[dict] | None = None,
    top_n: int | None = None,
    trade_date: dt.date | None = None,
    force_snapshot: bool = False,
    setup_fn=None,
    setup_required: bool | None = None,
    regime: str | None = None,
) -> dict:
    """执行初筛。`snapshot` / `setup_fn` 可注入（测试）；top_n 缺省取 config.screen.daily_candidates。

    regime: "strong"/"weak" 可注入；生产路径自动取大盘状态用于经验校准（取不到则不调整）。
    """
    sf = session_factory or get_session_factory()
    trade_date = trade_date or dt.date.today()
    if top_n is None:
        top_n = int(_screen_cfg().get("daily_candidates", 5) or 5)

    source = "inline"
    if snapshot is None:
        from ..datasource import MarketDataService

        service = MarketDataService(session_factory=sf)
        rows, source = service.get_market_snapshot(force=force_snapshot)
        if setup_fn is None:
            setup_fn = _default_setup_fn(sf)
        if regime is None:
            try:
                state = service.market_regime_state()
                regime = state if state in ("strong", "weak") else ""
            except Exception:  # noqa: BLE001 — 大盘数据失败则不做经验调整
                regime = ""
    else:
        rows = snapshot
        if regime is None:
            regime = ""

    ensure_templates(sf)
    created = 0
    by_template: dict[str, int] = {}
    setup_stats: dict[str, int] = {}
    experience_notes: list[str] = []
    with sf() as session:
        templates = session.scalars(
            select(ScreenTemplate).where(ScreenTemplate.enabled.is_(True))
        ).all()

        # 阶段一：模板过滤（按代码合并候选池，保留最高分模板）
        passed_by_template: dict[int, tuple] = {}
        pool: dict[str, dict] = {}
        for template in templates:
            passed: list[tuple[float, dict, list[str]]] = []
            for row in rows:
                ok, reasons, score = filter_row(template.name, template.params_json or {}, row)
                if ok:
                    passed.append((score, row, reasons))
            passed.sort(key=lambda item: item[0], reverse=True)
            passed_by_template[template.id] = (template, passed)
            for score, row, reasons in passed:
                code = row["code"]
                current = pool.get(code)
                if current is None or score > current["score"]:
                    pool[code] = {
                        "score": score,
                        "row": row,
                        "reasons": reasons,
                        "template": template,
                    }

        def _insert(entry: dict, setup: dict | None, score: float) -> None:
            nonlocal created
            row = entry["row"]
            code = row["code"]
            reasons = list(entry["reasons"])
            payload: dict = {
                "reasons": reasons,
                "name": row.get("name", ""),
                "price": row.get("price"),
                "pct": row.get("pct"),
            }
            if setup is not None:
                reasons.append(f"买点：{setup['label']}｜{setup['note']}")
                payload["setup"] = setup
            exists = session.execute(
                select(Candidate.id).where(
                    Candidate.date == trade_date,
                    Candidate.code == code,
                    Candidate.template_id == entry["template"].id,
                )
            ).first()
            if exists:
                return
            session.add(
                Candidate(
                    date=trade_date,
                    code=code,
                    template_id=entry["template"].id,
                    score=score,
                    reasons_json=payload,
                    status="new",
                )
            )
            created += 1
            name = entry["template"].name
            by_template[name] = by_template.get(name, 0) + 1

        # 阶段二：买点评估
        picks: list[dict] = []
        if setup_fn is not None and pool:
            cfg = _screen_cfg()
            if setup_required is None:
                setup_required = bool(cfg.get("setup_required", True))
            limit = int(cfg.get("setup_eval_limit", 40) or 40)
            # 各模板评分不可跨类比较，按模板配额取评估名单，避免单一模板挤占
            per_cap = max(5, limit // max(len(templates), 1))
            selected: list[dict] = []
            counts: dict[str, int] = {}
            for entry in sorted(pool.values(), key=lambda e: e["score"], reverse=True):
                name = entry["template"].name
                if counts.get(name, 0) < per_cap:
                    counts[name] = counts.get(name, 0) + 1
                    selected.append(entry)
            evaluated = 0
            for entry in selected:
                try:
                    entry["setup"] = setup_fn(entry["row"]["code"])
                except Exception as exc:  # noqa: BLE001 — 单只失败跳过
                    log.warning("买点评估失败 %s: %s", entry["row"]["code"], exc)
                    entry["setup"] = None
                if entry["setup"] is not None:
                    evaluated += 1
            if evaluated == 0:
                log.warning("买点评估数据全部不可用，退化为模板评分选择")
                setup_fn = None
            else:
                if setup_required:
                    selected = [
                        e
                        for e in selected
                        if e.get("setup") and e["setup"].get("label") != "无明确买点"
                    ]
                # 经验校准（历史回放）：弱市剔除突破型、回踩降权；强市突破升权等
                selected, experience_notes = apply_experience(selected, regime)
                # 轮转取数（价值→成长→趋势循环），保证每日候选的多样性
                buckets: dict[str, list[dict]] = {}
                for entry in selected:
                    buckets.setdefault(entry["template"].name, []).append(entry)
                for bucket in buckets.values():
                    bucket.sort(
                        key=lambda e: (
                            (e.get("setup") or {}).get("bonus", 0.0),
                            e["score"],
                        ),
                        reverse=True,
                    )
                picks = []
                order = [t.name for t in templates]
                max_picks = max(top_n, 0)
                if regime == "weak":
                    # 经验：弱市收紧候选数量
                    max_picks = min(max_picks, int(cfg.get("weak_market_candidates", 3) or 3))
                while len(picks) < max_picks:
                    progressed = False
                    for name in order:
                        bucket = buckets.get(name) or []
                        if not bucket:
                            continue
                        picks.append(bucket.pop(0))
                        progressed = True
                        if len(picks) >= max_picks:
                            break
                    if not progressed:
                        break
                for entry in picks:
                    setup = entry.get("setup") or {}
                    label = setup.get("label", "未知")
                    setup_stats[label] = setup_stats.get(label, 0) + 1

        if setup_fn is not None:
            for entry in picks:
                setup = entry.get("setup")
                bonus = (setup or {}).get("bonus", 0.0)
                combined = round(entry["score"] / 10 + bonus * 10, 4)
                _insert(entry, setup, combined)
        else:
            # 旧路径（离线/测试）：每模板 Top N
            for template in templates:
                _, passed = passed_by_template[template.id]
                for score, row, reasons in passed[: max(top_n, 0)]:
                    _insert(
                        {"score": score, "row": row, "reasons": reasons, "template": template},
                        None,
                        score,
                    )
        session.commit()

    result = {
        "date": trade_date.isoformat(),
        "source": source,
        "snapshot_rows": len(rows),
        "created": created,
        "by_template": by_template,
        "setup": setup_stats,
        "experience": experience_notes,
    }
    log.info("初筛完成: %s", result)
    return result
