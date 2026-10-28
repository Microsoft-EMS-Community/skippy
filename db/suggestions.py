# ============================================================
# db/suggestions.py - Blogs members suggested with /blog suggest, waiting
# for the mods or decided.
# ============================================================

import json

from .connection import add_missing_columns, connect, execute, fetch_all, fetch_one

SCHEMA = """
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
"""


async def upgrade(conn):
    """Suggestions name the blog's author; older ones only said whether it
    was the suggester's own blog."""
    await add_missing_columns(conn, "feed_suggestions", {"author_id": "TEXT", "links": "TEXT"})
    await conn.execute("UPDATE feed_suggestions SET author_id = suggested_by "
                       "WHERE author_id IS NULL AND own_blog = 1")


async def add_suggestion(guild_id: str, url: str, title: str, suggested_by: str,
                         author_id: str, note: str | None, links: dict[str, str] | None = None) -> int:
    """Queue a suggestion. `links` are the author's profile links, saved when it's approved."""
    cursor = await execute(
        """INSERT INTO feed_suggestions (guild_id, url, title, suggested_by, author_id, own_blog, note, links)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, url, title, suggested_by, author_id, int(author_id == suggested_by), note,
         json.dumps(links) if links else None)
    )
    return cursor.lastrowid


async def set_suggestion_message(suggestion_id: int, message_id: str):
    await execute("UPDATE feed_suggestions SET message_id = ? WHERE id = ?", (message_id, suggestion_id))


async def get_suggestion(suggestion_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM feed_suggestions WHERE id = ?", (suggestion_id,))


async def pending_suggestions(guild_id: str, url: str | None = None,
                              suggested_by: str | None = None) -> list[dict]:
    query, params = "SELECT * FROM feed_suggestions WHERE guild_id = ? AND status = 'pending'", [guild_id]
    if url:
        query += " AND url = ?"
        params.append(url)
    if suggested_by:
        query += " AND suggested_by = ?"
        params.append(suggested_by)
    return await fetch_all(query + " ORDER BY id", params)


async def decide_suggestion(suggestion_id: int, status: str, decided_by: str | None,
                            reason: str | None = None) -> bool:
    """Move a suggestion out of (or, with status 'pending', back into) the
    queue. Deciding only succeeds while it's pending, so two mods clicking at
    once can't both act on it. Returns whether this call changed it."""
    async with connect() as conn:
        if status == "pending":
            cursor = await conn.execute(
                """UPDATE feed_suggestions SET status = 'pending', decided_by = NULL, reason = NULL,
                       decided_at = NULL WHERE id = ?""", (suggestion_id,))
        else:
            cursor = await conn.execute(
                """UPDATE feed_suggestions SET status = ?, decided_by = ?, reason = ?,
                       decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'""",
                (status, decided_by, reason, suggestion_id))
        await conn.commit()
        return cursor.rowcount > 0
