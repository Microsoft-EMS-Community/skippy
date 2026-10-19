# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Exercises the /feed polling logic against a throwaway database and a fake
# Discord channel. No network: fetch_feed is replaced with canned feeds,
# and posts' page previews (fetch_preview) come back empty unless a test
# gives one.

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ["SKIPPY_DB_PATH"] = os.path.join(_tmp.name, "test.db")

import aiosqlite  # noqa: E402
import discord  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import feed_reader  # noqa: E402
import feeds  # noqa: E402
from feed_reader import FeedError, FetchResult  # noqa: E402

CHANNEL_ID, ALERT_CHANNEL_ID = 111, feeds.FEED_ALERT_CHANNEL_ID
GENERAL_ID = 333  # where the roundup goes


def rss(*keys):
    """An RSS feed whose items (newest first) are the given keys."""
    items = "".join(f"<item><title>Post {k}</title><link>https://example.com/{k}</link><guid>{k}</guid></item>"
                    for k in keys)
    return FetchResult(f"<rss><channel><title>Blog</title>{items}</channel></rss>".encode(), None, None)


SKIPPY_ID = 1473839815643369686


def setUpModule():
    patcher = mock.patch.object(feed_reader, "fetch_preview", mock.AsyncMock(return_value=feed_reader.PagePreview()))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class FakeChannel:
    def __init__(self, channel_id=CHANNEL_ID):
        self.id, self.sent, self.fail_on, self.threads = channel_id, [], set(), []
        self.mention = f"<#{channel_id}>"
        self.embeds, self.votes_added, self.messages = [], [], {}
        self.thread_error, self.refuse_pictures = None, False

    async def send(self, content=None, allowed_mentions=None, embed=None, view=None):
        """Posts (a post's card, which links to it, or a summary's text) land
        in `sent`; other cards, like a suggestion's, in `embeds`."""
        if embed is not None and not embed.url:
            self.embeds.append(embed)
            return mock.Mock()
        if embed is not None and embed.url.rsplit("/", 1)[1] in self.fail_on:
            raise discord.HTTPException(mock.Mock(status=503, reason="unavailable"), "down")
        if embed is not None and embed.image.url and self.refuse_pictures:
            raise discord.HTTPException(mock.Mock(status=400, reason="Bad Request"), "Not a well formed URL")
        self.sent.append((embed or content, allowed_mentions))
        message_id = len(self.sent)

        async def create_thread(name, auto_archive_duration):
            if self.thread_error:
                raise self.thread_error
            self.threads.append(name)

        async def add_reaction(emoji):
            self.votes_added.append((message_id, emoji))

        message = mock.Mock(id=message_id, jump_url=f"https://discord.com/channels/1/{self.id}/{message_id}",
                            create_thread=create_thread, add_reaction=add_reaction, reactions=[], channel=self)
        self.messages[message_id] = message
        return message

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Message")
        return self.messages[message_id]

    def vote(self, message_id, *user_ids, emoji="👍"):
        """Give a posted message these users' reactions (Skippy's own included, as in Discord)."""
        async def users():
            for uid in (SKIPPY_ID, *user_ids):
                yield mock.Mock(id=uid)
        self.messages[message_id].reactions.append(mock.Mock(emoji=emoji, users=users))


class FeedTestCase(unittest.IsolatedAsyncioTestCase):
    """A fresh database with one feed (#1) and a fake channel."""

    async def asyncSetUp(self):
        if os.path.exists(os.environ["SKIPPY_DB_PATH"]):
            os.remove(os.environ["SKIPPY_DB_PATH"])
        await db.init_db()
        self.channel, self.alerts = FakeChannel(), FakeChannel()
        bot = mock.Mock()
        bot.get_channel = lambda cid: {CHANNEL_ID: self.channel, ALERT_CHANNEL_ID: self.alerts}.get(cid)
        self.cog = feeds.FeedCommands.__new__(feeds.FeedCommands)  # skip __init__: no poll loop
        self.cog.bot = bot
        bot.user = mock.Mock(id=SKIPPY_ID)
        self.cog.skip_until = {}
        self.cog.refusals_told = {}
        self.feed_id = await db.add_feed("1", str(CHANNEL_ID), "https://example.com/feed", "Blog", "9")
        await db.mark_feed_items_seen(self.feed_id, ["a", "b"])  # what /feed add does

    async def check(self, result):
        feed = await db.get_feed(self.feed_id, "1")
        fetch = mock.AsyncMock(side_effect=result) if isinstance(result, Exception) else mock.AsyncMock(return_value=result)
        with mock.patch.object(feed_reader, "fetch_feed", fetch):
            await self.cog._check(mock.Mock(), feed)
        return fetch

    def posted(self):
        return [card.url.rsplit("/", 1)[1] for card, _ in self.channel.sent]


