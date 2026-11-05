"""调度作业拆分：轮询只做本地判定，LLM 解读与投递走独立作业。

对应 `docs/requirements.md` US-4.1 AC1（单轮轮询为纯本地规则判断、不调用 LLM）
与 `docs/architecture.md` §6.1：慢模型 / 慢 SMTP 不得占用监控窗口。
"""

from __future__ import annotations

from types import SimpleNamespace

from invest_assistant.engine.notify_flow import notify_job, poll_job


class FakePoller:
    def __init__(self, warning: str = ""):
        self.calls = 0
        self._warning = warning

    def run_once(self):
        self.calls += 1
        return SimpleNamespace(system_warning=self._warning, summary=lambda: "规则 3 条")


class FakeFlow:
    def __init__(self):
        self.pending_calls = 0
        self.warnings: list[str] = []

    def process_pending(self):
        self.pending_calls += 1
        return {"sent": 1}

    def send_system_warning(self, warning: str):
        self.warnings.append(warning)
        return True


def test_poll_job_does_not_interpret_or_deliver():
    poller, flow = FakePoller(), FakeFlow()

    summary = poll_job(poller, flow)()

    assert poller.calls == 1
    assert flow.pending_calls == 0  # 关键：轮询不再触发 LLM 解读与投递
    assert "规则" in summary


def test_poll_job_still_sends_system_warning():
    poller, flow = FakePoller(warning="数据源连续失败"), FakeFlow()

    poll_job(poller, flow)()

    assert flow.warnings == ["数据源连续失败"]
    assert flow.pending_calls == 0


def test_notify_job_only_delivers():
    poller, flow = FakePoller(), FakeFlow()

    output = notify_job(flow)()

    assert flow.pending_calls == 1
    assert poller.calls == 0
    assert "通知投递" in output
