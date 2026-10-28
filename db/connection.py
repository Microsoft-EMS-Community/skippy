# ============================================================
# db/connection.py - The one way the db package talks to SQLite: open a
# connection, run a statement, fetch rows as dicts.
# ============================================================

from datetime import datetime, timezone

import aiosqlite

from config import DB_PATH


def connect() -> aiosqlite.Connection:
    """A connection to Skippy's database, for anything the helpers below don't cover."""
    return aiosqlite.connect(DB_PATH)


async def execute(sql: str, params=()) -> aiosqlite.Cursor:
    """Run one statement and commit. The cursor still has rowcount and lastrowid."""
    async with connect() as conn:
        cursor = await conn.execute(sql, params)
        await conn.commit()
        return cursor


async def execute_many(sql: str, rows: list) -> None:
    async with connect() as conn:
        await conn.executemany(sql, rows)
        await conn.commit()


async def fetch_all(sql: str, params=()) -> list[dict]:
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]


async def fetch_one(sql: str, params=()) -> dict | None:
    async with connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def fetch_value(sql: str, params=(), default=None):
    """The first column of the first row, or default when there's no row."""
    async with connect() as conn:
        cursor = await conn.execute(sql, params)
        row = await cursor.fetchone()
        return row[0] if row else default


def utc_now() -> datetime:
    """Now in UTC, without a timezone, as follow-up timestamps are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def add_missing_columns(conn: aiosqlite.Connection, table: str, columns: dict[str, str]):
    """Add columns that came after a table first shipped to databases created before them."""
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cursor.fetchall()}
    for name, definition in columns.items():
        if name not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
