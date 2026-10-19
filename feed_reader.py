# ============================================================
# feed_reader.py - Fetch and parse RSS/Atom feeds for /feed
# No bot, command or database code here, so it can be tested on its own.
# ============================================================

import email.utils
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp
import discord
from yarl import URL

from link_cleaner import _BlockedAddress, _is_public_host, _PublicOnlyResolver, clean_url

USER_AGENT = "Skippy feed reader (+https://github.com/Microsoft-EMS-Community/skippy)"
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECT_HOPS = 5
MAX_TITLE_CHARS = 200

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DC = "{http://purl.org/dc/elements/1.1/}"


class FeedError(Exception):
    """A feed couldn't be fetched or read. The message is shown to admins."""


class RateLimited(FeedError):
    """The site asked us to slow down. Don't fetch it again for retry_after seconds."""

    def __init__(self, status: int, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}, the site asked to wait {retry_after // 60} minutes")


@dataclass
class FeedItem:
    key: str                    # stable ID used to remember what was posted
    title: str
    link: str
    published: datetime | None  # always timezone-aware when set


@dataclass
class FetchResult:
    data: bytes | None          # None means 304 Not Modified
    etag: str | None
    last_modified: str | None


def session() -> aiohttp.ClientSession:
    """HTTP session whose connections can only reach public addresses
    (same DNS-rebinding-safe resolver as link_cleaner)."""
    connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver())
    return aiohttp.ClientSession(connector=connector, headers={"User-Agent": USER_AGENT})


async def fetch_feed(http: aiohttp.ClientSession, url: str,
                     etag: str | None = None, last_modified: str | None = None) -> FetchResult:
    """Download a feed. Redirects are followed by hand so every hop's host is
    checked before it's requested; IP-literal hosts never reach the resolver,
    so the up-front check is what stops a redirect to e.g. 127.0.0.1."""
    headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                         "text/xml;q=0.8, */*;q=0.5"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    current = url
    for _ in range(MAX_REDIRECT_HOPS + 1):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FeedError("that isn't an http(s) URL")
        if not await _is_public_host(parts.hostname):
            raise FeedError(f"{parts.hostname} isn't a public address")
        try:
            async with http.get(current, headers=headers, allow_redirects=False,
                                timeout=FETCH_TIMEOUT) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FeedError(f"HTTP {resp.status} redirect without a Location")
                    current = str(resp.url.join(URL(location)))
                    continue
                if resp.status == 304:
                    return FetchResult(None, etag, last_modified)
                # 429 always means "slow down"; a 503 only when it says for how long.
                retry_after = resp.headers.get("Retry-After")
                if resp.status == 429 or (resp.status == 503 and retry_after):
                    raise RateLimited(resp.status, _retry_after_seconds(retry_after))
                if resp.status != 200:
                    raise FeedError(f"HTTP {resp.status}")
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > MAX_FEED_BYTES:
                        raise FeedError("the feed is larger than 5 MB")
                return FetchResult(bytes(data), resp.headers.get("ETag"),
                                   resp.headers.get("Last-Modified"))
        except aiohttp.ClientConnectorDNSError as e:
            if isinstance(e.os_error, _BlockedAddress):
                raise FeedError(f"{parts.hostname} isn't a public address") from e
            raise FeedError(f"can't resolve {parts.hostname}") from e
        except aiohttp.ClientError as e:
            raise FeedError(f"couldn't connect ({type(e).__name__})") from e
        except TimeoutError as e:
            raise FeedError("timed out") from e
    raise FeedError("too many redirects")


# Zero-width and other invisible characters that ride along when links are
# copied from web pages and documents.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))


def normalize_url(raw: str) -> str:
    """A feed URL as people paste it: drop surrounding whitespace, invisible
    characters, Discord's <...> preview-hiding brackets and quotes, and add
    https:// when no scheme was given."""
    url = raw.translate(_INVISIBLE).strip().strip("<>\"'`").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def comparable_url(url: str) -> str:
    """A feed URL reduced to what identifies the feed, so the same feed written
    differently (http or https, www., a trailing slash, tracking parameters,
    upper-case host) compares equal."""
    parts = urlsplit(clean_url(normalize_url(url)))
    host = (parts.hostname or "").removeprefix("www.")  # hostname is lower-case already
    try:
        port = f":{parts.port}" if parts.port not in (None, 80, 443) else ""
    except ValueError:
        port = ""
    return host + port + parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")


def item_ids(items: list[FeedItem]) -> set[str]:
    """Everything that identifies a feed's items: their keys and links. Two
    feeds sharing any of these carry the same posts, like a blog's /feed and
    /rss addresses."""
    return {i.key for i in items} | {i.link for i in items} | {clean_url(i.link) for i in items}


def parse_feed(data: bytes) -> tuple[str, list[FeedItem]]:
    """Return (feed title, items in document order) for RSS 2.0, RSS 1.0 or Atom."""
    # Real feeds never declare entities; refusing them rules out entity
    # expansion tricks outright (expat also caps them).
    if b"<!ENTITY" in data:
        raise FeedError("the feed declares XML entities, which isn't allowed")
    if data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        raise FeedError("that's a web page, not a feed; look for the site's RSS or Atom link")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise FeedError(f"not valid XML ({e})") from e

    if root.tag == "rss":
        channel = root.find("channel")
        if channel is None:
            raise FeedError("RSS feed has no <channel>")
        title = _text(channel.find("title"))
        items = [_rss_item(el) for el in channel.findall("item")]
    elif root.tag == ATOM + "feed":
        title = _text(root.find(ATOM + "title"))
        items = [_atom_entry(el) for el in root.findall(ATOM + "entry")]
    elif root.tag == RDF + "RDF":
        channel = root.find(RSS1 + "channel")
        title = _text(channel.find(RSS1 + "title")) if channel is not None else ""
        items = [_rdf_item(el) for el in root.findall(RSS1 + "item")]
    else:
        raise FeedError("that's not an RSS or Atom feed")

    unique: dict[str, FeedItem] = {}
    for item in items:
        if item is not None and item.key not in unique:
            unique[item.key] = item
    return title, list(unique.values())


