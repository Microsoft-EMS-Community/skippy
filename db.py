# ============================================================
# db.py - Database layer (aiosqlite, async)
# Handles all SQLite interactions for follow-up tracking.
# ============================================================

import aiosqlite
import datetime
from config import DB_PATH


async def init_db():
    """Initialize the database and create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
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

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                actor_id TEXT,
                target_id TEXT,
                detail TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

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

            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                role_id TEXT,
                added_by TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                etag TEXT,
                last_modified TEXT,
                last_checked TIMESTAMP,
                last_error TEXT,
                failures INTEGER NOT NULL DEFAULT 0,
                UNIQUE (url, channel_id)
            );

            CREATE TABLE IF NOT EXISTS feed_items (
                feed_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (feed_id, item_key)
            );

            CREATE TABLE IF NOT EXISTS feed_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                feed_title TEXT,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                message_url TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS feed_posts_posted_at ON feed_posts (posted_at);

            CREATE TABLE IF NOT EXISTS feed_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                suggested_by TEXT NOT NULL,
                own_blog INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
                decided_by TEXT,
                reason TEXT,
                message_id TEXT,                          -- the mods' card with the buttons
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMP
            );

            -- Settings mods change from Discord (e.g. the feed activity rule).
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- Last activity-rule state per feed author ('active', 'warned',
            -- 'paused'), so each change is announced once.
            CREATE TABLE IF NOT EXISTS feed_author_state (
                user_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Columns added after the feeds table first shipped; add them to
        # databases created before that.
        await _add_missing_columns(db, "feeds", {
            "include_words": "TEXT",
            "exclude_words": "TEXT",
            "highlight_words": "TEXT",
            "summary_after": "INTEGER NOT NULL DEFAULT 5",
            "author_id": "TEXT",
            "activity_exempt": "INTEGER NOT NULL DEFAULT 0",
        })
        await db.commit()


async def _add_missing_columns(db, table: str, columns: dict[str, str]):
    cursor = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


# ──────────────────────────────────────────────
# Follow-up CRUD
# ──────────────────────────────────────────────

async def add_follow_up(user_id: str, guild_id: str, channel_id: str,
                         message_link: str, question_snippet: str) -> int:
    """Insert a new open follow-up record. Returns the new row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO follow_ups
               (user_id, guild_id, channel_id, message_link, question_snippet)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, guild_id, channel_id, message_link, question_snippet)
        )
        await db.commit()
        return cursor.lastrowid


async def get_open_follow_ups(user_id: str) -> list[dict]:
    """Return all open follow-up records for a given user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM follow_ups WHERE user_id = ? AND status = 'open'",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def resolve_follow_up(follow_up_id: int, resolver_id: str):
    """Mark a specific follow-up as resolved."""
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE follow_ups
               SET status = 'resolved', resolved_at = ?, resolver_id = ?
               WHERE id = ?""",
            (now, resolver_id, follow_up_id)
        )
        await db.commit()


async def resolve_all_open_follow_ups(user_id: str, resolver_id: str):
    """Mark ALL open follow-ups for a user as resolved (e.g., on role removal)."""
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE follow_ups
               SET status = 'resolved', resolved_at = ?, resolver_id = ?
               WHERE user_id = ? AND status = 'open'""",
            (now, resolver_id, user_id)
        )
        await db.commit()


async def get_overdue_follow_ups(hours: int) -> list[dict]:
    """Return open follow-ups older than `hours` hours (for warning tasks)."""
    cutoff = (datetime.datetime.utcnow() -
              datetime.timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM follow_ups
               WHERE status = 'open' AND flagged_at < ?""",
            (cutoff,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def mark_warning_sent(follow_up_id: int):
    """Set warning_sent = 1 for a follow-up so we don't re-warn."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE follow_ups SET warning_sent = 1 WHERE id = ?",
            (follow_up_id,)
        )
        await db.commit()


async def mark_ignored(follow_up_id: int):
    """Mark a follow-up as ignored (>7 days with no response)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE follow_ups SET status = 'ignored' WHERE id = ?",
            (follow_up_id,)
        )
        await db.commit()


