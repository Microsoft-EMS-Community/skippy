# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Anonymous polls: voting through the buttons, what the card shows while a
# poll is open and after it closes, and that nothing left behind says who
# voted. A throwaway database, no Discord.

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ.setdefault("SKIPPY_DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

import discord  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import db.connection  # noqa: E402
import poll_cards  # noqa: E402
import polls  # noqa: E402
from config import RSS_ADMIN_ROLE_ID  # noqa: E402

GUILD_ID, CHANNEL_ID, ROLE_ID = 1, 111, 777


class Member:
    def __init__(self, user_id, *role_ids, bot=False):
        self.id, self.bot, self.roles = user_id, bot, [SimpleNamespace(id=r) for r in role_ids]

    def __str__(self):
        return f"member{self.id}"


class Channel:
    def __init__(self):
        self.id, self.sent, self.refuses = CHANNEL_ID, [], False

    async def send(self, content=None, *, embed=None, view=None, allowed_mentions=None):
        if self.refuses:
            raise discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "Missing Permissions")
        message = SimpleNamespace(id=5000 + len(self.sent), content=content, embed=embed, view=view,
                                  allowed_mentions=allowed_mentions, edit=mock.AsyncMock())
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id):
        for message in self.sent:
            if message.id == message_id:
                return message
        raise discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Message")


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        await db.init_db()
        self.channel = Channel()
        self.contributors = [Member(i, ROLE_ID) for i in (1, 2, 3, 4)]
        role = SimpleNamespace(id=ROLE_ID, mention=f"<@&{ROLE_ID}>", is_default=lambda: False,
                               members=self.contributors + [Member(99, ROLE_ID, bot=True)])
        self.role = role
        self.guild = SimpleNamespace(id=GUILD_ID, get_role=lambda role_id: role if role_id == ROLE_ID else None)
        bot = mock.Mock()
        bot.get_guild.return_value, bot.get_channel.return_value = self.guild, self.channel
        self.cog = polls.Polls(bot)
        bot.get_cog.return_value = self.cog
        self.bot = bot
        self.mod = Member(50, RSS_ADMIN_ROLE_ID)

    def interaction(self, user, message=None):
        return SimpleNamespace(
            user=user, guild=self.guild, channel=self.channel, client=self.bot, message=message,
            response=SimpleNamespace(send_message=mock.AsyncMock(), defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock()))

    async def create(self, *options, role=None, question="Second summit?", duration=None):
        interaction = self.interaction(self.mod)
        padded = (list(options) or ["Yes", "No"]) + [None] * 5
        await polls.Polls.create.callback(self.cog, interaction, question, *padded[:5], duration=duration, role=role)
        return interaction

    async def click(self, user, choice, poll_id=1):
        """`user` clicks a choice on the poll's card. Returns what only they are told."""
        interaction = self.interaction(user, self.channel.sent[-1])
        await polls.VoteButton(poll_id, choice).callback(interaction)
        args, kwargs = interaction.response.send_message.await_args
        self.assertTrue(kwargs["ephemeral"])
        return args[0]

    def card(self):
        """The poll's card as it stands: its latest edit, or as posted."""
        message = self.channel.sent[-1]
        return message.edit.await_args.kwargs["embed"] if message.edit.await_args else message.embed

    # ---- creating ----

    async def test_create_posts_a_card_with_a_button_per_choice(self):
        interaction = await self.create("Yes", "No", "Only online")
        message = self.channel.sent[0]
        self.assertIn("Second summit?", message.embed.title)
        self.assertEqual([c.item.label for c in message.view.children],
                         ["Yes", "No", "Only online", "How is this anonymous?"])
        self.assertEqual([c.custom_id for c in message.view.children][:3], ["poll:1:0", "poll:1:1", "poll:1:2"])
        self.assertIsNone(message.content)  # no role, no ping
        self.assertIn("Poll #1 is posted", interaction.response.send_message.await_args.args[0])
        self.assertEqual((await db.get_poll(1))["message_id"], str(message.id))

    async def test_only_moderators_can_create_or_close(self):
        for command in (polls.Polls.create, polls.Polls.close):
            checks = [await check(self.interaction(Member(7))) for check in command.checks]
            self.assertEqual(checks, [False])
            self.assertTrue(all([await check(self.interaction(self.mod)) for check in command.checks]))

    async def test_choices_must_differ(self):
        interaction = await self.create("Yes", "Yes")
        self.assertIn("2 different choices", interaction.response.send_message.await_args.args[0])
        self.assertEqual(self.channel.sent, [])

    async def test_a_poll_that_cant_be_posted_isnt_kept(self):
        self.channel.refuses = True
        interaction = await self.create()
        self.assertIn("can't post", interaction.response.send_message.await_args.args[0])
        self.assertEqual(await db.open_polls(), [])

    async def test_a_roles_poll_pings_only_that_role(self):
        await self.create(role=self.role)
        message = self.channel.sent[0]
        self.assertEqual(message.content, f"<@&{ROLE_ID}>")
        self.assertEqual(message.allowed_mentions.roles, [self.role])
        self.assertIn("0 of 4", message.embed.description)  # the bot in the role isn't waited for

    # ---- voting ----

    async def test_a_vote_is_confirmed_privately_and_counted(self):
        await self.create()
        self.assertIn("**Yes**", await self.click(Member(7), 0))
        self.assertIn("1 vote so far", self.card().description)

    async def test_voting_again_changes_the_vote(self):
        await self.create()
        await self.click(Member(7), 0)
        await self.click(Member(7), 1)
        await self.click(Member(8), 1)
        self.assertIn("2 votes so far", self.card().description)
        self.assertEqual(await db.close_poll(1, None), [0, 2])

    async def test_the_open_card_never_shows_how_votes_are_split(self):
        await self.create("Apples", "Pears")
        for user_id in (7, 8, 9):
            await self.click(Member(user_id), 0)
        card = self.card()
        self.assertNotIn("%", card.description)
        self.assertNotIn("Apples", card.description)
        self.assertNotIn("█", card.description)

    async def test_a_roles_poll_refuses_everyone_else(self):
        await self.create(role=self.role)
        self.assertIn("only", await self.click(Member(7), 0))
        self.assertIn("Your vote is in", await self.click(self.contributors[0], 0))
        self.assertIn("1 of 4", self.card().description)

    async def test_votes_after_closing_are_refused(self):
        await self.create()
        await self.cog.close_poll(await db.get_poll(1))
        self.assertIn("closed", await self.click(Member(7), 0))
        self.assertFalse(await db.cast_poll_vote(1, "late", 0))  # even one that slips in as it closes
        self.assertEqual(await db.poll_voters(1), set())

    async def test_the_about_button_explains_the_anonymity(self):
        interaction = self.interaction(Member(7))
        await polls.AboutButton().callback(interaction)
        args, kwargs = interaction.response.send_message.await_args
        self.assertIn("scrambled code", args[0])
        self.assertTrue(kwargs["ephemeral"])

    # ---- closing ----

    async def test_closing_shows_the_results_and_removes_the_buttons(self):
        await self.create("Yes", "No", "Only online")
        for user_id, choice in ((7, 0), (8, 0), (9, 0), (10, 2)):
            await self.click(Member(user_id), choice)
        self.assertTrue(await self.cog.close_poll(await db.get_poll(1)))
        edit = self.channel.sent[0].edit.await_args.kwargs
        self.assertIsNone(edit["view"])
        description = edit["embed"].description
        self.assertIn("75% (3)", description)
        self.assertIn("0% (0)", description)
        self.assertIn("25% (1)", description)
        self.assertIn("**4 votes** · closed", description)

    async def test_a_roles_poll_says_how_many_didnt_vote_never_who(self):
        await self.create(role=self.role)
        await self.click(self.contributors[0], 0)
        await self.click(self.contributors[1], 1)
        await self.click(self.contributors[2], 1)
        await self.cog.close_poll(await db.get_poll(1))
        description = self.card().description
        self.assertIn("**1 didn't vote**", description)
        for member in self.contributors:
            self.assertNotIn(f"<@{member.id}>", description)
            self.assertNotIn(str(member), description)

    async def test_a_roles_poll_closes_as_soon_as_the_whole_role_has_voted(self):
        await self.create(role=self.role)
        self.assertIn("as soon as everyone has voted", self.card().description)
        for member in self.contributors[:3]:
            await self.click(member, 0)
        self.assertIsNone((await db.get_poll(1))["closed_at"])
        self.assertIn("Your vote is in", await self.click(self.contributors[3], 1))
        self.assertIsNotNone((await db.get_poll(1))["closed_at"])
        self.assertIn("everyone voted", self.card().description)
        self.assertIn("75% (3)", self.card().description)

    async def test_a_poll_for_everyone_never_closes_early(self):
        await self.create()
        for user_id in range(20, 30):
            await self.click(Member(user_id), 0)
        self.assertIsNone((await db.get_poll(1))["closed_at"])

    async def test_closing_erases_who_voted(self):
        await self.create(role=self.role)
        await self.click(self.contributors[0], 0)
        await self.cog.close_poll(await db.get_poll(1))
        poll = await db.get_poll(1)
        self.assertIsNone(poll["secret"])
        self.assertEqual((poll["results"], poll["absent"]), ([1, 0], 3))
        self.assertEqual(await db.poll_voters(1), set())

    async def test_a_poll_closes_only_once(self):
        await self.create()
        poll = await db.get_poll(1)
        self.assertTrue(await self.cog.close_poll(poll))
        self.assertFalse(await self.cog.close_poll(poll))

    async def test_due_polls_close_on_their_own(self):
        await self.create(question="Due")
        await self.create(question="Not yet")
        await self.due("closes_at")
        self.assertEqual([p["question"] for p in await db.open_polls()], ["Not yet"])

    async def test_closing_still_works_when_the_card_is_gone(self):
        await self.create()
        self.channel.sent.clear()
        self.assertTrue(await self.cog.close_poll(await db.get_poll(1)))

    async def test_close_command(self):
        await self.create()
        interaction = self.interaction(self.mod)
        await polls.Polls.close.callback(self.cog, interaction, 1)
        self.assertIn("closed", interaction.followup.send.await_args.args[0])
        await polls.Polls.close.callback(self.cog, interaction, 1)
        self.assertIn("isn't open", interaction.response.send_message.await_args.args[0])

    # ---- reminders ----

    async def due(self, column, poll_id=1):
        await db.connection.execute(f"UPDATE polls SET {column} = '2020-01-01T00:00:00' WHERE id = ?", (poll_id,))
        await polls.Polls.close_due.coro(self.cog)

    def test_the_reminder_comes_when_a_quarter_is_left_a_day_before_at_most(self):
        start = datetime(2026, 1, 1)
        for duration, before in ((timedelta(weeks=2), timedelta(hours=24)), (timedelta(weeks=1), timedelta(hours=24)),
                                 (timedelta(days=3), timedelta(hours=18)), (timedelta(hours=8), timedelta(hours=2)),
                                 (timedelta(hours=2), timedelta(minutes=30))):
            self.assertEqual(poll_cards.reminder_time(start, start + duration), start + duration - before)
        for short in (timedelta(minutes=30), timedelta(hours=1)):
            self.assertIsNone(poll_cards.reminder_time(start, start + short))

    async def test_durations_and_defaults(self):
        await self.create()
        await self.create(role=self.role)
        await self.create(duration=polls.DURATIONS["30 minutes"])
        everyone, role, short = await db.open_polls()

        def lasts(poll):
            return datetime.fromisoformat(poll["closes_at"]) - datetime.fromisoformat(poll["created_at"])
        self.assertAlmostEqual(lasts(everyone).total_seconds(), timedelta(days=3).total_seconds(), delta=5)
        self.assertAlmostEqual(lasts(role).total_seconds(), timedelta(weeks=1).total_seconds(), delta=5)
        self.assertAlmostEqual(lasts(short).total_seconds(), 1800, delta=5)
        self.assertIsNone(short["remind_at"])

    async def test_a_reminder_goes_out_once_without_pinging_anyone(self):
        await self.create()
        start_dm = mock.patch.object(polls.feed_notices, "dm", mock.AsyncMock())
        with start_dm as dm:
            await self.due("remind_at")
            await polls.Polls.close_due.coro(self.cog)
        self.assertEqual(len(self.channel.sent), 2)
        reminder = self.channel.sent[1]
        self.assertIn(f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/5000", reminder.content)
        self.assertIn("Second summit?", reminder.content)
        self.assertEqual(reminder.allowed_mentions.to_dict(), discord.AllowedMentions.none().to_dict())
        dm.assert_not_awaited()  # nobody in particular is waited for

    async def test_a_roles_reminder_dms_only_those_who_havent_voted(self):
        await self.create(role=self.role)
        await self.click(self.contributors[0], 0)
        with mock.patch.object(polls.feed_notices, "dm", mock.AsyncMock()) as dm:
            await self.due("remind_at")
        self.assertEqual(sorted(call.args[1] for call in dm.await_args_list), ["2", "3", "4"])
        self.assertIn("Second summit?", dm.await_args.args[2])
        line = self.channel.sent[1].content
        self.assertIn(f"3 of <@&{ROLE_ID}> still to vote", line)
        for member in self.contributors:
            self.assertNotIn(f"<@{member.id}>", line)

    async def test_no_reminder_when_the_whole_role_has_voted(self):
        await self.create(role=self.role)
        for member in self.contributors:
            await db.cast_poll_vote(1, poll_cards.voter_key((await db.get_poll(1))["secret"], member.id), 0)
        with mock.patch.object(polls.feed_notices, "dm", mock.AsyncMock()) as dm:
            await self.due("remind_at")
        dm.assert_not_awaited()
        self.assertEqual(len(self.channel.sent), 1)

    async def test_a_closed_poll_isnt_reminded(self):
        await self.create()
        await self.cog.close_poll(await db.get_poll(1))
        await self.due("remind_at")
        self.assertEqual(len(self.channel.sent), 1)

    # ---- anonymity ----

    async def test_no_member_id_is_stored_with_a_vote(self):
        await self.create()
        voter = Member(123456789012345678)
        await self.click(voter, 0)
        with sqlite3.connect(db.DB_PATH) as conn:
            rows = conn.execute("SELECT * FROM poll_votes").fetchall()
            logged = conn.execute("SELECT * FROM audit_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(str(voter.id), repr(rows))
        self.assertNotIn(str(voter.id), repr(logged))

    async def test_voting_isnt_logged(self):
        await self.create()
        with self.assertNoLogs("followup-bot", level="INFO"):
            await self.click(Member(7), 0)

    async def test_a_members_code_differs_between_polls(self):
        self.assertEqual(poll_cards.voter_key("a", 7), poll_cards.voter_key("a", 7))
        self.assertNotEqual(poll_cards.voter_key("a", 7), poll_cards.voter_key("b", 7))
        self.assertNotEqual(poll_cards.voter_key("a", 7), poll_cards.voter_key("a", 8))


if __name__ == "__main__":
    unittest.main()
