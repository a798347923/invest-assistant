"""实盘 App 执行器测试：控件树解析 + 填单回读 + fail-closed 提交。

用假的 adb 设备模拟东财交易页，验证关键安全契约：
- 回读不一致 → 中止且**零次**提交；
- dry_run → 只填单不提交；
- 确认弹窗缺失 → 标记未核实（不假设成功）；
- 输入框不足 → 中止；
- 非数字输入 → 拒绝（防止命令注入/输入法乱码）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import sessionmaker

from invest_assistant.db.models import Base, OrderIntent
from invest_assistant.db.session import make_engine
from invest_assistant.engine.live_app import (
    AdbDevice,
    AppExecutor,
    AppSettings,
    ExecuteError,
    LiveAppService,
    Screen,
)

CODE_Y, PRICE_Y, QTY_Y = 340, 440, 540


def fast_settings(**over) -> AppSettings:
    """测试用零延迟配置（生产默认有 0.2~1.5s 的界面等待）。"""
    base = dict(tab_delay=0, field_delay=0, submit_delay=0, settle_seconds=0)
    base.update(over)
    return AppSettings(**base)


def page_xml(
    code: str = "",
    price: str = "",
    qty: str = "",
    *,
    editables: int = 3,
    confirm: bool = False,
    success: bool = False,
    submit: bool = True,
) -> str:
    fields = [
        (CODE_Y, code), (PRICE_Y, price), (QTY_Y, qty),
    ][:editables]
    nodes = [
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,1920]">',
        '<node class="android.widget.TextView" text="买入" bounds="[0,100][540,180]" clickable="true" />',
        '<node class="android.widget.TextView" text="卖出" bounds="[540,100][1080,180]" clickable="true" />',
    ]
    for y, value in fields:
        nodes.append(
            f'<node class="android.widget.EditText" text="{value}" bounds="[300,{y}][1000,{y + 80}]" '
            f'focusable="true" />'
        )
    if submit:
        nodes.append(
            '<node class="android.widget.Button" text="买入" bounds="[100,1700][980,1800]" clickable="true" />'
        )
    if confirm:
        nodes.append(
            '<node class="android.widget.TextView" text="确定" bounds="[600,1000][900,1080]" clickable="true" />'
        )
    if success:
        nodes.append(
            '<node class="android.widget.TextView" text="委托成功，合同编号 12345" bounds="[100,600][900,680]" />'
        )
    nodes.append("</node>")
    return "<hierarchy>" + "".join(nodes) + "</hierarchy>"


class FakeAdb:
    """模拟交易页：3 个输入框 + 页签 + 提交按钮 + 确认弹窗 + 成功提示。"""

    def __init__(self, *, fail_write: bool = False, show_confirm: bool = True,
                 show_success: bool = True, editables: int = 3, submit: bool = True):
        self.values = {"code": "", "price": "", "qty": ""}
        self.fail_write = fail_write
        self.show_confirm = show_confirm
        self.show_success = show_success
        self.editables = editables
        self.submit_button = submit
        self.state = "page"
        self.submit_taps = 0
        self.confirm_taps = 0
        self.typed: list[str] = []

    def ensure(self) -> str:
        return "fake-device"

    def dump(self) -> Screen:
        return Screen.from_xml(page_xml(
            code=self.values["code"],
            price=self.values["price"],
            qty=self.values["qty"],
            editables=self.editables,
            submit=self.submit_button,
            confirm=self.state == "confirm",
            success=self.state == "success" and self.show_success,
        ))

    def tap_node(self, node) -> None:
        label = (node.text or "").strip()
        if label in ("买入", "卖出") and node.bounds[1] > 1000:
            self.submit_taps += 1
            if self.show_confirm:
                self.state = "confirm"
            return
        if label in ("确定", "确认"):
            self.confirm_taps += 1
            self.state = "success"

    def clear_and_type(self, node, value: str) -> None:
        key = {CODE_Y: "code", PRICE_Y: "price", QTY_Y: "qty"}[node.bounds[1]]
        self.typed.append(value)
        self.values[key] = "999999" if self.fail_write else value

    def screenshot(self, path) -> bool:
        return True

    def package_installed(self, package: str) -> bool:
        return True

    def current_package(self) -> str:
        return "com.eastmoney.android.berlin"


def make_intent(intent_id: int = 1, side: str = "buy") -> OrderIntent:
    return OrderIntent(
        id=intent_id, code="600519", name="贵州茅台", side=side, qty=100, price=1500.0,
        mode="auto", trade_date=dt.date(2026, 11, 2), idempotency_key="k1", source="watchlist",
    )


# ---------------------------------------------------------------- 控件树

def test_screen_parse_and_locate():
    screen = Screen.from_xml(page_xml(code="600519"))
    assert screen.size == (1080, 1920)
    assert len(screen.editable()) == 3
    assert screen.find(text="买入") is not None
    # node_at 取「最小包含节点」，即输入框本身而不是外层容器
    node = screen.node_at(500, CODE_Y + 10)
    assert node is not None and node.cls.endswith("EditText")


def test_screen_node_at_outside_returns_none():
    screen = Screen.from_xml(page_xml())
    assert screen.node_at(-10, -10) is None


# ---------------------------------------------------------------- 填单

def test_dry_run_fills_but_never_submits():
    fake = FakeAdb()
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=True))
    report = executor.execute(make_intent())

    assert fake.typed == ["600519", "1500.00", "100"]
    assert fake.submit_taps == 0          # 关键：dry-run 绝不点提交
    assert report.submitted is False
    assert any(s.name == "提交" and not s.ok for s in report.steps)


def test_readback_mismatch_aborts_without_submit():
    fake = FakeAdb(fail_write=True)          # 模拟键盘输入被吞
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=False))
    report = executor.execute(make_intent())

    assert fake.submit_taps == 0            # fail closed：零次提交
    assert report.submitted is False
    assert any("回读" in f for f in report.failures())


def test_missing_fields_aborts():
    fake = FakeAdb(editables=1)
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=False))
    report = executor.execute(make_intent())

    assert fake.submit_taps == 0
    assert report.submitted is False
    assert any("输入框" in f for f in report.failures())


# ---------------------------------------------------------------- 提交

def test_submit_with_confirm_is_verified():
    fake = FakeAdb()
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=False))
    report = executor.execute(make_intent())

    assert fake.submit_taps == 1
    assert fake.confirm_taps == 1
    assert report.submitted is True
    assert report.verified is True


def test_missing_confirm_dialog_marks_unverified():
    fake = FakeAdb(show_confirm=False)
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=False))
    report = executor.execute(make_intent())

    assert fake.submit_taps == 1
    assert report.submitted is True         # 已点提交，但不能当作成功
    assert report.verified is False
    assert any(s.name == "确认弹窗" and not s.ok for s in report.steps)


def test_success_text_absent_is_not_verified():
    fake = FakeAdb(show_success=False)
    executor = AppExecutor(device=fake, settings=fast_settings(dry_run=False))
    report = executor.execute(make_intent())

    assert report.submitted is True
    assert report.verified is False


# ---------------------------------------------------------------- 输入安全

def test_input_text_rejects_non_numeric():
    device = AdbDevice(adb_path="definitely-not-adb")
    for bad in ("600519.SH", "1 0 0", "", "abc", "12;rm -rf /"):
        with pytest.raises(ExecuteError):
            device.input_text(bad)


# ---------------------------------------------------------------- 与指令单集成

@pytest.fixture()
def sf(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live_app.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


class FakeSettings:
    def __init__(self, cfg):
        self._cfg = cfg

    def app_config(self):
        return self._cfg


def seed(sf, status: str = "pending") -> int:
    with sf() as session:
        row = OrderIntent(
            trade_date=dt.date(2026, 11, 2), code="600519", name="贵州茅台", side="buy",
            qty=100, price=1500.0, source="watchlist", mode="auto", status=status,
            idempotency_key="key-1",
        )
        session.add(row)
        session.commit()
        return row.id


def test_service_dry_run_keeps_pending_and_audits(sf):
    intent_id = seed(sf)
    service = LiveAppService(
        session_factory=sf,
        settings=FakeSettings({"live": {"enabled": True, "dry_run": True}}),
        executor=AppExecutor(device=FakeAdb(), settings=fast_settings(dry_run=True)),
    )
    result = service.execute_intent(intent_id)

    assert result["submitted"] is False
    assert result["status"] == "pending"
    with sf() as session:
        row = session.get(OrderIntent, intent_id)
        assert row.executor == "ui_app"
        assert "未提交" in row.note


def test_service_submit_marks_submitted(sf):
    intent_id = seed(sf)
    service = LiveAppService(
        session_factory=sf,
        settings=FakeSettings({"live": {"enabled": True, "dry_run": False}}),
        executor=AppExecutor(device=FakeAdb(), settings=fast_settings(dry_run=False)),
    )
    result = service.execute_intent(intent_id)

    assert result["status"] == "submitted"
    assert result["verified"] is True


def test_service_refuses_when_live_disabled(sf):
    """总开关关闭时，即使直接调用服务也不得触碰券商界面。"""
    intent_id = seed(sf)
    service = LiveAppService(
        session_factory=sf,
        settings=FakeSettings({"live": {"enabled": False, "dry_run": True}}),
        executor=AppExecutor(device=FakeAdb(), settings=fast_settings()),
    )
    with pytest.raises(ValueError):
        service.execute_intent(intent_id)


def test_service_refuses_filled_intent(sf):
    intent_id = seed(sf, status="filled")
    service = LiveAppService(
        session_factory=sf,
        settings=FakeSettings({"live": {"enabled": True, "dry_run": True}}),
        executor=AppExecutor(device=FakeAdb(), settings=fast_settings()),
    )
    with pytest.raises(ValueError):
        service.execute_intent(intent_id)
