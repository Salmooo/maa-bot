"""Unified command-line entry point.

Examples:
  python run.py doctor
  python run.py gui
  python run.py service
  python run.py listen --seconds 120
  python run.py send "test message"
  python run.py install-tasks
  python run.py setup-maa --times 08:00,20:00
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

from .common import (
    CONFIG_PATH,
    ROOT,
    load_config,
    maa_log_path,
    save_config,
    service_alive_seconds,
    service_running,
    setup_logging,
    validate_config,
)
from .maa import build_summary
from .wecom import WeComBot, send_message_once

log = setup_logging("cli", "cli.log")


async def _listen(cfg: dict, seconds: int) -> None:
    bot = WeComBot(
        cfg["bot_id"],
        cfg["secret"],
        cfg.get("ws_url", ""),
        int(cfg.get("heartbeat_seconds", 25)),
    )

    @bot.on_message
    async def _on(frame: dict) -> None:  # noqa: ANN202
        if frame.get("cmd") != "aibot_msg_callback":
            return
        body = frame.get("body") or {}
        chat_id = body.get("chatid") or (body.get("from") or {}).get("userid") or ""
        chat_type = 2 if body.get("chattype") == "group" else 1
        log.info("捕获会话：chat_id=%s chattype=%s", chat_id, body.get("chattype"))
        if chat_id and not cfg.get("chat_id"):
            cfg["chat_id"] = chat_id
            cfg["chat_type"] = chat_type
            save_config(cfg)
            log.info("已写入 %s", cfg.get("_config_path"))

    task = asyncio.create_task(bot.run())
    log.info("监听 %ss，请在会话里给机器人发一条消息 ...", seconds)
    try:
        await asyncio.sleep(seconds)
    finally:
        await bot.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _send(cfg: dict, content: str, chat_id: str | None) -> int:
    try:
        response = await send_message_once(cfg, content, chat_id)
    except Exception as exc:  # noqa: BLE001
        log.error("发送失败：%s", exc)
        return 5
    return 0 if response and response.get("errcode") == 0 else 5


def _doctor(cfg: dict | None = None) -> int:
    """Print a non-destructive environment and configuration report."""
    print(f"项目目录：{ROOT}")
    print(f"配置文件：{CONFIG_PATH}")
    if cfg is None:
        try:
            cfg = load_config()
        except Exception as exc:  # noqa: BLE001
            print(f"配置读取：失败 - {exc}")
            return 1

    problems = validate_config(cfg)
    maa_dir = Path(str(cfg.get("maa_dir") or ""))
    adb_path = str(cfg.get("adb_emu_path") or "").strip()
    log_path = maa_log_path(cfg)
    age = service_alive_seconds()

    print(f"Python：{sys.version.split()[0]}（{sys.executable}）")
    print(f"Bot ID：{'已配置' if cfg.get('bot_id') else '未配置'}")
    print(f"Bot Secret：{'已配置' if cfg.get('secret') else '未配置'}")
    print(f"会话 ID：{cfg.get('chat_id') or '未绑定'}")
    print(f"允许的聊天：{', '.join(cfg.get('allowed_chat_ids') or []) or '仅已绑定的私有会话'}")
    print(f"群聊控制：{'开启' if cfg.get('allow_group_chat') else '关闭'}")
    print(f"MAA 目录：{maa_dir}（{'存在' if (maa_dir / 'MAA.exe').exists() else '未找到 MAA.exe'}）")
    print(f"MAA 日志：{log_path}（{'存在' if log_path.exists() else '不存在'}）")
    print(f"ADB：{adb_path or '使用 PATH 中的 adb'}（{'存在' if adb_path and Path(adb_path).exists() else '待检查'}）")
    print(f"DeepSeek：{'已配置' if cfg.get('deepseek_api_key') else '未配置'}，模型 {cfg.get('deepseek_model')}")
    print(f"DeepSeek Key 来源：{'环境变量' if 'deepseek_api_key' in (cfg.get('_env_keys') or []) else '配置文件'}")
    print(f"每日定时：{', '.join(cfg.get('schedule_times') or []) or '未配置'}")
    print(f"账号数量：{len(cfg.get('accounts') or [])}")
    print(f"推送服务：{'运行中' if service_running(cfg) else '未运行'}")
    if age is not None:
        print(f"最近心跳：{int(age)} 秒前")
    if problems:
        print("配置问题：")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("检查结果：通过")
    return 0


def main() -> int:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    delegated = {
        "ensure": "ensure",
        "install-tasks": "install_tasks",
        "setup-maa": "setup_maa",
        "push-status": "push_status",
    }
    if len(sys.argv) > 1 and sys.argv[1] in delegated:
        from importlib import import_module

        command = sys.argv[1]
        module_name = delegated[command]
        module = import_module(f".{module_name}", package=__package__)
        old_argv = sys.argv
        try:
            sys.argv = [command, *sys.argv[2:]]
            return int(module.main())
        finally:
            sys.argv = old_argv

    parser = argparse.ArgumentParser(description="MAA 企业微信机器人")
    sub = parser.add_subparsers(dest="cmd", required=True)

    listen = sub.add_parser("listen", help="连接并监听，捕获会话 ID")
    listen.add_argument("--seconds", type=int, default=120)

    send = sub.add_parser("send", help="主动推送一条 markdown 消息")
    send.add_argument("text")
    send.add_argument("--chat-id", default=None)

    sub.add_parser("status", help="打印最近一次运行摘要")
    sub.add_parser("doctor", help="检查配置、目录和服务状态")
    sub.add_parser("ensure", help="执行定时前置检查")
    sub.add_parser("service", help="前台运行常驻服务")
    sub.add_parser("gui", help="打开图形控制台")
    sub.add_parser("install-tasks", help="注册 Windows 计划任务")
    sub.add_parser("setup-maa", help="写入 MAA 定时与结束后脚本")
    sub.add_parser("push-status", help="推送或打印最近一次 MAA 状态")

    args = parser.parse_args()
    cfg = None
    if args.cmd != "doctor":
        cfg = load_config()

    if args.cmd == "doctor":
        return _doctor(cfg)
    if args.cmd == "listen":
        asyncio.run(_listen(cfg, args.seconds))
        return 0
    if args.cmd == "send":
        return asyncio.run(_send(cfg, args.text, args.chat_id))
    if args.cmd == "status":
        print(build_summary(cfg))
        print("\n服务运行中：", service_running(cfg))
        return 0
    if args.cmd == "gui":
        from .gui import main as gui_main

        return gui_main()
    if args.cmd == "service":
        from .service import main as service_main

        return service_main()

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
