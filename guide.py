# ============================================================
# guide.py - /help: the moderator guide to everything Skippy does, and the
# command overview everyone else gets
# ============================================================

import discord
from discord import app_commands
from discord.ext import commands

from checks import is_moderator
from config import (AWAITING_ROLE_ID, FEED_CHANNELS, FEED_COMMUNITY_CHANNEL_ID, FEED_POLL_MINUTES,
                    FEED_ROUNDUP_CHANNEL_ID, FEED_VOTE_EMOJI, FLAG_EMOJI, GUILD_ID, HELPER_LOG_CHANNEL_ID,
                    NEW_ACCOUNT_AGE_DAYS, NEW_ACCOUNT_ROLE_ID, RSS_AUDIT_CHANNEL_ID)


def member_help() -> discord.Embed:
    """/help for everyone who isn't a moderator: the same for all of them, in every channel."""
    embed = discord.Embed(
        title="📎 Skippy - Commands",
        description="The skeptical follow-up bot. Here to make sure nobody ghosts their helpers.",
        color=discord.Color.orange()
    )

    embed.add_field(
        name="For Everyone",
        value=(
            "`/score @user` - See a user's follow-up history\n"
            "`/stats` - Server-wide overview and leaderboards\n"
            "`/ghostcheck @user` - Ghost probability meter. Science. Probably.\n"
            "`/verdict @user` - Let the server vote - will they ghost?\n"
            "`/trial @user` - Put someone on trial for ghosting. Court is in session.\n"
            "`/roulette` - Skippy picks today's most likely ghoster. Nobody is safe.\n"
            "`/slap @user` - Slap someone. In the name of Skippy.\n"
            "`/blog suggest <feed url> <author>` - Suggest a member's blog (yours or someone else's) for the community feed\n"
            "`/blog search <words>` - Search everything the feeds have posted\n"
            "`/blog links` - Show your LinkedIn, X, YouTube or GitHub under your blog's posts\n"
            "`/help` - This message"
        ),
        inline=False
    )

    embed.add_field(
        name="Community feed",
        value=(
            f"New posts from the community's blogs land in <#{FEED_COMMUNITY_CHANNEL_ID}>, each with a "
            f"discussion thread. Vote for your favourites with {FEED_VOTE_EMOJI}: every Monday the "
            f"roundup in <#{FEED_ROUNDUP_CHANNEL_ID}> recaps the week and crowns the 3 most-voted posts."
        ),
        inline=False
    )

    embed.add_field(
        name="Helpers Only",
        value=(
            "`/flag @user [message_link] [reason]` - Flag a user for follow-up\n"
            "`/unmark @user` - Resolve a flag and remove the 🔴 role\n"
            "`/audit` - View the last 10 actions logged by the bot"
        ),
        inline=False
    )

    embed.add_field(
        name="Special",
        value="`/effie @user` - Effie only. Don't ask.",
        inline=False
    )

    embed.add_field(
        name="How flagging works",
        value=(
            "React 🔴 on any message to flag that user - works in channels, threads and forum posts.\n"
            "The user gets pinged and the 🔴 role is assigned automatically.\n"
            "Helpers can unmark via the button in the helper log or with `/unmark`."
        ),
        inline=False
    )
    return embed


