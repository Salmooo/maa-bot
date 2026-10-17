"""MAA 相关：日志解析、模拟器/MAA 启动、Link Start 热键。"""

from __future__ import annotations

import ctypes
import logging
import re
import socket
import subprocess
import time
from pathlib import Path

from .common import maa_log_path

log = logging.getLogger("maa")

PREFIX_RE = re.compile(r"^\[[^\]]+\]\[[^\]]+\]\[[^\]]+\]\s*<[^>]*>\s*")
ERR_RE = re.compile(r"^\[[^\]]+\]\[ERR\]")
TIME_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
DURATION_RE = re.compile(r"^\(用时 (.+)\)")


def strip_prefix(line: str) -> str:
    return PREFIX_RE.sub("", line)


# ---------------------------------------------------------------- 日志解析


def build_summary(cfg: dict, title: str = "MAA 日常任务") -> str:
    path = maa_log_path(cfg)
    if not path.exists():
        return f"# {title}\nMAA 日志不存在：{path}"

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) > 6000:
        lines = lines[-6000:]

    start_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if "LinkStart Enter" in lines[i]:
            start_idx = i
            break
    segment = lines[start_idx:] if start_idx >= 0 else lines[-120:]

    start_time = ""
    if segment:
        m = TIME_RE.match(segment[0])
        if m:
            start_time = m.group(1)

    steps: list[dict] = []
    errors: list[str] = []
    finished = stopped = False
    total_time = sanity = fight_info = recover = ""

    for i, line in enumerate(segment):
        duration = ""
        if i + 1 < len(segment):
            dm = DURATION_RE.match(segment[i + 1])
            if dm:
                duration = dm.group(1)

        m = re.search(r"完成任务: (.+?)\s*$", line)
        if m:
            steps.append({"name": m.group(1), "ok": True, "time": duration})
        elif m := re.search(r"任务出错: (.+?)\s*$", line):
            steps.append({"name": m.group(1), "ok": False, "time": duration})
        elif "任务已全部完成！" in line:
            finished = True
        elif m := re.search(r"用时 (\d+h \d+m \d+s)", line):
            if finished and not total_time:
                total_time = m.group(1)
        elif "已停止" in line:
            stopped = True
        elif m := re.match(r"理智: (\d+/\d+)", strip_prefix(line)):
            sanity = m.group(1)
        elif m := re.match(r"开始行动 (.+?)$", strip_prefix(line)):
            fight_info = m.group(1)
        elif m := re.match(r"^(理智将在 .+)$", strip_prefix(line)):
            recover = m.group(1)

        if ERR_RE.match(line):
            errors.append(strip_prefix(line))

    err_steps = [s for s in steps if not s["ok"]]
    if finished and not stopped:
        status = "全部完成 ✅"
        if err_steps:
            status = f"完成（含 {len(err_steps)} 个异常）⚠"
    elif stopped and not err_steps:
        status = "已停止 ⏹"
    elif steps:
        status = "未正常结束 ⚠"
    else:
        status = "未检测到本次运行日志 ❓"

    out: list[str] = [f"# {title}", f"> 状态：**{status}**"]
    if start_time:
        out.append(f"> 开始：{start_time}")
    out.append(f"> 结束：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    if total_time:
        out.append(f"> 耗时：{total_time}")
    out.append("")

    if steps:
        out.append("**任务明细**")
        for step in steps:
            mark = "✅" if step["ok"] else "❌"
            suffix = f"（{step['time']}）" if step["time"] else ""
            out.append(f"- {step['name']} {mark}{suffix}")
        out.append("")

    if sanity:
        out.append(f"**理智**：{sanity}")
    if fight_info:
        out.append(f"**作战**：{fight_info}")
    if recover:
        out.append(recover)
    if errors:
        out.append("")
        out.append("**异常信息**")
        for err in errors[-4:]:
            out.append(f"- {err}")
    if not steps and not sanity:
        out.append("_本次未采集到任务明细，请查看 MAA 界面日志。_")
    return "\n".join(out)


class LogWatcher:
    """增量读取 MAA 日志，返回事件列表（不重复处理历史内容）。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.offset = 0
        self.pending = ""
        if self.path.exists():
            self.offset = self.path.stat().st_size

    def poll(self) -> list[tuple[str, dict]]:
        if not self.path.exists():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # 日志被清空或轮转
            self.offset = 0
            self.pending = ""
        if size == self.offset:
            return []

        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            text = self.pending + fh.read()
            self.offset = fh.tell()

        if text.endswith("\n"):
            self.pending = ""
        else:
            idx = text.rfind("\n")
            if idx < 0:
                self.pending = text
                return []
            self.pending = text[idx + 1 :]
            text = text[: idx + 1]

        events: list[tuple[str, dict]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if "LinkStart Enter" in line:
                events.append(("start", {}))
            if m := re.search(r"任务出错: (.+?)\s*$", line):
                events.append(("task_error", {"task": m.group(1)}))
            if "任务已全部完成！" in line:
                events.append(("complete", {}))
            if "任务已结束，以下任务出现错误" in line:
                events.append(("complete", {"errors": True}))
            if "未选择任务" in line:
                events.append(("stopped", {"reason": "未选择任务"}))
            if "已停止" in line and "Stopping" not in line:
                events.append(("stopped", {}))
            if ERR_RE.match(line):
                events.append(("err", {"text": strip_prefix(line)}))
        return events


# ---------------------------------------------------------------- 环境控制


def process_exists(name: str) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    return name.lower() in out.lower()


def adb_port_open(address: str, timeout: float = 1.5) -> bool:
    host, _, port = address.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_ready(cfg: dict, wait: bool = True) -> list[str]:
    """确保 MuMu 与 MAA 处于运行状态，返回处理说明。"""
    notes: list[str] = []
    mumu_exe = Path(cfg["mumu_dir"]) / "nx_main" / "MuMuNxMain.exe"
    if not process_exists("MuMuNxMain.exe"):
        if mumu_exe.exists():
            notes.append("模拟器未运行，正在启动 MuMu ...")
            subprocess.Popen([str(mumu_exe), "--from-shortcut"], cwd=str(mumu_exe.parent))
            if wait:
                for _ in range(24):
                    time.sleep(5)
                    if adb_port_open(cfg["adb_address"]):
                        notes.append(f"模拟器就绪（adb {cfg['adb_address']}）")
                        break
        else:
            notes.append(f"找不到 MuMu 启动器：{mumu_exe}")
    if not adb_port_open(cfg["adb_address"]):
        notes.append(f"警告：adb {cfg['adb_address']} 未就绪")

    maa_exe = Path(cfg["maa_dir"]) / "MAA.exe"
    if not process_exists("MAA.exe"):
        if maa_exe.exists():
            notes.append("MAA 未运行，正在启动 ...")
            subprocess.Popen([str(maa_exe)], cwd=str(maa_exe.parent))
            for _ in range(12):
                time.sleep(5)
                if process_exists("MAA.exe"):
                    notes.append("MAA 已启动")
                    time.sleep(15)
                    break
            else:
                notes.append("MAA 启动失败")
        else:
            notes.append(f"找不到 MAA：{maa_exe}")

    # 公告弹窗会让 MAA 一直等人点确认，这里顺手关掉
    try:
        note = dismiss_announcement()
        if note:
            notes.append(f"公告弹窗：{note}")
    except Exception as exc:  # noqa: BLE001
        log.debug("处理公告弹窗失败：%s", exc)
    return notes


def send_link_start() -> None:
    """模拟 MAA 全局热键 Ctrl + Shift + Alt + L（Link Start）。"""
    vk_control, vk_shift, vk_menu, vk_l = 0x11, 0x10, 0x12, 0x4C
    keyup = 0x0002
    user32 = ctypes.windll.user32
    for vk in (vk_control, vk_shift, vk_menu):
        user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.06)
    user32.keybd_event(vk_l, 0, 0, 0)
    time.sleep(0.06)
    user32.keybd_event(vk_l, 0, keyup, 0)
    for vk in (vk_menu, vk_shift, vk_control):
        user32.keybd_event(vk, 0, keyup, 0)


def log_size(path: Path | str) -> int:
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


def wait_run_started(path: Path | str, from_offset: int, timeout: float = 30.0) -> bool:
    """等待 MAA 日志出现新的 LinkStart Enter。"""
    p = Path(path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.8)
        try:
            if p.stat().st_size <= from_offset:
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(from_offset)
                if "LinkStart Enter" in fh.read():
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------- 公告弹窗

ANNOUNCEMENT_WINDOW = "公告"
ANNOUNCEMENT_CHECKBOX = "下次公告更新前不再显示"
ANNOUNCEMENT_CONFIRM = "确认"


def _iter_controls(control, max_depth: int = 12, depth: int = 0):
    if depth > max_depth:
        return
    yield control
    try:
        children = control.GetChildren()
    except Exception:  # noqa: BLE001
        return
    for child in children:
        yield from _iter_controls(child, max_depth, depth + 1)


def _announcement_container():
    """返回 (所在窗口, 复选框控件)；没有公告弹窗时返回 (None, None)。"""
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return None, None
    auto.SetGlobalSearchTimeout(0.6)

    window = auto.WindowControl(searchDepth=1, Name=ANNOUNCEMENT_WINDOW)
    if window.Exists(0.6, 0.2):
        checkbox = window.CheckBoxControl(searchDepth=0xFFFFFFFF, SubName=ANNOUNCEMENT_CHECKBOX)
        if checkbox.Exists(0.6, 0.2):
            return window, checkbox
        return window, None

    # 兼容公告内嵌在主窗口里的版本
    maa_window = find_maa_window()
    if maa_window is not None:
        checkbox = maa_window.CheckBoxControl(searchDepth=0xFFFFFFFF, SubName=ANNOUNCEMENT_CHECKBOX)
        if checkbox.Exists(0.5, 0.2):
            return maa_window, checkbox
    return None, None


def find_maa_window():
    """返回 MAA 主窗口的 UI Automation 对象（找不到返回 None）。"""
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return None
    auto.SetGlobalSearchTimeout(1)
    try:
        for window in auto.GetRootControl().GetChildren():
            if "MAA" in (window.Name or ""):
                return window
        window = auto.WindowControl(searchDepth=1, RegexName=".*MAA.*")
        return window if window.Exists(1, 0.3) else None
    except Exception:  # noqa: BLE001
        return None


def _scroll_announcement_to_bottom(window) -> bool:
    """把公告正文滚到最底部（正文区是一个可垂直滚动的 Pane）。"""
    candidates: list[tuple[int, object]] = []
    for control in _iter_controls(window):
        try:
            pattern = control.GetScrollPattern()
        except Exception:  # noqa: BLE001
            continue
        if pattern is None:
            continue
        try:
            if not pattern.VerticallyScrollable:
                continue
            rect = control.BoundingRectangle
            height = rect.height() if callable(getattr(rect, "height", None)) else (rect.bottom - rect.top)
        except Exception:  # noqa: BLE001
            height = 0
        # 正文区通常是 Pane，且比左侧目录列表更宽/更高
        priority = 0 if control.ControlTypeName == "PaneControl" else 1
        candidates.append((priority * 10_000_000 - height, control))

    if not candidates:
        return False
    if any(control.ControlTypeName == "PaneControl" for _, control in candidates):
        candidates = [(k, c) for k, c in candidates if c.ControlTypeName == "PaneControl"]
    candidates.sort(key=lambda item: item[0])

    for _, control in candidates:
        try:
            pattern = control.GetScrollPattern()
            pattern.SetScrollPercent(-1, 100)  # -1 == NoScroll（只滚垂直方向）
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def dismiss_announcement(tick_do_not_show: bool = True) -> str:
    """若 MAA 正在显示公告弹窗，则滚动到底部并点击确认。

    返回空字符串表示没有公告弹窗；否则返回处理结果描述（可推送到聊天）。
    """
    window, checkbox = _announcement_container()
    if window is None:
        return ""

    log.info("检测到 MAA 公告弹窗，尝试自动处理")
    notes: list[str] = []

    if _scroll_announcement_to_bottom(window):
        notes.append("已滚动到底部")
    else:
        notes.append("未找到滚动区域")
    time.sleep(0.4)

    if tick_do_not_show and checkbox is not None:
        try:
            if not checkbox.IsEnabled:
                notes.append("「不再显示」暂不可勾选")
            else:
                toggle = checkbox.GetTogglePattern()
                if toggle and toggle.ToggleState == 0:
                    checkbox.Click(simulateMove=False)
                    notes.append("已勾选「下次公告更新前不再显示」")
                else:
                    notes.append("「不再显示」已勾选")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"勾选失败：{exc}")
    time.sleep(0.3)

    confirm = window.ButtonControl(searchDepth=0xFFFFFFFF, SubName=ANNOUNCEMENT_CONFIRM)
    if not confirm.Exists(0.6, 0.2):
        notes.append("未找到确认按钮 ⚠")
    else:
        try:
            confirm.Click(simulateMove=False)
            notes.append("已点击确认")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"点击确认失败：{exc}")

    time.sleep(0.8)
    try:
        still_open = window.Exists(0.5, 0.2)
    except Exception:  # noqa: BLE001
        still_open = False
    notes.append("弹窗已关闭 ✅" if not still_open else "弹窗仍未关闭，请手动点一下 ⚠")
    result = "；".join(notes)
    log.info("公告处理结果：%s", result)
    return result


def stop_maa_run() -> str:
    """点击 MAA 主界面上的「停止」按钮，停止当前任务。"""
    if click_visible_control("ButtonControl", "停止"):
        time.sleep(1)
        return "已点击停止"
    return "没找到可见的停止按钮"


def maa_run_active() -> bool:
    """MAA 正在跑任务时，界面上显示的是「停止」按钮（隐藏的 Link Start! 不可见）。"""
    return bool(visible_controls("ButtonControl", "停止"))


def maa_idle_from_log(cfg: dict):
    """从 MAA 日志判断是否空闲（窗口最小化时 UIA 不可靠，用日志更稳）。"""
    path = maa_log_path(cfg)
    if not Path(path).exists():
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-400:]
    except OSError:
        return None
    state = None
    for line in lines:
        if "Idle: true to false" in line:
            state = "running"
        elif "Idle: false to true" in line:
            state = "idle"
    return None if state is None else state == "idle"


# ---------------------------------------------------------------- 掉线恢复

GAME_PACKAGE = "com.hypergryph.arknights"


def adb_run(cfg: dict, *args, timeout: int = 25) -> subprocess.CompletedProcess:
    exe = str(cfg.get("adb_emu_path") or "adb")
    target = str(cfg.get("adb_address") or "")
    cmd = [exe]
    if target:
        cmd += ["-s", target]
    cmd += [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)


def game_running(cfg: dict) -> bool:
    package = str(cfg.get("game_package") or GAME_PACKAGE)
    try:
        return bool(adb_run(cfg, "shell", "pidof", package).stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def game_foreground(cfg: dict) -> bool:
    """游戏是否在前台（pidof 只能说明进程在，可能卡在后台/黑屏）。"""
    package = str(cfg.get("game_package") or GAME_PACKAGE)
    try:
        out = adb_run(cfg, "shell", "dumpsys", "activity", "activities", timeout=40).stdout
    except Exception:  # noqa: BLE001
        return False
    for line in out.splitlines():
        if ("mResumedActivity" in line or "topResumedActivity" in line) and package in line:
            return True
    return False


def screen_size(cfg: dict):
    try:
        out = adb_run(cfg, "shell", "wm", "size").stdout
    except Exception:  # noqa: BLE001
        return None
    match = re.search(r"(\d+)\s*x\s*(\d+)", out)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def current_rotation(cfg: dict) -> int:
    """返回屏幕旋转（0=竖屏，1/3=横屏）。取不到时返回 -1。"""
    for args, pattern in (
        (("shell", "dumpsys", "input"), r"SurfaceOrientation:\s*(\d+)"),
        (("shell", "dumpsys", "display"), r"mCurrentOrientation=(\d+)"),
        (("shell", "dumpsys", "window", "displays"), r"mRotation=(\d+)"),
    ):
        try:
            out = adb_run(cfg, *args, timeout=30).stdout
        except Exception:  # noqa: BLE001
            continue
        match = re.search(pattern, out)
        if match:
            value = int(match.group(1))
            return value if value in (0, 1, 2, 3) else -1
    return -1


def rotated_screen_size(cfg: dict):
    """返回当前游戏方向下的 (宽, 高)，用于换算点击坐标。"""
    size = screen_size(cfg)
    if not size:
        return None
    width, height = size
    rotation = current_rotation(cfg)
    if rotation in (1, 3) and height > width:
        return height, width
    if rotation == 0 and width > height:
        return width, height
    return width, height


def tap_reconnect_dialog(cfg: dict) -> str:
    """点游戏「与神经网络连接丢失，请重新连接」弹窗里的确认按钮。"""
    size = rotated_screen_size(cfg)
    if not size:
        return "拿不到模拟器分辨率，跳过点击"
    width, height = size
    x, y = int(width * 0.482), int(height * 0.669)
    try:
        adb_run(cfg, "shell", "input", "tap", str(x), str(y))
    except Exception as exc:  # noqa: BLE001
        return f"点击重连弹窗失败：{exc}"
    return f"已点击重连弹窗确认按钮（{x},{y}）"


def restart_game(cfg: dict, wait_seconds: int = 40) -> str:
    """强制关闭并重新启动游戏客户端（等同重新登录）。"""
    package = str(cfg.get("game_package") or GAME_PACKAGE)
    try:
        adb_run(cfg, "shell", "am", "force-stop", package)
        time.sleep(3)
        launch_game(cfg, wait_seconds=0)
    except Exception as exc:  # noqa: BLE001
        return f"重启游戏失败：{exc}"
    time.sleep(max(wait_seconds, 0))
    return f"已重启游戏客户端（{package}），等待重新登录"


def launch_game(cfg: dict, wait_seconds: int = 30) -> str:
    """启动游戏客户端（没在运行时用）。"""
    package = str(cfg.get("game_package") or GAME_PACKAGE)
    try:
        adb_run(cfg, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
    except Exception as exc:  # noqa: BLE001
        return f"启动游戏失败：{exc}"
    time.sleep(max(wait_seconds, 0))
    return f"已启动游戏客户端（{package}）"


def recover_game(cfg: dict, attempt: int = 1) -> str:
    """掉线恢复：第一次先点弹窗，之后直接重启游戏客户端。"""
    notes: list[str] = []
    if attempt <= 1:
        notes.append(tap_reconnect_dialog(cfg))
        time.sleep(5)
        if not game_foreground(cfg):
            notes.append(launch_game(cfg, wait_seconds=45))
    else:
        notes.append(restart_game(cfg))
    return "；".join(n for n in notes if n)


# ---------------------------------------------------------------- 任务全选 / 起跳

TASK_TAB = "TaskQueueViewModel"
HEAVY_TASKS = ("自动肉鸽", "生息演算")
STANDARD_TASKS = ("开始唤醒", "理智作战", "基建换班", "自动公招", "信用收支", "领取奖励", "更新数据")


def _task_tab(window=None):
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return None
    auto.SetGlobalSearchTimeout(1)
    window = window or find_maa_window()
    if window is None:
        return None
    tab = window.TabItemControl(searchDepth=0xFFFFFFFF, SubName=TASK_TAB)
    return tab if tab.Exists(1, 0.2) else None


def _task_list(window=None):
    """返回一键长草页里的任务列表控件。"""
    tab = _task_tab(window)
    if tab is None:
        return None
    for control in _iter_controls(tab, max_depth=4):
        if control.ControlTypeName != "ListControl":
            continue
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            continue
        if children and any((c.Name or "").startswith("MaaWpfGui.ViewModels.TaskItemViewModel") for c in children):
            return control
    return None


def task_states(window=None) -> dict[str, int]:
    """返回 {任务名: 0/1}（1 表示已勾选），只包含任务卡本身的勾选状态。"""
    task_list = _task_list(window)
    if task_list is None:
        return {}
    states: dict[str, int] = {}
    for item in task_list.GetChildren():
        try:
            children = item.GetChildren()
        except Exception:  # noqa: BLE001
            continue
        for child in children:
            if child.ControlTypeName != "CheckBoxControl" or not child.Name:
                continue
            try:
                states[child.Name] = int(child.GetTogglePattern().ToggleState)
            except Exception:  # noqa: BLE001
                continue
    return states


def select_all_tasks(window=None) -> str:
    """点击「全选」，并确保不会顺带打开自动肉鸽 / 生息演算。"""
    tab = _task_tab(window)
    if tab is None:
        return ""
    task_list = _task_list(window)
    notes: list[str] = []
    if click_visible_control("ButtonControl", "全选", window, tab):
        notes.append("已点击全选")
        time.sleep(1.2)
    else:
        notes.append("找不到全选按钮")

    # 点全选后 MAA 会重建任务列表，这里重新抓几次
    states: dict[str, int] = {}
    for _ in range(4):
        task_list = _task_list(window)
        states = task_states(window)
        if states:
            break
        time.sleep(0.8)

    # 统一校正：只保留标准日常任务，其余（自动肉鸽/生息演算/干员培养…）一律关掉
    fixed: list[str] = []
    for name, state in list(states.items()):
        want = 1 if name in STANDARD_TASKS else 0
        if int(state) != want:
            click_visible_control("CheckBoxControl", name, window, task_list)
            fixed.append(("打开 " if want else "关闭 ") + name)
            time.sleep(0.35)
    if fixed:
        notes.append("校正：" + "、".join(fixed))
        states = task_states(window) or states

    states = task_states(window) or states
    enabled = [name for name, state in states.items() if state == 1]
    if enabled:
        notes.append("启用：" + "、".join(enabled))
    ok = all(states.get(name) == 1 for name in STANDARD_TASKS) and all(
        state == 0 for name, state in states.items() if name not in STANDARD_TASKS
    )
    if not ok:
        notes.append("⚠ 任务勾选异常，请人工确认一次")
    result = "；".join(notes)
    log.info("任务选择：%s", result)
    return result


def click_link_start(window=None) -> bool:
    """点击 MAA 上「可见的」Link Start! 按钮。"""
    return click_visible_control("ButtonControl", "Link Start!", window)


def _window_bounds(window):
    try:
        rect = window.BoundingRectangle
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:  # noqa: BLE001
        return None


def visible_controls(control_type: str, name: str, window=None, scope=None):
    """找出可见（且中心在窗口内）的同名控件。

    MAA 的视觉树里同一个按钮常常有两份（一份隐藏、坐标甚至落在窗口外），
    直接取第一个会点到别的程序，所以必须过滤。
    """
    window = window or find_maa_window()
    if window is None:
        return []
    scope = scope or window
    bounds = _window_bounds(window)
    found = []
    for control in _iter_controls(scope, max_depth=12):
        if control.ControlTypeName != control_type:
            continue
        if (control.Name or "").strip() != name:
            continue
        try:
            if control.IsOffscreen:
                continue
            rect = control.BoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            if bounds and not (bounds[0] <= cx <= bounds[2] and bounds[1] <= cy <= bounds[3]):
                continue
        except Exception:  # noqa: BLE001
            continue
        found.append((control, rect))
    return found


def _mouse_click(x: int, y: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP


def click_visible_control(control_type: str, name: str, window=None, scope=None) -> bool:
    """优先用 UIA Invoke（不移动鼠标），不支持时才用鼠标点中心。"""
    for control, rect in visible_controls(control_type, name, window, scope):
        try:
            pattern = control.GetInvokePattern()
        except Exception:  # noqa: BLE001
            pattern = None
        try:
            if pattern is not None:
                pattern.Invoke()
                log.info("已 Invoke %s (%s,%s)", name, rect.left, rect.top)
                return True
            _mouse_click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
            log.info("已鼠标点击 %s (%s,%s)", name, rect.left, rect.top)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("点击 %s 失败：%s", name, exc)
    log.warning("找不到可见的 %s 控件（%s）", name, control_type)
    return False
