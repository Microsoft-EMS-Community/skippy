# ============================================================
# link_watch.py - Watches links in members' messages: replies with the
# link minus its tracking junk, and blocks short links that lead to an
# internal address. Cleaning and checking links is link_cleaner.py's job.
# ============================================================

import asyncio

import discord
from discord.ext import commands

import db
import link_cleaner
from config import RSS_AUDIT_CHANNEL_ID
from member_cards import member_card

# How long Discord's crawler can take to attach a video embed after the message
# lands - long enough that a single suppress() right after on_message can miss it.
EMBED_RETRY_DELAY_SECONDS = 4


def _defang(url: str) -> str:
    """Neutralize a URL so it can be posted in a public channel without
    Discord embedding/rendering it as a live clickable link."""
    return url.replace("://", "[://]").replace(".", "[.]")


async def suppress_with_retry(message: discord.Message):
    """Suppress a message's embed now, then again after a delay, since video embeds
    (YouTube, TikTok, etc.) can attach well after the message and first suppress call."""
    try:
        await message.edit(suppress=True)
    except (discord.Forbidden, discord.HTTPException):
        return

    await asyncio.sleep(EMBED_RETRY_DELAY_SECONDS)
    try:
        await message.edit(suppress=True)
    except (discord.Forbidden, discord.HTTPException):
        pass


class LinkWatch(commands.Cog):
    """Cleans links in members' messages and blocks dangerous short links."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        checking_notice = None
        if link_cleaner.has_shortener_link(message.content):
            # Suppress the original's auto-embed up front, before the redirect check,
            # so the raw (un-cleaned) short link never gets a chance to render one.
            asyncio.create_task(suppress_with_retry(message))
            try:
                checking_notice = await message.reply(
                    "🔍 Checking short link...",
                    mention_author=False,
                )
            except discord.HTTPException:
                checking_notice = None

        already_suppressing = checking_notice is not None

        changed_links, blocked_links = await link_cleaner.clean_links_async(message.content)

        if blocked_links:
            plural = "s" if len(blocked_links) > 1 else ""
            # Defanged (no scheme, dots masked) and code-blocked so Discord never
            # renders it as a clickable/embeddable link in the public channel.
            defanged = "\n".join(f"`{_defang(u)}`" for u in blocked_links)
            warning = (
                f"⚠️ **Danger zone:** short link{plural} redirected to an internal/private "
                f"address and was blocked:\n{defanged}"
            )
            if changed_links:
                warning += "\n\nRemaining link(s) processed normally below."
            try:
                if checking_notice is not None:
                    await checking_notice.edit(content=warning)
                    checking_notice = None
                else:
                    await message.reply(warning, mention_author=False)
            except discord.HTTPException:
                pass
            if not already_suppressing:
                asyncio.create_task(suppress_with_retry(message))
                already_suppressing = True

            for blocked_url in blocked_links:
                await db.add_audit_log(
                    action="ssrf_link_blocked",
                    actor_id=str(message.author.id),
                    target_id=None,
                    detail=f"{blocked_url} in {message.channel.name} ({message.jump_url})"
                )

            audit_channel = message.guild.get_channel(RSS_AUDIT_CHANNEL_ID)
            if audit_channel:
                embed = await member_card(
                    title="🚨 Blocked Short Link (possible SSRF)",
                    color=discord.Color.red(),
                    user=message.author,
                    description=(
                        f"A short link posted in {message.channel.mention} redirected to a "
                        f"private/internal address and was not followed."
                    ),
                    extra_fields=[
                        ("Link(s)", "\n".join(f"`{u}`" for u in blocked_links), False),
                        ("Message", f"[Jump]({message.jump_url})", True),
                    ],
                )
                try:
                    await audit_channel.send(embed=embed)
                except discord.HTTPException:
                    pass

        if changed_links:
            cleaned_text = "\n".join(cleaned for _, cleaned in changed_links)
            plural = "s" if len(changed_links) > 1 else ""
            final_text = f"🧹 Cleaned tracking junk from that link{plural}:\n{cleaned_text}"
            try:
                if checking_notice is not None:
                    await checking_notice.edit(content=final_text)
                else:
                    await message.reply(final_text, mention_author=False)
            except discord.HTTPException:
                pass

            if not already_suppressing:
                asyncio.create_task(suppress_with_retry(message))
        elif checking_notice is not None:
            try:
                await checking_notice.delete()
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkWatch(bot))
