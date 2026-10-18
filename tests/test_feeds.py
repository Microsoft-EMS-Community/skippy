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
    def __init__(self):
        self.sent, self.fail_on = [], set()

    async def send(self, content, allowed_mentions=None):
        if any(f"/{k}\n" in content + "\n" for k in self.fail_on):
            raise discord.HTTPException(mock.Mock(status=503, reason="unavailable"), "down")
        self.sent.append((content, allowed_mentions))
        return mock.Mock(jump_url=f"https://discord.com/channels/1/{CHANNEL_ID}/{len(self.sent)}")


class FeedPollTests(unittest.IsolatedAsyncioTestCase):
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
