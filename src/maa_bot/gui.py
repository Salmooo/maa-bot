"""MAA 长草自动化控制台（图形界面）。

功能：配置企业微信机器人、管理游戏账号、立即执行清日常、查看状态与日志、
      校准「账号密码登录」兜底坐标。

启动：python run.py gui 或双击 start_gui.cmd
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .adb_login import calibrated, login_with_password, screenshot  # noqa: E402
from .common import (  # noqa: E402
    ROOT,
    load_config,
    save_config,
    service_alive_seconds,
    state_path,
    validate_config,
)
from .maa_config import (  # noqa: E402
    ROGUELIKE_AWARDS,
    ROGUELIKE_MODE_NAMES,
    ROGUELIKE_THEME_NAMES,
    get_roguelike,
    set_roguelike,
)
from .wecom import send_message_once  # noqa: E402

COMMAND_FILE = Path(ROOT) / "gui-command.json"


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MAA 长草自动化控制台")
        self.geometry("880x620")
        self.cfg = load_config()
        self.pick_target = tk.StringVar(value="account_field")
        self.status_text = tk.StringVar(value="")
        self.shot_image = None
        self.shot_scale = 1.0
        self.shot_offset = (0, 0)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_bot_tab(notebook)
        self._build_runtime_tab(notebook)
        self._build_accounts_tab(notebook)
        self._build_run_tab(notebook)
        self._build_roguelike_tab(notebook)
        self._build_login_tab(notebook)
        self._build_about_tab(notebook)

        self.refresh_status()

    # ------------------------------------------------ 机器人

    def _build_bot_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="机器人")
        self.bot_id = tk.StringVar(value=self.cfg.get("bot_id", ""))
        self.secret = tk.StringVar(value=self.cfg.get("secret", ""))
        self.chat_id = tk.StringVar(value=self.cfg.get("chat_id", ""))
        self.chat_type = tk.StringVar(value=str(self.cfg.get("chat_type", 1)))
        self.ws_url = tk.StringVar(value=self.cfg.get("ws_url", "wss://openws.work.weixin.qq.com"))
        self.heartbeat = tk.StringVar(value=str(self.cfg.get("heartbeat_seconds", 25)))

        rows = [
            ("机器人 BotID", self.bot_id),
            ("机器人 Secret", self.secret),
            ("会话 ID（chatid）", self.chat_id),
            ("会话类型（1 单聊 / 2 群聊）", self.chat_type),
            ("长连接地址", self.ws_url),
            ("心跳间隔（秒）", self.heartbeat),
        ]
        for index, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(
                frame,
                textvariable=var,
                width=60,
                show="*" if var is self.secret else "",
            ).grid(row=index, column=1, sticky="we", padx=10, pady=6)
        frame.columnconfigure(1, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(rows), column=0, columnspan=2, sticky="w", padx=10, pady=12)
        ttk.Button(buttons, text="保存", command=self.save_bot).pack(side="left", padx=4)
        ttk.Button(buttons, text="发送测试消息", command=self.send_test).pack(side="left", padx=4)
        ttk.Button(buttons, text="重启服务", command=self.restart_service).pack(side="left", padx=4)

        ttk.Label(
            frame,
            text="提示：会话 ID 留空也可以——你给机器人发一条消息后会自动记录；\n群里推送需要先在群聊里 @ 一次机器人。",
            foreground="#666",
            justify="left",
        ).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=10, pady=6)

    def save_bot(self) -> None:
        try:
            chat_type = int(self.chat_type.get() or 1)
            heartbeat = int(self.heartbeat.get() or 25)
        except ValueError:
            messagebox.showerror("保存失败", "会话类型和心跳间隔必须是整数")
            return
        self.cfg["bot_id"] = self.bot_id.get().strip()
        self.cfg["secret"] = self.secret.get().strip()
        self.cfg["chat_id"] = self.chat_id.get().strip()
        self.cfg["chat_type"] = chat_type
        self.cfg["ws_url"] = self.ws_url.get().strip()
        self.cfg["heartbeat_seconds"] = max(5, heartbeat)
        save_config(self.cfg)
        messagebox.showinfo("已保存", "机器人配置已保存到 config.json")
        self.refresh_status()

    def send_test(self) -> None:
        self.save_bot()
        age = service_alive_seconds()
        alive = age is not None and age < float(self.cfg.get("service_alive_seconds", 150))
        if not alive:
            try:
                response = asyncio.run(
                    send_message_once(
                        self.cfg,
                        f"✅ 测试消息（来自控制台）\n{time.strftime('%Y-%m-%d %H:%M:%S')}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("发送失败", str(exc))
                return
            if not response or response.get("errcode") != 0:
                messagebox.showerror("发送失败", f"企业微信返回：{response}")
                return
            messagebox.showinfo("已发送", "测试消息已直接发送")
            return
        cmd = {"cmd": "test_push", "ts": time.time()}
        COMMAND_FILE.write_text(json.dumps(cmd, ensure_ascii=False), encoding="utf-8")
        messagebox.showinfo("已发送", "已请求服务发送一条测试消息（几秒内送达）")

    # ------------------------------------------------ 运行设置

    def _build_runtime_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="运行设置")
        self.maa_dir = tk.StringVar(value=self.cfg.get("maa_dir", ""))
        self.maa_log = tk.StringVar(value=self.cfg.get("maa_log", ""))
        self.mumu_dir = tk.StringVar(value=self.cfg.get("mumu_dir", ""))
        self.adb_emu_path = tk.StringVar(value=self.cfg.get("adb_emu_path", ""))
        self.adb_address = tk.StringVar(value=self.cfg.get("adb_address", ""))
        self.schedule_times = tk.StringVar(value=", ".join(self.cfg.get("schedule_times") or []))
        self.allowed_chat_ids = tk.StringVar(value=", ".join(self.cfg.get("allowed_chat_ids") or []))
        self.allow_group = tk.BooleanVar(value=bool(self.cfg.get("allow_group_chat")))
        self.deepseek_key = tk.StringVar(value=self.cfg.get("deepseek_api_key", ""))
        self.deepseek_model = tk.StringVar(value=self.cfg.get("deepseek_model", "deepseek-flash"))
        self.ai_enabled = tk.BooleanVar(value=bool(self.cfg.get("ai_enabled", True)))
        self.catch_up = tk.BooleanVar(value=bool(self.cfg.get("catch_up_missed", True)))

        rows = [
            ("MAA 目录", self.maa_dir),
            ("MAA 日志（留空自动）", self.maa_log),
            ("MuMu 目录", self.mumu_dir),
            ("ADB 可执行文件", self.adb_emu_path),
            ("ADB 地址", self.adb_address),
            ("每日执行时间（逗号分隔）", self.schedule_times),
            ("允许的会话 ID（逗号分隔）", self.allowed_chat_ids),
            ("DeepSeek API Key", self.deepseek_key),
            ("DeepSeek 模型", self.deepseek_model),
        ]
        for index, (label, var) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", padx=10, pady=5)
            ttk.Entry(
                frame,
                textvariable=var,
                width=66,
                show="*" if var is self.deepseek_key else "",
            ).grid(row=index, column=1, sticky="we", padx=10, pady=5)
        frame.columnconfigure(1, weight=1)

        options = ttk.Frame(frame)
        options.grid(row=len(rows), column=0, columnspan=2, sticky="w", padx=10, pady=8)
        ttk.Checkbutton(options, text="允许群聊控制", variable=self.allow_group).pack(side="left", padx=4)
        ttk.Checkbutton(options, text="启用 DeepSeek 意图解析", variable=self.ai_enabled).pack(side="left", padx=12)
        ttk.Checkbutton(options, text="错过定时后补跑", variable=self.catch_up).pack(side="left", padx=12)
        ttk.Button(options, text="保存运行设置", command=self.save_runtime).pack(side="left", padx=12)

        ttk.Label(
            frame,
            text="安全提示：会话 ID 为空时只绑定第一条私聊；绑定后其他私聊不会获得控制权限。\n"
            "环境变量 MAA_BOT_ID / MAA_BOT_SECRET / DEEPSEEK_API_KEY 的优先级高于此处配置。",
            foreground="#666",
            justify="left",
        ).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=10, pady=8)

    def save_runtime(self) -> None:
        schedule = [value.strip() for value in self.schedule_times.get().replace("，", ",").split(",") if value.strip()]
        allowed = [
            value.strip() for value in self.allowed_chat_ids.get().replace("，", ",").split(",") if value.strip()
        ]
        self.cfg.update(
            {
                "maa_dir": self.maa_dir.get().strip(),
                "maa_log": self.maa_log.get().strip(),
                "mumu_dir": self.mumu_dir.get().strip(),
                "adb_emu_path": self.adb_emu_path.get().strip(),
                "adb_address": self.adb_address.get().strip(),
                "schedule_times": schedule,
                "allowed_chat_ids": allowed,
                "allow_group_chat": bool(self.allow_group.get()),
                "deepseek_api_key": self.deepseek_key.get().strip(),
                "deepseek_model": self.deepseek_model.get().strip() or "deepseek-flash",
                "ai_enabled": bool(self.ai_enabled.get()),
                "catch_up_missed": bool(self.catch_up.get()),
            }
        )
        problems = validate_config(self.cfg, require_bot=False)
        save_config(self.cfg)
        if problems:
            messagebox.showwarning("已保存，但配置仍需处理", "\n".join(problems))
        else:
            messagebox.showinfo("已保存", "运行设置已保存")

    # ------------------------------------------------ 账号

    def _build_accounts_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="账号")

        columns = ("name", "phone", "password", "switch", "stage")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        for key, title, width in (
            ("name", "名称", 120),
            ("phone", "登录账号（手机号）", 180),
            ("password", "密码", 160),
            ("switch", "MAA 切换值", 160),
            ("stage", "理智关卡", 140),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=10)
        self.tree.bind("<Double-1>", lambda _event: self.edit_account())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=6)
        ttk.Button(buttons, text="添加", command=self.add_account).pack(side="left", padx=4)
        ttk.Button(buttons, text="编辑", command=self.edit_account).pack(side="left", padx=4)
        ttk.Button(buttons, text="删除", command=self.delete_account).pack(side="left", padx=4)
        ttk.Button(buttons, text="上移", command=lambda: self.move_account(-1)).pack(side="left", padx=4)
        ttk.Button(buttons, text="下移", command=lambda: self.move_account(1)).pack(side="left", padx=4)
        ttk.Button(buttons, text="保存", command=self.save_accounts).pack(side="left", padx=12)

        ttk.Label(
            frame,
            text="说明：每次「清日常」会依次用这些账号各跑一遍；\n"
            "「MAA 切换值」填游戏登录界面显示的账号（可只填后 4 位，如 9083）；\n"
            "「理智关卡」留空表示沿用游戏内的「当前/上次」，也可填 1-7 / CE-6 / LS-6 等；\n"
            "每次开始前会自动「全选」任务（自动肉鸽、生息演算会被强制关闭）。",
            foreground="#666",
            justify="left",
        ).pack(anchor="w", padx=10, pady=6)
        self.reload_accounts()

    def reload_accounts(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for account in self.cfg.get("accounts", []):
            self.tree.insert(
                "",
                "end",
                values=(
                    account.get("name", ""),
                    account.get("phone", ""),
                    "********" if account.get("password") else "",
                    account.get("switch_name", ""),
                    account.get("stage", ""),
                ),
            )

    def _account_dialog(self, account: dict | None = None) -> dict | None:
        win = tk.Toplevel(self)
        win.title("账号信息")
        win.transient(self)
        win.grab_set()
        fields = {
            "name": tk.StringVar(value=(account or {}).get("name", "")),
            "phone": tk.StringVar(value=(account or {}).get("phone", "")),
            "password": tk.StringVar(value=(account or {}).get("password", "")),
            "switch_name": tk.StringVar(value=(account or {}).get("switch_name", "")),
            "stage": tk.StringVar(value=(account or {}).get("stage", "")),
        }
        labels = {
            "name": "名称",
            "phone": "登录账号（手机号）",
            "password": "密码",
            "switch_name": "MAA 切换值（登录界面显示的账号/后 4 位）",
            "stage": "理智关卡（留空＝当前/上次，例：1-7）",
        }
        for index, (key, var) in enumerate(fields.items()):
            ttk.Label(win, text=labels[key]).grid(row=index, column=0, sticky="w", padx=10, pady=6)
            ttk.Entry(
                win,
                textvariable=var,
                width=42,
                show="*" if key == "password" else "",
            ).grid(row=index, column=1, sticky="we", padx=10, pady=6)
        win.columnconfigure(1, weight=1)
        result: dict[str, str] = {}

        def ok() -> None:
            result.update({k: v.get().strip() for k, v in fields.items()})
            win.destroy()

        row = len(fields)
        ttk.Button(win, text="确定", command=ok).grid(row=row, column=0, padx=10, pady=12)
        ttk.Button(win, text="取消", command=win.destroy).grid(row=row, column=1, sticky="w", padx=10, pady=12)
        self.wait_window(win)
        return result or None

    def add_account(self) -> None:
        data = self._account_dialog()
        if not data:
            return
        if not data.get("switch_name") and data.get("phone"):
            data["switch_name"] = data["phone"][-4:]
        self.cfg.setdefault("accounts", []).append(data)
        self.reload_accounts()

    def edit_account(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        account = self.cfg["accounts"][index]
        data = self._account_dialog(account)
        if not data:
            return
        self.cfg["accounts"][index] = data
        self.reload_accounts()

    def delete_account(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        if messagebox.askyesno("确认", "确定删除该账号？"):
            self.cfg["accounts"].pop(index)
            self.reload_accounts()

    def move_account(self, delta: int) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        index = self.tree.index(selection[0])
        new_index = index + delta
        accounts = self.cfg.get("accounts", [])
        if 0 <= new_index < len(accounts):
            accounts[index], accounts[new_index] = accounts[new_index], accounts[index]
            self.reload_accounts()
            self.tree.selection_set(self.tree.get_children()[new_index])

    def save_accounts(self) -> None:
        save_config(self.cfg)
        messagebox.showinfo("已保存", "账号列表已保存")

    # ------------------------------------------------ 运行

    def _build_run_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="运行")

        ttk.Label(frame, textvariable=self.status_text, justify="left").pack(anchor="w", padx=12, pady=10)

        row = ttk.Frame(frame)
        row.pack(anchor="w", padx=12, pady=6)
        ttk.Button(row, text="立即执行（全部账号）", command=self.run_now).pack(side="left", padx=4)
        ttk.Button(row, text="刷新状态", command=self.refresh_status).pack(side="left", padx=4)
        ttk.Button(row, text="重启推送服务", command=self.restart_service).pack(side="left", padx=4)

        row2 = ttk.Frame(frame)
        row2.pack(anchor="w", padx=12, pady=6)
        ttk.Button(row2, text="打开 MAA", command=self.open_maa).pack(side="left", padx=4)
        ttk.Button(
            row2, text="打开 MAA 日志", command=lambda: self.open_path(Path(self.cfg["maa_dir"]) / "debug")
        ).pack(side="left", padx=4)
        ttk.Button(row2, text="打开程序目录", command=lambda: self.open_path(ROOT)).pack(side="left", padx=4)
        ttk.Button(row2, text="更新计划任务", command=self.install_tasks).pack(side="left", padx=4)

        self.cycle_label = ttk.Label(frame, text="", justify="left", foreground="#444")
        self.cycle_label.pack(anchor="w", padx=12, pady=10)

    def refresh_status(self) -> None:
        age = service_alive_seconds()
        if age is None:
            alive = "未运行（service-alive.txt 不存在）"
        elif age < float(self.cfg.get("service_alive_seconds", 150)):
            alive = f"运行中（心跳 {int(age)} 秒前）"
        else:
            alive = f"疑似已停止（心跳 {int(age)} 秒前）"
        state = {}
        state_file = state_path()
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state = {}
        lines = [f"推送服务：{alive}"]
        if state.get("last_end"):
            lines.append(f"上次运行：{state.get('last_start')} → {state.get('last_end')}（{state.get('last_status')}）")
        if state.get("cycle_active"):
            lines.append(f"当前周期：第 {state.get('cycle_done')}/{state.get('cycle_total')} 个账号")
        accounts = self.cfg.get("accounts", [])
        lines.append(f"账号数量：{len(accounts)}（每次清日常全部执行）")
        self.status_text.set("\n".join(lines))

    def service_is_alive(self) -> bool:
        age = service_alive_seconds()
        return age is not None and age < float(self.cfg.get("service_alive_seconds", 150))

    def ensure_service(self) -> bool:
        if self.service_is_alive():
            return True
        if not messagebox.askyesno("推送服务未运行", "常驻服务未运行，是否现在启动？"):
            return False
        self.restart_service()
        return self.service_is_alive()

    def run_now(self) -> None:
        self.save_bot()
        if not self.ensure_service():
            return
        if not messagebox.askyesno("确认", "现在立即执行一次清日常（所有账号依次执行）？"):
            return
        COMMAND_FILE.write_text(json.dumps({"cmd": "run_all", "ts": time.time()}, ensure_ascii=False), encoding="utf-8")
        messagebox.showinfo("已发送", "已请求服务立即执行；状态会推送到企业微信")
        self.refresh_status()

    def open_maa(self) -> None:
        subprocess.Popen([str(Path(self.cfg["maa_dir"]) / "MAA.exe")], cwd=self.cfg["maa_dir"])

    def open_path(self, path: Path) -> None:
        os.startfile(str(path))  # noqa: S606

    def restart_service(self) -> None:
        lock = Path(ROOT) / "service.pid"
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pid = 0
            if pid:
                query = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True,
                    text=True,
                    errors="ignore",
                )
                if "python" in (query.stdout or "").lower():
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                    )
                    time.sleep(2)
            with contextlib.suppress(OSError):
                lock.unlink()
        subprocess.Popen(
            [pythonw(), str(Path(ROOT) / "run.py"), "service"],
            cwd=str(ROOT),
        )
        time.sleep(6)
        self.refresh_status()
        messagebox.showinfo("已重启", "推送服务已重启")

    def install_tasks(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(ROOT) / "run.py"), "install-tasks"],
            capture_output=True,
            text=True,
            errors="ignore",
        )
        messagebox.showinfo("计划任务", (proc.stdout or proc.stderr or "完成")[-1500:])

    # ------------------------------------------------ 自动肉鸽

    def _build_roguelike_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="自动肉鸽")
        task = get_roguelike(self.cfg) or {}

        opers = "，".join(str(o.get("Name")) for o in (task.get("StartingOpers") or []) if o.get("Name"))
        awards_map = {v: k for k, v in ROGUELIKE_AWARDS.items()}
        awards = "，".join(
            awards_map.get(x.strip(), x.strip())
            for x in str(task.get("CollectibleStartAwards") or "").split(",")
            if x.strip()
        )

        self.rl_theme = tk.StringVar(value=ROGUELIKE_THEME_NAMES.get(str(task.get("Theme")), "黑流树海"))
        self.rl_mode = tk.StringVar(value=ROGUELIKE_MODE_NAMES.get(str(task.get("Mode")), "刷等级"))
        self.rl_difficulty = tk.StringVar(value=str(task.get("Difficulty", 1)))
        self.rl_squad = tk.StringVar(value=str(task.get("Squad") or ""))
        self.rl_roles = tk.StringVar(value=str(task.get("Roles") or ""))
        self.rl_opers = tk.StringVar(value=opers)
        self.rl_invest = tk.BooleanVar(value=bool(task.get("Investment")))
        self.rl_invest_count = tk.StringVar(value=str(task.get("InvestCount", 999)))
        self.rl_awards = tk.StringVar(value=awards)
        self.rl_start_count = tk.StringVar(value=str(task.get("StartCount", 999999)))
        self.rl_stop_level_max = tk.BooleanVar(value=bool(task.get("StopWhenLevelMax")))
        self.rl_stop_boss = tk.BooleanVar(value=bool(task.get("StopAtFinalBoss")))
        self.rl_elite2 = tk.BooleanVar(value=bool(task.get("StartWithEliteTwo")))
        accounts = [str(a.get("name") or "") for a in (self.cfg.get("accounts") or [])]
        self.rl_account = tk.StringVar(value=accounts[0] if accounts else "")

        def label(text: str, row: int) -> None:
            ttk.Label(frame, text=text).grid(row=row, column=0, sticky="w", padx=10, pady=5)

        label("主题", 0)
        ttk.Combobox(
            frame, textvariable=self.rl_theme, width=18, state="readonly", values=list(ROGUELIKE_THEME_NAMES.values())
        ).grid(row=0, column=1, sticky="w", padx=6)
        label("模式", 0 + 1)
        ttk.Combobox(
            frame, textvariable=self.rl_mode, width=18, state="readonly", values=list(ROGUELIKE_MODE_NAMES.values())
        ).grid(row=1, column=1, sticky="w", padx=6)
        label("难度", 2)
        ttk.Spinbox(frame, textvariable=self.rl_difficulty, from_=0, to=15, width=8).grid(
            row=2, column=1, sticky="w", padx=6
        )

        label("分队（留空＝默认）", 3)
        ttk.Entry(frame, textvariable=self.rl_squad, width=22).grid(row=3, column=1, sticky="w", padx=6)
        label("职业组（留空＝默认）", 4)
        ttk.Entry(frame, textvariable=self.rl_roles, width=22).grid(row=4, column=1, sticky="w", padx=6)
        label("开局干员（逗号分隔）", 5)
        ttk.Entry(frame, textvariable=self.rl_opers, width=34).grid(row=5, column=1, sticky="w", padx=6)

        ttk.Checkbutton(frame, text="投资源石锭", variable=self.rl_invest).grid(row=6, column=1, sticky="w", padx=6)
        label("投资次数", 7)
        ttk.Spinbox(frame, textvariable=self.rl_invest_count, from_=0, to=9999, width=8).grid(
            row=7, column=1, sticky="w", padx=6
        )
        label("期望奖励（如 热水壶，希望）", 8)
        ttk.Entry(frame, textvariable=self.rl_awards, width=22).grid(row=8, column=1, sticky="w", padx=6)
        label("开局次数", 9)
        ttk.Spinbox(frame, textvariable=self.rl_start_count, from_=1, to=999999, width=10).grid(
            row=9, column=1, sticky="w", padx=6
        )

        ttk.Checkbutton(frame, text="满级后自动停止", variable=self.rl_stop_level_max).grid(
            row=10, column=1, sticky="w", padx=6
        )
        ttk.Checkbutton(frame, text="第五层 BOSS 前暂停", variable=self.rl_stop_boss).grid(
            row=11, column=1, sticky="w", padx=6
        )
        ttk.Checkbutton(frame, text="凹 ｢开局干员｣ 直升精二", variable=self.rl_elite2).grid(
            row=12, column=1, sticky="w", padx=6
        )

        row = ttk.Frame(frame)
        row.grid(row=13, column=0, columnspan=2, sticky="w", padx=10, pady=12)
        ttk.Button(row, text="读取当前设置", command=self.reload_roguelike).pack(side="left", padx=4)
        ttk.Button(row, text="保存到 MAA", command=self.save_roguelike).pack(side="left", padx=4)
        ttk.Label(row, text="   用账号：").pack(side="left")
        ttk.Combobox(row, textvariable=self.rl_account, width=12, state="readonly", values=accounts).pack(side="left")
        ttk.Button(row, text="开始自动肉鸽", command=self.start_roguelike_gui).pack(side="left", padx=8)

        ttk.Label(
            frame,
            text="说明：保存/开始都会把设置写进 MAA（需要重启 MAA 约 1 分钟）；开始时只会启用「开始唤醒 + 自动肉鸽」，\n"
            "跑完或停止后会自动把 MAA 恢复成日常任务集，不影响 08:00 / 20:00 的定时。",
            foreground="#666",
            justify="left",
        ).grid(row=14, column=0, columnspan=2, sticky="w", padx=10, pady=6)

    def _roguelike_options(self) -> dict:
        return {
            "主题": self.rl_theme.get(),
            "模式": self.rl_mode.get(),
            "难度": self.rl_difficulty.get(),
            "分队": self.rl_squad.get(),
            "职业组": self.rl_roles.get(),
            "开局干员": self.rl_opers.get(),
            "投资": "开" if self.rl_invest.get() else "关",
            "投资次数": self.rl_invest_count.get(),
            "期望奖励": self.rl_awards.get(),
            "开局次数": self.rl_start_count.get(),
            "满级停止": "开" if self.rl_stop_level_max.get() else "关",
            "BOSS前暂停": "开" if self.rl_stop_boss.get() else "关",
            "凹精英二": "开" if self.rl_elite2.get() else "关",
        }

    def reload_roguelike(self) -> None:
        self.cfg = load_config()
        task = get_roguelike(self.cfg) or {}
        self.rl_theme.set(ROGUELIKE_THEME_NAMES.get(str(task.get("Theme")), "黑流树海"))
        self.rl_mode.set(ROGUELIKE_MODE_NAMES.get(str(task.get("Mode")), "刷等级"))
        self.rl_difficulty.set(str(task.get("Difficulty", 1)))
        self.rl_squad.set(str(task.get("Squad") or ""))
        self.rl_roles.set(str(task.get("Roles") or ""))
        self.rl_opers.set("，".join(str(o.get("Name")) for o in (task.get("StartingOpers") or []) if o.get("Name")))
        self.rl_invest.set(bool(task.get("Investment")))
        self.rl_invest_count.set(str(task.get("InvestCount", 999)))
        messagebox.showinfo("已读取", "已从 MAA 配置读取当前肉鸽设置")

    def save_roguelike(self) -> None:
        if not messagebox.askyesno("确认", "写入 MAA 肉鸽设置（会重启 MAA，约 1 分钟），继续？"):
            return
        try:
            note = set_roguelike(self.cfg, self._roguelike_options())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", note)

    def start_roguelike_gui(self) -> None:
        if not self.ensure_service():
            return
        if not messagebox.askyesno(
            "确认", f"用「{self.rl_account.get()}」开始自动肉鸽？\n（会先把上面的设置写入 MAA）"
        ):
            return
        COMMAND_FILE.write_text(
            json.dumps(
                {
                    "cmd": "run_roguelike",
                    "account": self.rl_account.get(),
                    "options": self._roguelike_options(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        messagebox.showinfo("已发送", "已请求服务开始自动肉鸽，状态会推送到企业微信")

    # ------------------------------------------------ 登录兜底

    def _build_login_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="登录兜底")
        conf = self.cfg.setdefault("adb_login", {})

        self.adb_enabled = tk.BooleanVar(value=bool(conf.get("enabled")))
        ttk.Checkbutton(
            frame, text="启用账号密码登录兜底（MAA 账号切换失败时自动尝试）", variable=self.adb_enabled
        ).pack(anchor="w", padx=12, pady=8)

        top = ttk.Frame(frame)
        top.pack(anchor="w", padx=12, pady=4)
        ttk.Button(top, text="① 从模拟器截屏", command=self.capture_login).pack(side="left", padx=4)
        ttk.Label(top, text="② 选择取点：").pack(side="left", padx=8)
        for value, text in (("account_field", "账号框"), ("password_field", "密码框"), ("login_button", "登录按钮")):
            ttk.Radiobutton(top, text=text, value=value, variable=self.pick_target).pack(side="left")
        ttk.Label(top, text="③ 在图片上点一下").pack(side="left", padx=8)

        self.canvas = tk.Canvas(frame, width=860, height=380, background="#111")
        self.canvas.pack(padx=12, pady=8)
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        self.coord_text = tk.StringVar()
        ttk.Label(frame, textvariable=self.coord_text, justify="left").pack(anchor="w", padx=12, pady=4)
        row = ttk.Frame(frame)
        row.pack(anchor="w", padx=12, pady=6)
        ttk.Button(row, text="保存校准", command=self.save_login_conf).pack(side="left", padx=4)
        ttk.Button(row, text="测试密码登录（第一个账号）", command=self.test_login).pack(side="left", padx=12)
        self.update_coord_text()
        existing = Path(ROOT) / "login-shot.png"
        if existing.exists():
            try:
                self.load_shot(existing)
            except Exception:  # noqa: BLE001
                pass

    def capture_login(self) -> None:
        try:
            shot = screenshot(self.cfg, Path(ROOT) / "login-shot.png")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("截屏失败", str(exc))
            return
        self.load_shot(shot)

    def load_shot(self, path: Path) -> None:
        try:
            from PIL import Image, ImageTk
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("缺少 Pillow", str(exc))
            return
        image = Image.open(path)
        canvas_w, canvas_h = 860, 380
        scale = min(canvas_w / image.width, canvas_h / image.height, 1.0)
        resized = image.resize((int(image.width * scale), int(image.height * scale)))
        self.shot_scale = scale
        self.shot_offset = ((canvas_w - resized.width) // 2, (canvas_h - resized.height) // 2)
        self.shot_image = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(self.shot_offset[0], self.shot_offset[1], anchor="nw", image=self.shot_image)
        self.draw_marks()

    def draw_marks(self) -> None:
        self.canvas.delete("marks")
        conf = self.cfg.get("adb_login") or {}
        for key, color in (("account_field", "#4ade80"), ("password_field", "#60a5fa"), ("login_button", "#f87171")):
            point = conf.get(key) or []
            if len(point) != 2:
                continue
            x = self.shot_offset[0] + point[0] * self.shot_scale
            y = self.shot_offset[1] + point[1] * self.shot_scale
            self.canvas.create_oval(x - 6, y - 6, x + 6, y + 6, outline=color, width=3, tags="marks")

    def on_canvas_click(self, event: tk.Event) -> None:
        if self.shot_image is None:
            return
        x = (event.x - self.shot_offset[0]) / self.shot_scale
        y = (event.y - self.shot_offset[1]) / self.shot_scale
        if x < 0 or y < 0:
            return
        key = self.pick_target.get()
        self.cfg.setdefault("adb_login", {})[key] = [int(x), int(y)]
        self.draw_marks()
        self.update_coord_text()

    def update_coord_text(self) -> None:
        conf = self.cfg.get("adb_login") or {}
        self.coord_text.set(
            "账号框：{0}    密码框：{1}    登录按钮：{2}\n已校准：{3}".format(
                conf.get("account_field") or "未设置",
                conf.get("password_field") or "未设置",
                conf.get("login_button") or "未设置",
                "是" if calibrated(self.cfg) else "否",
            )
        )

    def save_login_conf(self) -> None:
        self.cfg.setdefault("adb_login", {})["enabled"] = bool(self.adb_enabled.get())
        save_config(self.cfg)
        messagebox.showinfo("已保存", "登录兜底配置已保存")

    def test_login(self) -> None:
        self.save_login_conf()
        accounts = self.cfg.get("accounts") or []
        if not accounts:
            messagebox.showwarning("无账号", "请先在「账号」页添加账号")
            return
        if not calibrated(self.cfg):
            messagebox.showwarning("未校准", "请先截屏并按顺序点击三个位置")
            return
        phone = str(accounts[0].get("phone") or "")
        masked_phone = f"{phone[:3]}****{phone[-4:]}" if len(phone) >= 7 else "该账号"
        if not messagebox.askyesno("确认", f"将在模拟器里用 {masked_phone} 尝试密码登录，继续？"):
            return
        note = login_with_password(self.cfg, accounts[0])
        messagebox.showinfo("执行结果", note)

    # ------------------------------------------------ 说明

    def _build_about_tab(self, notebook: ttk.Notebook) -> None:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="说明")
        text = (
            "工作方式\n"
            "  1) 每天 08:00 / 20:00 由 MAA 定时执行启动一键长草；07:50 / 19:50 的计划任务会先确保\n"
            "     MuMu、MAA、推送服务都在运行。\n"
            "  2) 常驻服务监听 MAA 日志：开始 / 任务出错 / 完成 / 停止 都会推送到企业微信。\n"
            "  3) 每次清日常会依次把所有账号各跑一遍：上一个账号跑完 → 切到下一个账号 → 继续。\n"
            "  4) MAA 的公告弹窗会被自动关闭（滚到底部 + 勾选不再显示 + 确认）。\n\n"
            "账号切换\n"
            "  MAA 只支持「切换登录界面上已保存的账号」（输入登录界面显示的账号，可只填后 4 位）；\n"
            "  它不支持用登录账号+密码登录。所以：\n"
            "   · 正常情况：账号已在游戏登录页的「账号管理」里，MAA 直接切换即可；\n"
            "   · 登录失败：服务会先把失败情况推送到微信，然后（若已开启并在「登录兜底」页校准）\n"
            "     用 adb 在登录页输入手机号+密码并点登录，成功后再重试一次。\n\n"
            "聊天指令：清日常 / 一键长草 / start、状态、帮助"
        )
        ttk.Label(frame, text=text, justify="left").pack(anchor="nw", padx=14, pady=14)


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
