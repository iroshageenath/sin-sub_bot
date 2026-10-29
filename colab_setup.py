"""Optional helper for Google Colab.

The recommended method is the Colab notebook included with the project.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_colab_secrets() -> None:
    try:
        from google.colab import userdata
    except Exception:
        return
    for name in [
        "BOT_TOKEN",
        "ADMIN_USER_ID",
        *[f"GEMINI_API_KEY_{i}" for i in range(1, 11)],
    ]:
        try:
            value = userdata.get(name)
        except Exception:
            value = ""
        if value:
            os.environ[name] = value
    os.environ.setdefault("DATA_DIR", "/content/anime_sinhala_bot_data")


if __name__ == "__main__":
    load_colab_secrets()
    print("Colab environment prepared. Run: python bot.py")
