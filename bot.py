# ============================================================
# bot.py - Main entry point for the Follow-Up Bot
#
# Features:
#   • Detects when 🔴 Awaiting Follow-up role is added/removed
#   • Notifies users with open follow-ups when they post new messages
#   • Slash commands: /unmark /score /audit
#   • Background 24h warning task (48h warn, 7d mark ignored)
# ============================================================

import discord
from discord.ext import commands
import logging
import logging.handlers
import asyncio
import os

import db
import views as v
import followup_notify
import link_cleaner
from config import (
    BOT_TOKEN,
    GUILD_ID,
    AWAITING_ROLE_ID,
    HELPER_LOG_CHANNEL_ID,
    HELPER_ROLE_IDS,
    FLAG_EMOJI,
    RSS_AUDIT_CHANNEL_ID,
    NEW_ACCOUNT_ROLE_ID,
    NEW_ACCOUNT_AGE_DAYS,
    LOG_DIR,
    LOG_DAYS,
)
from datetime import datetime, timezone, timedelta


def _defang(url: str) -> str:
    """Neutralize a URL so it can be posted in a public channel without
    Discord embedding/rendering it as a live clickable link."""
    return url.replace("://", "[://]").replace(".", "[.]")


# In-memory cooldown tracker: user_id → last nudge datetime (UTC)
# Resets on bot restart - that's fine, DB is the source of truth.
NUDGE_COOLDOWN_HOURS = 24
nudge_cooldowns: dict[str, datetime] = {}

# In-memory join-burst tracker: rolling list of join timestamps.
# Resets on bot restart - fine, this is a live-detection signal, not historical record.
JOIN_BURST_WINDOW_MINUTES = 10
JOIN_BURST_THRESHOLD = 3  # N+ joins within the window trips the alert
recent_joins: list[datetime] = []

# In-memory cooldown tracker for new-account verification nudges - same pattern as nudge_cooldowns.
NEW_ACCOUNT_NUDGE_COOLDOWN_HOURS = 24
new_account_nudge_cooldowns: dict[str, datetime] = {}

# Tracks the public "check your DMs" notice per user, so it can be updated once they verify.
# Resets on bot restart - if it goes stale, the note just stays unedited, no functional harm.
pending_verification_notices: dict[str, discord.Message] = {}

# Tracks when the verify prompt was sent per user, so we can flag suspiciously fast clicks
# (a real person needs to read the message and move a mouse; sub-second clicks smell like a script).
VERIFY_CLICK_SUSPICIOUS_SECONDS = 1.5
verify_prompt_sent_at: dict[str, datetime] = {}

# One evolving audit card per user through the new-account lifecycle (tagged -> verification
# requested -> verified), instead of 3 separate embeds piling up in the audit channel.
# Resets on bot restart - worst case a restart mid-lifecycle starts a fresh card, no functional harm.
account_audit_cards: dict[str, dict] = {}  # user_id -> {"message": discord.Message, "history": [str, ...]}

# How long Discord's crawler can take to attach a video embed after the message
# lands - long enough that a single suppress() right after on_message can miss it.
EMBED_RETRY_DELAY_SECONDS = 4


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


async def post_account_lifecycle_card(guild: discord.Guild, member: discord.Member, stage_desc: str,
                                       color: discord.Color, final: bool = False):
    """
    Post/refresh the single audit-channel card tracking a user's new-account lifecycle.
    Deletes the previous card and reposts a consolidated one (bumps to bottom of channel = still
    reads as "new"), collapsing tag/request/verify into one card instead of three.
    """
    audit_channel = guild.get_channel(RSS_AUDIT_CHANNEL_ID)
    if not audit_channel:
        return

    key = str(member.id)
    entry = account_audit_cards.setdefault(key, {"message": None, "history": []})
    entry["history"].append(f"<t:{int(datetime.now(timezone.utc).timestamp())}:R> — {stage_desc}")

    embed = discord.Embed(
        title=f"\U0001f195 Account Lifecycle: {member}",
        description="\n".join(entry["history"]),
        color=color,
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)

    old_message = entry.get("message")
    if old_message is not None:
        try:
            await old_message.delete()
        except (discord.NotFound, discord.HTTPException):
            pass

    entry["message"] = await audit_channel.send(embed=embed)

    if final:
        # Lifecycle complete - stop tracking so a future re-flag starts a clean new card.
        account_audit_cards.pop(key, None)


