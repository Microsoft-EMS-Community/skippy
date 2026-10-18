# ============================================================
# config.py - Bot configuration
# Fill in all placeholder values before running the bot.
# ============================================================

import os

# Your bot token from https://discord.com/developers/applications.
# Set via DISCORD_BOT_TOKEN env var (see secrets.env, loaded by
# followup-bot.service's EnvironmentFile) - never hardcode it here.
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

# The guild (server) ID where this bot operates.
# Enable Developer Mode (Settings → Advanced) and right-click the server icon.
GUILD_ID = 1008077287813550090

# Role IDs - enable Developer Mode, go to Server Settings → Roles,
# right-click a role and select "Copy Role ID"
AWAITING_ROLE_ID = 1473695101996761200   # 🔴 "Awaiting Follow-up" role
FOLLOWSUP_ROLE_ID = 1473694869863010407  # ✅ "Follows Up" role (optional, for tracking)

# Channel IDs - enable Developer Mode, right-click a channel → "Copy Channel ID"
FOLLOWUP_CHANNEL_ID = 1473843480278339656   # Where follow-up pings are posted to users
HELPER_LOG_CHANNEL_ID = 1473843359930908693 # Where helper/mod audit logs are posted

# Helper roles - only members with one of these roles can flag users (via reaction or /flag)
HELPER_ROLE_IDS = [
    1473843182830878780,  # Original helper role
    1008083497291874304,
    1424794853052514356,
]

# Emoji helpers react with to flag a user (on the question message itself)
# Default: 🔴 (standard Unicode). If using a custom server emoji, use its name as a string.
FLAG_EMOJI = "🔴"

# Path to the SQLite database file. Next to this file, so moving the bot's
# folder can't silently point it at a fresh empty database.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "followup.db")

# RSS Watchlist feature
RSS_ADMIN_ROLE_ID = 1008083497291874304   # Role that can use /rsswatch commands
RSS_AUDIT_CHANNEL_ID = 1438251870106947806  # Channel where RSS reports are posted

# New Account heuristic tagging
NEW_ACCOUNT_ROLE_ID = 1534323968591532083   # 🆕 auto-assigned on join if heuristics trip
NEW_ACCOUNT_AGE_DAYS = 7                    # Account younger than this trips the heuristic
