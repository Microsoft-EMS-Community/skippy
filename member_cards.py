# ============================================================
# member_cards.py - The card about a member that Skippy posts for helpers
# and mods: what happened, plus the member's follow-up record.
# ============================================================

import discord

import db


async def member_card(
    title: str,
    color: discord.Color,
    user: discord.Member,
    description: str = "",
    link: str = None,
    extra_fields: list[tuple] = None,
) -> discord.Embed:
    """
    Build a clean embed for the helper log channel.
    Automatically appends score stats as inline fields.
    """
    embed = discord.Embed(title=title, color=color)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

    if description:
        embed.description = description
    if link:
        embed.add_field(name="Message", value=f"[Jump]({link})", inline=False)
    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)

    # Score stats
    score = await db.get_user_score(str(user.id))
    open_items = await db.get_open_follow_ups(str(user.id))
    if score:
        embed.add_field(name="🚩 Flagged", value=str(score["flag_count"]), inline=True)
        embed.add_field(name="✅ Resolved", value=str(score["resolved_count"]), inline=True)
        embed.add_field(name="🚫 Ignored", value=str(score["ignored_count"]), inline=True)
        embed.add_field(name="🔴 Open", value=str(len(open_items)), inline=True)

    return embed
