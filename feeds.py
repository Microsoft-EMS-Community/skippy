# ============================================================
# feeds.py - /feed commands and the loop that posts new feed items
# Fetching and parsing live in feed_reader.py.
# ============================================================

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import feed_reader
from commands import is_rss_admin
from config import FEED_ALERT_CHANNEL_ID, FEED_CHANNELS, FEED_POLL_MINUTES
from feed_reader import FeedError, RateLimited

logger = logging.getLogger("followup-bot")

ALERT_AFTER_FAILURES = max(1, 60 // FEED_POLL_MINUTES)  # an hour of failed checks
CONCURRENT_CHECKS = 5     # feeds checked at once, so slow or dead ones can't hold up the rest

# A fixed list, not any channel: feeds only ever post where they're meant to.
# Values are strings because channel IDs are too big for Discord's integers.
CHANNEL_CHOICES = [app_commands.Choice(name=name, value=str(cid)) for name, cid in FEED_CHANNELS.items()]
CLEARABLE_SETTINGS = [
    app_commands.Choice(name="role ping", value="role_id"),
    app_commands.Choice(name="include words", value="include_words"),
    app_commands.Choice(name="exclude words", value="exclude_words"),
    app_commands.Choice(name="highlight words", value="highlight_words"),
]


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
    @app_commands.choices(channel=CHANNEL_CHOICES)
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
            f"Preview the format with `/feed test feed:{feed_id}`, and set filters with `/feed edit`."
            + "".join(notes),
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
            filters = " · 🔎 filtered" if f["include_words"] or f["exclude_words"] else ""
            name = discord.utils.escape_markdown(f["title"] or f["url"])
            lines.append(f"`#{f['id']}` **{name}** → <#{f['channel_id']}>{role}{filters} · {status}")
        embed = discord.Embed(title="📰 Feeds", description="\n".join(lines)[:4000], color=discord.Color.blurple())
        embed.set_footer(text=f"{len(feeds)} feed(s) · checked every {FEED_POLL_MINUTES} minutes · "
                              "details: /feed edit")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @feed.command(name="edit", description="Change a feed's settings. With only a feed picked, shows them.")
    @app_commands.describe(
        feed="The feed to change",
        channel="Move it to another feed channel",
        role="Role to ping with every post",
        include="Only post titles mentioning one of these words (comma-separated)",
        exclude="Never post titles mentioning any of these words (comma-separated)",
        highlight="In summaries, list titles mentioning these first, in bold (comma-separated)",
        summary_after="More new posts than this at once become one summary post (default 5)",
        clear="Remove one setting")
    @app_commands.choices(channel=CHANNEL_CHOICES, clear=CLEARABLE_SETTINGS)
    @is_rss_admin()
    async def feed_edit(self, interaction: discord.Interaction, feed: int, channel: str | None = None,
                        role: discord.Role | None = None, include: str | None = None,
                        exclude: str | None = None, highlight: str | None = None,
                        summary_after: app_commands.Range[int, 1, 25] | None = None,
                        clear: str | None = None):
        guild_id = str(interaction.guild_id)
        if not await db.get_feed(feed, guild_id):
            await interaction.response.send_message(f"There's no feed #{feed}.", ephemeral=True)
            return
        changes = {}
        if channel:
            if int(channel) not in FEED_CHANNELS.values():
                await interaction.response.send_message("That isn't one of the feed channels.", ephemeral=True)
                return
            changes["channel_id"] = channel
        if role:
            changes["role_id"] = str(role.id)
        for name, value in (("include_words", include), ("exclude_words", exclude),
                            ("highlight_words", highlight)):
            if value is not None:
                changes[name] = ", ".join(feed_reader.word_list(value)) or None
        if summary_after:
            changes["summary_after"] = summary_after
        if clear:
            changes[clear] = None
        try:
            await db.update_feed(feed, guild_id, **changes)
        except sqlite3.IntegrityError:
            await interaction.response.send_message("That feed already posts in that channel.", ephemeral=True)
            return
        row = await db.get_feed(feed, guild_id)
        verb = "Updated" if changes else "Settings for"
        await interaction.response.send_message(f"{verb} feed #{feed}:\n{self._describe(row)}", ephemeral=True)
        if changes:
            logger.info("Feed #%s edited by %s: %s", feed, interaction.user, changes)

    @feed.command(name="search", description="Search everything the feeds have posted.")
    @app_commands.describe(words="Words to look for in post titles and feed names")
    async def feed_search(self, interaction: discord.Interaction, words: app_commands.Range[str, 2, 100]):
        results = await db.search_feed_posts(words.split()[:5])
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
    @feed_edit.autocomplete("feed")
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
        new = feed_reader.select_new(items, seen, len(items))
        include = feed_reader.word_list(feed["include_words"])
        exclude = feed_reader.word_list(feed["exclude_words"])
        to_post = [i for i in new if feed_reader.wanted(i, include, exclude)]
        posted = await self._post(channel, feed, title or feed["title"], to_post)
        # Filtered-out items are marked seen (skipped for good). Wanted items
        # that didn't make it to Discord stay unseen, so the next check retries.
        unposted = {i.key for i in to_post} - posted
        await db.mark_feed_items_seen(feed["id"], [i.key for i in items if i.key not in unposted])
        await self._record_success(feed, result, title)
        if new:
            logger.info("Feed #%s: %d new, %d filtered out, %d posted",
                        feed["id"], len(new), len(new) - len(to_post), len(posted))

    async def _post(self, channel, feed: dict, name: str, items: list) -> set[str]:
        """Post items one by one, or as one summary when there are more than
        the feed's summary_after. Archives what was posted and returns its keys."""
        if not items:
            return set()
        role_id = feed["role_id"]
        mentions = discord.AllowedMentions(everyone=False, users=False,
                                           roles=[discord.Object(int(role_id))] if role_id else False)
        posted, archive = set(), []
        try:
            if len(items) > feed["summary_after"]:
                highlight = feed_reader.word_list(feed["highlight_words"])
                for part in feed_reader.format_summary(name, items, highlight, role_id):
                    message = await channel.send(part, allowed_mentions=mentions)
                    if not posted:  # once the first part is out, don't repeat the summary on a retry
                        posted = {i.key for i in items}
                        archive = [(i, message.jump_url) for i in items]
                    mentions = discord.AllowedMentions.none()  # ping once, not for every part
            else:
                for item in items:
                    message = await channel.send(feed_reader.format_post(name, item, role_id),
                                                 allowed_mentions=mentions)
                    posted.add(item.key)
                    archive.append((item, message.jump_url))
        except discord.HTTPException as e:
            logger.warning("Feed #%s: posting failed, retrying next check: %s", feed["id"], e)
        if archive:
            await db.archive_feed_posts([(feed["id"], name, i.title, feed_reader.clean_url(i.link), url)
                                         for i, url in archive])
        return posted

    @staticmethod
    def _describe(feed: dict) -> str:
        none = "—"
        role = f"<@&{feed['role_id']}>" if feed["role_id"] else none
        return "\n".join([
            f"**{discord.utils.escape_markdown(feed['title'] or feed['url'])}** <{feed['url']}>",
            f"Channel: <#{feed['channel_id']}>",
            f"Ping role: {role}",
            f"Only titles with: {feed['include_words'] or 'anything'}",
            f"Never titles with: {feed['exclude_words'] or none}",
            f"Highlight in summaries: {feed['highlight_words'] or none}",
            f"Summary when more than {feed['summary_after']} new posts at once",
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
        return discord.utils.escape_markdown(feed["title"] or feed["url"])


async def setup(bot: commands.Bot):
    """Called by bot.py to load this cog."""
    await bot.add_cog(FeedCommands(bot))