class FeedPollTests(FeedTestCase):
    async def test_only_new_items_post_oldest_first_pinging_nobody(self):
        await self.check(rss("a", "b"))
        self.assertEqual(self.posted(), [])
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        card, mentions = self.channel.sent[0]
        self.assertEqual((card.author.name, card.title, card.url), ("Blog", "Post c", "https://example.com/c"))
        self.assertIsNone(card.description)  # no summary in the feed, no author to credit
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])  # nothing posts twice

    async def test_links_without_the_sites_address_still_post(self):
        hugo = b"<rss><channel><title>Blog</title><item><title>Post r</title><link>/2026/r/</link></item></channel></rss>"
        await self.check(FetchResult(hugo, None, None))
        self.assertEqual(self.channel.sent[0][0].url, "https://example.com/2026/r/")  # read against the feed's address

    async def test_burst_becomes_one_summary_with_nothing_dropped(self):
        keys = [f"n{i}" for i in range(8, 0, -1)]  # n8 newest ... n1 oldest
        await self.check(rss(*keys))
        self.assertEqual(len(self.channel.sent), 1)
        summary, mentions = self.channel.sent[0]
        self.assertTrue(summary.startswith("Blog - 8 new posts:"))
        self.assertEqual([line.split("](<")[1][:-2] for line in summary.splitlines()[1:]],
                         [f"https://example.com/n{i}" for i in range(1, 9)])  # oldest first, all 8
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)
        await self.check(rss(*keys))
        self.assertEqual(len(self.channel.sent), 1)  # not repeated

    async def test_summary_threshold_is_per_feed(self):
        await db.update_feed(self.feed_id, "1", summary_after=10)
        await self.check(rss(*[f"n{i}" for i in range(8, 0, -1)]))
        self.assertEqual(len(self.channel.sent), 8)  # under the threshold: posted one by one

    async def test_filters_skip_for_good_and_highlight_orders_summary(self):
        await db.update_feed(self.feed_id, "1", exclude_words="chromium", summary_after=1,
                             highlight_words="remote code execution")
        items = "".join(f"<item><title>{t}</title><link>https://e.com/{n}</link><guid>{n}</guid></item>"
                        for n, t in (("3", "Chromium CVE-1: Use after free"), ("2", "CVE-2 Remote Code Execution"),
                                     ("1", "CVE-3 Information Disclosure")))
        result = FetchResult(f"<rss><channel><title>MSRC</title>{items}</channel></rss>".encode(), None, None)
        await self.check(result)
        lines = self.channel.sent[0][0].splitlines()
        self.assertEqual(lines[0], "MSRC - 2 new posts:")
        self.assertIn("**CVE-2 Remote Code Execution**", lines[1])  # highlighted, first
        self.assertIn("CVE-3 Information Disclosure", lines[2])
        self.assertTrue(all("Chromium" not in line for line in lines))
        self.assertEqual(await db.get_seen_feed_items(self.feed_id) >= {"1", "2", "3"}, True)

    async def test_posts_are_archived_and_searchable(self):
        await self.check(rss("c", "a", "b"))
        found = await db.search_feed_posts(["post", "C"])
        self.assertEqual([(r["title"], r["feed_title"], r["link"]) for r in found],
                         [("Post c", "Blog", "https://example.com/c")])
        self.assertTrue(found[0]["message_url"].startswith("https://discord.com/channels/"))
        self.assertEqual(await db.search_feed_posts(["%"]), [])  # LIKE wildcards are literal

    async def test_failed_send_is_retried_next_check(self):
        self.channel.fail_on = {"d"}
        await self.check(rss("e", "d", "c"))
        self.assertEqual(self.posted(), ["c"])
        self.channel.fail_on = set()
        await self.check(rss("e", "d", "c"))
        self.assertEqual(self.posted(), ["c", "d", "e"])

    async def test_alerts_once_after_repeated_failures_then_on_recovery(self):
        for _ in range(feeds.ALERT_AFTER_FAILURES + 2):
            await self.check(FeedError("HTTP 500"))
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn(f"failed {feeds.ALERT_AFTER_FAILURES} checks in a row: HTTP 500", self.alerts.sent[0][0])
        await self.check(rss("a", "b"))
        self.assertEqual(len(self.alerts.sent), 2)
        self.assertIn("working again", self.alerts.sent[1][0])
        self.assertEqual((await db.get_feed(self.feed_id, "1"))["failures"], 0)

    async def test_missing_channel_counts_as_failure(self):
        self.cog.bot.get_channel = lambda cid: None
        await self.check(rss("c"))
        feed = await db.get_feed(self.feed_id, "1")
        self.assertEqual((feed["failures"], feed["last_error"]), (1, "its channel no longer exists"))

    async def test_remove_forgets_history(self):
        self.assertTrue(await db.remove_feed(self.feed_id, "1"))
        self.assertEqual(await db.get_seen_feed_items(self.feed_id), set())
        self.assertFalse(await db.remove_feed(self.feed_id, "1"))

    async def test_rate_limited_feed_is_left_alone_until_the_wait_is_over(self):
        await self.check(feed_reader.RateLimited(429, 600))
        fetch = await self.check(rss("c", "a", "b"))
        fetch.assert_not_awaited()
        self.assertEqual(self.posted(), [])
        self.cog.skip_until[self.feed_id] = 0  # wait over
        fetch = await self.check(rss("c", "a", "b"))
        fetch.assert_awaited_once()
        self.assertEqual(self.posted(), ["c"])

    async def test_poll_checks_every_feed_at_most_five_at_once(self):
        running, peak, checked = 0, 0, []

        async def slow_check(http, feed):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            checked.append(feed["id"])
            running -= 1

        many = [{"id": i} for i in range(12)]
        with mock.patch.object(db, "get_feeds", mock.AsyncMock(return_value=many)), \
             mock.patch.object(self.cog, "_check", slow_check):
            await feeds.FeedCommands.poll.coro(self.cog)
        self.assertEqual(sorted(checked), list(range(12)))
        self.assertEqual(peak, feeds.CONCURRENT_CHECKS)


class CommunityTests(FeedTestCase):

    async def test_author_is_credited_without_a_ping(self):
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        await self.check(rss("c", "a", "b"))
        card, mentions = self.channel.sent[0]
        self.assertEqual(card.description, "by <@77>")
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)  # mentions in a card never ping

    async def test_linking_an_author_watches_them_without_resetting_history(self):
        await db.add_to_watchlist("77", "1", "9")
        await db.log_rss_activity("77", "1", "5", "6", "https://discord.com/channels/1/5/6")
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "10")
        self.assertTrue(await db.is_watched("77"))
        self.assertEqual(await db.get_rss_activity_count("77"), 1)
        self.assertEqual((await db.get_feed(self.feed_id, "1"))["author_id"], "77")
        await feeds.FeedCommands._link_author(self.feed_id, "1", "88", "10")
        self.assertTrue(await db.is_watched("88"))

    async def test_threads_only_in_thread_channels_and_never_block_posting(self):
        await self.check(rss("c", "a", "b"))
        self.assertEqual(self.channel.threads, [])  # 111 isn't a thread channel
        with mock.patch.object(feeds, "FEED_THREAD_CHANNEL_IDS", {CHANNEL_ID}):
            await self.check(rss("d", "c", "a", "b"))
            self.assertEqual(self.channel.threads, ["Post d"])
            self.channel.thread_error = discord.HTTPException(mock.Mock(status=403, reason="no"), "Missing Permissions")
            await self.check(rss("e", "d", "c", "a", "b"))
        self.assertEqual(len(self.channel.sent), 3)  # e was still posted ...
        self.assertIn("e", await db.get_seen_feed_items(self.feed_id))  # ... and counted as posted


    async def test_add_posts_the_newest_item(self):
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        _, items = feed_reader.parse_feed(rss("new", "old").data)
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "Blog", items, self.channel))
        i = interaction()
        await feeds.FeedCommands.feed_add.callback(self.cog, i, "https://example.com/feed")
        self.assertEqual(len(self.channel.sent), 1)
        card, mentions = self.channel.sent[0]
        self.assertEqual((card.title, card.description), ("Post new", "by <@77>"))  # like any post
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)
        self.assertEqual([r["title"] for r in await db.search_feed_posts(["new"])], ["Post new"])
        self.assertIn("Posted its newest item", i.followup.send.await_args.args[0])

    async def test_add_of_an_empty_feed_posts_nothing(self):
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "Blog", [], self.channel))
        i = interaction()
        await feeds.FeedCommands.feed_add.callback(self.cog, i, "https://example.com/feed")
        self.assertEqual(self.channel.sent, [])
        self.assertIn("✅ Added", i.followup.send.await_args.args[0])