async def make_embed(
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


# Keep a plain score line for cases where we need text only (e.g. member_remove before member object is gone)
async def score_line(user_id: str) -> str:
    score = await db.get_user_score(user_id)
    open_items = await db.get_open_follow_ups(user_id)
    if score is None:
        return "No history"
    return (
        f"🚩{score['flag_count']} flagged | "
        f"✅{score['resolved_count']} resolved | "
        f"🚫{score['ignored_count']} ignored | "
        f"🔴{len(open_items)} open"
    )

# ──────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),  # the service's journal
        # skippy.log, plus one file per earlier day; older ones are deleted
        logging.handlers.TimedRotatingFileHandler(os.path.join(LOG_DIR, "skippy.log"), when="midnight",
                                                  backupCount=LOG_DAYS, encoding="utf-8"),
    ],
)
logger = logging.getLogger("followup-bot")

# ──────────────────────────────────────────────
# Bot setup
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True          # Required for on_member_update
intents.message_content = True  # Required for reading message content
intents.reactions = True        # Required for on_raw_reaction_add

bot = commands.Bot(command_prefix="!", intents=intents)


# ──────────────────────────────────────────────
# on_ready
# ──────────────────────────────────────────────
@bot.event
async def on_error(event_method, *args, **kwargs):
    """Global safety net: an uncaught exception in any event handler lands here
    instead of only hitting stderr, so it's logged and visible in Discord too."""
    import traceback
    tb = traceback.format_exc()
    logger.error(f"Unhandled exception in {event_method}:\n{tb}")

    try:
        channel = bot.get_channel(HELPER_LOG_CHANNEL_ID)
        if channel:
            snippet = tb.strip().splitlines()[-1][:500]
            await channel.send(f"⚠️ Bugged out in `{event_method}`: `{snippet}`")
    except Exception:
        pass  # Never let the error handler itself take the bot down


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Serving guild ID: {GUILD_ID}")

    # Sync slash commands to the guild (guild-scoped = instant; global takes ~1h)
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    logger.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}.")


# ──────────────────────────────────────────────
# on_member_update - role add/remove detection
# ──────────────────────────────────────────────
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """
    Detect when the 🔴 Awaiting Follow-up role is removed.
    Role additions are handled by on_raw_reaction_add and /flag - not here.
    This only handles manual role removal (e.g. via server settings).
    """
    before_role_ids = {r.id for r in before.roles}
    after_role_ids = {r.id for r in after.roles}

    role_removed = AWAITING_ROLE_ID in before_role_ids and AWAITING_ROLE_ID not in after_role_ids

    # ── Role Removed → Resolve follow-up ──
    if role_removed:
        logger.info(f"✅ Awaiting role removed from {after} ({after.id})")

        # We don't know who removed it (would need Audit Log API); mark resolver as "unknown".
        # /unmark command sets a proper resolver_id.
        open_items = await db.get_open_follow_ups(str(after.id))
        if open_items:
            await db.resolve_all_open_follow_ups(
                user_id=str(after.id),
                resolver_id="unknown"  # /unmark will have already set this properly
            )
            await db.increment_resolved_count(str(after.id))
            await db.add_audit_log(
                action="role_removed",
                actor_id=None,
                target_id=str(after.id),
                detail=f"Resolved {len(open_items)} open follow-up(s) on role removal"
            )
            logger.info(f"Resolved {len(open_items)} open follow-up(s) for {after.id}")


