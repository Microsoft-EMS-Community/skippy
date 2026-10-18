# ============================================================
# link_cleaner.py - Strip tracking params from URLs posted in chat
# ============================================================

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, unquote_plus

import aiohttp

URL_RE = re.compile(r"https?://\S+")

# Known link shorteners. Resolved via a live redirect follow before cleaning,
# since the tracking cruft only shows up on the real destination URL.
# Kept as a fixed allowlist rather than resolving arbitrary domains, so the
# bot never makes outbound requests to hosts it doesn't already trust.
SHORTENER_DOMAINS = {
    "bit.ly", "tinyurl.com", "t.co", "lnkd.in", "goo.gl", "ow.ly", "buff.ly",
    "rebrand.ly", "is.gd", "shorturl.at", "cutt.ly", "rb.gy", "s.id", "tiny.cc",
    "amzn.to", "spoti.fi", "msems.community", "aka.ms",
}
SHORTENER_TIMEOUT = aiohttp.ClientTimeout(total=5)
MAX_REDIRECT_HOPS = 5


class UnsafeRedirect(Exception):
    """Raised when a shortener redirect points at a private/internal host
    (SSRF attempt), so callers can warn instead of silently dropping it."""
    def __init__(self, hop_url: str):
        self.hop_url = hop_url
        super().__init__(f"blocked redirect to internal host: {hop_url}")