async def mark_left_server(user_id: str):
    """Mark all open follow-ups as 'left_server' when a user leaves."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE follow_ups SET status = 'left_server' WHERE user_id = ? AND status = 'open'",
            (user_id,)
        )
        await db.commit()


async def get_left_server_follow_ups(user_id: str) -> list[dict]:
    """Return follow-ups marked left_server (for when a user rejoins)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM follow_ups WHERE user_id = ? AND status = 'left_server'",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# User Scores
# ──────────────────────────────────────────────

async def increment_flag_count(user_id: str, guild_id: str):
    """Upsert user_scores row and bump flag_count."""
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO user_scores (user_id, guild_id, flag_count, last_flagged)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   flag_count = flag_count + 1,
                   last_flagged = excluded.last_flagged""",
            (user_id, guild_id, now)
        )
        await db.commit()


async def increment_resolved_count(user_id: str):
    """Bump resolved_count for a user."""
    now = datetime.datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE user_scores
               SET resolved_count = resolved_count + 1, last_resolved = ?
               WHERE user_id = ?""",
            (now, user_id)
        )
        await db.commit()


async def increment_ignored_count(user_id: str):
    """Bump ignored_count for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_scores SET ignored_count = ignored_count + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.commit()


async def get_user_score(user_id: str) -> dict | None:
    """Return the user_scores row for a user, or None if not found."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_scores WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_avg_resolution_time(user_id: str) -> float | None:
    """Calculate average resolution time in hours for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT AVG(
                   (julianday(resolved_at) - julianday(flagged_at)) * 24
               ) as avg_hours
               FROM follow_ups
               WHERE user_id = ? AND status = 'resolved'
                 AND resolved_at IS NOT NULL""",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None


# ──────────────────────────────────────────────
# Audit Log
# ──────────────────────────────────────────────

async def add_audit_log(action: str, actor_id: str | None,
                         target_id: str | None, detail: str | None):
    """Append an entry to the audit log."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO audit_log (action, actor_id, target_id, detail)
               VALUES (?, ?, ?, ?)""",
            (action, actor_id, target_id, detail)
        )
        await db.commit()


async def get_recent_audit_log(limit: int = 10) -> list[dict]:
    """Return the most recent `limit` audit log entries."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# Server-wide Stats
# ──────────────────────────────────────────────

async def get_server_stats(guild_id: str) -> dict:
    """Return server-wide follow-up statistics for /stats command."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Overall counts
        cursor = await db.execute(
            "SELECT COUNT(*) as total FROM follow_ups WHERE guild_id = ?", (guild_id,)
        )
        total = (await cursor.fetchone())["total"]

        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM follow_ups WHERE guild_id = ? AND status = 'open'", (guild_id,)
        )
        open_count = (await cursor.fetchone())["c"]

        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM follow_ups WHERE guild_id = ? AND status = 'resolved'", (guild_id,)
        )
        resolved_count = (await cursor.fetchone())["c"]

        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM follow_ups WHERE guild_id = ? AND status = 'ignored'", (guild_id,)
        )
        ignored_count = (await cursor.fetchone())["c"]

        # Average resolution time across all resolved follow-ups
        cursor = await db.execute(
            """SELECT AVG((julianday(resolved_at) - julianday(flagged_at)) * 24) as avg_hours
               FROM follow_ups
               WHERE guild_id = ? AND status = 'resolved' AND resolved_at IS NOT NULL""",
            (guild_id,)
        )
        row = await cursor.fetchone()
        avg_hours = row["avg_hours"] if row and row["avg_hours"] is not None else None

        # Top 5 most flagged users
        cursor = await db.execute(
            """SELECT user_id, flag_count FROM user_scores
               WHERE guild_id = ? ORDER BY flag_count DESC LIMIT 5""",
            (guild_id,)
        )
        top_flagged = [dict(r) for r in await cursor.fetchall()]

        # Top 5 most active helpers (by unmark/flag actions in audit log)
        cursor = await db.execute(
            """SELECT actor_id, COUNT(*) as actions FROM audit_log
               WHERE action IN ('flagged_via_reaction', 'flagged_via_command', 'unmark', 'unmark_button')
               AND actor_id IS NOT NULL
               GROUP BY actor_id ORDER BY actions DESC LIMIT 5""",
        )
        top_helpers = [dict(r) for r in await cursor.fetchall()]

        return {
            "total": total,
            "open": open_count,
            "resolved": resolved_count,
            "ignored": ignored_count,
            "avg_hours": avg_hours,
            "top_flagged": top_flagged,
            "top_helpers": top_helpers,
        }


