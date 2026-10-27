# ============================================================
# bot.py - Starts Skippy: logging, the Discord connection, and loading
# each feature. Every feature is its own module (EXTENSIONS) that adds
# its commands and event listeners; nothing feature-specific lives here.
# ============================================================

import asyncio
import logging
import logging.handlers
import os
import traceback

import discord
from discord.ext import commands

import db
from config import BOT_TOKEN, GUILD_ID, HELPER_LOG_CHANNEL_ID, LOG_DAYS, LOG_DIR

EXTENSIONS = (
    "followup_events",    # 🔴 flags, nudges, departures and the helper log's buttons
    "followup_commands",  # /flag /unmark /score /stats /audit
    "followup_tasks",     # 48h reminders, 7-day ignores, monthly cleanup
    "new_accounts",       # tagging, verification, join bursts
    "link_watch",         # tracking junk and dangerous short links
    "mentions",           # answering @Skippy
    "rss_watch",          # the RSS watchlist and /rsswatch
    "feeds",              # /feed and /blog: posting RSS/Atom feeds
    "guide",              # /help
    "fun",                # /ghostcheck /verdict /roulette /trial /slap /effie
)

logger = logging.getLogger("followup-bot")


def setup_logging():
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


class Skippy(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # Required for on_member_update
        intents.message_content = True  # Required for reading message content
        intents.reactions = True        # Required for on_raw_reaction_add
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Serving guild ID: {GUILD_ID}")

        # Sync slash commands to the guild (guild-scoped = instant; global takes ~1h)
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}.")

    async def on_error(self, event_method, *args, **kwargs):
        """Global safety net: an uncaught exception in any event handler lands here
        instead of only hitting stderr, so it's logged and visible in Discord too."""
        tb = traceback.format_exc()
        logger.error(f"Unhandled exception in {event_method}:\n{tb}")

        try:
            channel = self.get_channel(HELPER_LOG_CHANNEL_ID)
            if channel:
                snippet = tb.strip().splitlines()[-1][:500]
                await channel.send(f"⚠️ Bugged out in `{event_method}`: `{snippet}`")
        except Exception:
            pass  # Never let the error handler itself take the bot down


async def main():
    setup_logging()
    bot = Skippy()
    async with bot:
        # Initialize DB before anything else - ensures tables exist before
        # the background tasks fire on bot ready.
        await db.init_db()
        logger.info("Database initialized.")
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
