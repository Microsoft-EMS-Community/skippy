# ============================================================
# feed_notices.py - How the feed features tell people things beyond a
# command's own reply: mods in the alert channel, members by DM, and mods'
# feed actions in the audit log.
# ============================================================

import logging

import discord
from discord import app_commands

import db
from config import FEED_ALERT_CHANNEL_ID

logger = logging.getLogger("followup-bot")


async def alert(bot, text: str):
    """A line in the mods' channel. Mentions in it never ping."""
    channel = bot.get_channel(FEED_ALERT_CHANNEL_ID)
    if channel:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())


async def dm(bot, user_id: str, text: str):
    """Best effort: plenty of members don't accept DMs from server bots."""
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        await user.send(text)
    except discord.HTTPException as e:
        logger.info("Couldn't DM %s: %s", user_id, e)


async def audit(bot, interaction: discord.Interaction, action: str, text: str):
    """Record a mod's feed action: in the audit log (/audit), the bot's
    log, and as a line in the mods' channel so other mods see it."""
    await db.add_audit_log(action=f"feed_{action}", actor_id=str(interaction.user.id),
                           target_id=None, detail=text)
    logger.info("%s %s", interaction.user, text)
    await alert(bot, f"🛠️ {interaction.user.mention} {text}")


async def command_failed(interaction: discord.Interaction, error: app_commands.AppCommandError, refused: str):
    """Tell the member why a command didn't work: `refused` when they lack
    the role, otherwise that something went wrong (with the details logged)."""
    if isinstance(error, app_commands.CheckFailure):
        message = refused
    else:
        logger.exception("/%s command failed", getattr(interaction.command, "qualified_name", "feed"),
                         exc_info=error)
        message = "Something went wrong; the details are in the bot's log."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
