# ============================================================
# feed_poller.py - Checks every feed every FEED_POLL_MINUTES and posts
# what's new; reports feeds that keep failing, and blogs whose links all
# changed at once. Reading feeds is feed_reader.py's job, posting is
# feed_posting.py's.
# ============================================================

import asyncio
import logging
import time

from discord.ext import commands, tasks

import db
import feed_activity
import feed_notices
import feed_posting
import feed_reader
from config import FEED_POLL_MINUTES
from feed_reader import FeedError, RateLimited
from feed_registry import feed_name, shown_name

logger = logging.getLogger("followup-bot")

ALERT_AFTER_FAILURES = max(1, 60 // FEED_POLL_MINUTES)  # an hour of failed checks
CONCURRENT_CHECKS = 5     # feeds checked at once, so slow or dead ones can't hold up the rest
MOVED_MIN_ITEMS = 3       # feeds this big are never all new at once (one listing only its latest post always is)


async def record_success(bot, feed: dict, result=None, title: str | None = None):
    """Save a working check; the mods hear it if the feed had been failing."""
    await db.record_feed_check(feed["id"], None, result and result.etag,
                               result and result.last_modified, title)
    if feed["failures"] >= ALERT_AFTER_FAILURES:
        await feed_notices.alert(bot, f"✅ Feed `#{feed['id']}` **{shown_name(feed)}** is working again.")


class FeedPoller(commands.Cog):
    """The loop that checks feeds and posts their new items."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.skip_until: dict[int, float] = {}  # feed id -> monotonic time a site asked us to wait until
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    @tasks.loop(minutes=FEED_POLL_MINUTES)
    async def poll(self):
        # Any exception escaping a tasks.loop stops it for good, so nothing may.
        try:
            feeds = await db.get_feeds()
            if not feeds:
                return
            limit = asyncio.Semaphore(CONCURRENT_CHECKS)

            async def check(http, feed):
                async with limit:
                    try:
                        await self.check(http, feed)
                    except Exception:
                        logger.exception("Feed #%s: check failed", feed["id"])

            async with feed_reader.session() as http:
                await asyncio.gather(*(check(http, feed) for feed in feeds))
        except Exception:
            logger.exception("Feed poll failed")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def check(self, http, feed: dict):
        """Fetch one feed and post what's new in it."""
        if time.monotonic() < self.skip_until.get(feed["id"], 0):
            return  # the site asked us to wait (HTTP 429 / Retry-After)
        held = await feed_activity.holds_back(self.bot, feed)
        channel = self.bot.get_channel(int(feed["channel_id"]))
        try:
            if channel is None:
                raise FeedError("its channel no longer exists")
            result = await feed_reader.fetch_feed(http, feed["url"], feed["etag"], feed["last_modified"])
            if result.data is None:  # 304 Not Modified
                await record_success(self.bot, feed)
                return
            title, items = feed_reader.parse_feed(result.data, feed["url"])
        except FeedError as e:
            if isinstance(e, RateLimited):
                self.skip_until[feed["id"]] = time.monotonic() + e.retry_after
            failures = await db.record_feed_check(feed["id"], str(e))
            if failures == ALERT_AFTER_FAILURES:
                await feed_notices.alert(self.bot, f"⚠️ Feed `#{feed['id']}` **{shown_name(feed)}** has failed "
                                                   f"{failures} checks in a row: {e}")
            return

        seen = await db.get_seen_feed_items(feed["id"])
        if seen and len(items) >= MOVED_MIN_ITEMS and not any(i.key in seen for i in items):
            await self._moved(feed, result, title, items)
            return
        new = feed_reader.select_new(items, seen, len(items))
        include = feed_reader.word_list(feed["include_words"])
        exclude = feed_reader.word_list(feed["exclude_words"])
        # While the author's posts are paused, new items are skipped, not
        # saved up: coming back doesn't flood the channel with old posts.
        to_post = [] if held else [i for i in new if feed_reader.wanted(i, include, exclude)]
        posted = await feed_posting.post_items(channel, feed, feed_name(feed, title), to_post)
        # Filtered-out items are marked seen (skipped for good). Wanted items
        # that didn't make it to Discord stay unseen, so the next check retries.
        unposted = {i.key for i in to_post} - posted
        await db.mark_feed_items_seen(feed["id"], [i.key for i in items if i.key not in unposted])
        await record_success(self.bot, feed, result, title)
        if new:
            logger.info("Feed #%s: %d new, %d filtered out, %d posted",
                        feed["id"], len(new), len(new) - len(to_post), len(posted))

    async def _moved(self, feed: dict, result, title: str, items: list):
        """Every post in the feed looking new at once isn't a blog publishing
        its whole history: its links changed (a new domain, or a site rebuilt
        with another address), so they're the same posts. They count as seen
        and nothing posts; the mods hear why, in case one really is new."""
        await db.mark_feed_items_seen(feed["id"], [i.key for i in items])
        await record_success(self.bot, feed, result, title)
        logger.warning("Feed #%s: all %d posts have new links; taken as moved, none posted", feed["id"], len(items))
        example = next((f" (like <{i.link}>)" for i in items if i.link), "")
        await feed_notices.alert(self.bot, f"🔀 Feed `#{feed['id']}` **{shown_name(feed)}**: all {len(items)} posts "
                                           f"in it have new links now{example}, so I took it that the blog moved and "
                                           f"reposted none of them. If the newest one really is new, "
                                           f"`/feed test feed:{feed['id']}` posts it.")


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedPoller(bot))
