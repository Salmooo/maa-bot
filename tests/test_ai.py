from __future__ import annotations

import unittest

from maa_bot import ai


class AiNormalizeTests(unittest.TestCase):
    def test_default_model_is_flash(self) -> None:
        self.assertEqual(ai.MODEL, "deepseek-flash")

    def test_unknown_actions_are_rejected(self) -> None:
        actions = ai.normalize(
            {
                "actions": [
                    {"type": "run_shell", "command": "whoami"},
                    {"type": "status"},
                ]
            }
        )
        self.assertEqual(actions, [{"type": "status", "account": "", "after_previous": False}])

    def test_roguelike_options_are_whitelisted(self) -> None:
        actions = ai.normalize(
            {
                "actions": [
                    {
                        "type": "set_roguelike",
                        "options": {"主题": "萨米", "path": "C:\\secret.txt"},
                    }
                ]
            }
        )
        self.assertEqual(actions[0]["options"], {"主题": "萨米"})

    def test_invalid_time_is_ignored(self) -> None:
        actions = ai.normalize({"actions": [{"type": "run_at", "time": "99:99"}]})
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
