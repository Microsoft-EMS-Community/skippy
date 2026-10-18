# ============================================================
# feeds.py - /feed commands and the loop that posts new feed items
# Fetching and parsing live in feed_reader.py.
# ============================================================

import asyncio
import logging
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import feed_reader
from commands import is_rss_admin
from config import FEED_ALERT_CHANNEL_ID, FEED_CHANNELS, FEED_POLL_MINUTES
from feed_reader import FeedError, RateLimited

logger = logging.getLogger("followup-bot")

MAX_POSTS_PER_CHECK = 5   # a feed that suddenly republishes everything can't flood a channel
ALERT_AFTER_FAILURES = 6  # one hour of failed checks at the default 10-minute interval
CONCURRENT_CHECKS = 5     # feeds checked at once, so slow or dead ones can't hold up the rest


class FeedCommands(commands.Cog):
    """Post new items from RSS/Atom feeds into channels."""

    feed = app_commands.Group(name="feed", description="Post RSS/Atom feeds into channels.")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.skip_until: dict[int, float] = {}  # feed id -> monotonic time a site asked us to wait until
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            message = "You need the RSS admin role to manage feeds."
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

    @feed.command(name="add", description="Post a feed's new items in one of the feed channels.")
    @app_commands.describe(url="The RSS or Atom feed URL",
                           channel=f"Where to post (default: #{next(iter(FEED_CHANNELS))})",
                           role="Optional role to ping with every post")
    # A fixed list, not any channel: feeds only ever post where they're meant to.
    # Values are strings because channel IDs are too big for Discord's integers.
    @app_commands.choices(channel=[app_commands.Choice(name=name, value=str(cid))
                                   for name, cid in FEED_CHANNELS.items()])
    @is_rss_admin()
    async def feed_add(self, interaction: discord.Interaction, url: str,
                       channel: str | None = None, role: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True)
        channel_id = int(channel) if channel else next(iter(FEED_CHANNELS.values()))
        channel = interaction.guild.get_channel(channel_id) if channel_id in FEED_CHANNELS.values() else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                f"Feed channel {channel_id} doesn't exist anymore; update FEED_CHANNELS in config.py.",
                ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.followup.send(
                f"I can't post in {channel.mention}: I need View Channel, Send Messages and Embed Links there.",
                ephemeral=True)
            return

        async with feed_reader.session() as http:
            try:
                result = await feed_reader.fetch_feed(http, url)
                title, items = feed_reader.parse_feed(result.data)
            except FeedError as e:
                await interaction.followup.send(f"Couldn't read that feed: {e}.", ephemeral=True)
                return

        try:
            feed_id = await db.add_feed(str(interaction.guild_id), str(channel.id), url, title,
                                        str(role.id) if role else None, str(interaction.user.id))
        except sqlite3.IntegrityError:
            await interaction.followup.send(f"That feed already posts in {channel.mention}.", ephemeral=True)
            return
        # Everything already in the feed counts as seen, so adding a feed
        # doesn't dump its whole history into the channel.
        await db.mark_feed_items_seen(feed_id, [i.key for i in items])
        await db.record_feed_check(feed_id, None, result.etag, result.last_modified, title)

        notes = []
        if role and not role.mentionable and not perms.mention_everyone:
            notes.append(f"\n⚠️ {role.mention} isn't mentionable and I can't mention all roles, "
                         "so posts will show the role without pinging anyone.")
        await interaction.followup.send(
            f"✅ Added feed #{feed_id} **{discord.utils.escape_markdown(title or url)}** → {channel.mention}. "
            f"Its {len(items)} current items are skipped; new ones are posted within {FEED_POLL_MINUTES} minutes. "
            f"Preview the format with `/feed test feed:{feed_id}`." + "".join(notes),
            ephemeral=True)
        logger.info("Feed #%s added by %s: %s -> #%s", feed_id, interaction.user, url, channel)

    @feed.command(name="remove", description="Stop posting a feed.")
    @app_commands.describe(feed="The feed to remove")
    @is_rss_admin()
    async def feed_remove(self, interaction: discord.Interaction, feed: int):
        row = await db.get_feed(feed, str(interaction.guild_id))
        if not row or not await db.remove_feed(feed, str(interaction.guild_id)):
            await interaction.response.send_message(f"There's no feed #{feed}.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🗑️ Removed feed #{feed} **{discord.utils.escape_markdown(row['title'] or row['url'])}**.",
            ephemeral=True)
        logger.info("Feed #%s removed by %s", feed, interaction.user)

    @feed.command(name="list", description="Show all feeds and whether they're working.")
    @is_rss_admin()
    async def feed_list(self, interaction: discord.Interaction):
        feeds = await db.get_feeds(str(interaction.guild_id))
        if not feeds:
            await interaction.response.send_message("No feeds yet. Add one with `/feed add`.", ephemeral=True)
            return
        lines = []
        for f in feeds:
            status = f"⚠️ failing: {f['last_error']}" if f["failures"] else "✅"
            role = f" · pings <@&{f['role_id']}>" if f["role_id"] else ""
            name = discord.utils.escape_markdown(f["title"] or f["url"])
            lines.append(f"`#{f['id']}` **{name}** → <#{f['channel_id']}>{role} · {status}")
        embed = discord.Embed(title="📰 Feeds", description="\n".join(lines)[:4000], color=discord.Color.blurple())
        embed.set_footer(text=f"{len(feeds)} feed(s) · checked every {FEED_POLL_MINUTES} minutes")
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
                title, items = feed_reader.parse_feed((await feed_reader.fetch_feed(http, row["url"])).data)
            except FeedError as e:
                await interaction.followup.send(f"Couldn't read the feed: {e}.", ephemeral=True)
                return
        newest = feed_reader.select_new(items, set(), 1)
        if not newest:
            await interaction.followup.send("The feed has no items right now.", ephemeral=True)
            return
        # The test post never pings the role.
        await channel.send(feed_reader.format_post(title or row["title"], newest[0]),
                           allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(f"Posted the newest item in {channel.mention}.", ephemeral=True)

    @feed_remove.autocomplete("feed")
    @feed_test.autocomplete("feed")
    async def _feed_choices(self, interaction: discord.Interaction, current: str):
        choices = []
        for f in await db.get_feeds(str(interaction.guild_id)):
            label = f"#{f['id']} {f['title'] or f['url']}"
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
        channel = self.bot.get_channel(int(feed["channel_id"]))
        try:
            if channel is None:
                raise FeedError("its channel no longer exists")
            result = await feed_reader.fetch_feed(http, feed["url"], feed["etag"], feed["last_modified"])
            if result.data is None:  # 304 Not Modified
                await self._record_success(feed)
                return
            title, items = feed_reader.parse_feed(result.data)
        except FeedError as e:
            if isinstance(e, RateLimited):
                self.skip_until[feed["id"]] = time.monotonic() + e.retry_after
            failures = await db.record_feed_check(feed["id"], str(e))
            if failures == ALERT_AFTER_FAILURES:
                await self._alert(f"⚠️ Feed `#{feed['id']}` **{self._name(feed)}** has failed "
                                  f"{failures} checks in a row: {e}")
            return

        seen = await db.get_seen_feed_items(feed["id"])
        new = feed_reader.select_new(items, seen, MAX_POSTS_PER_CHECK)
        unposted = {i.key for i in new}
        role_id = feed["role_id"]
        mentions = discord.AllowedMentions(everyone=False, users=False,
                                           roles=[discord.Object(int(role_id))] if role_id else False)
        for item in new:
            try:
                await channel.send(feed_reader.format_post(title or feed["title"], item, role_id),
                                   allowed_mentions=mentions)
            except discord.HTTPException as e:
                logger.warning("Feed #%s: posting failed, retrying next check: %s", feed["id"], e)
                break
            unposted.discard(item.key)
        # Unposted new items stay unseen so the next check retries them; items
        # past MAX_POSTS_PER_CHECK are marked seen on purpose.
        await db.mark_feed_items_seen(feed["id"], [i.key for i in items if i.key not in unposted])
        await self._record_success(feed, result, title)
        if new:
            logger.info("Feed #%s: posted %d new item(s)", feed["id"], len(new) - len(unposted))

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
        return discord.utils.escape_markdown(feed["title"] or feed["url"])


async def setup(bot: commands.Bot):
    """Called by bot.py to load this cog."""
    await bot.add_cog(FeedCommands(bot))
