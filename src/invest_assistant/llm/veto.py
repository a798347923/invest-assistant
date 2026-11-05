"""交易执行前的 AI 复核（否决制）。

每次买入/卖出前让模型快速确认（小提示词、低成本），返回：
- approve=true → 放行；
- approve=false → 取消本次操作（reason 必须引用具体数据）。

调用失败/超时由调用方按策略处理（买入保守取消、卖出按原计划执行、止损默认豁免）。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, field_validator

from ..db.session import get_session_factory
from .client import LLMClient, LLMError
from .prompts import VETO_V1_SYSTEM, veto_user
from .usage import record_usage

log = logging.getLogger(__name__)

_TRUE_WORDS = {"true", "1", "同意", "是", "通过", "approve", "yes"}
_FALSE_WORDS = {"false", "0", "反对", "否", "否决", "reject", "no"}


class VetoRead(BaseModel):
    approve: bool = True
    reason: str = ""

    @field_validator("approve", mode="before")
    @classmethod
    def _coerce_approve(cls, value):
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        raise ValueError(f"无法解析 approve: {value!r}")

    @field_validator("reason", mode="before")
    @classmethod
    def _coerce_reason(cls, value):
        return "" if value is None else str(value)


class VetoReviewer:
    """AI 复核员：返回 {"approve": bool, "reason": str}；未配置 LLM 返回 None。"""

    def __init__(self, llm=None, session_factory=None):
        self.llm = llm or LLMClient()
        self._sf = session_factory or get_session_factory()

    @property
    def configured(self) -> bool:
        return bool(self.llm.configured)

    def review(self, payload: dict) -> dict | None:
        if not self.configured:
            return None
        try:
            result = self.llm.complete_json(
                purpose="veto",
                system=VETO_V1_SYSTEM,
                user=veto_user(payload),
                schema=VetoRead,
                max_tokens=256,
                temperature=0.2,
            )
            record_usage(
                self._sf,
                purpose="veto",
                model=result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cached_in=result.cached_in,
            )
            return result.content
        except LLMError as exc:
            log.warning("AI 复核失败: %s", exc)
            return None
