# ============================================================
# feeds.py - /feed, for mods: add, edit, test and remove the RSS/Atom
# feeds Skippy posts, plus the weekly roundup and the activity rule.
# The work behind it lives in feed_registry.py (the feeds), feed_poller.py
# (checking them), feed_posting.py (posting), feed_roundup.py,
# feed_activity.py and feed_reader.py (reading feeds).
# ============================================================

import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import feed_activity
import feed_notices
import feed_poller
import feed_posting
import feed_reader
import feed_registry
import feed_roundup
import profile_links
from checks import is_rss_admin
from config import (FEED_CHANNELS, FEED_COMMUNITY_CHANNEL_ID, FEED_POLL_MINUTES, FEED_ROUNDUP_CHANNEL_ID,
                    FEED_ROUNDUP_TIMEZONE, RSS_ADMIN_ROLE_ID)
from feed_reader import FeedError
from feed_registry import DEFAULT_CHANNEL_ID, feed_name, shown_name

# The activity rule for feeds linked to an author, switched by mods with
# /feed activity: phase it in by watching first, then reminding, then pausing.
ACTIVITY_MODES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="watch: only show authors' activity", value="watch"),
    app_commands.Choice(name="warn: remind inactive authors, keep posting", value="warn"),
    app_commands.Choice(name="enforce: pause inactive authors' posts", value="enforce"),
]
ROUNDUP_ACTIONS = [
    app_commands.Choice(name="preview (only you see it)", value="preview"),
    app_commands.Choice(name="post it now", value="post"),
    app_commands.Choice(name="weekly roundup on", value="on"),
    app_commands.Choice(name="weekly roundup off", value="off"),
]
# A fixed list, not any channel: feeds only ever post where they're meant to.
# Values are strings because channel IDs are too big for Discord's integers.
CHANNEL_CHOICES = [app_commands.Choice(name=name, value=str(cid)) for name, cid in FEED_CHANNELS.items()]
CLEARABLE_SETTINGS = [
    app_commands.Choice(name="author", value="author_id"),
    app_commands.Choice(name="name (use the feed's own title)", value="display_name"),
    app_commands.Choice(name="include words", value="include_words"),
    app_commands.Choice(name="exclude words", value="exclude_words"),
    app_commands.Choice(name="highlight words", value="highlight_words"),
] + [app_commands.Choice(name=f"{label} link", value=f"link:{site}") for site, label in profile_links.SITES.items()]


