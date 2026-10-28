# ============================================================
# db/feeds.py - The feeds Skippy follows, the items each has seen, and
# everything posted from them (for /blog search and the roundup).
# ============================================================

from .connection import add_missing_columns, connect, execute, execute_many, fetch_all, fetch_one

SCHEMA = """
    CREATE TABLE IF NOT EXISTS feeds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        role_id TEXT,                             -- unused: feeds no longer ping a role
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
    CREATE INDEX IF NOT EXISTS feed_items_item_key ON feed_items (item_key);
"""

FEED_SETTINGS = ("url", "channel_id", "include_words", "exclude_words",
                 "highlight_words", "summary_after", "author_id", "activity_exempt", "display_name")


async def upgrade(conn):
    """Columns added after the feeds table first shipped."""
    await add_missing_columns(conn, "feeds", {
        "include_words": "TEXT",
        "exclude_words": "TEXT",
        "highlight_words": "TEXT",
        "summary_after": "INTEGER NOT NULL DEFAULT 5",
        "author_id": "TEXT",
        "activity_exempt": "INTEGER NOT NULL DEFAULT 0",
        "display_name": "TEXT",
    })


async def add_feed(guild_id: str, channel_id: str, url: str, title: str, added_by: str) -> int:
    """Insert a feed. Raises sqlite3.IntegrityError if the URL already posts to that channel."""
    cursor = await execute(
        """INSERT INTO feeds (guild_id, channel_id, url, title, added_by)
           VALUES (?, ?, ?, ?, ?)""",
        (guild_id, channel_id, url, title, added_by)
    )
    return cursor.lastrowid


async def get_feeds(guild_id: str | None = None) -> list[dict]:
    """Return all feeds, or one guild's feeds."""
    if guild_id is None:
        return await fetch_all("SELECT * FROM feeds ORDER BY id")
    return await fetch_all("SELECT * FROM feeds WHERE guild_id = ? ORDER BY id", (guild_id,))


async def get_feed(feed_id: int, guild_id: str) -> dict | None:
    return await fetch_one("SELECT * FROM feeds WHERE id = ? AND guild_id = ?", (feed_id, guild_id))


async def remove_feed(feed_id: int, guild_id: str) -> bool:
    """Delete a feed and its posted-item history. Returns False if it didn't exist."""
    async with connect() as conn:
        cursor = await conn.execute("DELETE FROM feeds WHERE id = ? AND guild_id = ?", (feed_id, guild_id))
        if cursor.rowcount:
            await conn.execute("DELETE FROM feed_items WHERE feed_id = ?", (feed_id,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_seen_feed_items(feed_id: int) -> set[str]:
    return {row["item_key"] for row in await fetch_all("SELECT item_key FROM feed_items WHERE feed_id = ?", (feed_id,))}


async def mark_feed_items_seen(feed_id: int, keys: list[str], forget_after_days: int = 90):
    """Remember items as posted (or deliberately skipped). Items still in the
    feed are refreshed on every check; ones that left it are forgotten after
    `forget_after_days` so the table doesn't grow forever."""
    async with connect() as conn:
        await conn.executemany(
            """INSERT INTO feed_items (feed_id, item_key) VALUES (?, ?)
               ON CONFLICT(feed_id, item_key) DO UPDATE SET seen_at = CURRENT_TIMESTAMP""",
            [(feed_id, key) for key in keys]
        )
        await conn.execute(
            "DELETE FROM feed_items WHERE feed_id = ? AND seen_at < datetime('now', ?)",
            (feed_id, f"-{forget_after_days} days")
        )
        await conn.commit()


async def record_feed_check(feed_id: int, error: str | None, etag: str | None = None,
                            last_modified: str | None = None, title: str | None = None) -> int:
    """Store a check's outcome. Success resets the failure count and saves the
    caching headers; an error increments it. Returns the new failure count."""
    async with connect() as conn:
        if error is None:
            await conn.execute(
                """UPDATE feeds SET last_checked = CURRENT_TIMESTAMP, last_error = NULL, failures = 0,
                       etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),
                       title = COALESCE(NULLIF(?, ''), title)
                   WHERE id = ?""",
                (etag, last_modified, title, feed_id)
            )
        else:
            await conn.execute(
                """UPDATE feeds SET last_checked = CURRENT_TIMESTAMP, last_error = ?, failures = failures + 1
                   WHERE id = ?""",
                (error, feed_id)
            )
        await conn.commit()
        cursor = await conn.execute("SELECT failures FROM feeds WHERE id = ?", (feed_id,))
        row = await cursor.fetchone()
        return row[0] if row else 0


async def update_feed(feed_id: int, guild_id: str, **settings) -> bool:
    """Change a feed's settings (any of FEED_SETTINGS; None clears a text
    setting). Returns False if the feed doesn't exist."""
    unknown = set(settings) - set(FEED_SETTINGS)
    if unknown:
        raise ValueError(f"not feed settings: {unknown}")
    if not settings:
        return await get_feed(feed_id, guild_id) is not None
    if "url" in settings:  # the old address's caching headers mean nothing to the new one
        settings = {**settings, "etag": None, "last_modified": None}
    assignments = ", ".join(f"{name} = ?" for name in settings)
    cursor = await execute(f"UPDATE feeds SET {assignments} WHERE id = ? AND guild_id = ?",
                           (*settings.values(), feed_id, guild_id))
    return cursor.rowcount > 0


async def archive_feed_posts(rows: list[tuple]):
    """rows: (feed_id, feed_title, title, link, message_url)."""
    await execute_many(
        """INSERT INTO feed_posts (feed_id, feed_title, title, link, message_url)
           VALUES (?, ?, ?, ?, ?)""",
        rows
    )


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
    return await fetch_all(
        f"SELECT * FROM feed_posts WHERE {conditions} ORDER BY posted_at DESC, id DESC LIMIT ?",
        (*params, limit)
    )


async def feeds_with_items(ids: set[str]) -> set[int]:
    """The feeds that already have any of these items (feed_reader.item_ids),
    as an item they've seen or a link they've posted."""
    ids, found = list(ids), set()
    async with connect() as conn:
        for start in range(0, len(ids), 400):  # stay well under SQLite's limit on query parameters
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            cursor = await conn.execute(
                f"""SELECT feed_id FROM feed_items WHERE item_key IN ({marks})
                    UNION SELECT feed_id FROM feed_posts WHERE link IN ({marks})""", chunk + chunk)
            found.update(row[0] for row in await cursor.fetchall())
    return found


async def feed_item_ids(feed_id: int) -> set[str]:
    """What identifies a stored feed's items: seen item keys and posted links."""
    rows = await fetch_all(
        "SELECT item_key FROM feed_items WHERE feed_id = ? UNION SELECT link FROM feed_posts WHERE feed_id = ?",
        (feed_id, feed_id))
    return {row["item_key"] for row in rows}


async def roundup_posts(channel_id: str, days: int = 7) -> list[dict]:
    """What the feeds posting in channel_id posted in the last N days, oldest
    first, with each feed's author."""
    return await fetch_all(
        """SELECT p.title, p.link, p.feed_title, p.posted_at, p.message_url, f.id AS feed_id, f.author_id
           FROM feed_posts p JOIN feeds f ON f.id = p.feed_id
           WHERE f.channel_id = ? AND p.posted_at >= datetime('now', ?)
           ORDER BY p.posted_at, p.id""",
        (channel_id, f"-{days} days")
    )
