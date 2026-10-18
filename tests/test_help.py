# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# /help lists the commands each member can use: everyone sees /blog and the
# community feed; moderators also see the /feed commands.

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ.setdefault("SKIPPY_DB_PATH", os.path.join(tempfile.gettempdir(), "skippy-test-help.db"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import commands  # noqa: E402


async def help_for(roles):
    interaction = mock.Mock()
    interaction.user.roles = [mock.Mock(id=r) for r in roles]
    interaction.response.send_message = mock.AsyncMock()
    cog = commands.FollowUpCommands.__new__(commands.FollowUpCommands)
    await commands.FollowUpCommands.help.callback(cog, interaction)
    return interaction.response.send_message.await_args.kwargs["embed"]


class HelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_members_see_blog_and_community_feed(self):
        embed = await help_for([])
        names = [f.name for f in embed.fields]
        self.assertIn("Community feed", names)
        self.assertNotIn("Moderators: feeds", names)
        self.assertIn("/blog suggest", embed.fields[0].value)
        self.assertIn("/blog search", embed.fields[0].value)

    async def test_moderators_also_see_feed_commands(self):
        embed = await help_for([commands.RSS_ADMIN_ROLE_ID])
        mod = next(f for f in embed.fields if f.name == "Moderators: feeds")
        for command in ("/feed add", "/feed edit", "/feed activity", "/feed roundup", "/rsswatch"):
            self.assertIn(command, mod.value)

    async def test_fields_fit_discord_limits(self):
        embed = await help_for([commands.RSS_ADMIN_ROLE_ID])
        self.assertTrue(all(len(f.value) <= 1024 for f in embed.fields))
        self.assertLessEqual(len(embed), 6000)


if __name__ == "__main__":
    unittest.main()
