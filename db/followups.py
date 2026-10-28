# ============================================================
# db/followups.py - Follow-ups (one row per flagged message) and each
# member's follow-up score.
# ============================================================

from datetime import timedelta

from .connection import connect, execute, fetch_all, fetch_one, fetch_value, utc_now

SCHEMA = """
    CREATE TABLE IF NOT EXISTS follow_ups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        channel_id TEXT,
        message_link TEXT,
        question_snippet TEXT,
        flagged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP,
        resolver_id TEXT,
        status TEXT DEFAULT 'open',
        warning_sent INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS user_scores (
        user_id TEXT PRIMARY KEY,
        guild_id TEXT NOT NULL,
        flag_count INTEGER DEFAULT 0,
        resolved_count INTEGER DEFAULT 0,
        ignored_count INTEGER DEFAULT 0,
        last_flagged TIMESTAMP,
        last_resolved TIMESTAMP
    );
"""


async def add_follow_up(user_id: str, guild_id: str, channel_id: str,
                        message_link: str, question_snippet: str) -> int:
    """Insert a new open follow-up record. Returns the new row id."""
    cursor = await execute(
        """INSERT INTO follow_ups
           (user_id, guild_id, channel_id, message_link, question_snippet)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, guild_id, channel_id, message_link, question_snippet)
    )
    return cursor.lastrowid


async def get_open_follow_ups(user_id: str) -> list[dict]:
    """Return all open follow-up records for a given user."""
    return await fetch_all("SELECT * FROM follow_ups WHERE user_id = ? AND status = 'open'", (user_id,))


async def resolve_all_open_follow_ups(user_id: str, resolver_id: str):
    """Mark ALL open follow-ups for a user as resolved (e.g., on role removal)."""
    await execute(
        """UPDATE follow_ups
           SET status = 'resolved', resolved_at = ?, resolver_id = ?
           WHERE user_id = ? AND status = 'open'""",
        (utc_now().isoformat(), resolver_id, user_id)
    )


async def get_overdue_follow_ups(hours: int) -> list[dict]:
    """Return open follow-ups older than `hours` hours (for warning tasks)."""
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    return await fetch_all("SELECT * FROM follow_ups WHERE status = 'open' AND flagged_at < ?", (cutoff,))


async def mark_warning_sent(follow_up_id: int):
    """Set warning_sent = 1 for a follow-up so we don't re-warn."""
    await execute("UPDATE follow_ups SET warning_sent = 1 WHERE id = ?", (follow_up_id,))


async def mark_ignored(follow_up_id: int):
    """Mark a follow-up as ignored (>7 days with no response)."""
    await execute("UPDATE follow_ups SET status = 'ignored' WHERE id = ?", (follow_up_id,))


async def mark_left_server(user_id: str):
    """Mark all open follow-ups as 'left_server' when a user leaves."""
    await execute("UPDATE follow_ups SET status = 'left_server' WHERE user_id = ? AND status = 'open'", (user_id,))


async def get_left_server_follow_ups(user_id: str) -> list[dict]:
    """Return follow-ups marked left_server (for when a user rejoins)."""
    return await fetch_all("SELECT * FROM follow_ups WHERE user_id = ? AND status = 'left_server'", (user_id,))


async def delete_closed_follow_ups(older_than_days: int) -> int:
    """Delete resolved, ignored and left-server follow-ups flagged more than N days ago. Returns how many."""
    cutoff = (utc_now() - timedelta(days=older_than_days)).isoformat()
    cursor = await execute(
        """DELETE FROM follow_ups
           WHERE status IN ('resolved', 'ignored', 'left_server')
           AND flagged_at < ?""",
        (cutoff,)
    )
    return cursor.rowcount


# ──────────────────────────────────────────────
# Scores
# ──────────────────────────────────────────────

async def increment_flag_count(user_id: str, guild_id: str):
    """Upsert user_scores row and bump flag_count."""
    await execute(
        """INSERT INTO user_scores (user_id, guild_id, flag_count, last_flagged)
           VALUES (?, ?, 1, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               flag_count = flag_count + 1,
               last_flagged = excluded.last_flagged""",
        (user_id, guild_id, utc_now().isoformat())
    )


async def increment_resolved_count(user_id: str):
    """Bump resolved_count for a user."""
    await execute(
        """UPDATE user_scores
           SET resolved_count = resolved_count + 1, last_resolved = ?
           WHERE user_id = ?""",
        (utc_now().isoformat(), user_id)
    )


async def increment_ignored_count(user_id: str):
    """Bump ignored_count for a user."""
    await execute("UPDATE user_scores SET ignored_count = ignored_count + 1 WHERE user_id = ?", (user_id,))


async def get_user_score(user_id: str) -> dict | None:
    """Return the user_scores row for a user, or None if not found."""
    return await fetch_one("SELECT * FROM user_scores WHERE user_id = ?", (user_id,))


async def get_avg_resolution_time(user_id: str) -> float | None:
    """Calculate average resolution time in hours for a user."""
    return await fetch_value(
        """SELECT AVG(
               (julianday(resolved_at) - julianday(flagged_at)) * 24
           ) as avg_hours
           FROM follow_ups
           WHERE user_id = ? AND status = 'resolved'
             AND resolved_at IS NOT NULL""",
        (user_id,)
    )


async def get_server_stats(guild_id: str) -> dict:
    """Return server-wide follow-up statistics for /stats command."""
    async with connect() as conn:
        async def count(status_condition: str) -> int:
            cursor = await conn.execute(f"SELECT COUNT(*) FROM follow_ups WHERE guild_id = ?{status_condition}",
                                        (guild_id,))
            return (await cursor.fetchone())[0]

        total = await count("")
        open_count = await count(" AND status = 'open'")
        resolved_count = await count(" AND status = 'resolved'")
        ignored_count = await count(" AND status = 'ignored'")

        # Average resolution time across all resolved follow-ups
        cursor = await conn.execute(
            """SELECT AVG((julianday(resolved_at) - julianday(flagged_at)) * 24) as avg_hours
               FROM follow_ups
               WHERE guild_id = ? AND status = 'resolved' AND resolved_at IS NOT NULL""",
            (guild_id,)
        )
        avg_hours = (await cursor.fetchone())[0]

    # Top 5 most flagged users
    top_flagged = await fetch_all(
        """SELECT user_id, flag_count FROM user_scores
           WHERE guild_id = ? ORDER BY flag_count DESC LIMIT 5""",
        (guild_id,)
    )
    # Top 5 most active helpers (by unmark/flag actions in audit log)
    top_helpers = await fetch_all(
        """SELECT actor_id, COUNT(*) as actions FROM audit_log
           WHERE action IN ('flagged_via_reaction', 'flagged_via_command', 'unmark', 'unmark_button')
           AND actor_id IS NOT NULL
           GROUP BY actor_id ORDER BY actions DESC LIMIT 5""",
    )
    return {
        "total": total,
        "open": open_count,
        "resolved": resolved_count,
        "ignored": ignored_count,
        "avg_hours": avg_hours,
        "top_flagged": top_flagged,
        "top_helpers": top_helpers,
    }
