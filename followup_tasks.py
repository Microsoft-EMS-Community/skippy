# ============================================================
# followup_tasks.py - Follow-ups over time (runs on a 24h loop)
# Warning progression: 48h → warning ping, 7d → mark ignored
# Plus the monthly cleanup of old follow-ups and audit log entries.
# ============================================================

import logging
from discord.ext import commands, tasks

import db
import followup_notify
from config import HELPER_LOG_CHANNEL_ID, GUILD_ID

logger = logging.getLogger("followup-bot")


class FollowUpTasks(commands.Cog):
    """Periodic background tasks for follow-up monitoring."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.warning_check.start()
        self.monthly_cleanup.start()

    def cog_unload(self):
        self.warning_check.cancel()
        self.monthly_cleanup.cancel()

    # ──────────────────────────────────────────
    # 24-hour warning check loop
    # ──────────────────────────────────────────
    @tasks.loop(hours=24)
    async def warning_check(self):
        """
        Every 24 hours:
        - Follow-ups > 48h old → send a warning ping (once)
        - Follow-ups > 7 days old → mark as 'ignored'
        """
        HOURS_48 = 48
        HOURS_7D = 7 * 24

        # --- 7-day ignored check (must run first to avoid double-processing) ---
        overdue_7d = await db.get_overdue_follow_ups(HOURS_7D)
        for fu in overdue_7d:
            # Only process if still 'open' (not already ignored)
            if fu["status"] != "open":
                continue

            await db.mark_ignored(fu["id"])
            await db.increment_ignored_count(fu["user_id"])
            await db.add_audit_log(
                action="auto_ignored",
                actor_id=None,
                target_id=fu["user_id"],
                detail=f"Follow-up #{fu['id']} auto-marked ignored after 7 days"
            )

            # Try to log in the helper channel
            guild = self.bot.get_guild(int(fu["guild_id"]))
            if guild:
                log_channel = guild.get_channel(HELPER_LOG_CHANNEL_ID)
                if log_channel:
                    await log_channel.send(
                        f"🚫 <@{fu['user_id']}> has had an open follow-up for **7+ days** "
                        f"with no response. Marked as **ignored**.\n"
                        f"Original message: {fu['message_link'] or 'N/A'}"
                    )

        # --- 48-hour warning (send once per follow-up) ---
        overdue_48h = await db.get_overdue_follow_ups(HOURS_48)
        for fu in overdue_48h:
            # Skip if already ignored, resolved, or warning already sent
            if fu["status"] != "open" or fu["warning_sent"]:
                continue

            await db.mark_warning_sent(fu["id"])
            await db.add_audit_log(
                action="warning_sent",
                actor_id=None,
                target_id=fu["user_id"],
                detail=f"48h warning sent for follow-up #{fu['id']}"
            )

            guild = self.bot.get_guild(int(fu["guild_id"]))
            member = guild and guild.get_member(int(fu["user_id"]))
            if member:  # not if they've left the server
                await followup_notify.notify_member(
                    guild, member, fu["message_link"],
                    reply_text=(
                        f"⏰ **Reminder** - {member.mention}, this follow-up has been open for "
                        f"**48+ hours**. Please reply here, or ask a helper to close it out."
                    ),
                    dm_text=(
                        f"⏰ Reminder: you've had an open follow-up in {guild.name} for **48+ hours**."
                        + (f"\n🔗 {fu['message_link']}" if fu["message_link"] else "")
                        + "\nPlease respond to it, or ask a helper to close it out."
                    ),
                )

    @warning_check.before_loop
    async def before_warning_check(self):
        await self.bot.wait_until_ready()

    # ──────────────────────────────────────────
    # Monthly cleanup loop (runs every 30 days)
    # ──────────────────────────────────────────
    @tasks.loop(hours=24 * 30)
    async def monthly_cleanup(self):
        """
        Every 30 days:
        - Delete audit_log entries older than 90 days
        - Delete resolved/ignored follow_ups older than 180 days
        - Log results to helper log channel
        """
        result = await db.cleanup_old_records(audit_log_days=90, follow_up_days=180)
        logger.info(
            f"Monthly cleanup: removed {result['audit_log']} audit log entries, "
            f"{result['follow_ups']} old follow-up records."
        )

        # Post summary to helper log
        guild = self.bot.get_guild(GUILD_ID)
        if guild:
            log_channel = guild.get_channel(HELPER_LOG_CHANNEL_ID)
            if log_channel and (result["audit_log"] > 0 or result["follow_ups"] > 0):
                await log_channel.send(
                    f"🧹 **Monthly cleanup complete:**\n"
                    f"- Audit log entries removed (>90 days): **{result['audit_log']}**\n"
                    f"- Old resolved/ignored follow-ups removed (>180 days): **{result['follow_ups']}**\n"
                    f"- User scores: untouched"
                )

    @monthly_cleanup.before_loop
    async def before_monthly_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    """Called by bot.py to load this cog."""
    await bot.add_cog(FollowUpTasks(bot))
