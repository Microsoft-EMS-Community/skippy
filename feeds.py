# ============================================================
# feeds.py - /feed commands and the loop that posts new feed items
# Fetching and parsing live in feed_reader.py.
# ============================================================

import asyncio
import json
import logging
from collections import Counter
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from datetime import time as day_time
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import feed_reader
import profile_links
from commands import is_rss_admin
from config import (FEED_ALERT_CHANNEL_ID, FEED_CHANNELS, FEED_COMMUNITY_CHANNEL_ID, FEED_POLL_MINUTES,
                    FEED_ROUNDUP_CHANNEL_ID, FEED_ROUNDUP_TIMEZONE, FEED_SUGGESTIONS_CHANNEL_ID,
                    FEED_LINK_EMOJIS, FEED_THREAD_CHANNEL_IDS, FEED_VOTE_EMOJI, RSS_ADMIN_ROLE_ID)
from feed_reader import FeedError, RateLimited

logger = logging.getLogger("followup-bot")

ALERT_AFTER_FAILURES = max(1, 60 // FEED_POLL_MINUTES)  # an hour of failed checks
CONCURRENT_CHECKS = 5     # feeds checked at once, so slow or dead ones can't hold up the rest
MAX_PENDING_SUGGESTIONS = 3  # per member, so nobody can flood the mods' queue
REFUSAL_REPEAT_SECONDS = 3600  # a member retrying the same refused blog is reported to the mods once an hour
THREAD_ARCHIVE_MINUTES = 4320  # discussion threads on posts go quiet after 3 days
ACTIVITY_DAYS = 30        # activity window, and the grace period for newly linked authors

# The activity rule for feeds linked to an author, switched by mods with
# /feed activity: phase it in by watching first, then reminding, then pausing.
ACTIVITY_MODES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="watch: only show authors' activity", value="watch"),
    app_commands.Choice(name="warn: remind inactive authors, keep posting", value="warn"),
    app_commands.Choice(name="enforce: pause inactive authors' posts", value="enforce"),
]
ACTIVITY_BADGES = {"active": "🟢", "new": "🆕", "inactive": "🔴", "untracked": "⚪"}
ROUNDUP_TZ = ZoneInfo(FEED_ROUNDUP_TIMEZONE)
ROUNDUP_AT = day_time(9, 0)  # Mondays, local time in ROUNDUP_TZ
ROUNDUP_ACTIONS = [
    app_commands.Choice(name="preview (only you see it)", value="preview"),
    app_commands.Choice(name="post it now", value="post"),
    app_commands.Choice(name="weekly roundup on", value="on"),
    app_commands.Choice(name="weekly roundup off", value="off"),
]


def build_roundup(posts: list[dict], now: datetime, votes: dict[str, int] | None = None) -> discord.Embed | None:
    """The weekly roundup of the community channel, for #general: last week's
    posts grouped per blogger (in the order they first posted), plus the
    week's three most-voted posts by
    FEED_VOTE_EMOJI reactions. None for a quiet week. Mentions in embeds never
    ping, so crediting people here is silent."""
    votes = votes or {}
    # The same article can be posted twice (say, by /feed test): keep the copy
    # with the most votes, in its first position.
    best: dict[str, dict] = {}
    for p in posts:
        kept = best.get(p["link"])
        if kept is None or votes.get(p["message_url"] or "", 0) > votes.get(kept["message_url"] or "", 0):
            best[p["link"]] = p
    posts = [p for p in posts if best[p["link"]] is p]
    if not posts:
        return None
    groups: dict[str, dict] = {}
    for p in posts:  # oldest first, so groups come out in order of each blogger's first post
        key = p["author_id"] or f"feed-{p['feed_id']}"
        groups.setdefault(key, {"author_id": p["author_id"], "feed_title": p["feed_title"] or "", "posts": []})
        groups[key]["posts"].append(p)

    def who(author_id, feed_title):
        return f"<@{author_id}>" if author_id else f"**{discord.utils.escape_markdown(feed_title or '')}**"

    def linked(p):
        title = discord.utils.escape_markdown(" ".join(p["title"].split())[:150]).replace("[", "(").replace("]", ")")
        return f"[{title}](<{p['link']}>)"

    header = (f"**{len(posts)} new post{'s' if len(posts) != 1 else ''} from {len(groups)} "
              f"blogger{'s' if len(groups) != 1 else ''}** in <#{FEED_COMMUNITY_CHANNEL_ID}> this week. "
              "Thanks for writing! 💙\n")
    body, shown = "", 0
    for group in groups.values():
        block = f"\n{who(group['author_id'], group['feed_title'])}"
        for p in group["posts"]:
            block += f"\n• {linked(p)}"
        if len(header) + len(body) + len(block) > 3900:  # embed descriptions stop at 4096
            body += f"\n\n…and {len(posts) - shown} more posts."
            break
        body += block
        shown += len(group["posts"])

    week = (now - timedelta(days=1)).isocalendar().week  # the week that just ended
    embed = discord.Embed(title=f"📰 Community roundup · week {week}", description=header + body,
                          color=discord.Color.blurple())
    voted = sorted((p for p in posts if votes.get(p["message_url"] or "", 0) > 0),
                   key=lambda p: -votes[p["message_url"]])  # stable: ties go to the earlier post
    if voted:
        lines = [f"{medal} {linked(p)} by {who(p['author_id'], p['feed_title'])} · {votes[p['message_url']]} {FEED_VOTE_EMOJI}"
                 for medal, p in zip(["🥇", "🥈", "🥉"], voted)]
        # Server emojis render in field values, not in field names or footers.
        embed.add_field(name="🏆 Community favourites this week", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="Vote for favourites by reacting to posts · Want your blog in here? /blog suggest")
    return embed


# A fixed list, not any channel: feeds only ever post where they're meant to.
# Values are strings because channel IDs are too big for Discord's integers.
CHANNEL_CHOICES = [app_commands.Choice(name=name, value=str(cid)) for name, cid in FEED_CHANNELS.items()]
DEFAULT_CHANNEL_ID = next(iter(FEED_CHANNELS.values()))
# The author's profile links, as options on /feed add, /feed edit, /blog suggest and /blog links.
LINK_OPTIONS = {site: f"{label} profile, like {profile_links.EXAMPLES[site]}"
                for site, label in profile_links.SITES.items()}
LINK_CHOICES = ([app_commands.Choice(name=label, value=site) for site, label in profile_links.SITES.items()]
                + [app_commands.Choice(name="all of them", value="all")])

CLEARABLE_SETTINGS = [
    app_commands.Choice(name="author", value="author_id"),
    app_commands.Choice(name="name (use the feed's own title)", value="display_name"),
    app_commands.Choice(name="include words", value="include_words"),
    app_commands.Choice(name="exclude words", value="exclude_words"),
    app_commands.Choice(name="highlight words", value="highlight_words"),
] + [app_commands.Choice(name=f"{label} link", value=f"link:{site}") for site, label in profile_links.SITES.items()]


def read_links(**given: str | None) -> dict[str, str]:
    """Profile links given as command options (None when left out), checked
    and in their standard form. Raises ValueError with a message for the member."""
    return {site: profile_links.normalize(site, value) for site, value in given.items() if value}


def feed_name(feed: dict, fresh_title: str | None = None) -> str:
    """What posts and messages call a feed: the name mods gave it, else the
    title the feed gives itself (fresh_title, if just fetched), else its URL."""
    return feed.get("display_name") or fresh_title or feed["title"] or feed["url"]


def is_mod(member) -> bool:
    return any(r.id == RSS_ADMIN_ROLE_ID for r in getattr(member, "roles", []))


