# ============================================================
# followup_commands.py - Slash commands for follow-ups:
# /flag, /unmark (helpers), /score, /stats (everyone), /audit (helpers)
# ============================================================

import discord
from discord import app_commands
from discord.ext import commands

import db
import followup_notify
from checks import is_helper
from config import AWAITING_ROLE_ID, GUILD_ID


class FollowUpCommands(commands.Cog):
    """Slash commands for managing follow-ups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """A member without the right role gets told, instead of "the application did not respond"."""
        if not isinstance(error, app_commands.CheckFailure):
            return  # other errors are logged by the command tree, as before
        message = "That command is for helpers and moderators."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    # ──────────────────────────────────────────
    # /flag @user - helper flags a user manually
    # ──────────────────────────────────────────
    @app_commands.command(
        name="flag",
        description="Flag a user with 🔴 Awaiting Follow-up (helpers only)."
    )
    @app_commands.describe(
        user="The user to flag",
        message_link="Link to the specific message (paste Discord message link)",
        reason="Brief context (optional)"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @is_helper()
    async def flag(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        message_link: str = "",
        reason: str = ""
    ):
        """
        Helper manually flags a user. Adds 🔴 role, logs to DB, pings user.
        Use this when you can't react to a specific message (e.g. old threads).
        """
        await interaction.response.defer(ephemeral=True)

        if user.bot:
            await interaction.followup.send("Can't flag a bot.", ephemeral=True)
            return

        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role is None:
            await interaction.followup.send(
                "⚠️ AWAITING_ROLE_ID is not configured correctly in config.py.",
                ephemeral=True
            )
            return

        # Add role if not already set
        if awaiting_role in user.roles:
            await interaction.followup.send(
                f"{user.mention} is already flagged with 🔴 Awaiting Follow-up.",
                ephemeral=True
            )
            return

        try:
            await user.add_roles(awaiting_role, reason=f"Flagged by {interaction.user} via /flag")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to assign roles.", ephemeral=True
            )
            return

        # Validate message_link if provided
        clean_link = message_link.strip() if message_link else ""
        if clean_link and "discord.com/channels/" not in clean_link:
            await interaction.followup.send(
                "⚠️ That doesn't look like a valid Discord message link. "
                "Right-click a message → Copy Message Link.", ephemeral=True
            )
            return

        # Log to DB
        question_snippet = reason[:200] if reason else "(no reason provided)"
        follow_up_id = await db.add_follow_up(
            user_id=str(user.id),
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
            message_link=clean_link,
            question_snippet=question_snippet
        )
        await db.increment_flag_count(str(user.id), str(interaction.guild_id))
        await db.add_audit_log(
            action="flagged_via_command",
            actor_id=str(interaction.user.id),
            target_id=str(user.id),
            detail=f"Follow-up #{follow_up_id} | link: {clean_link or 'none'} | reason: {question_snippet}"
        )

        await interaction.followup.send(
            f"🔴 {user.mention} has been flagged.", ephemeral=True
        )

        # Tell the user: on the linked message if there is one, otherwise by DM
        reason_text = f"\n> {reason}" if reason else ""
        reached = await followup_notify.notify_member(
            interaction.guild, user, clean_link,
            reply_text=(
                f"Hey {user.mention}, a helper has flagged this message for follow-up.{reason_text}\n"
                f"Please **reply to this message** to close it; a helper will remove the flag "
                f"once they see your response."
            ),
            dm_text=(
                f"Hey! A helper in {interaction.guild.name} has flagged you for a follow-up.{reason_text}"
                + (f"\n🔗 {clean_link}" if clean_link else "")
                + "\nPlease go back and address it; a helper will remove the flag once they see your response."
            ),
        )
        if reached is None:
            await interaction.followup.send(
                f"⚠️ I couldn't reach {user.mention} (no message to reply to, and their DMs are closed). "
                "You may want to ping them yourself.", ephemeral=True)

    # ──────────────────────────────────────────
    # /unmark @user
    # ──────────────────────────────────────────
    @app_commands.command(
        name="unmark",
        description="Remove the 🔴 Awaiting Follow-up role from a user and mark resolved."
    )
    @app_commands.describe(user="The user to unmark")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @is_helper()
    async def unmark(self, interaction: discord.Interaction, user: discord.Member):
        """
        Helper removes the 🔴 role, marks all open follow-ups as resolved,
        and logs the action to the audit log.
        """
        await interaction.response.defer(ephemeral=True)

        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role is None:
            await interaction.followup.send(
                "⚠️ AWAITING_ROLE_ID is not configured correctly in config.py.",
                ephemeral=True
            )
            return

        # Check the user actually has the role
        if awaiting_role not in user.roles:
            await interaction.followup.send(
                f"{user.mention} doesn't have the 🔴 Awaiting Follow-up role.",
                ephemeral=True
            )
            return

        # Remove the role
        try:
            await user.remove_roles(awaiting_role, reason=f"Unmark by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to remove roles.", ephemeral=True
            )
            return

        # Mark all open follow-ups as resolved in the DB
        open_items = await db.get_open_follow_ups(str(user.id))
        await db.resolve_all_open_follow_ups(
            user_id=str(user.id),
            resolver_id=str(interaction.user.id)
        )
        await db.increment_resolved_count(str(user.id))

        # Audit log
        await db.add_audit_log(
            action="unmark",
            actor_id=str(interaction.user.id),
            target_id=str(user.id),
            detail=f"Resolved via /unmark command in guild {interaction.guild_id}"
        )

        await interaction.followup.send(
            f"✅ {user.mention} has been unmarked and all open follow-ups resolved.",
            ephemeral=True
        )

        # Close the loop on each flagged message, so readers see it's resolved
        for item in open_items[:3]:
            await followup_notify.notify_member(
                interaction.guild, user, item.get("message_link"),
                reply_text=f"✅ {user.mention}, this follow-up was resolved by {interaction.user.mention}. Thanks!",
            )

    # ──────────────────────────────────────────
    # /score @user
    # ──────────────────────────────────────────
    @app_commands.command(
        name="score",
        description="Show a user's follow-up stats (flag count, resolved, open, avg time)."
    )
    @app_commands.describe(user="The user to check")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def score(self, interaction: discord.Interaction, user: discord.Member):
        """Show flag_count, resolved_count, open_count, avg_resolution_time."""
        await interaction.response.defer(ephemeral=True)

        score_data = await db.get_user_score(str(user.id))
        open_items = await db.get_open_follow_ups(str(user.id))
        avg_hours = await db.get_avg_resolution_time(str(user.id))

        if score_data is None:
            await interaction.followup.send(
                f"No follow-up history found for {user.mention}.",
                ephemeral=True
            )
            return

        avg_str = f"{avg_hours:.1f}h" if avg_hours is not None else "N/A"

        embed = discord.Embed(
            title=f"📊 Follow-up Score - {user.display_name}",
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="🚩 Total Flagged", value=str(score_data["flag_count"]), inline=True)
        embed.add_field(name="✅ Resolved", value=str(score_data["resolved_count"]), inline=True)
        embed.add_field(name="🔴 Currently Open", value=str(len(open_items)), inline=True)
        embed.add_field(name="🚫 Ignored (>7d)", value=str(score_data["ignored_count"]), inline=True)
        embed.add_field(name="⏱ Avg Resolution Time", value=avg_str, inline=True)

        if score_data["last_flagged"]:
            embed.add_field(name="📅 Last Flagged", value=score_data["last_flagged"][:19], inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # /stats - server-wide overview
    # ──────────────────────────────────────────
    @app_commands.command(
        name="stats",
        description="Show server-wide follow-up stats and leaderboards."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def stats(self, interaction: discord.Interaction):
        """Server-wide stats: totals, resolution rate, top flagged users, top helpers."""
        await interaction.response.defer(ephemeral=True)

        data = await db.get_server_stats(str(interaction.guild_id))

        total = data["total"]
        resolved_pct = (
            round(data["resolved"] / total * 100) if total > 0 else 0
        )
        avg_str = f"{data['avg_hours']:.1f}h" if data["avg_hours"] is not None else "N/A"

        embed = discord.Embed(
            title="📊 Server Follow-up Stats",
            color=discord.Color.blurple()
        )

        # Overall numbers
        embed.add_field(name="Total Flags", value=str(total), inline=True)
        embed.add_field(name="Open", value=str(data["open"]), inline=True)
        embed.add_field(name="Resolved", value=f"{data['resolved']} ({resolved_pct}%)", inline=True)
        embed.add_field(name="Ignored", value=str(data["ignored"]), inline=True)
        embed.add_field(name="Avg Resolution", value=avg_str, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

        # Top flagged users
        if data["top_flagged"]:
            lines = [
                f"{i+1}. <@{u['user_id']}> - {u['flag_count']} flags"
                for i, u in enumerate(data["top_flagged"])
            ]
            embed.add_field(name="Most Flagged", value="\n".join(lines), inline=False)

        # Top helpers
        if data["top_helpers"]:
            lines = [
                f"{i+1}. <@{h['actor_id']}> - {h['actions']} actions"
                for i, h in enumerate(data["top_helpers"])
            ]
            embed.add_field(name="Most Active Helpers", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # /audit
    # ──────────────────────────────────────────
    @app_commands.command(
        name="audit",
        description="Show the last 10 audit log entries (flags, resolves, etc.)."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @is_helper()
    async def audit(self, interaction: discord.Interaction):
        """Display the 10 most recent audit log entries."""
        await interaction.response.defer(ephemeral=True)

        entries = await db.get_recent_audit_log(limit=10)
        if not entries:
            await interaction.followup.send("No audit log entries yet.", ephemeral=True)
            return

        lines = []
        for e in entries:
            ts = e["timestamp"][:19] if e["timestamp"] else "unknown"
            actor = f"<@{e['actor_id']}>" if e["actor_id"] else "system"
            target = f"<@{e['target_id']}>" if e["target_id"] else "-"
            lines.append(f"`{ts}` **{e['action']}** | actor: {actor} | target: {target} | {e['detail'] or ''}")

        embed = discord.Embed(
            title="📋 Audit Log (last 10)",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(FollowUpCommands(bot))
