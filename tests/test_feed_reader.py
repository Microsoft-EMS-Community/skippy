# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# No test touches the internet: fetching runs against a local test server.

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import feed_reader  # noqa: E402
from feed_reader import FeedError, FeedItem, format_post, parse_feed, select_new  # noqa: E402

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
  <title>Intune Blog</title>
  <item><title>Newest &amp; best</title><link>https://example.com/b?utm_source=rss&amp;id=2</link>
        <guid isPermaLink="false">post-2</guid><pubDate>Tue, 14 Oct 2026 09:00:00 +0000</pubDate></item>
  <item><title>Older</title><link>https://example.com/a</link>
        <pubDate>Mon, 13 Oct 2026 09:00:00 GMT</pubDate></item>
  <item><title>Guid only</title><guid>https://example.com/c</guid></item>
  <item><title>No link at all</title></item>
  <item><title>Duplicate</title><link>https://example.com/b2</link><guid>post-2</guid></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <title>Some Channel</title>
  <entry><id>yt:video:abc</id><title>Video one</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc"/>
    <published>2026-10-15T10:00:00+00:00</published></entry>
  <entry><id>tag:x,2026:2</id><title>Self link only</title>
    <link rel="self" href="https://example.com/self"/><link href="https://example.com/post"/>
    <updated>2026-10-14T10:00:00Z</updated></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://example.org/"><title>RDF Feed</title></channel>
  <item rdf:about="https://example.org/1"><title>First</title><link>https://example.org/1</link>
        <dc:date>2026-10-10T08:00:00Z</dc:date></item>
</rdf:RDF>"""


def item(key, published=None):
    return FeedItem(key, key, f"https://example.com/{key}", published)


class ParseTests(unittest.TestCase):
    def test_rss2(self):
        title, items = parse_feed(RSS)
        self.assertEqual(title, "Intune Blog")
        self.assertEqual([i.key for i in items],
                         ["post-2", "https://example.com/a", "https://example.com/c"])
        self.assertEqual(items[0].title, "Newest & best")
        self.assertEqual(items[0].published, datetime(2026, 10, 14, 9, tzinfo=timezone.utc))
        self.assertEqual(items[2].link, "https://example.com/c")  # guid used as the link

    def test_atom(self):
        title, items = parse_feed(ATOM)
        self.assertEqual(title, "Some Channel")
        self.assertEqual([(i.key, i.link) for i in items],
                         [("yt:video:abc", "https://www.youtube.com/watch?v=abc"),
                          ("tag:x,2026:2", "https://example.com/post")])
        self.assertEqual(items[1].published, datetime(2026, 10, 14, 10, tzinfo=timezone.utc))

    def test_rss1_rdf(self):
        title, items = parse_feed(RDF)
        self.assertEqual((title, [i.key for i in items]), ("RDF Feed", ["https://example.org/1"]))

    def test_dates_without_timezone_become_utc(self):
        # Both forms parse to naive datetimes; select_new needs them comparable.
        _, items = parse_feed(b"""<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>t</title>
            <item><link>https://e.com/1</link><pubDate>Mon, 13 Oct 2026 09:00:00 -0000</pubDate></item>
            <item><link>https://e.com/2</link><dc:date>2026-10-12T09:00:00</dc:date></item>
            </channel></rss>""")
        self.assertEqual([i.published for i in items],
                         [datetime(2026, 10, 13, 9, tzinfo=timezone.utc), datetime(2026, 10, 12, 9, tzinfo=timezone.utc)])

    def test_rejects_entities_bad_xml_and_non_feeds(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>'
        for data in (bomb, b"<rss><channel>", b"<feed><entry/></feed>", b"<rss/>"):
            with self.assertRaises(FeedError, msg=data):
                parse_feed(data)

    def test_web_page_gets_a_helpful_error(self):
        for page in (b"\n<!DOCTYPE html><html><head>", b"<HTML><body>hi</body></HTML>"):
            with self.assertRaisesRegex(FeedError, "web page, not a feed"):
                parse_feed(page)


class SelectAndFormatTests(unittest.TestCase):
    def test_oldest_first_by_date_and_capped_to_newest(self):
        d = lambda day: datetime(2026, 10, day, tzinfo=timezone.utc)  # noqa: E731
        items = [item("c", d(3)), item("a", d(1)), item("b", d(2))]
        self.assertEqual([i.key for i in select_new(items, set(), 5)], ["a", "b", "c"])
        self.assertEqual([i.key for i in select_new(items, {"c"}, 5)], ["a", "b"])
        self.assertEqual([i.key for i in select_new(items, set(), 2)], ["b", "c"])

    def test_without_dates_assumes_newest_first(self):
        items = [item("new"), item("mid"), item("old")]
        self.assertEqual([i.key for i in select_new(items, set(), 5)], ["old", "mid", "new"])

    def test_format_post(self):
        post = format_post("My *Blog*", FeedItem("k", "  A\n  __title__ ", "https://x.com/p?utm_source=rss&id=1", None))
        self.assertEqual(post, "**My \\*Blog\\***: A \\_\\_title\\_\\_\nhttps://x.com/p?id=1")
        self.assertTrue(format_post("", item("t"), role_id="42").startswith("<@&42> **t**"))
        self.assertLessEqual(len(format_post("F", FeedItem("k", "x" * 5000, "https://x.com/" + "y" * 3000, None))), 2000)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        app.router.add_get("/feed", self.feed)
        app.router.add_get("/hop", lambda r: web.HTTPFound("/feed"))
        app.router.add_get("/evil", lambda r: web.HTTPFound("http://blocked.test/"))
        app.router.add_get("/big", lambda r: web.Response(body=b"x" * (feed_reader.MAX_FEED_BYTES + 10)))
        app.router.add_get("/broken", lambda r: web.Response(status=500))
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()
        # The local server is (rightly) not a public address, so let it through.
        patcher = mock.patch.object(feed_reader, "_is_public_host",
                                    mock.AsyncMock(side_effect=lambda host: host != "blocked.test"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = feed_reader.session()

    async def asyncTearDown(self):
        await self.http.close()
        await self.server.close()

    async def feed(self, request):
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.Response(body=RSS, headers={"ETag": '"v1"', "Content-Type": "application/rss+xml"})

    def url(self, path):
        return str(self.server.make_url(path))

    async def test_fetch_then_not_modified(self):
        first = await feed_reader.fetch_feed(self.http, self.url("/feed"))
        self.assertEqual((first.data, first.etag), (RSS, '"v1"'))
        again = await feed_reader.fetch_feed(self.http, self.url("/feed"), etag=first.etag)
        self.assertIsNone(again.data)

    async def test_follows_redirects(self):
        self.assertEqual((await feed_reader.fetch_feed(self.http, self.url("/hop"))).data, RSS)

    async def test_refuses_redirect_to_non_public_host(self):
        with self.assertRaisesRegex(FeedError, "blocked.test"):
            await feed_reader.fetch_feed(self.http, self.url("/evil"))

    async def test_size_limit_status_and_scheme(self):
        for path, pattern in (("/big", "larger than 5 MB"), ("/broken", "HTTP 500")):
            with self.assertRaisesRegex(FeedError, pattern):
                await feed_reader.fetch_feed(self.http, self.url(path))
        with self.assertRaisesRegex(FeedError, "http"):
            await feed_reader.fetch_feed(self.http, "file:///etc/passwd")


if __name__ == "__main__":
    unittest.main()
