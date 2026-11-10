# ============================================================
# poll_cards.py - What an anonymous poll looks like while it's open and
# once it has closed, and the scrambled code a vote is stored under.
# No Discord or database calls.
# ============================================================

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import discord

BAR_WIDTH = 20
# A poll says it's about to close when a quarter of its time is left, a day
# before at the earliest. Polls shorter than this get no reminder: the poll
# itself was posted moments ago.
REMINDER_SHARE, REMINDER_MAX, REMINDER_MIN_DURATION = 0.25, timedelta(hours=24), timedelta(hours=2)
ABOUT = (
    "**How this poll is anonymous**\n"
    "• The poll only ever shows totals. Nobody in Discord, moderators included, can see who voted "
    "for what, or who didn't vote.\n"
    "• To stop double votes Skippy has to recognise a returning voter. It stores a scrambled code "
    "for that instead of your name or ID, different in every poll.\n"
    "• When the poll closes the codes are deleted. Only the totals are kept.\n"
    "• Results stay hidden until the poll closes, so early votes don't sway anyone."
)


def voter_key(secret: str, user_id: int) -> str:
    """The code a member's vote is stored under: the same for one member in
    one poll (so a second click replaces their vote), meaningless without
    the poll's secret, which is erased when the poll closes."""
    return hmac.new(secret.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()


def reminder_time(created: datetime, closes: datetime) -> datetime | None:
    """When a poll open from `created` to `closes` reminds people, if at all."""
    duration = closes - created
    if duration < REMINDER_MIN_DURATION:
        return None
    return closes - min(REMINDER_MAX, duration * REMINDER_SHARE)


def channel_reminder(poll: dict, url: str, waiting: int | None = None) -> str:
    """The line in the poll's channel. `waiting` is how many of a role's members haven't voted."""
    line = f"⏰ [This poll]({url}) closes <t:{closes_timestamp(poll)}:R>: **{poll['question']}**"
    return line + (f"\n{waiting} of <@&{poll['role_id']}> still to vote." if waiting else "")


def dm_reminder(poll: dict, url: str) -> str:
    """What a role's member who hasn't voted yet is told, privately."""
    return (f"🗳️ A poll is waiting for your vote and closes <t:{closes_timestamp(poll)}:R>:\n"
            f"**{poll['question']}**\n{url}\n"
            "-# Only people who haven't voted get this reminder. Nobody else can see whether you voted.")


def closes_timestamp(poll: dict) -> int:
    return int(datetime.fromisoformat(poll["closes_at"]).replace(tzinfo=timezone.utc).timestamp())


def _votes(count: int) -> str:
    return f"{count} vote" if count == 1 else f"{count} votes"


def open_card(poll: dict, votes: int, eligible: int | None = None, voted: int | None = None) -> discord.Embed:
    """The poll while people can vote: the question and how many have voted,
    never how the votes are split. `eligible` and `voted` count the poll's
    role when it has one."""
    if poll["role_id"]:
        who = f"For <@&{poll['role_id']}>. "
        turnout = f"**{voted} of {eligible}** have voted"
        closes = ", or as soon as everyone has voted"
    else:
        who, turnout, closes = "", f"**{_votes(votes)} so far**", ""
    embed = discord.Embed(
        title=f"🗳️ {poll['question']}",
        description=(f"{who}Pick one. Your vote is anonymous and you can change it until the poll closes.\n\n"
                     f"{turnout} · closes <t:{closes_timestamp(poll)}:R>{closes}"),
        color=0x7c3aed)
    embed.set_footer(text="Anonymous poll · results are shown when it closes")
    return embed


def bar(share: float) -> str:
    filled = round(share * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def closed_card(poll: dict, results: list[int], absent: int | None = None) -> discord.Embed:
    """The poll after it closed: a bar per choice and, for a role's poll,
    how many of the role didn't vote (never who)."""
    total = sum(results)
    lines = []
    for option, count in zip(poll["options"], results):
        share = count / total if total else 0
        lines.append(f"**{option}**\n`{bar(share)}` {round(share * 100)}% ({count})")
    summary = f"**{_votes(total)}** · closed"
    if poll["role_id"] and absent is not None:
        summary += f"\n<@&{poll['role_id']}>: **{absent} didn't vote**" if absent else \
                   f"\n<@&{poll['role_id']}>: everyone voted"
    embed = discord.Embed(title=f"🗳️ {poll['question']}", description="\n".join(lines) + f"\n\n{summary}",
                          color=0x06b6d4)
    embed.set_footer(text="Anonymous poll · nobody can see who voted for what")
    return embed
