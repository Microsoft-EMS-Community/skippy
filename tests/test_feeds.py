# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Exercises the /feed polling logic against a throwaway database and a fake
# Discord channel. No network: fetch_feed is replaced with canned feeds.

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ["SKIPPY_DB_PATH"] = os.path.join(_tmp.name, "test.db")

import discord  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import feed_reader  # noqa: E402
import feeds  # noqa: E402
from feed_reader import FeedError, FetchResult  # noqa: E402

CHANNEL_ID, ALERT_CHANNEL_ID = 111, feeds.FEED_ALERT_CHANNEL_ID


def rss(*keys):
    """An RSS feed whose items (newest first) are the given keys."""
    items = "".join(f"<item><title>Post {k}</title><link>https://example.com/{k}</link><guid>{k}</guid></item>"
                    for k in keys)
    return FetchResult(f"<rss><channel><title>Blog</title>{items}</channel></rss>".encode(), None, None)


class FakeChannel:
    def __init__(self, channel_id=CHANNEL_ID):
        self.id, self.sent, self.fail_on, self.threads = channel_id, [], set(), []
        self.mention = f"<#{channel_id}>"
        self.thread_error = None

    async def send(self, content, allowed_mentions=None):
        if any(f"/{k}\n" in content + "\n" for k in self.fail_on):
            raise discord.HTTPException(mock.Mock(status=503, reason="unavailable"), "down")
        self.sent.append((content, allowed_mentions))

        async def create_thread(name, auto_archive_duration):
            if self.thread_error:
                raise self.thread_error
            self.threads.append(name)

        return mock.Mock(jump_url=f"https://discord.com/channels/1/{self.id}/{len(self.sent)}",
                         create_thread=create_thread)


class FeedTestCase(unittest.IsolatedAsyncioTestCase):
    """A fresh database with one feed (#1, role 42) and a fake channel."""

    async def asyncSetUp(self):
        if os.path.exists(os.environ["SKIPPY_DB_PATH"]):
            os.remove(os.environ["SKIPPY_DB_PATH"])
        await db.init_db()
        self.channel, self.alerts = FakeChannel(), FakeChannel()
        bot = mock.Mock()
        bot.get_channel = lambda cid: {CHANNEL_ID: self.channel, ALERT_CHANNEL_ID: self.alerts}.get(cid)
        self.cog = feeds.FeedCommands.__new__(feeds.FeedCommands)  # skip __init__: no poll loop
        self.cog.bot = bot
        self.cog.skip_until = {}
        self.feed_id = await db.add_feed("1", str(CHANNEL_ID), "https://example.com/feed", "Blog", "42", "9")
        await db.mark_feed_items_seen(self.feed_id, ["a", "b"])  # what /feed add does

    async def check(self, result):
        feed = await db.get_feed(self.feed_id, "1")
        fetch = mock.AsyncMock(side_effect=result) if isinstance(result, Exception) else mock.AsyncMock(return_value=result)
        with mock.patch.object(feed_reader, "fetch_feed", fetch):
            await self.cog._check(mock.Mock(), feed)
        return fetch

    def posted(self):
        return [content.rsplit("/", 1)[1] for content, _ in self.channel.sent]


class FeedPollTests(FeedTestCase):
    async def test_only_new_items_post_oldest_first_with_role_ping(self):
        await self.check(rss("a", "b"))
        self.assertEqual(self.posted(), [])
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        content, mentions = self.channel.sent[0]
        self.assertTrue(content.startswith("<@&42> Blog - Post c\n"))
        self.assertEqual([r.id for r in mentions.roles], [42])
        self.assertFalse(mentions.everyone)
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])  # nothing posts twice

    async def test_burst_becomes_one_summary_with_nothing_dropped(self):
        keys = [f"n{i}" for i in range(8, 0, -1)]  # n8 newest ... n1 oldest
        await self.check(rss(*keys))
        self.assertEqual(len(self.channel.sent), 1)
        summary, mentions = self.channel.sent[0]
        self.assertTrue(summary.startswith("<@&42> Blog - 8 new posts:"))
        self.assertEqual([line.split("](<")[1][:-2] for line in summary.splitlines()[1:]],
                         [f"https://example.com/n{i}" for i in range(1, 9)])  # oldest first, all 8
        self.assertEqual([r.id for r in mentions.roles], [42])
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
        self.assertEqual(lines[0], "<@&42> MSRC - 2 new posts:")
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

    async def test_author_is_credited_and_pinged(self):
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        await self.check(rss("c", "a", "b"))
        content, mentions = self.channel.sent[0]
        self.assertTrue(content.startswith("<@&42> Blog - Post c · by <@77>\n"))
        self.assertEqual([u.id for u in mentions.users], [77])

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


    async def test_add_with_post_latest_posts_newest_without_pings(self):
        await feeds.FeedCommands._link_author(self.feed_id, "1", "77", "9")
        _, items = feed_reader.parse_feed(rss("new", "old").data)
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "Blog", items, self.channel))
        i = interaction()
        await feeds.FeedCommands.feed_add.callback(self.cog, i, "https://example.com/feed", post_latest=True)
        self.assertEqual(len(self.channel.sent), 1)
        content, mentions = self.channel.sent[0]
        self.assertTrue(content.startswith("<@&42> Blog - Post new · by <@77>\n"))  # looks like a normal post ...
        self.assertFalse(mentions.users or mentions.roles or mentions.everyone)   # ... but pings nobody
        self.assertEqual([r["title"] for r in await db.search_feed_posts(["new"])], ["Post new"])
        self.assertIn("Posted its newest item", i.followup.send.await_args.args[0])

    async def test_add_without_post_latest_posts_nothing(self):
        _, items = feed_reader.parse_feed(rss("new", "old").data)
        self.cog._create_feed = mock.AsyncMock(return_value=(self.feed_id, "Blog", items, self.channel))
        await feeds.FeedCommands.feed_add.callback(self.cog, interaction(), "https://example.com/feed")
        self.assertEqual(self.channel.sent, [])

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
        self.sid = await db.add_suggestion("1", "https://blog.example/feed", "My Blog", "300", True, None)
        self.channel = mock.Mock(mention="<#community>")
        self.cog._create_feed = mock.AsyncMock(return_value=(7, "My Blog", 3, self.channel))

    async def test_approve_adds_feed_credits_author_and_tells_everyone(self):
        i = interaction()
        await self.cog.approve_suggestion(i, self.sid)
        self.cog._create_feed.assert_awaited_once_with(i.guild, "https://blog.example/feed",
                                                       feeds.DEFAULT_CHANNEL_ID, None, "300", "500")
        suggestion = await db.get_suggestion(self.sid)
        self.assertEqual((suggestion["status"], suggestion["decided_by"]), ("approved", "500"))
        card = i.message.edit.await_args.kwargs
        self.assertIsNone(card["view"])  # buttons removed
        self.assertIn("Approved by <@500>: feed #7", card["embed"].fields[-1].value)
        self.assertIn("approved", self.cog._dm.await_args.args[1])

    async def test_not_own_blog_gets_no_author(self):
        sid = await db.add_suggestion("1", "https://other.example/feed", "Other", "300", False, None)
        await self.cog.approve_suggestion(interaction(), sid)
        self.assertIsNone(self.cog._create_feed.await_args.args[4])

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


if __name__ == "__main__":
    unittest.main()
