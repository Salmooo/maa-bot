"""注册/卸载 Windows 计划任务（全部由 Python 生成任务 XML）。

  MAA-AutoDaily-Ensure  ：每天定时执行前 10 分钟，确保 MuMu / MAA / 推送服务就绪
  MAA-AutoDaily-Service ：登录后常驻（隐藏窗口），负责实时推送与聊天触发

用法：
  python run.py install-tasks
  python run.py install-tasks --uninstall
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .common import ROOT, load_config  # noqa: E402

ENSURE_TASK = "MAA-AutoDaily-Ensure"
SERVICE_TASK = "MAA-AutoDaily-Service"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc}</Description>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
{restart}    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def daily_trigger(hhmm: str) -> str:
    date = datetime.now().strftime("%Y-%m-%d")
    return (
        "    <CalendarTrigger>\n"
        f"      <StartBoundary>{date}T{hhmm}:00</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <ScheduleByDay>\n"
        "        <DaysInterval>1</DaysInterval>\n"
        "      </ScheduleByDay>\n"
        "    </CalendarTrigger>"
    )


def logon_trigger(user: str) -> str:
    return f"    <LogonTrigger>\n      <Enabled>true</Enabled>\n      <UserId>{user}</UserId>\n    </LogonTrigger>"


def build_xml(
    desc: str, triggers: str, command: str, arguments: str, workdir: str, time_limit: str = "PT30M", restart: str = ""
) -> str:
    return TASK_XML.format(
        desc=desc,
        triggers=triggers,
        user=os.environ.get("USERNAME", ""),
        restart=restart,
        time_limit=time_limit,
        command=command,
        arguments=arguments,
        workdir=workdir,
    )


def register(name: str, xml: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{name}.xml"
        path.write_text(xml, encoding="utf-16")
        proc = subprocess.run(
            ["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"],
            capture_output=True,
            text=True,
            errors="ignore",
        )
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    print(output)
    if proc.returncode != 0:
        raise SystemExit(f"注册失败：{name}（退出码 {proc.returncode}）")


def unregister(name: str) -> None:
    proc = subprocess.run(
        ["schtasks", "/Delete", "/TN", name, "/F"],
        capture_output=True,
        text=True,
        errors="ignore",
    )
    print((proc.stdout or proc.stderr or "").strip())


def ensure_times(cfg: dict) -> list[str]:
    lead = int(cfg.get("ensure_lead_minutes", 10))
    times: list[str] = []
    for hhmm in cfg.get("schedule_times", []):
        try:
            hour, minute = (int(x) for x in hhmm.split(":"))
        except ValueError:
            continue
        moment = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(minutes=lead)
        times.append(moment.strftime("%H:%M"))
    return times or ["07:50", "19:50"]


def main() -> int:
    parser = argparse.ArgumentParser(description="注册 MAA 微信机器人计划任务")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()

    if args.uninstall:
        unregister(ENSURE_TASK)
        unregister(SERVICE_TASK)
        return 0

    cfg = load_config()
    workdir = str(ROOT)
    exe = pythonw()

    ensure_xml = build_xml(
        desc="MAA 长草自动化：定时执行前确保 MuMu / MAA / 推送服务就绪",
        triggers="\n".join(daily_trigger(t) for t in ensure_times(cfg)),
        command=exe,
        arguments=f'"{ROOT / "run.py"}" ensure',
        workdir=workdir,
        time_limit="PT30M",
    )
    register(ENSURE_TASK, ensure_xml)

    service_xml = build_xml(
        desc="MAA 长草自动化：企业微信长连接推送服务（实时状态 + 聊天触发）",
        triggers=logon_trigger(os.environ.get("USERNAME", "")),
        command=exe,
        arguments=f'"{ROOT / "run.py"}" service',
        workdir=workdir,
        time_limit="PT0S",
        restart="    <RestartOnFailure>\n      <Interval>PT1M</Interval>\n      <Count>999</Count>\n    </RestartOnFailure>\n",
    )
    register(SERVICE_TASK, service_xml)

    for name in (ENSURE_TASK, SERVICE_TASK):
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", name, "/FO", "LIST"],
            capture_output=True,
            text=True,
            errors="ignore",
        )
        print((proc.stdout or proc.stderr or "").strip())
        print("-" * 40)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
