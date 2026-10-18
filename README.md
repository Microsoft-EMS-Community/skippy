# 🤖 Discord Follow-Up Bot

[![tests](https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml/badge.svg)](https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml)

A Discord bot that tracks members with open follow-ups, sends reminders, and gives helpers tools to manage and audit follow-up resolution.

---

## 🗂 File Structure

```
skippy/
├── bot.py           # Main bot entry point (events, on_member_update, on_message)
├── db.py            # Async SQLite layer (aiosqlite)
├── config.py        # All configuration (tokens, IDs, DB path)
├── commands.py      # Slash commands (/unmark /score /audit)
├── tasks.py         # Background 24h loop (48h warning, 7d ignored)
├── feeds.py         # /feed commands + loop posting new RSS/Atom items
├── feed_reader.py   # Fetching and parsing RSS/Atom feeds
├── requirements.txt # Python dependencies (pinned)
├── tests/           # Unit tests (no network or Discord needed)
├── deploy/          # Reference copies of the server's service, sandbox and auto-deploy setup
├── .github/         # GitHub Actions: runs the tests on every PR
├── CONTRIBUTING.md  # How changes get reviewed and deployed
├── LICENSE          # MIT
└── README.md        # This file
```

---

## ⚙️ Setup

### 1. Create a Discord Bot & Get a Token

