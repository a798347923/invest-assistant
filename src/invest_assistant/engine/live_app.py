"""实盘执行器：东财 Android App（MuMu 模拟器）的 UI 自动化下单。

## 为什么是这条路（与 2026-10-18 取消的方案的区别）

上一版驱动 **东财 PC 客户端**：交易表单是 CEF/自绘控件，UIA 拿不到字段句柄，只能"窗口坐标 + 键盘模拟"，
窗口位移 / DPI / 输入法 / 确认弹窗都会误单，因此被取消（见 `docs/live-trading-assessment.md` §11）。

本版驱动 **Android App**，走 ADB + UIAutomator：

| 维度 | 旧方案（PC 坐标） | 本方案（App 控件树） |
|---|---|---|
| 定位 | 屏幕坐标，窗口一移就错 | 按 text/class/bounds 查控件树，窗口位置无关 |
| 填单校验 | 剪贴板回读（易受输入法干扰） | 回读控件文本（`node_at(x,y).text`） |
| 容器 | Windows 客户端（版本随时变） | MuMu 实例，只依赖 App 界面文案 |
| 自动化库 | pywinauto + Pillow | 只用 adb（外加标准库解析 XML） |

## 安全契约（全部 fail-closed）

1. 定位失败 / 回读不一致 / 确认弹窗找不到 → **立即中止，绝不提交**，指令记 `failed`；
2. `dry_run=true`（默认）走完填单与回读校验，但**不点提交**；
3. 提交后不"推测成功"：看到成功文案或委托记录才算 `submitted`，否则记 `submitted` + `待人工核对` 备注；
4. 每一步留截图（`data/live_orders/<intent>_*.png`）与步骤日志，全链路可追溯；
5. 不保存交易密码，只驱动**已登录**的 App。
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..db.models import OrderIntent
from ..db.session import get_session_factory


class AdbError(RuntimeError):
    """adb 不可用 / 设备未连接。"""


class ExecuteError(RuntimeError):
    """执行中止（fail closed）：调用方必须把指令记为失败，不做重试。"""


# ---------------------------------------------------------------- 控件树


@dataclass(frozen=True)
class Node:
    text: str = ""
    desc: str = ""
    cls: str = ""
    rid: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)

    @property
    def area(self) -> int:
        left, top, right, bottom = self.bounds
        return max(0, right - left) * max(0, bottom - top)

    def contains(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.bounds
        return left <= x <= right and top <= y <= bottom

    @property
    def label(self) -> str:
        return (self.text or self.desc or "").strip()


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    match = _BOUNDS_RE.match(raw or "")
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in match.groups())  # type: ignore[return-value]


@dataclass
class Screen:
    nodes: list[Node] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)

    @classmethod
    def from_xml(cls, xml: str) -> Screen:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise ExecuteError(f"控件树解析失败：{exc}") from exc
        nodes: list[Node] = []
        size = (0, 0)
        for element in root.iter("node"):
            attrs = element.attrib
            bounds = _parse_bounds(attrs.get("bounds", ""))
            nodes.append(
                Node(
                    text=attrs.get("text", "") or "",
                    desc=attrs.get("content-desc", "") or "",
                    cls=attrs.get("class", "") or "",
                    rid=attrs.get("resource-id", "") or "",
                    bounds=bounds,
                    clickable=attrs.get("clickable") == "true",
                    enabled=attrs.get("enabled") != "false",
                    focusable=attrs.get("focusable") == "true",
                )
            )
            if size == (0, 0) and bounds[2] > 0 and bounds[3] > 0:
                size = (bounds[2], bounds[3])
        return cls(nodes=nodes, size=size)

    def find_all(
        self,
        *,
        text: str | None = None,
        contains: str | None = None,
        cls: str | None = None,
        clickable: bool | None = None,
    ) -> list[Node]:
        out = []
        for node in self.nodes:
            if text is not None and node.text.strip() != text:
                continue
            if contains is not None and contains not in node.label:
                continue
            if cls is not None and not node.cls.endswith(cls):
                continue
            if clickable is not None and node.clickable != clickable:
                continue
            out.append(node)
        return out

    def find(self, **kwargs) -> Node | None:
        found = self.find_all(**kwargs)
        return found[0] if found else None

    def node_at(self, x: int, y: int) -> Node | None:
        """返回包含该点的**最小**节点（用于回读某个输入框当前文本）。"""
        hits = [n for n in self.nodes if n.contains(x, y) and n.area > 0]
        if not hits:
            return None
        return min(hits, key=lambda n: n.area)

    def editable(self) -> list[Node]:
        """可见输入框：EditText，或可聚焦且能点的输入类控件。"""
        nodes = [
            n
            for n in self.nodes
            if n.cls.endswith(("EditText", "AutoCompleteTextView"))
            or (n.focusable and "EditText" in n.cls)
        ]
        # 过滤掉零尺寸（不可见）节点
        nodes = [n for n in nodes if n.area > 0]
        return sorted(nodes, key=lambda n: (n.bounds[1], n.bounds[0]))

    def texts(self) -> list[str]:
        return [n.label for n in self.nodes if n.label]


# ---------------------------------------------------------------- adb 设备


def _candidate_adb_paths() -> list[str]:
    return [
        r"D:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe",
        r"D:\Program Files\Netease\MuMu\nx_main\adb.exe",
        str(Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"),
    ]


def resolve_adb(explicit: str = "") -> str:
    """定位 adb：显式配置 > MuMu 常见路径 > PATH。"""
    if explicit:
        return explicit
    for path in _candidate_adb_paths():
        if Path(path).exists():
            return path
    return "adb"


class AdbDevice:
    """adb 原语封装（只做设备操作，不含业务语义）。"""

    def __init__(self, adb_path: str = "", serial: str = "", timeout: float = 20.0):
        self.adb = resolve_adb(adb_path)
        self.serial = serial
        self.timeout = timeout

    # -- 基础
    def _run(self, args: list[str], binary: bool = False):
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout)
        except FileNotFoundError as exc:
            raise AdbError(f"找不到 adb：{self.adb}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb 超时：{' '.join(args)}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise AdbError(f"adb 失败（{proc.returncode}）：{err or ' '.join(args)}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def devices(self) -> list[str]:
        out = self._run(["devices"])
        serials = []
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].strip() == "device":
                serials.append(parts[0].strip())
        return serials

    def ensure(self) -> str:
        """确保有可用设备；返回 serial。"""
        serials = self.devices()
        if not serials:
            raise AdbError("没有已连接的设备：请先启动 MuMu 并开启 ADB 调试")
        if self.serial and self.serial not in serials:
            raise AdbError(f"指定的设备 {self.serial} 不在线（在线：{', '.join(serials)}）")
        if not self.serial:
            self.serial = serials[0]
        return self.serial

    def shell(self, command: str) -> str:
        return self._run(["shell", command])

    # -- UI
    def dump(self) -> Screen:
        remote = "/sdcard/dsh_ui.xml"
        self.shell(f"uiautomator dump {remote} >/dev/null 2>&1")
        xml = self.shell(f"cat {remote}")
        if "<hierarchy" not in xml:
            raise ExecuteError("控件树为空：App 可能仍在加载，或界面为 Surface/WebView 自绘")
        return Screen.from_xml(xml)

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}")

    def tap_node(self, node: Node) -> None:
        x, y = node.center
        self.tap(x, y)

    def input_text(self, text: str) -> None:
        # 只允许数字与小数点：避免输入法/特殊字符导致命令注入或乱码
        if not re.fullmatch(r"[0-9.]+", text or ""):
            raise ExecuteError(f"拒绝输入非数字内容：{text!r}")
        self.shell(f"input text {text}")

    def clear_and_type(self, node: Node, value: str, deletes: int = 12) -> None:
        self.tap_node(node)
        time.sleep(0.15)
        self.shell("input keyevent KEYCODE_MOVE_END")
        for _ in range(deletes):
            self.shell("input keyevent KEYCODE_DEL")
        self.input_text(value)

    def screenshot(self, path: Path) -> bool:
        try:
            data = self._run(["exec-out", "screencap", "-p"], binary=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return True
        except (AdbError, OSError):
            return False

    def current_package(self) -> str:
        """当前前台包名（不依赖设备端 grep，兼容不同 Android 版本）。"""
        for command in ("dumpsys window", "dumpsys activity activities"):
            try:
                out = self.shell(command)
            except AdbError:
                continue
            for line in out.splitlines():
                if "mCurrentFocus" not in line and "mResumedActivity" not in line:
                    continue
                match = re.search(r"([A-Za-z][\w.]*)/[\w.$]+", line)
                if match:
                    return match.group(1)
        return ""

    def package_installed(self, package: str) -> bool:
        if not package:
            return False
        out = self.shell(f"pm list packages {package}")
        return f"package:{package}" in out

    def launch(self, package: str) -> None:
        self.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def doctor(self) -> dict:
        info: dict = {"adb": self.adb}
        try:
            devices = self.devices()
        except AdbError as exc:
            info.update({"devices": [], "error": str(exc)})
            return info
        info["devices"] = devices
        if not devices:
            return info
        try:
            # 多设备时 adb 不带 -s 会报错，这里先选定一台
            info["serial"] = self.ensure()
            info["current_package"] = self.current_package()
        except AdbError as exc:
            info["error"] = str(exc)
        return info


# ---------------------------------------------------------------- 业务执行


@dataclass
class AppSettings:
    """App 执行器配置（config.live.app）。"""

    package: str = "com.eastmoney.android.berlin"
    adb_path: str = ""
    device: str = ""
    dry_run: bool = True
    buy_tab: tuple[str, ...] = ("买入",)
    sell_tab: tuple[str, ...] = ("卖出",)
    confirm: tuple[str, ...] = ("确定", "确认")
    success: tuple[str, ...] = ("已委托", "委托成功", "已受理", "提交成功", "已提交")
    screenshot: bool = True
    settle_seconds: float = 1.5
    tab_delay: float = 0.4
    field_delay: float = 0.2
    submit_delay: float = 0.6

    @classmethod
    def from_config(cls, cfg: dict | None) -> AppSettings:
        cfg = cfg or {}

        def _tuple(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            value = cfg.get(key)
            if not value:
                return default
            if isinstance(value, str):
                return (value,)
            return tuple(str(x) for x in value)

        return cls(
            package=str(cfg.get("package", "com.eastmoney.android.berlin")),
            adb_path=str(cfg.get("adb_path", "") or ""),
            device=str(cfg.get("device", "") or ""),
            dry_run=bool(cfg.get("dry_run", True)),
            buy_tab=_tuple("buy_tab", ("买入",)),
            sell_tab=_tuple("sell_tab", ("卖出",)),
            confirm=_tuple("confirm", ("确定", "确认")),
            success=_tuple("success", ("已委托", "委托成功", "已受理", "提交成功", "已提交")),
            settle_seconds=float(cfg.get("settle_seconds", 1.5) or 0),
            tab_delay=float(cfg.get("tab_delay", 0.4) or 0),
            field_delay=float(cfg.get("field_delay", 0.2) or 0),
            submit_delay=float(cfg.get("submit_delay", 0.6) or 0),
        )


def _same_value(expected: str, actual: str) -> bool:
    """回读校验：数字按数值比较（10.5 == 10.50），代码按字符串比较。"""
    left = (expected or "").replace(",", "").strip()
    right = (actual or "").replace(",", "").strip()
    if not right:
        return False
    try:
        return abs(float(left) - float(right)) < 1e-6
    except ValueError:
        return left == right


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class ExecutionReport:
    intent_id: int
    submitted: bool
    verified: bool
    steps: list[Step] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.submitted and all(s.ok for s in self.steps)

    def summary(self) -> str:
        head = "已提交" if self.submitted else "未提交（仅演练）"
        body = "；".join(f"{s.name}:{'ok' if s.ok else 'FAIL'}{'(' + s.detail + ')' if s.detail else ''}"
                        for s in self.steps)
        return f"#{self.intent_id} {head} | {body}"

    def failures(self) -> list[str]:
        return [f"{s.name}：{s.detail}" for s in self.steps if not s.ok]


class AppExecutor:
    """把一条 OrderIntent 变成一次受控下单（含回读校验与提交后确认）。"""

    def __init__(self, device: AdbDevice | None = None, settings: AppSettings | None = None,
                 out_dir: Path | None = None):
        self.settings = settings or AppSettings()
        self.device = device or AdbDevice(self.settings.adb_path, self.settings.device)
        self.out_dir = out_dir or (get_settings().data_dir / "live_orders")

    # -- 内部工具
    def _shot(self, intent_id: int, tag: str, report: ExecutionReport) -> None:
        if not self.settings.screenshot:
            return
        path = self.out_dir / f"{intent_id}_{tag}_{dt.datetime.now():%H%M%S}.png"
        if self.device.screenshot(path):
            report.screenshots.append(str(path))

    def _step(self, report: ExecutionReport, name: str, ok: bool, detail: str = "") -> None:
        report.steps.append(Step(name, ok, detail))

    def _open_trade_page(self, side: str, report: ExecutionReport) -> Screen:
        screen = self.device.dump()
        labels = self.settings.buy_tab if side == "buy" else self.settings.sell_tab
        for label in labels:
            node = screen.find(text=label, clickable=True) or screen.find(text=label)
            if node is not None:
                self.device.tap_node(node)
                self._step(report, "进入买卖页", True, label)
                time.sleep(self.settings.tab_delay)
                return self.device.dump()
        self._step(report, "进入买卖页", False, f"找不到「{'/'.join(labels)}」入口")
        raise ExecuteError("找不到买卖页入口")

    def _fields(self, screen: Screen, report: ExecutionReport) -> list[Node]:
        nodes = screen.editable()
        if len(nodes) < 3:
            self._step(report, "定位输入框", False, f"只找到 {len(nodes)} 个输入框（需代码/价格/数量）")
            raise ExecuteError("输入框数量不足")
        self._step(report, "定位输入框", True, f"{len(nodes)} 个，取前 3 个")
        return nodes[:3]

    def _fill(self, field_node: Node, value: str, name: str, report: ExecutionReport,
              screen: Screen) -> None:
        self.device.clear_and_type(field_node, value)
        time.sleep(self.settings.field_delay)
        after = self.device.dump()
        actual = after.node_at(*field_node.center).text if after.node_at(*field_node.center) else ""
        if not _same_value(value, actual):
            self._step(report, f"填写{name}", False, f"回读 {actual!r} != {value!r}")
            raise ExecuteError(f"{name}回读校验失败")
        self._step(report, f"填写{name}", True, f"{value}")

    def _submit_node(self, screen: Screen, side: str) -> Node | None:
        labels = self.settings.buy_tab if side == "buy" else self.settings.sell_tab
        candidates = [
            n for n in screen.nodes
            if n.clickable and n.enabled and n.text.strip() in labels
        ]
        if not candidates:
            return None
        # 提交按钮通常在页面下方；取 y 最大者（同一个词可能既是页签又是按钮）
        return max(candidates, key=lambda n: n.bounds[1])

    def execute(self, intent: OrderIntent) -> ExecutionReport:
        report = ExecutionReport(intent_id=intent.id, submitted=False, verified=False)
        try:
            self.device.ensure()
        except AdbError as exc:
            self._step(report, "连接设备", False, str(exc))
            return report

        self._shot(intent.id, "before", report)
        try:
            screen = self._open_trade_page(intent.side, report)
            code_node, price_node, qty_node = self._fields(screen, report)

            self._fill(code_node, str(intent.code), "代码", report, screen)
            self._fill(price_node, f"{intent.price:.2f}", "价格", report, screen)
            self._fill(qty_node, str(int(intent.qty)), "数量", report, screen)

            screen = self.device.dump()
            self._shot(intent.id, "filled", report)

            if self.settings.dry_run:
                self._step(report, "提交", False, "dry_run=true：已填单并校验，未提交")
                return report

            submit = self._submit_node(screen, intent.side)
            if submit is None:
                self._step(report, "提交", False, "找不到提交按钮")
                return report
            self.device.tap_node(submit)
            self._step(report, "提交", True, submit.text)

            time.sleep(self.settings.submit_delay)
            confirm_screen = self.device.dump()
            confirm = next(
                (n for label in self.settings.confirm
                 for n in confirm_screen.find_all(text=label, clickable=True)),
                None,
            )
            if confirm is None:
                self._step(report, "确认弹窗", False, "未出现确认框：可能已直接提交，需人工核对")
                report.submitted = True
                report.verified = False
                self._shot(intent.id, "unconfirmed", report)
                return report
            self.device.tap_node(confirm)
            self._step(report, "确认弹窗", True, confirm.text)

            time.sleep(self.settings.settle_seconds)
            final = self.device.dump()
            texts = " ".join(final.texts())
            hit = next((w for w in self.settings.success if w in texts), "")
            report.submitted = True
            report.verified = bool(hit)
            self._step(report, "提交结果", bool(hit), hit or "未见成功文案（待人工核对）")
            self._shot(intent.id, "after", report)
            return report
        except ExecuteError as exc:
            self._step(report, "中止", False, str(exc))
            self._shot(intent.id, "abort", report)
            return report
        except AdbError as exc:
            self._step(report, "adb 异常", False, str(exc))
            return report


# ---------------------------------------------------------------- 与指令单集成


class LiveAppService:
    """在既有风控/审计之上，增加「自动执行」能力（默认 dry-run）。"""

    def __init__(self, session_factory=None, settings=None, executor: AppExecutor | None = None):
        self._sf = session_factory or get_session_factory()
        self._settings = settings or get_settings()
        cfg = (self._settings.app_config() or {}).get("live") or {}
        self.app = AppSettings.from_config(cfg.get("app") or {})
        self.dry_run = bool(cfg.get("dry_run", True))
        self.app.dry_run = self.dry_run
        self._executor = executor

    def _executor_for(self) -> AppExecutor:
        return self._executor or AppExecutor(settings=self.app)

    def execute_intent(self, intent_id: int) -> dict:
        cfg = (self._settings.app_config() or {}).get("live") or {}
        if not cfg.get("enabled"):
            # 深层防御：即使有人绕过 CLI，也拒绝在总开关关闭时触碰券商界面
            raise ValueError("实盘总开关未开启（live.enabled=false），拒绝自动执行")
        with self._sf() as session:
            row = session.get(OrderIntent, intent_id)
            if row is None:
                raise ValueError(f"指令单 #{intent_id} 不存在")
            if row.status not in ("pending", "confirmed"):
                raise ValueError(f"指令单状态为 {row.status}，不能执行")
            # 用 detached 副本，避免持有 session 期间执行长任务
            snapshot = OrderIntent(
                id=row.id, code=row.code, name=row.name, side=row.side,
                qty=row.qty, price=row.price, status=row.status, mode=row.mode,
                trade_date=row.trade_date, idempotency_key=row.idempotency_key,
                source=row.source, note=row.note,
            )

        report = self._executor_for().execute(snapshot)
        status = "failed"
        if report.submitted and report.verified:
            status = "submitted"
        elif report.submitted:
            status = "submitted"
        elif self.dry_run and all(s.ok for s in report.steps if s.name != "提交"):
            status = "pending"  # 演练通过，仍待执行

        note_bits = [report.summary()]
        if report.failures():
            note_bits.append("失败：" + "；".join(report.failures()))
        if report.screenshots:
            note_bits.append("截图：" + ",".join(report.screenshots))
        with self._sf() as session:
            row = session.get(OrderIntent, intent_id)
            if row is not None:
                row.executor = "ui_app"
                row.status = status
                row.note = f"{row.note};{' | '.join(note_bits)}".strip(";")
                session.commit()
        return {
            "intent_id": intent_id,
            "status": status,
            "dry_run": self.dry_run,
            "submitted": report.submitted,
            "verified": report.verified,
            "summary": report.summary(),
            "screenshots": report.screenshots,
        }

    def doctor(self) -> dict:
        device = AdbDevice(self.app.adb_path, self.app.device)
        info = device.doctor()
        info.update({
            "dry_run": self.dry_run,
            "package": self.app.package,
            "screenshot_dir": str(get_settings().data_dir / "live_orders"),
        })
        try:
            info["package_installed"] = device.package_installed(self.app.package) \
                if info.get("devices") else False
        except AdbError:
            info["package_installed"] = False
        return info

    def probe(self, save: bool = True) -> dict:
        """导出当前界面控件树与截图（校准选择器用）。"""
        device = AdbDevice(self.app.adb_path, self.app.device)
        device.ensure()
        screen = device.dump()
        out_dir = get_settings().data_dir / "live_probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        result: dict = {
            "device": device.serial,
            "current_package": device.current_package(),
            "nodes": len(screen.nodes),
            "size": list(screen.size),
            "editables": len(screen.editable()),
            "clickable": [
                {"text": n.text, "desc": n.desc, "bounds": list(n.bounds)}
                for n in screen.nodes if n.clickable and n.label
            ][:40],
            "saved": {},
        }
        if save:
            tree = out_dir / f"ui_{stamp}.xml"
            tree.write_text(
                "\n".join(f"{n.cls} | {n.label!r} | {n.bounds}" for n in screen.nodes),
                encoding="utf-8",
            )
            shot = out_dir / f"screen_{stamp}.png"
            device.screenshot(shot)
            result["saved"] = {"tree": str(tree), "screenshot": str(shot)}
        return result
