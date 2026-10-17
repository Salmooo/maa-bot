from __future__ import annotations

import unittest

from maa_bot.wecom import redact


class RedactTests(unittest.TestCase):
    def test_secrets_are_redacted_recursively(self) -> None:
        value = {
            "body": {
                "secret": "abc",
                "items": [{"api_key": "xyz", "safe": 1}],
                "response_url": "https://example.test/token",
            }
        }
        result = redact(value)
        self.assertEqual(result["body"]["secret"], "<redacted>")
        self.assertEqual(result["body"]["items"][0]["api_key"], "<redacted>")
        self.assertEqual(result["body"]["response_url"], "<redacted>")
        self.assertEqual(result["body"]["items"][0]["safe"], 1)


if __name__ == "__main__":
    unittest.main()
