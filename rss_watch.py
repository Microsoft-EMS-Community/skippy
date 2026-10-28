# ============================================================
# rss_watch.py - The RSS watchlist: counts watched members' messages,
# /rsswatch to manage and report on it, and a full report on the 1st of
# every month. Feeds use it to judge whether a blog's author is active.
# ============================================================

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from checks import is_rss_admin
from config import GUILD_ID, RSS_AUDIT_CHANNEL_ID

logger = logging.getLogger("followup-bot")

WINDOW_DAYS = 30


def badge(count: int) -> str:
    """🟢 active (5+ messages in the window), 🟡 low (1-4), 🔴 inactive."""
    return "🟢" if count >= 5 else ("🟡" if count >= 1 else "🔴")


async def report_lines(guild: discord.Guild, watchlist: list[dict], with_link: bool = True) -> list[str]:
    """One line per watched member: badge, name, last seen, message count."""
    lines = []
    for entry in watchlist:
        uid = entry["user_id"]
        count = await db.get_rss_activity_count(uid, days=WINDOW_DAYS)
        latest = await db.get_latest_rss_message(uid)
        member = guild.get_member(int(uid))
        name = member.display_name if member else f"‹{uid}›"
        last_seen = latest["sent_at"][:10] if latest else "never"
        line = f"{badge(count)} **{name}** | Last seen: {last_seen} | Msgs (30d): {count}"
        if with_link:
            line += " | " + (f"[latest]({latest['message_link']})" if latest else "no messages")
        lines.append(line)
    return lines


