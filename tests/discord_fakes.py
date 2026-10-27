# Stand-ins for the Discord objects Skippy's event handlers touch, so events
# can go through the real bot (its own dispatch, every listener) without a
# connection. They record what Skippy did: messages sent, replies, DMs,
# roles and nicknames.

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import discord

from config import (AWAITING_ROLE_ID, FLAG_EMOJI, GUILD_ID, HELPER_LOG_CHANNEL_ID, HELPER_ROLE_IDS,
                    NEW_ACCOUNT_ROLE_ID, RSS_AUDIT_CHANNEL_ID)

_ids = itertools.count(10_000)
GENERAL_ID = 222


def not_found() -> discord.NotFound:
    return discord.NotFound(mock.Mock(status=404, reason="Not Found"), "Unknown Message")


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "Missing Permissions")


class FakeRole:
    def __init__(self, role_id: int, name: str):
        self.id, self.name, self.mention = role_id, name, f"<@&{role_id}>"


class FakeMember:
    def __init__(self, guild, name: str, roles=(), bot=False, default_avatar=False, age_days=365):
        self.id, self.name, self.display_name, self.nick = next(_ids), name, name, None
        self.mention, self.bot, self.guild = f"<@{self.id}>", bot, guild
        self.roles = list(roles)
        self.avatar = None if default_avatar else SimpleNamespace(url="https://cdn.example/a.png")
        self.display_avatar = SimpleNamespace(url="https://cdn.example/a.png")
        self.created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
        self.dms, self.dms_closed, self.can_rename = [], False, True

    def __str__(self):
        return self.name

    def has(self, role_id: int) -> bool:
        return any(r.id == role_id for r in self.roles)

    async def add_roles(self, *roles, reason=None):
        self.roles += [r for r in roles if r not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]

    async def edit(self, *, reason=None, **changes):
        if "nick" in changes:
            if not self.can_rename:
                raise forbidden()
            self.nick = changes["nick"]
            self.display_name = self.nick or self.name

    async def send(self, content=None, *, view=None, embed=None):
        if self.dms_closed:
            raise discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "Cannot send messages to this user")
        message = FakeMessage(None, SKIPPY, content, embed=embed, view=view)
        self.dms.append(message)
        return message


SKIPPY = FakeMember(None, "skippy_msems", bot=True)


class FakeChannel:
    def __init__(self, guild, name: str, channel_id: int | None = None):
        self.id, self.name, self.guild = channel_id or next(_ids), name, guild
        self.mention = f"<#{self.id}>"
        self.sent, self.messages = [], {}

    async def send(self, content=None, *, embed=None, view=None, allowed_mentions=None):
        message = FakeMessage(self, SKIPPY, content, embed=embed, view=view)
        self.sent.append(message)
        return message

    async def fetch_message(self, message_id: int):
        if message_id in self.messages:
            return self.messages[message_id]
        raise not_found()

    def post(self, author, content="", *, mentions=(), reply_to=None, mention_everyone=False):
        """A member's message in this channel, as Discord would deliver it."""
        message = FakeMessage(self, author, content, mentions=mentions, reply_to=reply_to,
                              mention_everyone=mention_everyone)
        self.messages[message.id] = message
        return message

    def titles(self) -> list[str]:
        return [m.embed.title for m in self.sent if m.embed]


class FakeMessage:
    def __init__(self, channel, author, content=None, *, embed=None, view=None, mentions=(), reply_to=None,
                 mention_everyone=False):
        self.id, self.channel, self.author, self.content = next(_ids), channel, author, content or ""
        self.guild = channel.guild if channel else None
        self.embed, self.view, self.mentions = embed, view, list(mentions)
        self.embeds = [embed] if embed else []
        self.mention_everyone = mention_everyone
        self.reference = SimpleNamespace(message_id=reply_to.id) if reply_to else None
        self.replies, self.edits, self.deleted = [], [], False
        self._state = None  # read by discord.py's prefix-command check, which finds nothing to run
        where = f"{self.guild.id}/{channel.id}" if channel else "@me/0"
        self.jump_url = f"https://discord.com/channels/{where}/{self.id}"

    async def reply(self, content=None, *, embed=None, view=None, mention_author=None, allowed_mentions=None):
        message = FakeMessage(self.channel, SKIPPY, content, embed=embed, view=view)
        self.replies.append(message)
        return message

    async def edit(self, **changes):
        self.edits.append(changes)
        if "content" in changes:
            self.content = changes["content"]

    async def delete(self):
        self.deleted = True


class FakeGuild:
    def __init__(self):
        self.id, self.name = GUILD_ID, "Microsoft EMS Community"
        self.roles = {role_id: FakeRole(role_id, name) for role_id, name in
                      [(AWAITING_ROLE_ID, "🔴Awaiting Follow-up"), (NEW_ACCOUNT_ROLE_ID, "New Account")]
                      + [(r, f"helper {r}") for r in HELPER_ROLE_IDS]}
        self.members, self.channels = {}, {}
        self.helper_log = self.channel("bot-management", HELPER_LOG_CHANNEL_ID)
        self.audit = self.channel("moderator-audit", RSS_AUDIT_CHANNEL_ID)
        self.general = self.channel("general", GENERAL_ID)

    def channel(self, name: str, channel_id: int | None = None) -> FakeChannel:
        channel = FakeChannel(self, name, channel_id)
        self.channels[channel.id] = channel
        return channel

    def member(self, name: str, *, helper=False, role_ids=(), **kwargs) -> FakeMember:
        roles = [self.roles[r] for r in ((HELPER_ROLE_IDS[0],) if helper else ()) + tuple(role_ids)]
        member = FakeMember(self, name, roles, **kwargs)
        self.members[member.id] = member
        return member

    def get_member(self, member_id):
        return self.members.get(int(member_id))

    def get_role(self, role_id):
        return self.roles.get(role_id)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_thread(self, channel_id):
        return None

    def get_channel_or_thread(self, channel_id):
        return self.get_channel(channel_id)

    async def fetch_channel(self, channel_id):
        if channel := self.get_channel(channel_id):
            return channel
        raise not_found()


def reaction(message: FakeMessage, user: FakeMember, emoji: str = FLAG_EMOJI):
    """The payload Discord sends when someone reacts to a message."""
    return SimpleNamespace(guild_id=message.guild.id, channel_id=message.channel.id, message_id=message.id,
                           user_id=user.id, emoji=emoji)


def button_click(user: FakeMember, custom_id: str, message: FakeMessage):
    """An interaction for someone clicking one of Skippy's buttons."""
    return SimpleNamespace(
        type=discord.InteractionType.component, data={"custom_id": custom_id}, user=user,
        guild=user.guild, message=message,
        response=SimpleNamespace(send_message=mock.AsyncMock(), defer=mock.AsyncMock()),
        followup=SimpleNamespace(send=mock.AsyncMock()),
    )