class FeedCommands(commands.Cog):
    """/feed: managing the feeds Skippy posts."""

    # Mods: hidden from members without Manage Roles (which the Moderator role
    # has). That's only visibility; is_rss_admin is the real check, and server
    # admins can change who sees it under Server Settings > Integrations.
    feed = app_commands.Group(name="feed", description="Manage the RSS/Atom feeds Skippy posts (mods).",
                              default_permissions=discord.Permissions(manage_roles=True), guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await feed_notices.command_failed(interaction, error, f"You need the <@&{RSS_ADMIN_ROLE_ID}> role to manage feeds.")

    @feed.command(name="add", description="Post a feed's new items in one of the feed channels.")
    @app_commands.describe(url="The RSS or Atom feed URL",
                           channel=f"Where to post (default: #{next(iter(FEED_CHANNELS))})",
                           author="The member who writes it: credited on posts, and activity-tracked",
                           name="What posts call it, if the feed's own title isn't good (e.g. just \"Blog\")",
                           **profile_links.OPTION_TEXT)
    @app_commands.choices(channel=CHANNEL_CHOICES)
    @is_rss_admin()
    async def feed_add(self, interaction: discord.Interaction, url: str, channel: str | None = None,
                       author: discord.Member | None = None,
                       name: app_commands.Range[str, 1, 80] | None = None, linkedin: str | None = None,
                       x: str | None = None, youtube: str | None = None, github: str | None = None):
        await interaction.response.defer(ephemeral=True)
        url = feed_reader.normalize_url(url)
        try:
            links = profile_links.read_all(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:
            await interaction.followup.send(f"Couldn't add that feed: {e}.", ephemeral=True)
            return
        if links and not author:
            await interaction.followup.send("Profile links belong to the blog's author; pick the `author` too.",
                                            ephemeral=True)
            return
        try:
            feed_id, title, items, target = await feed_registry.create_feed(
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
            if await feed_posting.post_items(target, row, feed_name(row, title), newest):
                notes.append(f"\n📮 Posted its newest item in {target.mention}.")
            else:
                notes.append("\n⚠️ Couldn't post its newest item; see the bot's log.")
        await interaction.followup.send(
            f"✅ Added feed #{feed_id} **{discord.utils.escape_markdown(feed_name(row, title))}** → {target.mention}. "
            f"Its {len(items)} current items are skipped; new ones are posted within {FEED_POLL_MINUTES} minutes. "
            f"Preview the format with `/feed test feed:{feed_id}`, and set filters with `/feed edit`."
            + "".join(notes),
            ephemeral=True)
        await feed_notices.audit(self.bot, interaction, "add", f"added feed #{feed_id} **{shown_name(row)}** → {target.mention}"
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
        await feed_notices.audit(self.bot, interaction, "remove", f"removed feed #{feed} **{shown_name(row)}**")

    @feed.command(name="list", description="Show all feeds and whether they're working.")
    @is_rss_admin()
    async def feed_list(self, interaction: discord.Interaction):
        feeds = await db.get_feeds(str(interaction.guild_id))
        if not feeds:
            await interaction.response.send_message("No feeds yet. Add one with `/feed add`.", ephemeral=True)
            return
        mode, min_messages = await feed_activity.current_rule()
        lines = []
        for f in feeds:
            status = f"⚠️ failing: {f['last_error']}" if f["failures"] else "✅"
            author = f" · by <@{f['author_id']}>" if f["author_id"] else ""
            if f["author_id"] and mode != "off" and not f["activity_exempt"]:
                author += " " + feed_activity.BADGES[(await feed_activity.author_status(f["author_id"], min_messages))[0]]
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
        url="Its new RSS or Atom address, if the blog moved",
        channel="Move it to another feed channel",
        author="The member who writes it: credited on posts, and activity-tracked",
        name="What posts call it, instead of the feed's own title",
        include="Only post titles mentioning one of these words (comma-separated)",
        exclude="Never post titles mentioning any of these words (comma-separated)",
        highlight="In summaries, list titles mentioning these first, in bold (comma-separated)",
        summary_after="More new posts than this at once become one summary post (default 5)",
        activity_exempt="Exempt this feed from the activity rule (see /feed activity)",
        clear="Remove one setting",
        **{site: f"The author's {text}" for site, text in profile_links.OPTION_TEXT.items()})
    @app_commands.choices(channel=CHANNEL_CHOICES, clear=CLEARABLE_SETTINGS)
    @is_rss_admin()
    async def feed_edit(self, interaction: discord.Interaction, feed: int, url: str | None = None,
                        channel: str | None = None,
                        author: discord.Member | None = None,
                        name: app_commands.Range[str, 1, 80] | None = None,
                        include: str | None = None, exclude: str | None = None,
                        highlight: str | None = None,
                        summary_after: app_commands.Range[int, 1, 25] | None = None,
                        activity_exempt: bool | None = None, clear: str | None = None,
                        linkedin: str | None = None, x: str | None = None, youtube: str | None = None,
                        github: str | None = None):
        if url:  # fetching the new address can take longer than Discord waits for a reply
            await interaction.response.defer(ephemeral=True)
        send = interaction.followup.send if url else interaction.response.send_message
        guild_id = str(interaction.guild_id)
        current = await db.get_feed(feed, guild_id)
        if not current:
            await send(f"There's no feed #{feed}.", ephemeral=True)
            return
        # Profile links belong to the feed's author, so they need one.
        try:
            links = profile_links.read_all(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:
            await send(f"Couldn't change that: {e}.", ephemeral=True)
            return
        if clear and clear.startswith("link:"):
            links[clear.removeprefix("link:")] = None
            clear = None
        links_of = str(author.id) if author else current["author_id"]
        if links and not links_of:
            await send("Profile links belong to the blog's author, and this feed has none; set `author` too.",
                       ephemeral=True)
            return
        if author and (blog := await feed_registry.feed_by_author(guild_id, str(author.id), ignore_feed=feed)):
            await send(f"{author.mention} already has a blog on the feed: feed #{blog['id']} **{shown_name(blog)}**. "
                       f"Each member has one; to swap, clear that feed's author first "
                       f"(`/feed edit feed:{blog['id']} clear:author`).", ephemeral=True)
            return
        changes = {}
        if channel:
            if int(channel) not in FEED_CHANNELS.values():
                await send("That isn't one of the feed channels.", ephemeral=True)
                return
            row = await db.get_feed(feed, guild_id)
            duplicate = await feed_registry.duplicate_of(guild_id, row["url"], await db.feed_item_ids(feed), int(channel),
                                                 ignore_feed=feed)
            if duplicate:
                await send(f"Feed #{duplicate['id']} **{shown_name(duplicate)}** already posts that blog in <#{channel}>.",
                           ephemeral=True)
                return
            changes["channel_id"] = channel
        if name and name.strip():
            changes["display_name"] = name.strip()
        for setting, value in (("include_words", include), ("exclude_words", exclude),
                               ("highlight_words", highlight)):
            if value is not None:
                changes[setting] = ", ".join(feed_reader.word_list(value)) or None
        if url:
            url = feed_reader.normalize_url(url)
            try:
                async with feed_reader.session() as http:
                    result = await feed_reader.fetch_feed(http, url)
                    title, items = feed_reader.parse_feed(result.data, url)
            except FeedError as e:
                await send(f"Couldn't change the address: {e}.", ephemeral=True)
                return
            target = int(changes.get("channel_id") or current["channel_id"])
            duplicate = await feed_registry.duplicate_of(guild_id, url, feed_reader.item_ids(items), target, ignore_feed=feed)
            if duplicate:
                await send(f"That's the same blog as feed #{duplicate['id']} **{shown_name(duplicate)}**, "
                           f"which already posts in <#{target}>.", ephemeral=True)
                return
            changes["url"] = url
        if summary_after:
            changes["summary_after"] = summary_after
        if activity_exempt is not None:
            changes["activity_exempt"] = int(activity_exempt)
        if clear:
            changes[clear] = None
        try:
            await db.update_feed(feed, guild_id, **changes)
        except sqlite3.IntegrityError:
            await send("That feed already posts in that channel.", ephemeral=True)
            return
        if url:
            # What's at the new address counts as seen, like when a feed is
            # added, so moving a blog doesn't repost its history.
            await db.mark_feed_items_seen(feed, [i.key for i in items])
            await feed_poller.record_success(self.bot, current, result, title)  # tells the mods if it was failing
        if author:
            await feed_registry.link_author(feed, guild_id, str(author.id), str(interaction.user.id))
            changes["author_id"] = str(author.id)
        if links:
            await db.set_author_links(links_of, links)
            changes["links"] = links  # for the audit line
        row = await db.get_feed(feed, guild_id)
        verb = "Updated" if changes else "Settings for"
        shown_links = await db.get_author_links(row["author_id"]) if row["author_id"] else {}
        await send(f"{verb} feed #{feed}:\n{self._describe(row, shown_links)}", ephemeral=True)
        if changes:
            labels = {"url": "address", "channel_id": "channel", "author_id": "author", "display_name": "name",
                      "include_words": "include words", "exclude_words": "exclude words",
                      "highlight_words": "highlight words", "summary_after": "summary threshold",
                      "activity_exempt": "activity exemption", "links": "profile links"}
            await feed_notices.audit(self.bot, interaction, "edit", f"edited feed #{feed} **{shown_name(row)}**: "
                              + ", ".join(labels[k] for k in changes))

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
        if not await feed_posting.post_items(channel, row, feed_name(row, title), newest):
            await interaction.followup.send("Couldn't post it; see the bot's log.", ephemeral=True)
            return
        await interaction.followup.send(f"Posted the newest item in {channel.mention}.", ephemeral=True)
        await feed_notices.audit(self.bot, interaction, "test", f"test-posted feed #{feed} **{shown_name(row)}** in {channel.mention}")

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

    @feed.command(name="roundup", description="The weekly community roundup: preview it, post it now, or switch it.")
    @app_commands.describe(action="What to do (default: a preview only you see)")
    @app_commands.choices(action=ROUNDUP_ACTIONS)
    @is_rss_admin()
    async def feed_roundup(self, interaction: discord.Interaction, action: str = "preview"):
        when = f"Mondays 09:00 ({FEED_ROUNDUP_TIMEZONE}) in <#{FEED_ROUNDUP_CHANNEL_ID}>"
        if action in ("on", "off"):
            await db.set_setting("feed_roundup", action)
            await feed_notices.audit(self.bot, interaction, "roundup", f"switched the weekly roundup **{action}**")
            await interaction.response.send_message(
                f"Weekly roundup is now **{action}**" + (f": {when}." if action == "on" else "."), ephemeral=True)
            return
        now = datetime.now(feed_roundup.ROUNDUP_TZ)
        await interaction.response.defer(ephemeral=True)  # counting votes can take longer than Discord's 3 s
        embed = await feed_roundup.this_weeks_roundup(self.bot, now)
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
        await db.set_setting("feed_roundup_last_week", feed_roundup.week_of(now))
        await reply(f"Posted the roundup in {channel.mention}.", ephemeral=True)
        await feed_notices.audit(self.bot, interaction, "roundup", f"posted the weekly roundup now in {channel.mention}")

    @feed.command(name="activity", description="The activity rule for authors' feeds: see who it affects, or change it.")
    @app_commands.describe(mode="Phase it in: watch first, then warn, then enforce",
                           min_messages=f"Messages in the last {feed_activity.ACTIVITY_DAYS} days that count as active (default 1)")
    @app_commands.choices(mode=ACTIVITY_MODES)
    @is_rss_admin()
    async def feed_activity(self, interaction: discord.Interaction, mode: str | None = None,
                            min_messages: app_commands.Range[int, 1, 50] | None = None):
        if mode:
            await db.set_setting("feed_activity_mode", mode)
        if min_messages:
            await db.set_setting("feed_activity_min_messages", str(min_messages))
        changed = bool(mode or min_messages)
        mode, min_messages = await feed_activity.current_rule()
        if changed:
            await feed_notices.audit(self.bot, interaction, "activity", f"set the activity rule to **{mode}** "
                              f"(active = {min_messages}+ messages in {feed_activity.ACTIVITY_DAYS} days)")

        by_author: dict[str, list[dict]] = {}
        for f in await db.get_feeds(str(interaction.guild_id)):
            if f["author_id"]:
                by_author.setdefault(f["author_id"], []).append(f)
        lines = []
        for author_id, author_feeds in by_author.items():
            status, count = await feed_activity.author_status(author_id, min_messages)
            paused = await db.get_author_state(author_id) == "paused"
            names = ", ".join(shown_name(f) + (" (exempt)" if f["activity_exempt"] else "") for f in author_feeds)
            lines.append(f"{feed_activity.BADGES[status]} <@{author_id}> · {count} msg · {names}"
                         + (" · ⏸️ paused" if paused else ""))
        effect = {
            "off": "Nothing happens; authors' activity isn't checked.",
            "watch": "Activity is shown here and nothing else happens: a good first phase to see who'd be affected.",
            "warn": "Authors who go 🔴 get one friendly DM and mods get a notice. Their posts keep going.",
            "enforce": "🔴 authors' new posts are skipped until they're active again; they and the mods are told both ways.",
        }[mode]
        embed = discord.Embed(
            title=f"Activity rule: {mode}",
            description=(f"{effect}\nActive means {min_messages}+ message(s) in the last {feed_activity.ACTIVITY_DAYS} days. "
                         f"🆕 = linked less than {feed_activity.ACTIVITY_DAYS} days ago (grace period). Feeds without an author, "
                         "and exempt ones, are never affected.\n\n"
                         + ("\n".join(lines) or "No feeds are linked to an author yet."))[:4000],
            color=discord.Color.blurple())
        embed.set_footer(text="Exempt a feed with /feed edit feed:<feed> activity_exempt:True")
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _describe(feed: dict, links: dict[str, str] | None = None) -> str:
        none = "—"
        shown = " · ".join(f"[{label}](<{links[site]}>)" for site, label in profile_links.SITES.items()
                           if (links or {}).get(site))
        author = f"<@{feed['author_id']}>" if feed["author_id"] else none
        checked = (f"<t:{int(datetime.fromisoformat(feed['last_checked']).replace(tzinfo=timezone.utc).timestamp())}:R>"
                   if feed["last_checked"] else "not yet")  # shows as "2 minutes ago", in the viewer's own time
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
            f"Last checked: {checked}",
        ])


async def setup(bot: commands.Bot):
    await bot.add_cog(FeedCommands(bot))