# ──────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────

async def cleanup_old_records(
    audit_log_days: int = 90,
    follow_up_days: int = 180
) -> dict:
    """
    Prune old records to keep the DB lean.
    - audit_log: delete entries older than audit_log_days
    - follow_ups: delete resolved/ignored rows older than follow_up_days
    - user_scores: never touched (reputation data kept forever)
    Returns counts of deleted rows.
    """
    audit_cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=audit_log_days)
    ).isoformat()
    followup_cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=follow_up_days)
    ).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?",
            (audit_cutoff,)
        )
        deleted_audit = cursor.rowcount

        cursor = await db.execute(
            """DELETE FROM follow_ups
               WHERE status IN ('resolved', 'ignored', 'left_server')
               AND flagged_at < ?""",
            (followup_cutoff,)
        )
        deleted_followups = cursor.rowcount

        await db.commit()

    return {"audit_log": deleted_audit, "follow_ups": deleted_followups}


# ──────────────────────────────────────────────
# RSS Watchlist
# ──────────────────────────────────────────────

async def add_to_watchlist(user_id: str, guild_id: str, added_by: str):
    """Upsert a user into the RSS watchlist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO rss_watchlist (user_id, guild_id, added_by)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET added_by=excluded.added_by, added_at=CURRENT_TIMESTAMP""",
            (user_id, guild_id, added_by)
        )
        await db.commit()


async def remove_from_watchlist(user_id: str):
    """Remove a user from the RSS watchlist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM rss_watchlist WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_watchlist(guild_id: str) -> list[dict]:
    """Return all watched users for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM rss_watchlist WHERE guild_id = ? ORDER BY added_at ASC",
            (guild_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def is_watched(user_id: str) -> bool:
    """Return True if the user is on the watchlist."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM rss_watchlist WHERE user_id = ? LIMIT 1", (user_id,)
        )
        return await cursor.fetchone() is not None


async def log_rss_activity(user_id: str, guild_id: str, channel_id: str,
                            message_id: str, message_link: str):
    """Record a message from a watched user."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO rss_activity (user_id, guild_id, channel_id, message_id, message_link)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, guild_id, channel_id, message_id, message_link)
        )
        await db.commit()


async def get_rss_activity(user_id: str, days: int = 30) -> list[dict]:
    """Return all activity rows for a user within the last N days."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(" ")  # same format as CURRENT_TIMESTAMP rows
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM rss_activity
               WHERE user_id = ? AND sent_at >= ?
               ORDER BY sent_at DESC""",
            (user_id, cutoff)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_rss_activity_count(user_id: str, days: int = 30) -> int:
    """Return message count for a user in the last N days."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(" ")  # same format as CURRENT_TIMESTAMP rows
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM rss_activity WHERE user_id = ? AND sent_at >= ?",
            (user_id, cutoff)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_latest_rss_message(user_id: str) -> dict | None:
    """Return the most recent activity row for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM rss_activity WHERE user_id = ?
               ORDER BY sent_at DESC LIMIT 1""",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def cleanup_rss_activity(days: int = 30) -> int:
    """Delete rss_activity rows older than N days. Returns count deleted."""
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat(" ")  # same format as CURRENT_TIMESTAMP rows
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM rss_activity WHERE sent_at < ?", (cutoff,)
        )
        await db.commit()
        return cursor.rowcount


# ──────────────────────────────────────────────
# RSS/Atom feeds (/feed)
# ──────────────────────────────────────────────

async def add_feed(guild_id: str, channel_id: str, url: str, title: str,
                   role_id: str | None, added_by: str) -> int:
    """Insert a feed. Raises sqlite3.IntegrityError if the URL already posts to that channel."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO feeds (guild_id, channel_id, url, title, role_id, added_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, url, title, role_id, added_by)
        )
        await db.commit()
        return cursor.lastrowid


