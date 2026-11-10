# ============================================================
# polls.py - /poll, for moderators: anonymous polls. Members vote with
# buttons; the card shows only how many have voted until the poll closes,
# then the results. A poll can be for one role: only its members vote, and
# the result says how many of them didn't (never who).
# ============================================================

import logging
import secrets
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import feed_notices
import poll_cards
from checks import is_rss_admin
from config import RSS_ADMIN_ROLE_ID
from db.connection import utc_now

logger = logging.getLogger("followup-bot")

MAX_QUESTION, MAX_OPTION = 250, 80  # what fits a card's title and a button
DURATIONS = {"1 hour": 1, "1 day": 24, "3 days": 72, "1 week": 168, "2 weeks": 336}


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll:(?P<id>[0-9]+):(?P<choice>[0-9]+)"):
    """One choice of a poll. The poll and choice live in the button's
    custom_id, so the buttons keep working after Skippy restarts."""

    def __init__(self, poll_id: int, choice: int, label: str = "Vote"):
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary,
                                           custom_id=f"poll:{poll_id}:{choice}"))
        self.poll_id, self.choice = poll_id, choice

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]), int(match["choice"]))

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Polls").vote(interaction, self.poll_id, self.choice)


class AboutButton(discord.ui.DynamicItem[discord.ui.Button], template=r"poll-about"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="How is this anonymous?", style=discord.ButtonStyle.secondary,
                                           emoji="🔒", custom_id="poll-about", row=1))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(poll_cards.ABOUT, ephemeral=True)


