# ============================================================
# feed_activity.py - The activity rule: an author's blog posts while they
# take part in the server. Mods phase it in with /feed activity: off →
# watch → warn (a reminder) → enforce (new posts pause until they're back).
# Activity comes from the RSS watchlist (rss_watch.py).
# ============================================================

from datetime import datetime, timedelta, timezone

import db
import feed_notices
from feed_registry import shown_name

ACTIVITY_DAYS = 30  # activity window, and the grace period for newly linked authors
BADGES = {"active": "🟢", "new": "🆕", "inactive": "🔴", "untracked": "⚪"}


async def author_status(author_id: str, min_messages: int) -> tuple[str, int]:
    """("active" | "new" | "inactive" | "untracked", messages in the window).
    Activity comes from the RSS watchlist, which only counts messages since
    the member was added, so newly added authors get a grace period."""
    entry = await db.get_watch_entry(author_id)
    if entry is None:
        return "untracked", 0  # a mod took them off the watchlist; nothing to judge by
    count = await db.get_rss_activity_count(author_id, days=ACTIVITY_DAYS)
    if count >= min_messages:
        return "active", count
    added = datetime.fromisoformat(entry["added_at"]).replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - added < timedelta(days=ACTIVITY_DAYS):
        return "new", count
    return "inactive", count


async def current_rule() -> tuple[str, int]:
    """(mode, messages needed in the window to count as active)."""
    return (await db.get_setting("feed_activity_mode", "off"),
            int(await db.get_setting("feed_activity_min_messages", "1")))


async def holds_back(bot, feed: dict) -> bool:
    """Apply the activity rule to a feed's author, announcing any change
    once. Returns True when the author's new posts should be held back."""
    if not feed["author_id"] or feed["activity_exempt"]:
        return False
    mode, min_messages = await current_rule()
    if mode not in ("warn", "enforce"):
        return False
    status, count = await author_status(feed["author_id"], min_messages)
    if status == "inactive":
        state = "paused" if mode == "enforce" else "warned"
    else:
        state = "active"
    previous = await db.get_author_state(feed["author_id"]) or "active"
    if state != previous:
        await db.set_author_state(feed["author_id"], state)
        await _announce(bot, feed, previous, state, count, min_messages)
    return state == "paused"


async def _announce(bot, feed: dict, previous: str, state: str, count: int, min_messages: int):
    """Tell the mods, and the author where it concerns them, that their state changed."""
    author, name = f"<@{feed['author_id']}>", shown_name(feed)
    guild = bot.get_guild(int(feed["guild_id"]))
    server = guild.name if guild else "the server"
    bar = f"{count} message(s) in {ACTIVITY_DAYS} days; active means {min_messages}+"
    if state == "paused":
        await feed_notices.alert(bot, f"⏸️ {author} is inactive ({bar}), so new posts from **{name}** are paused "
                                      "until they're active again.")
        await feed_notices.dm(bot, feed["author_id"],
                              f"⏸️ Hi! New posts from your blog **{name}** are paused in the {server} content tracker, "
                              f"because you haven't been active in the server lately. As soon as you're back in the "
                              f"conversation ({min_messages}+ message(s) in {ACTIVITY_DAYS} days), your posts are "
                              "shared again automatically.")
    elif state == "warned" and previous == "active":
        await feed_notices.alert(bot, f"📉 {author} is inactive ({bar}). **{name}** still posts; they got a friendly reminder.")
        await feed_notices.dm(bot, feed["author_id"],
                              f"👋 Hi! Your blog **{name}** is part of the {server} content tracker, and we'd love to "
                              "see you around the server too. Blogs in the tracker are for active members, so if you "
                              "stay quiet, new posts may get paused. Joining a conversation now and then is all it takes.")
    elif state == "warned":  # paused -> warned: mods switched enforce off
        await feed_notices.alert(bot, f"▶️ **{name}** by {author} posts again: the activity rule is back to reminders only.")
    elif previous == "paused":
        await feed_notices.alert(bot, f"▶️ {author} is active again, so **{name}** posts again.")
        await feed_notices.dm(bot, feed["author_id"],
                              f"▶️ Welcome back! Posts from your blog **{name}** are being shared in {server} again.")
    else:
        await feed_notices.alert(bot, f"📈 {author} is active again.")