def _is_public_host(hostname: str) -> bool:
    """Resolve hostname and reject anything pointing at private/loopback/
    link-local/reserved space, so a malicious shortener can't redirect the
    bot into an SSRF against internal infra (e.g. cloud metadata endpoints)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
    return True

TRACKING_PARAMS = {
    # UTM
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name", "utm_social", "utm_social-type",
    # Click IDs
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "twclid", "ttclid",
    "igshid", "gbraid", "wbraid", "yclid",
    # Referral / affiliate
    "ref", "ref_src", "ref_url", "referrer", "spm", "affid",
    # Mailchimp / marketing platforms
    "mc_cid", "mc_eid",
    # Misc analytics
    "_hsenc", "_hsmi", "hsCtaTracking", "vero_id", "vero_conv",
    "si", "s_kwcid", "ns_source", "ns_mchannel", "ns_campaign", "ns_linkname",
    # Instagram / Meta share tracking
    "igsh", "igshid", "igsi",
    # TikTok
    "_r", "_t", "share_item_id",
    # LinkedIn
    "trk", "trkinfo",
    # Pinterest
    "epik",
    # Snapchat
    "share_id",
}

# Params that are only safe to strip on specific domains, since the same
# param name can carry real meaning elsewhere (e.g. "s" or "t" as a legit
# query param on other sites).
DOMAIN_TRACKING_PARAMS = {
    "amazon.": {"ref", "ref_", "tag", "crid", "sprefix", "qid", "th", "psc",
                "pd_rd_i", "pd_rd_r", "pd_rd_w", "pd_rd_wg",
                "pf_rd_p", "pf_rd_r", "pf_rd_s", "pf_rd_t", "pf_rd_i", "pf_rd_m"},
    "twitter.com": {"s", "t"},
    "x.com": {"s", "t"},
    "reddit.com": {"share_id", "context"},
    "google.": {"ved", "uact", "sa", "source", "ei", "gs_lcp", "gs_lp",
                "oq", "aqs", "sourceid", "ie", "sxsrf", "sca_esv", "gs_ssp",
                # Google Maps app share links
                "entry", "coh", "g_ep", "skid", "g_st"},
    "linkedin.com": {"rcm", "midtoken", "midsig", "trkemail",
                     "highlightedupdateurn", "highlightedupdatetype", "origin",
                     "lipi", "licu", "trackingid", "refid",
                     "originalsubdomain", "original_referer"},
    "youtube.com": {"feature"},
    "youtu.be": {"feature"},
    "hotels.com": {"x_pwa", "rfrr", "pwa_ts", "referrerUrl", "searchId", "userIntent"},
    "expedia.": {"x_pwa", "rfrr", "pwa_ts", "referrerUrl", "searchId", "userIntent"},
    "booking.com": {"label", "aid"},
    "bing.com": {"form", "sk", "sp", "sc", "qs", "cvid", "pq"},
    "etsy.com": {"click_key", "click_sum"},
}


def _domain_extra_params(netloc: str) -> set[str]:
    host = netloc.lower()
    extra = set()
    for domain, params in DOMAIN_TRACKING_PARAMS.items():
        if domain in host:
            extra |= params
    return extra


def _clean_query(query: str, extra_params: set[str]) -> str:
    # Filter the raw "key=value" segments instead of decoding and re-encoding
    # the query, so kept params keep their exact original encoding. Re-encoding
    # rewrites characters like "," or ":" and made untouched URLs look cleaned.
    drop = {p.lower() for p in TRACKING_PARAMS | extra_params}
    kept = [seg for seg in query.split("&")
            if unquote_plus(seg.split("=", 1)[0]).lower() not in drop]
    return "&".join(kept)


def clean_url(url: str) -> str:
    parts = urlsplit(url)

    # Unwrap Google's /url?q=<real url> redirect wrapper (e.g. from shared search results)
    if "google." in parts.netloc.lower() and parts.path in ("/url", "/amp/s"):
        qs = dict(parse_qsl(parts.query))
        target = qs.get("q") or qs.get("url")
        if target:
            return clean_url(target)

    path = parts.path
    new_query = parts.query
    # Amazon product links: only /dp/ASIN or /gp/product/ASIN matters. The
    # product-name slug before it and everything after it (/ref=...) is cruft.
    if "amazon." in parts.netloc.lower():
        m = re.search(r"((?:/[a-z]{2})?/(?:dp|gp/product)/[A-Z0-9]{10})(?:/|$)", path)
        if m:
            path = m.group(1)
    if parts.query:
        extra_params = _domain_extra_params(parts.netloc)
        new_query = _clean_query(parts.query, extra_params)
    return urlunsplit((parts.scheme, parts.netloc, path, new_query, parts.fragment))


def _extract_urls(text: str) -> list[str]:
    urls = []
    for url in URL_RE.findall(text):
        # Trim common trailing punctuation that isn't part of the URL
        while url and url[-1] in ").,!?>\"'":
            url = url[:-1]
        urls.append(url)
    return urls


def _is_shortener(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in SHORTENER_DOMAINS)


def has_shortener_link(text: str) -> bool:
    """True if text contains a known short-link domain that needs a live redirect check."""
    return any(_is_shortener(url) for url in _extract_urls(text))


def clean_links(text: str) -> list[tuple[str, str]]:
    """Return [(original, cleaned)] for every non-shortener URL in text whose tracking params were stripped."""
    changed = []
    for url in _extract_urls(text):
        if _is_shortener(url):
            continue
        cleaned = clean_url(url)
        if cleaned != url:
            changed.append((url, cleaned))
    return changed


async def _resolve_shortener(session: aiohttp.ClientSession, url: str) -> str | None:
    # Follow redirects manually (allow_redirects=False) so each hop's host
    # can be checked against private/internal IP ranges before we request it.
    # A malicious shortener could otherwise 302 the bot at cloud metadata
    # endpoints or internal services.
    current = url
    for _ in range(MAX_REDIRECT_HOPS):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        if not _is_public_host(parts.hostname):
            raise UnsafeRedirect(current)
        for method in (session.head, session.get):
            try:
                async with method(current, allow_redirects=False, timeout=SHORTENER_TIMEOUT) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            return None
                        current = str(aiohttp.client.URL(current).join(aiohttp.client.URL(location)))
                        break
                    if resp.status < 400:
                        return current
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        else:
            return None
    return None


async def clean_links_async(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Like clean_links, but also follows known shortener domains to their real
    destination (live HTTP request) before cleaning, since shorteners hide
    tracking params behind an opaque token.

    Returns (changed, blocked): `changed` is [(original, cleaned)] as before;
    `blocked` lists original shortener URLs whose redirect pointed at a
    private/internal host and was refused (possible SSRF attempt)."""
    changed = clean_links(text)
    blocked = []

    shortener_urls = [u for u in _extract_urls(text) if _is_shortener(u)]
    if not shortener_urls:
        return changed, blocked

    async with aiohttp.ClientSession() as session:
        resolved = await asyncio.gather(
            *(_resolve_shortener(session, u) for u in shortener_urls),
            return_exceptions=True,
        )

    for orig, final in zip(shortener_urls, resolved):
        if isinstance(final, UnsafeRedirect):
            blocked.append(orig)
            continue
        if isinstance(final, BaseException) or not final:
            continue
        cleaned = clean_url(final)
        if cleaned != orig:
            changed.append((orig, cleaned))

    return changed, blocked
