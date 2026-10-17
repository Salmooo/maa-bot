"""读写 MAA 配置、重启 MAA、切换开始唤醒账号。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .common import ROOT
from .maa import process_exists

log = logging.getLogger("maa_config")

# 每次清日常希望启用的任务（其余一律关闭，尤其是自动肉鸽 / 生息演算）
DESIRED_TASKS = {
    "StartUp": True,
    "Fight": True,
    "Infrast": True,
    "Recruit": True,
    "Mall": True,
    "Award": True,
    "UserDataUpdate": True,
    "Roguelike": False,
    "Reclamation": False,
    "OperProgress": False,
}

# 肉鸽主题 / 模式 / 奖励 的中文↔配置值映射
ROGUELIKE_THEMES = {
    "傀影": "Phantom",
    "phantom": "Phantom",
    "水月": "Mizuki",
    "mizuki": "Mizuki",
    "萨米": "Sami",
    "sami": "Sami",
    "萨卡兹": "Sarkaz",
    "sarkaz": "Sarkaz",
    "界园": "JieGarden",
    "jiegarden": "JieGarden",
    "黑流树海": "BlackFlow",
    "blackflow": "BlackFlow",
}
ROGUELIKE_THEME_NAMES = {
    "Phantom": "傀影",
    "Mizuki": "水月",
    "Sami": "萨米",
    "Sarkaz": "萨卡兹",
    "JieGarden": "界园",
    "BlackFlow": "黑流树海",
}
ROGUELIKE_MODES = {
    "刷等级": "Exp",
    "等级": "Exp",
    "exp": "Exp",
    "刷源石锭": "Gold",
    "源石锭": "Gold",
    "gold": "Gold",
    "刷开局": "LastReward",
    "开局": "LastReward",
    "lastreward": "LastReward",
    "刷坍缩范式": "Collapse",
    "坍缩": "Collapse",
    "collapse": "Collapse",
    "刷月度小队": "MonthlySquad",
    "月度小队": "MonthlySquad",
    "monthlysquad": "MonthlySquad",
    "刷深入调查": "DeepExploration",
    "深入调查": "DeepExploration",
    "deepexploration": "DeepExploration",
    "刷常乐节点": "FindPlaytime",
    "常乐节点": "FindPlaytime",
    "findplaytime": "FindPlaytime",
}
ROGUELIKE_MODE_NAMES = {
    "Exp": "刷等级",
    "Gold": "刷源石锭",
    "LastReward": "刷开局",
    "Collapse": "刷坍缩范式",
    "MonthlySquad": "刷月度小队",
    "DeepExploration": "刷深入调查",
    "FindPlaytime": "刷常乐节点",
}
ROGUELIKE_AWARDS = {
    "热水壶": "HotWater",
    "盾": "Shield",
    "锭": "Ingot",
    "希望": "Hope",
    "随机奖励": "RandomReward",
    "钥匙": "Key",
    "骰子": "Dice",
    "构想": "Idea",
    "票券": "Ticket",
}

# 肉鸽运行时需要启用的任务
ROGUELIKE_TASKS = {
    "StartUp": True,
    "Roguelike": True,
    "Fight": False,
    "Infrast": False,
    "Recruit": False,
    "Mall": False,
    "Award": False,
    "Reclamation": False,
    "UserDataUpdate": False,
    "OperProgress": False,
}


def gui_config_path(cfg: dict) -> Path:
    return Path(cfg["maa_dir"]) / "config" / "gui.new.json"


def load_gui(cfg: dict) -> dict:
    path = gui_config_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"找不到 MAA 配置：{path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_gui(cfg: dict, data: dict) -> None:
    path = gui_config_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.maa-bot.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)


def backup_gui(cfg: dict) -> Path:
    src = gui_config_path(cfg)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(ROOT) / "backup" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.parent.glob("*"):
        if item.is_file():
            shutil.copy2(item, dest / item.name)
    return dest


def close_maa(timeout: int = 30) -> bool:
    if not process_exists("MAA.exe"):
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


def start_maa(cfg: dict) -> bool:
    maa_dir = Path(cfg["maa_dir"])
    subprocess.Popen([str(maa_dir / "MAA.exe")], cwd=str(maa_dir))
    for _ in range(30):
        time.sleep(2)
        if process_exists("MAA.exe"):
            return True
    return process_exists("MAA.exe")


def current_account(cfg: dict) -> str:
    """读取 MAA 当前配置里的账号切换值。"""
    try:
        data = load_gui(cfg)
        for task in data["Configurations"]["Default"]["TaskQueue"]:
            if task.get("TaskType") == "StartUp":
                return str(task.get("AccountName") or "")
    except Exception as exc:  # noqa: BLE001
        log.warning("读取 MAA 账号失败：%s", exc)
    return ""


def matches_account(configured: str, account: dict) -> bool:
    """判断 MAA 里的账号值与某个账号是否匹配（支持掩码写法/后四位）。"""
    value = (configured or "").strip()
    if not value:
        return False
    phone = str(account.get("phone") or "")
    switch = str(account.get("switch_name") or "")
    candidates = {phone, switch, phone[-4:], f"{phone[:3]}****{phone[-4:]}"}
    return any(c and (c in value or value in c) for c in candidates)


def prepare_account(cfg: dict, account: dict, restart: bool = True, wait_ready: int = 25) -> str:
    return prepare_run(cfg, account, mode="daily", restart=restart, wait_ready=wait_ready)


def prepare_run(cfg: dict, account: dict, mode: str = "daily", restart: bool = True, wait_ready: int = 25) -> str:
    """把 MAA 配置改成「该账号 + 该账号的理智关卡」（需重启 MAA 生效）。"""
    switch_name = str(account.get("switch_name") or str(account.get("phone") or "")[-4:])
    stage = str(account.get("stage") or "").strip()
    if stage in ("当前/上次", "当前", "上次", "默认"):
        stage = ""

    data = load_gui(cfg)
    changed = False
    for task in data["Configurations"]["Default"]["TaskQueue"]:
        if task.get("TaskType") == "StartUp":
            if str(task.get("AccountName") or "") != switch_name:
                task["AccountName"] = switch_name
                changed = True
            if task.get("AccountSwitchEnabled") is False:
                task["AccountSwitchEnabled"] = True
                changed = True
        elif task.get("TaskType") == "Fight":
            plan = [str(x) for x in (task.get("StagePlan") or [""])]
            if plan != [stage]:
                task["StagePlan"] = [stage]
                changed = True
            if task.get("IsStageManually") is True:
                task["IsStageManually"] = False
                changed = True

    wanted_tasks = ROGUELIKE_TASKS if mode == "roguelike" else DESIRED_TASKS
    for task in data["Configurations"]["Default"]["TaskQueue"]:
        task_type = task.get("TaskType")
        if task_type not in wanted_tasks:
            continue
        want = wanted_tasks[task_type]
        if bool(task.get("IsEnable")) != want:
            task["IsEnable"] = want
            changed = True

    running = process_exists("MAA.exe")
    if not changed and running:
        return f"账号已是 {switch_name}，关卡 {stage or '当前/上次'}，任务集已就绪"

    if running:
        close_maa()
    save_gui(cfg, data)
    if restart:
        start_maa(cfg)
        if wait_ready:
            time.sleep(wait_ready)
    return f"已切换账号为 {switch_name}，关卡 {stage or '当前/上次'}"


# ---------------------------------------------------------------- 肉鸽设置


def _roguelike_task(data: dict) -> dict | None:
    for task in data["Configurations"]["Default"]["TaskQueue"]:
        if task.get("TaskType") == "Roguelike":
            return task
    return None


def get_roguelike(cfg: dict) -> dict:
    try:
        return _roguelike_task(load_gui(cfg)) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("读取肉鸽配置失败：%s", exc)
        return {}


def describe_roguelike(cfg: dict) -> str:
    task = get_roguelike(cfg)
    if not task:
        return "读不到肉鸽配置"

    def name_map(value: str, mapping: dict) -> str:
        return mapping.get(value, value)

    opers = "、".join(str(o.get("Name")) for o in (task.get("StartingOpers") or []) if o.get("Name"))
    awards = "、".join(
        {v: k for k, v in ROGUELIKE_AWARDS.items()}.get(x.strip(), x.strip())
        for x in str(task.get("CollectibleStartAwards") or "").split(",")
        if x.strip()
    )
    lines = [
        "**自动肉鸽当前设置**",
        f"- 主题：{name_map(str(task.get('Theme')), ROGUELIKE_THEME_NAMES)}",
        f"- 模式：{name_map(str(task.get('Mode')), ROGUELIKE_MODE_NAMES)}",
        f"- 难度：{task.get('Difficulty')}",
        f"- 分队：{task.get('Squad') or '（默认）'}",
        f"- 职业组：{task.get('Roles') or '（默认）'}",
        f"- 开局干员：{opers or '（未设置）'}",
        f"- 投资：{'开' if task.get('Investment') else '关'}（次数 {task.get('InvestCount')}）",
        f"- 期望奖励：{awards or '（未设置）'}",
        f"- 开局次数：{task.get('StartCount')}",
        f"- 满级后停止：{'开' if task.get('StopWhenLevelMax') else '关'}"
        f"；第五层 BOSS 前暂停：{'开' if task.get('StopAtFinalBoss') else '关'}"
        f"；凹精英二开局：{'开' if task.get('StartWithEliteTwo') else '关'}",
        "",
        "改设置：`肉鸽 主题=萨米 模式=刷等级 难度=15 分队=特训分队 开局干员=令,黍`",
        "开跑：`肉鸽开始`（先用 `主号` / `小号` 切到目标账号）",
    ]
    return "\n".join(lines)


def set_roguelike(cfg: dict, options: dict, restart: bool = True, wait_ready: int = 25) -> str:
    """按给定选项修改 MAA 肉鸽任务设置（需要重启 MAA 生效）。"""
    data = load_gui(cfg)
    task = _roguelike_task(data)
    if task is None:
        return "MAA 配置里没有自动肉鸽任务"

    applied: list[str] = []
    unknown: list[str] = []

    def as_bool(value) -> bool:
        return str(value).strip().lower() in ("1", "true", "on", "开", "是", "yes", "y")

    def bounded_int(label: str, value, minimum: int, maximum: int) -> int | None:
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError, OverflowError):
            unknown.append(f"{label} {value}（不是整数）")
            return None
        if not minimum <= number <= maximum:
            unknown.append(f"{label} {value}（范围 {minimum}-{maximum}）")
            return None
        return number

    for raw_key, value in options.items():
        key = raw_key.strip().lower()
        text = str(value).strip()
        if key in ("主题", "theme"):
            theme = ROGUELIKE_THEMES.get(text.lower(), ROGUELIKE_THEMES.get(text))
            if theme:
                task["Theme"] = theme
                applied.append(f"主题={ROGUELIKE_THEME_NAMES.get(theme, theme)}")
            else:
                unknown.append(f"主题 {text}")
        elif key in ("模式", "mode"):
            mode = ROGUELIKE_MODES.get(text.lower(), ROGUELIKE_MODES.get(text))
            if mode:
                task["Mode"] = mode
                applied.append(f"模式={ROGUELIKE_MODE_NAMES.get(mode, mode)}")
            else:
                unknown.append(f"模式 {text}")
        elif key in ("难度", "difficulty"):
            number = bounded_int("难度", text, 0, 15)
            if number is not None:
                task["Difficulty"] = number
                applied.append(f"难度={number}")
        elif key in ("分队", "squad"):
            task["Squad"] = text
            applied.append(f"分队={text}")
        elif key in ("职业组", "roles", "职业"):
            task["Roles"] = text
            applied.append(f"职业组={text}")
        elif key in ("开局干员", "干员", "opers"):
            names = [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]
            task["StartingOpers"] = [{"Name": n, "UseSupport": False} for n in names]
            applied.append("开局干员=" + "、".join(names))
        elif key in ("投资", "invest"):
            task["Investment"] = as_bool(text)
            applied.append(f"投资={'开' if task['Investment'] else '关'}")
        elif key in ("投资次数", "investcount"):
            number = bounded_int("投资次数", text, 0, 9999)
            if number is not None:
                task["InvestCount"] = number
                applied.append(f"投资次数={number}")
        elif key in ("期望奖励", "奖励", "awards"):
            codes = []
            for item in text.replace("，", ",").split(","):
                item = item.strip()
                if not item:
                    continue
                codes.append(ROGUELIKE_AWARDS.get(item, item))
            task["CollectibleStartAwards"] = ", ".join(codes)
            applied.append("期望奖励=" + "、".join(codes))
        elif key in ("开局次数", "startcount"):
            number = bounded_int("开局次数", text, 1, 999999)
            if number is not None:
                task["StartCount"] = number
                applied.append(f"开局次数={number}")
        elif key in ("满级停止", "stopwhenlevelmax"):
            task["StopWhenLevelMax"] = as_bool(text)
            applied.append(f"满级停止={'开' if task['StopWhenLevelMax'] else '关'}")
        elif key in ("boss前暂停", "第五层暂停", "stopatfinalboss"):
            task["StopAtFinalBoss"] = as_bool(text)
            applied.append(f"第五层BOSS前暂停={'开' if task['StopAtFinalBoss'] else '关'}")
        elif key in ("凹精英二", "精英二", "startwithelitetwo"):
            task["StartWithEliteTwo"] = as_bool(text)
            applied.append(f"凹精英二={'开' if task['StartWithEliteTwo'] else '关'}")
        else:
            unknown.append(raw_key)

    if not applied:
        return "没有可应用的选项；" + ("无法识别：" + "、".join(unknown) if unknown else describe_roguelike(cfg))

    running = process_exists("MAA.exe")
    if running:
        close_maa()
    save_gui(cfg, data)
    if restart:
        start_maa(cfg)
        if wait_ready:
            time.sleep(wait_ready)
    text = "已更新肉鸽设置：" + "；".join(applied)
    if unknown:
        text += "；忽略无法识别的项：" + "、".join(unknown)
    return text


def set_account(cfg: dict, switch_name: str, restart: bool = True, wait_ready: int = 25) -> str:
    """只切换账号（保留原关卡设置）。"""
    account = {"switch_name": switch_name, "stage": _current_stage(cfg)}
    return prepare_account(cfg, account, restart=restart, wait_ready=wait_ready)


def _current_stage(cfg: dict) -> str:
    try:
        data = load_gui(cfg)
        for task in data["Configurations"]["Default"]["TaskQueue"]:
            if task.get("TaskType") == "Fight":
                plan = task.get("StagePlan") or [""]
                return str(plan[0] or "") if plan else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def current_stage(cfg: dict) -> str:
    return _current_stage(cfg)
