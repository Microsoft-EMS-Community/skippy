# ============================================================
# db - Skippy's SQLite database (aiosqlite, async), one module per area.
# Everything is used as db.<function>, so callers don't need to know
# which area a table belongs to. Each area creates its own tables in
# SCHEMA and brings older databases up to date in upgrade().
# ============================================================

from . import audit, authors, feeds, followups, settings, suggestions, watchlist
from .audit import add_audit_log, get_recent_audit_log
from .authors import delete_author_links, get_author_links, get_author_state, set_author_links, set_author_state
from .connection import DB_PATH, connect
from .feeds import (FEED_SETTINGS, add_feed, archive_feed_posts, feed_item_ids, feeds_with_items, get_feed,
                    get_feeds, get_seen_feed_items, mark_feed_items_seen, record_feed_check, remove_feed,
                    roundup_posts, search_feed_posts, update_feed)
from .followups import (add_follow_up, get_avg_resolution_time, get_left_server_follow_ups, get_open_follow_ups,
                        get_overdue_follow_ups, get_server_stats, get_user_score, increment_flag_count,
                        increment_ignored_count, increment_resolved_count, mark_ignored, mark_left_server,
                        mark_warning_sent, resolve_all_open_follow_ups)
from .settings import get_setting, set_setting
from .suggestions import (add_suggestion, decide_suggestion, get_suggestion, pending_suggestions,
                          set_suggestion_message)
from .watchlist import (add_to_watchlist, cleanup_rss_activity, get_latest_rss_message, get_rss_activity,
                        get_rss_activity_count, get_watch_entry, get_watchlist, is_watched, log_rss_activity,
                        remove_from_watchlist)

AREAS = (followups, audit, watchlist, feeds, suggestions, settings, authors)


async def init_db():
    """Create every table that's missing, and bring older databases up to date."""
    async with connect() as conn:
        for area in AREAS:
            await conn.executescript(area.SCHEMA)
            if hasattr(area, "upgrade"):
                await area.upgrade(conn)
        await conn.commit()


async def cleanup_old_records(audit_log_days: int = 90, follow_up_days: int = 180) -> dict:
    """
    Prune old records to keep the DB lean.
    - audit_log: delete entries older than audit_log_days
    - follow_ups: delete resolved/ignored rows older than follow_up_days
    - user_scores: never touched (reputation data kept forever)
    Returns counts of deleted rows.
    """
    return {"audit_log": await audit.delete_old_audit_log(audit_log_days),
            "follow_ups": await followups.delete_closed_follow_ups(follow_up_days)}
