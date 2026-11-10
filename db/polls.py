# ============================================================
# db/polls.py - Anonymous polls (/poll). A vote is stored under a scrambled
# code instead of the member's ID, and the codes are deleted when the poll
# closes: only the totals stay.
# ============================================================

import json

from .connection import connect, execute, fetch_all, fetch_one, utc_now

SCHEMA = """
    CREATE TABLE IF NOT EXISTS polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        message_id TEXT,
        question TEXT NOT NULL,
        options TEXT NOT NULL,                    -- JSON list of the choices
        role_id TEXT,                             -- only this role votes; NULL = everyone
        created_by TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        closes_at TIMESTAMP NOT NULL,
        closed_at TIMESTAMP,
        secret TEXT,                              -- scrambles voters' IDs; erased at close
        results TEXT,                             -- JSON list of votes per choice, set at close
        absent INTEGER                            -- role members who didn't vote, set at close
    );

    CREATE TABLE IF NOT EXISTS poll_votes (
        poll_id INTEGER NOT NULL,
        voter TEXT NOT NULL,                      -- scrambled code, never a member ID
        choice INTEGER NOT NULL,
        PRIMARY KEY (poll_id, voter)
    );
"""


def _poll(row: dict | None) -> dict | None:
    if row:
        row["options"] = json.loads(row["options"])
        row["results"] = json.loads(row["results"]) if row["results"] else None
    return row


async def add_poll(guild_id: str, channel_id: str, question: str, options: list[str], role_id: str | None,
                   created_by: str, closes_at: str, secret: str) -> int:
    cursor = await execute(
        """INSERT INTO polls (guild_id, channel_id, question, options, role_id, created_by, closes_at, secret)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, channel_id, question, json.dumps(options), role_id, created_by, closes_at, secret)
    )
    return cursor.lastrowid


async def set_poll_message(poll_id: int, message_id: str):
    await execute("UPDATE polls SET message_id = ? WHERE id = ?", (message_id, poll_id))


async def delete_poll(poll_id: int):
    """Remove a poll that never got posted."""
    await execute("DELETE FROM polls WHERE id = ?", (poll_id,))


async def get_poll(poll_id: int) -> dict | None:
    return _poll(await fetch_one("SELECT * FROM polls WHERE id = ?", (poll_id,)))


async def open_polls(guild_id: str | None = None, due_by: str | None = None) -> list[dict]:
    """Polls still open: a server's, or everywhere the ones that should have closed by `due_by`."""
    query, params = "SELECT * FROM polls WHERE closed_at IS NULL", []
    if guild_id:
        query += " AND guild_id = ?"
        params.append(guild_id)
    if due_by:
        query += " AND closes_at <= ?"
        params.append(due_by)
    return [_poll(row) for row in await fetch_all(query + " ORDER BY id", params)]


async def cast_poll_vote(poll_id: int, voter: str, choice: int) -> bool:
    """Save a vote, replacing the voter's earlier one. False when the poll has closed."""
    async with connect() as conn:
        cursor = await conn.execute(
            """INSERT INTO poll_votes (poll_id, voter, choice)
               SELECT id, ?, ? FROM polls WHERE id = ? AND closed_at IS NULL
               ON CONFLICT(poll_id, voter) DO UPDATE SET choice = excluded.choice""",
            (voter, choice, poll_id))
        await conn.commit()
        return cursor.rowcount > 0


async def poll_voters(poll_id: int) -> set[str]:
    """The scrambled codes that have voted in an open poll."""
    return {row["voter"] for row in await fetch_all("SELECT voter FROM poll_votes WHERE poll_id = ?", (poll_id,))}


async def close_poll(poll_id: int, absent: int | None) -> list[int] | None:
    """Close a poll: keep the totals, erase who voted and the secret that
    scrambled them. Returns the votes per choice, or None if it was already closed."""
    async with connect() as conn:
        cursor = await conn.execute("SELECT options FROM polls WHERE id = ? AND closed_at IS NULL", (poll_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        results = [0] * len(json.loads(row[0]))
        cursor = await conn.execute("SELECT choice, COUNT(*) FROM poll_votes WHERE poll_id = ? GROUP BY choice",
                                    (poll_id,))
        for choice, votes in await cursor.fetchall():
            results[choice] = votes
        await conn.execute("UPDATE polls SET closed_at = ?, results = ?, absent = ?, secret = NULL WHERE id = ?",
                           (utc_now().isoformat(), json.dumps(results), absent, poll_id))
        await conn.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll_id,))
        await conn.commit()
        return results
