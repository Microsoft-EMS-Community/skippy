# ============================================================
# followup_notify.py - Tell a flagged member about their follow-up
# Replies on the flagged message itself, so the context is right there
# (works in channels, threads and forum posts); falls back to a DM when
# that message can't be reached.
# ============================================================

import logging

import discord

logger = logging.getLogger("followup-bot")


async def flagged_message(guild: discord.Guild, message_link: str | None) -> discord.Message | None:
    """The message a follow-up is about, from its jump link, if Skippy can still reach it."""
    if not message_link:
        return None
    try:
        channel_id, message_id = (int(part) for part in message_link.rstrip("/").split("/")[-2:])
    except ValueError:
        return None
    try:
        channel = guild.get_channel_or_thread(channel_id) or await guild.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)
    except (discord.HTTPException, AttributeError):
        return None  # deleted, or somewhere Skippy can't see


async def notify_member(guild: discord.Guild, member: discord.abc.User, message_link: str | None,
                        reply_text: str, dm_text: str | None = None,
                        message: discord.Message | None = None) -> str | None:
    """Post reply_text as a reply on the flagged message (looked up from
    message_link unless it's passed in), or send dm_text as a DM when there's
    no message to reply to. Only the member is pinged. Returns "reply", "dm",
    or None if they couldn't be reached."""
    message = message or await flagged_message(guild, message_link)
    if message is not None:
        try:
            await message.reply(reply_text, allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[member], replied_user=False))
            return "reply"
        except discord.HTTPException as e:
            logger.warning("Couldn't reply on flagged message %s: %s", message_link, e)
    if dm_text:
        try:
            await member.send(dm_text)
            return "dm"
        except discord.HTTPException as e:
            logger.info("Couldn't DM %s about their follow-up: %s", member, e)
    return None
