# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Flagged members are told on the flagged message itself, or by DM when
# that message can't be reached.

import sys
import unittest
from pathlib import Path
from unittest import mock

import discord

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import followup_notify  # noqa: E402

LINK = "https://discord.com/channels/1/222/333"


def http_error(status=404):
    return discord.HTTPException(mock.Mock(status=status, reason="nope"), "nope")


def setup(message_exists=True, channel_cached=True):
    message = mock.Mock(reply=mock.AsyncMock())
    channel = mock.Mock(fetch_message=mock.AsyncMock(
        return_value=message, side_effect=None if message_exists else http_error()))
    guild = mock.Mock(name="guild")
    guild.get_channel_or_thread = mock.Mock(return_value=channel if channel_cached else None)
    guild.fetch_channel = mock.AsyncMock(return_value=channel)
    member = mock.Mock(send=mock.AsyncMock())
    return guild, channel, message, member


class NotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_replies_on_the_flagged_message_pinging_only_the_member(self):
        guild, channel, message, member = setup()
        how = await followup_notify.notify_member(guild, member, LINK, "hi", "dm hi")
        self.assertEqual(how, "reply")
        channel.fetch_message.assert_awaited_once_with(333)
        mentions = message.reply.await_args.kwargs["allowed_mentions"]
        self.assertEqual((mentions.users, mentions.roles, mentions.everyone), ([member], False, False))
        member.send.assert_not_awaited()

    async def test_finds_threads_and_forum_posts_that_are_not_cached(self):
        guild, channel, message, member = setup(channel_cached=False)
        self.assertEqual(await followup_notify.notify_member(guild, member, LINK, "hi"), "reply")
        guild.fetch_channel.assert_awaited_once_with(222)

    async def test_uses_a_message_it_is_given_without_looking_it_up(self):
        guild, channel, message, member = setup()
        given = mock.Mock(reply=mock.AsyncMock())
        await followup_notify.notify_member(guild, member, LINK, "hi", message=given)
        given.reply.assert_awaited_once()
        channel.fetch_message.assert_not_awaited()

    async def test_falls_back_to_a_dm_when_the_message_is_gone(self):
        guild, channel, message, member = setup(message_exists=False)
        self.assertEqual(await followup_notify.notify_member(guild, member, LINK, "hi", "dm hi"), "dm")
        member.send.assert_awaited_once_with("dm hi")

    async def test_falls_back_to_a_dm_when_replying_fails(self):
        guild, channel, message, member = setup()
        message.reply.side_effect = http_error(403)
        self.assertEqual(await followup_notify.notify_member(guild, member, LINK, "hi", "dm hi"), "dm")

    async def test_no_link_or_closed_dms(self):
        guild, channel, message, member = setup()
        self.assertEqual(await followup_notify.notify_member(guild, member, None, "hi", "dm hi"), "dm")
        member.send.side_effect = http_error(403)
        self.assertIsNone(await followup_notify.notify_member(guild, member, None, "hi", "dm hi"))
        self.assertIsNone(await followup_notify.notify_member(guild, member, "not a link", "hi"))  # no DM text


if __name__ == "__main__":
    unittest.main()
