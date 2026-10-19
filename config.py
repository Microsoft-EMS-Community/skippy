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

# Path to the SQLite database file. The server sets SKIPPY_DB_PATH (see
# deploy/skippy.service) to keep data out of the code checkout; locally it
# defaults to next to this file, so moving the folder can't silently point
# the bot at a fresh empty database.
DB_PATH = os.environ.get("SKIPPY_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "followup.db"
)

# Log files, next to the database (on the server: /var/lib/skippy/logs). A new
# file starts every midnight and files older than LOG_DAYS are deleted, so the
# logs never grow past about a week.
LOG_DIR = os.path.join(os.path.dirname(DB_PATH), "logs")
LOG_DAYS = 7

# RSS Watchlist feature
RSS_ADMIN_ROLE_ID = 1008083497291874304   # Role that can use /rsswatch commands
RSS_AUDIT_CHANNEL_ID = 1438251870106947806  # Channel where RSS reports are posted

# RSS/Atom feed posting (/feed commands, managed by RSS_ADMIN_ROLE_ID)
# The only channels feeds may post in: /feed add offers exactly these.
# The first one is the default.
FEED_CHANNELS = {
    "community-feed": 1010964682741387354,
    "ms-blogs": 1010929103978897448,
    "ms-status": 1105424571798126652,
    "cve": 1312419690802450453,
}
FEED_POLL_MINUTES = 5                         # How often every feed is checked
FEED_ALERT_CHANNEL_ID = RSS_AUDIT_CHANNEL_ID  # Where failing feeds are reported
FEED_SUGGESTIONS_CHANNEL_ID = RSS_AUDIT_CHANNEL_ID  # Where mods get /blog suggest cards to approve or reject
FEED_COMMUNITY_CHANNEL_ID = FEED_CHANNELS["community-feed"]  # The community's blogs: voted on and rounded up weekly
FEED_THREAD_CHANNEL_IDS = {FEED_COMMUNITY_CHANNEL_ID}       # Posts here get a discussion thread
FEED_ROUNDUP_CHANNEL_ID = 1008077287813550093               # #general: where the weekly roundup is posted
FEED_ROUNDUP_TIMEZONE = "Europe/Copenhagen"                 # The roundup goes out Mondays 09:00 in this timezone
# Added to every post in the community channel; the roundup's top 3 are the
# posts with the most of these. The server's own :msems: emoji ("<:name:id>" format).
FEED_VOTE_EMOJI = "<:msems:1368551524963192842>"
# In front of an author's profile links under their posts (server emojis in
# "<:name:id>" format). Swap these if the server gets its own logos.
FEED_LINK_EMOJIS = {
    "linkedin": "💼",
    "x": "<:x_:1561698449114800208>",
    "youtube": "▶️",
    "github": "<:tool_github:1031898235167576094>",
}

# New Account heuristic tagging
NEW_ACCOUNT_ROLE_ID = 1534323968591532083   # 🆕 auto-assigned on join if heuristics trip
NEW_ACCOUNT_AGE_DAYS = 7                    # Account younger than this trips the heuristic