class PostCardTests(FeedTestCase):
    """Posts are one card: the page's preview picture and description when it
    has them, the feed's otherwise."""

    async def test_card_uses_the_posts_page_preview(self):
        preview = feed_reader.PagePreview("https://img.example/cover.png", "What the page says")
        with mock.patch.object(feed_reader, "fetch_preview", mock.AsyncMock(return_value=preview)) as fetch:
            await self.check(rss("c", "a", "b"))
        self.assertEqual(fetch.await_args.args[1], "https://example.com/c")
        card = self.channel.sent[0][0]
        self.assertEqual((card.description, card.image.url), ("What the page says", "https://img.example/cover.png"))

    async def test_a_picture_discord_refuses_doesnt_stop_the_post(self):
        self.channel.refuse_pictures = True
        preview = feed_reader.PagePreview("https://img.example/bad picture.png", "")
        with mock.patch.object(feed_reader, "fetch_preview", mock.AsyncMock(return_value=preview)):
            await self.check(rss("c", "a", "b"))
        self.assertEqual(self.posted(), ["c"])
        self.assertIsNone(self.channel.sent[0][0].image.url)
        self.assertIn("c", await db.get_seen_feed_items(self.feed_id))


class ActivityRuleTests(FeedTestCase):
    """Feed #1 is linked to author 77, who has been on the watchlist for 60 days."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog._dm = mock.AsyncMock()
        self.cog.bot.get_guild = lambda gid: mock.Mock(name="guild")
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        await self.age_watch("77", days=60)

    @staticmethod
    async def age_watch(user_id, days):
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE rss_watchlist SET added_at = datetime('now', ?) WHERE user_id = ?",
                            (f"-{days} days", user_id))
            await c.commit()

    async def say_something(self, days_ago=0):
        await db.log_rss_activity("77", "1", "5", "6", "https://discord.com/channels/1/5/6")
        if days_ago:
            async with aiosqlite.connect(db.DB_PATH) as c:
                await c.execute("UPDATE rss_activity SET sent_at = datetime('now', ?)", (f"-{days_ago} days",))
                await c.commit()

    async def test_status_new_active_inactive_untracked(self):
        status = feeds.FeedCommands._author_status
        self.assertEqual(await status("77", 1), ("inactive", 0))
        await self.say_something()
        self.assertEqual(await status("77", 1), ("active", 1))
        self.assertEqual(await status("77", 5), ("inactive", 1))  # bar raised to 5
        await db.add_to_watchlist("88", "1", "9")  # just linked: grace period
        self.assertEqual(await status("88", 1), ("new", 0))
        self.assertEqual(await status("99", 1), ("untracked", 0))

    async def test_boundary_day_messages_count(self):
        # Stored as "YYYY-MM-DD HH:MM:SS"; the cutoff used to be compared in
        # ISO "T" format, which dropped the oldest day of the window.
        await self.say_something(days_ago=29.9)
        self.assertEqual(await db.get_rss_activity_count("77", days=30), 1)

    async def test_off_and_watch_never_hold_or_notify(self):
        for mode in ("off", "watch"):
            await db.set_setting("feed_activity_mode", mode)
            await self.check(rss("c", "a", "b") if mode == "off" else rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        self.assertEqual((self.alerts.sent, self.cog._dm.await_count), ([], 0))

    async def test_warn_reminds_once_and_keeps_posting(self):
        await db.set_setting("feed_activity_mode", "warn")
        await self.check(rss("c", "a", "b"))
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        self.assertEqual(len(self.alerts.sent), 1)
        self.assertIn("friendly reminder", self.alerts.sent[0][0])
        self.assertEqual(self.cog._dm.await_count, 1)

    async def test_enforce_pauses_skips_and_resumes(self):
        await db.set_setting("feed_activity_mode", "enforce")
        await self.check(rss("c", "a", "b"))
        self.assertEqual(self.posted(), [])
        self.assertIn("c", await db.get_seen_feed_items(self.feed_id))  # skipped, not saved up
        self.assertIn("paused", self.alerts.sent[-1][0])
        await self.say_something()
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["d"])
        self.assertIn("active again", self.alerts.sent[-1][0])
        self.assertIn("Welcome back", self.cog._dm.await_args.args[1])
        self.assertEqual(len(self.alerts.sent), 2)

    async def test_warned_then_enforced_gets_the_pause_message(self):
        await db.set_setting("feed_activity_mode", "warn")
        await self.check(rss("a", "b"))
        await db.set_setting("feed_activity_mode", "enforce")
        await self.check(rss("c", "a", "b"))
        self.assertEqual(self.posted(), [])
        self.assertEqual(self.cog._dm.await_count, 2)
        self.assertIn("paused", self.cog._dm.await_args.args[1])

    async def test_exempt_and_new_authors_are_left_alone(self):
        await db.set_setting("feed_activity_mode", "enforce")
        await db.update_feed(self.feed_id, "1", activity_exempt=1)
        await self.check(rss("c", "a", "b"))
        await db.update_feed(self.feed_id, "1", activity_exempt=0)
        await self.age_watch("77", days=3)  # pretend just linked
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        self.assertEqual(self.alerts.sent, [])

    async def test_activity_command_sets_mode_and_lists_authors(self):
        i = interaction()
        await feeds.FeedCommands.feed_activity.callback(self.cog, i, mode="watch", min_messages=3)
        self.assertEqual(await self.cog._activity_rule(), ("watch", 3))
        embed = i.response.send_message.await_args.kwargs["embed"]
        self.assertEqual(embed.title, "Activity rule: watch")
        self.assertIn("🔴 <@77> · 0 msg · Blog", embed.description)


def post(title, author_id=None, feed_id=1, feed_title="Blog"):
    return {"title": title, "link": f"https://e.com/{title.replace(' ', '-')}", "feed_title": feed_title,
            "posted_at": "2026-10-15 10:00:00", "feed_id": feed_id, "author_id": author_id, "message_url": None}


MONDAY_9 = datetime(2026, 10, 19, 9, 0, tzinfo=feeds.ROUNDUP_TZ)


class RoundupTests(FeedTestCase):
    """#community-feed's posts are voted on there and rounded up in #general."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name, value in (("FEED_COMMUNITY_CHANNEL_ID", CHANNEL_ID), ("FEED_ROUNDUP_CHANNEL_ID", GENERAL_ID),
                            ("FEED_VOTE_EMOJI", "👍")):
            patcher = mock.patch.object(feeds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.general = FakeChannel(GENERAL_ID)
        self.cog.bot.get_channel = {CHANNEL_ID: self.channel, ALERT_CHANNEL_ID: self.alerts,
                                    GENERAL_ID: self.general}.get

    def test_build_groups_per_blogger_in_posting_order_with_a_vote_podium(self):
        posts = [post("A1", "77"), post("B1", None, 2, "Some *Blog*"), post("A2", "77"), post("C1", "99", 3)]
        for n, p in enumerate(posts):
            p["message_url"] = f"https://discord.com/channels/1/{CHANNEL_ID}/{n}"
        votes = {posts[3]["message_url"]: 5, posts[1]["message_url"]: 2, posts[0]["message_url"]: 2}
        embed = feeds.build_roundup(posts, MONDAY_9, votes)
        self.assertEqual(embed.title, "📰 Community roundup · week 42")
        lines = embed.description.splitlines()
        self.assertEqual(lines[0], "**4 new posts from 3 bloggers** in <#111> this week. Thanks for writing! 💙")
        self.assertEqual(lines[2:5], ["<@77>", "• [A1](<https://e.com/A1>)", "• [A2](<https://e.com/A2>)"])
        self.assertIn("**Some \\*Blog\\***", embed.description)  # feeds without an author by name
        self.assertEqual(embed.fields[0].name, "🏆 Community favourites this week")
        self.assertEqual(embed.fields[0].value.splitlines(), [
            "🥇 [C1](<https://e.com/C1>) by <@99> · 5 👍",
            "🥈 [A1](<https://e.com/A1>) by <@77> · 2 👍",       # tie: the earlier post first
            "🥉 [B1](<https://e.com/B1>) by **Some \\*Blog\\*** · 2 👍"])
        self.assertIn("/blog suggest", embed.footer.text)

    def test_no_votes_no_podium(self):
        embed = feeds.build_roundup([post("A1", "77"), post("C1", "99", 3)], MONDAY_9, {})
        self.assertEqual(embed.fields, [])

    def test_build_quiet_week_and_single_blogger(self):
        self.assertIsNone(feeds.build_roundup([], MONDAY_9))
        embed = feeds.build_roundup([post("Only", "77")], MONDAY_9)
        self.assertTrue(embed.description.startswith("**1 new post from 1 blogger**"))

    def test_build_stays_within_discord_limits(self):
        many = [post(f"Post number {n} " + "x" * 100, str(1000 + n % 30), n) for n in range(200)]
        embed = feeds.build_roundup(many, MONDAY_9)
        self.assertLessEqual(len(embed.description), 4096)
        self.assertRegex(embed.description, r"…and \d+ more posts\.$")

    async def test_query_is_this_channels_feeds_last_seven_days(self):
        other = await db.add_feed("1", "222", "https://other.example/feed", "Other", "9")
        await db.archive_feed_posts([(self.feed_id, "Blog", "Fresh", "https://e.com/f", None),
                                     (self.feed_id, "Blog", "Stale", "https://e.com/s", None),
                                     (other, "Other", "Elsewhere", "https://e.com/x", None)])
        async with aiosqlite.connect(db.DB_PATH) as c:
            await c.execute("UPDATE feed_posts SET posted_at = datetime('now', '-8 days') WHERE title = 'Stale'")
            await c.commit()
        self.assertEqual([p["title"] for p in await db.roundup_posts(str(CHANNEL_ID))], ["Fresh"])

    async def test_posts_monday_from_nine_once_per_week(self):
        await db.archive_feed_posts([(self.feed_id, "Blog", "Fresh", "https://e.com/f", None)])
        self.assertFalse(await self.cog._maybe_post_roundup(MONDAY_9 - timedelta(minutes=1)))
        self.assertFalse(await self.cog._maybe_post_roundup(MONDAY_9 + timedelta(days=1)))  # Tuesday
        self.assertTrue(await self.cog._maybe_post_roundup(MONDAY_9 + timedelta(minutes=3)))
        self.assertFalse(await self.cog._maybe_post_roundup(MONDAY_9 + timedelta(hours=2)))  # already out
        self.assertEqual(len(self.general.embeds), 1)
        self.assertEqual(self.channel.embeds, [])  # in #general, not in the feed channel
        self.assertTrue(await self.cog._maybe_post_roundup(MONDAY_9 + timedelta(days=7)))  # next week

    async def test_switched_off_or_quiet_posts_nothing(self):
        self.assertFalse(await self.cog._maybe_post_roundup(MONDAY_9))  # quiet week
        await db.archive_feed_posts([(self.feed_id, "Blog", "Fresh", "https://e.com/f", None)])
        await db.set_setting("feed_roundup", "off")
        self.assertFalse(await self.cog._maybe_post_roundup(MONDAY_9 + timedelta(days=7)))
        self.assertEqual(self.general.embeds, [])

    async def test_posts_in_the_community_channel_get_the_vote_emoji(self):
        await self.check(rss("c", "a", "b"))
        self.assertEqual(self.channel.votes_added, [(1, "👍")])
        with mock.patch.object(feeds, "FEED_COMMUNITY_CHANNEL_ID", 222):  # any other channel: no vote emoji
            await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(len(self.channel.votes_added), 1)

    async def test_votes_skip_skippy_and_the_author_and_summaries(self):
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        await self.check(rss("c", "a", "b"))                     # message 1
        await self.check(rss("e", "d", "c", "a", "b"))           # messages 2, 3
        self.channel.vote(1, 77, 501, 502)                        # author's own vote doesn't count
        self.channel.vote(3, 501)
        self.channel.vote(3, 503, emoji="❤️")                     # other emoji don't count
        await db.archive_feed_posts([(self.feed_id, "Blog", "S1", "https://e.com/s1", "https://discord.com/channels/1/111/9"),
                                     (self.feed_id, "Blog", "S2", "https://e.com/s2", "https://discord.com/channels/1/111/9")])
        posts = await db.roundup_posts(str(CHANNEL_ID))
        votes = await self.cog._vote_counts(posts)
        self.assertEqual(votes, {"https://discord.com/channels/1/111/1": 2, "https://discord.com/channels/1/111/3": 1})
        embed = feeds.build_roundup(posts, MONDAY_9, votes)
        self.assertTrue(embed.fields[0].value.startswith("🥇 [Post c]"))

    def test_server_emoji_vote_matches_how_discord_shows_reactions(self):
        # A custom emoji reaction's str() is "<:name:id>", the format FEED_VOTE_EMOJI uses.
        from config import FEED_VOTE_EMOJI
        self.assertEqual(str(discord.PartialEmoji.from_str(FEED_VOTE_EMOJI)), FEED_VOTE_EMOJI)

    async def test_command_preview_post_and_switch(self):
        await db.archive_feed_posts([(self.feed_id, "Blog", "Fresh", "https://e.com/f", None)])
        i = interaction()
        await feeds.FeedCommands.feed_roundup.callback(self.cog, i)  # default: preview
        self.assertTrue(i.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(self.general.embeds, [])
        await feeds.FeedCommands.feed_roundup.callback(self.cog, interaction(), action="post")
        self.assertEqual(len(self.general.embeds), 1)
        self.assertIsNotNone(await db.get_setting("feed_roundup_last_week"))  # Monday won't repeat it
        await feeds.FeedCommands.feed_roundup.callback(self.cog, interaction(), action="off")
        self.assertEqual(await db.get_setting("feed_roundup"), "off")


class FeedNameTests(FeedTestCase):
    async def test_display_name_replaces_the_feeds_own_title(self):
        await db.update_feed(self.feed_id, "1", display_name="CirriusTech")
        await self.check(rss("c", "a", "b"))
        self.assertEqual(self.channel.sent[0][0].author.name, "CirriusTech")
        self.assertEqual((await db.search_feed_posts(["post", "c"]))[0]["feed_title"], "CirriusTech")
        feed = await db.get_feed(self.feed_id, "1")
        self.assertEqual(feeds.feed_name(feed), "CirriusTech")
        self.assertIn('calls itself "Blog"', feeds.FeedCommands._describe(feed))

    async def test_add_takes_a_name_and_a_messy_url(self):
        _, items = feed_reader.parse_feed(rss("new").data)
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "Blogs", items, self.channel))
        await feeds.FeedCommands.feed_add.callback(self.cog, interaction(), "<\u200bcirriustech.co.uk/blog/index.xml>",
                                                   name="CirriusTech")
        self.assertEqual(self.cog._create_feed.await_args.args[1], "https://cirriustech.co.uk/blog/index.xml")
        self.assertEqual(self.channel.sent[0][0].author.name, "CirriusTech")

    async def test_edit_sets_and_clears_the_name(self):
        i = interaction()
        await feeds.FeedCommands.feed_edit.callback(self.cog, i, self.feed_id, name="  CirriusTech ")
        self.assertEqual((await db.get_feed(self.feed_id, "1"))["display_name"], "CirriusTech")
        await feeds.FeedCommands.feed_edit.callback(self.cog, interaction(), self.feed_id, clear="display_name")
        self.assertEqual(feeds.feed_name(await db.get_feed(self.feed_id, "1")), "Blog")


