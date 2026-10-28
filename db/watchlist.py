# ============================================================
# db/watchlist.py - The RSS watchlist and the messages counted for each
# watched member.
# ============================================================

from datetime import timedelta

from .connection import execute, fetch_all, fetch_one, fetch_value, utc_now

SCHEMA = """
    CREATE TABLE IF NOT EXISTS rss_watchlist (
        user_id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        added_by TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS rss_activity (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        message_link TEXT NOT NULL,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""


def _cutoff(days: int) -> str:
    """N days ago, in the same format as CURRENT_TIMESTAMP rows."""
    return (utc_now() - timedelta(days=days)).isoformat(" ")


async def add_to_watchlist(user_id: str, guild_id: str, added_by: str):
    """Upsert a user into the RSS watchlist."""
    await execute(
        """INSERT INTO rss_watchlist (user_id, guild_id, added_by)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET added_by=excluded.added_by, added_at=CURRENT_TIMESTAMP""",
        (user_id, guild_id, added_by)
    )


async def remove_from_watchlist(user_id: str):
    """Remove a user from the RSS watchlist."""
    await execute("DELETE FROM rss_watchlist WHERE user_id = ?", (user_id,))


async def get_watchlist(guild_id: str) -> list[dict]:
    """Return all watched users for a guild."""
    return await fetch_all("SELECT * FROM rss_watchlist WHERE guild_id = ? ORDER BY added_at ASC", (guild_id,))


async def get_watch_entry(user_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM rss_watchlist WHERE user_id = ?", (user_id,))


async def is_watched(user_id: str) -> bool:
    """Return True if the user is on the watchlist."""
    return await fetch_value("SELECT 1 FROM rss_watchlist WHERE user_id = ? LIMIT 1", (user_id,)) is not None


async def log_rss_activity(user_id: str, guild_id: str, channel_id: str,
                           message_id: str, message_link: str):
    """Record a message from a watched user."""
    await execute(
        """INSERT INTO rss_activity (user_id, guild_id, channel_id, message_id, message_link)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, guild_id, channel_id, message_id, message_link)
    )


async def get_rss_activity(user_id: str, days: int = 30) -> list[dict]:
    """Return all activity rows for a user within the last N days."""
    return await fetch_all(
        """SELECT * FROM rss_activity
           WHERE user_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC""",
        (user_id, _cutoff(days))
    )


async def get_rss_activity_count(user_id: str, days: int = 30) -> int:
    """Return message count for a user in the last N days."""
    return await fetch_value("SELECT COUNT(*) FROM rss_activity WHERE user_id = ? AND sent_at >= ?",
                             (user_id, _cutoff(days)), default=0)


async def get_latest_rss_message(user_id: str) -> dict | None:
    """Return the most recent activity row for a user."""
    return await fetch_one("SELECT * FROM rss_activity WHERE user_id = ? ORDER BY sent_at DESC LIMIT 1", (user_id,))


async def cleanup_rss_activity(days: int = 30) -> int:
    """Delete rss_activity rows older than N days. Returns count deleted."""
    cursor = await execute("DELETE FROM rss_activity WHERE sent_at < ?", (_cutoff(days),))
    return cursor.rowcount
