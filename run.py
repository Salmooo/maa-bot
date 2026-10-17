"""Source-tree launcher.

All Windows scheduled tasks should call this file so they work without an
editable package installation. Example: ``python run.py service``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MAA_BOT_HOME", str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from maa_bot.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
