# ============================================================
# db/audit.py - The audit log: what Skippy and its users did (/audit).
# ============================================================

from datetime import timedelta

from .connection import execute, fetch_all, utc_now

SCHEMA = """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        actor_id TEXT,
        target_id TEXT,
        detail TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
"""


async def add_audit_log(action: str, actor_id: str | None,
                        target_id: str | None, detail: str | None):
    """Append an entry to the audit log."""
    await execute(
        """INSERT INTO audit_log (action, actor_id, target_id, detail)
           VALUES (?, ?, ?, ?)""",
        (action, actor_id, target_id, detail)
    )


async def get_recent_audit_log(limit: int = 10) -> list[dict]:
    """Return the most recent `limit` audit log entries."""
    return await fetch_all("SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,))


async def delete_old_audit_log(older_than_days: int) -> int:
    """Delete entries older than N days. Returns how many."""
    cutoff = (utc_now() - timedelta(days=older_than_days)).isoformat()
    cursor = await execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
    return cursor.rowcount
