from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, default).split(",") if x.strip())


# ---------------------------------------------------------------------------
# Valid Gemini models confirmed working via API (October 2026).
# Ordered model choices shown to the admin; runtime uses only the selected model.
# Dead models removed: gemini-3.1-flash, gemini-2.5-*, gemini-3.0-*
# ---------------------------------------------------------------------------
ALL_KNOWN_MODELS: tuple[str, ...] = (
    "gemini-3.1-flash-lite",   # ⚡ Fastest · cheapest · default
    "gemini-3.5-flash-lite",   # ⚡ 3.5 Lite · fast alternative
    "gemini-3.5-flash",        # ⚖️  3.5 Flash · balanced
    "gemini-3.6-flash",        # ⚖️  3.6 Flash
    "gemini-3.7-flash",        # ⚖️  3.7 Flash
    "gemini-3.8-flash",        # ⚖️  3.8 Flash · most reliable fallback
    "gemini-3.1-pro-preview",  # 🎯 Pro · highest quality · slowest
)

MODEL_LABELS: dict[str, str] = {
    "gemini-3.1-flash-lite":  "⚡ Fastest",
    "gemini-3.5-flash-lite":  "⚡ 3.5 Lite",
    "gemini-3.5-flash":       "⚖️  3.5 Flash",
    "gemini-3.6-flash":       "⚖️  3.6 Flash",
    "gemini-3.7-flash":       "⚖️  3.7 Flash",
    "gemini-3.8-flash":       "⚖️  3.8 Flash",
    "gemini-3.1-pro-preview": "🎯 Pro",
}


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_user_id: int
    gemini_keys: tuple[str, ...]
    models: tuple[str, ...]
    default_model: str

    max_files: int = 12
    max_download_mb: int = 20
    max_zip_members: int = 12
    max_zip_uncompressed_mb: int = 250
    max_entries_per_call: int = 50
    max_chars_per_call: int = 12000
    context_entries: int = 8
    inter_call_delay: float = 1.2
    retry_per_attempt: int = 2
    result_ttl_minutes: int = 15
    max_errors_shown: int = 5
    send_delay: float = 0.7
    data_dir: Path = Path("data")
    auto_model_fallback: bool = False
    dev_group_id: int | None = None

    @staticmethod
    def from_env() -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise ValueError("BOT_TOKEN is missing")

        try:
            admin_user_id = int(os.getenv("ADMIN_USER_ID", "0"))
        except ValueError as exc:
            raise ValueError("ADMIN_USER_ID must be a numeric Telegram user ID") from exc
        if admin_user_id <= 0:
            raise ValueError("ADMIN_USER_ID must be set to your numeric Telegram user ID")

        # Keep all ten positions so Fast/Super Fast retry lanes always map to
        # the actual GEMINI_API_KEY_N variable, even if a slot is missing.
        keys = [os.getenv(f"GEMINI_API_KEY_{i}", "").strip() for i in range(1, 11)]
        if not keys[0]:
            raise ValueError("GEMINI_API_KEY_1 is required")
        if not any(keys):
            raise ValueError("At least one GEMINI_API_KEY_N is required")

        # Default fallback chain — only confirmed-working models
        default_fallback = (
            "gemini-3.1-flash-lite,"
            "gemini-3.5-flash-lite,"
            "gemini-3.5-flash,"
            "gemini-3.6-flash,"
            "gemini-3.7-flash,"
            "gemini-3.8-flash,"
            "gemini-3.1-pro-preview"
        )
        models = _csv_env("AVAILABLE_MODELS", default_fallback)
        if not models:
            raise ValueError("AVAILABLE_MODELS is empty")

        default_model = os.getenv("DEFAULT_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite"
        if default_model not in models:
            models = (default_model, *models)

        data_dir = Path(os.getenv("DATA_DIR", "data"))
        raw_dev_group_id = os.getenv("DEV_GROUP_ID", "").strip()
        try:
            dev_group_id = int(raw_dev_group_id) if raw_dev_group_id else None
        except ValueError as exc:
            raise ValueError("DEV_GROUP_ID must be a numeric Telegram chat ID") from exc

        return Settings(
            bot_token=bot_token,
            admin_user_id=admin_user_id,
            gemini_keys=tuple(keys),
            models=models,
            default_model=default_model,
            max_files=max(1, int(os.getenv("MAX_FILES", "12"))),
            max_download_mb=max(1, int(os.getenv("MAX_DOWNLOAD_MB", "20"))),
            max_zip_members=max(1, int(os.getenv("MAX_ZIP_MEMBERS", "12"))),
            max_zip_uncompressed_mb=max(10, int(os.getenv("MAX_ZIP_UNCOMPRESSED_MB", "250"))),
            max_entries_per_call=max(1, int(os.getenv("MAX_ENTRIES_PER_CALL", "50"))),
            max_chars_per_call=max(2000, int(os.getenv("MAX_CHARS_PER_CALL", "12000"))),
            context_entries=max(0, int(os.getenv("CONTEXT_ENTRIES", "8"))),
            inter_call_delay=max(0.0, float(os.getenv("INTER_CALL_DELAY", "1.2"))),
            retry_per_attempt=max(1, int(os.getenv("RETRY_PER_ATTEMPT", "2"))),
            result_ttl_minutes=max(1, int(os.getenv("RESULT_TTL_MINUTES", "15"))),
            max_errors_shown=max(1, int(os.getenv("MAX_ERRORS_SHOWN", "5"))),
            send_delay=max(0.0, float(os.getenv("SEND_DELAY", "0.7"))),
            data_dir=data_dir,
            auto_model_fallback=False,  # Fixed: selected model only; no automatic model fallback
            dev_group_id=dev_group_id,
        )
