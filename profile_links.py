# ============================================================
# profile_links.py - Authors' LinkedIn, X, YouTube and GitHub links
# Shown under their posts in the community feed. Only real profile
# addresses on those sites are accepted, so nobody can put other links in
# the feed. No bot, command or database code here.
# ============================================================

import re
from urllib.parse import unquote, urlsplit

from feed_reader import normalize_url

SITES = {  # in the order they're shown
    "linkedin": "LinkedIn",
    "x": "X",
    "youtube": "YouTube",
    "github": "GitHub",
}

EXAMPLES = {
    "linkedin": "linkedin.com/in/your-name",
    "x": "x.com/yourhandle or @yourhandle",
    "youtube": "youtube.com/@yourchannel or @yourchannel",
    "github": "github.com/yourname",
}

# How each site's option reads on commands that take profile links.
OPTION_TEXT = {site: f"{label} profile, like {EXAMPLES[site]}" for site, label in SITES.items()}

_HOSTS = {
    "linkedin": {"linkedin.com"},
    "x": {"x.com", "twitter.com"},
    "youtube": {"youtube.com"},
    "github": {"github.com"},
}

_LINKEDIN = re.compile(r"/in/([\w\-%.]{2,100})", re.ASCII)
_X = re.compile(r"/@?([A-Za-z0-9_]{1,15})")
_X_RESERVED = {"home", "i", "search", "explore", "intent", "share", "settings", "messages", "notifications",
               "login", "signup", "tos", "privacy", "hashtag"}
_YOUTUBE = re.compile(r"/(@[\w.\-]{3,30}|channel/UC[\w\-]{22})", re.ASCII)
_GITHUB = re.compile(r"/([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})")
_GITHUB_RESERVED = {"orgs", "topics", "explore", "marketplace", "settings", "login", "about", "features",
                    "pricing", "sponsors", "search", "notifications", "issues", "pulls"}


def read_all(**given: str | None) -> dict[str, str]:
    """Profile links as given per site (None when left out), checked and in
    their standard form. Raises ValueError with a message for the member."""
    return {site: normalize(site, value) for site, value in given.items() if value}


def normalize(site: str, raw: str) -> str:
    """The profile link for `site`, from a URL or a handle as people type it,
    in one standard form. Raises ValueError with a message for the member
    when it isn't a profile on that site."""
    value = raw.strip()
    # A handle on its own ("@name" or "name") rather than a link.
    if "/" not in value and ":" not in value and (value.startswith("@") or "." not in value):
        value = {"linkedin": "linkedin.com/in/", "x": "x.com/", "youtube": "youtube.com/@",
                 "github": "github.com/"}[site] + value.lstrip("@")
    parts = urlsplit(normalize_url(value))
    host = (parts.hostname or "").removeprefix("www.").removeprefix("mobile.").removeprefix("m.")
    if host.endswith(".linkedin.com"):  # country subdomains, like dk.linkedin.com
        host = "linkedin.com"
    if parts.scheme not in ("http", "https") or host not in _HOSTS[site]:
        raise ValueError(_wrong(site))
    path = parts.path.rstrip("/") or "/"
    if site == "linkedin" and (m := _LINKEDIN.fullmatch(path)):
        return f"https://www.linkedin.com/in/{m.group(1)}"
    if site == "x" and (m := _X.fullmatch(path)) and m.group(1).lower() not in _X_RESERVED:
        return f"https://x.com/{m.group(1)}"
    if site == "youtube" and (m := _YOUTUBE.fullmatch(path)):
        return f"https://www.youtube.com/{m.group(1)}"
    if site == "github" and (m := _GITHUB.fullmatch(path)) and m.group(1).lower() not in _GITHUB_RESERVED:
        return f"https://github.com/{m.group(1)}"
    raise ValueError(_wrong(site))


def line(links: dict[str, str], emojis: dict[str, str]) -> str:
    """The line under a post: each site's icon followed by the handle,
    linking to the profile without adding its own preview card. (Discord
    doesn't make server emojis clickable in messages, so the handle is the
    link.) Empty when there are no links."""
    return "  ".join(f"{emojis.get(site, '🔗')} [{_escape(handle(links[site]))}](<{links[site]}>)"
                     for site in SITES if links.get(site))


def handle(url: str) -> str:
    """How a profile is shown: @name on X and YouTube, the name on LinkedIn
    and GitHub, "channel" for a YouTube channel without a handle. Letters
    like ë are shown as letters, not as the %C3%AB in the address."""
    parts = urlsplit(url)
    name = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    if parts.hostname == "x.com":
        return "@" + name
    if parts.path.startswith("/channel/"):
        return "channel"
    return name


def _escape(text: str) -> str:
    """Handles can hold _ (X), which Discord would read as italics."""
    return re.sub(r"([_*~`|\\])", r"\\\1", text)


def _wrong(site: str) -> str:
    article = "an" if site == "x" else "a"
    return f"that doesn't look like {article} {SITES[site]} profile; use {EXAMPLES[site]}"
