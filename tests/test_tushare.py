"""Tushare 适配器测试：解析、权限错误、限频重试、未配置降级（不联网）。"""

from __future__ import annotations

import pytest

from invest_assistant.datasource.base import MarketDataError
from invest_assistant.datasource.tushare import TushareClient, TushareResult


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def success(fields, items):
    return FakeResp({"code": 0, "msg": None, "data": {"fields": fields, "items": items}})


def failure(msg, code=40001):
    return FakeResp({"code": code, "msg": msg, "data": None})


@pytest.fixture()
def client(monkeypatch):
    c = TushareClient(token="x" * 40)
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        calls.append(json)
        return c._next_response  # type: ignore[attr-defined]

    monkeypatch.setattr("invest_assistant.datasource.tushare.requests.post", fake_post)
    c.calls = calls  # type: ignore[attr-defined]
    return c


def test_unconfigured_raises_and_doctor_reports(monkeypatch):
    c = TushareClient(token="")
    assert c.configured is False
    with pytest.raises(MarketDataError, match="TUSHARE_TOKEN"):
        c.call("daily_basic", {})
    info = c.doctor()
    assert info["configured"] is False and "error" in info


def test_parses_rows_into_dicts(client):
    client._next_response = success(
        ["ts_code", "trade_date", "pe_ttm"],
        [["600519.SH", "20261030", 21.37], ["000001.SZ", "20261030", 5.1]],
    )
    result = client.call("daily_basic", {"trade_date": "20261030"})
    assert isinstance(result, TushareResult)
    assert len(result) == 2
    assert result.rows[0]["pe_ttm"] == 21.37
    assert client.calls[0]["api_name"] == "daily_basic"


def test_permission_error_is_raised_not_retried(client):
    client._next_response = failure("抱歉，您没有访问该接口的权限")
    with pytest.raises(MarketDataError, match="权限"):
        client.call("top_list", {"trade_date": "20261030"})
    assert len(client.calls) == 1  # 权限问题不重试


def test_rate_limit_is_retried(client, monkeypatch):
    responses = [failure("抱歉，您每分钟最多访问该接口500次"), success(["ts_code"], [["600519.SH"]])]
    monkeypatch.setattr(
        "invest_assistant.datasource.tushare.time.sleep", lambda _s: None
    )

    def fake_post(url, json=None, timeout=None):  # noqa: A002
        client.calls.append(json)  # type: ignore[attr-defined]
        return responses.pop(0)

    monkeypatch.setattr("invest_assistant.datasource.tushare.requests.post", fake_post)
    client.calls = []  # type: ignore[attr-defined]
    result = client.call("daily_basic", {"trade_date": "20261030"})
    assert len(result) == 1
    assert len(client.calls) == 2  # 第一次限频，第二次成功


def test_daily_basic_builds_params(client):
    client._next_response = success(["ts_code"], [])
    client.daily_basic("600519.SH", "20230101", "20231231")
    payload = client.calls[0]
    assert payload["params"]["ts_code"] == "600519.SH"
    assert payload["params"]["start_date"] == "20230101"
    assert "pe_ttm" in payload["fields"]


def test_doctor_all_ok(client):
    client._next_response = success(["ts_code"], [["600519.SH"]])
    info = client.doctor()
    assert info["configured"] is True
    assert len(info["ok"]) == 7
    assert info["failed"] == []