class FeedCommandTestCase(FeedTestCase):
    """Runs /feed add, /blog suggest and approvals for real, with feed
    channels CHANNEL_ID (the community channel) and 222."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name, value in (("FEED_CHANNELS", {"community-feed": CHANNEL_ID, "ms-blogs": 222}),
                            ("DEFAULT_CHANNEL_ID", CHANNEL_ID), ("FEED_COMMUNITY_CHANNEL_ID", CHANNEL_ID)):
            patcher = mock.patch.object(feeds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cog._post = mock.AsyncMock(return_value=set())
        self.cog._dm = mock.AsyncMock()

    def guild_interaction(self, user_id="500"):
        i = interaction(user_id)
        i.user.bot, i.user.joined_at = False, None
        channels = {cid: mock.Mock(spec=discord.TextChannel, id=cid, mention=f"<#{cid}>") for cid in (CHANNEL_ID, 222)}
        i.guild.get_channel = channels.get
        return i

    async def add(self, url, result, channel=None, author=None, **links):
        i = self.guild_interaction()
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=result)):
            await feeds.FeedCommands.feed_add.callback(self.cog, i, url, channel=channel, author=author, **links)
        return i.followup.send.await_args.args[0]

    async def suggest(self, url, result=None, author=None, user_id="300", **links):
        """A suggestion by member user_id, who picks author (themselves unless given)."""
        i = self.guild_interaction(user_id)
        result = result or rss("x")
        fetch = mock.AsyncMock(side_effect=result) if isinstance(result, Exception) else mock.AsyncMock(return_value=result)
        with mock.patch.object(feed_reader, "fetch_feed", fetch):
            await feeds.FeedCommands.feed_suggest.callback(self.cog, i, url, author or i.user, **links)
        return i.followup.send.await_args.args[0]


class DuplicateTests(FeedCommandTestCase):
    """The same blog never posts twice in a channel, however it's added.
    Feed #1 is https://example.com/feed (items a and b) in CHANNEL_ID."""

    async def test_the_same_feed_written_differently_is_refused(self):
        for url in ("http://www.example.com/feed/", "EXAMPLE.com/feed?utm_source=mail"):
            self.assertIn("same blog as feed #1", await self.add(url, rss("x", "y")))
        self.assertEqual(len(await db.get_feeds("1")), 1)

    async def test_the_same_blog_at_another_address_is_refused(self):
        self.assertIn("same blog as feed #1", await self.add("https://example.com/rss", rss("c", "b")))

    async def test_another_channel_or_another_blog_is_fine(self):
        self.assertIn("✅ Added", await self.add("https://example.com/feed", rss("a", "b"), channel="222"))
        self.assertIn("✅ Added", await self.add("https://other.example/feed", rss("x", "y")))

    async def test_moving_a_feed_next_to_the_same_blog_is_refused(self):
        await self.add("https://example.com/rss", rss("a", "b"), channel="222")  # feed #2
        i = interaction()
        await feeds.FeedCommands.feed_edit.callback(self.cog, i, 2, channel=str(CHANNEL_ID))
        self.assertIn("Feed #1 **Blog** already posts that blog", i.response.send_message.await_args.args[0])
        self.assertEqual((await db.get_feed(2, "1"))["channel_id"], "222")

    async def test_approving_a_duplicate_keeps_the_suggestion_open(self):
        sid = await db.add_suggestion("1", "https://example.com/rss", "Blog", "300", True, None)
        i = self.guild_interaction()
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=rss("b"))):
            await self.cog.approve_suggestion(i, sid)
        self.assertIn("same blog as feed #1", i.followup.send.await_args.args[0])
        self.assertEqual((await db.get_suggestion(sid))["status"], "pending")

    async def test_suggesting_a_blog_already_on_the_feed(self):
        self.assertIn("already on the feed as **Blog**", await self.suggest("http://example.com/feed/"))
        self.assertIn("already on the feed", await self.suggest("https://example.com/rss", rss("c", "a")))
        self.assertEqual(await db.pending_suggestions("1"), [])

    async def test_the_answer_says_whether_it_credits_you(self):
        self.assertIn("Ask a mod to credit you", await self.suggest("https://example.com/feed"))
        self.assertIn("Ask a mod to credit <@301>", await self.suggest("https://example.com/feed", author=member(301)))
        await feeds.FeedCommands._link_author(self.feed_id, "1", "300", "9")
        self.assertIn("Its posts credit you", await self.suggest("https://example.com/feed"))

    async def test_the_same_suggestion_written_differently_waits_once(self):
        self.assertIn("went to the mods", await self.suggest("https://new.example/feed"))
        self.assertIn("already suggested", await self.suggest("http://www.new.example/feed/", user_id="301"))
        self.assertEqual(len(await db.pending_suggestions("1")), 1)


