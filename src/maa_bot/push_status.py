"""MAA「结束后脚本」入口：把最近一次运行的状态摘要推送到企业微信。

如果常驻服务正在运行（它会实时推送），这里会自动跳过，避免重复消息。
"""

from __future__ import annotations

import argparse
import asyncio

from .common import load_config, service_running, setup_logging  # noqa: E402
from .maa import build_summary, maa_log_path  # noqa: E402
from .wecom import send_message_once  # noqa: E402

log = setup_logging("push", "push.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="推送 MAA 运行状态")
    parser.add_argument("--dry-run", action="store_true", help="只生成内容，不发送")
    parser.add_argument("--ignore-service", action="store_true", help="即使常驻服务在运行也发送")
    args = parser.parse_args()

    cfg = load_config()
    log_path = maa_log_path(cfg)
    if not log_path.exists():
        log.error("找不到 MAA 日志：%s", log_path)
        return 2

    if not args.ignore_service and service_running(cfg):
        log.info("常驻服务在运行，跳过本次推送")
        return 0

    content = build_summary(cfg)
    log.info("推送内容：\n%s", content)
    if args.dry_run:
        log.info("dry-run：未发送")
        return 0

    if not cfg.get("chat_id"):
        log.error("未配置 chat_id，无法发送")
        return 4
    try:
        resp = asyncio.run(send_message_once(cfg, content))
    except Exception as exc:  # noqa: BLE001
        log.error("推送异常：%s", exc)
        return 5
    return 0 if resp and resp.get("errcode") == 0 else 5


if __name__ == "__main__":
    raise SystemExit(main())
