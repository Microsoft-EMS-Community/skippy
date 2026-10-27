# ============================================================
# fun.py - Skippy's fun commands: /ghostcheck, /verdict, /roulette,
# /trial, /slap and /effie (Effie only)
# ============================================================

import asyncio
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_ID


class FunCommands(commands.Cog):
    """Ghost-themed fun for everyone."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────
    # /ghostcheck @user - Ghost Probability Meter
    # ──────────────────────────────────────────
    @app_commands.command(
        name="ghostcheck",
        description="Run a ghost probability check on a user. Science. Probably."
    )
    @app_commands.describe(user="The suspect")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def ghostcheck(self, interaction: discord.Interaction, user: discord.Member):
        score = random.randint(0, 100)

        if score <= 20:
            verdict = "Clean record. Suspicious."
            bar = "🟢🟢🟢🟢🟢"
            color = discord.Color.green()
        elif score <= 40:
            verdict = "Probably fine. Probably."
            bar = "🟢🟢🟢🟡⬜"
            color = discord.Color.yellow()
        elif score <= 60:
            verdict = "Could go either way. I'm watching."
            bar = "🟡🟡🟡⬜⬜"
            color = discord.Color.orange()
        elif score <= 80:
            verdict = "Ghosting potential detected. Be careful."
            bar = "🔴🔴🔴🔴⬜"
            color = discord.Color.red()
        elif score < 100:
            verdict = "High risk. Very high risk."
            bar = "🔴🔴🔴🔴🔴"
            color = discord.Color.dark_red()
        else:
            verdict = "It's already too late."
            bar = "💀💀💀💀💀"
            color = discord.Color.dark_red()

        embed = discord.Embed(
            title=f"👻 Ghost Probability: {user.display_name}",
            color=color
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Score", value=f"**{score}%** {bar}", inline=False)
        embed.add_field(name="Skippy's Verdict", value=verdict, inline=False)
        embed.set_footer(text="Skippy Ghost Detection System v1.0 | Results are scientifically dubious")
        await interaction.response.send_message(embed=embed)

    # ──────────────────────────────────────────
    # /verdict @user - Community Verdict poll
    # ──────────────────────────────────────────
    @app_commands.command(
        name="verdict",
        description="Let the server decide - is this person going to ghost?"
    )
    @app_commands.describe(user="The accused")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def verdict(self, interaction: discord.Interaction, user: discord.Member):
        openers = [
            f"The server will now decide the fate of {user.mention}.",
            f"Skippy is calling a vote. {user.mention} stands accused.",
            f"Court is in session. {user.mention} - how do you plead? Actually, it doesn't matter.",
            f"The people have a right to know. Will {user.mention} ghost?",
            f"Skippy has gathered enough evidence. Time to let the server judge {user.mention}.",
        ]
        closes_at = int(time.time()) + 120
        embed = discord.Embed(
            title="👻 Community Ghost Verdict",
            description=f"{random.choice(openers)}\n\nVoting closes <t:{closes_at}:R>",
            color=discord.Color.blurple()
        )
        embed.add_field(name="👻 Ghost", value="They'll vanish. Trust nothing.", inline=True)
        embed.add_field(name="✅ Not a Ghost", value="Give them the benefit of the doubt.", inline=True)
        embed.set_footer(text="Skippy will be watching the results")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await msg.add_reaction("👻")
        await msg.add_reaction("✅")

        async def close_verdict():
            await asyncio.sleep(120)
            msg_updated = await msg.channel.fetch_message(msg.id)
            ghost = not_ghost = 0
            for r in msg_updated.reactions:
                if str(r.emoji) == "👻":
                    ghost = max(0, r.count - 1)
                elif str(r.emoji) == "✅":
                    not_ghost = max(0, r.count - 1)
            if ghost > not_ghost:
                result = f"👻 The server has spoken. {user.mention} is a ghoster. Skippy is not surprised."
            elif not_ghost > ghost:
                result = f"✅ {user.mention} gets the benefit of the doubt. Skippy is watching anyway."
            else:
                result = f"🤨 It's a tie. Skippy is counting that as suspicious. {user.mention} - eyes on you."
            await msg.channel.send(
                f"**Verdict closed.** ({ghost} 👻 vs {not_ghost} ✅)\n{result}"
            )

        asyncio.create_task(close_verdict())

    # ──────────────────────────────────────────
    # /roulette - Ghost Roulette
    # ──────────────────────────────────────────
    @app_commands.command(
        name="roulette",
        description="Skippy randomly picks today's most likely ghoster. Nobody is safe."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def roulette(self, interaction: discord.Interaction):
        members = [m for m in interaction.guild.members if not m.bot and m.id != interaction.user.id]
        if not members:
            await interaction.response.send_message("Nobody to pick. Suspicious.", ephemeral=True)
            return

        target = random.choice(members)
        declarations = [
            f"🎰 The wheel has spoken. Today's most likely ghoster is... {target.mention}. No reason. Just vibes.",
            f"🎰 Skippy has run the numbers. {target.mention} is today's ghost risk. Good luck.",
            f"🎰 Completely random. Totally fair. {target.mention} is under watch today.",
            f"🎰 The algorithm has chosen {target.mention}. Skippy accepts no complaints.",
            f"🎰 Today's ghost roulette lands on {target.mention}. Nothing personal. Everything personal.",
            f"🎰 {target.mention} - you didn't do anything wrong. That we know of. Yet.",
            f"🎰 Skippy spins the wheel... it slows... it stops. {target.mention}. Better answer your messages today.",
        ]
        await interaction.response.send_message(random.choice(declarations))

    # ──────────────────────────────────────────
    # /trial @user - The Trial
    # ──────────────────────────────────────────
    @app_commands.command(
        name="trial",
        description="Put someone on trial for ghosting. Court is now in session."
    )
    @app_commands.describe(user="The accused")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def trial(self, interaction: discord.Interaction, user: discord.Member):
        all_charges = [
            "Typing '...' for 45 minutes and sending nothing",
            "Being online at 2am but too busy to reply",
            "Reacting with 👀 as if that counts as an answer",
            "Saying 'omg yes let's talk!' and then vanishing for 6 days",
            "Leaving a message on read and then liking someone else's meme 30 seconds later",
            "Sending a voice message that was just breathing",
            "Replying 'haha' and considering that a conversation",
            "Going idle mid-sentence. Twice.",
            "Blaming it on notifications. Again.",
            "Opening the message, closing it, and hoping it would resolve itself",
            "Typing 'lol' in response to a question that needed an actual answer",
            "Posting in general chat 4 minutes after claiming to be busy",
            "Saying 'I'll reply in a bit' in 2019 and still not following up",
        ]
        charges = random.sample(all_charges, k=random.randint(2, 3))
        charges_text = "\n".join(f"• {c}" for c in charges)

        openers = [
            f"⚖️ Okay everyone, {user.mention} has been dragged to court. Let's go.",
            f"⚖️ {user.mention} - Skippy has assembled a jury and none of them look happy.",
            f"⚖️ The case against {user.mention} has been filed. The evidence is embarrassing.",
            f"⚖️ {user.mention} didn't think this would happen. And yet. Here we are.",
        ]

        embed = discord.Embed(
            title="⚖️ Ghost Trial",
            description=random.choice(openers),
            color=discord.Color.dark_gold()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="The Charges", value=charges_text, inline=False)
        embed.add_field(
            name="Cast your vote",
            value="🔨 **GUILTY** - absolutely.\n🕊️ **NOT GUILTY** - sure, why not.",
            inline=False
        )
        closes_at = int(time.time()) + 120
        embed.add_field(name="Voting closes", value=f"<t:{closes_at}:R>", inline=False)
        embed.set_footer(text="Skippy's totally legitimate court | No appeals accepted")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await msg.add_reaction("🔨")
        await msg.add_reaction("🕊️")

        async def close_trial():
            await asyncio.sleep(120)
            msg_updated = await msg.channel.fetch_message(msg.id)
            guilty = not_guilty = 0
            for r in msg_updated.reactions:
                if str(r.emoji) == "🔨":
                    guilty = max(0, r.count - 1)
                elif str(r.emoji) == "🕊️":
                    not_guilty = max(0, r.count - 1)
            if guilty > not_guilty:
                result = f"🔨 **GUILTY.** {user.mention} - the jury has spoken. You know what you did."
            elif not_guilty > guilty:
                result = f"🕊️ **NOT GUILTY.** The jury was lenient. Skippy remains skeptical."
            else:
                result = f"🤨 **HUNG JURY.** It's a tie. Skippy is declaring {user.mention} suspicious by default."
            await msg.channel.send(
                f"⚖️ **Trial closed.** ({guilty} guilty vs {not_guilty} not guilty)\n{result}"
            )

        asyncio.create_task(close_trial())

    # ──────────────────────────────────────────
    # /slap @user - anyone can slap anyone
    # ──────────────────────────────────────────
    @app_commands.command(
        name="slap",
        description="Slap someone. In the name of Skippy."
    )
    @app_commands.describe(user="The unfortunate target")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def slap(self, interaction: discord.Interaction, user: discord.Member):
        responses = [
            f"👋 *Skippy slaps {user.mention} with a wet fish.*",
            f"💥 *{user.mention} didn't see it coming. Skippy did.*",
            f"🧤 *Skippy removes one glove and slaps {user.mention} across the face. A duel has been declared.*",
            f"👋 *{user.mention} has been slapped into next week.*",
            f"😈 *Skippy grins and slaps {user.mention}. Just because.*",
            f"💫 *{user.mention} sees stars. Courtesy of Skippy.*",
            f"👋 *Skippy doesn't ask questions. Skippy just slaps {user.mention}.*",
            f"🎯 *Clean hit. {user.mention} never stood a chance.*",
            f"😌 *Skippy slaps {user.mention} and walks away without looking back.*",
            f"👀 *{user.mention} has been slapped. Skippy is already gone.*",
        ]
        await interaction.response.send_message(random.choice(responses))

    # ──────────────────────────────────────────
    # /effie @user - Effie only
    # ──────────────────────────────────────────
    EFFIE_USER_ID = 804698656791592999

    @app_commands.command(
        name="effie",
        description="Effie's personal command. Don't ask."
    )
    @app_commands.describe(user="The target of Effie's wrath")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def effie(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.user.id != FunCommands.EFFIE_USER_ID:
            await interaction.response.send_message(
                "😤 This command belongs to Effie. You are not Effie.", ephemeral=True
            )
            return
        responses = [
            f"😤 *Effie gasps.* {user.mention} how DARE you. I'm telling everyone.",
            f"💅 *Effie flips her hair and points at {user.mention}.* You are SO done.",
            f"😱 *Effie clutches her pearls.* {user.mention}!!! Shocked. Appalled. Disgusted. (I loved it.)",
            f"💔 *Effie places one hand on her heart.* {user.mention}, this is the worst day of my life. Probably.",
            f"😤 *Effie points dramatically at {user.mention}.* Everyone look at them. LOOK.",
            f"💅 You think you can just do that, {user.mention}? Bold. Stupid. But bold.",
            f"😩 *Effie stares at {user.mention}.* I have never been more betrayed in my entire life. And I've seen things.",
            f"🎭 *Effie dramatically faints.* Someone catch me. Not {user.mention} though.",
            f"💅 *Effie narrows her eyes at {user.mention}.* I'm not angry. I'm just disappointed. Actually no, I'm angry.",
            f"🌸 *Effie enters the chat, sees {user.mention}, and immediately exits.*",
        ]
        await interaction.response.send_message(random.choice(responses))


async def setup(bot: commands.Bot):
    await bot.add_cog(FunCommands(bot))
