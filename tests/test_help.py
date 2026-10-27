# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# /help depends only on who asks, never on the channel: moderators get the
# guide to everything Skippy does, everyone else the same command overview.

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ.setdefault("SKIPPY_DB_PATH", os.path.join(tempfile.gettempdir(), "skippy-test-help.db"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fun  # noqa: E402
import followup_commands  # noqa: E402
import guide  # noqa: E402
import rss_watch  # noqa: E402
from config import (FEED_ROUNDUP_CHANNEL_ID, HELPER_LOG_CHANNEL_ID, HELPER_ROLE_IDS, RSS_ADMIN_ROLE_ID,  # noqa: E402
                    RSS_AUDIT_CHANNEL_ID)

HELP = guide.Guide.help
HELPER_ONLY = next(r for r in HELPER_ROLE_IDS if r != RSS_ADMIN_ROLE_ID)


def interaction_with(roles, channel_id=1):
    interaction = mock.Mock()
    interaction.user.roles = [mock.Mock(id=r) for r in roles]
    interaction.channel = interaction.channel_id = mock.Mock(id=channel_id)
    interaction.response.send_message = mock.AsyncMock()
    return interaction


async def help_for(roles, channel_id=1):
    interaction = interaction_with(roles, channel_id)
    cog = guide.Guide.__new__(guide.Guide)
    await HELP.callback(cog, interaction)
    return interaction.response.send_message.await_args.kwargs["embed"]


async def passes_checks(command, roles):
    return all([await check(interaction_with(roles)) for check in command.checks])


def text(embed):
    return " ".join(f.value for f in embed.fields)


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_everyone_can_use_it_in_every_channel(self):
        self.assertIsNone(HELP.default_permissions)  # not hidden anywhere
        self.assertEqual(HELP.checks, [])

    async def test_moderators_get_the_guide_everyone_else_the_overview(self):
        self.assertIn("Moderator guide", (await help_for([RSS_ADMIN_ROLE_ID])).title)
        for roles in ([], [HELPER_ONLY]):
            self.assertEqual((await help_for(roles)).to_dict(), guide.member_help().to_dict())

    async def test_the_same_in_every_channel(self):
        for roles in ([], [RSS_ADMIN_ROLE_ID]):
            here, there = await help_for(roles, channel_id=111), await help_for(roles, channel_id=222)
            self.assertEqual(here.to_dict(), there.to_dict())

    async def test_overview_has_member_commands_not_mod_ones(self):
        overview = text(guide.member_help())
        for command in ("/score", "/stats", "/blog suggest", "/blog search", "/flag", "/unmark"):
            self.assertIn(f"`{command}", overview)
        self.assertNotIn("/feed", overview)
        self.assertNotIn("/rsswatch", overview)
        self.assertIn(f"roundup in <#{FEED_ROUNDUP_CHANNEL_ID}>", overview)

    async def test_guide_covers_every_area(self):
        mod_guide = guide.moderator_help()
        names = " ".join(f.name for f in mod_guide.fields)
        for area in ("Follow-ups", "Feeds", "Blog", "RSS watchlist", "New accounts", "Links", "Fun",
                     "Where Skippy reports", "Changing Skippy"):
            self.assertIn(area, names)
        for channel in (FEED_ROUNDUP_CHANNEL_ID, RSS_AUDIT_CHANNEL_ID,
                        HELPER_LOG_CHANNEL_ID):
            self.assertIn(f"<#{channel}>", text(mod_guide))

    async def test_guide_lists_every_slash_command_the_bot_has(self):
        import feeds
        groups = {"rsswatch": rss_watch.RssWatch.rsswatch, "feed": feeds.FeedCommands.feed,
                  "blog": feeds.FeedCommands.blog}
        names = [c.name for cog in (followup_commands.FollowUpCommands, fun.FunCommands)
                 for c in cog.__cog_app_commands__]
        names += [f"{g} {c.name}" for g, group in groups.items() for c in group.commands]
        self.assertGreater(len(names), 20)
        mod_guide = text(guide.moderator_help())
        self.assertEqual([n for n in names if f"`/{n}" not in mod_guide], [])

    async def test_both_fit_discord_limits(self):
        for embed in (guide.member_help(), guide.moderator_help()):
            self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))
            self.assertLessEqual(len(embed), 6000)

    async def test_audit_is_for_helpers(self):
        audit = followup_commands.FollowUpCommands.audit
        self.assertFalse(await passes_checks(audit, []))
        self.assertTrue(await passes_checks(audit, [HELPER_ROLE_IDS[0]]))

    async def test_a_refused_command_says_why(self):
        interaction = interaction_with([])
        interaction.response.is_done = mock.Mock(return_value=False)
        cog = followup_commands.FollowUpCommands.__new__(followup_commands.FollowUpCommands)
        await cog.cog_app_command_error(interaction, followup_commands.app_commands.CheckFailure())
        args, kwargs = interaction.response.send_message.await_args
        self.assertIn("helpers and moderators", args[0])
        self.assertTrue(kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
