"""MAA 长草自动化常驻服务。

职责：
  1) 维持企业微信智能机器人长连接（心跳 / 断线重连）
  2) 监听 MAA 日志：开始 / 任务出错 / 完成 / 停止 实时推送
  3) 私聊或群里 @机器人 发关键词 → 触发的清日常会依次跑完所有账号
  4) 自动关闭 MAA 公告弹窗；账号登录失败时提示并（可校准后）用密码登录兜底
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .adb_login import login_with_password  # noqa: E402
from .ai import AiError  # noqa: E402
from .ai import parse as ai_parse
from .common import (  # noqa: E402
    ROOT,
    load_config,
    maa_log_path,
    read_state,
    save_config,
    setup_logging,
    touch_alive,
    validate_config,
    write_state,
)
from .maa import (  # noqa: E402
    LogWatcher,
    build_summary,
    click_link_start,
    dismiss_announcement,
    ensure_ready,
    log_size,
    maa_idle_from_log,
    maa_run_active,
    recover_game,
    select_all_tasks,
    send_link_start,
    stop_maa_run,
    wait_run_started,
)
from .maa_config import (  # noqa: E402
    current_account,
    describe_roguelike,
    matches_account,
    prepare_account,
    prepare_run,
    set_roguelike,
)
from .policy import is_chat_allowed  # noqa: E402
from .wecom import WeComBot  # noqa: E402

log = setup_logging("service", "service.log")

COMMAND_FILE = Path(ROOT) / "gui-command.json"

HELP_TEXT = (
    "**可用指令**\n"
    "- `清日常` / `一键长草` / `start`：立即执行一次（会依次跑完所有账号）\n"
    "- `账号`：列出所有账号；直接发「主号」「小号」或「切换 小号」即可切号\n"
    "- `肉鸽`：查看自动肉鸽设置；`肉鸽 主题=萨米 模式=刷等级 难度=15 分队=… 开局干员=令,黍` 可改设置\n"
    "- `肉鸽开始`：用当前账号跑一次自动肉鸽\n"
    "- `状态` / `进度`：查看当前执行情况\n"
    "- `停止`：停止当前正在执行的任务\n"
    "- `帮助`：显示本菜单\n\n"
    "每天 08:00 / 20:00 自动执行；任务出错会立刻汇报。"
)


class MaaService:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.log_path = maa_log_path(cfg)
        self.watcher = LogWatcher(self.log_path)
        self.bot = WeComBot(
            cfg["bot_id"],
            cfg["secret"],
            cfg.get("ws_url", "wss://openws.work.weixin.qq.com"),
            int(cfg.get("heartbeat_seconds", 25)),
        )
        self.bot.on_message(self.on_message)
        self.bot.on_connect = touch_alive

        self.run_active = False
        self.run_started_at: float | None = None
        self.run_errors: list[str] = []
        self.pending_error: str = ""
        self.pending_error_at: float = 0.0
        self.suppress_start_notice = False
        self.retried_accounts: set[str] = set()
        self.run_mode = "daily"  # daily=全部账号清日常；roguelike=自动肉鸽

        # 多账号：一次「清日常」= 依次跑完所有账号
        self.cycle_active = False
        self.cycle_total = 0
        self.cycle_done = 0
        self.cycle_pending: list[str] = []
        self.current_account = ""
        self.pending: dict = {}
        self.timed_actions: list[dict] = []
        self.completed_slots: dict = {}
        self.recover_attempts = 0
        self.ai_enabled = bool(cfg.get("deepseek_api_key")) and bool(cfg.get("ai_enabled", True))

        self.triggers = [k.lower() for k in cfg.get("trigger_keywords", [])]
        self.status_kw = [k.lower() for k in cfg.get("status_keywords", [])]
        self.help_kw = [k.lower() for k in cfg.get("help_keywords", [])]

        state = read_state()
        self.last_run = {
            "start": state.get("last_start", ""),
            "end": state.get("last_end", ""),
            "status": state.get("last_status", ""),
        }
        self.cycle_pending = list(state.get("cycle_pending") or [])
        self.cycle_total = int(state.get("cycle_total") or 0)
        self.cycle_done = int(state.get("cycle_done") or 0)
        self.cycle_active = bool(state.get("cycle_active"))
        self.current_account = str(state.get("current_account") or "")
        self.pending = dict(state.get("pending") or {})
        self.timed_actions = list(state.get("timed_actions") or [])
        self.completed_slots = dict(state.get("completed_slots") or {})
        if not self.run_active:
            try:
                self.run_active = maa_run_active()
                if self.run_active:
                    log.info("启动时检测到 MAA 正在执行任务")
            except Exception as exc:  # noqa: BLE001
                log.debug("检测 MAA 运行状态失败：%s", exc)
        if not cfg.get("chat_id") and state.get("chat_id"):
            cfg["chat_id"] = state["chat_id"]

    # ---------------- 账号 ----------------

    @property
    def accounts(self) -> list[dict]:
        return list(self.cfg.get("accounts") or [])

    def account_by_switch(self, switch_name: str) -> dict | None:
        for account in self.accounts:
            if str(account.get("switch_name") or "") == str(switch_name):
                return account
            if matches_account(str(switch_name), account):
                return account
        return None

    def account_by_name(self, text: str) -> dict | None:
        """按名称 / 切换值 / 手机号后四位找账号。"""
        value = (text or "").strip()
        if not value:
            return None
        for account in self.accounts:
            if value == str(account.get("name") or ""):
                return account
            if matches_account(value, account):
                return account
        return None

    def describe_accounts(self) -> str:
        configured = self.maa_account()
        lines = ["**账号列表**"]
        for index, account in enumerate(self.accounts, 1):
            mark = "  ← 当前 MAA 配置" if matches_account(configured, account) else ""
            lines.append(
                f"{index}. **{account.get('name')}**（切换值 {account.get('switch_name') or str(account.get('phone'))[-4:]}）"
                f"关卡 {account.get('stage') or '当前/上次'}{mark}"
            )
        lines += ["", "切号：直接发「主号」或「切换 小号」", "跑肉鸽：`肉鸽开始`"]
        return "\n".join(lines)

    def describe_account(self, account: dict | None) -> str:
        if not account:
            return "未知账号"
        return f"{account.get('name') or account.get('phone')}（{account.get('switch_name') or account.get('phone')}）"

    def maa_account(self) -> str:
        return current_account(self.cfg)

    def build_cycle_queue(self, skip_current: bool = True) -> list[str]:
        """本次要跑的账号队列（当前 MAA 已配好的账号排在最前，其余依次跟上）。"""
        accounts = self.accounts
        if not accounts:
            return []
        configured = self.maa_account()
        current = None
        for account in accounts:
            if matches_account(configured, account):
                current = account
                break
        queue: list[str] = []
        if current is not None:
            self.current_account = current.get("name") or ""
            if skip_current:
                for account in accounts:
                    if account is not current:
                        queue.append(str(account.get("switch_name") or account.get("phone")))
            else:
                for account in accounts:
                    queue.append(str(account.get("switch_name") or account.get("phone")))
        else:
            # MAA 里配置的账号不在名单里：先切到第一个账号
            for account in accounts:
                queue.append(str(account.get("switch_name") or account.get("phone")))
        return queue

    # ---------------- 推送 ----------------

    async def notify(self, content: str, chat_id: str | None = None, chat_type: int | None = None) -> None:
        target = chat_id or self.cfg.get("chat_id")
        if not target:
            log.warning("尚未获得会话 ID，跳过推送")
            return
        kind = chat_type if chat_type else int(self.cfg.get("chat_type") or 0)
        for attempt in (1, 2, 3):
            try:
                resp = await self.bot.send_markdown(target, kind, content)
                if resp and resp.get("errcode") == 0:
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("推送异常（第 %s 次）：%s", attempt, exc)
            if attempt < 3:
                # 长连接可能已经半死：主动重连后再试
                if not self.bot.connected:
                    await self.bot.force_reconnect()
                    await self.bot.wait_connected(25)
                await asyncio.sleep(3)
        log.error("推送失败（已重试 3 次）：%s", content.splitlines()[0][:60])

    async def ack(
        self, req_id: str | None, content: str, chat_id: str | None = None, chat_type: int | None = None
    ) -> None:
        """回复优先级：response_url(HTTP) → 长连接被动回复 → 主动推送。"""
        response_url = getattr(self, "_response_url", "")
        if response_url:
            self._response_url = ""
            try:
                if await self.bot.reply_via_url(response_url, content):
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("response_url 回复失败，改用长连接：%s", exc)
        if req_id:
            try:
                resp = await self.bot.reply_markdown(req_id, content)
                if resp and resp.get("errcode") == 0:
                    return
            except Exception as exc:  # noqa: BLE001
                log.warning("被动回复失败，改用主动推送：%s", exc)
        await self.notify(content, chat_id, chat_type)
        # 三条路都可能失败说明长连接已经半死，立刻重建，别等心跳
        if not self.bot.connected:
            await self.bot.force_reconnect()

    def save_state(self) -> None:
        write_state(
            {
                "last_start": self.last_run["start"],
                "last_end": self.last_run["end"],
                "last_status": self.last_run["status"],
                "run_active": self.run_active,
                "chat_id": self.cfg.get("chat_id", ""),
                "cycle_active": self.cycle_active,
                "cycle_total": self.cycle_total,
                "cycle_done": self.cycle_done,
                "cycle_pending": self.cycle_pending,
                "current_account": self.current_account,
                "pending": self.pending,
                "timed_actions": self.timed_actions,
                "completed_slots": self.completed_slots,
            }
        )

    # ---------------- 会话 / 指令 ----------------

    def remember_chat(self, chat_id: str, chat_type: int) -> None:
        if not chat_id:
            return
        if not self.cfg.get("chat_id"):
            self.cfg["chat_id"] = chat_id
            self.cfg["chat_type"] = chat_type
            self.save_state()
            try:
                save_config(self.cfg)
                log.info("已记录会话 %s（chat_type=%s）到 config.json", chat_id, chat_type)
            except Exception as exc:  # noqa: BLE001
                log.warning("写入 config.json 失败：%s", exc)

    async def on_message(self, frame: dict) -> None:
        if frame.get("cmd") != "aibot_msg_callback":
            return
        body = frame.get("body") or {}
        chat_id = body.get("chatid") or (body.get("from") or {}).get("userid") or ""
        chat_type = 2 if body.get("chattype") == "group" else 1
        text = ((body.get("text") or {}).get("content") or "").strip()
        if not is_chat_allowed(self.cfg, chat_id, chat_type):
            log.warning(
                "拒绝未授权会话 chattype=%s chat_id=%s",
                body.get("chattype"),
                chat_id or "<empty>",
            )
            return
        self._response_url = str(body.get("response_url") or "")
        log.info("收到消息 chattype=%s chat_id=%s 内容=%s", body.get("chattype"), chat_id, text)
        self.remember_chat(chat_id, chat_type)

        lowered = text.lower()
        req_id = (frame.get("headers") or {}).get("req_id")
        account = self.account_by_name(text)
        # 复杂指令（含时间/先后顺序）交给 AI 解析
        time_like = bool(re.search(r"\d+\s*[:：点]", text))
        complex_hint = time_like or any(
            k in text for k in ("等", "再", "之后", "然后", "先", "明天", "今天", "提醒", "分钟后", "小时后")
        )
        if self.ai_enabled and complex_hint:
            await self.try_ai_command(text, chat_id, chat_type, req_id)
            return

        if "肉鸽" in text and ("开始" in text or "运行" in text or "执行" in text.split("肉鸽")[0]):
            target = None
            for acc in self.accounts:
                if str(acc.get("name")) in text and acc is not account:
                    target = acc
            await self.start_roguelike(chat_id, chat_type, req_id, target or account)
        elif text.startswith("肉鸽") or text.startswith("rogu"):
            await self.apply_roguelike_text(text, chat_id, chat_type, req_id)
        elif any(k in lowered for k in self.triggers):
            await self.start_daily_run(chat_id, chat_type, req_id)
        elif any(k in text for k in ("账号", "账户")) or "有哪些账号" in text:
            await self.ack(req_id, self.describe_accounts(), chat_id, chat_type)
        elif text.startswith("切换") or text.startswith("切 ") or account is not None:
            name = (
                text[2:].strip() if text.startswith("切换") else (text[1:].strip() if text.startswith("切 ") else text)
            )
            await self.switch_account(name or text, chat_id, chat_type, req_id)
        elif "停止" in text or "stop" in lowered:
            await self.ack(req_id, "⏹ 收到，正在停止当前任务 …", chat_id, chat_type)
            note = await asyncio.to_thread(stop_maa_run)
            extra = ""
            if any(k in text for k in ("所有", "全部", "一切")):
                self.pending = {}
                self.timed_actions = []
                self.cycle_active = False
                self.cycle_pending = []
                self.save_state()
                extra = "\n同时已清空排队中的任务和定时任务"
            await self.notify(f"⏹ {note}{extra}", chat_id, chat_type)
        elif any(k in text for k in ("结束", "停下", "取消", "终止", "别跑了")):
            await self.ack(req_id, "⏹ 收到，正在结束当前任务 …", chat_id, chat_type)
            note = await asyncio.to_thread(stop_maa_run)
            extra = ""
            if any(k in text for k in ("所有", "全部", "一切")):
                self.pending = {}
                self.timed_actions = []
                self.cycle_active = False
                self.cycle_pending = []
                self.save_state()
                extra = "\n同时已清空排队中的任务和定时任务"
            await self.notify(f"⏹ {note}{extra}", chat_id, chat_type)
        elif any(k in lowered for k in self.status_kw):
            await self.reply_status(chat_id, chat_type, req_id)
        elif any(k in lowered for k in self.help_kw):
            await self.ack(req_id, HELP_TEXT, chat_id, chat_type)
        elif self.ai_enabled:
            await self.try_ai_command(text, chat_id, chat_type, req_id)

    # ---------------- AI 意图理解 ----------------

    def ai_context(self) -> str:
        names = "、".join(str(a.get("name")) for a in self.accounts)
        current = self.current_account or self.maa_account()
        mode = "自动肉鸽" if self.run_mode == "roguelike" else "清日常"
        running = "正在执行" if self.run_active else "空闲"
        pending = json.dumps(self.pending, ensure_ascii=False) if self.pending else "无"
        return f"账号：{names}；当前账号：{current}；状态：{running}（{mode}）；排队：{pending}"

    async def try_ai_command(self, text: str, chat_id: str, chat_type: int, req_id: str | None) -> None:
        try:
            plan = await asyncio.to_thread(ai_parse, text, self.cfg, self.ai_context())
        except AiError as exc:
            await self.ack(req_id, f"（AI 解析不可用：{exc}）\n{HELP_TEXT}", chat_id, chat_type)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("AI 解析失败")
            await self.ack(req_id, f"（AI 解析出错：{exc}）", chat_id, chat_type)
            return
        actions = plan.get("actions") or []
        reply = plan.get("reply") or "收到，正在执行"
        log.info("AI 动作：%s", json.dumps(actions, ensure_ascii=False))
        await self.ack(req_id, f"🤖 {reply}", chat_id, chat_type)
        await self.execute_actions(actions, chat_id, chat_type)

    async def execute_actions(self, actions: list[dict], chat_id: str = "", chat_type: int = 0) -> None:
        for action in actions:
            kind = str(action.get("type") or "")
            account = self.account_by_name(str(action.get("account") or ""))
            if kind == "stop_now":
                await asyncio.to_thread(stop_maa_run)
                await self.notify("⏹ 已停止当前任务")
            elif kind in ("stop_at", "run_at"):
                self.timed_actions.append(action)
                self.save_state()
                when = action.get("fire_at") or action.get("time")
                what = (
                    "停止当前任务"
                    if kind == "stop_at"
                    else ("自动肉鸽" if action.get("mode") == "roguelike" else "清日常")
                )
                await self.notify(f"⏰ 已登记定时任务：{when} → {what}")
            elif kind == "run_daily":
                if action.get("after_previous") or self.run_active:
                    self.pending = {"mode": "daily", "account": str(action.get("account") or "")}
                    self.save_state()
                    await self.notify("🧹 已排队：等当前任务结束后执行清日常")
                else:
                    await self.start_daily_run(chat_id, chat_type)
            elif kind == "run_roguelike":
                if action.get("after_previous") or self.run_active:
                    name = (account or {}).get("name") or "当前账号"
                    self.pending = {"mode": "roguelike", "account": str(name)}
                    self.save_state()
                    await self.notify(f"🎲 已排队：等当前任务结束后用「{name}」开始自动肉鸽")
                else:
                    await self.start_roguelike(chat_id, chat_type, None, account)
            elif kind == "switch_account":
                if account is None:
                    await self.notify("没找到这个账号\n" + self.describe_accounts())
                else:
                    await self.switch_account(str(action.get("account")), chat_id, chat_type)
            elif kind == "set_roguelike":
                options = action.get("options") or {}
                if self.run_active:
                    await self.notify("当前有任务在跑，等结束后再改肉鸽设置")
                elif options:
                    note = await asyncio.to_thread(lambda: set_roguelike(self.cfg, options))
                    await self.notify(f"✅ {note}")
            elif kind == "list_accounts":
                await self.notify(self.describe_accounts())
            elif kind == "status":
                await self.reply_status(chat_id, chat_type)
            elif kind == "help":
                await self.notify(HELP_TEXT)

    async def run_due_actions(self) -> None:
        if not self.timed_actions:
            return
        now = time.strftime("%Y-%m-%d %H:%M")
        due = [a for a in self.timed_actions if str(a.get("fire_at") or "") <= now]
        if not due:
            return
        for action in due:
            self.timed_actions.remove(action)
        self.save_state()
        for action in due:
            log.info("执行定时动作：%s", json.dumps(action, ensure_ascii=False))
            await self.notify(f"⏰ 到点了：{action.get('fire_at')}")
            await self.execute_actions([action])

    # ---------------- 漏跑补偿 / 状态校正 ----------------

    def mark_daily_slot(self) -> None:
        """记录「今天的这个定时点已经跑过」，避免漏跑补偿重复触发。"""
        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        for slot in self.cfg.get("schedule_times", []):
            try:
                hour, minute = (int(x) for x in str(slot).split(":"))
            except ValueError:
                continue
            if abs(now_minutes - (hour * 60 + minute)) <= 90:
                self.completed_slots[f"{now.date()}-{slot}"] = True
        self.save_state()

    async def check_missed_daily(self) -> None:
        """定时点过了但没跑过（例如电脑休眠）→ 现在补跑一次。"""
        if not self.cfg.get("catch_up_missed", True):
            return
        if self.run_active or self.cycle_active or self.pending or self.timed_actions:
            return
        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        for slot in self.cfg.get("schedule_times", []):
            try:
                hour, minute = (int(x) for x in str(slot).split(":"))
            except ValueError:
                continue
            slot_minutes = hour * 60 + minute
            if not (slot_minutes + 15 <= now_minutes <= slot_minutes + 8 * 60):
                continue
            key = f"{now.date()}-{slot}"
            if self.completed_slots.get(key):
                continue
            self.completed_slots[key] = True
            self.save_state()
            await self.notify(f"⏰ 检测到 {slot} 的定时执行没有跑（可能电脑休眠），现在补跑一次清日常")
            await self.start_daily_run("", 0)
            return

    async def sync_run_state(self) -> None:
        """用 MAA 日志校正「是否在跑」的状态。"""
        try:
            idle = await asyncio.to_thread(maa_idle_from_log, self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.debug("状态校正失败：%s", exc)
            return
        if idle is None:
            return
        if idle == self.run_active:
            log.info("运行状态校正：run_active %s -> %s", self.run_active, not idle)
            self.run_active = not idle
            self.save_state()

    async def reply_status(self, chat_id: str, chat_type: int, req_id: str | None = None) -> None:
        lines = []
        if self.run_active:
            elapsed = int(time.time() - (self.run_started_at or time.time()))
            lines.append(f"**正在执行一键长草** ⏳（{self.current_account or '当前账号'}）")
            lines.append(f"已运行 {elapsed // 60} 分 {elapsed % 60} 秒")
            if self.cycle_total > 1:
                lines.append(f"进度：第 {self.cycle_done}/{self.cycle_total} 个账号")
        else:
            lines.append("**当前空闲** 💤")
            if self.last_run["end"]:
                lines.append(
                    f"上次运行：{self.last_run['start']} → {self.last_run['end']}（{self.last_run['status']}）"
                )
        if self.cycle_active and self.cycle_pending:
            names = [self.describe_account(self.account_by_switch(s)) for s in self.cycle_pending]
            lines.append("待执行：" + "、".join(names))
        times = " / ".join(self.cfg.get("schedule_times", []))
        if times:
            lines.append(f"定时：每天 {times}")
        if len(self.accounts) > 1:
            lines.append(f"账号：{len(self.accounts)} 个（每次全部执行）")
        text = "\n".join(lines)
        if req_id:
            await self.ack(req_id, text, chat_id, chat_type)
        else:
            await self.notify(text, chat_id, chat_type)

    async def switch_account(self, name: str, chat_id: str, chat_type: int, req_id: str | None = None) -> None:
        account = self.account_by_name(name)
        if account is None:
            await self.ack(req_id, "没找到这个账号。\n" + self.describe_accounts(), chat_id, chat_type)
            return
        desc = self.describe_account(account)
        await self.ack(req_id, f"正在切换到 {desc} …（需要重启一次 MAA，约 1 分钟）", chat_id, chat_type)
        try:
            note = await asyncio.to_thread(lambda: prepare_account(self.cfg, account))
        except Exception as exc:  # noqa: BLE001
            log.exception("切换账号失败")
            await self.notify(f"⚠ 切换账号失败：{exc}")
            return
        self.current_account = account.get("name") or ""
        self.save_state()
        await self.notify(f"✅ {note}")

    async def apply_roguelike_text(self, text: str, chat_id: str, chat_type: int, req_id: str | None = None) -> None:
        body = text[len("肉鸽") :].strip()
        if not body:
            await self.ack(req_id, describe_roguelike(self.cfg), chat_id, chat_type)
            return
        if self.run_active:
            await self.ack(req_id, "当前有任务在跑，等这一轮结束后再改肉鸽设置 ⏳", chat_id, chat_type)
            return
        options: dict[str, str] = {}
        for token in body.replace("，", ",").split():
            if "=" in token:
                key, _, value = token.partition("=")
                options[key.strip()] = value.strip()
        if not options:
            await self.ack(req_id, "没看懂要改什么。示例：`肉鸽 主题=萨米 模式=刷等级 难度=15`", chat_id, chat_type)
            return
        await self.ack(req_id, "正在更新肉鸽设置 …（需要重启一次 MAA）", chat_id, chat_type)
        try:
            note = await asyncio.to_thread(lambda: set_roguelike(self.cfg, options))
        except Exception as exc:  # noqa: BLE001
            log.exception("修改肉鸽设置失败")
            await self.notify(f"⚠ 修改肉鸽设置失败：{exc}")
            return
        self.run_mode = "daily"
        await self.notify(f"✅ {note}\n\n" + describe_roguelike(self.cfg))

    async def start_roguelike(
        self, chat_id: str, chat_type: int, req_id: str | None = None, account: dict | None = None
    ) -> None:
        if self.run_active:
            name = (account or {}).get("name") or "当前账号"
            self.pending = {"mode": "roguelike", "account": str(name)}
            self.save_state()
            await self.ack(
                req_id,
                f"当前有任务在跑，已排好队：等这一轮结束后自动用「{name}」开始自动肉鸽 ⏳",
                chat_id,
                chat_type,
            )
            return
        if account is None:
            account = self.account_by_switch(self.maa_account()) or (self.accounts[0] if self.accounts else None)
        if account is None:
            await self.ack(req_id, "还没有配置任何账号", chat_id, chat_type)
            return
        desc = self.describe_account(account)
        await self.ack(req_id, f"收到，准备用 {desc} 开始自动肉鸽 …", chat_id, chat_type)
        try:
            note = await asyncio.to_thread(lambda: prepare_run(self.cfg, account, mode="roguelike"))
            log.info("肉鸽预置：%s", note)
        except Exception as exc:  # noqa: BLE001
            log.exception("肉鸽预置失败")
            await self.notify(f"⚠ 肉鸽预置失败：{exc}")
            return
        self.run_mode = "roguelike"
        self.cycle_active = False
        self.cycle_pending = []
        self.current_account = account.get("name") or ""
        self.save_state()
        started = await self.trigger_run(select_all=False)
        if started:
            await self.notify(f"✅ 已开始自动肉鸽（{desc}）\n" + describe_roguelike(self.cfg))
        else:
            self.run_mode = "daily"
            await self.notify("⚠ 没能启动自动肉鸽，请检查 MAA 窗口")

    # ---------------- 触发执行 ----------------

    async def trigger_run(self, select_all: bool = True) -> bool:
        """确保环境就绪 → 任务全选 → 点 Link Start → 等待 MAA 真正跑起来。"""
        notes = await asyncio.to_thread(ensure_ready, self.cfg)
        for note in notes:
            log.info(note)
        try:
            note = dismiss_announcement()
            if note:
                log.info("公告弹窗：%s", note)
                await self.notify(f"📢 MAA 公告弹窗：{note}")
        except Exception as exc:  # noqa: BLE001
            log.debug("公告处理跳过：%s", exc)

        # 日常才会「全选」；跑肉鸽时任务集是特意只留 开始唤醒+自动肉鸽，不能动
        if select_all:
            try:
                info = await asyncio.to_thread(select_all_tasks)
                if info:
                    log.info("任务选择：%s", info)
                    if "已关闭" in info or "⚠" in info:
                        await self.notify(f"🧹 {info}")
            except Exception as exc:  # noqa: BLE001
                log.warning("全选任务失败：%s", exc)

        offset = log_size(self.log_path)
        self.suppress_start_notice = True
        # MAA 刚重启时可能还没注册热键，这里多试几次
        for attempt in range(1, 4):
            clicked = await asyncio.to_thread(click_link_start)
            if not clicked:
                await asyncio.to_thread(send_link_start)
                log.info("已发送 Link Start 热键（Ctrl+Shift+Alt+L），第 %s 次", attempt)
            else:
                log.info("已点击 Link Start 按钮，第 %s 次", attempt)
            if await asyncio.to_thread(wait_run_started, self.log_path, offset, 25):
                return True
            log.warning("第 %s 次触发未生效，稍后重试", attempt)
        return False

    async def start_daily_run(self, chat_id: str, chat_type: int, req_id: str | None = None) -> None:
        if self.run_active:
            self.pending = {"mode": "daily", "account": ""}
            self.save_state()
            await self.ack(
                req_id,
                "当前有任务在跑，已排队：等它结束后自动执行一次清日常（全部账号）⏳",
                chat_id,
                chat_type,
            )
            return

        # 先快速应答（被动回复），后面干重活
        await self.ack(req_id, "收到，正在准备开始一键长草…", chat_id, chat_type)

        # 建立本次周期：所有账号各跑一遍
        self.cycle_pending = self.build_cycle_queue()
        self.cycle_total = len(self.accounts) or 1
        self.cycle_done = 1
        self.cycle_active = True
        self.retried_accounts = set()
        self.mark_daily_slot()
        self.save_state()
        if len(self.accounts) > 1:
            names = "、".join(self.describe_account(a) for a in self.accounts)
            await self.notify(f"本次共 {self.cycle_total} 个账号：{names}")

        started = await self.trigger_run()
        if started:
            await self.notify("✅ 已开始执行一键长草", chat_id, chat_type)
        else:
            self.suppress_start_notice = False
            self.cycle_active = False
            self.save_state()
            await self.notify(
                "⚠ 没能确认 MAA 开始运行，请检查电脑上的 MAA 窗口（模拟器是否就绪 / 热键是否被占用）",
                chat_id,
                chat_type,
            )

    async def chain_next_account(self) -> None:
        """一个账号跑完，切到下一个账号并继续。"""
        if not self.cycle_pending:
            return
        switch_name = self.cycle_pending.pop(0)
        account = self.account_by_switch(switch_name) or {"name": switch_name, "switch_name": switch_name}
        self.cycle_done += 1
        self.current_account = account.get("name") or switch_name
        self.save_state()

        await self.notify(f"🔁 切换到账号 {self.describe_account(account)} ...")
        try:
            stage = account.get("stage") or ""
            note = await asyncio.to_thread(lambda: prepare_account(self.cfg, account))
            log.info(note)
            if stage:
                await self.notify(f"关卡：{stage}")
        except Exception as exc:  # noqa: BLE001
            log.exception("切换账号失败")
            await self.notify(f"⚠ 切换账号失败：{exc}")
            await self.finish_cycle()
            return

        await asyncio.sleep(5)
        started = await self.trigger_run()
        if not started:
            await self.notify(f"⚠ 账号 {self.describe_account(account)} 未能开始运行，跳过剩余账号")
            await self.finish_cycle()

    async def finish_cycle(self) -> None:
        if not self.cycle_active:
            return
        self.cycle_active = False
        self.cycle_pending = []
        self.save_state()
        if self.cycle_total > 1:
            await self.notify(f"🎉 **全部 {self.cycle_total} 个账号已执行完毕**")
        # 把 MAA 配置预置回第一个账号，这样下一次定时执行（MAA 自己启动）也是对的账号和关卡
        accounts = self.accounts
        if accounts:
            try:
                note = await asyncio.to_thread(lambda: prepare_account(self.cfg, accounts[0]))
                log.info("预置下次运行：%s", note)
            except Exception as exc:  # noqa: BLE001
                log.warning("预置下次账号失败：%s", exc)

    async def maybe_start_pending(self) -> None:
        """当前任务跑完后，执行排队中的指令（清日常 / 自动肉鸽）。"""
        if not self.pending or self.run_active or self.cycle_active:
            return
        action = dict(self.pending)
        self.pending = {}
        self.save_state()
        mode = str(action.get("mode") or "")
        name = str(action.get("account") or "")
        if mode == "roguelike":
            await self.notify(f"🎲 现在按排队开始自动肉鸽（{name or '当前账号'}）")
            await self.start_roguelike(chat_id="", chat_type=0, req_id=None, account=self.account_by_name(name))
        elif mode == "daily":
            await self.notify("🧹 现在按排队执行清日常")
            await self.start_daily_run("", 0)

    # ---------------- 登录失败处理 ----------------

    async def handle_login_failure(self, task: str) -> None:
        account = self.account_by_switch(self.maa_account()) or (
            self.account_by_switch(self.current_account) if self.current_account else None
        )
        desc = self.describe_account(account)
        conf = self.cfg.get("adb_login") or {}
        if account and conf.get("enabled"):
            await self.notify(f"🔑 账号 {desc} 登录失败，尝试用账号密码登录 …")
            try:
                note = await asyncio.to_thread(login_with_password, self.cfg, account)
            except Exception as exc:  # noqa: BLE001
                note = f"密码登录异常：{exc}"
            await self.notify(f"{note}")
            key = str(account.get("switch_name") or account.get("phone"))
            if key not in self.retried_accounts:
                self.retried_accounts.add(key)
                await asyncio.sleep(5)
                started = await self.trigger_run()
                if started:
                    await self.notify(f"✅ 密码登录后已重新开始（{desc}）")
                    return
        await self.notify(
            f"❗ 账号 {desc} 登录失败（{task}），请在游戏里手动用账号密码登录一次；"
            "登录成功后该账号会留在「账号管理」里，之后就能自动切换了。"
        )

    # ---------------- MAA 日志事件 ----------------

    async def handle_events(self, events: list[tuple[str, dict]]) -> None:
        for kind, data in events:
            if kind == "start":
                self.run_active = True
                self.run_started_at = time.time()
                self.run_errors = []
                self.recover_attempts = 0
                self.last_run["start"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.last_run["status"] = "执行中"

                if self.run_mode != "roguelike" and self.cfg.get("multi_account", True) and not self.cycle_active:
                    # MAA 定时执行自己启动的：把剩余账号排进本轮
                    self.cycle_pending = self.build_cycle_queue()
                    self.cycle_total = len(self.accounts) or 1
                    self.cycle_done = 1
                    self.cycle_active = True
                    self.mark_daily_slot()
                    log.info("本轮账号：当前 %s，待执行 %s", self.current_account, self.cycle_pending)
                self.save_state()
                log.info("检测到 MAA 开始运行")
                if self.suppress_start_notice:
                    self.suppress_start_notice = False
                elif self.cfg.get("notify_start", True):
                    await self.notify(f"▶ **开始执行一键长草**\n{time.strftime('%m-%d %H:%M:%S')}")

            elif kind == "task_error":
                self.pending_error = data.get("task", "")
                self.pending_error_at = time.time()

            elif kind == "err":
                self.run_errors.append(data.get("text", ""))

            elif kind == "complete":
                self.run_active = False
                self.last_run["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.last_run["status"] = "完成"
                self.save_state()
                log.info("检测到任务全部完成")
                summary = build_summary(self.cfg)
                if self.run_mode == "roguelike":
                    await self.notify(f"🎲 **自动肉鸽已结束（{self.current_account or '当前账号'}）**\n" + summary)
                    asyncio.create_task(self.restore_daily_config())
                elif self.cycle_active and self.cycle_total > 1:
                    header = (
                        f"**【账号 {self.cycle_done}/{self.cycle_total}：{self.current_account or '当前账号'}】**\n"
                    )
                    summary = header + summary
                    await self.notify(summary)
                    if self.cycle_pending:
                        asyncio.create_task(self.chain_next_account())
                    else:
                        await self.finish_cycle()
                else:
                    await self.notify(summary)
                    await self.finish_cycle()
                    await self.maybe_start_pending()

            elif kind == "stopped":
                if self.run_active:
                    self.run_active = False
                    self.last_run["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.last_run["status"] = "已停止"
                    self.save_state()
                    await self.notify(f"⏹ **任务已停止**\n{time.strftime('%m-%d %H:%M:%S')}")
                if self.run_mode == "roguelike":
                    await self.notify("🎲 自动肉鸽已停止")
                    asyncio.create_task(self.restore_daily_config())
                elif self.cycle_active:
                    self.cycle_active = False
                    self.cycle_pending = []
                    self.save_state()
                    await self.notify("⏹ 已停止，剩余账号不再执行")
                    await self.maybe_start_pending()

    async def restore_daily_config(self) -> None:
        """肉鸽跑完后把 MAA 恢复成「日常任务集 + 第一个账号」，保证定时执行不受影响。"""
        self.run_mode = "daily"
        accounts = self.accounts
        if not accounts:
            return
        try:
            note = await asyncio.to_thread(lambda: prepare_run(self.cfg, accounts[0], mode="daily"))
            log.info("已恢复日常配置：%s", note)
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复日常配置失败：%s", exc)

    async def flush_pending_error(self) -> None:
        if not self.pending_error:
            return
        if time.time() - self.pending_error_at < 2:
            return
        task = self.pending_error
        detail = "\n".join(self.run_errors[-3:])
        self.pending_error = ""
        self.pending_error_at = 0.0
        if self.cfg.get("notify_errors", True):
            text = f"❌ **任务出错：{task}**"
            if detail:
                text += f"\n{detail}"
            await self.notify(text)
        combined = f"{task} {detail}"
        if any(k in combined for k in ("掉线", "重新连接", "连接丢失", "与神经网络", "自动肉鸽")):
            asyncio.create_task(self.recover_from_disconnect())
        elif "开始唤醒" in task:
            await self.handle_login_failure(task)

    # ---------------- 掉线恢复 ----------------

    async def recover_from_disconnect(self) -> None:
        if not self.cfg.get("auto_recover", True):
            return
        max_attempts = int(self.cfg.get("recover_max_attempts", 2))
        self.recover_attempts += 1
        if self.recover_attempts > max_attempts:
            await self.notify(f"❌ 已尝试 {max_attempts} 次恢复仍失败，请手动看一下模拟器/游戏")
            return
        await self.notify(f"🔌 检测到游戏掉线，第 {self.recover_attempts} 次尝试恢复 …")
        try:
            note = await asyncio.to_thread(recover_game, self.cfg, self.recover_attempts)
        except Exception as exc:  # noqa: BLE001
            note = f"恢复动作失败：{exc}"
        await self.notify(f"🔧 {note}")
        await asyncio.sleep(8)
        await self.rerun_current()

    async def rerun_current(self) -> None:
        """掉线/中断后，把当前这一轮的账号和模式重跑一次。"""
        account = self.account_by_switch(self.maa_account()) or self.account_by_name(self.current_account)
        if self.run_mode == "roguelike":
            await self.start_roguelike(chat_id="", chat_type=0, req_id=None, account=account)
            return
        if account is not None:
            try:
                await asyncio.to_thread(lambda: prepare_run(self.cfg, account, mode="daily"))
            except Exception as exc:  # noqa: BLE001
                log.warning("重跑前预置失败：%s", exc)
        self.cycle_active = True
        self.cycle_total = max(self.cycle_total, 1)
        self.cycle_done = max(self.cycle_done, 1)
        started = await self.trigger_run()
        await self.notify("✅ 掉线恢复后已重新开始当前任务" if started else "⚠ 掉线恢复后仍未能启动，请手动检查")

    # ---------------- 主循环 ----------------

    async def watch_loop(self) -> None:
        last_alive = 0.0
        ticks = 0
        while True:
            await asyncio.sleep(2)
            try:
                await self.handle_events(self.watcher.poll())
                await self.flush_pending_error()
            except Exception:  # noqa: BLE001
                log.exception("处理 MAA 日志事件失败")

            await self.handle_gui_command()
            try:
                await self.run_due_actions()
            except Exception:  # noqa: BLE001
                log.exception("执行定时动作失败")

            ticks += 1
            if ticks % 10 == 0:
                try:
                    note = await asyncio.to_thread(dismiss_announcement)
                    if note:
                        log.info("公告弹窗：%s", note)
                        await self.notify(f"📢 **MAA 公告弹窗已自动处理**\n{note}")
                except Exception as exc:  # noqa: BLE001
                    log.debug("公告检查跳过：%s", exc)
            if ticks % 15 == 0:
                await self.sync_run_state()
                try:
                    await self.check_missed_daily()
                except Exception:  # noqa: BLE001
                    log.exception("漏跑补偿失败")

            if self.bot.connected:
                if self.bot.last_rx and time.time() - self.bot.last_rx > 90:
                    log.warning("超过 90 秒未收到服务端数据，重建连接")
                    await self.bot.force_reconnect()
                elif time.time() - last_alive > 20:
                    touch_alive()
                    last_alive = time.time()

    async def handle_gui_command(self) -> None:
        """处理 GUI（控制台）发来的指令文件。"""
        if not COMMAND_FILE.exists():
            return
        try:
            payload = json.loads(COMMAND_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            COMMAND_FILE.unlink()
        except OSError:
            pass

        cmd = str(payload.get("cmd") or "")
        log.info("收到界面指令：%s", cmd)
        if cmd == "run_all":
            if self.run_active:
                await self.notify("已在执行中，忽略界面的「立即执行」请求")
            else:
                await self.start_daily_run("", 0)
        elif cmd == "run_roguelike":
            account_name = str(payload.get("account") or "")
            account = self.account_by_name(account_name) if account_name else None
            options = payload.get("options") or {}
            if options and not self.run_active:
                try:
                    note = await asyncio.to_thread(lambda: set_roguelike(self.cfg, options))
                    log.info("界面写入肉鸽设置：%s", note)
                except Exception as exc:  # noqa: BLE001
                    log.warning("界面写入肉鸽设置失败：%s", exc)
            await self.start_roguelike(chat_id="", chat_type=0, req_id=None, account=account)
        elif cmd == "test_push":
            await self.notify(f"✅ 测试消息（来自控制台）\n{time.strftime('%Y-%m-%d %H:%M:%S')}")

    async def main(self) -> None:
        log.info(
            "=== 服务启动 bot_id=%s… chat_id=%s 账号数=%s ===",
            self.cfg["bot_id"][:8],
            self.cfg.get("chat_id"),
            len(self.accounts),
        )
        log.info("MAA 日志：%s", self.log_path)
        bot_task = asyncio.create_task(self.bot.run())
        watch_task = asyncio.create_task(self.watch_loop())
        try:
            await asyncio.gather(bot_task, watch_task)
        finally:
            for task in (bot_task, watch_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            errors="ignore",
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return str(pid) in out


def main() -> int:
    cfg = load_config()
    problems = validate_config(cfg)
    if problems:
        for problem in problems:
            log.error("配置错误：%s", problem)
        return 3
    lock = Path(ROOT) / "service.pid"
    if lock.exists():
        try:
            pid = int((lock.read_text(encoding="utf-8") or "0").strip())
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            log.warning("已有服务实例在运行（PID %s），本次退出", pid)
            return 0
    lock.write_text(str(os.getpid()), encoding="utf-8")
    service = MaaService(cfg)
    try:
        asyncio.run(service.main())
    except KeyboardInterrupt:
        log.info("服务被中断")
    finally:
        with contextlib.suppress(Exception):
            lock.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
