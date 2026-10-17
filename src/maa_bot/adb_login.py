"""账号密码登录兜底：通过 MuMu 的 adb 在游戏登录页输入手机号/密码并点登录。

需要先在 config.json 里校准坐标（GUI 的「登录兜底」页可以截屏取点）：
  adb_login.account_field  = [x, y]
  adb_login.password_field = [x, y]
  adb_login.login_button   = [x, y]
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("adb_login")


def _adb(cfg: dict, *args: str, timeout: int = 20) -> subprocess.CompletedProcess:
    exe = str(cfg.get("adb_emu_path") or "adb")
    target = str(cfg.get("adb_address") or "")
    cmd = [exe]
    if target:
        cmd += ["-s", target]
    cmd += [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)


def screenshot(cfg: dict, path: Path | str) -> Path:
    """从模拟器截屏保存到本地（先存到设备再 pull，避免管道把二进制数据写坏）。"""
    out = Path(path)
    remote = "/sdcard/maa_bot_shot.png"
    exe = str(cfg.get("adb_emu_path") or "adb")
    target = str(cfg.get("adb_address") or "")
    shot = _adb(cfg, "shell", "screencap", "-p", remote, timeout=40)
    if shot.returncode != 0:
        raise RuntimeError(f"截屏失败：{shot.stderr[:120]}")
    pull = subprocess.run(
        [exe, "-s", target, "pull", remote, str(out)],
        capture_output=True,
        text=True,
        errors="ignore",
        timeout=90,
    )
    _adb(cfg, "shell", "rm", "-f", remote)
    if pull.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        raise RuntimeError(f"拉取截屏失败：{pull.stderr[:120]}")
    return out


def _clear_field(cfg: dict, point: list) -> None:
    _adb(cfg, "shell", "input", "tap", str(point[0]), str(point[1]))
    time.sleep(0.6)
    _adb(cfg, "shell", "input", "keyevent", "123")  # MOVE_END
    for _ in range(24):
        _adb(cfg, "shell", "input", "keyevent", "67")  # DEL
    time.sleep(0.3)


def _type(cfg: dict, text: str) -> None:
    safe = text.replace(" ", "%s")
    _adb(cfg, "shell", "input", "text", safe)


def calibrated(cfg: dict) -> bool:
    conf = cfg.get("adb_login") or {}
    return all(conf.get(k) for k in ("account_field", "password_field", "login_button"))


def login_with_password(cfg: dict, account: dict) -> str:
    """在游戏登录页用账号密码登录。返回处理说明。"""
    conf = cfg.get("adb_login") or {}
    if not calibrated(cfg):
        return "未校准登录坐标（可在 GUI 的「登录兜底」页校准）"

    phone = str(account.get("phone") or "")
    password = str(account.get("password") or "")
    if not phone or not password:
        return f"账号 {account.get('name')} 缺少手机号或密码"

    log.info("尝试密码登录：%s", phone)
    _clear_field(cfg, conf["account_field"])
    _type(cfg, phone)
    time.sleep(0.5)
    _clear_field(cfg, conf["password_field"])
    _type(cfg, password)
    time.sleep(0.5)
    _adb(cfg, "shell", "input", "tap", str(conf["login_button"][0]), str(conf["login_button"][1]))
    wait_s = int(conf.get("submit_wait_seconds") or 25)
    time.sleep(wait_s)
    return f"已用 {phone} 提交密码登录（等待 {wait_s}s）"
