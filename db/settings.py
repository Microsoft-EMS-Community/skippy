# ============================================================
# db/settings.py - Settings mods change from Discord (e.g. the feed
# activity rule, the weekly roundup).
# ============================================================

from .connection import execute, fetch_value

SCHEMA = """
    CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


async def get_setting(key: str, default: str | None = None) -> str | None:
    return await fetch_value("SELECT value FROM bot_settings WHERE key = ?", (key,), default)


async def set_setting(key: str, value: str):
    await execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
