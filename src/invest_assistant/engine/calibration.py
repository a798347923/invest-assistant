"""预测校准闭环：分析卡「预测」→ 结果自动评估 → 分组统计「在什么条件下容易错」→ 注入提示词。

- record_prediction：卡生成时记录（代码/建议/计划价位/当时大盘与个股状态）；
- evaluate_pending：成熟预测（≥20 个自然日）自动评分：5/10/20 日前向收益、
  回踩/突破是否触发、触发后 5 日表现与失效价是否被跌破、回避是否正确；
- calibration_stats：按（大盘状态 × 建议类型）分桶统计；findings 生成可执行的校准结论；
- run_calibration：每周执行（评估 + 统计 + 存报告 + 推送）；首次自动回填历史分析卡；
- calibration_notes：把最新报告结论注入后续 分析卡 / 日报 / 收盘体检 的提示词。
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..datasource import MarketDataService
from ..db.models import AnalysisCard, CalibrationReport, Prediction
from ..db.session import get_session_factory
from ..domain.indicators import percentile_rank
from ..domain.plan import parse_plan_levels
from ..notify import Message, build_dispatcher

log = logging.getLogger(__name__)

TRIGGER_WINDOW = 10  # 触发窗口（交易日）
MATURE_DAYS = 20  # 预测满 20 个自然日后评估
EXPIRE_DAYS = 60  # 超过 60 天仍无法评估（长期停牌等）作废
INDEX_SYMBOL = "sh000001"


def _classify(action: str) -> str:
    """建议类型归一：回踩 / 突破 / 回避 / 现价分批 / 观察。"""
    text = str(action or "")
    if "回避" in text:
        return "回避"
    if "回踩" in text or "回调" in text:
        return "回踩"
    if "突破" in text:
        return "突破"
    if "分批" in text or "现价" in text or "建仓" in text:
        return "现价分批"
    return "观察"


def _score(prediction: Prediction, bars: list) -> dict | None:
    """对单条预测评分；数据不足返回 None（保持 pending）。"""
    forward = [b for b in bars if b.date >= prediction.date]
    if len(forward) < 6:
        return None
    p0 = prediction.price or forward[0].close
    if not p0:
        return None

    def _ret(offset: int) -> float | None:
        if len(forward) > offset:
            return round((forward[offset].close / p0 - 1) * 100, 2)
        return None

    outcome: dict = {"ret5": _ret(5), "ret10": _ret(10), "ret20": _ret(20)}
    action = _classify(prediction.action)
    window = forward[: TRIGGER_WINDOW + 1]

    if action == "回踩" and prediction.zone_high:
        trigger_idx = next(
            (i for i, b in enumerate(window) if b.low <= prediction.zone_high), None
        )
        entry_ref = prediction.zone_high
    elif action == "突破" and prediction.breakout:
        trigger_idx = next(
            (i for i, b in enumerate(window) if b.high >= prediction.breakout), None
        )
        entry_ref = prediction.breakout
    else:
        trigger_idx, entry_ref = None, None

    if action in ("回踩", "突破") and entry_ref:
        outcome["triggered"] = trigger_idx is not None
        if trigger_idx is None:
            outcome["verdict"] = (
                "未触发（同期上涨，偏保守）"
                if (outcome.get("ret20") or 0) > 5
                else "未触发"
            )
        else:
            outcome["trigger_day"] = trigger_idx
            post = forward[trigger_idx:]
            if len(post) > 5:
                outcome["post_entry_ret5"] = round(
                    (post[5].close / entry_ref - 1) * 100, 2
                )
                outcome["post_entry_low"] = round(
                    (min(b.low for b in post[:6]) / entry_ref - 1) * 100, 2
                )
            stop_hit = prediction.stop is not None and any(
                b.low <= prediction.stop for b in post[:6]
            )
            if stop_hit:
                outcome["verdict"] = "触发后跌破失效价"
            elif (outcome.get("post_entry_ret5") or 0) >= 3:
                outcome["verdict"] = "命中后走强"
            elif outcome.get("post_entry_ret5") is None:
                outcome["verdict"] = "命中（数据不足）"
            else:
                outcome["verdict"] = (
                    "命中后走弱" if outcome["post_entry_ret5"] <= -3 else "命中后震荡"
                )
        return outcome

    if action == "回避":
        ret10 = outcome.get("ret10")
        if ret10 is None:
            outcome["verdict"] = "中性"
        elif ret10 <= -3:
            outcome["verdict"] = "回避正确"
        elif ret10 >= 3:
            outcome["verdict"] = "回避错失"
        else:
            outcome["verdict"] = "回避中性"
    else:
        outcome["verdict"] = "中性"
    return outcome


def backfill_predictions(
    session_factory=None, data=None, days: int = 90, limit: int = 200
) -> int:
    """首次运行时回填历史分析卡为预测（近 days 天、非基金卡）。"""
    sf = session_factory or get_session_factory()
    data = data or MarketDataService(session_factory=sf)
    since = dt.datetime.now() - dt.timedelta(days=days)
    with sf() as session:
        cards = session.scalars(
            select(AnalysisCard)
            .where(AnalysisCard.created_at >= since, AnalysisCard.kind == "stock")
            .order_by(AnalysisCard.created_at)
            .limit(limit)
        ).all()
        existing = {p.card_id for p in session.scalars(select(Prediction))}

    index_closes: list[tuple[dt.date, float]] = []
    try:
        end = dt.date.today()
        bars = data.tencent.get_daily_bars(
            INDEX_SYMBOL, end - dt.timedelta(days=days + 120), end, adjust="raw"
        )
        index_closes = [(b.date, b.close) for b in bars]
    except Exception as exc:  # noqa: BLE001 — 指数不可用则 regime 留空
        log.debug("回填取指数失败: %s", exc)

    created = 0
    for card in cards:
        if card.id in existing:
            continue
        card_day = card.created_at.date()
        try:
            bars = data.get_daily_bars(
                card.code, card_day - dt.timedelta(days=1100), card_day, adjust="qfq"
            )
        except Exception:  # noqa: BLE001
            continue
        up_to = [b for b in bars if b.date <= card_day]
        if not up_to:
            continue
        closes = [b.close for b in up_to]
        plan = parse_plan_levels(card.content_json or {})
        predictor = _prediction_row(
            card_day,
            card.code,
            card.id,
            str((card.content_json or {}).get("action_bias") or ""),
            closes,
            plan,
            _regime_at(index_closes, card_day),
        )
        with sf() as session:
            session.add(predictor)
            session.commit()
        created += 1
    if created:
        log.info("已回填 %s 条历史预测", created)
    return created


def _regime_at(index_closes: list[tuple[dt.date, float]], day: dt.date) -> str:
    points = [c for d, c in index_closes if d <= day and (day - d).days <= 40]
    if len(points) < 20:
        return ""
    ma20 = sum(points[-20:]) / 20
    return "指数≥20日线" if points[-1] >= ma20 else "指数<20日线"


def _prediction_row(
    day: dt.date,
    code: str,
    card_id: int | None,
    action: str,
    closes: list[float],
    plan: dict | None,
    regime: str,
):
    plan = plan or {}
    trend = ""
    if len(closes) >= 20:
        ma20 = sum(closes[-20:]) / 20
        trend = "多头(>MA20)" if closes[-1] >= ma20 else "空头(<MA20)"
    pct = percentile_rank(closes, closes[-1]) if len(closes) >= 60 else None
    return Prediction(
        date=day,
        code=code,
        card_id=card_id,
        action=action,
        price=closes[-1],
        zone_low=plan.get("zone_low"),
        zone_high=plan.get("zone_high"),
        breakout=plan.get("breakout"),
        stop=plan.get("stop"),
        regime=regime,
        trend=trend,
        price_pct=pct,
    )


def evaluate_pending(
    session_factory=None, data=None, now: dt.datetime | None = None
) -> dict:
    """评估到期的 pending 预测（按代码批量取 K 线）。"""
    sf = session_factory or get_session_factory()
    data = data or MarketDataService(session_factory=sf)
    now = now or dt.datetime.now()
    with sf() as session:
        pending = session.scalars(
            select(Prediction).where(Prediction.status == "pending")
        ).all()
        todo = [
            (p.id, p.code, p.date)
            for p in pending
            if (now.date() - p.date).days >= MATURE_DAYS
        ]
    if not todo:
        return {"evaluated": 0, "expired": 0}

    codes = sorted({code for _, code, _ in todo})
    earliest = min(day for _, _, day in todo)
    bars_cache: dict[str, list] = {}
    for code in codes:
        try:
            bars_cache[code] = data.get_daily_bars(
                code, earliest - dt.timedelta(days=5), now.date(), adjust="qfq"
            )
        except Exception as exc:  # noqa: BLE001 — 单只失败保持 pending
            log.debug("校准取日线失败 %s: %s", code, exc)
            bars_cache[code] = []

    evaluated = expired = 0
    with sf() as session:
        for pred_id, code, day in todo:
            prediction = session.get(Prediction, pred_id)
            if prediction is None:
                continue
            outcome = _score(prediction, bars_cache.get(code) or [])
            if outcome is None:
                if (now.date() - day).days > EXPIRE_DAYS:
                    prediction.status = "expired"
                    expired += 1
                continue
            prediction.outcome_json = outcome
            prediction.status = "evaluated"
            evaluated += 1
        session.commit()
    return {"evaluated": evaluated, "expired": expired}


def _avg(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def calibration_stats(session_factory=None, days: int = 90) -> dict:
    """按（大盘状态 × 建议类型）分桶统计已评估预测。"""
    sf = session_factory or get_session_factory()
    since = dt.date.today() - dt.timedelta(days=days)
    with sf() as session:
        rows = session.scalars(
            select(Prediction).where(
                Prediction.status == "evaluated", Prediction.date >= since
            )
        ).all()
        buckets: dict[tuple[str, str], list[Prediction]] = {}
        for row in rows:
            buckets.setdefault((_classify(row.action), row.regime or "未知大盘"), []).append(row)

        stats: dict[str, dict] = {}
        for (action, regime), items in sorted(buckets.items()):
            outcomes = [p.outcome_json or {} for p in items]
            triggers = [o["triggered"] for o in outcomes if "triggered" in o]
            avoid = [o["verdict"] for o in outcomes if str(o.get("verdict", "")).startswith("回避")]
            stats[f"{action}｜{regime}"] = {
                "n": len(items),
                "ret5": _avg([o.get("ret5") for o in outcomes]),
                "ret10": _avg([o.get("ret10") for o in outcomes]),
                "ret20": _avg([o.get("ret20") for o in outcomes]),
                "trigger_rate": round(sum(1 for t in triggers if t) / len(triggers) * 100, 0)
                if triggers
                else None,
                "post_entry_ret5": _avg([o.get("post_entry_ret5") for o in outcomes]),
                "avoid_correct_rate": round(
                    sum(1 for v in avoid if v == "回避正确") / len(avoid) * 100, 0
                )
                if avoid
                else None,
            }
        return {"window_days": days, "total": len(rows), "buckets": stats}


def _build_findings(stats: dict, min_samples: int = 5) -> list[str]:
    """统计 → 可执行结论（确定性规则，不经过 LLM）。"""
    findings: list[str] = []
    for key, bucket in stats.get("buckets", {}).items():
        if bucket["n"] < min_samples:
            continue
        action, regime = key.split("｜", 1)
        parts = [f"{action}｜{regime}：{bucket['n']} 例"]
        if bucket["ret10"] is not None:
            parts.append(f"建议后 10 日平均 {bucket['ret10']:+.1f}%")
        if bucket.get("trigger_rate") is not None:
            parts.append(f"触发率 {bucket['trigger_rate']:.0f}%")
        if bucket.get("post_entry_ret5") is not None:
            parts.append(f"触发后 5 日平均 {bucket['post_entry_ret5']:+.1f}%")
        if bucket.get("avoid_correct_rate") is not None:
            parts.append(f"回避正确率 {bucket['avoid_correct_rate']:.0f}%")
        findings.append("；".join(parts))

    by_key = stats.get("buckets", {})
    for key, bucket in by_key.items():
        if bucket["n"] < min_samples:
            continue
        action, regime = key.split("｜", 1)
        if "指数<20日线" in regime and action == "突破" and (bucket["ret10"] or 0) < 0:
            findings.append("→ 弱势市场（指数<20日线）追突破统计为负：弱市应放弃突破型买点或仓位减半")
        if action == "回踩" and bucket.get("trigger_rate") is not None and bucket["trigger_rate"] < 35:
            findings.append(
                f"→「等待回踩」多数未触发（{bucket['trigger_rate']:.0f}%）：可考虑放宽区间上沿，或对强势股改用现价分批"
            )
        if action == "回避" and (bucket.get("avoid_correct_rate") or 0) >= 60:
            findings.append("→ 回避类建议多数正确：弱势环境下应延续回避，不要抢反弹")
        if action == "现价分批" and bucket["ret10"] is not None and bucket["ret10"] < -1:
            findings.append("→ 现价/分批建议统计偏弱：优先等待回踩或突破确认后再买入")
        if action == "回踩" and (bucket.get("post_entry_ret5") or 0) >= 3:
            findings.append("→ 回踩触发后走强概率较高：回踩确认仍是有效的入场方式，维持纪律执行")

    seen: set[str] = set()
    unique: list[str] = []
    for line in findings:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique[:8]


def calibration_notes(session_factory, limit: int = 3) -> list[str]:
    """最新校准报告中的结论（注入提示词用）；无报告返回 []。"""
    sf = session_factory
    try:
        with sf() as session:
            report = session.scalars(
                select(CalibrationReport).order_by(CalibrationReport.date.desc()).limit(1)
            ).first()
    except Exception:  # noqa: BLE001
        return []
    if report is None or not isinstance(report.content_json, dict):
        return []
    return [str(x) for x in (report.content_json.get("findings") or [])[:limit]]


def run_calibration(
    session_factory=None,
    data=None,
    dispatcher=None,
    now: dt.datetime | None = None,
    days: int = 90,
    notify: bool = True,
) -> dict:
    """每周校准：评估到期预测（首次自动回填历史卡）→ 统计 → 存报告 → 推送。"""
    sf = session_factory or get_session_factory()
    data = data or MarketDataService(session_factory=sf)
    now = now or dt.datetime.now()

    backfilled = 0
    with sf() as session:
        has_any = session.scalar(select(Prediction.id).limit(1))
    if has_any is None:
        try:
            backfilled = backfill_predictions(session_factory=sf, data=data)
        except Exception:  # noqa: BLE001 — 回填失败不阻塞
            log.exception("历史预测回填失败")

    evaluated = evaluate_pending(session_factory=sf, data=data, now=now)
    stats = calibration_stats(session_factory=sf, days=days)
    findings = _build_findings(stats)
    summary = "；".join(findings[:3]) or "样本积累中：暂无可用的校准结论（继续生成分析卡即可自动积累）"

    with sf() as session:
        day = now.date()
        row = session.scalars(
            select(CalibrationReport).where(CalibrationReport.date == day)
        ).first()
        if row is None:
            row = CalibrationReport(date=day)
            session.add(row)
        row.summary = summary
        row.content_json = {"stats": stats, "findings": findings}
        session.commit()

    sent = False
    if notify:
        try:
            dispatcher = dispatcher or build_dispatcher()
            if dispatcher.configured:
                body_lines = [summary, ""]
                body_lines.extend(f"- {x}" for x in findings)
                body_lines.append("")
                body_lines.append(
                    f"样本：已评估 {stats['total']} 条（近 {days} 天分析卡），"
                    f"本次新评估 {evaluated['evaluated']} 条，回填 {backfilled} 条。"
                )
                body_lines.append(
                    "结论来自对历史建议的统计校准（非 LLM 生成），会自动注入后续分析提示词。"
                )
                results = dispatcher.send(
                    Message(title="【预测校准】建议质量周报", body="\n".join(body_lines))
                )
                sent = any(r.ok for r in results)
        except Exception as exc:  # noqa: BLE001 — 推送失败不影响落库
            log.warning("校准报告推送失败: %s", exc)

    report = {
        "date": now.date().isoformat(),
        "backfilled": backfilled,
        "evaluated": evaluated["evaluated"],
        "expired": evaluated["expired"],
        "samples": stats["total"],
        "findings": len(findings),
        "notified": sent,
    }
    log.info("预测校准完成: %s", report)
    return report