1. Go to [https://discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name → **Create**
3. In the left sidebar, click **Bot** → **Add Bot** → **Yes, do it!**
4. Under **Token**, click **Reset Token** and copy it
5. Enable these **Privileged Gateway Intents**:
   - ✅ **Server Members Intent** (required for `on_member_update`)
   - ✅ **Message Content Intent** (required for reading message content)
6. Under **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Manage Roles`, `Read Message History`, `View Channels`, `Embed Links`, `Add Reactions`
7. Copy the generated URL and open it to invite the bot to your server

### 2. Get Your Guild (Server) ID

1. In Discord: **Settings → Advanced → Enable Developer Mode**
2. Right-click your server icon → **Copy Server ID**

### 3. Get Role IDs

1. Go to **Server Settings → Roles**
2. Right-click the role you want → **Copy Role ID**

You need:
- **AWAITING_ROLE_ID** — the 🔴 "Awaiting Follow-up" role
- **FOLLOWSUP_ROLE_ID** — the ✅ "Follows Up" role (optional, for tracking)
- **HELPER_ROLE_ID** — your helper/moderator role (only these members can flag users)

### 4. Get Channel IDs

1. Right-click any text channel → **Copy Channel ID**

You need:
- **FOLLOWUP_CHANNEL_ID** — where follow-up pings are sent to users
- **HELPER_LOG_CHANNEL_ID** — where helper/mod logs appear

---

## 📝 Configure `config.py`

Open `config.py` and fill in your values:

```python
BOT_TOKEN = "your-bot-token-here"
GUILD_ID = 1234567890               # Your server ID
AWAITING_ROLE_ID = 1234567890       # 🔴 Awaiting Follow-up role ID
FOLLOWSUP_ROLE_ID = 1234567890      # ✅ Follows Up role ID
FOLLOWUP_CHANNEL_ID = 1234567890    # Channel for user pings
HELPER_LOG_CHANNEL_ID = 1234567890  # Channel for helper logs
HELPER_ROLE_ID = 1234567890         # Helper/mod role (can flag users)
FLAG_EMOJI = "🔴"                   # Emoji helpers react with to flag
DB_PATH = ...                       # followup.db next to config.py (no edit needed)
```

---

## 📦 Install Dependencies

```bash
cd skippy

# Create virtualenv
python3 -m venv venv

# Install deps
venv/bin/pip install -r requirements.txt
```

---

## 🚀 Run Manually (for testing)

```bash
cd skippy
venv/bin/python bot.py
```

---

## 🛠 Deployment

The production server runs Skippy as its own unprivileged `skippy` user in a
sandboxed systemd service, and deploys `master` automatically: every 5
minutes it pulls from GitHub, runs the tests, restarts the bot and posts the
result to the helper log channel, rolling back if the bot doesn't stay up.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the flow, and `deploy/` for
reference copies of the server setup.

```bash
sudo systemctl status skippy            # service status
sudo journalctl -u skippy -f            # live logs
sudo journalctl -u skippy-deploy -n 50  # recent auto-deploy runs
sudo skippy-deploy --manual             # deploy a requirements.txt change after review
```

---

## 🤖 Bot Behavior

### Flagging a User (helper actions)

**Primary — React with 🔴 on the question message:**
A helper reacts with the `FLAG_EMOJI` (default 🔴) directly on the user's message.
- Bot verifies the reactor has `HELPER_ROLE_ID`
- Adds the 🔴 role to the message author automatically
- Captures the exact message as context (no searching needed)
- Pings the user in `FOLLOWUP_CHANNEL_ID` with a direct link

**Backup — `/flag @user [reason]`:**
Use when you can't react to a specific message (e.g. old threads, mobile edge cases).
- Same effect as the reaction, with an optional reason string logged as context

### Role Added (🔴 Awaiting Follow-up)
When the 🔴 role is added (via reaction, `/flag`, or manually):
1. Bot logs the event to SQLite (user, channel, message link, snippet)
2. Increments the user's `flag_count`
3. Pings the user in `FOLLOWUP_CHANNEL_ID`

### New Message from Flagged User
When a member with 🔴 role posts in any non-bot channel:
1. Posts a warning to `HELPER_LOG_CHANNEL_ID`
2. DMs or pings the user to resolve their open follow-ups first

### Warning Progression (every 24h)
- **> 48 hours**: Sends a reminder ping in `FOLLOWUP_CHANNEL_ID` (once)
- **> 7 days**: Marks follow-up as `ignored` in the database

### Role Removed (🔴 removed)
- Marks all open follow-ups as resolved in SQLite

---

## 💬 Slash Commands

| Command | Who | Description |
|---------|-----|-------------|
| `/flag @user [reason]` | Helpers only | Flag a user with 🔴, log context, ping them |
| `/unmark @user` | Helpers only | Remove 🔴 role, resolve all open follow-ups, log action |
| `/score @user` | Anyone | Show flag_count, resolved_count, open_count, avg resolution time |
| `/audit` | Anyone | Show last 10 audit log entries |
| `/feed add <url> [channel] [role]` | RSS admins | Post a feed's new items in one of the feed channels, optionally pinging a role |
| `/feed remove <feed>` | RSS admins | Stop posting a feed |
| `/feed list` | RSS admins | Show all feeds and whether they're working |
| `/feed test <feed>` | RSS admins | Post a feed's newest item now, to check the format |

> **Note:** Commands are guild-scoped (registered per-server) for instant availability.
> `/flag` and `/unmark` are restricted to members with `HELPER_ROLE_ID`.

---

## 📰 Feeds

Skippy posts new items from RSS and Atom feeds (blogs, YouTube channels,
release notes, ...) in a fixed set of feed channels (`FEED_CHANNELS`:
#community-feed by default, #ms-blogs, #ms-status and #cve); `/feed add` offers
only those. Members with the RSS admin role manage feeds with `/feed`.

- Feeds are checked every 10 minutes (`FEED_POLL_MINUTES`), using conditional
  requests so unchanged feeds cost almost nothing.
- Adding a feed skips everything already in it; only new items are posted,
  oldest first, at most 5 per check. Nothing is posted twice, even across
  restarts.
- Links are cleaned of tracking parameters, the same way chat links are.
- A feed that fails for an hour is reported in the RSS audit channel
  (`FEED_ALERT_CHANNEL_ID`), and again when it recovers.
- Fetching follows the same rules as the link cleaner: public addresses only,
  checked on every redirect, at most 5 MB, and no XML entity tricks.

---

## 🗃 Database Schema

Located at `SKIPPY_DB_PATH` if set (on the server: `/var/lib/skippy/followup.db`), otherwise `followup.db` next to `config.py`.

- **follow_ups** — one row per flagged event (status: `open` / `resolved` / `ignored`)
- **user_scores** — running totals per user
- **audit_log** — timestamped log of every significant action
- **feeds** — one row per feed: URL, optional ping role, caching headers, health
- **feed_items** — which items of each feed were already posted

---

## 🔒 Required Bot Permissions

| Permission | Reason |
|---|---|
| `Send Messages` | Post pings and logs |
| `Manage Roles` | Remove 🔴 role via `/unmark` |
| `Read Message History` | Find user's last message for context |
| `View Channels` | Access text channels |
| `Embed Links` | Send score embeds |
| `Add Reactions` | (optional) Remove flag emoji after processing |
| `Privileged: Server Members` | Detect role changes via `on_member_update` |
| `Privileged: Message Content` | Read message text for `question_snippet` |
| `Privileged: (Reactions intent auto-enabled)` | Detect helper emoji reactions for flagging |

---

## 📄 License

MIT, see [LICENSE](LICENSE). Contributions welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).
