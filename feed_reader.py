# ============================================================
# feed_reader.py - Fetch and parse RSS/Atom feeds for /feed
# No bot, command or database code here, so it can be tested on its own.
# ============================================================

import email.utils
import html
import re
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
import discord
from yarl import URL

from link_cleaner import _BlockedAddress, _is_public_host, _PublicOnlyResolver, clean_url

USER_AGENT = "Skippy feed reader (+https://github.com/Microsoft-EMS-Community/skippy)"
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECT_HOPS = 5
MAX_TITLE_CHARS = 200
MAX_SUMMARY_CHARS = 200  # about two lines on a phone

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
MEDIA = "{http://search.yahoo.com/mrss/}"


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
    # For the post's card (post_card). Kept as the feed has them, and only
    # worked out for the few items that get posted.
    summary: str = ""           # HTML, usually the post's first lines
    content: str = ""           # HTML, often the whole post
    image: str = ""             # a picture the feed names for the post
    writer: str = ""            # the name the feed credits


@dataclass
class PagePreview:
    """What a post's web page offers for link previews: the picture and
    description in its og: and twitter: meta tags."""
    image: str = ""
    description: str = ""


@dataclass
class FetchResult:
    data: bytes | None          # None means 304 Not Modified
    etag: str | None
    last_modified: str | None


# Python's standard TLS settings (certificates checked as usual), not
# aiohttp's own: aiohttp announces it only speaks HTTP/1.1 (ALPN), which
# WordPress.com's bot check treats as a bot and answers with a "Checking your
# browser" page, so blogs hosted there (EMS Route, Intune IRL, ...) couldn't
# be read.
_TLS = ssl.create_default_context()


def session() -> aiohttp.ClientSession:
    """HTTP session whose connections can only reach public addresses
    (same DNS-rebinding-safe resolver as link_cleaner)."""
    connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver(), ssl=_TLS)
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


async def fetch_preview(http: aiohttp.ClientSession, url: str) -> PagePreview:
    """The picture and description a post's page offers for link previews,
    the same ones Discord's own preview card used. Fetched like a feed (the
    same address checks and size limit). Empty when the page can't be
    fetched; the card then uses what the feed has."""
    try:
        data = (await fetch_feed(http, url)).data or b""
    except FeedError:
        return PagePreview()
    page = data.decode("utf-8", "replace")
    end = page.lower().find("</head>")
    tags = _MetaTags()
    tags.feed(page[:end] if end >= 0 else page)
    image = tags.first("og:image", "og:image:url", "twitter:image", "twitter:image:src")
    return PagePreview(_web_address(image, url), _one_line(tags.first("og:description", "twitter:description",
                                                                      "description")))


class _MetaTags(HTMLParser):
    """A page's <meta> tags by property or name (the first of each)."""

    def __init__(self):
        super().__init__()
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            attrs = dict(attrs)
            name = (attrs.get("property") or attrs.get("name") or "").lower()
            if name and attrs.get("content"):
                self.found.setdefault(name, attrs["content"])

    def first(self, *names: str) -> str:
        return next((self.found[n] for n in names if n in self.found), "")


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


def parse_feed(data: bytes, base_url: str = "") -> tuple[str, list[FeedItem]]:
    """Return (feed title, items in document order) for RSS 2.0, RSS 1.0 or
    Atom. Links written without the site's address ("/2026/10/post/", as
    some Hugo sites do) are read against base_url, the feed's own address."""
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
        items = [_rss_item(el, base_url) for el in channel.findall("item")]
    elif root.tag == ATOM + "feed":
        title = _text(root.find(ATOM + "title"))
        items = [_atom_entry(el, base_url) for el in root.findall(ATOM + "entry")]
    elif root.tag == RDF + "RDF":
        channel = root.find(RSS1 + "channel")
        title = _text(channel.find(RSS1 + "title")) if channel is not None else ""
        items = [_rdf_item(el, base_url) for el in root.findall(RSS1 + "item")]
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


def post_card(feed_title: str, item: FeedItem, preview: PagePreview | None = None,
              author_id: str | None = None, links_line: str | None = None) -> discord.Embed:
    """A post as one card, like MEE6's: the blog's name on top, the title
    linking to the post, a short summary, who wrote it and their profile links
    (profile_links.line), then the picture. Nothing outside the card, so it
    pings nobody. The picture and summary are the page's own link-preview
    ones, else what the feed has."""
    preview = preview or PagePreview()
    url = _web_address(clean_url(item.link), item.link)
    card = discord.Embed(title=_one_line(item.title)[:256] or "New post", url=url or None,
                         color=discord.Color.blurple())
    if feed_title:
        card.set_author(name=_one_line(feed_title)[:256])
    lines = []
    summary = _shorten(preview.description or _plain_text(item.summary) or _plain_text(item.content))
    if summary:
        lines += [discord.utils.escape_mentions(discord.utils.escape_markdown(summary)), ""]
    if author_id:
        lines.append(f"by <@{author_id}>")
    elif item.writer:
        lines.append(f"by {discord.utils.escape_mentions(discord.utils.escape_markdown(_one_line(item.writer)[:100]))}")
    if links_line:
        lines.append(links_line)
    card.description = "\n".join(lines).strip() or None
    image = preview.image or item.image or _first_image(item.link, item.content, item.summary)
    if image:
        card.set_image(url=image)
    return card


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


