from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from maa_bot.common import load_config, merge_defaults, save_config, validate_config


class ConfigTests(unittest.TestCase):
    def test_defaults_are_deep_merged(self) -> None:
        cfg = merge_defaults({"adb_login": {"enabled": True}, "schedule_times": ["09:30"]})
        self.assertTrue(cfg["adb_login"]["enabled"])
        self.assertIn("account_field", cfg["adb_login"])
        self.assertEqual(cfg["schedule_times"], ["09:30"])
        self.assertEqual(cfg["deepseek_model"], "deepseek-flash")

    def test_duplicate_accounts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maa_dir = Path(tmp)
            (maa_dir / "MAA.exe").touch()
            cfg = merge_defaults(
                {
                    "bot_id": "bot",
                    "secret": "secret",
                    "maa_dir": str(maa_dir),
                    "accounts": [
                        {"name": "主号", "switch_name": "9083"},
                        {"name": "主号", "switch_name": "9083"},
                    ],
                }
            )
            problems = validate_config(cfg)
        self.assertTrue(any("名称重复" in item for item in problems))
        self.assertTrue(any("切换值重复" in item for item in problems))

    def test_invalid_schedule_is_reported(self) -> None:
        cfg = merge_defaults({"schedule_times": ["25:00"]})
        with tempfile.TemporaryDirectory() as tmp:
            maa_dir = Path(tmp)
            (maa_dir / "MAA.exe").touch()
            cfg.update({"bot_id": "bot", "secret": "secret", "maa_dir": str(maa_dir)})
            problems = validate_config(cfg)
        self.assertTrue(any("无效定时时间" in item for item in problems))

    def test_environment_secret_is_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"bot_id": "file-bot", "secret": "file-secret"}', encoding="utf-8")
            with patch.dict("os.environ", {"MAA_BOT_SECRET": "environment-secret"}):
                cfg = load_config(path)
                self.assertEqual(cfg["secret"], "environment-secret")
                save_config(cfg)
            saved = path.read_text(encoding="utf-8")
        self.assertNotIn("environment-secret", saved)
        self.assertIn("file-secret", saved)


if __name__ == "__main__":
    unittest.main()