def select_new(items: list[FeedItem], seen: set[str], limit: int) -> list[FeedItem]:
    """Items not posted before, oldest first, at most the newest `limit`."""
    new = [i for i in items if i.key not in seen]
    if all(i.published for i in new):
        new.sort(key=lambda i: i.published)
    else:
        new.reverse()  # feeds list newest first
    return new[-limit:] if limit else []


def format_post(feed_title: str, item: FeedItem, author_id: str | None = None,
                links_line: str | None = None) -> str:
    """"Feed name - Post title" with the link below, which Discord turns into
    a preview card. The same plain format MEE6 used, so the channel looks
    unchanged after the switch, plus a credit for the author's Discord account
    and, below the link, their profile links (profile_links.line)."""
    title = discord.utils.escape_markdown(_one_line(item.title)[:MAX_TITLE_CHARS] or "New post")
    head = f"{discord.utils.escape_markdown(_one_line(feed_title))} - {title}" if feed_title else title
    if author_id:
        head += f" · by <@{author_id}>"
    text = f"{head}\n{clean_url(item.link)}"
    if links_line:
        text += f"\n{links_line}"
    return text[:2000]


def word_list(setting: str | None) -> list[str]:
    """A feed's comma-separated word setting ("Chromium, Edge") as lowercase phrases."""
    return [w.strip().lower() for w in (setting or "").split(",") if w.strip()]


def mentions_any(item: FeedItem, words: list[str]) -> bool:
    title = item.title.lower()
    return any(word in title for word in words)


def wanted(item: FeedItem, include: list[str], exclude: list[str]) -> bool:
    """Exclude words always win; with include words, the title must mention one."""
    if mentions_any(item, exclude):
        return False
    return not include or mentions_any(item, include)


def format_summary(feed_title: str, items: list[FeedItem], highlight: list[str],
                   max_messages: int = 3, author_id: str | None = None) -> list[str]:
    """One summary instead of a burst of posts: a line per item, highlighted
    ones first and bold, links shown without preview cards. Split over at
    most `max_messages` messages; anything beyond that is counted."""
    ordered = sorted(items, key=lambda i: not mentions_any(i, highlight))  # stable: keeps date order
    name = discord.utils.escape_markdown(_one_line(feed_title)) if feed_title else "Feed"
    header = f"{name} - {len(items)} new posts" + (f" · by <@{author_id}>:" if author_id else ":")

    messages, current = [], header
    for index, item in enumerate(ordered):
        # Square brackets would end the masked link early.
        plain = _one_line(item.title)[:MAX_TITLE_CHARS].replace("[", "(").replace("]", ")")
        title = discord.utils.escape_markdown(plain or "New post")
        if mentions_any(item, highlight):
            title = f"**{title}**"
        # <url> inside a masked link stops Discord from adding a preview card per line.
        line = f"• [{title}](<{clean_url(item.link)}>)"
        if len(current) + len(line) + 1 > 1900:
            messages.append(current)
            if len(messages) == max_messages:
                messages[-1] += f"\n…and {len(ordered) - index} more."
                return messages
            current = line
        else:
            current = f"{current}\n{line}"
    messages.append(current)
    return messages


def _retry_after_seconds(value: str | None) -> int:
    """Retry-After as seconds (it's either a number or an HTTP date), kept
    between 1 minute and 6 hours. Missing or unreadable: 30 minutes."""
    seconds = 30 * 60
    if value and value.strip().isdigit():
        seconds = int(value)
    elif value:
        try:
            seconds = int((email.utils.parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass
    return max(60, min(seconds, 6 * 3600))


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _one_line(s: str) -> str:
    return " ".join(s.split())


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)  # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value)         # Atom / dc:date: ISO 8601
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _item(key: str, title: str, link: str, published: str) -> FeedItem | None:
    if not link.startswith(("http://", "https://")):
        return None
    return FeedItem(key or link, title, link, _date(published))


def _rss_item(el) -> FeedItem | None:
    link = _text(el.find("link"))
    guid = _text(el.find("guid"))
    if not link and guid.startswith(("http://", "https://")):
        link = guid
    return _item(guid, _text(el.find("title")), link, _text(el.find("pubDate")) or _text(el.find(DC + "date")))


def _atom_entry(el) -> FeedItem | None:
    link = ""
    for candidate in el.findall(ATOM + "link"):
        if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
            link = candidate.get("href")
            break
    published = _text(el.find(ATOM + "published")) or _text(el.find(ATOM + "updated"))
    return _item(_text(el.find(ATOM + "id")), _text(el.find(ATOM + "title")), link, published)


def _rdf_item(el) -> FeedItem | None:
    link = _text(el.find(RSS1 + "link"))
    return _item(el.get(RDF + "about", ""), _text(el.find(RSS1 + "title")), link, _text(el.find(DC + "date")))