async def send_report(channel: discord.abc.Messageable, header: str, lines: list[str]):
    """The header and lines as one message, or split up to fit Discord's 2000 characters."""
    report = header + "\n".join(lines)
    if len(report) <= 2000:
        await channel.send(report)
        return
    await channel.send(header.strip())
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 1900:
            await channel.send(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await channel.send(chunk)


class RssWatch(commands.Cog):
    """RSS watchlist: activity counting, commands and the monthly report."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_monthly_report: str | None = None  # "YYYY-MM" of the last automatic report
        self.monthly_report.start()

    def cog_unload(self):
        self.monthly_report.cancel()

    rsswatch = app_commands.Group(
        name="rsswatch",
        description="Manage the RSS activity watchlist."
    )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Count every message from a watched member."""
        if message.author.bot or message.guild is None or not await db.is_watched(str(message.author.id)):
            return
        await db.log_rss_activity(
            user_id=str(message.author.id),
            guild_id=str(message.guild.id),
            channel_id=str(message.channel.id),
            message_id=str(message.id),
            message_link=f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}",
        )

    @rsswatch.command(name="add", description="Add a user to the RSS watchlist.")
    @app_commands.describe(user="The member to watch")
    @is_rss_admin()
    async def rsswatch_add(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await db.add_to_watchlist(
            user_id=str(user.id),
            guild_id=str(interaction.guild_id),
            added_by=str(interaction.user.id),
        )
        await interaction.followup.send(
            f"✅ **{user.display_name}** added to RSS watchlist. Skippy is now tracking their activity.",
            ephemeral=True,
        )

    @rsswatch.command(name="remove", description="Remove a user from the RSS watchlist.")
    @app_commands.describe(user="The member to remove")
    @is_rss_admin()
    async def rsswatch_remove(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        await db.remove_from_watchlist(str(user.id))
        await interaction.followup.send(
            f"❌ **{user.display_name}** removed from RSS watchlist.",
            ephemeral=True,
        )

    @rsswatch.command(name="list", description="Show all users on the RSS watchlist with current status.")
    @is_rss_admin()
    async def rsswatch_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        watchlist = await db.get_watchlist(str(interaction.guild_id))
        if not watchlist:
            await interaction.followup.send("📄 Watchlist is empty.", ephemeral=True)
            return
        embed = discord.Embed(
            title="📋 RSS Watchlist",
            description="\n".join(await report_lines(interaction.guild, watchlist, with_link=False)),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(watchlist)} user(s) watched")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @rsswatch.command(name="report", description="Post RSS activity report. Leave user blank for full server report.")
    @app_commands.describe(user="Optional: single user deep-dive")
    @is_rss_admin()
    async def rsswatch_report(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)
        audit_channel = interaction.guild.get_channel(RSS_AUDIT_CHANNEL_ID)
        if not audit_channel:
            await interaction.followup.send("⚠️ Audit channel not found. Check RSS_AUDIT_CHANNEL_ID in config.", ephemeral=True)
            return
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

        if user:
            # ── Single-user deep dive ──
            uid = str(user.id)
            count = await db.get_rss_activity_count(uid, days=WINDOW_DAYS)
            activity = await db.get_rss_activity(uid, days=WINDOW_DAYS)

            embed = discord.Embed(
                title=f"{badge(count)} RSS Report - {user.display_name}",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Messages (30d)", value=str(count), inline=True)
            embed.add_field(name="Status", value="Active" if count >= 5 else ("Low" if count >= 1 else "Inactive"), inline=True)

            if activity:
                links = "\n".join(
                    f"• [{row['sent_at'][:10]} {row['sent_at'][11:16]}]({row['message_link']})"
                    for row in activity[:25]  # cap at 25 to avoid embed limits
                )
                embed.add_field(name=f"Message Links (last {min(len(activity), 25)})", value=links, inline=False)
            else:
                embed.add_field(name="Messages", value="No activity recorded in the last 30 days.", inline=False)

            embed.set_footer(text=f"Requested by {interaction.user.display_name} • {now}")
            await audit_channel.send(embed=embed)
            await interaction.followup.send(f"✅ Deep-dive report for **{user.display_name}** posted to audit channel.", ephemeral=True)
            return

        # ── Full server report ──
        watchlist = await db.get_watchlist(str(interaction.guild_id))
        if not watchlist:
            await interaction.followup.send("📄 Watchlist is empty - nothing to report.", ephemeral=True)
            return
        header = f"📊 **RSS Watchlist Report** - {now}\n*Requested by {interaction.user.display_name}*\n\n"
        await send_report(audit_channel, header, await report_lines(interaction.guild, watchlist))
        await interaction.followup.send(f"✅ Report posted to audit channel ({len(watchlist)} user(s)).", ephemeral=True)

    @rsswatch.command(name="help", description="Show all /rsswatch commands and how to use them.")
    async def rsswatch_help(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Skippy - RSS Watchlist Commands",
            description="Track member activity for RSS feed management. Admin-only commands.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Managing the Watchlist",
            value=(
                "`/rsswatch add @user` - Add a member to the watchlist\n"
                "`/rsswatch remove @user` - Remove a member from the watchlist\n"
                "`/rsswatch list` - Show all watched members with current activity status"
            ),
            inline=False,
        )
        embed.add_field(
            name="Reports",
            value=(
                "`/rsswatch report` - Post full activity report for all watched members to audit channel\n"
                "`/rsswatch report @user` - Deep-dive report for one member - shows all stored message links"
            ),
            inline=False,
        )
        embed.add_field(
            name="Activity Thresholds (30-day window)",
            value=(
                "🟢 **Active** - 5 or more messages\n"
                "🟡 **Low** - 1 to 4 messages\n"
                "🔴 **Inactive** - 0 messages"
            ),
            inline=False,
        )
        embed.add_field(
            name="Auto Reports",
            value="A full report is automatically posted to the audit channel on the 1st of each month at 09:00 UTC.",
            inline=False,
        )
        embed.set_footer(text="Skippy - RSS Watchlist | Admin only")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # The 1st of each month, 09:00 UTC: a full report, then old activity is cleared.
    @tasks.loop(hours=1)
    async def monthly_report(self):
        now = datetime.now(timezone.utc)
        if now.day != 1 or now.hour != 9:
            return
        month_key = now.strftime("%Y-%m")
        if self._last_monthly_report == month_key:
            return  # already fired this month
        self._last_monthly_report = month_key

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return
        audit_channel = guild.get_channel(RSS_AUDIT_CHANNEL_ID)
        if not audit_channel:
            return

        watchlist = await db.get_watchlist(str(GUILD_ID))
        if not watchlist:
            await audit_channel.send("📄 **Monthly RSS Watchlist Report** - watchlist is empty.")
            return

        lines = await report_lines(guild, watchlist)
        await db.cleanup_rss_activity(days=WINDOW_DAYS)  # after counting, so this report still has it
        await send_report(audit_channel, f"📊 **Monthly RSS Watchlist Report** - {now.strftime('%B %Y')}\n\n", lines)
        logger.info(f"Monthly RSS report posted for {month_key}")

    @monthly_report.before_loop
    async def before_monthly_report(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(RssWatch(bot))