class AuthorTests(FeedCommandTestCase):
    """Members suggest members' blogs: every suggestion has an author, and
    each member has one blog on the feed. Feed #1 is linked to member 77."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")

    async def test_a_suggestion_always_names_an_author(self):
        self.assertTrue(feeds.FeedCommands.feed_suggest.get_parameter("author").required)  # even if it's you
        await self.suggest("https://mine.example/feed")                          # picking themselves
        await self.suggest("https://theirs.example/feed", author=member(301))    # or another member
        authors = [(s["suggested_by"], s["author_id"]) for s in await db.pending_suggestions("1")]
        self.assertEqual(authors, [("300", "300"), ("300", "301")])
        card = self.alerts.embeds[-1].fields
        self.assertIn("<@301>", card[-1].value)
        self.assertIn("Suggested by <@300>", card[-1].value)
        bot = member(302)
        bot.bot = True
        self.assertIn("not a bot", await self.suggest("https://bot.example/feed", author=bot))

    async def test_a_member_with_a_blog_on_the_feed_cannot_get_another(self):
        self.assertIn("You already have a blog on the feed: **Blog**",
                      await self.suggest("https://second.example/feed", user_id="77"))
        self.assertIn("<@77> already has a blog", await self.suggest("https://second.example/feed", author=member(77)))
        self.assertEqual(await db.pending_suggestions("1"), [])

    async def test_one_waiting_suggestion_per_author(self):
        await self.suggest("https://mine.example/feed")
        self.assertIn("already a suggestion waiting for your blog", await self.suggest("https://other.example/feed"))
        self.assertIn("waiting for <@300>'s blog",
                      await self.suggest("https://other.example/feed", author=member(300), user_id="301"))

    async def test_mods_cannot_give_a_member_a_second_blog_either(self):
        self.assertIn("<@77> already has a blog on the feed: feed #1",
                      await self.add("https://second.example/feed", rss("x"), author=member(77)))
        await self.add("https://second.example/feed", rss("x"))  # feed #2, no author
        i = interaction()
        await feeds.FeedCommands.feed_edit.callback(self.cog, i, 2, author=member(77))
        self.assertIn("clear:author", i.response.send_message.await_args.args[0])
        self.assertIsNone((await db.get_feed(2, "1"))["author_id"])
        i = interaction()
        await feeds.FeedCommands.feed_edit.callback(self.cog, i, 1, author=member(77))  # re-linking their own: fine
        self.assertIn("Updated feed #1", i.response.send_message.await_args.args[0])

    async def test_approving_waits_if_the_author_got_a_blog_meanwhile(self):
        sid = await db.add_suggestion("1", "https://second.example/feed", "Second", "77", "77", None)
        i = self.guild_interaction()
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=rss("x"))):
            await self.cog.approve_suggestion(i, sid)
        self.assertIn("already has a blog", i.followup.send.await_args.args[0])
        self.assertEqual((await db.get_suggestion(sid))["status"], "pending")

    async def test_mods_adding_a_community_feed_without_an_author_are_told(self):
        self.assertIn("No author", await self.add("https://news.example/feed", rss("x")))
        self.assertNotIn("No author", await self.add("https://ms.example/feed", rss("y"), channel="222"))


class RefusalReportTests(FeedCommandTestCase):
    """A refused /blog suggest is reported to the mods, so they can reach
    out. Feed #1 (https://example.com/feed) is linked to member 77."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")

    def reports(self):
        return [content for content, _ in self.alerts.sent if content.startswith("🚫")]

    async def test_mods_see_who_tried_what_and_why_without_pings(self):
        await self.suggest("https://second.example/feed", user_id="77")
        self.assertEqual(self.reports(), ["🚫 <@77> suggested their blog <https://second.example/feed>, refused: "
                                          "<@77> already has a blog on the feed: feed #1 **Blog**"])
        mentions = self.alerts.sent[-1][1]
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)
        entry = (await db.get_recent_audit_log(1))[0]
        self.assertEqual((entry["action"], entry["actor_id"], entry["target_id"]),
                         ("feed_suggestion_refused", "77", "77"))

    async def test_every_kind_of_refusal_is_reported(self):
        bot = member(302)
        bot.bot = True
        await self.suggest("https://bot.example/feed", author=bot)
        await self.suggest("http://www.example.com/feed/", author=member(301))
        await self.suggest("https://broken.example/feed", result=feeds.FeedError("HTTP 404"))
        reports = self.reports()
        self.assertIn("the author is a bot", reports[0])
        self.assertIn("suggested <@301>'s blog", reports[1])
        self.assertIn("already on the feed as feed #1 **Blog** (credited to <@77>)", reports[1])
        self.assertIn("couldn't read it as a feed: HTTP 404", reports[2])

    async def test_retrying_the_same_blog_is_reported_once_an_hour(self):
        for _ in range(3):
            await self.suggest("https://broken.example/feed", result=feeds.FeedError("HTTP 404"))
        self.assertEqual(len(self.reports()), 1)
        await self.suggest("https://other-broken.example/feed", result=feeds.FeedError("HTTP 404"))
        self.assertEqual(len(self.reports()), 2)
        self.cog.refusals_told = {k: t - feeds.REFUSAL_REPEAT_SECONDS for k, t in self.cog.refusals_told.items()}
        await self.suggest("https://broken.example/feed", result=feeds.FeedError("HTTP 404"))
        self.assertEqual(len(self.reports()), 3)

    async def test_accepted_suggestions_are_not_reported(self):
        await self.suggest("https://fine.example/feed")
        self.assertEqual(self.reports(), [])


