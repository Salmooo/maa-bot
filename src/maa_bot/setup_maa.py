"""写入 MAA 的「定时执行」时间点与「结束后脚本」。

用法：
  python run.py setup-maa --times 08:00,20:00      # 设置定时并挂上推送脚本
  python run.py setup-maa --remove-hook            # 只取消结束后脚本
  python run.py setup-maa --times 08:00,20:00 --no-restart

注意：MAA 运行中会覆盖配置文件，所以脚本会先关闭 MAA，改完再启动。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .common import ROOT, load_config, save_config, setup_logging  # noqa: E402
from .maa import process_exists  # noqa: E402

log = setup_logging("setup_maa", "setup.log")


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def close_maa(timeout: int = 30) -> bool:
    if not process_exists("MAA.exe"):
        log.info("MAA 未在运行")
        return True
    log.info("正在关闭 MAA ...")
    subprocess.run(["taskkill", "/IM", "MAA.exe"], capture_output=True, text=True, errors="ignore")
    for _ in range(timeout):
        if not process_exists("MAA.exe"):
            log.info("MAA 已退出")
            return True
        time.sleep(1)
    log.warning("MAA 未正常退出，强制结束")
    subprocess.run(["taskkill", "/IM", "MAA.exe", "/F"], capture_output=True, text=True, errors="ignore")
    time.sleep(3)
    return not process_exists("MAA.exe")


def main() -> int:
    parser = argparse.ArgumentParser(description="配置 MAA 定时执行与结束后脚本")
    parser.add_argument("--times", help="定时点，逗号分隔，例如 08:00,20:00")
    parser.add_argument("--no-restart", action="store_true", help="修改后不启动 MAA")
    parser.add_argument("--remove-hook", action="store_true", help="清空结束后脚本")
    parser.add_argument("--force-scheduled", action="store_true", help="开启强制定时启动")
    parser.add_argument("--show-announcement", action="store_true", help="保留 MAA 公告弹窗（默认关闭弹窗）")
    args = parser.parse_args()

    cfg = load_config()
    maa_dir = Path(cfg["maa_dir"])
    gui_path = maa_dir / "config" / "gui.new.json"
    if not gui_path.exists():
        log.error("找不到 MAA 配置：%s", gui_path)
        return 2

    close_maa()

    # 备份
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "backup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for item in (gui_path.parent).glob("*"):
        if item.is_file():
            shutil.copy2(item, backup_dir / item.name)
    log.info("已备份 MAA 配置到 %s", backup_dir)

    with open(gui_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    hook = f'"{pythonw()}" "{ROOT / "run.py"}" push-status'
    runtime = data["Configurations"]["Default"]["Gui"]["RuntimeSettings"]
    runtime["PostRunScript"] = "" if args.remove_hook else hook

    # 公告弹窗会要求滑动到底再确认，自动运行时会挡住界面，默认关闭
    announcement = data.setdefault("AnnouncementInfo", {})
    announcement["DoNotShowAgain"] = not args.show_announcement

    timers = data.setdefault("Timers", {})
    if args.times:
        wanted = []
        for chunk in args.times.split(","):
            hour, _, minute = chunk.strip().partition(":")
            wanted.append((int(hour), int(minute)))
        timers["ForceScheduledStart"] = bool(args.force_scheduled)
        timers["CustomConfig"] = False
        for index, item in enumerate(timers.get("List", [])):
            if index < len(wanted):
                hour, minute = wanted[index]
                item["IsEnabled"] = True
                item["Hour"] = hour
                item["Minute"] = minute
                item["Config"] = ""
            else:
                item["IsEnabled"] = False
        cfg["schedule_times"] = [f"{h:02d}:{m:02d}" for h, m in wanted]
        save_config(cfg)

    with open(gui_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    with open(gui_path, "r", encoding="utf-8") as fh:
        check = json.load(fh)
    enabled = [f"{t['Hour']:02d}:{t['Minute']:02d}" for t in check["Timers"]["List"] if t.get("IsEnabled")]
    log.info("定时点：%s", ", ".join(enabled) or "（无）")
    log.info("强制定时启动：%s", check["Timers"].get("ForceScheduledStart"))
    log.info("结束后脚本：%s", check["Configurations"]["Default"]["Gui"]["RuntimeSettings"]["PostRunScript"])

    if not args.no_restart:
        maa_exe = maa_dir / "MAA.exe"
        log.info("启动 MAA ...")
        subprocess.Popen([str(maa_exe)], cwd=str(maa_dir))
        time.sleep(20)
        log.info("MAA 进程：%s", "运行中" if process_exists("MAA.exe") else "未启动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