class SuggestionButton(discord.ui.DynamicItem[discord.ui.Button],
                       template=r"feed-suggestion:(?P<action>approve|reject):(?P<id>[0-9]+)"):
    """Approve/Reject on a suggestion card. The suggestion ID lives in the
    button's custom_id, so the buttons keep working after Skippy restarts."""

    def __init__(self, action: str, suggestion_id: int):
        approve = action == "approve"
        super().__init__(discord.ui.Button(
            label="Approve" if approve else "Reject", emoji="✅" if approve else "❌",
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
            custom_id=f"feed-suggestion:{action}:{suggestion_id}"))
        self.action, self.suggestion_id = action, suggestion_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_mod(interaction.user):
            return True
        await interaction.response.send_message(f"Only <@&{RSS_ADMIN_ROLE_ID}>s can decide on suggestions.",
                                                ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("FeedCommands")
        if self.action == "approve":
            await cog.approve_suggestion(interaction, self.suggestion_id)
        else:
            await interaction.response.send_modal(RejectReason(self.suggestion_id))


class RejectReason(discord.ui.Modal, title="Reject suggestion"):
    reason = discord.ui.TextInput(label="Reason (sent to the member, optional)", required=False,
                                  style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, suggestion_id: int):
        super().__init__()
        self.suggestion_id = suggestion_id

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("FeedCommands")
        await cog.reject_suggestion(interaction, self.suggestion_id, self.reason.value.strip() or None)


class FeedCommands(commands.Cog):
    """Post new items from RSS/Atom feeds into channels."""

    # Mods: hidden from members without Manage Roles (which the Moderator role
    # has). That's only visibility; is_rss_admin is the real check, and server
    # admins can change who sees it under Server Settings > Integrations.
    feed = app_commands.Group(name="feed", description="Manage the RSS/Atom feeds Skippy posts (mods).",
                              default_permissions=discord.Permissions(manage_roles=True), guild_only=True)
    # Everyone: suggest a blog, search what's been posted.
    blog = app_commands.Group(name="blog", description="Suggest your blog, or search the community content tracker.",
                              guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.skip_until: dict[int, float] = {}  # feed id -> monotonic time a site asked us to wait until
        self.refusals_told: dict[tuple[str, str], float] = {}  # (member, blog) -> when the mods were last told
        self.poll.start()
        self.weekly_roundup.start()

    def cog_unload(self):
        self.poll.cancel()
        self.weekly_roundup.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            message = f"You need the <@&{RSS_ADMIN_ROLE_ID}> role to manage feeds."
        else:
            logger.exception("/feed command failed", exc_info=error)
            message = "Something went wrong; the details are in the bot's log."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ──────────────────────────────────────────
    # Commands
    # ──────────────────────────────────────────

    async def _create_feed(self, guild: discord.Guild, url: str, channel_id: int,
                           author_id: str | None, added_by: str):
        """Check and add a feed; shared by /feed add and approving a suggestion.
        Returns (feed id, title, current items, channel). Raises
        FeedError with a message for mods, also when the same blog already
        posts in that channel (sqlite3.IntegrityError if one was added at
        the same moment)."""
        if author_id and (blog := await self._feed_by_author(str(guild.id), author_id)):
            raise FeedError(f"<@{author_id}> already has a blog on the feed: feed #{blog['id']} "
                            f"**{self._name(blog)}**. Each member has one; to swap, remove that feed or "
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
        duplicate = await self._duplicate_of(str(guild.id), url, feed_reader.item_ids(items), channel.id)
        if duplicate:
            raise FeedError(f"that's the same blog as feed #{duplicate['id']} **{self._name(duplicate)}**, "
                            f"which already posts in {channel.mention}")
        feed_id = await db.add_feed(str(guild.id), str(channel.id), url, title, added_by)
        # Everything already in the feed counts as seen, so adding a feed
        # doesn't dump its whole history into the channel.
        await db.mark_feed_items_seen(feed_id, [i.key for i in items])
        await db.record_feed_check(feed_id, None, result.etag, result.last_modified, title)
        if author_id:
            await self._link_author(feed_id, str(guild.id), author_id, added_by)
        return feed_id, title, items, channel

    @staticmethod
    async def _duplicate_of(guild_id: str, url: str, item_ids: set[str], channel_id: int | None = None,
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

    @staticmethod
    async def _feed_by_author(guild_id: str, author_id: str, ignore_feed: int | None = None) -> dict | None:
        """The feed credited to this member, if any: each member has one blog on the feed."""
        return next((f for f in await db.get_feeds(guild_id)
                     if f["author_id"] == author_id and f["id"] != ignore_feed), None)

    @staticmethod
    async def _link_author(feed_id: int, guild_id: str, author_id: str, linked_by: str):
        """Credit a member for a feed, and put them on the RSS watchlist so
        their activity in the server is tracked like any watched member's."""
        await db.update_feed(feed_id, guild_id, author_id=author_id)
        if not await db.is_watched(author_id):  # re-adding would reset when tracking started
            await db.add_to_watchlist(author_id, guild_id, linked_by)

    @feed.command(name="add", description="Post a feed's new items in one of the feed channels.")
    @app_commands.describe(url="The RSS or Atom feed URL",
                           channel=f"Where to post (default: #{next(iter(FEED_CHANNELS))})",
                           author="The member who writes it: credited on posts, and activity-tracked",
                           name="What posts call it, if the feed's own title isn't good (e.g. just \"Blog\")",
                           **LINK_OPTIONS)
    @app_commands.choices(channel=CHANNEL_CHOICES)
    @is_rss_admin()
    async def feed_add(self, interaction: discord.Interaction, url: str, channel: str | None = None,
                       author: discord.Member | None = None,
                       name: app_commands.Range[str, 1, 80] | None = None, linkedin: str | None = None,
                       x: str | None = None, youtube: str | None = None, github: str | None = None):
        await interaction.response.defer(ephemeral=True)
        url = feed_reader.normalize_url(url)
        try:
            links = read_links(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:
            await interaction.followup.send(f"Couldn't add that feed: {e}.", ephemeral=True)
            return
        if links and not author:
            await interaction.followup.send("Profile links belong to the blog's author; pick the `author` too.",
                                            ephemeral=True)
            return
        try:
            feed_id, title, items, target = await self._create_feed(
                interaction.guild, url, int(channel) if channel else DEFAULT_CHANNEL_ID,
                str(author.id) if author else None, str(interaction.user.id))
        except FeedError as e:
            await interaction.followup.send(f"Couldn't add that feed: {e}.", ephemeral=True)
            return
        except sqlite3.IntegrityError:
            await interaction.followup.send("That feed already posts in that channel.", ephemeral=True)
            return

        notes = []
        if author:
            notes.append(f"\n👤 Posts credit {author.mention}, who's now on the RSS watchlist.")
        if links:
            await db.set_author_links(str(author.id), links)
            notes.append(f"\n🔗 Posts link to their {', '.join(profile_links.SITES[site] for site in links)}.")
        if not author and target.id == FEED_COMMUNITY_CHANNEL_ID:
            notes.append(f"\n👤 No author: posts won't credit anyone and the activity rule doesn't apply. "
                         f"If a member writes it, add them with `/feed edit feed:{feed_id} author:`.")
        if name and name.strip():
            await db.update_feed(feed_id, str(interaction.guild_id), display_name=name.strip())
        row = await db.get_feed(feed_id, str(interaction.guild_id))
        newest = feed_reader.select_new(items, set(), 1)
        if newest:
            # The newest item as a real post (archived, credited, threaded):
            # a visible sign the feed is connected.
            if await self._post(target, row, feed_name(row, title), newest):
                notes.append(f"\n📮 Posted its newest item in {target.mention}.")
            else:
                notes.append("\n⚠️ Couldn't post its newest item; see the bot's log.")
        await interaction.followup.send(
            f"✅ Added feed #{feed_id} **{discord.utils.escape_markdown(feed_name(row, title))}** → {target.mention}. "
            f"Its {len(items)} current items are skipped; new ones are posted within {FEED_POLL_MINUTES} minutes. "
            f"Preview the format with `/feed test feed:{feed_id}`, and set filters with `/feed edit`."
            + "".join(notes),
            ephemeral=True)
        await self._audit(interaction, "add", f"added feed #{feed_id} **{self._name(row)}** → {target.mention}"
                          + (f", by {author.mention}" if author else ""))

    @feed.command(name="remove", description="Stop posting a feed.")
    @app_commands.describe(feed="The feed to remove")
    @is_rss_admin()
    async def feed_remove(self, interaction: discord.Interaction, feed: int):
        row = await db.get_feed(feed, str(interaction.guild_id))
        if not row or not await db.remove_feed(feed, str(interaction.guild_id)):
            await interaction.response.send_message(f"There's no feed #{feed}.", ephemeral=True)
            return
        if row["author_id"]:
            await db.delete_author_links(row["author_id"])  # they show with that blog's posts only
        await interaction.response.send_message(
            f"🗑️ Removed feed #{feed} **{discord.utils.escape_markdown(feed_name(row))}**.",
            ephemeral=True)
        await self._audit(interaction, "remove", f"removed feed #{feed} **{self._name(row)}**")

    @feed.command(name="list", description="Show all feeds and whether they're working.")
    @is_rss_admin()
    async def feed_list(self, interaction: discord.Interaction):
        feeds = await db.get_feeds(str(interaction.guild_id))
        if not feeds:
            await interaction.response.send_message("No feeds yet. Add one with `/feed add`.", ephemeral=True)
            return
        mode, min_messages = await self._activity_rule()
        lines = []
        for f in feeds:
            status = f"⚠️ failing: {f['last_error']}" if f["failures"] else "✅"
            author = f" · by <@{f['author_id']}>" if f["author_id"] else ""
            if f["author_id"] and mode != "off" and not f["activity_exempt"]:
                author += " " + ACTIVITY_BADGES[(await self._author_status(f["author_id"], min_messages))[0]]
            filters = " · 🔎 filtered" if f["include_words"] or f["exclude_words"] else ""
            name = discord.utils.escape_markdown(feed_name(f))
            lines.append(f"`#{f['id']}` **{name}** → <#{f['channel_id']}>{author}{filters} · {status}")
        embed = discord.Embed(title="📰 Feeds", description="\n".join(lines)[:4000], color=discord.Color.blurple())
        embed.set_footer(text=f"{len(feeds)} feed(s) · checked every {FEED_POLL_MINUTES} minutes · "
                              "details: /feed edit")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @feed.command(name="edit", description="Change a feed's settings. With only a feed picked, shows them.")
    @app_commands.describe(
        feed="The feed to change",
        channel="Move it to another feed channel",
        author="The member who writes it: credited on posts, and activity-tracked",
        name="What posts call it, instead of the feed's own title",
        include="Only post titles mentioning one of these words (comma-separated)",
        exclude="Never post titles mentioning any of these words (comma-separated)",
        highlight="In summaries, list titles mentioning these first, in bold (comma-separated)",
        summary_after="More new posts than this at once become one summary post (default 5)",
        activity_exempt="Exempt this feed from the activity rule (see /feed activity)",
        clear="Remove one setting",
        **{site: f"The author's {text}" for site, text in LINK_OPTIONS.items()})
    @app_commands.choices(channel=CHANNEL_CHOICES, clear=CLEARABLE_SETTINGS)
    @is_rss_admin()
    async def feed_edit(self, interaction: discord.Interaction, feed: int, channel: str | None = None,
                        author: discord.Member | None = None,
                        name: app_commands.Range[str, 1, 80] | None = None,
                        include: str | None = None, exclude: str | None = None,
                        highlight: str | None = None,
                        summary_after: app_commands.Range[int, 1, 25] | None = None,
                        activity_exempt: bool | None = None, clear: str | None = None,
                        linkedin: str | None = None, x: str | None = None, youtube: str | None = None,
                        github: str | None = None):
        guild_id = str(interaction.guild_id)
        current = await db.get_feed(feed, guild_id)
        if not current:
            await interaction.response.send_message(f"There's no feed #{feed}.", ephemeral=True)
            return
        # Profile links belong to the feed's author, so they need one.
        try:
            links = read_links(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:
            await interaction.response.send_message(f"Couldn't change that: {e}.", ephemeral=True)
            return
        if clear and clear.startswith("link:"):
            links[clear.removeprefix("link:")] = None
            clear = None
        links_of = str(author.id) if author else current["author_id"]
        if links and not links_of:
            await interaction.response.send_message(
                "Profile links belong to the blog's author, and this feed has none; set `author` too.", ephemeral=True)
            return
        if author and (blog := await self._feed_by_author(guild_id, str(author.id), ignore_feed=feed)):
            await interaction.response.send_message(
                f"{author.mention} already has a blog on the feed: feed #{blog['id']} **{self._name(blog)}**. "
                f"Each member has one; to swap, clear that feed's author first "
                f"(`/feed edit feed:{blog['id']} clear:author`).", ephemeral=True)
            return
        changes = {}
        if channel:
            if int(channel) not in FEED_CHANNELS.values():
                await interaction.response.send_message("That isn't one of the feed channels.", ephemeral=True)
                return
            row = await db.get_feed(feed, guild_id)
            duplicate = await self._duplicate_of(guild_id, row["url"], await db.feed_item_ids(feed), int(channel),
                                                 ignore_feed=feed)
            if duplicate:
                await interaction.response.send_message(
                    f"Feed #{duplicate['id']} **{self._name(duplicate)}** already posts that blog in <#{channel}>.",
                    ephemeral=True)
                return
            changes["channel_id"] = channel
        if name and name.strip():
            changes["display_name"] = name.strip()
        for setting, value in (("include_words", include), ("exclude_words", exclude),
                               ("highlight_words", highlight)):
            if value is not None:
                changes[setting] = ", ".join(feed_reader.word_list(value)) or None
        if summary_after:
            changes["summary_after"] = summary_after
        if activity_exempt is not None:
            changes["activity_exempt"] = int(activity_exempt)
        if clear:
            changes[clear] = None
        try:
            await db.update_feed(feed, guild_id, **changes)
        except sqlite3.IntegrityError:
            await interaction.response.send_message("That feed already posts in that channel.", ephemeral=True)
            return
        if author:
            await self._link_author(feed, guild_id, str(author.id), str(interaction.user.id))
            changes["author_id"] = str(author.id)
        if links:
            await db.set_author_links(links_of, links)
            changes["links"] = links  # for the audit line
        row = await db.get_feed(feed, guild_id)
        verb = "Updated" if changes else "Settings for"
        shown_links = await db.get_author_links(row["author_id"]) if row["author_id"] else {}
        await interaction.response.send_message(f"{verb} feed #{feed}:\n{self._describe(row, shown_links)}",
                                                ephemeral=True)
        if changes:
            labels = {"channel_id": "channel", "author_id": "author", "display_name": "name",
                      "include_words": "include words", "exclude_words": "exclude words",
                      "highlight_words": "highlight words", "summary_after": "summary threshold",
                      "activity_exempt": "activity exemption", "links": "profile links"}
            await self._audit(interaction, "edit", f"edited feed #{feed} **{self._name(row)}**: "
                              + ", ".join(labels[k] for k in changes))

    @blog.command(name="search", description="Search everything the feeds have posted.")
    @app_commands.describe(words="Words to look for in post titles and feed names")
    async def feed_search(self, interaction: discord.Interaction, words: app_commands.Range[str, 2, 100]):
        results, seen = [], set()
        for r in await db.search_feed_posts(words.split()[:5], limit=30):
            if r["link"] not in seen and len(results) < 10:  # an article posted twice shows once
                seen.add(r["link"])
                results.append(r)
        if not results:
            await interaction.response.send_message(f"Nothing posted matches **{discord.utils.escape_markdown(words)}**.",
                                                    ephemeral=True)
            return
        lines = []
        for r in results:
            when = int(datetime.fromisoformat(r["posted_at"]).replace(tzinfo=timezone.utc).timestamp())
            title = discord.utils.escape_markdown(r["title"]).replace("[", "(").replace("]", ")")
            lines.append(f"• [{title}](<{r['link']}>) · {discord.utils.escape_markdown(r['feed_title'] or '')} · <t:{when}:d>")
        embed = discord.Embed(title=f"🔎 {words}"[:256], description="\n".join(lines)[:4000], color=discord.Color.blurple())
        embed.set_footer(text=f"Newest {len(results)} match(es)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # Suggestions: members propose, mods approve with a button
    # ──────────────────────────────────────────

    # Members can only suggest members' blogs: every suggestion names the
    # member who writes it (a required choice, even when it's the suggester),
    # who's credited on posts and covered by the activity rule. Feeds without
    # an author are for mods to add (/feed add).
    @blog.command(name="suggest", description="Suggest a member's blog for the community feed. Mods approve it.")
    @app_commands.describe(url="The blog's RSS or Atom feed URL (often ends in /feed or /rss)",
                           author="The member who writes it: pick yourself if it's your blog",
                           note="Anything the mods should know",
                           **{site: f"Optional: the author's {text}" for site, text in LINK_OPTIONS.items()})
    async def feed_suggest(self, interaction: discord.Interaction, url: str, author: discord.Member,
                           note: app_commands.Range[str, 1, 300] | None = None, linkedin: str | None = None,
                           x: str | None = None, youtube: str | None = None, github: str | None = None):
        await interaction.response.defer(ephemeral=True)
        url = feed_reader.normalize_url(url)
        try:
            links = read_links(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:  # a typo, not a refusal: nothing for the mods
            await interaction.followup.send(f"Couldn't send that suggestion: {e}.", ephemeral=True)
            return
        guild_id, member_id, author_id = str(interaction.guild_id), str(interaction.user.id), str(author.id)
        yours = author_id == member_id
        if author.bot:
            await self._refuse(interaction, url, author, "The author has to be a member who writes the blog, "
                               "not a bot.", "the author is a bot")
            return
        if duplicate := await self._duplicate_of(guild_id, url, set()):
            await self._refuse(interaction, url, author, self._already_tracked(duplicate, member_id, author_id),
                               self._already_tracked_reason(duplicate))
            return
        if blog := await self._feed_by_author(guild_id, author_id):
            has = "You already have" if yours else f"{author.mention} already has"
            await self._refuse(interaction, url, author,
                               f"{has} a blog on the feed: **{self._name(blog)}**. Each member has one; ask a mod "
                               "if it should be swapped.",
                               f"{author.mention} already has a blog on the feed: feed #{blog['id']} "
                               f"**{self._name(blog)}**")
            return
        pending = await db.pending_suggestions(guild_id)
        if waiting := next((w for w in pending if w["author_id"] == author_id), None):
            whose = "your" if yours else f"{author.mention}'s"
            await self._refuse(interaction, url, author, f"There's already a suggestion waiting for {whose} blog; "
                               "the mods will look at it soon.",
                               f"a suggestion for {author.mention}'s blog is already waiting (#{waiting['id']})")
            return
        same_url = feed_reader.comparable_url(url)
        if waiting := next((w for w in pending if feed_reader.comparable_url(w["url"]) == same_url), None):
            await self._refuse(interaction, url, author, "Someone already suggested that feed; the mods will look "
                               "at it soon.", f"the same feed is already waiting (#{waiting['id']})")
            return
        if len(await db.pending_suggestions(guild_id, suggested_by=member_id)) >= MAX_PENDING_SUGGESTIONS:
            await self._refuse(interaction, url, author,
                               f"You have {MAX_PENDING_SUGGESTIONS} suggestions waiting already; please wait for "
                               "the mods to get to those first.",
                               f"they have {MAX_PENDING_SUGGESTIONS} suggestions waiting already")
            return
        async with feed_reader.session() as http:
            try:
                title, items = feed_reader.parse_feed((await feed_reader.fetch_feed(http, url)).data, url)
            except FeedError as e:
                await self._refuse(interaction, url, author,
                                   f"I couldn't read that as a feed: {e}. Look for the blog's RSS or Atom link; "
                                   "it often ends in `/feed`, `/rss` or `/atom.xml`.",
                                   f"couldn't read it as a feed: {e}")
                return
        # The same blog at another address (/feed and /rss, say) has the same posts.
        if duplicate := await self._duplicate_of(guild_id, url, feed_reader.item_ids(items)):
            await self._refuse(interaction, url, author, self._already_tracked(duplicate, member_id, author_id),
                               self._already_tracked_reason(duplicate))
            return

        suggestion_id = await db.add_suggestion(guild_id, url, title, member_id, author_id, note, links)
        cards = self.bot.get_channel(FEED_SUGGESTIONS_CHANNEL_ID)
        if cards is None:
            logger.error("Feed suggestions channel %s not found; suggestion #%s is only in the database",
                         FEED_SUGGESTIONS_CHANNEL_ID, suggestion_id)
        else:
            newest = feed_reader.select_new(items, set(), 1)
            embed = discord.Embed(title=f"📬 Feed suggestion #{suggestion_id}", color=discord.Color.gold())
            embed.add_field(name="Feed", value=f"**{discord.utils.escape_markdown(title or url)}**\n<{url}>"[:1024],
                            inline=False)
            if newest:
                embed.add_field(name="Latest post", value=f"[{discord.utils.escape_markdown(newest[0].title)[:200]}]"
                                                          f"(<{feed_reader.clean_url(newest[0].link)}>)"[:1024],
                                inline=False)
            joined = author.joined_at
            embed.add_field(name="Author",
                            value=author.mention
                                  + (f", member since <t:{int(joined.timestamp())}:R>" if joined else "")
                                  + ("" if author == interaction.user else f"\nSuggested by {interaction.user.mention}"),
                            inline=False)
            if links:
                embed.add_field(name="Profile links", value=profile_links.line(links, FEED_LINK_EMOJIS), inline=False)
            if note:
                embed.add_field(name="Note", value=note, inline=False)
            embed.set_footer(text=f"Approving adds it to #{next(iter(FEED_CHANNELS))}; change it afterwards "
                                  "with /feed edit.")
            view = discord.ui.View(timeout=None)
            view.add_item(SuggestionButton("approve", suggestion_id))
            view.add_item(SuggestionButton("reject", suggestion_id))
            card = await cards.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            await db.set_suggestion_message(suggestion_id, str(card.id))
        await interaction.followup.send(
            f"🙏 Thanks! **{discord.utils.escape_markdown(title or url)}** went to the mods. "
            "I'll DM you when they've decided.", ephemeral=True)
        logger.info("Feed suggestion #%s by %s: %s", suggestion_id, interaction.user, url)

    async def _refuse(self, interaction: discord.Interaction, url: str, author: discord.Member,
                      answer: str, reason: str):
        """Turn down a /blog suggest. The member gets `answer`; the mods get
        `reason` in their channel, so they can reach out if needed, and in
        the audit log (/audit). A member retrying the same blog is reported
        once an hour, so retries can't flood the mods' channel."""
        await interaction.followup.send(answer, ephemeral=True)
        member = interaction.user
        whose = "their" if author.id == member.id else f"{author.mention}'s"
        text = f"suggested {whose} blog <{url[:300]}>, refused: {reason}"
        logger.info("%s %s", member, text)
        now = time.monotonic()
        self.refusals_told = {k: t for k, t in self.refusals_told.items() if now - t < REFUSAL_REPEAT_SECONDS}
        key = (str(member.id), feed_reader.comparable_url(url))
        if key in self.refusals_told:
            return
        self.refusals_told[key] = now
        await db.add_audit_log(action="feed_suggestion_refused", actor_id=str(member.id),
                               target_id=str(author.id), detail=f"<{url[:300]}>: {reason}")
        await self._alert(f"🚫 {member.mention} {text}")

    def _already_tracked_reason(self, feed: dict) -> str:
        """For the mods: which feed a refused suggestion duplicates, and whom it credits."""
        credit = f"credited to <@{feed['author_id']}>" if feed["author_id"] else "no author yet"
        return f"it's already on the feed as feed #{feed['id']} **{self._name(feed)}** ({credit})"

    @blog.command(name="links", description="Show your LinkedIn, X, YouTube or GitHub under your blog's posts.")
    @app_commands.describe(clear="Remove one of your links", **LINK_OPTIONS)
    @app_commands.choices(clear=LINK_CHOICES)
    async def blog_links(self, interaction: discord.Interaction, linkedin: str | None = None,
                         x: str | None = None, youtube: str | None = None, github: str | None = None,
                         clear: str | None = None):
        member_id = str(interaction.user.id)
        if not await self._feed_by_author(str(interaction.guild_id), member_id):
            await interaction.response.send_message(
                f"Your links show under your blog's posts in <#{FEED_COMMUNITY_CHANNEL_ID}>, so you need a blog "
                "on the feed first. Suggest it with `/blog suggest`; you can add your links there too.",
                ephemeral=True)
            return
        try:
            links = read_links(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:
            await interaction.response.send_message(f"Couldn't save that: {e}.", ephemeral=True)
            return
        if clear:
            links.update({site: None for site in profile_links.SITES if clear in (site, "all")})
        if links:
            await db.set_author_links(member_id, links)
        line = profile_links.line(await db.get_author_links(member_id), FEED_LINK_EMOJIS)
        if not links:
            await interaction.response.send_message(
                f"Under your posts: {line or 'no links yet'}. Add or change them with the options on this command.",
                ephemeral=True)
            return
        await interaction.response.send_message(f"✅ Saved. Your posts now show: {line or 'no links'}",
                                                ephemeral=True)
        changed = ", ".join(f"{profile_links.SITES[site]} " + (f"<{url}>" if url else "removed")
                            for site, url in links.items())
        await self._audit(interaction, "links", f"changed their profile links: {changed}")

    def _already_tracked(self, feed: dict, member_id: str, author_id: str) -> str:
        """The answer to suggesting a blog that's already on the feed."""
        text = f"That blog is already on the feed as **{self._name(feed)}**. 🎉"
        who = "you" if author_id == member_id else f"<@{author_id}>"
        if feed["author_id"] == author_id:
            return text + f" Its posts credit {who}."
        if not feed["author_id"]:
            return text + f" Ask a mod to credit {who} on its posts."
        return text

    async def approve_suggestion(self, interaction: discord.Interaction, suggestion_id: int):
        await interaction.response.defer()
        if not await db.decide_suggestion(suggestion_id, "approved", str(interaction.user.id)):
            await interaction.followup.send("Another mod already decided on this one.", ephemeral=True)
            return
        suggestion = await db.get_suggestion(suggestion_id)
        author_id = suggestion["author_id"]
        try:
            feed_id, title, items, channel = await self._create_feed(
                interaction.guild, suggestion["url"], DEFAULT_CHANNEL_ID, author_id, str(interaction.user.id))
        except (FeedError, sqlite3.IntegrityError) as e:
            await db.decide_suggestion(suggestion_id, "pending", None)  # back in the queue, buttons stay
            problem = "it already posts in that channel" if isinstance(e, sqlite3.IntegrityError) else str(e)
            await interaction.followup.send(f"Couldn't add it: {problem}. The suggestion is still open.",
                                            ephemeral=True)
            return
        if author_id and suggestion["links"]:
            await db.set_author_links(author_id, json.loads(suggestion["links"]))
        credit = f", credited to <@{author_id}>" if author_id else ""
        newest = feed_reader.select_new(items, set(), 1)
        row = await db.get_feed(feed_id, str(interaction.guild_id))
        if newest:  # welcome the blog with its newest article, which also shows the feed works
            await self._post(channel, row, feed_name(row, title), newest)
        await self._close_card(interaction.message, discord.Color.green(),
                               f"✅ Approved by {interaction.user.mention}: feed #{feed_id} → {channel.mention}{credit}")
        blog = discord.utils.escape_markdown(title or suggestion["url"])
        suggested_by = suggestion["suggested_by"]
        shout_out = {None: ".", suggested_by: ", with a shout-out to you."}.get(author_id, f", crediting <@{author_id}>.")
        await self._dm(suggested_by,
                       f"🎉 Your suggestion **{blog}** was approved! New posts will appear in {channel.mention}"
                       + shout_out)
        if author_id and author_id != suggested_by:  # someone suggested their blog: tell them too
            await self._dm(author_id, f"🎉 Your blog **{blog}** is now on the community feed, suggested by "
                                      f"<@{suggested_by}>. New posts will appear in {channel.mention}, "
                                      "with a shout-out to you.")
        logger.info("Feed suggestion #%s approved by %s as feed #%s", suggestion_id, interaction.user, feed_id)
        await db.add_audit_log(action="feed_suggestion_approved", actor_id=str(interaction.user.id),
                               target_id=suggestion["suggested_by"],
                               detail=f"suggestion #{suggestion_id} → feed #{feed_id} {feed_name(row, title)}")

    async def reject_suggestion(self, interaction: discord.Interaction, suggestion_id: int, reason: str | None):
        if not await db.decide_suggestion(suggestion_id, "rejected", str(interaction.user.id), reason):
            await interaction.response.send_message("Another mod already decided on this one.", ephemeral=True)
            return
        await interaction.response.defer()
        suggestion = await db.get_suggestion(suggestion_id)
        await self._close_card(interaction.message, discord.Color.red(),
                               f"❌ Rejected by {interaction.user.mention}" + (f": {reason}" if reason else ""))
        await self._dm(suggestion["suggested_by"],
                       f"Thanks for suggesting **{discord.utils.escape_markdown(suggestion['title'] or suggestion['url'])}**. "
                       "The mods decided not to add it this time."
                       + (f"\nReason: {reason}" if reason else ""))
        logger.info("Feed suggestion #%s rejected by %s", suggestion_id, interaction.user)
        await db.add_audit_log(action="feed_suggestion_rejected", actor_id=str(interaction.user.id),
                               target_id=suggestion["suggested_by"],
                               detail=f"suggestion #{suggestion_id} {suggestion['url']}" + (f": {reason}" if reason else ""))

    @staticmethod
    async def _close_card(card: discord.Message | None, color: discord.Color, decision: str):
        """Record the decision on the mods' card and remove its buttons."""
        if card is None or not card.embeds:
            return
        embed = card.embeds[0]
        embed.color = color
        embed.add_field(name="Decision", value=decision, inline=False)
        try:
            await card.edit(embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.warning("Couldn't update suggestion card %s: %s", card.id, e)

    async def _dm(self, user_id: str, text: str):
        """Best effort: plenty of members don't accept DMs from server bots."""
        try:
            user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
            await user.send(text)
        except discord.HTTPException as e:
            logger.info("Couldn't DM %s: %s", user_id, e)

    @feed.command(name="test", description="Post a feed's newest item now, to check how posts look.")
    @app_commands.describe(feed="The feed to test")
    @is_rss_admin()
    async def feed_test(self, interaction: discord.Interaction, feed: int):
        await interaction.response.defer(ephemeral=True)
        row = await db.get_feed(feed, str(interaction.guild_id))
        channel = self.bot.get_channel(int(row["channel_id"])) if row else None
        if not row or channel is None:
            await interaction.followup.send(f"There's no feed #{feed}, or its channel is gone.", ephemeral=True)
            return
        async with feed_reader.session() as http:
            try:
                title, items = feed_reader.parse_feed((await feed_reader.fetch_feed(http, row["url"])).data, row["url"])
            except FeedError as e:
                await interaction.followup.send(f"Couldn't read the feed: {e}.", ephemeral=True)
                return
        newest = feed_reader.select_new(items, set(), 1)
        if not newest:
            await interaction.followup.send("The feed has no items right now.", ephemeral=True)
            return
        # Posted like any other post (archived, vote emoji, thread).
        if not await self._post(channel, row, feed_name(row, title), newest):
            await interaction.followup.send("Couldn't post it; see the bot's log.", ephemeral=True)
            return
        await interaction.followup.send(f"Posted the newest item in {channel.mention}.", ephemeral=True)
        await self._audit(interaction, "test", f"test-posted feed #{feed} **{self._name(row)}** in {channel.mention}")

    @feed_remove.autocomplete("feed")
    @feed_edit.autocomplete("feed")
    @feed_test.autocomplete("feed")
    async def _feed_choices(self, interaction: discord.Interaction, current: str):
        choices = []
        for f in await db.get_feeds(str(interaction.guild_id)):
            label = f"#{f['id']} {feed_name(f)}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=f["id"]))
        return choices[:25]

    # ──────────────────────────────────────────
    # Polling
    # ──────────────────────────────────────────

    @tasks.loop(minutes=FEED_POLL_MINUTES)
    async def poll(self):
        # Any exception escaping a tasks.loop stops it for good, so nothing may.
        try:
            feeds = await db.get_feeds()
            if not feeds:
                return
            limit = asyncio.Semaphore(CONCURRENT_CHECKS)

            async def check(http, feed):
                async with limit:
                    try:
                        await self._check(http, feed)
                    except Exception:
                        logger.exception("Feed #%s: check failed", feed["id"])

            async with feed_reader.session() as http:
                await asyncio.gather(*(check(http, feed) for feed in feeds))
        except Exception:
            logger.exception("Feed poll failed")

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    async def _check(self, http, feed: dict):
        if time.monotonic() < self.skip_until.get(feed["id"], 0):
            return  # the site asked us to wait (HTTP 429 / Retry-After)
        held = await self._activity_gate(feed)
        channel = self.bot.get_channel(int(feed["channel_id"]))
        try:
            if channel is None:
                raise FeedError("its channel no longer exists")
            result = await feed_reader.fetch_feed(http, feed["url"], feed["etag"], feed["last_modified"])
            if result.data is None:  # 304 Not Modified
                await self._record_success(feed)
                return
            title, items = feed_reader.parse_feed(result.data, feed["url"])
        except FeedError as e:
            if isinstance(e, RateLimited):
                self.skip_until[feed["id"]] = time.monotonic() + e.retry_after
            failures = await db.record_feed_check(feed["id"], str(e))
            if failures == ALERT_AFTER_FAILURES:
                await self._alert(f"⚠️ Feed `#{feed['id']}` **{self._name(feed)}** has failed "
                                  f"{failures} checks in a row: {e}")
            return

        seen = await db.get_seen_feed_items(feed["id"])
        new = feed_reader.select_new(items, seen, len(items))
        include = feed_reader.word_list(feed["include_words"])
        exclude = feed_reader.word_list(feed["exclude_words"])
        # While the author's posts are paused, new items are skipped, not
        # saved up: coming back doesn't flood the channel with old posts.
        to_post = [] if held else [i for i in new if feed_reader.wanted(i, include, exclude)]
        posted = await self._post(channel, feed, feed_name(feed, title), to_post)
        # Filtered-out items are marked seen (skipped for good). Wanted items
        # that didn't make it to Discord stay unseen, so the next check retries.
        unposted = {i.key for i in to_post} - posted
        await db.mark_feed_items_seen(feed["id"], [i.key for i in items if i.key not in unposted])
        await self._record_success(feed, result, title)
        if new:
            logger.info("Feed #%s: %d new, %d filtered out, %d posted",
                        feed["id"], len(new), len(new) - len(to_post), len(posted))

    # ──────────────────────────────────────────
    # Weekly roundup: Mondays 09:00 in the roundup channel
    # ──────────────────────────────────────────

    # Checking every few minutes (rather than a fixed-time loop) keeps it at
    # 09:00 local time across daylight saving changes, and catches up on its
    # own if Skippy was restarting at 09:00.
    @tasks.loop(minutes=FEED_POLL_MINUTES)
    async def weekly_roundup(self):
        try:
            await self._maybe_post_roundup(datetime.now(ROUNDUP_TZ))
        except Exception:
            logger.exception("Weekly roundup failed")

    @weekly_roundup.before_loop
    async def before_weekly_roundup(self):
        await self.bot.wait_until_ready()

    async def _maybe_post_roundup(self, now: datetime) -> bool:
        """Post this week's roundup if it's Monday from 09:00, it's switched
        on and it hasn't gone out yet this week. Returns whether it posted."""
        if now.weekday() != 0 or now.time() < ROUNDUP_AT:
            return False
        if await db.get_setting("feed_roundup", "on") != "on":
            return False
        week = "{0}-W{1}".format(*now.isocalendar()[:2])
        if await db.get_setting("feed_roundup_last_week") == week:
            return False
        await db.set_setting("feed_roundup_last_week", week)  # first, so a crash can't post it twice
        posts = await db.roundup_posts(str(FEED_COMMUNITY_CHANNEL_ID))
        embed = build_roundup(posts, now, await self._vote_counts(posts))
        channel = self.bot.get_channel(FEED_ROUNDUP_CHANNEL_ID)
        if embed is None or channel is None:
            logger.info("Weekly roundup: %s", "quiet week, nothing to post" if embed is None else "channel not found")
            return False
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        logger.info("Weekly roundup posted for %s", week)
        return True

    async def _vote_counts(self, posts: list[dict]) -> dict[str, int]:
        """FEED_VOTE_EMOJI votes per posted message, not counting Skippy's own
        reaction or the author's. Summary messages (several posts in one
        message) are left out: a vote there can't tell the posts apart."""
        per_message = Counter(p["message_url"] for p in posts if p["message_url"])
        authors = {p["message_url"]: p["author_id"] for p in posts}
        votes = {}
        for url, n in per_message.items():
            if n > 1:
                continue
            try:
                channel_id, message_id = (int(part) for part in url.rstrip("/").split("/")[-2:])
                message = await self.bot.get_channel(channel_id).fetch_message(message_id)
            except (ValueError, AttributeError, discord.HTTPException):
                continue  # deleted message or channel: no votes to count
            for reaction in message.reactions:
                if str(reaction.emoji) == FEED_VOTE_EMOJI:
                    votes[url] = len([u async for u in reaction.users()
                                      if u.id != self.bot.user.id and str(u.id) != authors[url]])
        return votes

    @feed.command(name="roundup", description="The weekly community roundup: preview it, post it now, or switch it.")
    @app_commands.describe(action="What to do (default: a preview only you see)")
    @app_commands.choices(action=ROUNDUP_ACTIONS)
    @is_rss_admin()
    async def feed_roundup(self, interaction: discord.Interaction, action: str = "preview"):
        when = f"Mondays 09:00 ({FEED_ROUNDUP_TIMEZONE}) in <#{FEED_ROUNDUP_CHANNEL_ID}>"
        if action in ("on", "off"):
            await db.set_setting("feed_roundup", action)
            await self._audit(interaction, "roundup", f"switched the weekly roundup **{action}**")
            await interaction.response.send_message(
                f"Weekly roundup is now **{action}**" + (f": {when}." if action == "on" else "."), ephemeral=True)
            return
        now = datetime.now(ROUNDUP_TZ)
        await interaction.response.defer(ephemeral=True)  # counting votes can take longer than Discord's 3 s
        posts = await db.roundup_posts(str(FEED_COMMUNITY_CHANNEL_ID))
        embed = build_roundup(posts, now, await self._vote_counts(posts))
        reply = interaction.followup.send
        if embed is None:
            await reply("No posts from the community feeds in the last 7 days, so there's nothing to round up.",
                        ephemeral=True)
            return
        if action == "preview":
            status = "on" if await db.get_setting("feed_roundup", "on") == "on" else "off"
            await reply(f"Preview (weekly roundup is **{status}**: {when}):", embed=embed, ephemeral=True)
            return
        channel = self.bot.get_channel(FEED_ROUNDUP_CHANNEL_ID)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        # Counts as this week's roundup, so Monday's doesn't repeat it.
        await db.set_setting("feed_roundup_last_week", "{0}-W{1}".format(*now.isocalendar()[:2]))
        await reply(f"Posted the roundup in {channel.mention}.", ephemeral=True)
        await self._audit(interaction, "roundup", f"posted the weekly roundup now in {channel.mention}")

    # ──────────────────────────────────────────
    # Activity rule: authors' feeds post while they're active in the server
    # ──────────────────────────────────────────

    @staticmethod
    async def _author_status(author_id: str, min_messages: int) -> tuple[str, int]:
        """("active" | "new" | "inactive" | "untracked", messages in the window).
        Activity comes from the RSS watchlist, which only counts messages since
        the member was added, so newly added authors get a grace period."""
        entry = await db.get_watch_entry(author_id)
        if entry is None:
            return "untracked", 0  # a mod took them off the watchlist; nothing to judge by
        count = await db.get_rss_activity_count(author_id, days=ACTIVITY_DAYS)
        if count >= min_messages:
            return "active", count
        added = datetime.fromisoformat(entry["added_at"]).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - added < timedelta(days=ACTIVITY_DAYS):
            return "new", count
        return "inactive", count

    @staticmethod
    async def _activity_rule() -> tuple[str, int]:
        return (await db.get_setting("feed_activity_mode", "off"),
                int(await db.get_setting("feed_activity_min_messages", "1")))

    async def _activity_gate(self, feed: dict) -> bool:
        """Apply the activity rule to a feed's author, announcing any change
        once. Returns True when the author's new posts should be held back."""
        if not feed["author_id"] or feed["activity_exempt"]:
            return False
        mode, min_messages = await self._activity_rule()
        if mode not in ("warn", "enforce"):
            return False
        status, count = await self._author_status(feed["author_id"], min_messages)
        if status == "inactive":
            state = "paused" if mode == "enforce" else "warned"
        else:
            state = "active"
        previous = await db.get_author_state(feed["author_id"]) or "active"
        if state != previous:
            await db.set_author_state(feed["author_id"], state)
            await self._announce_activity_change(feed, previous, state, count, min_messages)
        return state == "paused"

    async def _announce_activity_change(self, feed: dict, previous: str, state: str,
                                        count: int, min_messages: int):
        author, name = f"<@{feed['author_id']}>", self._name(feed)
        guild = self.bot.get_guild(int(feed["guild_id"]))
        server = guild.name if guild else "the server"
        bar = f"{count} message(s) in {ACTIVITY_DAYS} days; active means {min_messages}+"
        if state == "paused":
            await self._alert(f"⏸️ {author} is inactive ({bar}), so new posts from **{name}** are paused "
                              "until they're active again.")
            await self._dm(feed["author_id"],
                           f"⏸️ Hi! New posts from your blog **{name}** are paused in the {server} content tracker, "
                           f"because you haven't been active in the server lately. As soon as you're back in the "
                           f"conversation ({min_messages}+ message(s) in {ACTIVITY_DAYS} days), your posts are "
                           "shared again automatically.")
        elif state == "warned" and previous == "active":
            await self._alert(f"📉 {author} is inactive ({bar}). **{name}** still posts; they got a friendly reminder.")
            await self._dm(feed["author_id"],
                           f"👋 Hi! Your blog **{name}** is part of the {server} content tracker, and we'd love to "
                           "see you around the server too. Blogs in the tracker are for active members, so if you "
                           "stay quiet, new posts may get paused. Joining a conversation now and then is all it takes.")
        elif state == "warned":  # paused -> warned: mods switched enforce off
            await self._alert(f"▶️ **{name}** by {author} posts again: the activity rule is back to reminders only.")
        elif previous == "paused":
            await self._alert(f"▶️ {author} is active again, so **{name}** posts again.")
            await self._dm(feed["author_id"],
                           f"▶️ Welcome back! Posts from your blog **{name}** are being shared in {server} again.")
        else:
            await self._alert(f"📈 {author} is active again.")

    @feed.command(name="activity", description="The activity rule for authors' feeds: see who it affects, or change it.")
    @app_commands.describe(mode="Phase it in: watch first, then warn, then enforce",
                           min_messages=f"Messages in the last {ACTIVITY_DAYS} days that count as active (default 1)")
    @app_commands.choices(mode=ACTIVITY_MODES)
    @is_rss_admin()
    async def feed_activity(self, interaction: discord.Interaction, mode: str | None = None,
                            min_messages: app_commands.Range[int, 1, 50] | None = None):
        if mode:
            await db.set_setting("feed_activity_mode", mode)
        if min_messages:
            await db.set_setting("feed_activity_min_messages", str(min_messages))
        changed = bool(mode or min_messages)
        mode, min_messages = await self._activity_rule()
        if changed:
            await self._audit(interaction, "activity", f"set the activity rule to **{mode}** "
                              f"(active = {min_messages}+ messages in {ACTIVITY_DAYS} days)")

        by_author: dict[str, list[dict]] = {}
        for f in await db.get_feeds(str(interaction.guild_id)):
            if f["author_id"]:
                by_author.setdefault(f["author_id"], []).append(f)
        lines = []
        for author_id, author_feeds in by_author.items():
            status, count = await self._author_status(author_id, min_messages)
            paused = await db.get_author_state(author_id) == "paused"
            names = ", ".join(self._name(f) + (" (exempt)" if f["activity_exempt"] else "") for f in author_feeds)
            lines.append(f"{ACTIVITY_BADGES[status]} <@{author_id}> · {count} msg · {names}"
                         + (" · ⏸️ paused" if paused else ""))
        effect = {
            "off": "Nothing happens; authors' activity isn't checked.",
            "watch": "Activity is shown here and nothing else happens: a good first phase to see who'd be affected.",
            "warn": "Authors who go 🔴 get one friendly DM and mods get a notice. Their posts keep going.",
            "enforce": "🔴 authors' new posts are skipped until they're active again; they and the mods are told both ways.",
        }[mode]
        embed = discord.Embed(
            title=f"Activity rule: {mode}",
            description=(f"{effect}\nActive means {min_messages}+ message(s) in the last {ACTIVITY_DAYS} days. "
                         f"🆕 = linked less than {ACTIVITY_DAYS} days ago (grace period). Feeds without an author, "
                         "and exempt ones, are never affected.\n\n"
                         + ("\n".join(lines) or "No feeds are linked to an author yet."))[:4000],
            color=discord.Color.blurple())
        embed.set_footer(text="Exempt a feed with /feed edit feed:<feed> activity_exempt:True")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    async def _post(self, channel, feed: dict, name: str, items: list) -> set[str]:
        """Post items one by one as cards, or as one summary when there are
        more than the feed's summary_after. Archives what was posted and
        returns its keys. Posts credit the author but ping nobody."""
        if not items:
            return set()
        author_id = feed["author_id"]
        posted, archive = set(), []
        try:
            if len(items) > feed["summary_after"]:
                highlight = feed_reader.word_list(feed["highlight_words"])
                for part in feed_reader.format_summary(name, items, highlight, author_id=author_id):
                    message = await channel.send(part, allowed_mentions=discord.AllowedMentions.none())
                    if not posted:  # once the first part is out, don't repeat the summary on a retry
                        posted = {i.key for i in items}
                        archive = [(i, message.jump_url) for i in items]
                        extras = await self._finish_post(message, f"{name} - {len(items)} new posts", summary=True)
                        logger.info("Feed #%s: posted a summary of %d items in #%s%s",
                                    feed["id"], len(items), channel, extras)
            else:
                links_line = await self._links_line(author_id)
                async with feed_reader.session() as http:
                    for item in items:
                        preview = await feed_reader.fetch_preview(http, item.link)
                        message = await self._send_card(
                            channel, feed_reader.post_card(name, item, preview, author_id, links_line))
                        posted.add(item.key)
                        archive.append((item, message.jump_url))
                        extras = await self._finish_post(message, item.title)
                        logger.info("Feed #%s: posted %r in #%s%s", feed["id"], item.title[:80], channel, extras)
        except discord.HTTPException as e:
            logger.warning("Feed #%s: posting failed, retrying next check: %s", feed["id"], e)
        if archive:
            await db.archive_feed_posts([(feed["id"], name, i.title, feed_reader.clean_url(i.link), url)
                                         for i, url in archive])
        return posted

    @staticmethod
    async def _send_card(channel, card: discord.Embed) -> discord.Message:
        """Send a post's card. If Discord turns down the picture's address,
        the card goes without the picture rather than not at all."""
        try:
            return await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            if e.status != 400 or not card.image.url:
                raise
            logger.info("Discord refused the picture %s, posting without it: %s", card.image.url, e)
            card.set_image(url=None)
            return await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    async def _links_line(author_id: str | None) -> str | None:
        """The author's profile links for under their posts, if they have any."""
        if not author_id:
            return None
        return profile_links.line(await db.get_author_links(author_id), FEED_LINK_EMOJIS) or None

    async def _finish_post(self, message: discord.Message, title: str, summary: bool = False) -> str:
        """What every post gets, however it was posted: the vote emoji in the
        community channel (not on summaries: a vote there can't tell posts
        apart) and a discussion thread in thread channels. Neither failing
        counts the post as unposted. Returns a note for the log."""
        notes = []
        if message.channel.id == FEED_COMMUNITY_CHANNEL_ID and not summary:
            notes.append("vote " + ("ok" if await self._add_vote(message) else "FAILED"))
        if message.channel.id in FEED_THREAD_CHANNEL_IDS:
            notes.append("thread " + ("ok" if await self._open_thread(message, title) else "FAILED"))
        return f" ({', '.join(notes)})" if notes else ""

    @staticmethod
    async def _add_vote(message: discord.Message) -> bool:
        """Skippy's own vote emoji on the post, so voting is a single click."""
        try:
            await message.add_reaction(FEED_VOTE_EMOJI)
            return True
        except discord.HTTPException as e:
            logger.warning("Couldn't add the vote emoji to %s: %s", message.jump_url, e)
            return False

    @staticmethod
    async def _open_thread(message: discord.Message, title: str) -> bool:
        """A discussion thread on the post itself."""
        try:
            await message.create_thread(name=" ".join(title.split())[:100] or "Discussion",
                                        auto_archive_duration=THREAD_ARCHIVE_MINUTES)
            return True
        except discord.HTTPException as e:
            logger.warning("Couldn't open a thread on %s: %s", message.jump_url, e)
            return False

    async def _audit(self, interaction: discord.Interaction, action: str, text: str):
        """Record a mod's feed action: in the audit log (/audit), the bot's
        log, and as a line in the mods' channel so other mods see it."""
        await db.add_audit_log(action=f"feed_{action}", actor_id=str(interaction.user.id),
                               target_id=None, detail=text)
        logger.info("%s %s", interaction.user, text)
        await self._alert(f"🛠️ {interaction.user.mention} {text}")

    @staticmethod
    def _describe(feed: dict, links: dict[str, str] | None = None) -> str:
        none = "—"
        shown = " · ".join(f"[{label}](<{links[site]}>)" for site, label in profile_links.SITES.items()
                           if (links or {}).get(site))
        author = f"<@{feed['author_id']}>" if feed["author_id"] else none
        return "\n".join([
            f"**{discord.utils.escape_markdown(feed_name(feed))}** <{feed['url']}>"
            + (f" (the feed calls itself \"{discord.utils.escape_markdown(feed['title'])}\")"
               if feed["display_name"] and feed["title"] and feed["title"] != feed["display_name"] else ""),
            f"Channel: <#{feed['channel_id']}>",
            f"Author: {author}",
            f"Author's profile links: {shown or none}",
            f"Only titles with: {feed['include_words'] or 'anything'}",
            f"Never titles with: {feed['exclude_words'] or none}",
            f"Highlight in summaries: {feed['highlight_words'] or none}",
            f"Summary when more than {feed['summary_after']} new posts at once",
            f"Activity rule: {'exempt' if feed['activity_exempt'] else 'applies' if feed['author_id'] else 'n/a (no author)'}",
            f"Status: {'⚠️ failing: ' + feed['last_error'] if feed['failures'] else '✅ working'}",
        ])

    async def _record_success(self, feed: dict, result=None, title: str | None = None):
        await db.record_feed_check(feed["id"], None, result and result.etag,
                                   result and result.last_modified, title)
        if feed["failures"] >= ALERT_AFTER_FAILURES:
            await self._alert(f"✅ Feed `#{feed['id']}` **{self._name(feed)}** is working again.")

    async def _alert(self, message: str):
        channel = self.bot.get_channel(FEED_ALERT_CHANNEL_ID)
        if channel:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _name(feed: dict) -> str:
        return discord.utils.escape_markdown(feed_name(feed))


async def setup(bot: commands.Bot):
    """Called by bot.py to load this cog."""
    bot.add_dynamic_items(SuggestionButton)  # cards' buttons keep working across restarts
    await bot.add_cog(FeedCommands(bot))
