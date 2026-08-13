# ============================================================
# views.py - Discord UI button views for helper log embeds
# Uses custom_id encoding to survive bot restarts.
# Interaction handling is in bot.py (on_interaction).
# ============================================================

import discord


class FlaggedView(discord.ui.View):
    """Buttons for the 🔴 Flagged embed - just Unmark."""

    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Unmark",
            style=discord.ButtonStyle.success,
            custom_id=f"followup:unmark:{user_id}",
            emoji="✅"
        ))


class VerifyHumanView(discord.ui.View):
    """Button for the new-account nudge - only the tagged user can verify themselves.

    Includes a decoy button (grey, unlabeled purpose) as a honeypot - a real
    person reading "Click below to confirm you're human" has no reason to
    press it, but a script blindly clicking the first/any button will.
    """

    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Continue",
            style=discord.ButtonStyle.secondary,
            custom_id=f"followup:verify_honeypot:{user_id}",
        ))
        self.add_item(discord.ui.Button(
            label="I'm Human",
            style=discord.ButtonStyle.success,
            custom_id=f"followup:verify_human:{user_id}",
            emoji="✅"
        ))


class RepliedView(discord.ui.View):
    """Buttons for the 💬 Replied embed - Unmark or Re-flag."""

    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="✅ Unmark",
            style=discord.ButtonStyle.success,
            custom_id=f"followup:unmark:{user_id}",
        ))
        self.add_item(discord.ui.Button(
            label="🔴 Re-flag",
            style=discord.ButtonStyle.danger,
            custom_id=f"followup:reflag:{user_id}",
        ))
