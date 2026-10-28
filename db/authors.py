# ============================================================
# db/authors.py - Blog authors: the profile links shown under their posts,
# and where they stand with the activity rule.
# ============================================================

from .connection import connect, execute, fetch_all, fetch_value

SCHEMA = """
    -- Last activity-rule state per feed author ('active', 'warned',
    -- 'paused'), so each change is announced once.
    CREATE TABLE IF NOT EXISTS feed_author_state (
        user_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Authors' LinkedIn, X, YouTube and GitHub, shown under their posts
    CREATE TABLE IF NOT EXISTS author_links (
        author_id TEXT NOT NULL,
        site TEXT NOT NULL,                       -- linkedin / x / youtube / github
        url TEXT NOT NULL,
        PRIMARY KEY (author_id, site)
    );
"""


async def get_author_links(author_id: str) -> dict[str, str]:
    """An author's profile links, as {site: url}."""
    rows = await fetch_all("SELECT site, url FROM author_links WHERE author_id = ?", (author_id,))
    return {row["site"]: row["url"] for row in rows}


async def set_author_links(author_id: str, links: dict[str, str | None]):
    """Save some of an author's links; a None url removes that one."""
    async with connect() as conn:
        for site, url in links.items():
            if url is None:
                await conn.execute("DELETE FROM author_links WHERE author_id = ? AND site = ?", (author_id, site))
            else:
                await conn.execute(
                    """INSERT INTO author_links (author_id, site, url) VALUES (?, ?, ?)
                       ON CONFLICT(author_id, site) DO UPDATE SET url = excluded.url""",
                    (author_id, site, url))
        await conn.commit()


async def delete_author_links(author_id: str):
    await execute("DELETE FROM author_links WHERE author_id = ?", (author_id,))


async def get_author_state(user_id: str) -> str | None:
    return await fetch_value("SELECT state FROM feed_author_state WHERE user_id = ?", (user_id,))


async def set_author_state(user_id: str, state: str):
    await execute(
        """INSERT INTO feed_author_state (user_id, state) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, changed_at = CURRENT_TIMESTAMP""",
        (user_id, state)
    )
