"""OpenAI 兼容 LLM 客户端（默认 DeepSeek）。

- `complete_json`：强制 JSON 输出 + pydantic 校验（失败自动重试 1 次）；
- 传输层失败：tenacity 退避重试（2 次）+ OpenAI 客户端硬超时（总 90s / 连接 15s，禁用 SDK 自带重试）；
- 用量（含缓存命中）由返回值携带，由调用方经 `usage.record_usage` 落库。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ..config import get_settings

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"


class LLMError(Exception):
    """LLM 调用或解析失败（调用方负责降级）。"""


@dataclass
class LLMResult:
    content: dict
    raw: str
    model: str
    tokens_in: int
    tokens_out: int
    cached_in: int
    ms: int


def _cached_tokens(usage: Any) -> int:
    if usage is None:
        return 0
    value = getattr(usage, "prompt_cache_hit_tokens", None)
    if isinstance(value, int):
        return value
    details = getattr(usage, "prompt_tokens_details", None)
    try:
        return int(getattr(details, "cached_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


class LLMClient:
    def __init__(self, settings=None, raw_client=None, base_url: str | None = None):
        self._settings = settings or get_settings()
        llm_cfg = self._settings.app_config().get("llm") or {}
        self._model = str(llm_cfg.get("model", "deepseek-chat"))
        self._base_url = base_url or DEFAULT_BASE_URL
        self._raw = raw_client  # 测试注入；None 时懒加载 openai.OpenAI

    @property
    def configured(self) -> bool:
        return bool(self._settings.deepseek_api_key) or self._raw is not None

    def _get_raw(self):
        if self._raw is None:
            from openai import OpenAI

            self._raw = OpenAI(
                api_key=self._settings.deepseek_api_key,
                base_url=self._base_url,
                timeout=httpx.Timeout(90.0, connect=15.0),
                max_retries=0,  # 重试统一交给 tenacity，避免 SDK 默认 600s 超时 × 多层重试
            )
        return self._raw

    def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
        max_tokens: int = 900,
        temperature: float = 0.4,
        model: str | None = None,
    ) -> LLMResult:
        if not self.configured:
            raise LLMError("未配置 DEEPSEEK_API_KEY")

        used_model = model or self._model
        last_error: LLMError | None = None
        for _attempt in range(2):  # 仅 JSON/校验失败重试 1 次（传输层由 tenacity 处理）
            started = time.perf_counter()
            try:
                response = self._call_api(system, user, max_tokens, temperature, used_model)
            except Exception as exc:  # noqa: BLE001 — 传输层已由 tenacity 重试 2 次，直接失败
                raise LLMError(f"调用失败: {type(exc).__name__}: {exc}") from exc
            ms = round((time.perf_counter() - started) * 1000)
            content = (response.choices[0].message.content or "").strip()
            usage = getattr(response, "usage", None)
            tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
            tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
            cached_in = _cached_tokens(usage)

            try:
                parsed = json.loads(content)
            except ValueError as exc:
                last_error = LLMError(f"JSON 解析失败: {exc}")
                log.warning("LLM 输出非法 JSON（purpose=%s）", purpose)
                continue

            if schema is not None:
                try:
                    parsed = schema.model_validate(parsed).model_dump()
                except PydanticValidationError as exc:
                    last_error = LLMError(f"schema 校验失败: {exc}")
                    log.warning("LLM 输出未通过 schema（purpose=%s）", purpose)
                    continue

            return LLMResult(
                content=parsed,
                raw=content,
                model=used_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cached_in=cached_in,
                ms=ms,
            )
        raise last_error or LLMError("未知失败")

    @retry(
        reraise=True,
        stop=stop_after_attempt(2),
        wait=wait_exponential_jitter(multiplier=0.8, max=4.0),
        retry=retry_if_exception_type(Exception),
    )
    def _call_api(self, system: str, user: str, max_tokens: int, temperature: float, model: str):
        return self._get_raw().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            max_tokens=max_tokens,
        )
