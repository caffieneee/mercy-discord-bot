"""Discord UI components for the ticket system.

Two kinds of views live here:

* **Persistent views** (``timeout=None`` with fixed ``custom_id``s) - the ticket
  panel and the in-ticket control bar. They are registered once and keep working
  across restarts; state is always looked up from the database / channel at
  click time, never held in the view instance.
* **Ephemeral views** (modals, confirm dialogs, user pickers) - created per
  interaction and discarded afterwards.

The views are intentionally thin: they extract the interaction data and hand off
to methods on the cog, which owns all the logic and the database work.
"""
from __future__ import annotations

import logging
from typing import List

import discord

from cogs.interaction_utils import (
    deregister_modal,
    safe_defer,
    safe_edit,
    safe_reply,
    send_modal,
)
from .config import PRIORITY_META, PRIORITY_ORDER, TicketType, safe_emoji

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Panel
# --------------------------------------------------------------------------- #
class TicketPanelView(discord.ui.View):
    """The public panel. One button per configured ticket type."""

    def __init__(self, cog, guild_id: int, types: List[TicketType]) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        seen: set = set()
        for ticket_type in types:
            custom_id = f"tt:open:{guild_id}:{ticket_type.key}"
            # Discord rejects a ``custom_id`` over 100 characters, and a duplicate
            # ``custom_id`` on one message, with an HTTP 400 - which would take the
            # whole panel (and ``/ticket setup``) down. Keys are length-capped when
            # they are stored, but a hand-edited document could still be bad, so
            # skip rather than fail.
            if len(custom_id) > 100:
                log.warning(
                    "Skipping ticket type %r: custom_id would exceed Discord's 100-char limit (%d)",
                    ticket_type.key, len(custom_id),
                )
                continue
            if custom_id in seen:
                log.warning("Skipping ticket type %r: duplicate custom_id %r", ticket_type.key, custom_id)
                continue
            if len(self.children) >= 25:  # Discord's per-message component limit
                log.warning("Skipping ticket type %r: panel is full (25 buttons)", ticket_type.key)
                break
            try:
                button = discord.ui.Button(
                    label=(ticket_type.label or "Ticket")[:80],
                    emoji=safe_emoji(ticket_type.emoji) or None,
                    style=discord.ButtonStyle.primary,
                    custom_id=custom_id,
                )
            except Exception:
                log.exception("Skipping ticket type %r: could not build button", ticket_type.key)
                continue
            seen.add(custom_id)
            button.callback = self._make_callback(ticket_type.key)
            self.add_item(button)

    def _make_callback(self, type_key: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.handle_open(interaction, type_key)

        return callback


# --------------------------------------------------------------------------- #
# In-ticket controls
# --------------------------------------------------------------------------- #
class TicketControlView(discord.ui.View):
    """Persistent control bar attached to every ticket intro message."""

    def __init__(self, cog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Claim", style=discord.ButtonStyle.success, custom_id="tt:claim", emoji="\U0001F64B"
    )
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_claim(interaction)

    @discord.ui.button(
        label="Add / Remove", style=discord.ButtonStyle.secondary, custom_id="tt:adduser", emoji="\U0001F465"
    )
    async def add_user(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_manage_users(interaction)

    @discord.ui.button(
        label="Rename", style=discord.ButtonStyle.secondary, custom_id="tt:rename", emoji="\u270F\uFE0F"
    )
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_rename(interaction)

    @discord.ui.button(
        label="Transcript", style=discord.ButtonStyle.secondary, custom_id="tt:transcript", emoji="\U0001F4C4"
    )
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_transcript(interaction)

    @discord.ui.button(
        label="Close", style=discord.ButtonStyle.danger, custom_id="tt:close", emoji="\U0001F512"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_close_button(interaction)

    @discord.ui.select(
        placeholder="Set priority\u2026",
        custom_id="tt:priority",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label=PRIORITY_META[key]["label"],
                value=key,
                emoji=PRIORITY_META[key]["emoji"],
            )
            for key in PRIORITY_ORDER
        ],
    )
    async def priority(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        await self.cog.handle_priority(interaction, select.values[0])


# --------------------------------------------------------------------------- #
# Modals
# --------------------------------------------------------------------------- #
class OpenTicketModal(discord.ui.Modal, title="Open a Ticket"):
    subject = discord.ui.TextInput(
        label="Subject",
        placeholder="A short summary of your request",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
    )
    description = discord.ui.TextInput(
        label="Describe the issue",
        placeholder="Give us as much detail as you can\u2026",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, cog, guild_id: int, type_key: str, type_label: str) -> None:
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id
        self.type_key = type_key
        self.title = f"Open a {type_label} Ticket"[:45]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        await self.cog.create_ticket(
            interaction,
            self.guild_id,
            self.type_key,
            self.subject.value.strip(),
            self.description.value.strip(),
        )


class CloseReasonModal(discord.ui.Modal, title="Close Ticket"):
    reason = discord.ui.TextInput(
        label="Reason for closing",
        placeholder="Optional \u2014 shared in the transcript",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, cog, channel_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        await self.cog.finalize_close(interaction, self.reason.value.strip() or None)


class RenameModal(discord.ui.Modal, title="Rename Ticket"):
    new_name = discord.ui.TextInput(
        label="New channel name",
        placeholder="e.g. billing-issue",
        style=discord.TextStyle.short,
        required=True,
        max_length=90,
    )

    def __init__(self, cog, channel_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        deregister_modal(interaction, self)
        await self.cog.apply_rename(interaction, self.new_name.value)


# --------------------------------------------------------------------------- #
# Ephemeral flows
# --------------------------------------------------------------------------- #
class CloseConfirmView(discord.ui.View):
    """Ephemeral confirmation shown when staff/owner presses Close."""

    def __init__(self, cog, channel_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id

    @discord.ui.button(label="Confirm Close", style=discord.ButtonStyle.danger, emoji="\U0001F512")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.finalize_close(interaction, None)

    @discord.ui.button(label="Close with Reason", style=discord.ButtonStyle.primary, emoji="\u270D\uFE0F")
    async def with_reason(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        sent = await send_modal(interaction, CloseReasonModal(self.cog, self.channel_id))
        if not sent:
            await safe_reply(
                interaction, "Sorry, I couldn't open the close form. Please try again.", ephemeral=True
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="\u2716\uFE0F")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # ``ephemeral=False`` is deliberate: it makes the defer a
        # ``deferred_message_update`` so the edit below lands on *this* message
        # and clears its buttons. Requesting an ephemeral defer would instead
        # spawn a fresh ephemeral message and leave these buttons on screen.
        await safe_defer(interaction, ephemeral=False)
        # An empty view clears the buttons; safe_edit ignores ``view=None`` by design.
        await safe_edit(
            interaction, content="Ticket close cancelled.", embed=None, view=discord.ui.View()
        )


class ManageUsersView(discord.ui.View):
    """Ephemeral entry point for adding/removing members on a ticket."""

    def __init__(self, cog, channel_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id

    @discord.ui.button(label="Add User", style=discord.ButtonStyle.success, emoji="\u2795")
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Edit this message in place (see CloseConfirmView.cancel for why
        # ``ephemeral`` must stay False on a component callback).
        await safe_defer(interaction, ephemeral=False)
        await safe_edit(
            interaction,
            content="Pick the member you want to **add** to this ticket.",
            view=AddUserView(self.cog, self.channel_id),
        )

    @discord.ui.button(label="Remove User", style=discord.ButtonStyle.danger, emoji="\u2796")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await safe_defer(interaction, ephemeral=False)
        view = await self.cog.build_remove_user_view(self.channel_id)
        if view is None:
            await safe_edit(interaction, content="There are no added members to remove.")
            return
        await safe_edit(
            interaction,
            content="Pick the member you want to **remove** from this ticket.",
            view=view,
        )


class AddUserView(discord.ui.View):
    def __init__(self, cog, channel_id: int) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Select a member to add\u2026",
        min_values=1,
        max_values=1,
        custom_id="tt:adduser_select",
    )
    async def select_user(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        await self.cog.add_user(interaction, select.values[0].id)


class RemoveUserSelect(discord.ui.Select):
    def __init__(self, cog, channel_id: int, options: List[discord.SelectOption]) -> None:
        super().__init__(placeholder="Select a member to remove\u2026", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.remove_user(interaction, int(self.values[0]))


class RemoveUserView(discord.ui.View):
    def __init__(self, cog, channel_id: int, options: List[discord.SelectOption]) -> None:
        super().__init__(timeout=120)
        self.add_item(RemoveUserSelect(cog, channel_id, options))
