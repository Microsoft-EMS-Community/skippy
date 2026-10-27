# ============================================================
# mentions.py - What Skippy says when someone @mentions it
# ============================================================

import random

import discord
from discord.ext import commands

from config import HELPER_LOG_CHANNEL_ID, HELPER_ROLE_IDS

# When someone points Skippy at another member.
CALLOUTS = [
    "👀 Oh, {target}? Yeah. I'm watching.",
    "🤨 {target} has been noted. Whether they know it or not.",
    "👻 {target} - your friends are skeptical. That's never a great sign.",
    "📎 Interesting that you're pointing at {target}. I'll keep that in mind.",
    "🔴 You want me to watch {target}? Consider it done.",
    "😐 {target}. Noted. I don't forget names.",
    "👀 {target} just got put on my radar. Hope they follow up on things.",
    "📋 {target} added to the watchlist. Unofficially. For now.",
    "🤖 Activating skepticism mode for {target}. Thanks for the tip.",
    "😶 {target}. I see them. The question is - do they see me?",
]

# Questions about what Skippy is for.
PURPOSE_KEYWORDS = [
    "why do you exist", "why exist", "what do you do", "what are you",
    "what is your purpose", "what's your purpose", "whats your purpose",
    "why are you here", "how do you work", "how does this work",
    "explain yourself", "what can you do", "purpose", "who are you",
    "tell me about yourself", "what is this", "whats this", "what's this",
]

# Everything else.
QUIPS = [
    "👀 Thinking about ghosting someone? Bold strategy.",
    "📎 Pinging me won't close your follow-ups. Just saying.",
    "🤨 I see you. Your helpers see you too.",
    "📎 Still here. Still watching. Still waiting for that follow-up.",
    "👻 Ghosting is not the move. I'm literally designed to prevent that.",
    "🕵️ Interesting choice to ping the anti-ghosting bot. Very interesting.",
    "📎 I don't forget. I don't sleep. I don't accept excuses.",
    "😐 You pinged me. I pinged your conscience. We're even.",
    "🔴 My whole personality is follow-ups. What do you need?",
    "👀 Oh hi. Didn't see you there. Actually I did. I always do.",
    "📋 Fun fact: pinging me is way less useful than answering your follow-up.",
    "😶 Noted. Now go answer your helpers.",
    "🤖 Skippy online. Ghost detection active. You have been seen.",
]


def mentions_skippy(bot: commands.Bot, message: discord.Message) -> bool:
    """A message for Skippy: it @mentions Skippy and isn't an @everyone ping.
    Skippy answers those, and doesn't also nudge the sender about follow-ups
    or verification."""
    return bot.user in message.mentions and not message.mention_everyone


class MentionReplies(commands.Cog):
    """Answers @mentions: what Skippy is for, an intro for helpers, or a quip."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not mentions_skippy(self.bot, message):
            return
        member = message.guild.get_member(message.author.id)
        member_role_ids = {r.id for r in member.roles} if member else set()
        is_helper = HELPER_ROLE_IDS and any(rid in member_role_ids for rid in HELPER_ROLE_IDS)

        # Check if another user is mentioned alongside the bot - skeptical callout mode
        other_mentions = [u for u in message.mentions if u.id != self.bot.user.id and not u.bot]
        if other_mentions:
            await message.reply(random.choice(CALLOUTS).format(target=other_mentions[0].mention))
            return

        msg_lower = message.content.lower()
        if any(kw in msg_lower for kw in PURPOSE_KEYWORDS):
            await message.reply(
                "📎 Good question.\n\n"
                "I'm **Skippy** - I exist so helpers don't get left on read.\n"
                "When someone needs to follow up on something, a helper flags them with 🔴 and I make sure it doesn't get forgotten. "
                "If they go chat elsewhere without answering, I remind them.\n\n"
                "Nobody ghosts the helpers on my watch."
            )
        elif is_helper and message.channel.id == HELPER_LOG_CHANNEL_ID:
            # Full intro only for helpers in the helper log channel
            embed = discord.Embed(
                title="👋 Hey there! I'm Skippy.",
                description=(
                    "Your friendly neighbourhood follow-up bot - here to make sure helpers get closure "
                    "and nobody's question slips through the cracks.\n\n"
                    "**How it works:**\n"
                    "• A helper reacts 🔴 to a message that needs a follow-up\n"
                    "• The user gets a heads-up and the 🔴 role until it's resolved\n"
                    "• If they post elsewhere without addressing it, I'll remind them\n"
                    "• Once it's sorted, a helper removes the flag and everyone moves on\n\n"
                    "**Commands you can use:**\n"
                    "`/score @user` - see someone's follow-up history\n"
                    "`/stats` - server-wide overview\n"
                    "`/help` - full command list\n\n"
                    "*Helpers have a few extra commands for managing flags.*"
                ),
                color=discord.Color.orange()
            )
            embed.set_footer(text="Skippy 📎 | Keeping follow-ups on track")
            await message.reply(embed=embed)
        else:
            await message.reply(random.choice(QUIPS))


async def setup(bot: commands.Bot):
    await bot.add_cog(MentionReplies(bot))
