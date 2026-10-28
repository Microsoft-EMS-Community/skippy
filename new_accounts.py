# ============================================================
# new_accounts.py - Spotting new or throwaway accounts: tags them on join,
# asks them to verify with a button (with a decoy for scripts), alerts mods
# to bursts of joins, and keeps one audit card per account's lifecycle.
# ============================================================

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import db
import views as v
from config import GUILD_ID, NEW_ACCOUNT_AGE_DAYS, NEW_ACCOUNT_ROLE_ID, RSS_AUDIT_CHANNEL_ID
from mentions import mentions_skippy

logger = logging.getLogger("followup-bot")

JOIN_BURST_WINDOW_MINUTES = 10
JOIN_BURST_THRESHOLD = 3  # N+ joins within the window trips the alert
NUDGE_COOLDOWN_HOURS = 24
# A real person needs to read the message and move a mouse; sub-second
# clicks smell like a script.
VERIFY_CLICK_SUSPICIOUS_SECONDS = 1.5


class NewAccounts(commands.Cog):
    """New-account tagging, verification and join-burst alerts.

    All state is in memory and resets on restart, which is fine: it's for
    live detection, and the worst case is an extra nudge or a fresh audit card."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent_joins: list[datetime] = []  # rolling join times, for burst detection
        self.nudged_at: dict[str, datetime] = {}  # user_id -> last verification nudge
        # The public "check your DMs" notice per user, updated once they verify.
        self.verification_notices: dict[str, discord.Message] = {}
        self.prompt_sent_at: dict[str, datetime] = {}  # user_id -> when the verify prompt went out
        # One evolving audit card per user through the lifecycle (tagged -> verification
        # requested -> verified), instead of 3 separate embeds piling up in the audit channel.
        self.audit_cards: dict[str, dict] = {}  # user_id -> {"message": discord.Message, "history": [str, ...]}

    async def _lifecycle_card(self, guild: discord.Guild, member: discord.Member, stage_desc: str,
                              color: discord.Color, final: bool = False):
        """
        Post/refresh the single audit-channel card tracking a user's new-account lifecycle.
        Deletes the previous card and reposts a consolidated one (bumps to bottom of channel = still
        reads as "new"), collapsing tag/request/verify into one card instead of three.
        """
        audit_channel = guild.get_channel(RSS_AUDIT_CHANNEL_ID)
        if not audit_channel:
            return

        key = str(member.id)
        entry = self.audit_cards.setdefault(key, {"message": None, "history": []})
        entry["history"].append(f"<t:{int(datetime.now(timezone.utc).timestamp())}:R> — {stage_desc}")

        embed = discord.Embed(
            title=f"\U0001f195 Account Lifecycle: {member}",
            description="\n".join(entry["history"]),
            color=color,
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)

        old_message = entry.get("message")
        if old_message is not None:
            try:
                await old_message.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        entry["message"] = await audit_channel.send(embed=embed)

        if final:
            # Lifecycle complete - stop tracking so a future re-flag starts a clean new card.
            self.audit_cards.pop(key, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Alert on join bursts, and tag likely new/throwaway accounts (age, default avatar)."""
        now = datetime.now(timezone.utc)
        self.recent_joins.append(now)
        cutoff = now - timedelta(minutes=JOIN_BURST_WINDOW_MINUTES)
        while self.recent_joins and self.recent_joins[0] < cutoff:
            self.recent_joins.pop(0)

        if len(self.recent_joins) >= JOIN_BURST_THRESHOLD:
            await db.add_audit_log(
                action="join_burst_detected",
                actor_id=None,
                target_id=str(member.id),
                detail=f"{len(self.recent_joins)} joins within {JOIN_BURST_WINDOW_MINUTES}m (latest: {member})"
            )
            audit_channel = member.guild.get_channel(RSS_AUDIT_CHANNEL_ID)
            if audit_channel:
                embed = discord.Embed(
                    title="🚨 Join Burst Detected",
                    description=(
                        f"**{len(self.recent_joins)}** members joined within the last "
                        f"{JOIN_BURST_WINDOW_MINUTES} minutes.\nLatest: {member.mention}"
                    ),
                    color=discord.Color.red(),
                )
                await audit_channel.send(embed=embed)
            logger.warning(f"🚨 Join burst: {len(self.recent_joins)} joins in {JOIN_BURST_WINDOW_MINUTES}m")

        account_age = now - member.created_at
        is_young = account_age.days < NEW_ACCOUNT_AGE_DAYS
        is_default_avatar = member.avatar is None
        if not (is_young or is_default_avatar):
            return

        reasons = []
        if is_young:
            reasons.append(f"account age {account_age.days}d (< {NEW_ACCOUNT_AGE_DAYS}d)")
        if is_default_avatar:
            reasons.append("default avatar")

        new_account_role = member.guild.get_role(NEW_ACCOUNT_ROLE_ID)
        if new_account_role:
            try:
                await member.add_roles(new_account_role, reason=f"Heuristic tag: {', '.join(reasons)}")
            except discord.Forbidden:
                logger.error("Missing permission to assign New Account role.")

            if "[unverified]" not in member.display_name:
                try:
                    await member.edit(
                        nick=f"{member.display_name} [unverified]"[:32],
                        reason="Tagged as new/unverified account",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.error("Missing permission to set nickname for new account tag.")

        await db.add_audit_log(
            action="new_account_tagged",
            actor_id=None,
            target_id=str(member.id),
            detail=", ".join(reasons)
        )

        await self._lifecycle_card(
            member.guild, member,
            stage_desc=f"🆕 **Tagged** on join — {', '.join(reasons)} (created <t:{int(member.created_at.timestamp())}:R>)",
            color=discord.Color.gold(),
        )

        logger.info(f"🆕 {member} tagged as new account: {', '.join(reasons)}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Tagged accounts that post get asked to verify, at most once a day."""
        if message.author.bot or message.guild is None or mentions_skippy(self.bot, message):
            return
        member = message.guild.get_member(message.author.id)
        if member is None or not any(r.id == NEW_ACCOUNT_ROLE_ID for r in member.roles):
            return
        now = datetime.now(timezone.utc)
        last_nudge = self.nudged_at.get(str(member.id))
        if last_nudge is not None and (now - last_nudge).total_seconds() < NUDGE_COOLDOWN_HOURS * 3600:
            return

        dm_sent = False
        try:
            await member.send(
                f"👋 You're flagged as a possibly new/unverified account. "
                f"Click below to confirm you're human.",
                view=v.VerifyHumanView(str(member.id)),
            )
            dm_sent = True
        except (discord.Forbidden, discord.HTTPException):
            dm_sent = False

        self.prompt_sent_at[str(member.id)] = datetime.now(timezone.utc)

        if dm_sent:
            notice = await message.channel.send(
                f"🆕 {member.mention} has been asked to verify - check your DMs."
            )
            self.verification_notices[str(member.id)] = notice
        else:
            await message.reply(
                f"👋 {member.mention}, you're flagged as a possibly new/unverified account. "
                f"Click below to confirm you're human.",
                view=v.VerifyHumanView(str(member.id)),
            )

        await db.add_audit_log(
            action="verification_requested",
            actor_id=None,
            target_id=str(member.id),
            detail=f"Asked to verify via {'DM' if dm_sent else 'public channel'} in {message.channel.name}"
        )

        await self._lifecycle_card(
            message.guild, member,
            stage_desc=f"📨 **Verification requested** via {'DM' if dm_sent else 'public channel'} in {message.channel.mention}",
            color=discord.Color.gold(),
        )

        self.nudged_at[str(member.id)] = now

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """The verify prompt's buttons: "I'm Human", and the decoy."""
        clicked = v.button_action(interaction)
        if clicked is None or clicked[0] not in ("verify_human", "verify_honeypot"):
            return
        action, user_id = clicked
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message(
                "This verification isn't for you.", ephemeral=True
            )
            return
        if action == "verify_honeypot":
            await self._decoy_clicked(interaction, user_id)
        else:
            await self._verify(interaction, user_id)

    async def _decoy_clicked(self, interaction: discord.Interaction, user_id: str):
        # No real reason for a human reading the prompt to click this - only a script
        # blindly clicking buttons would. Don't tip it off: respond exactly like the
        # real button would, but flag quietly instead of verifying anyone.
        await interaction.response.send_message("✅ You're verified. Welcome!", ephemeral=True)

        await db.add_audit_log(
            action="verify_honeypot_triggered",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail="Clicked decoy button on verify prompt - likely automated"
        )

        guild = interaction.guild or self.bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        logger.warning(f"⚠️ {member or user_id} clicked the verify honeypot button - likely a bot")

        # Same cosmetic "verified" strikethrough as the real button, so the clicker
        # sees nothing different - the honeypot is only visible in the audit card.
        await self._mark_verified(interaction, user_id)

        if guild and member:
            await self._lifecycle_card(
                guild, member,
                stage_desc="🍯 **Honeypot tripped** - clicked decoy button, likely a bot. **Not** auto-verified, review manually.",
                color=discord.Color.red(),
                final=False,
            )

    async def _verify(self, interaction: discord.Interaction, user_id: str):
        guild = interaction.guild or self.bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        if member is None:
            await interaction.response.send_message("Couldn't find you in the server.", ephemeral=True)
            return

        prompt_sent_at = self.prompt_sent_at.pop(user_id, None)
        click_seconds = (
            (datetime.now(timezone.utc) - prompt_sent_at).total_seconds()
            if prompt_sent_at else None
        )
        suspiciously_fast = click_seconds is not None and click_seconds < VERIFY_CLICK_SUSPICIOUS_SECONDS

        # A sub-1.5s click can't be an accident like a stray honeypot press - nobody reads
        # and decides that fast. Treat it at least as seriously: don't auto-clear the role,
        # route to manual review instead.
        if not suspiciously_fast:
            new_account_role = guild.get_role(NEW_ACCOUNT_ROLE_ID)
            if new_account_role and new_account_role in member.roles:
                await member.remove_roles(new_account_role, reason="Self-verified as human via button")

            if "[unverified]" in member.display_name:
                try:
                    await member.edit(
                        nick=member.display_name.replace(" [unverified]", "").replace("[unverified]", "").strip() or None,
                        reason="Self-verified as human via button",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.error("Missing permission to clear nickname tag after verification.")

        detail = "Verified via 'I'm Human' button"
        if click_seconds is not None:
            detail += f" ({click_seconds:.2f}s after prompt)"
        if suspiciously_fast:
            detail += " ⚠️ suspiciously fast click - not auto-verified, held for manual review"

        await db.add_audit_log(
            action="self_verified_human" if not suspiciously_fast else "verify_click_too_fast",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=detail
        )

        if suspiciously_fast:
            logger.warning(
                f"⚠️ {member} clicked verify in {click_seconds:.2f}s - held for manual review, possible bot"
            )

        # Same cosmetic "verified" response either way - the real signal only shows in the audit card.
        await interaction.response.send_message("✅ You're verified. Welcome!", ephemeral=True)
        await self._mark_verified(interaction, user_id)

        if suspiciously_fast:
            stage_desc = (
                f"⚠️ **Verify click too fast** ({click_seconds:.2f}s) - "
                f"likely a bot, **not** auto-verified, review manually."
            )
        else:
            stage_desc = "✅ **Verified** as human via button"

        await self._lifecycle_card(
            guild, member,
            stage_desc=stage_desc,
            color=discord.Color.red() if suspiciously_fast else discord.Color.green(),
            final=not suspiciously_fast,
        )

        logger.info(f"✅ {member} self-verified as human via button")

    async def _mark_verified(self, interaction: discord.Interaction, user_id: str):
        """Cross out the prompt (and the public "check your DMs" notice) as verified."""
        try:
            await interaction.message.edit(
                content=f"~~{interaction.message.content or ''}~~ **✅ Verified**",
                view=None
            )
        except Exception:
            pass

        notice = self.verification_notices.pop(user_id, None)
        if notice is not None:
            try:
                await notice.edit(
                    content=f"~~{notice.content or ''}~~ **✅ Verified**"
                )
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(NewAccounts(bot))
