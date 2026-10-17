from __future__ import annotations

import unittest

from maa_bot.policy import is_chat_allowed


class PolicyTests(unittest.TestCase):
    def test_first_private_chat_can_bind(self) -> None:
        self.assertTrue(is_chat_allowed({"chat_id": ""}, "owner", 1))

    def test_other_private_chat_is_rejected_after_binding(self) -> None:
        self.assertFalse(is_chat_allowed({"chat_id": "owner"}, "intruder", 1))

    def test_group_is_disabled_by_default(self) -> None:
        self.assertFalse(is_chat_allowed({"allowed_chat_ids": ["group"]}, "group", 2))

    def test_explicit_group_allowlist(self) -> None:
        cfg = {"allow_group_chat": True, "allowed_chat_ids": ["group"]}
        self.assertTrue(is_chat_allowed(cfg, "group", 2))
        self.assertFalse(is_chat_allowed(cfg, "other", 2))


if __name__ == "__main__":
    unittest.main()
