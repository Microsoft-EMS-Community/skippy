# ============================================================
# feed_registry.py - The feeds Skippy follows: adding one after checking
# it, spotting the same blog at another address, each member's one blog,
# and what a feed is called.
# ============================================================

import discord

import db
import feed_reader
from config import FEED_CHANNELS
from feed_reader import FeedError

DEFAULT_CHANNEL_ID = next(iter(FEED_CHANNELS.values()))


def feed_name(feed: dict, fresh_title: str | None = None) -> str:
    """What posts and messages call a feed: the name mods gave it, else the
    title the feed gives itself (fresh_title, if just fetched), else its URL."""
    return feed.get("display_name") or fresh_title or feed["title"] or feed["url"]


def shown_name(feed: dict) -> str:
    """feed_name, safe to put in bold in a Discord message."""
    return discord.utils.escape_markdown(feed_name(feed))


async def create_feed(guild: discord.Guild, url: str, channel_id: int, author_id: str | None, added_by: str):
    """Check and add a feed; shared by /feed add and approving a suggestion.
    Returns (feed id, title, current items, channel). Raises
    FeedError with a message for mods, also when the same blog already
    posts in that channel (sqlite3.IntegrityError if one was added at
    the same moment)."""
    if author_id and (blog := await feed_by_author(str(guild.id), author_id)):
        raise FeedError(f"<@{author_id}> already has a blog on the feed: feed #{blog['id']} "
                        f"**{shown_name(blog)}**. Each member has one; to swap, remove that feed or "
                        "clear its author first")
    channel = guild.get_channel(channel_id) if channel_id in FEED_CHANNELS.values() else None
    if not isinstance(channel, discord.TextChannel):
        raise FeedError(f"feed channel {channel_id} doesn't exist anymore; update FEED_CHANNELS in config.py")
    perms = channel.permissions_for(guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        raise FeedError(f"I can't post in {channel.mention}: I need View Channel, Send Messages and Embed Links there")
    async with feed_reader.session() as http:
        result = await feed_reader.fetch_feed(http, url)
        title, items = feed_reader.parse_feed(result.data, url)
    duplicate = await duplicate_of(str(guild.id), url, feed_reader.item_ids(items), channel.id)
    if duplicate:
        raise FeedError(f"that's the same blog as feed #{duplicate['id']} **{shown_name(duplicate)}**, "
                        f"which already posts in {channel.mention}")
    feed_id = await db.add_feed(str(guild.id), str(channel.id), url, title, added_by)
    # Everything already in the feed counts as seen, so adding a feed
    # doesn't dump its whole history into the channel.
    await db.mark_feed_items_seen(feed_id, [i.key for i in items])
    await db.record_feed_check(feed_id, None, result.etag, result.last_modified, title)
    if author_id:
        await link_author(feed_id, str(guild.id), author_id, added_by)
    return feed_id, title, items, channel


async def duplicate_of(guild_id: str, url: str, item_ids: set[str], channel_id: int | None = None,
                       ignore_feed: int | None = None) -> dict | None:
    """The feed that a new one would duplicate: the same feed URL written
    differently, or a feed that already has any of its posts (the same
    blog at another address, like /feed and /rss). Only feeds posting in
    channel_id count when it's given."""
    feeds = [f for f in await db.get_feeds(guild_id) if f["id"] != ignore_feed
             and (channel_id is None or int(f["channel_id"]) == channel_id)]
    same_url = feed_reader.comparable_url(url)
    for feed in feeds:
        if feed_reader.comparable_url(feed["url"]) == same_url:
            return feed
    sharing = await db.feeds_with_items(item_ids) if item_ids else set()
    return next((f for f in feeds if f["id"] in sharing), None)


async def feed_by_author(guild_id: str, author_id: str, ignore_feed: int | None = None) -> dict | None:
    """The feed credited to this member, if any: each member has one blog on the feed."""
    return next((f for f in await db.get_feeds(guild_id)
                 if f["author_id"] == author_id and f["id"] != ignore_feed), None)


async def link_author(feed_id: int, guild_id: str, author_id: str, linked_by: str):
    """Credit a member for a feed, and put them on the RSS watchlist so
    their activity in the server is tracked like any watched member's."""
    await db.update_feed(feed_id, guild_id, author_id=author_id)
    if not await db.is_watched(author_id):  # re-adding would reset when tracking started
        await db.add_to_watchlist(author_id, guild_id, linked_by)