class ProfileLinkTests(FeedCommandTestCase):
    """Authors' LinkedIn, X, YouTube and GitHub under their posts. Feed #1
    is linked to member 77."""

    LINKEDIN = "https://www.linkedin.com/in/jonas-b"

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")

    async def links_command(self, user_id="77", **options):
        i = self.guild_interaction(user_id)
        await feeds.FeedCommands.blog_links.callback(self.cog, i, **options)
        return i.response.send_message.await_args.args[0]

    async def test_posts_show_the_authors_links_under_the_credit(self):
        await db.set_author_links("77", {"linkedin": self.LINKEDIN, "x": "https://x.com/jonasb"})
        del self.cog._post  # post for real
        await self.check(rss("c", "a", "b"))
        emoji = feeds.FEED_LINK_EMOJIS
        self.assertEqual(self.channel.sent[0][0].description.split("\n"),
                         ["by <@77>", f"{emoji['linkedin']} [jonas-b](<{self.LINKEDIN}>)  "
                                      f"{emoji['x']} [@jonasb](<https://x.com/jonasb>)"])

    async def test_suggestion_carries_links_to_the_card_and_approval_saves_them(self):
        await self.suggest("https://new.example/feed", user_id="300", linkedin="jonas-b")
        suggestion = (await db.pending_suggestions("1"))[0]
        self.assertIn(self.LINKEDIN, self.alerts.embeds[-1].fields[-1].value)
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=rss("x"))):
            await self.cog.approve_suggestion(self.guild_interaction(), suggestion["id"])
        self.assertEqual(await db.get_author_links("300"), {"linkedin": self.LINKEDIN})

    async def test_a_bad_link_is_a_typo_not_a_refusal(self):
        message = await self.suggest("https://new.example/feed", user_id="300", x="https://x.com/home")
        self.assertIn("doesn't look like an X profile", message)
        self.assertEqual(await db.pending_suggestions("1"), [])
        self.assertEqual([c for c, _ in self.alerts.sent if c.startswith("🚫")], [])

    async def test_mods_set_links_with_an_author_only(self):
        self.assertIn("pick the `author` too", await self.add("https://new.example/feed", rss("x"), linkedin="jonas-b"))
        await self.add("https://new.example/feed", rss("x"), author=member(300), github="JonasBogvad")
        self.assertEqual(await db.get_author_links("300"), {"github": "https://github.com/JonasBogvad"})
        i = interaction()
        await feeds.FeedCommands.feed_edit.callback(self.cog, i, self.feed_id, youtube="@skymadesimple")
        self.assertIn("[YouTube](<https://www.youtube.com/@skymadesimple>)", i.response.send_message.await_args.args[0])
        await feeds.FeedCommands.feed_edit.callback(self.cog, interaction(), self.feed_id, clear="link:youtube")
        self.assertEqual(await db.get_author_links("77"), {})

    async def test_authors_manage_their_own_links(self):
        self.assertIn("need a blog on the feed first", await self.links_command(user_id="300", x="@jonasb"))
        self.assertEqual(await db.get_author_links("300"), {})
        self.assertIn("✅ Saved", await self.links_command(x="@jonasb", linkedin="jonas-b"))
        self.assertEqual(self.alerts.sent[-1][0], "🛠️ <@77> changed their profile links: "
                                                  f"LinkedIn <{self.LINKEDIN}>, X <https://x.com/jonasb>")
        self.assertIn("[@jonasb](<https://x.com/jonasb>)", await self.links_command())  # no options: shows them
        await self.links_command(clear="all")
        self.assertEqual(await db.get_author_links("77"), {})

    async def test_removing_the_blog_removes_its_links(self):
        await db.set_author_links("77", {"x": "https://x.com/jonasb"})
        await feeds.FeedCommands.feed_remove.callback(self.cog, interaction(), self.feed_id)
        self.assertEqual(await db.get_author_links("77"), {})