def _item(key: str, title: str, link: str, published: str, base: str, **card) -> FeedItem | None:
    if base and link and not link.startswith(("http://", "https://")):
        link = urljoin(base, link)
    if not link.startswith(("http://", "https://")):
        return None
    if card.get("image"):
        card["image"] = _web_address(card["image"], link)
    return FeedItem(key or link, title, link, _date(published), **card)


def _rss_item(el, base: str) -> FeedItem | None:
    link = _text(el.find("link"))
    guid = _text(el.find("guid"))
    if not link and guid.startswith(("http://", "https://")):
        link = guid
    # <author> is an e-mail address, with the name in brackets if at all.
    named = re.search(r"\(([^)]+)\)", _text(el.find("author")))
    return _item(guid, _text(el.find("title")), link, _text(el.find("pubDate")) or _text(el.find(DC + "date")),
                 base, summary=_text(el.find("description")), content=_text(el.find(CONTENT + "encoded")),
                 image=_media_image(el), writer=_text(el.find(DC + "creator")) or (named.group(1) if named else ""))


def _atom_entry(el, base: str) -> FeedItem | None:
    link = ""
    for candidate in el.findall(ATOM + "link"):
        if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
            link = candidate.get("href")
            break
    published = _text(el.find(ATOM + "published")) or _text(el.find(ATOM + "updated"))
    return _item(_text(el.find(ATOM + "id")), _text(el.find(ATOM + "title")), link, published, base,
                 summary=_text(el.find(ATOM + "summary")), content=_text(el.find(ATOM + "content")),
                 image=_media_image(el), writer=_text(el.find(f"{ATOM}author/{ATOM}name")))


def _rdf_item(el, base: str) -> FeedItem | None:
    link = _text(el.find(RSS1 + "link"))
    return _item(el.get(RDF + "about", ""), _text(el.find(RSS1 + "title")), link, _text(el.find(DC + "date")), base,
                 summary=_text(el.find(RSS1 + "description")), writer=_text(el.find(DC + "creator")))


def _media_image(el) -> str:
    """The picture a feed names for an item: Media RSS (Ghost, WordPress
    plugins, YouTube) or an image enclosure (Substack). Videos don't count."""
    for tag in (MEDIA + "content", MEDIA + "thumbnail", "enclosure", ATOM + "link"):
        for media in el.iter(tag):
            url = media.get("href") if tag == ATOM + "link" else media.get("url")
            kind = media.get("type") or ""
            if tag == ATOM + "link" and media.get("rel") != "enclosure":
                continue
            if url and (kind.startswith("image/") or media.get("medium") == "image"
                        or (tag == MEDIA + "thumbnail") or (not kind and _IMAGE_FILE.search(url))):
                return url
    return ""


_IMAGE_FILE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", re.I)
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
# Avatars, smileys and tracking pixels that blogs put in their posts.
_NOT_A_PICTURE = ("gravatar.com", "/emoji/", "s.w.org/images", "feeds.feedburner.com", "stats.wp.com",
                  "pixel.wp.com", "/wp-includes/images/")


def _first_image(base: str, *html_texts: str) -> str:
    """The first real picture in a post's HTML, as an absolute address."""
    for text in html_texts:
        for tag in _IMG_TAG.findall(text or ""):
            src = _attribute(tag, "src")
            if not src or any(junk in src for junk in _NOT_A_PICTURE):
                continue
            if _attribute(tag, "width") in ("0", "1") or _attribute(tag, "height") in ("0", "1"):
                continue
            if url := _web_address(src, base):
                return url
    return ""


def _attribute(tag: str, name: str) -> str:
    found = re.search(rf"\s{name}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", tag, re.I)
    return html.unescape(next(g for g in found.groups() if g is not None)) if found else ""


def _web_address(raw: str, base: str) -> str:
    """An address made absolute and safe to hand to Discord (spaces and
    other letters encoded), or "" if it isn't a usable http(s) one."""
    url = urljoin(base, raw.strip()) if raw else ""
    if urlsplit(url).scheme not in ("http", "https") or len(url) > 2000:
        return ""
    return quote(url, safe=":/?#[]@!$&'()*+,;=%~")


def _plain_text(markup: str) -> str:
    """A post's HTML summary as one line of text, without the "The post …
    appeared first on …" footer WordPress adds; a trailing [...] or "Continue
    reading" becomes …."""
    if not markup:
        return ""
    text = re.sub(r"<(script|style)\b.*?</\1\s*>|<[^>]+>", " ", markup, flags=re.S | re.I)
    text = _one_line(html.unescape(text))
    text = re.sub(r"\s*The post .{1,300}? appeared first on .{1,200}$", "", text)
    text, cut = re.subn(r"\s*(\[(…|\.\.\.)\]|Continue reading\b.*|Read more\b.*)$", "", text)
    return text + ("…" if cut and not text.endswith((".", "!", "?", "…")) else "")


def _shorten(text: str, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Text cut at a word to at most `limit` characters, with … if cut."""
    text = _one_line(text)
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-–—") + "…"