async def get_feeds(guild_id: str | None = None) -> list[dict]:
    """Return all feeds, or one guild's feeds."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if guild_id is None:
            cursor = await db.execute("SELECT * FROM feeds ORDER BY id")
        else:
            cursor = await db.execute("SELECT * FROM feeds WHERE guild_id = ? ORDER BY id", (guild_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_feed(feed_id: int, guild_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM feeds WHERE id = ? AND guild_id = ?", (feed_id, guild_id)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def remove_feed(feed_id: int, guild_id: str) -> bool:
    """Delete a feed and its posted-item history. Returns False if it didn't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM feeds WHERE id = ? AND guild_id = ?", (feed_id, guild_id)
        )
        if cursor.rowcount:
            await db.execute("DELETE FROM feed_items WHERE feed_id = ?", (feed_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_seen_feed_items(feed_id: int) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT item_key FROM feed_items WHERE feed_id = ?", (feed_id,))
        return {row[0] for row in await cursor.fetchall()}


async def mark_feed_items_seen(feed_id: int, keys: list[str], forget_after_days: int = 90):
    """Remember items as posted (or deliberately skipped). Items still in the
    feed are refreshed on every check; ones that left it are forgotten after
    `forget_after_days` so the table doesn't grow forever."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO feed_items (feed_id, item_key) VALUES (?, ?)
               ON CONFLICT(feed_id, item_key) DO UPDATE SET seen_at = CURRENT_TIMESTAMP""",
            [(feed_id, key) for key in keys]
        )
        await db.execute(
            "DELETE FROM feed_items WHERE feed_id = ? AND seen_at < datetime('now', ?)",
            (feed_id, f"-{forget_after_days} days")
        )
        await db.commit()


async def record_feed_check(feed_id: int, error: str | None, etag: str | None = None,
                            last_modified: str | None = None, title: str | None = None) -> int:
    """Store a check's outcome. Success resets the failure count and saves the
    caching headers; an error increments it. Returns the new failure count."""
    async with aiosqlite.connect(DB_PATH) as db:
        if error is None:
            await db.execute(
                """UPDATE feeds SET last_checked = CURRENT_TIMESTAMP, last_error = NULL, failures = 0,
                       etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),
                       title = COALESCE(NULLIF(?, ''), title)
                   WHERE id = ?""",
                (etag, last_modified, title, feed_id)
            )
        else:
            await db.execute(
                """UPDATE feeds SET last_checked = CURRENT_TIMESTAMP, last_error = ?, failures = failures + 1
                   WHERE id = ?""",
                (error, feed_id)
            )
        await db.commit()
        cursor = await db.execute("SELECT failures FROM feeds WHERE id = ?", (feed_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


FEED_SETTINGS = ("channel_id", "role_id", "include_words", "exclude_words",
                 "highlight_words", "summary_after", "author_id", "activity_exempt")


async def update_feed(feed_id: int, guild_id: str, **settings) -> bool:
    """Change a feed's settings (any of FEED_SETTINGS; None clears a text
    setting). Returns False if the feed doesn't exist."""
    unknown = set(settings) - set(FEED_SETTINGS)
    if unknown:
        raise ValueError(f"not feed settings: {unknown}")
    if not settings:
        return await get_feed(feed_id, guild_id) is not None
    assignments = ", ".join(f"{name} = ?" for name in settings)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE feeds SET {assignments} WHERE id = ? AND guild_id = ?",
            (*settings.values(), feed_id, guild_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def archive_feed_posts(rows: list[tuple]):
    """rows: (feed_id, feed_title, title, link, message_url)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT INTO feed_posts (feed_id, feed_title, title, link, message_url)
               VALUES (?, ?, ?, ?, ?)""",
            rows
        )
        await db.commit()


async def search_feed_posts(words: list[str], limit: int = 10) -> list[dict]:
    """Newest posted items whose title or feed name contains every word."""
    if not words:
        return []
    conditions = " AND ".join(
        "(title LIKE ? ESCAPE '\\' OR feed_title LIKE ? ESCAPE '\\')" for _ in words)
    params = []
    for word in words:
        pattern = "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params += [pattern, pattern]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM feed_posts WHERE {conditions} ORDER BY posted_at DESC, id DESC LIMIT ?",
            (*params, limit)
        )
        return [dict(r) for r in await cursor.fetchall()]


async def feed_url_tracked(guild_id: str, url: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM feeds WHERE guild_id = ? AND url = ? LIMIT 1", (guild_id, url)
        )
        return await cursor.fetchone() is not None


# ──────────────────────────────────────────────
# Feed suggestions (/feed suggest)
# ──────────────────────────────────────────────

async def add_suggestion(guild_id: str, url: str, title: str, suggested_by: str,
                         own_blog: bool, note: str | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO feed_suggestions (guild_id, url, title, suggested_by, own_blog, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, url, title, suggested_by, int(own_blog), note)
        )
        await db.commit()
        return cursor.lastrowid


async def set_suggestion_message(suggestion_id: int, message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE feed_suggestions SET message_id = ? WHERE id = ?", (message_id, suggestion_id)
        )
        await db.commit()


async def get_suggestion(suggestion_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM feed_suggestions WHERE id = ?", (suggestion_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def pending_suggestions(guild_id: str, url: str | None = None,
                              suggested_by: str | None = None) -> list[dict]:
    query, params = "SELECT * FROM feed_suggestions WHERE guild_id = ? AND status = 'pending'", [guild_id]
    if url:
        query += " AND url = ?"
        params.append(url)
    if suggested_by:
        query += " AND suggested_by = ?"
        params.append(suggested_by)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query + " ORDER BY id", params)
        return [dict(r) for r in await cursor.fetchall()]


async def decide_suggestion(suggestion_id: int, status: str, decided_by: str | None,
                            reason: str | None = None) -> bool:
    """Move a suggestion out of (or, with status 'pending', back into) the
    queue. Deciding only succeeds while it's pending, so two mods clicking at
    once can't both act on it. Returns whether this call changed it."""
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "pending":
            cursor = await db.execute(
                """UPDATE feed_suggestions SET status = 'pending', decided_by = NULL, reason = NULL,
                       decided_at = NULL WHERE id = ?""", (suggestion_id,))
        else:
            cursor = await db.execute(
                """UPDATE feed_suggestions SET status = ?, decided_by = ?, reason = ?,
                       decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'""",
                (status, decided_by, reason, suggestion_id))
        await db.commit()
        return cursor.rowcount > 0



# ──────────────────────────────────────────────
# Settings and the feed activity rule
# ──────────────────────────────────────────────

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        await db.commit()


async def get_watch_entry(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM rss_watchlist WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_author_state(user_id: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT state FROM feed_author_state WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_author_state(user_id: str, state: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO feed_author_state (user_id, state) VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, changed_at = CURRENT_TIMESTAMP""",
            (user_id, state)
        )
        await db.commit()



async def roundup_posts(channel_id: str, days: int = 7) -> list[dict]:
    """What the feeds posting in channel_id posted in the last N days, oldest
    first, with each feed's author."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT p.title, p.link, p.feed_title, p.posted_at, f.id AS feed_id, f.author_id
               FROM feed_posts p JOIN feeds f ON f.id = p.feed_id
               WHERE f.channel_id = ? AND p.posted_at >= datetime('now', ?)
               ORDER BY p.posted_at, p.id""",
            (channel_id, f"-{days} days")
        )
        return [dict(r) for r in await cursor.fetchall()]