class ConsistentPostsTests(FeedTestCase):
    """Every post in #community-feed gets the vote emoji and a thread, however it was posted."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name, value in (("FEED_COMMUNITY_CHANNEL_ID", CHANNEL_ID), ("FEED_THREAD_CHANNEL_IDS", {CHANNEL_ID}),
                            ("FEED_VOTE_EMOJI", "👍")):
            patcher = mock.patch.object(feeds, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_feed_test_posts_like_any_other_post(self):
        i = interaction()
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=rss("new", "old"))):
            await feeds.FeedCommands.feed_test.callback(self.cog, i, self.feed_id)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(len(self.channel.votes_added), 1)
        self.assertEqual(self.channel.threads, ["Post new"])
        self.assertFalse(self.channel.sent[0][1].users or self.channel.sent[0][1].roles)  # no pings
        self.assertEqual([r["title"] for r in await db.search_feed_posts(["new"])], ["Post new"])  # archived
        self.assertIn("test-posted feed #1 **Blog**", self.alerts.sent[-1][0])  # mods see it

    async def test_summary_gets_a_thread_but_no_vote(self):
        await self.check(rss(*[f"n{i}" for i in range(8, 0, -1)]))
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.channel.votes_added, [])
        self.assertEqual(self.channel.threads, ["Blog - 8 new posts"])

    async def test_mod_actions_are_audited_in_discord_and_the_audit_log(self):
        await feeds.FeedCommands.feed_edit.callback(self.cog, interaction(), self.feed_id, name="Skippy Blog",
                                                    exclude="chromium")
        self.assertEqual(self.alerts.sent[-1][0], "🛠️ <@500> edited feed #1 **Skippy Blog**: name, exclude words")
        self.assertFalse(self.alerts.sent[-1][1].users)  # mentioned, not pinged
        entry = (await db.get_recent_audit_log(1))[0]
        self.assertEqual((entry["action"], entry["actor_id"]), ("feed_edit", "500"))
        await feeds.FeedCommands.feed_edit.callback(self.cog, interaction(), self.feed_id)  # just looking
        self.assertEqual(len(self.alerts.sent), 1)
        await feeds.FeedCommands.feed_remove.callback(self.cog, interaction(), self.feed_id)
        self.assertEqual(self.alerts.sent[-1][0], "🛠️ <@500> removed feed #1 **Skippy Blog**")

    async def test_an_article_posted_twice_shows_once(self):
        await self.check(rss("c", "a", "b"))                     # message 1
        with mock.patch.object(feed_reader, "fetch_feed", mock.AsyncMock(return_value=rss("c", "a", "b"))):
            await feeds.FeedCommands.feed_test.callback(self.cog, interaction(), self.feed_id)  # message 2, same post
        self.channel.vote(2, 501, 502)
        posts = await db.roundup_posts(str(CHANNEL_ID))
        self.assertEqual(len(posts), 2)
        embed = feeds.build_roundup(posts, MONDAY_9, await self.cog._vote_counts(posts))
        self.assertEqual(embed.description.count("[Post c]"), 1)
        self.assertIn("· 2", embed.fields[0].value)  # the copy with the votes counts
        i = interaction()
        await feeds.FeedCommands.feed_search.callback(self.cog, i, "post c")
        self.assertEqual(i.response.send_message.await_args.kwargs["embed"].description.count("[Post c]"), 1)


class LogFileTests(unittest.TestCase):
    def test_logs_live_next_to_the_database_and_keep_a_week(self):
        import config
        self.assertEqual(config.LOG_DIR, os.path.join(os.path.dirname(config.DB_PATH), "logs"))
        self.assertEqual(config.LOG_DAYS, 7)

def member(user_id):
    return mock.Mock(id=user_id, mention=f"<@{user_id}>", bot=False, joined_at=None)


def interaction(user_id="500", mod=True):
    i = mock.Mock()
    i.user = mock.Mock(id=int(user_id), mention=f"<@{user_id}>",
                       roles=[mock.Mock(id=feeds.RSS_ADMIN_ROLE_ID)] if mod else [])
    i.guild, i.guild_id = mock.Mock(id=1), 1
    i.response.defer = mock.AsyncMock()
    i.response.send_message = mock.AsyncMock()
    i.followup.send = mock.AsyncMock()
    i.message = mock.Mock(id=999, embeds=[discord.Embed(title="📬 Feed suggestion")], edit=mock.AsyncMock())
    return i


class SuggestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.path.exists(os.environ["SKIPPY_DB_PATH"]):
            os.remove(os.environ["SKIPPY_DB_PATH"])
        await db.init_db()
        self.cog = feeds.FeedCommands.__new__(feeds.FeedCommands)
        self.cog.bot = mock.Mock()
        self.cog._dm = mock.AsyncMock()
        self.sid = await db.add_suggestion("1", "https://blog.example/feed", "My Blog", "300", "300", None)
        self.channel = FakeChannel()
        self.feed_id = await db.add_feed("1", str(CHANNEL_ID), "https://blog.example/feed", "My Blog", "500")
        _, items = feed_reader.parse_feed(rss("latest", "older").data)
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "My Blog", items, self.channel))

    async def test_approve_adds_feed_credits_author_and_tells_everyone(self):
        i = interaction()
        await self.cog.approve_suggestion(i, self.sid)
        self.cog._create_feed.assert_awaited_once_with(i.guild, "https://blog.example/feed",
                                                       feeds.DEFAULT_CHANNEL_ID, "300", "500")
        suggestion = await db.get_suggestion(self.sid)
        self.assertEqual((suggestion["status"], suggestion["decided_by"]), ("approved", "500"))
        card = i.message.edit.await_args.kwargs
        self.assertIsNone(card["view"])  # buttons removed
        self.assertIn(f"Approved by <@500>: feed #{self.feed_id}", card["embed"].fields[-1].value)
        self.assertIn("approved", self.cog._dm.await_args.args[1])
        self.assertEqual(len(self.channel.sent), 1)  # the blog's newest article, as a welcome
        self.assertEqual((self.channel.sent[0][0].author.name, self.channel.sent[0][0].title), ("My Blog", "Post latest"))

    async def test_another_members_blog_credits_them_and_tells_both(self):
        sid = await db.add_suggestion("1", "https://other.example/feed", "Other", "300", "301", None)
        await self.cog.approve_suggestion(interaction(), sid)
        self.assertEqual(self.cog._create_feed.await_args.args[3], "301")
        told = {call.args[0]: call.args[1] for call in self.cog._dm.await_args_list}
        self.assertIn("crediting <@301>", told["300"])
        self.assertIn("suggested by <@300>", told["301"])

    async def test_second_mod_cannot_decide_again(self):
        await self.cog.approve_suggestion(interaction("500"), self.sid)
        late = interaction("501")
        await self.cog.reject_suggestion(late, self.sid, "nope")
        self.assertIn("already decided", late.response.send_message.await_args.args[0])
        self.assertEqual((await db.get_suggestion(self.sid))["status"], "approved")

    async def test_failed_approval_goes_back_to_the_queue(self):
        self.cog._create_feed.side_effect = feeds.FeedError("HTTP 404")
        i = interaction()
        await self.cog.approve_suggestion(i, self.sid)
        self.assertEqual((await db.get_suggestion(self.sid))["status"], "pending")
        self.assertIn("HTTP 404", i.followup.send.await_args.args[0])
        i.message.edit.assert_not_awaited()  # card and buttons untouched
        self.assertEqual(self.channel.sent, [])

    async def test_reject_with_reason(self):
        i = interaction()
        await self.cog.reject_suggestion(i, self.sid, "Not about Microsoft tech")
        suggestion = await db.get_suggestion(self.sid)
        self.assertEqual((suggestion["status"], suggestion["reason"]), ("rejected", "Not about Microsoft tech"))
        self.assertIn("Rejected by <@500>: Not about Microsoft tech", i.message.edit.await_args.kwargs["embed"].fields[-1].value)
        self.assertIn("Reason: Not about Microsoft tech", self.cog._dm.await_args.args[1])

    async def test_buttons_only_work_for_mods(self):
        button = feeds.SuggestionButton("approve", self.sid)
        self.assertEqual(button.item.custom_id, f"feed-suggestion:approve:{self.sid}")
        self.assertTrue(await button.interaction_check(interaction(mod=True)))
        member = interaction(mod=False)
        self.assertFalse(await button.interaction_check(member))
        self.assertIn("can decide on suggestions", member.response.send_message.await_args.args[0])


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_feeds_table_from_before_settings_is_upgraded(self):
        path = os.environ["SKIPPY_DB_PATH"]
        if os.path.exists(path):
            os.remove(path)
        with sqlite3.connect(path) as old:  # the feeds table as the first /feed release created it
            old.execute("""CREATE TABLE feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT, role_id TEXT, added_by TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, etag TEXT, last_modified TEXT,
                last_checked TIMESTAMP, last_error TEXT, failures INTEGER NOT NULL DEFAULT 0,
                UNIQUE (url, channel_id))""")
            old.execute("INSERT INTO feeds (guild_id, channel_id, url) VALUES ('1', '2', 'https://e.com/feed')")
        await db.init_db()
        await db.init_db()  # running it again is harmless
        feed = (await db.get_feeds())[0]
        self.assertEqual((feed["url"], feed["summary_after"], feed["exclude_words"]), ("https://e.com/feed", 5, None))

    async def test_old_suggestions_get_their_author(self):
        path = os.environ["SKIPPY_DB_PATH"]
        if os.path.exists(path):
            os.remove(path)
        with sqlite3.connect(path) as old:  # before suggestions named an author
            old.execute("""CREATE TABLE feed_suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT, suggested_by TEXT NOT NULL,
                own_blog INTEGER NOT NULL DEFAULT 1, note TEXT, status TEXT NOT NULL DEFAULT 'pending',
                decided_by TEXT, reason TEXT, message_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                decided_at TIMESTAMP)""")
            old.execute("INSERT INTO feed_suggestions (guild_id, url, suggested_by, own_blog) VALUES ('1', 'u1', '300', 1)")
            old.execute("INSERT INTO feed_suggestions (guild_id, url, suggested_by, own_blog) VALUES ('1', 'u2', '300', 0)")
        await db.init_db()
        self.assertEqual([s["author_id"] for s in await db.pending_suggestions("1")], ["300", None])


if __name__ == "__main__":
    unittest.main()
