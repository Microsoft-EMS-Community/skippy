# ============================================================
# followup_events.py - Follow-ups as members act: a helper's 🔴 reaction
# flags a message, flagged members who post elsewhere get nudged, replies
# and departures are reported to helpers, and the helper log's buttons
# (Unmark, Re-flag) act on them.
# ============================================================

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

import db
import followup_notify
import views as v
from config import AWAITING_ROLE_ID, FLAG_EMOJI, HELPER_LOG_CHANNEL_ID, HELPER_ROLE_IDS
from member_cards import member_card
from mentions import mentions_skippy

logger = logging.getLogger("followup-bot")

NUDGE_COOLDOWN_HOURS = 24


def _unix(ts: str) -> int:
    """Convert SQLite timestamp string to Unix int for Discord relative format."""
    try:
        return int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


class FollowUpEvents(commands.Cog):
    """Reacts to flags, posts, departures and button clicks for follow-ups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> last public nudge. Resets on restart, which is fine:
        # the database is the source of truth, this only spaces out nudges.
        self.nudged_at: dict[str, datetime] = {}

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """
        Detect when the 🔴 Awaiting Follow-up role is removed.
        Role additions are handled by on_raw_reaction_add and /flag - not here.
        This only handles manual role removal (e.g. via server settings).
        """
        before_role_ids = {r.id for r in before.roles}
        after_role_ids = {r.id for r in after.roles}
        if not (AWAITING_ROLE_ID in before_role_ids and AWAITING_ROLE_ID not in after_role_ids):
            return
        logger.info(f"✅ Awaiting role removed from {after} ({after.id})")

        # We don't know who removed it (would need Audit Log API); mark resolver as "unknown".
        # /unmark command sets a proper resolver_id.
        open_items = await db.get_open_follow_ups(str(after.id))
        if open_items:
            await db.resolve_all_open_follow_ups(
                user_id=str(after.id),
                resolver_id="unknown"  # /unmark will have already set this properly
            )
            await db.increment_resolved_count(str(after.id))
            await db.add_audit_log(
                action="role_removed",
                actor_id=None,
                target_id=str(after.id),
                detail=f"Resolved {len(open_items)} open follow-up(s) on role removal"
            )
            logger.info(f"Resolved {len(open_items)} open follow-up(s) for {after.id}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        When a helper reacts with FLAG_EMOJI (🔴) to a message:
          1. Verify the reactor has the HELPER_ROLE_ID
          2. Add the 🔴 Awaiting Follow-up role to the message author
          3. Log to DB with the exact message as context (no searching needed)
          4. Reply on the flagged message to tell the user (DM if that fails)
        """
        # Ignore DMs and bot reactions
        if payload.guild_id is None or str(payload.emoji) != FLAG_EMOJI:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        # Verify the reactor is a helper
        reactor = guild.get_member(payload.user_id)
        if reactor is None or reactor.bot:
            return

        reactor_role_ids = {r.id for r in reactor.roles}
        if HELPER_ROLE_IDS and not any(rid in reactor_role_ids for rid in HELPER_ROLE_IDS):
            return  # Not a helper - ignore

        # Fetch the message to get author + content
        # Try regular channel first, then thread (covers forum posts and thread channels)
        channel = guild.get_channel(payload.channel_id) or guild.get_thread(payload.channel_id)
        if channel is None:
            # Last resort: fetch directly from API (handles uncached forum threads)
            try:
                channel = await guild.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                logger.warning(f"Could not resolve channel/thread {payload.channel_id} - missing access?")
                return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.HTTPException):
            logger.warning(f"Could not fetch message {payload.message_id} for reaction flag")
            return

        target = message.author
        if target.bot:
            return  # Don't flag bots

        target_member = guild.get_member(target.id)
        if target_member is None:
            return

        # Check if already flagged (avoid double-logging)
        open_items = await db.get_open_follow_ups(str(target_member.id))
        already_flagged = any(item for item in open_items)

        # Add the 🔴 role (on_member_update will also fire, but we handle logging here
        # and skip it there when we detect the flag was set via reaction)
        awaiting_role = guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role is None:
            logger.error(f"AWAITING_ROLE_ID ({AWAITING_ROLE_ID}) not found in guild.")
            return

        if awaiting_role not in target_member.roles:
            try:
                await target_member.add_roles(awaiting_role, reason=f"Flagged by {reactor} via reaction")
            except discord.Forbidden:
                logger.error("Missing permission to assign roles.")
                return

        question_snippet = message.content[:200] if message.content else "(no text)"
        message_link = message.jump_url

        # Skip if this exact message is already an open follow-up (duplicate reaction)
        duplicate = any(
            fu.get("message_link", "").endswith(f"/{payload.message_id}")
            for fu in open_items
        )
        if duplicate:
            logger.info(f"Duplicate reaction on already-tracked message {payload.message_id} - skipping")
            return

        # Always log to DB - every new flagged message gets its own record
        follow_up_id = await db.add_follow_up(
            user_id=str(target_member.id),
            guild_id=str(guild.id),
            channel_id=str(channel.id),
            message_link=message_link,
            question_snippet=question_snippet
        )
        await db.increment_flag_count(str(target_member.id), str(guild.id))
        await db.add_audit_log(
            action="flagged_via_reaction",
            actor_id=str(reactor.id),
            target_id=str(target_member.id),
            detail=f"Follow-up #{follow_up_id} | msg: {message_link}"
        )

        logger.info(
            f"🔴 {target_member} flagged by {reactor} via reaction on msg {payload.message_id}"
        )

        helper_log = guild.get_channel(HELPER_LOG_CHANNEL_ID)

        if not already_flagged:
            # First flag - tell the user, right on the flagged message
            await followup_notify.notify_member(
                guild, target_member, message_link,
                reply_text=(
                    f"Hey {target_member.mention}, a helper has flagged this message for follow-up. "
                    f"Please **reply to this message** to close it; a helper will remove the flag "
                    f"once they see your response."
                ),
                dm_text=(
                    f"Hey! A helper in {guild.name} has flagged one of your messages for follow-up:\n"
                    f"🔗 {message_link}\nPlease **reply directly to that message** to close it."
                ),
                message=message,
            )
            if helper_log:
                embed = await member_card(
                    title="🔴 Flagged",
                    color=discord.Color.red(),
                    user=target_member,
                    description=f"Flagged by {reactor.mention} in {channel.mention}",
                    link=message_link,
                )
                await helper_log.send(embed=embed, view=v.FlaggedView(str(target_member.id)))
        else:
            # Already flagged - add to the pile silently, notify helpers only
            open_count = len(open_items) + 1
            if helper_log:
                embed = await member_card(
                    title="🔴 Follow-up Added",
                    color=discord.Color.orange(),
                    user=target_member,
                    description=f"Flagged again by {reactor.mention} in {channel.mention} - now **{open_count}** open. User not re-pinged.",
                    link=message_link,
                )
                await helper_log.send(embed=embed, view=v.FlaggedView(str(target_member.id)))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        When a user with 🔴 Awaiting Follow-up posts a new message: a reply
        to their flagged message is reported to helpers; anything else gets
        them a public nudge, at most once a day (helpers hear about the rest).
        """
        if message.author.bot or message.guild is None or mentions_skippy(self.bot, message):
            return
        member = message.guild.get_member(message.author.id)
        if member is None or not any(r.id == AWAITING_ROLE_ID for r in member.roles):
            return

        # Fetch their open follow-ups
        open_items = await db.get_open_follow_ups(str(member.id))
        if not open_items:
            return

        # ── Reply detection ──
        # If the user replied directly to one of their flagged messages, notify helpers.
        # Extract message ID from stored link (format: .../channels/guild/channel/MESSAGE_ID)
        if message.reference and message.reference.message_id:
            ref_id = str(message.reference.message_id)
            matched = [
                fu for fu in open_items
                if fu.get("message_link", "").endswith(f"/{ref_id}")
            ]
            if matched:
                helper_log = message.guild.get_channel(HELPER_LOG_CHANNEL_ID)
                if helper_log:
                    fu = matched[0]
                    embed = await member_card(
                        title="💬 Replied to Flagged Message",
                        color=discord.Color.green(),
                        user=member,
                        description=f"Replied in {message.channel.mention}.",
                        extra_fields=[
                            ("Original", f"[Jump]({fu['message_link']})" if fu['message_link'] else "N/A", True),
                            ("Reply", f"[Jump]({message.jump_url})", True),
                        ],
                    )
                    await helper_log.send(embed=embed, view=v.RepliedView(str(member.id)))
                return  # Don't nudge them for replying - that's the right behaviour

        count = len(open_items)
        link_list = "\n".join(
            f"  • {fu['message_link'] or 'N/A'} (<t:{_unix(fu['flagged_at'])}:R>)"
            for fu in open_items
        )

        # Check nudge cooldown
        now = datetime.now(timezone.utc)
        last_nudge = self.nudged_at.get(str(member.id))
        on_cooldown = (
            last_nudge is not None and
            (now - last_nudge).total_seconds() < NUDGE_COOLDOWN_HOURS * 3600
        )

        helper_log = message.guild.get_channel(HELPER_LOG_CHANNEL_ID)

        if on_cooldown:
            # Cooldown active - silent info to helper log only, no public ping
            seconds_remaining = (NUDGE_COOLDOWN_HOURS * 3600) - (now - last_nudge).total_seconds()
            hours_left = int(seconds_remaining // 3600)
            mins_left = int((seconds_remaining % 3600) // 60)
            if helper_log:
                embed = await member_card(
                    title="🔕 Nudge Suppressed (cooldown)",
                    color=discord.Color.greyple(),
                    user=member,
                    description=f"Posted in {message.channel.mention} - not nudged. Next nudge in **{hours_left}h {mins_left}m**.",
                )
                await helper_log.send(embed=embed, view=v.FlaggedView(str(member.id)))
        else:
            # Ping publicly in the channel where they posted - visible to everyone
            await message.channel.send(
                f"⚠️ {member.mention} - you have **{count}** open follow-up(s).\n"
                f"You have messages waiting for a response - please address them first:\n{link_list}"
            )

            # Audit to helper log as embed
            if helper_log:
                embed = await member_card(
                    title="📋 Nudged Publicly",
                    color=discord.Color.from_rgb(243, 156, 18),
                    user=member,
                    description=f"Posted in {message.channel.mention} - nudged publicly with {count} open follow-up(s).",
                )
                await helper_log.send(embed=embed, view=v.FlaggedView(str(member.id)))

            # Record nudge time
            self.nudged_at[str(member.id)] = now

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """
        When a member leaves (or is kicked/banned), check for open follow-ups.
        Mark them as 'left_server' and alert the helper log.
        """
        open_items = await db.get_open_follow_ups(str(member.id))
        if not open_items:
            return

        count = len(open_items)
        await db.mark_left_server(str(member.id))
        await db.add_audit_log(
            action="left_server",
            actor_id=None,
            target_id=str(member.id),
            detail=f"Left with {count} open follow-up(s)"
        )

        helper_log = member.guild.get_channel(HELPER_LOG_CHANNEL_ID)
        if helper_log:
            links = "\n".join(fu['message_link'] for fu in open_items if fu.get('message_link'))
            embed = await member_card(
                title="🚪 Left Server with Open Follow-ups",
                color=discord.Color.dark_red(),
                user=member,
                description=f"Left with **{count}** unresolved follow-up(s).\n{links}",
            )
            await helper_log.send(embed=embed)

        logger.info(f"🚪 {member} left with {count} open follow-up(s) - marked as left_server")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """A member who left with unresolved follow-ups is back: tell helpers."""
        left_items = await db.get_left_server_follow_ups(str(member.id))
        if not left_items:
            return

        count = len(left_items)
        await db.add_audit_log(
            action="rejoined",
            actor_id=None,
            target_id=str(member.id),
            detail=f"Rejoined with {count} unresolved follow-up(s) from before leaving"
        )

        helper_log = member.guild.get_channel(HELPER_LOG_CHANNEL_ID)
        if helper_log:
            links = "\n".join(fu['message_link'] for fu in left_items if fu.get('message_link'))
            embed = await member_card(
                title="👀 Rejoined - Prior Follow-ups",
                color=discord.Color.blue(),
                user=member,
                description=f"Back with **{count}** unresolved follow-up(s) from before leaving. Use `/flag @{member.name}` to re-flag if needed.\n{links}",
            )
            await helper_log.send(embed=embed)

        logger.info(f"👀 {member} rejoined with {count} prior unresolved follow-up(s)")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """The helper log's Unmark and Re-flag buttons."""
        clicked = v.button_action(interaction)
        if clicked is None or clicked[0] not in ("unmark", "reflag"):
            return
        action, user_id = clicked

        # Verify the clicker is a helper
        clicker_role_ids = {r.id for r in interaction.user.roles}
        if HELPER_ROLE_IDS and not any(rid in clicker_role_ids for rid in HELPER_ROLE_IDS):
            await interaction.response.send_message(
                "You need a helper role to use this.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        member = interaction.guild.get_member(int(user_id))
        if member is None:
            await interaction.followup.send("User not found in server.", ephemeral=True)
            return
        if action == "unmark":
            await self._unmark(interaction, member, user_id)
        else:
            await self._reflag(interaction, member, user_id)

    async def _unmark(self, interaction: discord.Interaction, member: discord.Member, user_id: str):
        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role and awaiting_role in member.roles:
            await member.remove_roles(awaiting_role, reason=f"Unmarked via button by {interaction.user}")

        await db.resolve_all_open_follow_ups(str(user_id), resolver_id=str(interaction.user.id))
        await db.increment_resolved_count(str(user_id))
        await db.add_audit_log(
            action="unmark_button",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=f"Resolved via button click in helper log"
        )

        # Update the original message to show it's been resolved
        try:
            await interaction.message.edit(
                content=f"~~{interaction.message.content or ''}~~ **Resolved by {interaction.user.mention}**",
                view=None  # Remove buttons
            )
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ {member.mention} unmarked and all follow-ups resolved.", ephemeral=True
        )
        logger.info(f"✅ {member} unmarked via button by {interaction.user}")

    async def _reflag(self, interaction: discord.Interaction, member: discord.Member, user_id: str):
        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role and awaiting_role not in member.roles:
            await member.add_roles(awaiting_role, reason=f"Re-flagged via button by {interaction.user}")

        await db.add_audit_log(
            action="reflag_button",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=f"Re-flagged via button click - reply deemed insufficient"
        )

        try:
            await interaction.message.edit(view=None)  # Remove buttons after action
        except Exception:
            pass

        await interaction.followup.send(
            f"🔴 {member.mention} re-flagged. Use 🔴 reaction on a specific message to add context.",
            ephemeral=True
        )
        logger.info(f"🔴 {member} re-flagged via button by {interaction.user}")


async def setup(bot: commands.Bot):
    await bot.add_cog(FollowUpEvents(bot))
