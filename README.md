<p align="center">
  <img src="skippy-logo.png" alt="Skippy, a skeptical paperclip wearing a headset" width="160">
</p>

<h1 align="center">Skippy</h1>

<p align="center">
  The Microsoft EMS Community's Discord bot: it makes sure questions get followed up,<br>
  cleans tracking junk from links, and runs the community content feed.
</p>

<p align="center">
  <a href="https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml"><img src="https://github.com/Microsoft-EMS-Community/skippy/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
</p>

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
- **HELPER_LOG_CHANNEL_ID** — where helper/mod logs appear

---

## 📝 Configure `config.py`

Open `config.py` and fill in your values:

```python
BOT_TOKEN = "your-bot-token-here"
GUILD_ID = 1234567890               # Your server ID
AWAITING_ROLE_ID = 1234567890       # 🔴 Awaiting Follow-up role ID
FOLLOWSUP_ROLE_ID = 1234567890      # ✅ Follows Up role ID
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
sudo less /var/lib/skippy/logs/skippy.log  # Skippy's own log: today, plus one file per day for 7 days
```

---

## 🤖 Bot Behavior

### Flagging a User (helper actions)

**Primary — React with 🔴 on the question message:**
A helper reacts with the `FLAG_EMOJI` (default 🔴) directly on the user's message.
- Bot verifies the reactor has `HELPER_ROLE_ID`
- Adds the 🔴 role to the message author automatically
- Captures the exact message as context (no searching needed)
- Replies on that message to tell the user (or DMs them if it can't)

**Backup — `/flag @user [reason]`:**
Use when you can't react to a specific message (e.g. old threads, mobile edge cases).
- Same effect as the reaction, with an optional reason string logged as context

### Role Added (🔴 Awaiting Follow-up)
When the 🔴 role is added (via reaction, `/flag`, or manually):
1. Bot logs the event to SQLite (user, channel, message link, snippet)
2. Increments the user's `flag_count`
3. Tells the user with a reply on the flagged message (a DM if the message is gone)

### New Message from Flagged User
When a member with 🔴 role posts in any non-bot channel:
1. Posts a warning to `HELPER_LOG_CHANNEL_ID`
2. DMs or pings the user to resolve their open follow-ups first

### Warning Progression (every 24h)
- **> 48 hours**: Replies on the flagged message with a reminder (once; a DM if the message is gone)
- **> 7 days**: Marks follow-up as `ignored` in the database, and notes it in the helper log

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
| `/feed add <url> [channel] [role] [author] [name]` | RSS admins | Post a feed's new items in one of the feed channels, optionally pinging a role; its newest item is posted right away (no pings) as a sign it's connected |
| `/feed remove <feed>` | RSS admins | Stop posting a feed |
| `/feed list` | RSS admins | Show all feeds and whether they're working |
| `/feed activity [mode] [min_messages]` | RSS admins | Show authors' activity, or switch the activity rule (off / watch / warn / enforce) |
| `/feed roundup [action]` | RSS admins | Preview the weekly roundup (default), post it now, or switch it on/off |
| `/feed edit <feed> [...]` | RSS admins | Change a feed's channel, role, word filters or summary threshold; with just a feed, show its settings |
| `/blog search <words>` | Anyone | Search everything the feeds have posted |
| `/blog suggest <url> [own_blog] [note]` | Anyone | Suggest a blog; mods approve or reject it with a button |
| `/feed test <feed>` | RSS admins | Post a feed's newest item now, to check the format |

> **Note:** Commands are guild-scoped (registered per-server) for instant availability.
> `/flag` and `/unmark` are restricted to members with `HELPER_ROLE_ID`.

---

## 📰 Feeds

Skippy posts new items from RSS and Atom feeds (blogs, YouTube channels,
release notes, ...) in a fixed set of feed channels (`FEED_CHANNELS`:
#community-feed by default, #ms-blogs, #ms-status and #cve); `/feed add` offers
only those. Members with the RSS admin role manage feeds with `/feed`.

- Posts read "Feed name - Post title" with the link below (Discord shows the
  preview card), the same plain format MEE6 used.
- Feeds are checked every 5 minutes (`FEED_POLL_MINUTES`), five at a time,
  using conditional requests so unchanged feeds cost almost nothing. A site
  that answers "too many requests" (HTTP 429, or 503 with Retry-After) is
  left alone for as long as it asks, up to 6 hours.
- Adding a feed skips everything already in it; only new items are posted,
  oldest first. Nothing is posted twice, even across restarts.
- When more new items arrive at once than the feed's summary threshold
  (default 5), they become one summary post instead of a flood, with nothing
  dropped. Patch Tuesday in #cve is the typical case. Words set as
  "highlight" (e.g. Remote Code Execution) are listed first, in bold.
- Per-feed word filters (`/feed edit`): "exclude" words are never posted
  (e.g. Chromium), and "include" words limit a feed to matching titles.
- Everything posted is archived, and anyone can search it with `/blog search`.
- Links are cleaned of tracking parameters, the same way chat links are.
- A feed can get a display `name` (`/feed add` or `/feed edit`) when its own
  title is unhelpful, e.g. just "Blogs". Pasted URLs are forgiving: <...>
  brackets, quotes, invisible characters and a missing https:// are fixed.
- A feed that fails for an hour is reported in the RSS audit channel
  (`FEED_ALERT_CHANNEL_ID`), and again when it recovers.
- Fetching follows the same rules as the link cleaner: public addresses only,
  checked on every redirect, at most 5 MB, and no XML entity tricks.

---

### Community

- **Suggestions:** members run `/blog suggest <feed url>`. Skippy checks it's a
  readable feed that isn't tracked yet, then posts a card in the RSS audit
  channel (`FEED_SUGGESTIONS_CHANNEL_ID`) with **Approve** and **Reject**
  buttons for moderators. Approving adds the feed to #community-feed
  (credited to the member if it's their own blog) and posts its newest article; rejecting asks for an
  optional reason. Either way the member gets a DM. Each member can have at
  most 3 suggestions waiting.
- **Author credit:** a feed linked to a member (`author` on `/feed add` or
  `/feed edit`) shows "· by @member" on every post and pings them, and puts
  them on the RSS watchlist so their server activity is tracked.
- **Discussion threads:** posts in #community-feed (`FEED_THREAD_CHANNEL_IDS`)
  get a thread on the post itself, named after the article. Every post gets
  the same treatment however it was posted (a check, `/feed add`, an approved
  suggestion or `/feed test`); summaries get a thread but no vote emoji.
- **Audit trail:** every mod action on feeds (add, edit, remove, test, the
  activity rule, the roundup) is posted as a line in the RSS audit channel and
  stored in the audit log (`/audit`).
- **Activity rule:** the content tracker is for active members. A feed
  linked to an author follows the rule mods set with `/feed activity`,
  phased in step by step:
  - `off`: nothing is checked (the default).
  - `watch`: `/feed activity` and `/feed list` show each author's activity
    (🟢 active, 🆕 grace period, 🔴 inactive). Nothing else happens.
  - `warn`: an author who goes 🔴 gets one friendly DM and mods get a notice
    in the RSS audit channel; their posts keep going.
  - `enforce`: a 🔴 author's new posts are skipped (not saved up) until
    they're active again. They and the mods are told both ways.

  Active means at least `min_messages` (default 1) messages in the last 30
  days, counted by the RSS watchlist. Authors linked less than 30 days ago
  get a grace period. Feeds without an author, and ones exempted with
  `/feed edit activity_exempt:True`, are never affected.
- **Weekly roundup:** Mondays 09:00 Danish time (`FEED_ROUNDUP_TIMEZONE`),
  #general (`FEED_ROUNDUP_CHANNEL_ID`) gets a recap of the week's posts in
  #community-feed (`FEED_COMMUNITY_CHANNEL_ID`), grouped per blogger, with
  the week's 🏆 community favourites: the 3 posts with the most :msems: votes
  (`FEED_VOTE_EMOJI`).
  Skippy adds the emoji to each post so voting is one click; its own and the
  author's votes don't count, nor do posts that went out in a summary.
  Nobody is pinged. A week without posts gets no roundup. Mods preview it,
  post it early or switch it off with `/feed roundup`.
- `/feed` is only shown to members with Manage Roles (the Moderator role);
  everyone gets `/blog`. Server admins can change visibility under Server
  Settings → Integrations → Skippy.

---

## 🗃 Database Schema

Located at `SKIPPY_DB_PATH` if set (on the server: `/var/lib/skippy/followup.db`), otherwise `followup.db` next to `config.py`.

- **follow_ups** — one row per flagged event (status: `open` / `resolved` / `ignored`)
- **user_scores** — running totals per user
- **audit_log** — timestamped log of every significant action
- **feeds** — one row per feed: URL, optional ping role, caching headers, health
- **feed_items** — which items of each feed were already posted
- **feed_posts** — archive of everything posted (title, link, message), for `/blog search`
- **feed_suggestions** — `/blog suggest` queue: who suggested what, and the mods' decision

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