# ──────────────────────────────────────────────
# on_raw_reaction_add - helper reacts 🔴 to flag a user
# ──────────────────────────────────────────────
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """
    When a helper reacts with FLAG_EMOJI (🔴) to a message:
      1. Verify the reactor has the HELPER_ROLE_ID
      2. Add the 🔴 Awaiting Follow-up role to the message author
      3. Log to DB with the exact message as context (no searching needed)
      4. Reply on the flagged message to tell the user (DM if that fails)
    """
    # Ignore DMs and bot reactions
    if payload.guild_id is None:
        return

    emoji_name = str(payload.emoji)
    if emoji_name != FLAG_EMOJI:
        return

    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return

    # Verify the reactor is a helper
    reactor = guild.get_member(payload.user_id)
    if reactor is None or reactor.bot:
        return

    reactor_role_ids = {r.id for r in reactor.roles}
    if HELPER_ROLE_IDS and not any(rid in reactor_role_ids for rid in HELPER_ROLE_IDS):
        return  # Not a helper - ignore

    # Fetch the message to get author + content
    # Try regular channel first, then thread (covers forum posts and thread channels)
    channel = guild.get_channel(payload.channel_id) or guild.get_thread(payload.channel_id)
    if channel is None:
        # Last resort: fetch directly from API (handles uncached forum threads)
        try:
            channel = await guild.fetch_channel(payload.channel_id)
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            logger.warning(f"Could not resolve channel/thread {payload.channel_id} - missing access?")
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except (discord.NotFound, discord.HTTPException):
        logger.warning(f"Could not fetch message {payload.message_id} for reaction flag")
        return

    target = message.author
    if target.bot:
        return  # Don't flag bots

    target_member = guild.get_member(target.id)
    if target_member is None:
        return

    # Check if already flagged (avoid double-logging)
    open_items = await db.get_open_follow_ups(str(target_member.id))
    already_flagged = any(item for item in open_items)

    # Add the 🔴 role (on_member_update will also fire, but we handle logging here
    # and skip it there when we detect the flag was set via reaction)
    awaiting_role = guild.get_role(AWAITING_ROLE_ID)
    if awaiting_role is None:
        logger.error(f"AWAITING_ROLE_ID ({AWAITING_ROLE_ID}) not found in guild.")
        return

    if awaiting_role not in target_member.roles:
        try:
            await target_member.add_roles(awaiting_role, reason=f"Flagged by {reactor} via reaction")
        except discord.Forbidden:
            logger.error("Missing permission to assign roles.")
            return

    question_snippet = message.content[:200] if message.content else "(no text)"
    message_link = message.jump_url

    # Skip if this exact message is already an open follow-up (duplicate reaction)
    duplicate = any(
        fu.get("message_link", "").endswith(f"/{payload.message_id}")
        for fu in open_items
    )
    if duplicate:
        logger.info(f"Duplicate reaction on already-tracked message {payload.message_id} - skipping")
        return

    # Always log to DB - every new flagged message gets its own record
    follow_up_id = await db.add_follow_up(
        user_id=str(target_member.id),
        guild_id=str(guild.id),
        channel_id=str(channel.id),
        message_link=message_link,
        question_snippet=question_snippet
    )
    await db.increment_flag_count(str(target_member.id), str(guild.id))
    await db.add_audit_log(
        action="flagged_via_reaction",
        actor_id=str(reactor.id),
        target_id=str(target_member.id),
        detail=f"Follow-up #{follow_up_id} | msg: {message_link}"
    )

    logger.info(
        f"🔴 {target_member} flagged by {reactor} via reaction on msg {payload.message_id}"
    )

    helper_log = guild.get_channel(HELPER_LOG_CHANNEL_ID)

    if not already_flagged:
        # First flag - tell the user, right on the flagged message
        await followup_notify.notify_member(
            guild, target_member, message_link,
            reply_text=(
                f"Hey {target_member.mention}, a helper has flagged this message for follow-up. "
                f"Please **reply to this message** to close it; a helper will remove the flag "
                f"once they see your response."
            ),
            dm_text=(
                f"Hey! A helper in {guild.name} has flagged one of your messages for follow-up:\n"
                f"🔗 {message_link}\nPlease **reply directly to that message** to close it."
            ),
            message=message,
        )
        if helper_log:
            embed = await make_embed(
                title="🔴 Flagged",
                color=discord.Color.red(),
                user=target_member,
                description=f"Flagged by {reactor.mention} in {channel.mention}",
                link=message_link,
            )
            await helper_log.send(embed=embed, view=v.FlaggedView(str(target_member.id)))
    else:
        # Already flagged - add to the pile silently, notify helpers only
        open_count = len(open_items) + 1
        if helper_log:
            embed = await make_embed(
                title="🔴 Follow-up Added",
                color=discord.Color.orange(),
                user=target_member,
                description=f"Flagged again by {reactor.mention} in {channel.mention} - now **{open_count}** open. User not re-pinged.",
                link=message_link,
            )
            await helper_log.send(embed=embed, view=v.FlaggedView(str(target_member.id)))


