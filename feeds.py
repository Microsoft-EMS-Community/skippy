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
from config import (FEED_ALERT_CHANNEL_ID, FEED_CHANNELS, FEED_POLL_MINUTES, FEED_SUGGESTIONS_CHANNEL_ID,
                    FEED_THREAD_CHANNEL_IDS, RSS_ADMIN_ROLE_ID)
from feed_reader import FeedError, RateLimited

logger = logging.getLogger("followup-bot")

ALERT_AFTER_FAILURES = max(1, 60 // FEED_POLL_MINUTES)  # an hour of failed checks
CONCURRENT_CHECKS = 5     # feeds checked at once, so slow or dead ones can't hold up the rest
MAX_PENDING_SUGGESTIONS = 3  # per member, so nobody can flood the mods' queue
THREAD_ARCHIVE_MINUTES = 4320  # discussion threads on posts go quiet after 3 days

# A fixed list, not any channel: feeds only ever post where they're meant to.
# Values are strings because channel IDs are too big for Discord's integers.
CHANNEL_CHOICES = [app_commands.Choice(name=name, value=str(cid)) for name, cid in FEED_CHANNELS.items()]
DEFAULT_CHANNEL_ID = next(iter(FEED_CHANNELS.values()))
CLEARABLE_SETTINGS = [
    app_commands.Choice(name="role ping", value="role_id"),
    app_commands.Choice(name="author", value="author_id"),
    app_commands.Choice(name="include words", value="include_words"),
    app_commands.Choice(name="exclude words", value="exclude_words"),
    app_commands.Choice(name="highlight words", value="highlight_words"),
]


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
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

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

    async def _create_feed(self, guild: discord.Guild, url: str, channel_id: int, role_id: str | None,
                           author_id: str | None, added_by: str):
        """Check and add a feed; shared by /feed add and approving a suggestion.
        Returns (feed id, title, current items, channel). Raises
        FeedError with a message for mods, or sqlite3.IntegrityError if the
        URL already posts in that channel."""
        channel = guild.get_channel(channel_id) if channel_id in FEED_CHANNELS.values() else None
        if not isinstance(channel, discord.TextChannel):
            raise FeedError(f"feed channel {channel_id} doesn't exist anymore; update FEED_CHANNELS in config.py")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise FeedError(f"I can't post in {channel.mention}: I need View Channel, Send Messages and Embed Links there")
        async with feed_reader.session() as http:
            result = await feed_reader.fetch_feed(http, url)
            title, items = feed_reader.parse_feed(result.data)
        feed_id = await db.add_feed(str(guild.id), str(channel.id), url, title, role_id, added_by)
        # Everything already in the feed counts as seen, so adding a feed
        # doesn't dump its whole history into the channel.
        await db.mark_feed_items_seen(feed_id, [i.key for i in items])
        await db.record_feed_check(feed_id, None, result.etag, result.last_modified, title)
        if author_id:
            await self._link_author(feed_id, str(guild.id), author_id, added_by)
        return feed_id, title, items, channel

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
                           role="Optional role to ping with every post",
                           author="The member who writes it: credited and pinged on posts, and activity-tracked",
                           post_latest="Post its newest item right away, without pings (off: skip what's there now)")
    @app_commands.choices(channel=CHANNEL_CHOICES)
    @is_rss_admin()
    async def feed_add(self, interaction: discord.Interaction, url: str, channel: str | None = None,
                       role: discord.Role | None = None, author: discord.Member | None = None,
                       post_latest: bool = False):
        await interaction.response.defer(ephemeral=True)
        try:
            feed_id, title, items, target = await self._create_feed(
                interaction.guild, url, int(channel) if channel else DEFAULT_CHANNEL_ID,
                str(role.id) if role else None, str(author.id) if author else None, str(interaction.user.id))
        except FeedError as e:
            await interaction.followup.send(f"Couldn't add that feed: {e}.", ephemeral=True)
            return
        except sqlite3.IntegrityError:
            await interaction.followup.send("That feed already posts in that channel.", ephemeral=True)
            return

        notes = []
        if role and not role.mentionable and not target.permissions_for(interaction.guild.me).mention_everyone:
            notes.append(f"\n⚠️ {role.mention} isn't mentionable and I can't mention all roles, "
                         "so posts will show the role without pinging anyone.")
        if author:
            notes.append(f"\n👤 Posts credit {author.mention}, who's now on the RSS watchlist.")
        newest = feed_reader.select_new(items, set(), 1) if post_latest else []
        if newest:
            # The newest item as a real post (archived, credited, threaded),
            # just without pings: a visible sign the feed is connected.
            row = await db.get_feed(feed_id, str(interaction.guild_id))
            if await self._post(target, row, title, newest, ping=False):
                notes.append(f"\n📮 Posted its newest item in {target.mention}.")
            else:
                notes.append("\n⚠️ Couldn't post its newest item; see the bot's log.")
        await interaction.followup.send(
            f"✅ Added feed #{feed_id} **{discord.utils.escape_markdown(title or url)}** → {target.mention}. "
            f"Its {len(items)} current items are skipped; new ones are posted within {FEED_POLL_MINUTES} minutes. "
            f"Preview the format with `/feed test feed:{feed_id}`, and set filters with `/feed edit`."
            + "".join(notes),
            ephemeral=True)
        logger.info("Feed #%s added by %s: %s -> #%s", feed_id, interaction.user, url, target)

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
            author = f" · by <@{f['author_id']}>" if f["author_id"] else ""
            filters = " · 🔎 filtered" if f["include_words"] or f["exclude_words"] else ""
            name = discord.utils.escape_markdown(f["title"] or f["url"])
            lines.append(f"`#{f['id']}` **{name}** → <#{f['channel_id']}>{author}{role}{filters} · {status}")
        embed = discord.Embed(title="📰 Feeds", description="\n".join(lines)[:4000], color=discord.Color.blurple())
        embed.set_footer(text=f"{len(feeds)} feed(s) · checked every {FEED_POLL_MINUTES} minutes · "
                              "details: /feed edit")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @feed.command(name="edit", description="Change a feed's settings. With only a feed picked, shows them.")
    @app_commands.describe(
        feed="The feed to change",
        channel="Move it to another feed channel",
        role="Role to ping with every post",
        author="The member who writes it: credited and pinged on posts, and activity-tracked",
        include="Only post titles mentioning one of these words (comma-separated)",
        exclude="Never post titles mentioning any of these words (comma-separated)",
        highlight="In summaries, list titles mentioning these first, in bold (comma-separated)",
        summary_after="More new posts than this at once become one summary post (default 5)",
        clear="Remove one setting")
    @app_commands.choices(channel=CHANNEL_CHOICES, clear=CLEARABLE_SETTINGS)
    @is_rss_admin()
    async def feed_edit(self, interaction: discord.Interaction, feed: int, channel: str | None = None,
                        role: discord.Role | None = None, author: discord.Member | None = None,
                        include: str | None = None, exclude: str | None = None,
                        highlight: str | None = None,
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
        if author:
            await self._link_author(feed, guild_id, str(author.id), str(interaction.user.id))
            changes["author_id"] = str(author.id)
        row = await db.get_feed(feed, guild_id)
        verb = "Updated" if changes else "Settings for"
        await interaction.response.send_message(f"{verb} feed #{feed}:\n{self._describe(row)}", ephemeral=True)
        if changes:
            logger.info("Feed #%s edited by %s: %s", feed, interaction.user, changes)

    @blog.command(name="search", description="Search everything the feeds have posted.")
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

    # ──────────────────────────────────────────
    # Suggestions: members propose, mods approve with a button
    # ──────────────────────────────────────────

    @blog.command(name="suggest", description="Suggest a blog for the community feed. Mods approve it.")
    @app_commands.describe(url="The blog's RSS or Atom feed URL (often ends in /feed or /rss)",
                           own_blog="Is this your own blog? Then posts credit you (default: yes)",
                           note="Anything the mods should know")
    async def feed_suggest(self, interaction: discord.Interaction, url: str, own_blog: bool = True,
                           note: app_commands.Range[str, 1, 300] | None = None):
        await interaction.response.defer(ephemeral=True)
        guild_id, member_id = str(interaction.guild_id), str(interaction.user.id)
        if await db.feed_url_tracked(guild_id, url):
            await interaction.followup.send("That feed is already in the content tracker. 🎉", ephemeral=True)
            return
        if await db.pending_suggestions(guild_id, url=url):
            await interaction.followup.send("Someone already suggested that feed; the mods will look at it soon.",
                                            ephemeral=True)
            return
        if len(await db.pending_suggestions(guild_id, suggested_by=member_id)) >= MAX_PENDING_SUGGESTIONS:
            await interaction.followup.send(
                f"You have {MAX_PENDING_SUGGESTIONS} suggestions waiting already; please wait for the mods "
                "to get to those first.", ephemeral=True)
            return
        async with feed_reader.session() as http:
            try:
                title, items = feed_reader.parse_feed((await feed_reader.fetch_feed(http, url)).data)
            except FeedError as e:
                await interaction.followup.send(
                    f"I couldn't read that as a feed: {e}. Look for the blog's RSS or Atom link; "
                    "it often ends in `/feed`, `/rss` or `/atom.xml`.", ephemeral=True)
                return

        suggestion_id = await db.add_suggestion(guild_id, url, title, member_id, own_blog, note)
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
            joined = interaction.user.joined_at
            embed.add_field(name="Suggested by",
                            value=f"{interaction.user.mention} ({'their own blog' if own_blog else 'not their blog'})"
                                  + (f", member since <t:{int(joined.timestamp())}:R>" if joined else ""),
                            inline=False)
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

    async def approve_suggestion(self, interaction: discord.Interaction, suggestion_id: int):
        await interaction.response.defer()
        if not await db.decide_suggestion(suggestion_id, "approved", str(interaction.user.id)):
            await interaction.followup.send("Another mod already decided on this one.", ephemeral=True)
            return
        suggestion = await db.get_suggestion(suggestion_id)
        author_id = suggestion["suggested_by"] if suggestion["own_blog"] else None
        try:
            feed_id, title, _, channel = await self._create_feed(
                interaction.guild, suggestion["url"], DEFAULT_CHANNEL_ID, None, author_id, str(interaction.user.id))
        except (FeedError, sqlite3.IntegrityError) as e:
            await db.decide_suggestion(suggestion_id, "pending", None)  # back in the queue, buttons stay
            problem = "it already posts in that channel" if isinstance(e, sqlite3.IntegrityError) else str(e)
            await interaction.followup.send(f"Couldn't add it: {problem}. The suggestion is still open.",
                                            ephemeral=True)
            return
        credit = f", credited to <@{author_id}>" if author_id else ""
        await self._close_card(interaction.message, discord.Color.green(),
                               f"✅ Approved by {interaction.user.mention}: feed #{feed_id} → {channel.mention}{credit}")
        await self._dm(suggestion["suggested_by"],
                       f"🎉 Your suggestion **{discord.utils.escape_markdown(title or suggestion['url'])}** was "
                       f"approved! New posts will appear in {channel.mention}"
                       + (", with a shout-out to you." if author_id else "."))
        logger.info("Feed suggestion #%s approved by %s as feed #%s", suggestion_id, interaction.user, feed_id)

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
                title, items = feed_reader.parse_feed((await feed_reader.fetch_feed(http, row["url"])).data)
            except FeedError as e:
                await interaction.followup.send(f"Couldn't read the feed: {e}.", ephemeral=True)
                return
        newest = feed_reader.select_new(items, set(), 1)
        if not newest:
            await interaction.followup.send("The feed has no items right now.", ephemeral=True)
            return
        # The test post shows the author credit but never pings anyone.
        await channel.send(feed_reader.format_post(title or row["title"], newest[0], author_id=row["author_id"]),
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

    async def _post(self, channel, feed: dict, name: str, items: list, ping: bool = True) -> set[str]:
        """Post items one by one, or as one summary when there are more than
        the feed's summary_after. Archives what was posted and returns its keys."""
        if not items:
            return set()
        role_id, author_id = feed["role_id"], feed["author_id"]
        # Only the feed's own role and author can be pinged, never anyone else.
        mentions = discord.AllowedMentions(everyone=False,
                                           users=[discord.Object(int(author_id))] if author_id and ping else False,
                                           roles=[discord.Object(int(role_id))] if role_id and ping else False)
        posted, archive = set(), []
        try:
            if len(items) > feed["summary_after"]:
                highlight = feed_reader.word_list(feed["highlight_words"])
                for part in feed_reader.format_summary(name, items, highlight, role_id, author_id=author_id):
                    message = await channel.send(part, allowed_mentions=mentions)
                    if not posted:  # once the first part is out, don't repeat the summary on a retry
                        posted = {i.key for i in items}
                        archive = [(i, message.jump_url) for i in items]
                    mentions = discord.AllowedMentions.none()  # ping once, not for every part
            else:
                for item in items:
                    message = await channel.send(feed_reader.format_post(name, item, role_id, author_id),
                                                 allowed_mentions=mentions)
                    posted.add(item.key)
                    archive.append((item, message.jump_url))
                    if channel.id in FEED_THREAD_CHANNEL_IDS:
                        await self._open_thread(message, item.title)
        except discord.HTTPException as e:
            logger.warning("Feed #%s: posting failed, retrying next check: %s", feed["id"], e)
        if archive:
            await db.archive_feed_posts([(feed["id"], name, i.title, feed_reader.clean_url(i.link), url)
                                         for i, url in archive])
        return posted

    @staticmethod
    async def _open_thread(message: discord.Message, title: str):
        """A discussion thread on the post itself. Failing here (say, a missing
        Create Public Threads permission) must not count the post as unposted."""
        try:
            await message.create_thread(name=" ".join(title.split())[:100] or "Discussion",
                                        auto_archive_duration=THREAD_ARCHIVE_MINUTES)
        except discord.HTTPException as e:
            logger.warning("Couldn't open a thread on %s: %s", message.jump_url, e)

    @staticmethod
    def _describe(feed: dict) -> str:
        none = "—"
        role = f"<@&{feed['role_id']}>" if feed["role_id"] else none
        author = f"<@{feed['author_id']}>" if feed["author_id"] else none
        return "\n".join([
            f"**{discord.utils.escape_markdown(feed['title'] or feed['url'])}** <{feed['url']}>",
            f"Channel: <#{feed['channel_id']}>",
            f"Ping role: {role}",
            f"Author: {author}",
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
    bot.add_dynamic_items(SuggestionButton)  # cards' buttons keep working across restarts
    await bot.add_cog(FeedCommands(bot))
