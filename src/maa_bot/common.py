"""公用工具：配置读取、日志、服务心跳。"""

from __future__ import annotations

import contextlib
import copy
import ctypes
import json
import logging
import os
import sys
import time
from pathlib import Path


def enable_dpi_awareness() -> None:
    """让本进程按物理像素读坐标/发点击，避免多屏+缩放下点击偏移。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


enable_dpi_awareness()

ROOT = Path(os.environ.get("MAA_BOT_HOME") or Path.cwd()).resolve()
CONFIG_PATH = ROOT / "config.json"
ALIVE_FILE = ROOT / "service-alive.txt"
STATE_FILE = ROOT / "service-state.json"

DEFAULT_CONFIG: dict = {
    "schema_version": 2,
    "bot_id": "",
    "secret": "",
    "ws_url": "wss://openws.work.weixin.qq.com",
    "heartbeat_seconds": 25,
    "chat_id": "",
    "chat_type": 1,
    "allowed_chat_ids": [],
    "allow_group_chat": False,
    "maa_dir": "",
    "maa_log": "",
    "mumu_dir": "",
    "adb_address": "127.0.0.1:16416",
    "adb_emu_path": "",
    "game_package": "com.hypergryph.arknights",
    "notify_start": True,
    "notify_errors": True,
    "trigger_keywords": ["清日常", "一键长草", "start"],
    "status_keywords": ["状态", "进度", "status"],
    "help_keywords": ["帮助", "help", "菜单"],
    "schedule_times": ["08:00", "20:00"],
    "ensure_lead_minutes": 10,
    "service_alive_seconds": 150,
    "auto_recover": True,
    "recover_max_attempts": 2,
    "catch_up_missed": True,
    "ai_enabled": True,
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-flash",
    "accounts": [],
    "adb_login": {
        "enabled": False,
        "account_field": None,
        "password_field": None,
        "login_button": None,
    },
}

ENV_CONFIG = {
    "MAA_BOT_ID": "bot_id",
    "MAA_BOT_SECRET": "secret",
    "DEEPSEEK_API_KEY": "deepseek_api_key",
}


def merge_defaults(raw: dict) -> dict:
    """Recursively fill missing configuration fields without mutating input."""
    result = copy.deepcopy(DEFAULT_CONFIG)

    def merge(target: dict, source: dict) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)

    merge(result, raw)
    return result


def validate_config(cfg: dict, *, require_bot: bool = True) -> list[str]:
    """Return human-readable configuration problems."""
    problems: list[str] = []
    if require_bot and not str(cfg.get("bot_id") or "").strip():
        problems.append("缺少 bot_id")
    if require_bot and not str(cfg.get("secret") or "").strip():
        problems.append("缺少 secret")
    maa_dir = str(cfg.get("maa_dir") or "").strip()
    if not maa_dir:
        problems.append("缺少 maa_dir")
    elif not (Path(maa_dir) / "MAA.exe").exists():
        problems.append(f"maa_dir 下找不到 MAA.exe：{maa_dir}")

    for value in cfg.get("schedule_times") or []:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except ValueError:
            problems.append(f"无效定时时间：{value}")

    names: set[str] = set()
    switches: set[str] = set()
    for index, account in enumerate(cfg.get("accounts") or [], 1):
        if not isinstance(account, dict):
            problems.append(f"第 {index} 个账号格式错误")
            continue
        name = str(account.get("name") or "").strip()
        switch = str(account.get("switch_name") or "").strip()
        if not name:
            problems.append(f"第 {index} 个账号缺少名称")
        elif name in names:
            problems.append(f"账号名称重复：{name}")
        if not switch:
            problems.append(f"账号 {name or index} 缺少 MAA 切换值")
        elif switch in switches:
            problems.append(f"MAA 切换值重复：{switch}")
        names.add(name)
        switches.add(switch)
    return problems


def load_config(path: Path | str | None = None) -> dict:
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"配置文件不存在：{cfg_path}\n请先复制 config.example.json 为 config.json 并完成配置。")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件根节点必须是对象：{cfg_path}")
    cfg = merge_defaults(raw)
    env_keys: set[str] = set()
    for env_name, key in ENV_CONFIG.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
            env_keys.add(key)
    cfg["_config_path"] = str(cfg_path)
    cfg["_env_keys"] = env_keys
    return cfg


def save_config(cfg: dict) -> None:
    env_keys = set(cfg.get("_env_keys") or [])
    path = Path(cfg.get("_config_path") or CONFIG_PATH)
    existing: dict = {}
    if path.exists():
        with contextlib.suppress(OSError, ValueError, TypeError):
            with open(path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                existing = loaded
    data = {key: value for key, value in cfg.items() if not key.startswith("_") and key not in env_keys}
    for key in env_keys:
        if key in existing:
            data[key] = existing[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def setup_logging(name: str, filename: str, level: int = logging.INFO) -> logging.Logger:
    fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(level)

        # 控制台可能是 GBK/无控制台（pythonw），这里统一成 UTF-8 并允许替换字符
        stream_target = sys.stdout
        if stream_target is not None:
            with contextlib.suppress(Exception):
                stream_target.reconfigure(encoding="utf-8", errors="replace")
            stream = logging.StreamHandler(stream_target)
            stream.setFormatter(fmt)
            root.addHandler(stream)

        try:
            file_handler = logging.FileHandler(ROOT / filename, encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except Exception:  # noqa: BLE001
            pass

    logger = logging.getLogger(name)
    logger.setLevel(level)
    # comtypes / uiautomation 的初始化日志太吵，降级
    for noisy in ("comtypes", "uiautomation"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logger


def touch_alive() -> None:
    try:
        ALIVE_FILE.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def service_alive_seconds() -> float | None:
    if not ALIVE_FILE.exists():
        return None
    try:
        return time.time() - ALIVE_FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        return None


def service_running(cfg: dict) -> bool:
    age = service_alive_seconds()
    limit = float(cfg.get("service_alive_seconds", 150))
    return age is not None and age < limit


def maa_log_path(cfg: dict) -> Path:
    if cfg.get("maa_log"):
        return Path(cfg["maa_log"])
    return Path(cfg["maa_dir"]) / "debug" / "gui.log"


def state_path() -> Path:
    return STATE_FILE


def read_state() -> dict:
    path = state_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def write_state(state: dict) -> None:
    try:
        with open(state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