def poll_view(poll_id: int, options: list[str]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for choice, option in enumerate(options):
        view.add_item(VoteButton(poll_id, choice, option))
    view.add_item(AboutButton())
    return view


def role_voters(guild, poll: dict) -> list | None:
    """The members a role's poll is waiting for, or None when it's for everyone."""
    role = guild.get_role(int(poll["role_id"])) if poll["role_id"] else None
    return [m for m in role.members if not m.bot] if role else None


def count_absent(poll: dict, members: list, voters: set[str]) -> int:
    return sum(poll_cards.voter_key(poll["secret"], m.id) not in voters for m in members)


class Polls(commands.Cog):
    """/poll and the loop that closes polls on time."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.close_due.start()

    async def cog_unload(self):
        self.close_due.cancel()

    # Hidden from members without Manage Roles (which the Moderator role
    # has). That's only visibility; is_rss_admin is the real check.
    poll = app_commands.Group(name="poll", description="Anonymous polls",
                              default_permissions=discord.Permissions(manage_roles=True), guild_only=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        await feed_notices.command_failed(interaction, error, f"Only <@&{RSS_ADMIN_ROLE_ID}>s can run polls.")

    @poll.command(name="create", description="Post an anonymous poll in this channel.")
    @app_commands.describe(
        question="What you're asking",
        option1="First choice", option2="Second choice", option3="Third choice (optional)",
        option4="Fourth choice (optional)", option5="Fifth choice (optional)",
        duration="How long people can vote (default: 3 days)",
        role="Only this role votes; it gets pinged, and the result says how many of it didn't vote")
    @app_commands.choices(duration=[app_commands.Choice(name=name, value=hours)
                                    for name, hours in DURATIONS.items()])
    @is_rss_admin()
    async def create(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, MAX_QUESTION],
                     option1: app_commands.Range[str, 1, MAX_OPTION],
                     option2: app_commands.Range[str, 1, MAX_OPTION],
                     option3: app_commands.Range[str, 1, MAX_OPTION] | None = None,
                     option4: app_commands.Range[str, 1, MAX_OPTION] | None = None,
                     option5: app_commands.Range[str, 1, MAX_OPTION] | None = None,
                     duration: int = DURATIONS["3 days"], role: discord.Role | None = None):
        options = [o.strip() for o in (option1, option2, option3, option4, option5) if o and o.strip()]
        if len(set(options)) != len(options) or len(options) < 2:
            await interaction.response.send_message("A poll needs at least 2 different choices.", ephemeral=True)
            return
        if role and role.is_default():
            await interaction.response.send_message("Leave `role` empty for a poll everyone can vote in.",
                                                    ephemeral=True)
            return

        closes_at = (utc_now() + timedelta(hours=duration)).isoformat()
        poll_id = await db.add_poll(str(interaction.guild.id), str(interaction.channel.id), question.strip(),
                                    options, str(role.id) if role else None, str(interaction.user.id),
                                    closes_at, secrets.token_hex(32))
        poll = await db.get_poll(poll_id)
        try:
            message = await interaction.channel.send(
                role.mention if role else None, embed=await self.open_card(interaction.guild, poll),
                view=poll_view(poll_id, options),
                allowed_mentions=discord.AllowedMentions(roles=[role]) if role else discord.AllowedMentions.none())
        except discord.HTTPException:
            await db.delete_poll(poll_id)
            await interaction.response.send_message("I can't post in this channel.", ephemeral=True)
            return
        await db.set_poll_message(poll_id, str(message.id))
        await db.add_audit_log(action="poll_created", actor_id=str(interaction.user.id), target_id=None,
                               detail=f"poll #{poll_id}: {question.strip()}")
        logger.info("%s created poll #%s", interaction.user, poll_id)
        await interaction.response.send_message(
            f"Poll #{poll_id} is posted. It closes <t:{poll_cards.closes_timestamp(poll)}:R>, or right away "
            f"with `/poll close`.", ephemeral=True)

    @poll.command(name="close", description="Close a poll now and show its results.")
    @app_commands.describe(poll="The poll to close")
    @is_rss_admin()
    async def close(self, interaction: discord.Interaction, poll: int):
        found = await db.get_poll(poll)
        if not found or found["guild_id"] != str(interaction.guild.id) or found["closed_at"]:
            await interaction.response.send_message("That poll isn't open.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.close_poll(found)
        await db.add_audit_log(action="poll_closed", actor_id=str(interaction.user.id), target_id=None,
                               detail=f"poll #{poll}: {found['question']}")
        await interaction.followup.send(f"Poll #{poll} is closed; the results are on its card.", ephemeral=True)

    @close.autocomplete("poll")
    async def _open_polls(self, interaction: discord.Interaction, current: str):
        polls = await db.open_polls(guild_id=str(interaction.guild.id))
        return [app_commands.Choice(name=f"#{p['id']} {p['question']}"[:100], value=p["id"])
                for p in polls if current.lower() in p["question"].lower()][:25]

    async def open_card(self, guild, poll: dict) -> discord.Embed:
        voters = await db.poll_voters(poll["id"])
        members = role_voters(guild, poll)
        if members is None:
            return poll_cards.open_card(poll, len(voters))
        return poll_cards.open_card(poll, len(voters), eligible=len(members),
                                    voted=len(members) - count_absent(poll, members, voters))

    async def vote(self, interaction: discord.Interaction, poll_id: int, choice: int):
        """A member clicked a choice. Who voted is never logged or shown."""
        poll = await db.get_poll(poll_id)
        if not poll or poll["closed_at"] or choice >= len(poll["options"]):
            await interaction.response.send_message("This poll has closed.", ephemeral=True)
            return
        if poll["role_id"] and not any(str(r.id) == poll["role_id"] for r in interaction.user.roles):
            await interaction.response.send_message(f"This poll is for <@&{poll['role_id']}> only.",
                                                    ephemeral=True)
            return
        if not await db.cast_poll_vote(poll_id, poll_cards.voter_key(poll["secret"], interaction.user.id), choice):
            await interaction.response.send_message("This poll has closed.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Your vote is in: **{poll['options'][choice]}**. You can change it until the poll closes.",
            ephemeral=True)
        try:
            await interaction.message.edit(embed=await self.open_card(interaction.guild, poll))
        except discord.HTTPException as e:
            logger.info("Couldn't update poll #%s's card: %s", poll_id, e)

    async def close_poll(self, poll: dict) -> bool:
        """Close a poll and turn its card into the results. False if it was already closed."""
        guild = self.bot.get_guild(int(poll["guild_id"]))
        members = role_voters(guild, poll) if guild else None
        absent = None if members is None else count_absent(poll, members, await db.poll_voters(poll["id"]))
        results = await db.close_poll(poll["id"], absent)
        if results is None:
            return False
        logger.info("Poll #%s closed with %s votes", poll["id"], sum(results))
        channel = self.bot.get_channel(int(poll["channel_id"]))
        try:
            message = await channel.fetch_message(int(poll["message_id"]))
            await message.edit(embed=poll_cards.closed_card(poll, results, absent), view=None)
        except (AttributeError, TypeError, discord.HTTPException) as e:
            logger.info("Couldn't show poll #%s's results on its card: %s", poll["id"], e)
        return True

    @tasks.loop(minutes=1)
    async def close_due(self):
        try:
            for poll in await db.open_polls(due_by=utc_now().isoformat()):
                await self.close_poll(poll)
        except Exception:
            logger.exception("Closing due polls failed")

    @close_due.before_loop
    async def before_close_due(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    bot.add_dynamic_items(VoteButton, AboutButton)  # polls' buttons keep working across restarts
    await bot.add_cog(Polls(bot))
