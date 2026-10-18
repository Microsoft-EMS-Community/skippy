# ============================================================
# commands.py - Slash commands (guild-scoped)
# /unmark, /score, /audit
# ============================================================

import discord
from discord import app_commands
from discord.ext import commands

import datetime
import db
from config import GUILD_ID, AWAITING_ROLE_ID, FOLLOWUP_CHANNEL_ID, HELPER_ROLE_IDS, RSS_ADMIN_ROLE_ID, RSS_AUDIT_CHANNEL_ID
from config import FEED_ROUNDUP_CHANNEL_ID, FEED_VOTE_EMOJI


def is_helper():
    """Check that the command invoker has at least one of the configured HELPER_ROLE_IDS."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not HELPER_ROLE_IDS:
            return True  # No restriction configured - allow all
        user_role_ids = {r.id for r in interaction.user.roles}
        return any(rid in user_role_ids for rid in HELPER_ROLE_IDS)
    return app_commands.check(predicate)


class FollowUpCommands(commands.Cog):
    """Slash commands for managing follow-ups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ──────────────────────────────────────────
    # /flag @user - helper flags a user manually
    # ──────────────────────────────────────────
    @app_commands.command(
        name="flag",
        description="Flag a user with 🔴 Awaiting Follow-up (helpers only)."
    )
    @app_commands.describe(
        user="The user to flag",
        message_link="Link to the specific message (paste Discord message link)",
        reason="Brief context (optional)"
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @is_helper()
    async def flag(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        message_link: str = "",
        reason: str = ""
    ):
        """
        Helper manually flags a user. Adds 🔴 role, logs to DB, pings user.
        Use this when you can't react to a specific message (e.g. old threads).
        """
        await interaction.response.defer(ephemeral=True)

        if user.bot:
            await interaction.followup.send("Can't flag a bot.", ephemeral=True)
            return

        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role is None:
            await interaction.followup.send(
                "⚠️ AWAITING_ROLE_ID is not configured correctly in config.py.",
                ephemeral=True
            )
            return

        # Add role if not already set
        if awaiting_role in user.roles:
            await interaction.followup.send(
                f"{user.mention} is already flagged with 🔴 Awaiting Follow-up.",
                ephemeral=True
            )
            return

        try:
            await user.add_roles(awaiting_role, reason=f"Flagged by {interaction.user} via /flag")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to assign roles.", ephemeral=True
            )
            return

        # Validate message_link if provided
        clean_link = message_link.strip() if message_link else ""
        if clean_link and "discord.com/channels/" not in clean_link:
            await interaction.followup.send(
                "⚠️ That doesn't look like a valid Discord message link. "
                "Right-click a message → Copy Message Link.", ephemeral=True
            )
            return

        # Log to DB
        question_snippet = reason[:200] if reason else "(no reason provided)"
        follow_up_id = await db.add_follow_up(
            user_id=str(user.id),
            guild_id=str(interaction.guild_id),
            channel_id=str(interaction.channel_id),
            message_link=clean_link,
            question_snippet=question_snippet
        )
        await db.increment_flag_count(str(user.id), str(interaction.guild_id))
        await db.add_audit_log(
            action="flagged_via_command",
            actor_id=str(interaction.user.id),
            target_id=str(user.id),
            detail=f"Follow-up #{follow_up_id} | link: {clean_link or 'none'} | reason: {question_snippet}"
        )

        await interaction.followup.send(
            f"🔴 {user.mention} has been flagged.", ephemeral=True
        )

        # Ping user in follow-up channel
        followup_channel = interaction.guild.get_channel(FOLLOWUP_CHANNEL_ID)
        if followup_channel:
            reason_text = f"\n> {reason}" if reason else ""
            link_text = f"\n🔗 {clean_link}" if clean_link else ""
            await followup_channel.send(
                f"Hey {user.mention}, a helper has flagged you for a follow-up.{reason_text}{link_text}\n"
                f"Please go back and address it. A helper will remove the flag once they see your response."
            )

    # ──────────────────────────────────────────
    # /unmark @user
    # ──────────────────────────────────────────
    @app_commands.command(
        name="unmark",
        description="Remove the 🔴 Awaiting Follow-up role from a user and mark resolved."
    )
    @app_commands.describe(user="The user to unmark")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @is_helper()
    async def unmark(self, interaction: discord.Interaction, user: discord.Member):
        """
        Helper removes the 🔴 role, marks all open follow-ups as resolved,
        and logs the action to the audit log.
        """
        await interaction.response.defer(ephemeral=True)

        awaiting_role = interaction.guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role is None:
            await interaction.followup.send(
                "⚠️ AWAITING_ROLE_ID is not configured correctly in config.py.",
                ephemeral=True
            )
            return

        # Check the user actually has the role
        if awaiting_role not in user.roles:
            await interaction.followup.send(
                f"{user.mention} doesn't have the 🔴 Awaiting Follow-up role.",
                ephemeral=True
            )
            return

        # Remove the role
        try:
            await user.remove_roles(awaiting_role, reason=f"Unmark by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I don't have permission to remove roles.", ephemeral=True
            )
            return

        # Mark all open follow-ups as resolved in the DB
        await db.resolve_all_open_follow_ups(
            user_id=str(user.id),
            resolver_id=str(interaction.user.id)
        )
        await db.increment_resolved_count(str(user.id))

        # Audit log
        await db.add_audit_log(
            action="unmark",
            actor_id=str(interaction.user.id),
            target_id=str(user.id),
            detail=f"Resolved via /unmark command in guild {interaction.guild_id}"
        )

        await interaction.followup.send(
            f"✅ {user.mention} has been unmarked and all open follow-ups resolved.",
            ephemeral=True
        )

        # Post to follow-up channel as confirmation
        followup_channel = interaction.guild.get_channel(FOLLOWUP_CHANNEL_ID)
        if followup_channel:
            await followup_channel.send(
                f"✅ {user.mention}'s open follow-up(s) have been resolved by "
                f"{interaction.user.mention}."
            )

    # ──────────────────────────────────────────
    # /score @user
    # ──────────────────────────────────────────
    @app_commands.command(
        name="score",
        description="Show a user's follow-up stats (flag count, resolved, open, avg time)."
    )
    @app_commands.describe(user="The user to check")
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def score(self, interaction: discord.Interaction, user: discord.Member):
        """Show flag_count, resolved_count, open_count, avg_resolution_time."""
        await interaction.response.defer(ephemeral=True)

        score_data = await db.get_user_score(str(user.id))
        open_items = await db.get_open_follow_ups(str(user.id))
        avg_hours = await db.get_avg_resolution_time(str(user.id))

        if score_data is None:
            await interaction.followup.send(
                f"No follow-up history found for {user.mention}.",
                ephemeral=True
            )
            return

        avg_str = f"{avg_hours:.1f}h" if avg_hours is not None else "N/A"

        embed = discord.Embed(
            title=f"📊 Follow-up Score - {user.display_name}",
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="🚩 Total Flagged", value=str(score_data["flag_count"]), inline=True)
        embed.add_field(name="✅ Resolved", value=str(score_data["resolved_count"]), inline=True)
        embed.add_field(name="🔴 Currently Open", value=str(len(open_items)), inline=True)
        embed.add_field(name="🚫 Ignored (>7d)", value=str(score_data["ignored_count"]), inline=True)
        embed.add_field(name="⏱ Avg Resolution Time", value=avg_str, inline=True)

        if score_data["last_flagged"]:
            embed.add_field(name="📅 Last Flagged", value=score_data["last_flagged"][:19], inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # /help - command overview
    # ──────────────────────────────────────────
    @app_commands.command(
        name="help",
        description="Show available commands for this bot."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def help(self, interaction: discord.Interaction):
        """Show a clean overview of all commands, split by access level."""
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
                "`/blog suggest <feed url>` - Suggest your blog for the community feed\n"
                "`/blog search <words>` - Search everything the feeds have posted\n"
                "`/help` - This message"
            ),
            inline=False
        )

        embed.add_field(
            name="Community feed",
            value=(
                f"New posts from the community's blogs land in <#{FEED_ROUNDUP_CHANNEL_ID}>, each with a "
                "discussion thread. Vote for your favourites with "
                f"{FEED_VOTE_EMOJI}: every Monday the roundup recaps the week and crowns the 3 most-voted posts."
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

        # Feed management only shows up for the people who can use it.
        if any(r.id == RSS_ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            embed.add_field(
                name="Moderators: feeds",
                value=(
                    "`/feed add <url> [channel] [role] [author]` - Add a feed; its newest post goes out right away\n"
                    "`/feed edit <feed>` - Channel, ping role, author, word filters, summaries; no options shows them\n"
                    "`/feed list` · `/feed test` · `/feed remove`\n"
                    "`/feed activity [mode]` - Who's active; phase in off → watch → warn → enforce\n"
                    "`/feed roundup [action]` - Preview the Monday roundup, post it now, or switch it off\n"
                    f"`/blog suggest` cards land in <#{RSS_AUDIT_CHANNEL_ID}> with Approve/Reject buttons\n"
                    "`/rsswatch` - Members' activity tracking (`/rsswatch help`)"
                ),
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # /stats - server-wide overview
    # ──────────────────────────────────────────
    @app_commands.command(
        name="stats",
        description="Show server-wide follow-up stats and leaderboards."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def stats(self, interaction: discord.Interaction):
        """Server-wide stats: totals, resolution rate, top flagged users, top helpers."""
        await interaction.response.defer(ephemeral=True)

        data = await db.get_server_stats(str(interaction.guild_id))

        total = data["total"]
        resolved_pct = (
            round(data["resolved"] / total * 100) if total > 0 else 0
        )
        avg_str = f"{data['avg_hours']:.1f}h" if data["avg_hours"] is not None else "N/A"

        embed = discord.Embed(
            title="📊 Server Follow-up Stats",
            color=discord.Color.blurple()
        )

        # Overall numbers
        embed.add_field(name="Total Flags", value=str(total), inline=True)
        embed.add_field(name="Open", value=str(data["open"]), inline=True)
        embed.add_field(name="Resolved", value=f"{data['resolved']} ({resolved_pct}%)", inline=True)
        embed.add_field(name="Ignored", value=str(data["ignored"]), inline=True)
        embed.add_field(name="Avg Resolution", value=avg_str, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

        # Top flagged users
        if data["top_flagged"]:
            lines = [
                f"{i+1}. <@{u['user_id']}> - {u['flag_count']} flags"
                for i, u in enumerate(data["top_flagged"])
            ]
            embed.add_field(name="Most Flagged", value="\n".join(lines), inline=False)

        # Top helpers
        if data["top_helpers"]:
            lines = [
                f"{i+1}. <@{h['actor_id']}> - {h['actions']} actions"
                for i, h in enumerate(data["top_helpers"])
            ]
            embed.add_field(name="Most Active Helpers", value="\n".join(lines), inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────
    # /audit
    # ──────────────────────────────────────────
    @app_commands.command(
        name="audit",
        description="Show the last 10 audit log entries (flags, resolves, etc.)."
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    async def audit(self, interaction: discord.Interaction):
        """Display the 10 most recent audit log entries."""
        await interaction.response.defer(ephemeral=True)

        entries = await db.get_recent_audit_log(limit=10)
        if not entries:
            await interaction.followup.send("No audit log entries yet.", ephemeral=True)
            return

        lines = []
        for e in entries:
            ts = e["timestamp"][:19] if e["timestamp"] else "unknown"
            actor = f"<@{e['actor_id']}>" if e["actor_id"] else "system"
            target = f"<@{e['target_id']}>" if e["target_id"] else "-"
            lines.append(f"`{ts}` **{e['action']}** | actor: {actor} | target: {target} | {e['detail'] or ''}")

        embed = discord.Embed(
            title="📋 Audit Log (last 10)",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


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
        import random
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
        import random
        openers = [
            f"The server will now decide the fate of {user.mention}.",
            f"Skippy is calling a vote. {user.mention} stands accused.",
            f"Court is in session. {user.mention} - how do you plead? Actually, it doesn't matter.",
            f"The people have a right to know. Will {user.mention} ghost?",
            f"Skippy has gathered enough evidence. Time to let the server judge {user.mention}.",
        ]
        import time
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
            import asyncio
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

        import asyncio
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
        import random
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
        import random
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
        import time
        closes_at = int(time.time()) + 120
        embed.add_field(name="Voting closes", value=f"<t:{closes_at}:R>", inline=False)
        embed.set_footer(text="Skippy's totally legitimate court | No appeals accepted")
        await interaction.response.send_message(embed=embed)
        msg = await interaction.original_response()
        await msg.add_reaction("🔨")
        await msg.add_reaction("🕊️")

        async def close_trial():
            import asyncio
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

        import asyncio
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
        import random
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
        if interaction.user.id != FollowUpCommands.EFFIE_USER_ID:
            await interaction.response.send_message(
                "😤 This command belongs to Effie. You are not Effie.", ephemeral=True
            )
            return
        import random
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


# ============================================================
# RssWatchCommands cog - /rsswatch command group
# ============================================================

def is_rss_admin():
    """Check that the invoker has the RSS admin role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role_ids = {r.id for r in interaction.user.roles}
        return RSS_ADMIN_ROLE_ID in role_ids
    return app_commands.check(predicate)


class RssWatchCommands(commands.Cog):
    """RSS watchlist commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    rsswatch = app_commands.Group(
        name="rsswatch",
        description="Manage the RSS activity watchlist."
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

        lines = []
        for entry in watchlist:
            uid = entry["user_id"]
            count = await db.get_rss_activity_count(uid, days=30)
            latest = await db.get_latest_rss_message(uid)
            member = interaction.guild.get_member(int(uid))
            name = member.display_name if member else f"‹{uid}›"
            badge = "🟢" if count >= 5 else ("🟡" if count >= 1 else "🔴")
            last_seen = latest["sent_at"][:10] if latest else "never"
            lines.append(f"{badge} **{name}** | Last seen: {last_seen} | Msgs (30d): {count}")

        embed = discord.Embed(
            title="📋 RSS Watchlist",
            description="\n".join(lines),
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

        if user:
            # ── Single-user deep dive ──
            uid = str(user.id)
            count = await db.get_rss_activity_count(uid, days=30)
            activity = await db.get_rss_activity(uid, days=30)
            badge = "🟢" if count >= 5 else ("🟡" if count >= 1 else "🔴")

            embed = discord.Embed(
                title=f"{badge} RSS Report - {user.display_name}",
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

            embed.set_footer(text=f"Requested by {interaction.user.display_name} • {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
            await audit_channel.send(embed=embed)
            await interaction.followup.send(f"✅ Deep-dive report for **{user.display_name}** posted to audit channel.", ephemeral=True)

        else:
            # ── Full server report ──
            watchlist = await db.get_watchlist(str(interaction.guild_id))
            if not watchlist:
                await interaction.followup.send("📄 Watchlist is empty - nothing to report.", ephemeral=True)
                return

            lines = []
            for entry in watchlist:
                uid = entry["user_id"]
                count = await db.get_rss_activity_count(uid, days=30)
                latest = await db.get_latest_rss_message(uid)
                member = interaction.guild.get_member(int(uid))
                name = member.display_name if member else f"‹{uid}›"
                badge = "🟢" if count >= 5 else ("🟡" if count >= 1 else "🔴")
                last_link = f"[latest]({latest['message_link']})" if latest else "no messages"
                last_seen = latest["sent_at"][:10] if latest else "never"
                lines.append(f"{badge} **{name}** | Last seen: {last_seen} | Msgs (30d): {count} | {last_link}")

            header = f"📊 **RSS Watchlist Report** - {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n*Requested by {interaction.user.display_name}*\n\n"
            report = header + "\n".join(lines)

            if len(report) <= 2000:
                await audit_channel.send(report)
            else:
                await audit_channel.send(header.strip())
                chunk = ""
                for line in lines:
                    if len(chunk) + len(line) + 1 > 1900:
                        await audit_channel.send(chunk)
                        chunk = ""
                    chunk += line + "\n"
                if chunk:
                    await audit_channel.send(chunk)

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


async def setup(bot: commands.Bot):
    """Called by bot.py to load this cog."""
    await bot.add_cog(FollowUpCommands(bot))
    await bot.add_cog(RssWatchCommands(bot))
