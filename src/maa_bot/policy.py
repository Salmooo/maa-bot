"""Authorization policy for chat-triggered actions."""

from __future__ import annotations


def allowed_chat_ids(cfg: dict) -> set[str]:
    values = [str(value).strip() for value in (cfg.get("allowed_chat_ids") or [])]
    configured = str(cfg.get("chat_id") or "").strip()
    if configured:
        values.append(configured)
    return {value for value in values if value}


def is_chat_allowed(cfg: dict, chat_id: str, chat_type: int) -> bool:
    """Return whether a callback may control the local MAA instance.

    The first private conversation may be captured when no chat has been
    configured yet. Once an ID exists, only explicitly allowed IDs are trusted.
    Group commands are disabled by default.
    """
    if not chat_id:
        return False
    if chat_type == 2 and not bool(cfg.get("allow_group_chat", False)):
        return False
    trusted = allowed_chat_ids(cfg)
    if trusted:
        return chat_id in trusted
    return chat_type == 1 or bool(cfg.get("allow_group_chat", False))
