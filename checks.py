# ============================================================
# checks.py - Who may do what: helpers (HELPER_ROLE_IDS) and moderators
# (RSS_ADMIN_ROLE_ID, the "Moderator" role).
# ============================================================

import discord
from discord import app_commands

from config import HELPER_ROLE_IDS, RSS_ADMIN_ROLE_ID


def is_moderator(member) -> bool:
    return any(r.id == RSS_ADMIN_ROLE_ID for r in getattr(member, "roles", []))


def is_helper():
    """Check that the command invoker has at least one of the configured HELPER_ROLE_IDS."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not HELPER_ROLE_IDS:
            return True  # No restriction configured - allow all
        user_role_ids = {r.id for r in interaction.user.roles}
        return any(rid in user_role_ids for rid in HELPER_ROLE_IDS)
    return app_commands.check(predicate)


def is_rss_admin():
    """Check that the invoker has the RSS admin role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        role_ids = {r.id for r in interaction.user.roles}
        return RSS_ADMIN_ROLE_ID in role_ids
    return app_commands.check(predicate)