def moderator_help() -> discord.Embed:
    """/help for moderators: every feature, its commands, and where it reports."""
    mod_audit, bot_management = f"<#{RSS_AUDIT_CHANNEL_ID}>", f"<#{HELPER_LOG_CHANNEL_ID}>"
    embed = discord.Embed(
        title="📎 Skippy - Moderator guide",
        description=(
            "Everything Skippy does here and how to run it. Only moderators see this; everyone else "
            "gets a short command overview from `/help`. Commands marked *everyone* or *helpers* work "
            "for those members too."
        ),
        color=discord.Color.orange()
    )

    embed.add_field(
        name="🔴 Follow-ups",
        value=(
            f"A helper reacts {FLAG_EMOJI} on a question, or uses `/flag`: the member gets "
            f"<@&{AWAITING_ROLE_ID}> and a reply on that message.\n"
            "• Posting elsewhere while flagged gets a public nudge (once a day at most)\n"
            "• A reminder after 48 hours; after 7 days it's marked ignored\n"
            f"• Replies, leaving and rejoining show up in {bot_management} with buttons\n"
            "`/flag @user [message_link] [reason]` · `/unmark @user` · `/audit` - last 10 actions (*helpers*)\n"
            "`/score @user` · `/stats` - follow-up history and leaderboards (*everyone*)"
        ),
        inline=False
    )

    embed.add_field(
        name="📰 Feeds",
        value=(
            f"Every feed is checked every {FEED_POLL_MINUTES} minutes and posts in "
            + ", ".join(f"<#{c}>" for c in FEED_CHANNELS.values()) + ". "
            f"In <#{FEED_COMMUNITY_CHANNEL_ID}> each post gets a thread and the {FEED_VOTE_EMOJI} vote; "
            f"Mondays 09:00 the roundup in <#{FEED_ROUNDUP_CHANNEL_ID}> recaps the week and crowns "
            "the 3 most-voted posts.\n"
            "`/feed add <url>` - channel and author are options; its newest post goes out right away\n"
            "`/feed edit <feed>` - name, channel, author, filters, summaries; no options shows them\n"
            "`/feed list` · `/feed test` · `/feed remove`\n"
            "`/feed roundup [action]` - preview it, post it now, or switch it off\n"
            "`/feed activity [mode]` - only active members' blogs post: off → watch → warn → enforce"
        ),
        inline=False
    )

    embed.add_field(
        name="📬 Blog suggestions and search",
        value=(
            f"`/blog suggest <url> <author>` (*everyone*) - members' blogs only: they pick the member who "
            f"writes it, even if it's themselves; the card lands in {mod_audit} to approve or reject\n"
            "`/blog links` (*everyone*) - authors put their LinkedIn, X, YouTube or GitHub under their posts; "
            "mods can set them with `/feed add` and `/feed edit`\n"
            "`/blog search <words>` (*everyone*) - everything the feeds have posted"
        ),
        inline=False
    )

    embed.add_field(
        name="👀 RSS watchlist",
        value=(
            "Counts watched members' messages over 30 days (channels Skippy can see). "
            "Linking a feed's author adds them.\n"
            "`/rsswatch add @user` · `/rsswatch remove @user` · `/rsswatch list`\n"
            f"`/rsswatch report [@user]` - posted in {mod_audit}; a full one goes out on the 1st of each month\n"
            "`/rsswatch help` - the details"
        ),
        inline=False
    )

    embed.add_field(
        name="🆕 New accounts",
        value=(
            f"Accounts younger than {NEW_ACCOUNT_AGE_DAYS} days or with a default avatar get "
            f"<@&{NEW_ACCOUNT_ROLE_ID}> and \"[unverified]\" in their nickname. When they post, Skippy asks "
            "them to press \"I'm Human\" (by DM, or in the channel if their DMs are closed), which removes "
            f"both. Each account gets one card in {mod_audit} that shows decoy-button and too-fast clicks; "
            "3+ joins within 10 minutes raise a join-burst alert there."
        ),
        inline=False
    )

    embed.add_field(
        name="🔗 Links",
        value=(
            "Tracking junk (utm_ and friends) is stripped: Skippy replies with the clean link and hides "
            "the original's preview. Short links are followed first, and one that leads to a private "
            f"or internal address is blocked and reported in {mod_audit}."
        ),
        inline=False
    )

    embed.add_field(
        name="🎲 Fun (*everyone*)",
        value=(
            "`/ghostcheck` · `/verdict` · `/trial` · `/roulette` · `/slap` · `/effie` (Effie only). "
            "Mention @Skippy and it answers back."
        ),
        inline=False
    )

    embed.add_field(
        name="🗂️ Where Skippy reports",
        value=(
            f"{mod_audit} (mods): feed changes, suggestions, failing feeds, new accounts, join bursts, "
            "blocked links, watchlist reports\n"
            f"{bot_management} (helpers): the follow-up log and buttons, monthly cleanup, updates going live\n"
            "Its own log on the server keeps the last 7 days."
        ),
        inline=False
    )

    embed.add_field(
        name="🛠️ Changing Skippy",
        value=(
            "The code is open: [Microsoft-EMS-Community/skippy](https://github.com/Microsoft-EMS-Community/skippy). "
            "Anyone can propose a change; once the tests pass and it's merged, it goes live within about "
            f"5 minutes and {bot_management} says so."
        ),
        inline=False
    )
    return embed


class Guide(commands.Cog):
    """/help"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="help",
        description="Show what Skippy does and the commands you can use."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def help(self, interaction: discord.Interaction):
        """Moderators get the guide to everything Skippy does; everyone else
        gets the same command overview. It depends only on the member's
        roles, never on the channel it's used in."""
        embed = moderator_help() if is_moderator(interaction.user) else member_help()
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Guide(bot))
