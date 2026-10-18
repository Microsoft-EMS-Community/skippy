# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Exercises the /feed polling logic against a throwaway database and a fake
# Discord channel. No network: fetch_feed is replaced with canned feeds.

import os
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
        self.feed_id = await db.add_feed("1", str(CHANNEL_ID), "https://example.com/feed", "Blog", "42", "9")
        await db.mark_feed_items_seen(self.feed_id, ["a", "b"])  # what /feed add does

    async def check(self, result):
        feed = await db.get_feed(self.feed_id, "1")
        fetch = mock.AsyncMock(side_effect=result) if isinstance(result, Exception) else mock.AsyncMock(return_value=result)
        with mock.patch.object(feed_reader, "fetch_feed", fetch):
            await self.cog._check(mock.Mock(), feed)

    def posted(self):
        return [content.rsplit("/", 1)[1] for content, _ in self.channel.sent]

    async def test_only_new_items_post_oldest_first_with_role_ping(self):
        await self.check(rss("a", "b"))
        self.assertEqual(self.posted(), [])
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])
        content, mentions = self.channel.sent[0]
        self.assertTrue(content.startswith("<@&42> **Blog**: Post c"))
        self.assertEqual([r.id for r in mentions.roles], [42])
        self.assertFalse(mentions.everyone)
        await self.check(rss("d", "c", "a", "b"))
        self.assertEqual(self.posted(), ["c", "d"])  # nothing posts twice

    async def test_flood_is_capped_and_the_rest_skipped(self):
        keys = [f"n{i}" for i in range(8, 0, -1)]  # n8 newest ... n1 oldest
        await self.check(rss(*keys))
        self.assertEqual(self.posted(), ["n4", "n5", "n6", "n7", "n8"])
        await self.check(rss(*keys))
        self.assertEqual(len(self.channel.sent), 5)  # n1-n3 were skipped for good, not posted later

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
        self.assertIn("failed 6 checks in a row: HTTP 500", self.alerts.sent[0][0])
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


if __name__ == "__main__":
    unittest.main()
