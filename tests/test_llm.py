"""B2-04 LLM 客户端与用量测试（假客户端，无网络）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from invest_assistant.config import Settings
from invest_assistant.db.models import Base, LLMUsage
from invest_assistant.db.session import make_engine
from invest_assistant.llm import LLMClient, LLMError, daily_cost_summary, estimate_cost, record_usage


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRawClient:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def make_response(content: str, tokens_in=120, tokens_out=60, cached=20):
    usage = SimpleNamespace(
        prompt_tokens=tokens_in,
        completion_tokens=tokens_out,
        prompt_cache_hit_tokens=cached,
        prompt_tokens_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
    )


def make_client(responses):
    settings = Settings(deepseek_api_key="test-key")
    raw = FakeRawClient(responses)
    return LLMClient(settings=settings, raw_client=raw), raw


def test_complete_json_ok():
    client, raw = make_client([make_response('{"a": 1}')])
    result = client.complete_json(purpose="test", system="s", user="u")
    assert result.content == {"a": 1}
    assert result.tokens_in == 120
    assert result.tokens_out == 60
    assert result.cached_in == 20
    assert result.model == "deepseek-chat"
    assert raw.completions.calls == 1


def test_complete_json_retries_on_bad_json():
    client, raw = make_client([make_response("not json"), make_response('{"ok": true}')])
    result = client.complete_json(purpose="test", system="s", user="u")
    assert result.content == {"ok": True}
    assert raw.completions.calls == 2


def test_complete_json_schema_validation_and_retry():
    class Card(BaseModel):
        summary: str
        confidence: float

    client, raw = make_client(
        [
            make_response('{"summary": "x"}'),  # 缺 confidence → 重试
            make_response('{"summary": "x", "confidence": 0.5}'),
        ]
    )
    result = client.complete_json(purpose="test", system="s", user="u", schema=Card)
    assert result.content["confidence"] == 0.5
    assert raw.completions.calls == 2


def test_complete_json_schema_failure_raises():
    class Card(BaseModel):
        summary: str

    client, _ = make_client([make_response('{"x": 1}'), make_response('{"x": 2}')])
    with pytest.raises(LLMError):
        client.complete_json(purpose="test", system="s", user="u", schema=Card)


def test_complete_json_not_configured():
    client = LLMClient(settings=Settings(deepseek_api_key=""), raw_client=None)
    with pytest.raises(LLMError):
        client.complete_json(purpose="t", system="s", user="u")


def test_transport_error_recovers_on_retry():
    client, raw = make_client([RuntimeError("boom"), make_response('{"a": 1}')])
    result = client.complete_json(purpose="t", system="s", user="u")
    assert result.content == {"a": 1}
    assert raw.completions.calls == 2


def test_transport_error_exhausts_retries_and_raises():
    client, raw = make_client([RuntimeError("boom"), RuntimeError("boom2")])
    with pytest.raises(LLMError):
        client.complete_json(purpose="t", system="s", user="u")
    assert raw.completions.calls == 2  # tenacity 重试 2 次即失败，不再叠加外层重试


def test_estimate_cost_and_record(tmp_path):
    pricing = {"input": 2.0, "output": 8.0}
    assert estimate_cost(1_000_000, 1_000_000, pricing) == 10.0
    assert estimate_cost(500_000, 0, pricing) == 1.0

    engine = make_engine(f"sqlite:///{tmp_path / 'llm.db'}")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)

    cost = record_usage(
        sf, purpose="analysis", model="deepseek-chat", tokens_in=1_000_000, tokens_out=0
    )
    assert cost == 2.0
    record_usage(
        sf, purpose="trigger", model="deepseek-chat", tokens_in=0, tokens_out=500_000
    )
    with sf() as session:
        rows = session.scalars(select(LLMUsage)).all()
        assert len(rows) == 2
        assert {r.purpose for r in rows} == {"analysis", "trigger"}

    summary = daily_cost_summary(sf)
    assert summary["today_cost"] == 6.0
    assert summary["days"][0]["tokens"] == 1_500_000
    assert "threshold" in summary