# ──────────────────────────────────────────────
# on_message - detect new messages from flagged users
# ──────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    """
    When a user with 🔴 Awaiting Follow-up posts a new message,
    warn the helper log channel and DM/ping the user.
    """
    # Ignore bot messages and DMs
    if message.author.bot or message.guild is None:
        return

    # ── RSS Watchlist activity tracking (runs for all non-bot messages) ──
    if await db.is_watched(str(message.author.id)):
        jump_url = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"
        await db.log_rss_activity(
            user_id=str(message.author.id),
            guild_id=str(message.guild.id),
            channel_id=str(message.channel.id),
            message_id=str(message.id),
            message_link=jump_url,
        )

    # ── Tracking-param stripper ──
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

        audit_channel = message.guild.get_channel(RSS_AUDIT_CHANNEL_ID) if message.guild else None
        if audit_channel:
            embed = await make_embed(
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

    # Allow prefix commands to still work
    await bot.process_commands(message)

    # ── @mention → Skippy responds ──
    if bot.user in message.mentions and not message.mention_everyone:
        member = message.guild.get_member(message.author.id)
        member_role_ids = {r.id for r in member.roles} if member else set()
        is_helper = HELPER_ROLE_IDS and any(rid in member_role_ids for rid in HELPER_ROLE_IDS)

        # Check if another user is mentioned alongside the bot - skeptical callout mode
        other_mentions = [u for u in message.mentions if u.id != bot.user.id and not u.bot]
        if other_mentions:
            import random
            target = other_mentions[0]
            callouts = [
                f"👀 Oh, {target.mention}? Yeah. I'm watching.",
                f"🤨 {target.mention} has been noted. Whether they know it or not.",
                f"👻 {target.mention} - your friends are skeptical. That's never a great sign.",
                f"📎 Interesting that you're pointing at {target.mention}. I'll keep that in mind.",
                f"🔴 You want me to watch {target.mention}? Consider it done.",
                f"😐 {target.mention}. Noted. I don't forget names.",
                f"👀 {target.mention} just got put on my radar. Hope they follow up on things.",
                f"📋 {target.mention} added to the watchlist. Unofficially. For now.",
                f"🤖 Activating skepticism mode for {target.mention}. Thanks for the tip.",
                f"😶 {target.mention}. I see them. The question is - do they see me?",
            ]
            await message.reply(random.choice(callouts))
            return

        # Check if the message is asking what the bot does / why it exists
        msg_lower = message.content.lower()
        intent_keywords = [
            "why do you exist", "why exist", "what do you do", "what are you",
            "what is your purpose", "what's your purpose", "whats your purpose",
            "why are you here", "how do you work", "how does this work",
            "explain yourself", "what can you do", "purpose", "who are you",
            "tell me about yourself", "what is this", "whats this", "what's this",
        ]
        asking_purpose = any(kw in msg_lower for kw in intent_keywords)

        if asking_purpose:
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
            # Short skeptical response for regular users
            import random
            responses = [
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
            await message.reply(random.choice(responses))
        return

    # Check if the author has the awaiting role
    member = message.guild.get_member(message.author.id)
    if member is None:
        return

    # ── New Account verification nudge ──
    has_new_account_role = any(r.id == NEW_ACCOUNT_ROLE_ID for r in member.roles)
    if has_new_account_role:
        now = datetime.now(timezone.utc)
        last_nudge = new_account_nudge_cooldowns.get(str(member.id))
        on_cooldown = (
            last_nudge is not None and
            (now - last_nudge).total_seconds() < NEW_ACCOUNT_NUDGE_COOLDOWN_HOURS * 3600
        )
        if not on_cooldown:
            dm_sent = False
            try:
                await member.send(
                    f"👋 You're flagged as a possibly new/unverified account. "
                    f"Click below to confirm you're human.",
                    view=v.VerifyHumanView(str(member.id)),
                )
                dm_sent = True
            except (discord.Forbidden, discord.HTTPException):
                dm_sent = False

            verify_prompt_sent_at[str(member.id)] = datetime.now(timezone.utc)

            if dm_sent:
                notice = await message.channel.send(
                    f"🆕 {member.mention} has been asked to verify - check your DMs."
                )
                pending_verification_notices[str(member.id)] = notice
            else:
                await message.reply(
                    f"👋 {member.mention}, you're flagged as a possibly new/unverified account. "
                    f"Click below to confirm you're human.",
                    view=v.VerifyHumanView(str(member.id)),
                )

            await db.add_audit_log(
                action="verification_requested",
                actor_id=None,
                target_id=str(member.id),
                detail=f"Asked to verify via {'DM' if dm_sent else 'public channel'} in {message.channel.name}"
            )

            await post_account_lifecycle_card(
                message.guild, member,
                stage_desc=f"📨 **Verification requested** via {'DM' if dm_sent else 'public channel'} in {message.channel.mention}",
                color=discord.Color.gold(),
            )

            new_account_nudge_cooldowns[str(member.id)] = now

    has_awaiting_role = any(r.id == AWAITING_ROLE_ID for r in member.roles)
    if not has_awaiting_role:
        return

    # Fetch their open follow-ups
    open_items = await db.get_open_follow_ups(str(member.id))
    if not open_items:
        return

    # ── Reply detection ──
    # If the user replied directly to one of their flagged messages, notify helpers.
    # Extract message ID from stored link (format: .../channels/guild/channel/MESSAGE_ID)
    if message.reference and message.reference.message_id:
        ref_id = str(message.reference.message_id)
        matched = [
            fu for fu in open_items
            if fu.get("message_link", "").endswith(f"/{ref_id}")
        ]
        if matched:
            helper_log = message.guild.get_channel(HELPER_LOG_CHANNEL_ID)
            if helper_log:
                fu = matched[0]
                embed = await make_embed(
                    title="💬 Replied to Flagged Message",
                    color=discord.Color.green(),
                    user=member,
                    description=f"Replied in {message.channel.mention}.",
                    extra_fields=[
                        ("Original", f"[Jump]({fu['message_link']})" if fu['message_link'] else "N/A", True),
                        ("Reply", f"[Jump]({message.jump_url})", True),
                    ],
                )
                await helper_log.send(embed=embed, view=v.RepliedView(str(member.id)))
            return  # Don't nudge them for replying - that's the right behaviour

    count = len(open_items)

    def to_unix(ts: str) -> int:
        """Convert SQLite timestamp string to Unix int for Discord relative format."""
        try:
            return int(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            return 0

    link_list = "\n".join(
        f"  • {fu['message_link'] or 'N/A'} (<t:{to_unix(fu['flagged_at'])}:R>)"
        for fu in open_items
    )

    # Check nudge cooldown
    now = datetime.now(timezone.utc)
    last_nudge = nudge_cooldowns.get(str(member.id))
    on_cooldown = (
        last_nudge is not None and
        (now - last_nudge).total_seconds() < NUDGE_COOLDOWN_HOURS * 3600
    )

    helper_log = message.guild.get_channel(HELPER_LOG_CHANNEL_ID)

    if on_cooldown:
        # Cooldown active - silent info to helper log only, no public ping
        seconds_remaining = (NUDGE_COOLDOWN_HOURS * 3600) - (now - last_nudge).total_seconds()
        hours_left = int(seconds_remaining // 3600)
        mins_left = int((seconds_remaining % 3600) // 60)
        if helper_log:
            embed = await make_embed(
                title="🔕 Nudge Suppressed (cooldown)",
                color=discord.Color.greyple(),
                user=member,
                description=f"Posted in {message.channel.mention} - not nudged. Next nudge in **{hours_left}h {mins_left}m**.",
            )
            await helper_log.send(embed=embed, view=v.FlaggedView(str(member.id)))
    else:
        # Ping publicly in the channel where they posted - visible to everyone
        await message.channel.send(
            f"⚠️ {member.mention} - you have **{count}** open follow-up(s).\n"
            f"You have messages waiting for a response - please address them first:\n{link_list}"
        )

        # Audit to helper log as embed
        if helper_log:
            embed = await make_embed(
                title="📋 Nudged Publicly",
                color=discord.Color.from_rgb(243, 156, 18),
                user=member,
                description=f"Posted in {message.channel.mention} - nudged publicly with {count} open follow-up(s).",
            )
            await helper_log.send(embed=embed, view=v.FlaggedView(str(member.id)))

        # Record nudge time
        nudge_cooldowns[str(member.id)] = now


# ──────────────────────────────────────────────
# on_member_remove - user left/kicked/banned while flagged
# ──────────────────────────────────────────────
@bot.event
async def on_member_remove(member: discord.Member):
    """
    When a member leaves (or is kicked/banned), check for open follow-ups.
    Mark them as 'left_server' and alert the helper log.
    """
    open_items = await db.get_open_follow_ups(str(member.id))
    if not open_items:
        return

    count = len(open_items)
    await db.mark_left_server(str(member.id))
    await db.add_audit_log(
        action="left_server",
        actor_id=None,
        target_id=str(member.id),
        detail=f"Left with {count} open follow-up(s)"
    )

    helper_log = member.guild.get_channel(HELPER_LOG_CHANNEL_ID)
    if helper_log:
        links = "\n".join(fu['message_link'] for fu in open_items if fu.get('message_link'))
        embed = await make_embed(
            title="🚪 Left Server with Open Follow-ups",
            color=discord.Color.dark_red(),
            user=member,
            description=f"Left with **{count}** unresolved follow-up(s).\n{links}",
        )
        await helper_log.send(embed=embed)

    logger.info(f"🚪 {member} left with {count} open follow-up(s) - marked as left_server")


# ──────────────────────────────────────────────
# on_member_join - user rejoins with prior follow-ups
# ──────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    """
    When a member joins:
      1. Heuristic-tag likely new/throwaway accounts (age, default avatar).
      2. If they rejoined with unresolved follow-ups, alert helpers.
    """
    # ── Join-burst detection ──
    now = datetime.now(timezone.utc)
    recent_joins.append(now)
    cutoff = now - timedelta(minutes=JOIN_BURST_WINDOW_MINUTES)
    while recent_joins and recent_joins[0] < cutoff:
        recent_joins.pop(0)

    if len(recent_joins) >= JOIN_BURST_THRESHOLD:
        await db.add_audit_log(
            action="join_burst_detected",
            actor_id=None,
            target_id=str(member.id),
            detail=f"{len(recent_joins)} joins within {JOIN_BURST_WINDOW_MINUTES}m (latest: {member})"
        )
        audit_channel = member.guild.get_channel(RSS_AUDIT_CHANNEL_ID)
        if audit_channel:
            embed = discord.Embed(
                title="🚨 Join Burst Detected",
                description=(
                    f"**{len(recent_joins)}** members joined within the last "
                    f"{JOIN_BURST_WINDOW_MINUTES} minutes.\nLatest: {member.mention}"
                ),
                color=discord.Color.red(),
            )
            await audit_channel.send(embed=embed)
        logger.warning(f"🚨 Join burst: {len(recent_joins)} joins in {JOIN_BURST_WINDOW_MINUTES}m")

    account_age = now - member.created_at
    is_young = account_age.days < NEW_ACCOUNT_AGE_DAYS
    is_default_avatar = member.avatar is None

    if is_young or is_default_avatar:
        reasons = []
        if is_young:
            reasons.append(f"account age {account_age.days}d (< {NEW_ACCOUNT_AGE_DAYS}d)")
        if is_default_avatar:
            reasons.append("default avatar")

        new_account_role = member.guild.get_role(NEW_ACCOUNT_ROLE_ID)
        if new_account_role:
            try:
                await member.add_roles(new_account_role, reason=f"Heuristic tag: {', '.join(reasons)}")
            except discord.Forbidden:
                logger.error("Missing permission to assign New Account role.")

            if "[unverified]" not in member.display_name:
                try:
                    await member.edit(
                        nick=f"{member.display_name} [unverified]"[:32],
                        reason="Tagged as new/unverified account",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.error("Missing permission to set nickname for new account tag.")

        await db.add_audit_log(
            action="new_account_tagged",
            actor_id=None,
            target_id=str(member.id),
            detail=", ".join(reasons)
        )

        await post_account_lifecycle_card(
            member.guild, member,
            stage_desc=f"🆕 **Tagged** on join — {', '.join(reasons)} (created <t:{int(member.created_at.timestamp())}:R>)",
            color=discord.Color.gold(),
        )

        logger.info(f"🆕 {member} tagged as new account: {', '.join(reasons)}")

    left_items = await db.get_left_server_follow_ups(str(member.id))
    if not left_items:
        return

    count = len(left_items)
    await db.add_audit_log(
        action="rejoined",
        actor_id=None,
        target_id=str(member.id),
        detail=f"Rejoined with {count} unresolved follow-up(s) from before leaving"
    )

    helper_log = member.guild.get_channel(HELPER_LOG_CHANNEL_ID)
    if helper_log:
        links = "\n".join(fu['message_link'] for fu in left_items if fu.get('message_link'))
        embed = await make_embed(
            title="👀 Rejoined - Prior Follow-ups",
            color=discord.Color.blue(),
            user=member,
            description=f"Back with **{count}** unresolved follow-up(s) from before leaving. Use `/flag @{member.name}` to re-flag if needed.\n{links}",
        )
        await helper_log.send(embed=embed)

    logger.info(f"👀 {member} rejoined with {count} prior unresolved follow-up(s)")


# ──────────────────────────────────────────────
# on_interaction - handle button clicks from helper log embeds
# ──────────────────────────────────────────────
@bot.event
async def on_interaction(interaction: discord.Interaction):
    """
    Handle button interactions from helper log embeds.
    custom_id format: followup:ACTION:USER_ID
    """
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")
    if not custom_id.startswith("followup:"):
        return

    parts = custom_id.split(":")
    if len(parts) != 3:
        return

    _, action, user_id = parts

    if action == "verify_honeypot":
        # No real reason for a human reading the prompt to click this - only a script
        # blindly clicking buttons would. Don't tip it off: respond exactly like the
        # real button would, but flag quietly instead of verifying anyone.
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message(
                "This verification isn't for you.", ephemeral=True
            )
            return

        await interaction.response.send_message("✅ You're verified. Welcome!", ephemeral=True)

        await db.add_audit_log(
            action="verify_honeypot_triggered",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail="Clicked decoy button on verify prompt - likely automated"
        )

        guild = interaction.guild or bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        logger.warning(f"⚠️ {member or user_id} clicked the verify honeypot button - likely a bot")

        # Same cosmetic "verified" strikethrough as the real button, so the clicker
        # sees nothing different - the honeypot is only visible in the audit card.
        try:
            await interaction.message.edit(
                content=f"~~{interaction.message.content or ''}~~ **✅ Verified**",
                view=None
            )
        except Exception:
            pass

        notice = pending_verification_notices.pop(user_id, None)
        if notice is not None:
            try:
                await notice.edit(
                    content=f"~~{notice.content or ''}~~ **✅ Verified**"
                )
            except Exception:
                pass

        if guild and member:
            await post_account_lifecycle_card(
                guild, member,
                stage_desc="🍯 **Honeypot tripped** - clicked decoy button, likely a bot. **Not** auto-verified, review manually.",
                color=discord.Color.red(),
                final=False,
            )
        return

    if action == "verify_human":
        if str(interaction.user.id) != user_id:
            await interaction.response.send_message(
                "This verification isn't for you.", ephemeral=True
            )
            return

        guild = interaction.guild or bot.get_guild(GUILD_ID)
        member = guild.get_member(int(user_id)) if guild else None
        if member is None:
            await interaction.response.send_message("Couldn't find you in the server.", ephemeral=True)
            return

        prompt_sent_at = verify_prompt_sent_at.pop(user_id, None)
        click_seconds = (
            (datetime.now(timezone.utc) - prompt_sent_at).total_seconds()
            if prompt_sent_at else None
        )
        suspiciously_fast = click_seconds is not None and click_seconds < VERIFY_CLICK_SUSPICIOUS_SECONDS

        # A sub-1.5s click can't be an accident like a stray honeypot press - nobody reads
        # and decides that fast. Treat it at least as seriously: don't auto-clear the role,
        # route to manual review instead.
        if not suspiciously_fast:
            new_account_role = guild.get_role(NEW_ACCOUNT_ROLE_ID)
            if new_account_role and new_account_role in member.roles:
                await member.remove_roles(new_account_role, reason="Self-verified as human via button")

            if "[unverified]" in member.display_name:
                try:
                    await member.edit(
                        nick=member.display_name.replace(" [unverified]", "").replace("[unverified]", "").strip() or None,
                        reason="Self-verified as human via button",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.error("Missing permission to clear nickname tag after verification.")

        detail = "Verified via 'I'm Human' button"
        if click_seconds is not None:
            detail += f" ({click_seconds:.2f}s after prompt)"
        if suspiciously_fast:
            detail += " ⚠️ suspiciously fast click - not auto-verified, held for manual review"

        await db.add_audit_log(
            action="self_verified_human" if not suspiciously_fast else "verify_click_too_fast",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=detail
        )

        if suspiciously_fast:
            logger.warning(
                f"⚠️ {member} clicked verify in {click_seconds:.2f}s - held for manual review, possible bot"
            )

        # Same cosmetic "verified" response either way - the real signal only shows in the audit card.
        await interaction.response.send_message("✅ You're verified. Welcome!", ephemeral=True)

        try:
            await interaction.message.edit(
                content=f"~~{interaction.message.content or ''}~~ **✅ Verified**",
                view=None
            )
        except Exception:
            pass

        notice = pending_verification_notices.pop(user_id, None)
        if notice is not None:
            try:
                await notice.edit(
                    content=f"~~{notice.content or ''}~~ **✅ Verified**"
                )
            except Exception:
                pass

        if suspiciously_fast:
            stage_desc = (
                f"⚠️ **Verify click too fast** ({click_seconds:.2f}s) - "
                f"likely a bot, **not** auto-verified, review manually."
            )
        else:
            stage_desc = "✅ **Verified** as human via button"

        await post_account_lifecycle_card(
            guild, member,
            stage_desc=stage_desc,
            color=discord.Color.red() if suspiciously_fast else discord.Color.green(),
            final=not suspiciously_fast,
        )

        logger.info(f"✅ {member} self-verified as human via button")
        return

    # Verify the clicker is a helper
    clicker_role_ids = {r.id for r in interaction.user.roles}
    if HELPER_ROLE_IDS and not any(rid in clicker_role_ids for rid in HELPER_ROLE_IDS):
        await interaction.response.send_message(
            "You need a helper role to use this.", ephemeral=True
        )
        return

    guild = interaction.guild
    member = guild.get_member(int(user_id))

    if action == "unmark":
        await interaction.response.defer(ephemeral=True)

        if member is None:
            await interaction.followup.send("User not found in server.", ephemeral=True)
            return

        awaiting_role = guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role and awaiting_role in member.roles:
            await member.remove_roles(awaiting_role, reason=f"Unmarked via button by {interaction.user}")

        await db.resolve_all_open_follow_ups(str(user_id), resolver_id=str(interaction.user.id))
        await db.increment_resolved_count(str(user_id))
        await db.add_audit_log(
            action="unmark_button",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=f"Resolved via button click in helper log"
        )

        # Update the original message to show it's been resolved
        try:
            await interaction.message.edit(
                content=f"~~{interaction.message.content or ''}~~ **Resolved by {interaction.user.mention}**",
                view=None  # Remove buttons
            )
        except Exception:
            pass

        await interaction.followup.send(
            f"✅ {member.mention} unmarked and all follow-ups resolved.", ephemeral=True
        )
        logger.info(f"✅ {member} unmarked via button by {interaction.user}")

    elif action == "reflag":
        await interaction.response.defer(ephemeral=True)

        if member is None:
            await interaction.followup.send("User not found in server.", ephemeral=True)
            return

        awaiting_role = guild.get_role(AWAITING_ROLE_ID)
        if awaiting_role and awaiting_role not in member.roles:
            await member.add_roles(awaiting_role, reason=f"Re-flagged via button by {interaction.user}")

        await db.add_audit_log(
            action="reflag_button",
            actor_id=str(interaction.user.id),
            target_id=user_id,
            detail=f"Re-flagged via button click - reply deemed insufficient"
        )

        try:
            await interaction.message.edit(view=None)  # Remove buttons after action
        except Exception:
            pass

        await interaction.followup.send(
            f"🔴 {member.mention} re-flagged. Use 🔴 reaction on a specific message to add context.",
            ephemeral=True
        )
        logger.info(f"🔴 {member} re-flagged via button by {interaction.user}")


# ──────────────────────────────────────────────
# Load cogs and run
# ──────────────────────────────────────────────
async def main():
    async with bot:
        # Initialize DB before anything else - ensures tables exist before
        # the background task fires on bot ready.
        await db.init_db()
        logger.info("Database initialized.")

        await bot.load_extension("commands")  # Slash commands
        await bot.load_extension("tasks")     # Background warning task
        await bot.load_extension("feeds")     # /feed commands + RSS/Atom poller
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
