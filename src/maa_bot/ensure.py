"""定时前置检查（由计划任务在 07:50 / 19:50 调用）：
确保 MuMu 模拟器、MAA、推送常驻服务都在运行。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from .common import ROOT, load_config, service_alive_seconds, service_running, setup_logging  # noqa: E402
from .maa import ensure_ready  # noqa: E402
from .wecom import send_message_once  # noqa: E402

log = setup_logging("ensure", "ensure.log")


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def spawn_service() -> None:
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [pythonw(), str(ROOT / "run.py"), "service"],
        cwd=str(ROOT),
        creationflags=flags,
        close_fds=True,
    )


def main() -> int:
    cfg = load_config()
    log.info("=== 定时前置检查开始 ===")
    notes = ensure_ready(cfg)
    for note in notes:
        log.info(note)

    problems = [n for n in notes if "失败" in n or "找不到" in n or "未就绪" in n]

    if service_running(cfg):
        log.info("推送服务运行中（心跳 %ss 前）", int(service_alive_seconds() or 0))
    else:
        log.info("推送服务未运行，正在启动 ...")
        spawn_service()
        for _ in range(12):
            time.sleep(2)
            if service_running(cfg):
                break
        started = service_running(cfg)
        if not started:
            problems.append("推送服务未就绪，请检查 service.log")
        log.info("推送服务状态：%s", "已启动" if started else "未就绪")

    if problems and cfg.get("chat_id"):
        text = "⚠ **定时前置检查发现问题**\n" + "\n".join(f"- {p}" for p in problems)
        try:
            import asyncio

            asyncio.run(send_message_once(cfg, text))
        except Exception as exc:  # noqa: BLE001
            log.warning("发送告警失败：%s", exc)
    log.info("=== 定时前置检查结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
