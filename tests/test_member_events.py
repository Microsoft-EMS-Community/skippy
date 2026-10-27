# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# Skippy's reactions to what members do: follow-up flags, nudges and their
# buttons, new-account checks, link cleaning and @mentions. Events go through
# the bot's own dispatch to every listener, as on Discord, with the Discord
# objects faked (discord_fakes.py) and a throwaway database.

import asyncio
import os
import sys
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")  # config.py requires it
os.environ.setdefault("SKIPPY_DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # discord_fakes
import db  # noqa: E402
import views  # noqa: E402
from config import AWAITING_ROLE_ID, NEW_ACCOUNT_ROLE_ID  # noqa: E402
from discord_fakes import SKIPPY, FakeGuild, button_click, reaction  # noqa: E402


async def skippy_bot():
    """Skippy with all its event handling, not connected to Discord."""
    import bot as skippy
    for state in (skippy.nudge_cooldowns, skippy.new_account_nudge_cooldowns, skippy.pending_verification_notices,
                  skippy.verify_prompt_sent_at, skippy.account_audit_cards):
        state.clear()
    skippy.recent_joins.clear()
    await skippy.bot._async_setup_hook()
    return skippy.bot


class EventTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        await db.init_db()
        self.guild = FakeGuild()
        self.helper = self.guild.member("helper", helper=True)
        self.alice = self.guild.member("alice")
        self.bot = await skippy_bot()
        self.bot._connection.user = SKIPPY
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == self.guild.id else None
        self.bot.get_channel = self.guild.get_channel
        self.errors = []

        async def on_error(event, *args, **kwargs):
            self.errors.append(f"{event}: {traceback.format_exc()}")
        self.bot.on_error = on_error

    async def event(self, name: str, *args):
        """Dispatch an event like Discord would and wait until every listener is done."""
        self.bot.dispatch(name, *args)
        for _ in range(20):
            running = [t for t in asyncio.all_tasks() if t.get_name().startswith("discord.py: ") and not t.done()]
            if not running:
                break
            await asyncio.wait(running, timeout=5)
        self.assertEqual(self.errors, [])

    async def flag(self, message, by=None):
        await self.event("raw_reaction_add", reaction(message, by or self.helper))

    async def audit_actions(self) -> list[str]:
        """Every audit log action so far, oldest first."""
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            return [row[0] for row in await (await conn.execute("SELECT action FROM audit_log ORDER BY id")).fetchall()]


class FlagTests(EventTestCase):
    async def test_a_helpers_flag_tags_the_member_and_tells_them_on_the_message(self):
        question = self.guild.general.post(self.alice, "How do I enroll a Mac?")
        await self.flag(question)
        self.assertTrue(self.alice.has(AWAITING_ROLE_ID))
        [item] = await db.get_open_follow_ups(str(self.alice.id))
        self.assertEqual((item["message_link"], item["question_snippet"]), (question.jump_url, "How do I enroll a Mac?"))
        self.assertIn("flagged this message for follow-up", question.replies[0].content)
        [log] = self.guild.helper_log.sent
        self.assertEqual(log.embed.title, "🔴 Flagged")
        self.assertIsInstance(log.view, views.FlaggedView)
        self.assertEqual((await db.get_user_score(str(self.alice.id)))["flag_count"], 1)
        self.assertEqual(await self.audit_actions(), ["flagged_via_reaction"])

    async def test_only_helpers_can_flag(self):
        bob = self.guild.member("bob")
        await self.flag(self.guild.general.post(self.alice, "hi"), by=bob)
        self.assertFalse(self.alice.has(AWAITING_ROLE_ID))
        self.assertEqual(await db.get_open_follow_ups(str(self.alice.id)), [])

    async def test_other_emojis_bots_and_repeat_flags_add_nothing(self):
        question = self.guild.general.post(self.alice, "hi")
        await self.event("raw_reaction_add", reaction(question, self.helper, emoji="👍"))
        self.assertEqual(await db.get_open_follow_ups(str(self.alice.id)), [])
        await self.flag(self.guild.general.post(self.guild.member("a bot", bot=True), "beep"))
        await self.flag(question)
        await self.flag(question)  # the same message again
        self.assertEqual(len(await db.get_open_follow_ups(str(self.alice.id))), 1)

    async def test_a_second_flag_adds_to_the_pile_without_pinging_again(self):
        await self.flag(self.guild.general.post(self.alice, "first"))
        second = self.guild.general.post(self.alice, "second")
        await self.flag(second)
        self.assertEqual(second.replies, [])
        self.assertEqual(self.guild.helper_log.titles(), ["🔴 Flagged", "🔴 Follow-up Added"])
        self.assertIn("now **2** open", self.guild.helper_log.sent[1].embed.description)

    async def test_removing_the_role_by_hand_resolves_everything(self):
        await self.flag(self.guild.general.post(self.alice, "q"))
        before = mock.Mock(id=self.alice.id, roles=list(self.alice.roles))
        await self.alice.remove_roles(self.guild.get_role(AWAITING_ROLE_ID))
        await self.event("member_update", before, self.alice)
        self.assertEqual(await db.get_open_follow_ups(str(self.alice.id)), [])
        self.assertEqual((await db.get_user_score(str(self.alice.id)))["resolved_count"], 1)
        self.assertEqual((await self.audit_actions())[-1], "role_removed")


class NudgeTests(EventTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.question = self.guild.general.post(self.alice, "How do I enroll a Mac?")
        await self.flag(self.question)
        self.elsewhere = self.guild.channel("intune")

    async def test_posting_elsewhere_nudges_once_a_day_then_only_tells_helpers(self):
        await self.event("message", self.elsewhere.post(self.alice, "anyone know about Autopilot?"))
        [nudge] = self.elsewhere.sent
        self.assertIn(f"{self.alice.mention} - you have **1** open follow-up(s)", nudge.content)
        self.assertIn(self.question.jump_url, nudge.content)
        await self.event("message", self.elsewhere.post(self.alice, "hello?"))
        self.assertEqual(len(self.elsewhere.sent), 1)
        self.assertEqual(self.guild.helper_log.titles()[1:], ["📋 Nudged Publicly", "🔕 Nudge Suppressed (cooldown)"])

    async def test_replying_to_the_flagged_message_tells_helpers_without_a_nudge(self):
        await self.event("message", self.guild.general.post(self.alice, "Solved it!", reply_to=self.question))
        self.assertEqual(self.guild.general.sent, [])
        log = self.guild.helper_log.sent[-1]
        self.assertEqual(log.embed.title, "💬 Replied to Flagged Message")
        self.assertIsInstance(log.view, views.RepliedView)

    async def test_mentioning_skippy_gets_an_answer_but_no_nudge(self):
        mention = self.elsewhere.post(self.alice, f"<@{SKIPPY.id}> what do you do?", mentions=[SKIPPY])
        await self.event("message", mention)
        self.assertIn("I'm **Skippy**", mention.replies[0].content)
        self.assertEqual(self.elsewhere.sent, [])

    async def test_members_without_a_flag_are_left_alone(self):
        bob = self.guild.member("bob")
        await self.event("message", self.elsewhere.post(bob, "hi all"))
        self.assertEqual(self.elsewhere.sent, [])


class ButtonTests(EventTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.flag(self.guild.general.post(self.alice, "q"))
        self.card = self.guild.helper_log.sent[-1]

    async def test_unmark_resolves_and_crosses_out_the_card(self):
        click = button_click(self.helper, f"followup:unmark:{self.alice.id}", self.card)
        await self.event("interaction", click)
        self.assertFalse(self.alice.has(AWAITING_ROLE_ID))
        self.assertEqual(await db.get_open_follow_ups(str(self.alice.id)), [])
        self.assertIn(f"Resolved by {self.helper.mention}", self.card.content)
        self.assertIsNone(self.card.edits[-1]["view"])
        self.assertIn("unmarked", click.followup.send.await_args.args[0])
        self.assertEqual((await self.audit_actions())[-1], "unmark_button")

    async def test_reflag_puts_the_role_back(self):
        await self.alice.remove_roles(self.guild.get_role(AWAITING_ROLE_ID))
        await self.event("interaction", button_click(self.helper, f"followup:reflag:{self.alice.id}", self.card))
        self.assertTrue(self.alice.has(AWAITING_ROLE_ID))
        self.assertEqual((await self.audit_actions())[-1], "reflag_button")

    async def test_only_helpers_can_use_them(self):
        click = button_click(self.guild.member("bob"), f"followup:unmark:{self.alice.id}", self.card)
        await self.event("interaction", click)
        self.assertEqual(click.response.send_message.await_args.args[0], "You need a helper role to use this.")
        self.assertTrue(self.alice.has(AWAITING_ROLE_ID))

    async def test_other_buttons_are_not_skippys_business(self):
        click = button_click(self.helper, "feed:approve:1", self.card)
        await self.event("interaction", click)
        click.response.send_message.assert_not_awaited()


class LeaveAndRejoinTests(EventTestCase):
    async def test_leaving_flagged_then_rejoining_is_reported_to_helpers(self):
        await self.flag(self.guild.general.post(self.alice, "q"))
        await self.event("member_remove", self.alice)
        self.assertEqual(await db.get_open_follow_ups(str(self.alice.id)), [])
        self.assertEqual(len(await db.get_left_server_follow_ups(str(self.alice.id))), 1)
        await self.event("member_join", self.alice)
        self.assertEqual(self.guild.helper_log.titles()[1:],
                         ["🚪 Left Server with Open Follow-ups", "👀 Rejoined - Prior Follow-ups"])

    async def test_leaving_without_a_flag_is_nothing_to_report(self):
        await self.event("member_remove", self.alice)
        self.assertEqual(self.guild.helper_log.sent, [])


class NewAccountTests(EventTestCase):
    async def join(self, **kwargs):
        member = self.guild.member("newbie", **kwargs)
        await self.event("member_join", member)
        return member

    async def test_young_or_faceless_accounts_are_tagged_on_join(self):
        member = await self.join(age_days=2, default_avatar=True)
        self.assertTrue(member.has(NEW_ACCOUNT_ROLE_ID))
        self.assertEqual(member.nick, "newbie [unverified]")
        self.assertIn("account age 2d (< 7d), default avatar", (await db.get_recent_audit_log(1))[0]["detail"])
        self.assertEqual(self.guild.audit.titles(), ["🆕 Account Lifecycle: newbie"])
        established = await self.join(age_days=400)
        self.assertFalse(established.has(NEW_ACCOUNT_ROLE_ID))

    async def test_tagging_still_works_without_permission_to_rename(self):
        member = self.guild.member("newbie", default_avatar=True)
        member.can_rename = False
        await self.event("member_join", member)
        self.assertTrue(member.has(NEW_ACCOUNT_ROLE_ID))
        self.assertIsNone(member.nick)

    async def test_a_burst_of_joins_alerts_the_mods(self):
        for _ in range(3):
            await self.join()
        self.assertEqual(self.guild.audit.titles(), ["🚨 Join Burst Detected"])

    async def test_their_first_message_asks_them_to_verify_by_dm_once_a_day(self):
        member = await self.join(default_avatar=True)
        await self.event("message", self.guild.general.post(member, "hi"))
        [dm] = member.dms
        self.assertIsInstance(dm.view, views.VerifyHumanView)
        self.assertIn("has been asked to verify - check your DMs", self.guild.general.sent[0].content)
        await self.event("message", self.guild.general.post(member, "hi again"))
        self.assertEqual(len(member.dms), 1)
        [card] = [m for m in self.guild.audit.sent if not m.deleted]  # one card, reposted at each step
        self.assertIn("Tagged", card.embed.description)
        self.assertIn("Verification requested", card.embed.description)

    async def test_closed_dms_get_the_button_in_the_channel(self):
        member = await self.join(default_avatar=True)
        member.dms_closed = True
        message = self.guild.general.post(member, "hi")
        await self.event("message", message)
        self.assertIsInstance(message.replies[0].view, views.VerifyHumanView)

    async def verify(self, member, button="verify_human", after_seconds=5.0):
        import bot as skippy
        await self.event("message", self.guild.general.post(member, "hi"))
        skippy.verify_prompt_sent_at[str(member.id)] -= timedelta(seconds=after_seconds)
        click = button_click(member, f"followup:{button}:{member.id}", member.dms[0])
        await self.event("interaction", click)
        return click

    async def test_clicking_im_human_clears_the_tag(self):
        member = await self.join(default_avatar=True)
        click = await self.verify(member)
        self.assertEqual(click.response.send_message.await_args.args[0], "✅ You're verified. Welcome!")
        self.assertFalse(member.has(NEW_ACCOUNT_ROLE_ID))
        self.assertEqual(member.display_name, "newbie")
        self.assertIn("✅ Verified", self.guild.general.sent[0].content)  # the "check your DMs" notice
        self.assertEqual((await self.audit_actions())[-1], "self_verified_human")

    async def test_a_too_fast_click_or_the_decoy_looks_verified_but_isnt(self):
        fast, decoy = await self.join(default_avatar=True), await self.join(default_avatar=True)
        for member, click in ((fast, await self.verify(fast, after_seconds=0)),
                              (decoy, await self.verify(decoy, button="verify_honeypot"))):
            self.assertEqual(click.response.send_message.await_args.args[0], "✅ You're verified. Welcome!")
            self.assertTrue(member.has(NEW_ACCOUNT_ROLE_ID))
        actions = await self.audit_actions()
        self.assertIn("verify_click_too_fast", actions)
        self.assertIn("verify_honeypot_triggered", actions)

    async def test_nobody_can_verify_someone_else(self):
        member = await self.join(default_avatar=True)
        await self.event("message", self.guild.general.post(member, "hi"))
        click = button_click(self.alice, f"followup:verify_human:{member.id}", member.dms[0])
        await self.event("interaction", click)
        self.assertEqual(click.response.send_message.await_args.args[0], "This verification isn't for you.")
        self.assertTrue(member.has(NEW_ACCOUNT_ROLE_ID))


class LinkTests(EventTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        import link_cleaner
        patcher = mock.patch.object(link_cleaner, "clean_links_async", mock.AsyncMock(return_value=([], [])))
        self.clean_links = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_tracking_junk_is_cleaned_in_a_reply(self):
        self.clean_links.return_value = ([("https://e.com/?utm_source=x", "https://e.com/")], [])
        message = self.guild.general.post(self.alice, "look https://e.com/?utm_source=x")
        await self.event("message", message)
        self.assertEqual(message.replies[0].content, "🧹 Cleaned tracking junk from that link:\nhttps://e.com/")

    async def test_a_short_link_to_an_internal_address_is_blocked_and_reported(self):
        self.clean_links.return_value = ([], ["https://bit.ly/x"])
        message = self.guild.general.post(self.alice, "https://bit.ly/x")
        with mock.patch("asyncio.sleep", mock.AsyncMock()):
            await self.event("message", message)
            await asyncio.sleep(0)
        self.assertIn("Danger zone", message.replies[-1].content or message.replies[0].content)
        self.assertIn("bit[.]ly", "".join(r.content for r in message.replies))
        self.assertEqual(self.guild.audit.titles(), ["🚨 Blocked Short Link (possible SSRF)"])
        self.assertIn("ssrf_link_blocked", await self.audit_actions())


class MentionTests(EventTestCase):
    async def test_a_mention_with_someone_else_watches_them(self):
        bob = self.guild.member("bob")
        message = self.guild.general.post(self.alice, f"<@{SKIPPY.id}> <@{bob.id}>", mentions=[SKIPPY, bob])
        await self.event("message", message)
        self.assertIn(bob.mention, message.replies[0].content)

    async def test_helpers_get_the_intro_in_the_helper_channel(self):
        message = self.guild.helper_log.post(self.helper, f"<@{SKIPPY.id}> hey", mentions=[SKIPPY])
        await self.event("message", message)
        self.assertEqual(message.replies[0].embed.title, "👋 Hey there! I'm Skippy.")

    async def test_everyone_pings_are_ignored(self):
        message = self.guild.general.post(self.alice, "@everyone", mentions=[SKIPPY], mention_everyone=True)
        await self.event("message", message)
        self.assertEqual(message.replies, [])


class WatchlistTests(EventTestCase):
    async def test_watched_members_messages_are_counted(self):
        await db.add_to_watchlist(str(self.alice.id), str(self.guild.id), str(self.helper.id))
        message = self.guild.general.post(self.alice, "hello")
        await self.event("message", message)
        await self.event("message", self.guild.general.post(self.helper, "hi"))  # not watched
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            rows = await (await conn.execute("SELECT user_id, message_link FROM rss_activity")).fetchall()
        self.assertEqual(rows, [(str(self.alice.id), message.jump_url)])


if __name__ == "__main__":
    unittest.main()
