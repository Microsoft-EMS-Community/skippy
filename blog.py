# ============================================================
# blog.py - /blog, for everyone: suggest a member's blog for the community
# feed (mods approve or reject it with buttons), search what the feeds
# have posted, and set the profile links shown under your posts.
# ============================================================

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import feed_notices
import feed_posting
import feed_reader
import feed_registry
import profile_links
from checks import is_moderator
from config import (FEED_CHANNELS, FEED_COMMUNITY_CHANNEL_ID, FEED_LINK_EMOJIS, FEED_SUGGESTIONS_CHANNEL_ID,
                    RSS_ADMIN_ROLE_ID)
from feed_reader import FeedError
from feed_registry import feed_name, shown_name

logger = logging.getLogger("followup-bot")

MAX_PENDING_SUGGESTIONS = 3  # per member, so nobody can flood the mods' queue
REFUSAL_REPEAT_SECONDS = 3600  # a member retrying the same refused blog is reported to the mods once an hour

LINK_CHOICES = ([app_commands.Choice(name=label, value=site) for site, label in profile_links.SITES.items()]
                + [app_commands.Choice(name="all of them", value="all")])


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
        if is_moderator(interaction.user):
            return True
        await interaction.response.send_message(f"Only <@&{RSS_ADMIN_ROLE_ID}>s can decide on suggestions.",
                                                ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("BlogCommands")
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
        cog = interaction.client.get_cog("BlogCommands")
        await cog.reject_suggestion(interaction, self.suggestion_id, self.reason.value.strip() or None)


class BlogCommands(commands.Cog):
    """/blog suggest, search and links, and deciding on suggestions."""

    blog = app_commands.Group(name="blog", description="Suggest your blog, or search the community content tracker.",
                              guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.refusals_told: dict[tuple[str, str], float] = {}  # (member, blog) -> when the mods were last told

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await feed_notices.command_failed(interaction, error, "You can't use that here.")

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

    # Members can only suggest members' blogs: every suggestion names the
    # member who writes it (a required choice, even when it's the suggester),
    # who's credited on posts and covered by the activity rule. Feeds without
    # an author are for mods to add (/feed add).
    @blog.command(name="suggest", description="Suggest a member's blog for the community feed. Mods approve it.")
    @app_commands.describe(url="The blog's RSS or Atom feed URL (often ends in /feed or /rss)",
                           author="The member who writes it: pick yourself if it's your blog",
                           note="Anything the mods should know",
                           **{site: f"Optional: the author's {text}" for site, text in profile_links.OPTION_TEXT.items()})
    async def feed_suggest(self, interaction: discord.Interaction, url: str, author: discord.Member,
                           note: app_commands.Range[str, 1, 300] | None = None, linkedin: str | None = None,
                           x: str | None = None, youtube: str | None = None, github: str | None = None):
        await interaction.response.defer(ephemeral=True)
        url = feed_reader.normalize_url(url)
        try:
            links = profile_links.read_all(linkedin=linkedin, x=x, youtube=youtube, github=github)
        except ValueError as e:  # a typo, not a refusal: nothing for the mods
            await interaction.followup.send(f"Couldn't send that suggestion: {e}.", ephemeral=True)
            return
        guild_id, member_id, author_id = str(interaction.guild_id), str(interaction.user.id), str(author.id)
        yours = author_id == member_id
        if author.bot:
            await self._refuse(interaction, url, author, "The author has to be a member who writes the blog, "
                               "not a bot.", "the author is a bot")
            return
        if duplicate := await feed_registry.duplicate_of(guild_id, url, set()):
            await self._refuse(interaction, url, author, self._already_tracked(duplicate, member_id, author_id),
                               self._already_tracked_reason(duplicate))
            return
        if blog := await feed_registry.feed_by_author(guild_id, author_id):
            has = "You already have" if yours else f"{author.mention} already has"
            await self._refuse(interaction, url, author,
                               f"{has} a blog on the feed: **{shown_name(blog)}**. Each member has one; ask a mod "
                               "if it should be swapped.",
                               f"{author.mention} already has a blog on the feed: feed #{blog['id']} "
                               f"**{shown_name(blog)}**")
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
        if duplicate := await feed_registry.duplicate_of(guild_id, url, feed_reader.item_ids(items)):
            await self._refuse(interaction, url, author, self._already_tracked(duplicate, member_id, author_id),
                               self._already_tracked_reason(duplicate))
            return

        suggestion_id = await db.add_suggestion(guild_id, url, title, member_id, author_id, note, links)
        cards = self.bot.get_channel(FEED_SUGGESTIONS_CHANNEL_ID)
        if cards is None:
            logger.error("Feed suggestions channel %s not found; suggestion #%s is only in the database",
                         FEED_SUGGESTIONS_CHANNEL_ID, suggestion_id)
        else:
            card = await cards.send(embed=self._suggestion_card(suggestion_id, url, title, items, author,
                                                                interaction.user, links, note),
                                    view=self._decision_buttons(suggestion_id),
                                    allowed_mentions=discord.AllowedMentions.none())
            await db.set_suggestion_message(suggestion_id, str(card.id))
        await interaction.followup.send(
            f"🙏 Thanks! **{discord.utils.escape_markdown(title or url)}** went to the mods. "
            "I'll DM you when they've decided.", ephemeral=True)
        logger.info("Feed suggestion #%s by %s: %s", suggestion_id, interaction.user, url)

    @staticmethod
    def _suggestion_card(suggestion_id: int, url: str, title: str | None, items: list, author: discord.Member,
                         suggested_by, links: dict[str, str], note: str | None) -> discord.Embed:
        """The card the mods decide on."""
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
                              + ("" if author == suggested_by else f"\nSuggested by {suggested_by.mention}"),
                        inline=False)
        if links:
            embed.add_field(name="Profile links", value=profile_links.line(links, FEED_LINK_EMOJIS), inline=False)
        if note:
            embed.add_field(name="Note", value=note, inline=False)
        embed.set_footer(text=f"Approving adds it to #{next(iter(FEED_CHANNELS))}; change it afterwards "
                              "with /feed edit.")
        return embed

    @staticmethod
    def _decision_buttons(suggestion_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(SuggestionButton("approve", suggestion_id))
        view.add_item(SuggestionButton("reject", suggestion_id))
        return view

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
        await feed_notices.alert(self.bot, f"🚫 {member.mention} {text}")

    @staticmethod
    def _already_tracked_reason(feed: dict) -> str:
        """For the mods: which feed a refused suggestion duplicates, and whom it credits."""
        credit = f"credited to <@{feed['author_id']}>" if feed["author_id"] else "no author yet"
        return f"it's already on the feed as feed #{feed['id']} **{shown_name(feed)}** ({credit})"

    @staticmethod
    def _already_tracked(feed: dict, member_id: str, author_id: str) -> str:
        """The answer to suggesting a blog that's already on the feed."""
        text = f"That blog is already on the feed as **{shown_name(feed)}**. 🎉"
        who = "you" if author_id == member_id else f"<@{author_id}>"
        if feed["author_id"] == author_id:
            return text + f" Its posts credit {who}."
        if not feed["author_id"]:
            return text + f" Ask a mod to credit {who} on its posts."
        return text

    @blog.command(name="links", description="Show your LinkedIn, X, YouTube or GitHub under your blog's posts.")
    @app_commands.describe(clear="Remove one of your links", **profile_links.OPTION_TEXT)
    @app_commands.choices(clear=LINK_CHOICES)
    async def blog_links(self, interaction: discord.Interaction, linkedin: str | None = None,
                         x: str | None = None, youtube: str | None = None, github: str | None = None,
                         clear: str | None = None):
        member_id = str(interaction.user.id)
        if not await feed_registry.feed_by_author(str(interaction.guild_id), member_id):
            await interaction.response.send_message(
                f"Your links show under your blog's posts in <#{FEED_COMMUNITY_CHANNEL_ID}>, so you need a blog "
                "on the feed first. Suggest it with `/blog suggest`; you can add your links there too.",
                ephemeral=True)
            return
        try:
            links = profile_links.read_all(linkedin=linkedin, x=x, youtube=youtube, github=github)
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
        await feed_notices.audit(self.bot, interaction, "links", f"changed their profile links: {changed}")

    async def approve_suggestion(self, interaction: discord.Interaction, suggestion_id: int):
        await interaction.response.defer()
        if not await db.decide_suggestion(suggestion_id, "approved", str(interaction.user.id)):
            await interaction.followup.send("Another mod already decided on this one.", ephemeral=True)
            return
        suggestion = await db.get_suggestion(suggestion_id)
        author_id = suggestion["author_id"]
        try:
            feed_id, title, items, channel = await feed_registry.create_feed(
                interaction.guild, suggestion["url"], feed_registry.DEFAULT_CHANNEL_ID, author_id,
                str(interaction.user.id))
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
            await feed_posting.post_items(channel, row, feed_name(row, title), newest)
        await self._close_card(interaction.message, discord.Color.green(),
                               f"✅ Approved by {interaction.user.mention}: feed #{feed_id} → {channel.mention}{credit}")
        blog = discord.utils.escape_markdown(title or suggestion["url"])
        suggested_by = suggestion["suggested_by"]
        shout_out = {None: ".", suggested_by: ", with a shout-out to you."}.get(author_id, f", crediting <@{author_id}>.")
        await feed_notices.dm(self.bot, suggested_by,
                              f"🎉 Your suggestion **{blog}** was approved! New posts will appear in {channel.mention}"
                              + shout_out)
        if author_id and author_id != suggested_by:  # someone suggested their blog: tell them too
            await feed_notices.dm(self.bot, author_id,
                                  f"🎉 Your blog **{blog}** is now on the community feed, suggested by "
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
        await feed_notices.dm(self.bot, suggestion["suggested_by"],
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


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(SuggestionButton)  # cards' buttons keep working across restarts
    await bot.add_cog(BlogCommands(bot))
