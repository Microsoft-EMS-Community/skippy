# ============================================================
# feed_roundup.py - The weekly community roundup: Mondays 09:00 in the
# roundup channel, last week's posts from the community channel per
# blogger, and the three most-voted posts.
# ============================================================

import logging
from collections import Counter
from datetime import datetime, timedelta
from datetime import time as day_time
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import db
from config import (FEED_COMMUNITY_CHANNEL_ID, FEED_POLL_MINUTES, FEED_ROUNDUP_CHANNEL_ID, FEED_ROUNDUP_TIMEZONE,
                    FEED_VOTE_EMOJI)

logger = logging.getLogger("followup-bot")

ROUNDUP_TZ = ZoneInfo(FEED_ROUNDUP_TIMEZONE)
ROUNDUP_AT = day_time(9, 0)  # Mondays, local time in ROUNDUP_TZ


def week_of(now: datetime) -> str:
    """The ISO week a roundup belongs to, as stored in feed_roundup_last_week."""
    return "{0}-W{1}".format(*now.isocalendar()[:2])


def build_roundup(posts: list[dict], now: datetime, votes: dict[str, int] | None = None) -> discord.Embed | None:
    """The weekly roundup of the community channel, for #general: last week's
    posts grouped per blogger (in the order they first posted), plus the
    week's three most-voted posts by
    FEED_VOTE_EMOJI reactions. None for a quiet week. Mentions in embeds never
    ping, so crediting people here is silent."""
    votes = votes or {}
    # The same article can be posted twice (say, by /feed test): keep the copy
    # with the most votes, in its first position.
    best: dict[str, dict] = {}
    for p in posts:
        kept = best.get(p["link"])
        if kept is None or votes.get(p["message_url"] or "", 0) > votes.get(kept["message_url"] or "", 0):
            best[p["link"]] = p
    posts = [p for p in posts if best[p["link"]] is p]
    if not posts:
        return None
    groups: dict[str, dict] = {}
    for p in posts:  # oldest first, so groups come out in order of each blogger's first post
        key = p["author_id"] or f"feed-{p['feed_id']}"
        groups.setdefault(key, {"author_id": p["author_id"], "feed_title": p["feed_title"] or "", "posts": []})
        groups[key]["posts"].append(p)

    def who(author_id, feed_title):
        return f"<@{author_id}>" if author_id else f"**{discord.utils.escape_markdown(feed_title or '')}**"

    def linked(p):
        title = discord.utils.escape_markdown(" ".join(p["title"].split())[:150]).replace("[", "(").replace("]", ")")
        return f"[{title}](<{p['link']}>)"

    header = (f"**{len(posts)} new post{'s' if len(posts) != 1 else ''} from {len(groups)} "
              f"blogger{'s' if len(groups) != 1 else ''}** in <#{FEED_COMMUNITY_CHANNEL_ID}> this week. "
              "Thanks for writing! 💙\n")
    body, shown = "", 0
    for group in groups.values():
        block = f"\n{who(group['author_id'], group['feed_title'])}"
        for p in group["posts"]:
            block += f"\n• {linked(p)}"
        if len(header) + len(body) + len(block) > 3900:  # embed descriptions stop at 4096
            body += f"\n\n…and {len(posts) - shown} more posts."
            break
        body += block
        shown += len(group["posts"])

    week = (now - timedelta(days=1)).isocalendar().week  # the week that just ended
    embed = discord.Embed(title=f"📰 Community roundup · week {week}", description=header + body,
                          color=discord.Color.blurple())
    voted = sorted((p for p in posts if votes.get(p["message_url"] or "", 0) > 0),
                   key=lambda p: -votes[p["message_url"]])  # stable: ties go to the earlier post
    if voted:
        lines = [f"{medal} {linked(p)} by {who(p['author_id'], p['feed_title'])} · {votes[p['message_url']]} {FEED_VOTE_EMOJI}"
                 for medal, p in zip(["🥇", "🥈", "🥉"], voted)]
        # Server emojis render in field values, not in field names or footers.
        embed.add_field(name="🏆 Community favourites this week", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="Vote for favourites by reacting to posts · Want your blog in here? /blog suggest")
    return embed


async def vote_counts(bot, posts: list[dict]) -> dict[str, int]:
    """FEED_VOTE_EMOJI votes per posted message, not counting Skippy's own
    reaction or the author's. Summary messages (several posts in one
    message) are left out: a vote there can't tell the posts apart."""
    per_message = Counter(p["message_url"] for p in posts if p["message_url"])
    authors = {p["message_url"]: p["author_id"] for p in posts}
    votes = {}
    for url, n in per_message.items():
        if n > 1:
            continue
        try:
            channel_id, message_id = (int(part) for part in url.rstrip("/").split("/")[-2:])
            message = await bot.get_channel(channel_id).fetch_message(message_id)
        except (ValueError, AttributeError, discord.HTTPException):
            continue  # deleted message or channel: no votes to count
        for reaction in message.reactions:
            if str(reaction.emoji) == FEED_VOTE_EMOJI:
                votes[url] = len([u async for u in reaction.users()
                                  if u.id != bot.user.id and str(u.id) != authors[url]])
    return votes


async def this_weeks_roundup(bot, now: datetime) -> discord.Embed | None:
    """The roundup as it stands now: the community channel's last 7 days and their votes."""
    posts = await db.roundup_posts(str(FEED_COMMUNITY_CHANNEL_ID))
    return build_roundup(posts, now, await vote_counts(bot, posts))


class WeeklyRoundup(commands.Cog):
    """Posts the roundup every Monday from 09:00."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()

    # Checking every few minutes (rather than a fixed-time loop) keeps it at
    # 09:00 local time across daylight saving changes, and catches up on its
    # own if Skippy was restarting at 09:00.
    @tasks.loop(minutes=FEED_POLL_MINUTES)
    async def weekly(self):
        try:
            await self.maybe_post(datetime.now(ROUNDUP_TZ))
        except Exception:
            logger.exception("Weekly roundup failed")

    @weekly.before_loop
    async def before_weekly(self):
        await self.bot.wait_until_ready()

    async def maybe_post(self, now: datetime) -> bool:
        """Post this week's roundup if it's Monday from 09:00, it's switched
        on and it hasn't gone out yet this week. Returns whether it posted."""
        if now.weekday() != 0 or now.time() < ROUNDUP_AT:
            return False
        if await db.get_setting("feed_roundup", "on") != "on":
            return False
        week = week_of(now)
        if await db.get_setting("feed_roundup_last_week") == week:
            return False
        await db.set_setting("feed_roundup_last_week", week)  # first, so a crash can't post it twice
        embed = await this_weeks_roundup(self.bot, now)
        channel = self.bot.get_channel(FEED_ROUNDUP_CHANNEL_ID)
        if embed is None or channel is None:
            logger.info("Weekly roundup: %s", "quiet week, nothing to post" if embed is None else "channel not found")
            return False
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        logger.info("Weekly roundup posted for %s", week)
        return True


async def setup(bot: commands.Bot):
    await bot.add_cog(WeeklyRoundup(bot))
