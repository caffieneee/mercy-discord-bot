"""The ticket system cog.

Everything is driven by a single slash command - ``/ticket setup`` - which
configures the server and posts (or refreshes) the public panel. From there the
whole lifecycle is component driven: users open tickets with a panel button and
a short modal, and staff manage them with the buttons on the ticket's control
bar. The only other way to change configuration is to re-run ``/ticket setup``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.interaction_utils import safe_defer, safe_edit, safe_reply, send_modal

from .config import (
    ACTIVITY_WRITE_THROTTLE_SECONDS,
    AUTO_CLOSE_CHECK_MINUTES,
    AUTO_CLOSE_WARN_BEFORE_HOURS,
    CLOSE_DELETE_DELAY_SECONDS,
    DEFAULT_AUTO_CLOSE_HOURS,
    DEFAULT_PANEL_COLOR,
    DEFAULT_PANEL_DESCRIPTION,
    DEFAULT_PANEL_FOOTER,
    DEFAULT_PANEL_TITLE,
    ERROR_COLOR,
    INFO_COLOR,
    MAX_PANEL_TYPES,
    MAX_TYPE_KEY_LENGTH,
    SUCCESS_COLOR,
    TRANSCRIPT_COLOR,
    TRANSCRIPT_MESSAGE_LIMIT,
    TicketType,
    build_config_defaults,
    config_types,
    default_types,
    extract_emoji,
    find_type,
    is_emoji,
    normalize_priority,
    priority_color,
    priority_emoji,
    priority_label,
    safe_emoji,
    slugify,
)
from .store import TicketStore, utcnow
from .utils import (
    as_utc,
    build_files,
    collect_messages,
    human_dt,
    member_is_staff,
    parse_color,
    parse_staff_roles,
    sanitize_channel_name,
)
from .views import (
    CloseConfirmView,
    ManageUsersView,
    OpenTicketModal,
    RemoveUserView,
    RenameModal,
    TicketControlView,
    TicketPanelView,
)

log = logging.getLogger(__name__)


def _safe_int(value: Any, default: int) -> int:
    """Coerce ``value`` to ``int``, falling back to ``default``.

    Config documents are normally written by this cog, but a hand-edited or
    older document could hold a string/None where an int is expected. A bad
    value must never take down ``/ticket setup``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_types_param(text: Optional[str], base: List[TicketType]) -> List[TicketType]:
    """Parse the ``types`` setup option.

    Syntax: ``"Label:emoji:description; Label2:emoji2:description2"``. The emoji
    and description are optional; an emoji may also be embedded in the label.
    """
    if not text:
        return base
    result: List[TicketType] = []
    seen: set = set()
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        label_raw = parts[0]
        emoji = ""
        description = ""
        if len(parts) == 1:
            emoji, label = extract_emoji(label_raw)
        elif len(parts) == 2:
            emoji, label = extract_emoji(label_raw)
            if is_emoji(parts[1]):
                emoji = parts[1]
            else:
                description = parts[1]
        else:
            label = label_raw
            emoji = parts[1]
            description = ":".join(parts[2:])
            if not emoji:
                e, label2 = extract_emoji(label_raw)
                if label2:
                    emoji, label = e, label2
        label = (label or label_raw or "Ticket").strip()
        key = slugify(label)[:MAX_TYPE_KEY_LENGTH]
        if key in seen:
            continue
        seen.add(key)
        result.append(
            TicketType(
                key=key,
                label=label[:80],
                emoji=safe_emoji(emoji),
                description=description[:100],
            )
        )
        if len(result) >= MAX_PANEL_TYPES:
            break
    return result or base


class TicketSystem(commands.Cog):
    """A complete, first-class ticket system configured by one command."""

    # Class-level group so discord.py registers /ticket with the cog.
    ticket = app_commands.Group(name="ticket", description="Ticket system")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = TicketStore(bot)
        self.control_view: Optional[TicketControlView] = None
        self._panel_views: Dict[int, TicketPanelView] = {}
        self._ticket_channels: set = set()
        self._last_activity: Dict[int, datetime] = {}
        self._last_activity_write: Dict[int, float] = {}
        # Strong references to fire-and-forget tasks (e.g. the delayed channel
        # delete), so they are not garbage-collected mid-flight.
        self._background_tasks: set = set()
        # Channels currently being closed, to make a double "Confirm" a no-op.
        self._closing: set = set()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    async def cog_load(self) -> None:
        await self.store.ensure_indexes()

        # The control bar is guild-agnostic (fixed custom_ids + channel lookup),
        # so a single persistent instance serves every ticket.
        self.control_view = TicketControlView(self)
        try:
            self.bot.add_view(self.control_view)
        except Exception:
            log.exception("Failed to register ticket control view")

        await self._register_panel_views()
        await self._seed_activity_cache()

        if not self.auto_close.is_running():
            self.auto_close.start()

    async def _seed_activity_cache(self) -> None:
        """Track open tickets so human activity feeds the idle timer.

        ``setdefault`` keeps the fresher in-memory value when this runs again on
        a reconnect, so re-seeding never rewinds a ticket's idle clock.
        """
        for ticket in await self.store.get_open_tickets():
            channel_id = ticket.get("channel_id")
            if not channel_id:
                continue
            self._ticket_channels.add(channel_id)
            self._last_activity.setdefault(channel_id, ticket.get("last_activity") or utcnow())

    async def _register_panel_views(self) -> None:
        """(Re)build one persistent panel view per configured guild.

        Safe to call repeatedly: it replaces our registered instance for a guild
        and re-adds it, which overwrites the same ``custom_id`` entries rather
        than accumulating them. Each guild is processed in isolation so one
        malformed config cannot stop the others from registering.
        """
        for config in await self.store.all_configs():
            guild_id = config.get("guild_id")
            if not guild_id:
                continue
            try:
                previous = self._panel_views.get(guild_id)
                if previous is not None:
                    try:
                        self.bot.remove_view(previous)
                    except Exception:
                        pass
                view = TicketPanelView(self, guild_id, config_types(config))
                self._panel_views[guild_id] = view
                self.bot.add_view(view)
            except Exception:
                log.exception("Failed to register panel view for guild %s", guild_id)

    def _spawn(self, coro) -> None:
        """Run ``coro`` in the background, holding a reference until it finishes."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def cog_unload(self) -> None:
        try:
            if self.auto_close.is_running():
                self.auto_close.cancel()
        except Exception:
            pass
        for task in list(getattr(self, "_background_tasks", ())):
            try:
                task.cancel()
            except Exception:
                pass
        try:
            if self.control_view is not None:
                self.bot.remove_view(self.control_view)
        except Exception:
            pass
        for view in self._panel_views.values():
            try:
                self.bot.remove_view(view)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # small helpers
    # ------------------------------------------------------------------ #
    async def _config(self, guild_id: int) -> Optional[Dict[str, Any]]:
        return await self.store.get_config(guild_id)

    def _panel_embed(self, config: Dict[str, Any]) -> discord.Embed:
        panel = config.get("panel") or {}
        color = panel.get("color")
        if not isinstance(color, int):
            color = DEFAULT_PANEL_COLOR
        embed = discord.Embed(
            title=panel.get("title") or DEFAULT_PANEL_TITLE,
            description=panel.get("description") or DEFAULT_PANEL_DESCRIPTION,
            color=color,
        )
        footer = panel.get("footer")
        embed.set_footer(text=footer or DEFAULT_PANEL_FOOTER)
        if panel.get("image"):
            embed.set_image(url=panel["image"])
        return embed

    def _intro_embed(self, ticket: Dict[str, Any]) -> discord.Embed:
        number = ticket.get("number", 0)
        embed = discord.Embed(
            title=f"Ticket #{int(number):04d}" if isinstance(number, int) else f"Ticket {number}",
            color=priority_color(ticket.get("priority")),
            # Mongo returns naive UTC datetimes; discord.py would treat a naive one
            # as local time, so normalise to an aware UTC datetime first.
            timestamp=as_utc(ticket.get("created_at")) or utcnow(),
        )
        embed.add_field(name="Opened by", value=f"<@{ticket.get('owner_id')}>", inline=True)
        embed.add_field(name="Type", value=ticket.get("type_label") or ticket.get("type") or "-", inline=True)
        embed.add_field(
            name="Priority",
            value=f"{priority_emoji(ticket.get('priority'))} {priority_label(ticket.get('priority'))}",
            inline=True,
        )
        claimed = ticket.get("claimed_by")
        embed.add_field(name="Claimed by", value=f"<@{claimed}>" if claimed else "Unclaimed", inline=True)
        embed.add_field(name="Status", value=str(ticket.get("status", "open")).title(), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="Subject", value=(ticket.get("subject") or "-")[:1024], inline=False)
        description = (ticket.get("description") or "-")[:1024]
        embed.add_field(name="Description", value=description, inline=False)
        embed.set_footer(text=f"Ticket #{int(number):04d}" if isinstance(number, int) else "Ticket")
        return embed

    def _transcript_embed(self, meta: Dict[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=f"\U0001F4C4 Transcript \u2014 Ticket #{meta.get('number')}",
            color=TRANSCRIPT_COLOR,
            timestamp=utcnow(),
        )
        embed.add_field(name="Opened by", value=meta.get("owner") or "-", inline=True)
        embed.add_field(name="Type", value=meta.get("type") or "-", inline=True)
        embed.add_field(name="Priority", value=meta.get("priority") or "-", inline=True)
        embed.add_field(name="Claimed by", value=meta.get("claimed_by") or "Unclaimed", inline=True)
        embed.add_field(name="Closed by", value=meta.get("closed_by") or "-", inline=True)
        embed.add_field(name="Messages", value=str(meta.get("message_count", 0)), inline=True)
        embed.add_field(name="Subject", value=(meta.get("subject") or "-")[:1024], inline=False)
        if meta.get("reason"):
            embed.add_field(name="Close reason", value=str(meta["reason"])[:1024], inline=False)
        return embed

    async def _send_log(
        self, guild: discord.Guild, config: Optional[Dict[str, Any]], embed: discord.Embed
    ) -> None:
        if not config:
            return
        channel_id = config.get("log_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except Exception:
            log.exception("Failed to send ticket log for guild %s", guild.id)

    def _staff_mentions(self, config: Dict[str, Any], guild: discord.Guild) -> str:
        mentions: List[str] = []
        for role_id in config.get("staff_role_ids") or []:
            role = guild.get_role(int(role_id))
            if role and not role.is_default():
                mentions.append(role.mention)
        return " ".join(mentions)

    def _guild_of(self, channel_id: int) -> Optional[discord.Guild]:
        channel = self.bot.get_channel(channel_id)
        return getattr(channel, "guild", None)

    # ------------------------------------------------------------------ #
    # the single setup command
    # ------------------------------------------------------------------ #
    @ticket.command(name="setup", description="Set up or update the ticket system")
    @app_commands.describe(
        panel_channel="Channel where the ticket panel (open buttons) is posted",
        category="Category where ticket channels are created (default: 'Tickets')",
        staff_roles="Staff roles that can manage tickets (comma-separated mentions or IDs)",
        transcript_channel="Channel that receives closed-ticket transcripts (default: panel channel)",
        log_channel="Channel that receives ticket event logs (default: panel channel)",
        title="Panel embed title",
        description="Panel embed description",
        color="Panel embed colour (hex, e.g. #2f3136)",
        footer="Panel embed footer text",
        image="Panel embed image URL",
        ping_staff="Ping the staff roles when a ticket is opened",
        auto_close_hours="Auto-close tickets after this many idle hours (0 disables)",
        types="Replace the panel types. Format: Label:emoji:description; Label2:emoji2:description2",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        panel_channel: discord.TextChannel,
        category: Optional[discord.CategoryChannel] = None,
        staff_roles: Optional[str] = None,
        transcript_channel: Optional[discord.TextChannel] = None,
        log_channel: Optional[discord.TextChannel] = None,
        title: Optional[str] = None,
        description: Optional[str] = None,
        color: Optional[str] = None,
        footer: Optional[str] = None,
        image: Optional[str] = None,
        ping_staff: Optional[bool] = None,
        auto_close_hours: Optional[app_commands.Range[int, 0, 8760]] = None,
        types: Optional[str] = None,
    ) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return

        if interaction.guild is None:
            await interaction.followup.send("This can only be used in a server.", ephemeral=True)
            return
        if not self.store.available:
            await interaction.followup.send(
                "The database is unavailable right now, so the ticket system cannot be set up.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        existing = await self._config(guild.id) or {}

        # --- resolve category (create if needed) ---
        if category is None:
            category = discord.utils.get(guild.categories, name="Tickets")
            if category is None:
                try:
                    category = await guild.create_category(
                        "Tickets",
                        overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
                        reason="Ticket system setup",
                    )
                except discord.HTTPException as exc:
                    await interaction.followup.send(
                        f"I couldn't create the ticket category ({exc.status}).", ephemeral=True
                    )
                    return

        # --- staff roles ---
        if staff_roles is not None:
            parsed_roles = parse_staff_roles(guild, staff_roles)
            staff_role_ids = [r.id for r in parsed_roles]
        else:
            staff_role_ids = list(existing.get("staff_role_ids") or [])

        # --- types ---
        if types:
            parsed_types = _parse_types_param(types, config_types(existing) if existing else default_types())
        elif existing.get("types"):
            parsed_types = config_types(existing)
        else:
            parsed_types = default_types()

        # --- panel fields ---
        existing_panel = existing.get("panel") or {}
        new_color = parse_color(color)
        panel_cfg = {
            "title": title or existing_panel.get("title") or DEFAULT_PANEL_TITLE,
            "description": description or existing_panel.get("description") or DEFAULT_PANEL_DESCRIPTION,
            "color": new_color if new_color is not None else existing_panel.get("color", DEFAULT_PANEL_COLOR),
            "footer": footer if footer is not None else existing_panel.get("footer", DEFAULT_PANEL_FOOTER),
            "image": image if image is not None else existing_panel.get("image", ""),
        }

        defaults = build_config_defaults()
        config: Dict[str, Any] = {
            **defaults,
            **{k: v for k, v in existing.items() if k not in ("_id",)},
            "guild_id": guild.id,
            "category_id": category.id,
            "staff_role_ids": staff_role_ids,
            "types": [t.to_dict() for t in parsed_types],
            "panel": panel_cfg,
            "transcript_channel_id": (transcript_channel.id if transcript_channel else existing.get("transcript_channel_id") or panel_channel.id),
            "log_channel_id": (log_channel.id if log_channel else existing.get("log_channel_id") or panel_channel.id),
            "ping_staff": ping_staff if ping_staff is not None else existing.get("ping_staff", True),
            "auto_close_hours": (
                int(auto_close_hours)
                if auto_close_hours is not None
                else _safe_int(existing.get("auto_close_hours"), DEFAULT_AUTO_CLOSE_HOURS)
            ),
            "panel_channel_id": panel_channel.id,
        }

        # --- build + register the panel view ---
        view = TicketPanelView(self, guild.id, parsed_types)
        old_view = self._panel_views.get(guild.id)
        if old_view is not None:
            try:
                self.bot.remove_view(old_view)
            except Exception:
                pass
        self._panel_views[guild.id] = view
        try:
            self.bot.add_view(view)
        except Exception:
            log.exception("Failed to register new panel view for guild %s", guild.id)

        # --- post or edit the panel message ---
        embed = self._panel_embed(config)

        # If the panel used to live in a different channel, remove the stale copy
        # so re-running setup never leaves two panels behind.
        old_message_id = _safe_int(existing.get("panel_message_id"), 0)
        old_channel_id = _safe_int(existing.get("panel_channel_id"), 0)
        if old_message_id and old_channel_id and old_channel_id != panel_channel.id:
            old_channel = guild.get_channel(old_channel_id)
            if old_channel is not None:
                try:
                    stale = await old_channel.fetch_message(old_message_id)
                    await stale.delete()
                except Exception:
                    pass

        message = None
        if old_message_id and (not old_channel_id or old_channel_id == panel_channel.id):
            try:
                message = await panel_channel.fetch_message(old_message_id)
            except Exception:
                message = None
        try:
            if message is not None:
                await message.edit(embed=embed, view=view)
            else:
                message = await panel_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I don't have permission to post in {panel_channel.mention}.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            # Almost always a bad ``image`` URL or an emoji Discord rejected.
            log.exception("Failed to post ticket panel for guild %s", guild.id)
            await interaction.followup.send(
                f"I couldn't post the panel ({exc.status}). Please check the `image` URL and "
                "`types` emojis, then try again.",
                ephemeral=True,
            )
            return

        config["panel_message_id"] = message.id
        if not await self.store.save_config(guild.id, config):
            await interaction.followup.send(
                "The panel was posted, but I couldn't save the configuration. Please re-run setup.",
                ephemeral=True,
            )
            return

        notes: List[str] = []
        if staff_roles is not None and not staff_role_ids:
            notes.append("No valid staff roles were found in `staff_roles`.")
        if color is not None and new_color is None:
            notes.append(f"`color` {color!r} wasn't a valid hex colour, so the previous colour was kept.")

        role_display = ", ".join(f"<@&{rid}>" for rid in staff_role_ids) or "Administrators only"
        type_display = ", ".join(f"{t.emoji} {t.label}".strip() for t in parsed_types)
        auto_line = (
            f"**Auto-close:** {config['auto_close_hours']}h idle"
            if config["auto_close_hours"]
            else "**Auto-close:** disabled"
        )
        description = (
            f"**Panel:** {message.jump_url}\n"
            f"**Category:** {category.mention}\n"
            f"**Staff roles:** {role_display}\n"
            f"**Transcripts:** <#{config['transcript_channel_id']}>\n"
            f"**Logs:** <#{config['log_channel_id']}>\n"
            f"**Types:** {type_display}\n"
            f"{auto_line}"
        )
        if notes:
            description += "\n\n\u26A0\uFE0F " + " ".join(notes)
        await interaction.followup.send(
            embed=discord.Embed(
                title="\u2705 Ticket system configured",
                color=SUCCESS_COLOR,
                description=description,
            ).set_footer(text="Re-run /ticket setup any time to change these settings."),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    # opening tickets
    # ------------------------------------------------------------------ #
    async def handle_open(self, interaction: discord.Interaction, type_key: str) -> None:
        if interaction.guild is None:
            await safe_reply(interaction, "Tickets can only be opened in a server.", ephemeral=True)
            return
        if not self.store.available:
            await safe_reply(interaction, "The ticket system is temporarily unavailable.", ephemeral=True)
            return

        guild = interaction.guild
        config = await self._config(guild.id)
        if not config:
            await safe_reply(
                interaction,
                "The ticket system has not been set up yet. Please ask an administrator to run `/ticket setup`.",
                ephemeral=True,
            )
            return
        ticket_type = find_type(config, type_key)
        if ticket_type is None:
            await safe_reply(interaction, "That ticket type is no longer available.", ephemeral=True)
            return

        existing = await self.store.get_open_ticket_for_user(guild.id, interaction.user.id)
        if existing:
            channel = guild.get_channel(existing.get("channel_id"))
            where = channel.mention if channel else "an existing ticket"
            await safe_reply(
                interaction, f"You already have an open ticket: {where}. Please use that one.", ephemeral=True
            )
            return

        sent = await send_modal(
            interaction, OpenTicketModal(self, guild.id, ticket_type.key, ticket_type.label)
        )
        if not sent:
            await safe_reply(
                interaction,
                "Sorry, I couldn't open the ticket form. Please try again.",
                ephemeral=True,
            )

    async def create_ticket(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        type_key: str,
        subject: str,
        description: str,
    ) -> None:
        await safe_defer(interaction, ephemeral=True)

        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            await safe_reply(interaction, "Something went wrong. Please try again.", ephemeral=True)
            return

        config = await self._config(guild.id)
        if not config:
            await safe_reply(interaction, "The ticket system is not configured.", ephemeral=True)
            return
        ticket_type = find_type(config, type_key)
        if ticket_type is None:
            await safe_reply(interaction, "That ticket type is no longer available.", ephemeral=True)
            return

        # Authoritative duplicate check (the one before the modal is best-effort).
        existing = await self.store.get_open_ticket_for_user(guild.id, interaction.user.id)
        if existing:
            channel = guild.get_channel(existing.get("channel_id"))
            where = channel.mention if channel else "an existing ticket"
            await safe_reply(interaction, f"You already have an open ticket: {where}.", ephemeral=True)
            return

        category = guild.get_channel(config.get("category_id"))
        if not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name="Tickets")
        if category is None:
            try:
                category = await guild.create_category(
                    "Tickets",
                    overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
                    reason="Ticket system",
                )
            except discord.HTTPException:
                await safe_reply(interaction, "I can't create the ticket category.", ephemeral=True)
                return

        number = await self.store.next_number(guild.id)
        if number is None:
            await safe_reply(interaction, "I couldn't allocate a ticket number. Try again shortly.", ephemeral=True)
            return

        member = interaction.user
        priority = normalize_priority(ticket_type.priority)

        # Build overwrites: inherit the category, then tighten.
        overwrites = dict(category.overwrites)
        overwrites[guild.default_role] = discord.PermissionOverwrite(view_channel=False)
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
        for role_id in config.get("staff_role_ids") or []:
            role = guild.get_role(_safe_int(role_id, -1))
            # ``getattr(role, "is_default", ...)`` keeps this safe for fakes/tests;
            # @everyone must never be granted access to a ticket channel.
            if role is not None and not (getattr(role, "is_default", lambda: False)()):
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )

        try:
            channel = await guild.create_text_channel(
                name=f"ticket-{number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Ticket #{number:04d} | {ticket_type.label} | Owner: {member.id}",
                reason=f"Ticket opened by {member} ({member.id})",
            )
        except discord.Forbidden:
            await safe_reply(
                interaction,
                "I don't have permission to create ticket channels. Please contact an administrator.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.exception("Failed to create ticket channel for guild %s", guild.id)
            await safe_reply(
                interaction, f"I couldn't create the ticket channel ({exc.status}).", ephemeral=True
            )
            return

        ticket: Dict[str, Any] = {
            "guild_id": guild.id,
            "channel_id": channel.id,
            "owner_id": member.id,
            "number": number,
            "type": ticket_type.key,
            "type_label": ticket_type.label,
            "status": "open",
            "claimed_by": None,
            "priority": priority,
            "subject": subject or "(no subject)",
            "description": description or "",
            "added_users": [],
            "created_at": utcnow(),
            "last_activity": utcnow(),
            "warned_at": None,
            "closed_at": None,
            "closed_by": None,
            "close_reason": None,
            "message_id": None,
        }

        # Persist *before* posting the control bar. Otherwise a staff member who
        # clicks a button in the moment between the intro appearing and the
        # document being written would be told "this isn't a ticket channel".
        if not await self.store.create_ticket(ticket):
            # Without a stored document every button on the new channel would
            # report "this isn't a ticket channel" forever. Clean up the orphan.
            log.error("Could not persist ticket for channel %s; removing the channel", channel.id)
            try:
                await channel.delete(reason="Ticket could not be saved")
            except Exception:
                pass
            await safe_reply(
                interaction,
                "I couldn't save your ticket. Please try again in a moment.",
                ephemeral=True,
            )
            return

        self._ticket_channels.add(channel.id)
        self._last_activity[channel.id] = ticket["last_activity"]
        self._last_activity_write[channel.id] = time.monotonic()

        content = None
        staff_mentions = self._staff_mentions(config, guild)
        if config.get("ping_staff", True) and staff_mentions:
            content = f"{staff_mentions} \u2014 new ticket from {member.mention}"

        try:
            intro = await channel.send(
                content=content,
                embed=self._intro_embed(ticket),
                view=self.control_view,
                allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
            )
            ticket["message_id"] = intro.id
            await self.store.update_ticket(channel.id, {"message_id": intro.id})
        except discord.HTTPException:
            # The ticket still exists (staff can manage it and the owner can
            # write); a missing intro is recoverable, so log and carry on.
            log.exception("Failed to post ticket intro for channel %s", channel.id)

        await self._send_log(
            guild,
            config,
            discord.Embed(
                title="\U0001F4E5 Ticket opened",
                color=SUCCESS_COLOR,
                timestamp=utcnow(),
                description=(
                    f"**Ticket:** {channel.mention} (#{number:04d})\n"
                    f"**User:** {member.mention} (`{member.id}`)\n"
                    f"**Type:** {ticket_type.label}\n"
                    f"**Subject:** {subject}"
                ),
            ),
        )

        await safe_reply(interaction, f"Your ticket has been created: {channel.mention}", ephemeral=True)

    # ------------------------------------------------------------------ #
    # staff controls
    # ------------------------------------------------------------------ #
    async def _resolve_ticket(
        self, interaction: discord.Interaction, *, require_staff: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch the ticket for this channel + config, replying on failure.

        Callers that can should ``safe_defer`` *before* calling this, because it
        performs up to two database round trips and Discord only gives a handler
        three seconds to acknowledge.
        """
        channel = interaction.channel
        if channel is None:
            await safe_reply(interaction, "This can only be used in a ticket channel.", ephemeral=True)
            return None
        ticket = await self.store.get_ticket(channel.id)
        if not ticket:
            await safe_reply(interaction, "This isn't a ticket channel.", ephemeral=True)
            return None
        if require_staff:
            guild = interaction.guild
            if guild is None:
                await safe_reply(interaction, "This can only be used in a server.", ephemeral=True)
                return None
            config = await self._config(guild.id)
            if not member_is_staff(interaction.user, config):
                await safe_reply(interaction, "Only staff can use this.", ephemeral=True)
                return None
        return ticket

    async def handle_claim(self, interaction: discord.Interaction) -> None:
        # Acknowledge first: everything below may touch the database.
        await safe_defer(interaction, ephemeral=False)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        if ticket.get("status") == "closed":
            await safe_reply(interaction, "This ticket is already closed.", ephemeral=True)
            return

        user = interaction.user
        if ticket.get("claimed_by") == user.id:
            claimed_by = None
            status = "open"
            verb = "released"
        else:
            claimed_by = user.id
            status = "claimed"
            verb = "claimed"

        await self.store.update_ticket(interaction.channel.id, {"claimed_by": claimed_by, "status": status})
        ticket["claimed_by"] = claimed_by
        ticket["status"] = status

        await safe_edit(interaction, embed=self._intro_embed(ticket), view=self.control_view)

        config = await self._config(interaction.guild.id)
        await self._send_log(
            interaction.guild,
            config,
            discord.Embed(
                title=f"\U0001F64B Ticket {verb}",
                color=INFO_COLOR,
                timestamp=utcnow(),
                description=f"**Ticket:** {interaction.channel.mention}\n**By:** {user.mention}",
            ),
        )

    async def handle_priority(self, interaction: discord.Interaction, value: str) -> None:
        await safe_defer(interaction, ephemeral=False)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        value = normalize_priority(value)
        await self.store.update_ticket(interaction.channel.id, {"priority": value})
        ticket["priority"] = value

        await safe_edit(interaction, embed=self._intro_embed(ticket), view=self.control_view)

        config = await self._config(interaction.guild.id)
        await self._send_log(
            interaction.guild,
            config,
            discord.Embed(
                title="\U0001F3AF Priority updated",
                color=priority_color(value),
                timestamp=utcnow(),
                description=(
                    f"**Ticket:** {interaction.channel.mention}\n"
                    f"**Priority:** {priority_emoji(value)} {priority_label(value)}\n"
                    f"**By:** {interaction.user.mention}"
                ),
            ),
        )

    async def handle_manage_users(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        await safe_reply(
            interaction,
            "Add or remove members from this ticket:",
            view=ManageUsersView(self, interaction.channel.id),
            ephemeral=True,
        )

    async def build_remove_user_view(self, channel_id: int) -> Optional[RemoveUserView]:
        ticket = await self.store.get_ticket(channel_id)
        if not ticket:
            return None
        added = ticket.get("added_users") or []
        if not added:
            return None
        guild = self._guild_of(channel_id)
        if guild is None:
            return None
        options: List[discord.SelectOption] = []
        for user_id in added[:25]:
            member = guild.get_member(int(user_id))
            label = member.display_name if member else str(user_id)
            options.append(
                discord.SelectOption(label=label[:100], value=str(user_id), description=str(user_id))
            )
        if not options:
            return None
        return RemoveUserView(self, channel_id, options)

    async def add_user(self, interaction: discord.Interaction, user_id: int) -> None:
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return

        guild = interaction.guild
        channel = interaction.channel
        member = guild.get_member(int(user_id))
        if member is None:
            await safe_reply(interaction, "That member is no longer in the server.", ephemeral=True)
            return
        if member.id == ticket.get("owner_id"):
            await safe_reply(interaction, "That member already owns this ticket.", ephemeral=True)
            return

        try:
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                reason=f"Added to ticket by {interaction.user}",
            )
        except discord.Forbidden:
            await safe_reply(interaction, "I don't have permission to update channel access.", ephemeral=True)
            return

        added = set(int(u) for u in (ticket.get("added_users") or []))
        added.add(member.id)
        await self.store.update_ticket(channel.id, {"added_users": list(added)})
        await safe_reply(interaction, f"Added {member.mention} to the ticket.", ephemeral=True)

    async def remove_user(self, interaction: discord.Interaction, user_id: int) -> None:
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return

        if int(user_id) == ticket.get("owner_id"):
            await safe_reply(interaction, "You can't remove the ticket owner.", ephemeral=True)
            return

        guild = interaction.guild
        channel = interaction.channel
        member = guild.get_member(int(user_id))
        if member is not None:
            try:
                await channel.set_permissions(
                    member, overwrite=None, reason=f"Removed from ticket by {interaction.user}"
                )
            except discord.Forbidden:
                await safe_reply(interaction, "I don't have permission to update channel access.", ephemeral=True)
                return

        added = [int(u) for u in (ticket.get("added_users") or []) if int(u) != int(user_id)]
        await self.store.update_ticket(channel.id, {"added_users": added})
        await safe_reply(interaction, f"Removed <@{user_id}> from the ticket.", ephemeral=True)

    async def handle_rename(self, interaction: discord.Interaction) -> None:
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        sent = await send_modal(interaction, RenameModal(self, interaction.channel.id))
        if not sent:
            await safe_reply(
                interaction, "Sorry, I couldn't open the rename form. Please try again.", ephemeral=True
            )

    async def apply_rename(self, interaction: discord.Interaction, new_name: str) -> None:
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        name = sanitize_channel_name(new_name, fallback=f"ticket-{ticket.get('number', 0):04d}")
        try:
            await interaction.channel.edit(name=name, reason=f"Renamed by {interaction.user}")
        except discord.HTTPException as exc:
            await safe_reply(interaction, f"I couldn't rename the channel ({exc.status}).", ephemeral=True)
            return
        await safe_reply(interaction, f"Renamed to `{name}`.", ephemeral=True)

    # ------------------------------------------------------------------ #
    # transcripts
    # ------------------------------------------------------------------ #
    def _meta(self, ticket: Dict[str, Any], guild: discord.Guild, closed_by: Optional[str], reason: Optional[str]) -> Dict[str, Any]:
        owner = guild.get_member(ticket.get("owner_id"))
        claimed = guild.get_member(ticket.get("claimed_by")) if ticket.get("claimed_by") else None
        number = ticket.get("number")
        return {
            "number": f"{int(number):04d}" if isinstance(number, int) else number,
            "type": ticket.get("type_label") or ticket.get("type"),
            "guild": guild.name,
            "owner": f"{owner} ({owner.id})" if owner else str(ticket.get("owner_id")),
            "claimed_by": str(claimed) if claimed else "Unclaimed",
            "priority": f"{priority_label(ticket.get('priority'))}",
            "subject": ticket.get("subject"),
            "created_at": human_dt(ticket.get("created_at")),
            "closed_at": human_dt(utcnow()) if closed_by else "",
            "closed_by": closed_by or "",
            "reason": reason or "",
            "message_count": 0,
        }

    async def _send_transcript(
        self,
        guild: discord.Guild,
        config: Dict[str, Any],
        channel: discord.TextChannel,
        ticket: Dict[str, Any],
        closed_by: Optional[str],
        reason: Optional[str],
        *,
        dm_owner: bool,
    ) -> Optional[str]:
        try:
            messages = await collect_messages(channel, limit=TRANSCRIPT_MESSAGE_LIMIT)
        except Exception:
            # Missing history permission or a transient API error must not stop a
            # ticket from closing - fall back to a metadata-only transcript.
            log.exception("Failed to read history for transcript in channel %s", channel.id)
            messages = []
        meta = self._meta(ticket, guild, closed_by, reason)
        meta["message_count"] = len(messages)
        number = ticket.get("number")
        number_label = f"{int(number):04d}" if isinstance(number, int) else str(number)

        transcript_channel_id = config.get("transcript_channel_id") or config.get("panel_channel_id")
        transcript_channel = guild.get_channel(transcript_channel_id) if transcript_channel_id else None
        jump_url: Optional[str] = None
        if transcript_channel is not None:
            try:
                html_file, txt_file = build_files(messages, meta, number_label)
                msg = await transcript_channel.send(
                    embed=self._transcript_embed(meta), files=[html_file, txt_file]
                )
                jump_url = msg.jump_url
            except Exception:
                log.exception("Failed to post transcript for ticket #%s", number_label)

        if dm_owner:
            owner = guild.get_member(ticket.get("owner_id"))
            if owner is not None:
                try:
                    html_file, txt_file = build_files(messages, meta, number_label)
                    await owner.send(
                        content=f"Here is the transcript for your ticket **#{number_label}** in {guild.name}.",
                        files=[html_file, txt_file],
                    )
                except discord.Forbidden:
                    pass
                except Exception:
                    log.exception("Failed to DM transcript for ticket #%s", number_label)

        return jump_url

    async def handle_transcript(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction, require_staff=True)
        if ticket is None:
            return
        config = await self._config(interaction.guild.id)
        jump = await self._send_transcript(
            interaction.guild,
            config or {},
            interaction.channel,
            ticket,
            None,
            None,
            dm_owner=False,
        )
        if jump:
            await safe_reply(interaction, f"Transcript created: {jump}", ephemeral=True)
        else:
            await safe_reply(
                interaction,
                "I couldn't post a transcript. Please check the transcript/log channel permissions.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------ #
    # closing
    # ------------------------------------------------------------------ #
    async def handle_close_button(self, interaction: discord.Interaction) -> None:
        # Defer before the two database reads. The ephemeral defer creates a
        # private placeholder that the confirm view is then shown in, so the
        # ticket's intro message is never touched.
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction)
        if ticket is None:
            return
        config = await self._config(interaction.guild.id)
        is_owner = interaction.user.id == ticket.get("owner_id")
        if not is_owner and not member_is_staff(interaction.user, config):
            await safe_reply(
                interaction, "Only the ticket owner or staff can close this ticket.", ephemeral=True
            )
            return
        if ticket.get("status") == "closed":
            await safe_reply(interaction, "This ticket is already closed.", ephemeral=True)
            return
        await safe_reply(
            interaction,
            "Are you sure you want to close this ticket? A transcript will be saved and the channel deleted.",
            view=CloseConfirmView(self, interaction.channel.id),
            ephemeral=True,
        )

    async def finalize_close(self, interaction: discord.Interaction, reason: Optional[str]) -> None:
        # Acknowledge first - the checks and the close itself touch the database.
        await safe_defer(interaction, ephemeral=True)
        ticket = await self._resolve_ticket(interaction)
        if ticket is None:
            return
        channel_id = interaction.channel.id
        if ticket.get("status") == "closed" or channel_id in self._closing:
            await safe_reply(interaction, "This ticket is already closed.", ephemeral=True)
            return
        config = await self._config(interaction.guild.id)
        is_owner = interaction.user.id == ticket.get("owner_id")
        if not is_owner and not member_is_staff(interaction.user, config):
            await safe_reply(interaction, "Only the ticket owner or staff can close this ticket.", ephemeral=True)
            return

        # Guard against a double "Confirm" producing two transcripts and two
        # delete tasks for the same channel.
        self._closing.add(channel_id)
        try:
            jump = await self._perform_close(
                interaction.guild,
                config or {},
                interaction.channel,
                ticket,
                closed_by=str(interaction.user),
                closed_by_id=interaction.user.id,
                reason=reason,
                dm_owner=True,
            )
        finally:
            self._closing.discard(channel_id)
        message = "Ticket closed. The channel will be deleted shortly."
        if jump:
            message += f"\n[View transcript]({jump})"
        await safe_reply(interaction, message, ephemeral=True)

    async def _perform_close(
        self,
        guild: discord.Guild,
        config: Dict[str, Any],
        channel: discord.TextChannel,
        ticket: Dict[str, Any],
        closed_by: str,
        closed_by_id: Optional[int],
        reason: Optional[str],
        *,
        dm_owner: bool,
    ) -> Optional[str]:
        jump = await self._send_transcript(guild, config, channel, ticket, closed_by, reason, dm_owner=dm_owner)

        await self.store.close_ticket(
            channel.id,
            {
                "closed_by": closed_by_id,
                "close_reason": reason,
                "last_activity": utcnow(),
            },
        )
        self._ticket_channels.discard(channel.id)
        self._last_activity.pop(channel.id, None)
        self._last_activity_write.pop(channel.id, None)

        await self._send_log(
            guild,
            config,
            discord.Embed(
                title="\U0001F512 Ticket closed",
                color=ERROR_COLOR,
                timestamp=utcnow(),
                description=(
                    f"**Ticket:** #{ticket.get('number')} \u2014 {channel.name}\n"
                    f"**Closed by:** {closed_by}\n"
                    f"**Reason:** {reason or 'No reason provided'}"
                    + (f"\n**Transcript:** [open]({jump})" if jump else "")
                ),
            ),
        )

        notice = "\U0001F512 This ticket has been closed. The channel will be deleted in a few seconds."
        try:
            await channel.send(notice)
        except Exception:
            pass
        self._spawn(self._delete_channel_later(channel))
        return jump

    async def _delete_channel_later(self, channel: discord.TextChannel) -> None:
        await asyncio.sleep(CLOSE_DELETE_DELAY_SECONDS)
        try:
            await channel.delete(reason="Ticket closed")
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # listeners
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready fires again after a reconnect. If MongoDB was unavailable when
        # the cog first loaded, no panel views were registered; retry here so the
        # panel buttons keep working without needing a manual reload.
        if self.control_view is not None:
            try:
                self.bot.add_view(self.control_view)
            except Exception:
                pass
        try:
            await self._register_panel_views()
            await self._seed_activity_cache()
        except Exception:
            log.exception("Failed to refresh ticket state on ready")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        channel_id = message.channel.id
        if channel_id not in self._ticket_channels:
            return
        now = utcnow()
        self._last_activity[channel_id] = now
        last_write = self._last_activity_write.get(channel_id, 0.0)
        if time.monotonic() - last_write >= ACTIVITY_WRITE_THROTTLE_SECONDS:
            self._last_activity_write[channel_id] = time.monotonic()
            try:
                # Clearing ``warned_at`` lets a ticket that goes quiet again after
                # a reply get a fresh auto-close warning.
                await self.store.update_ticket(channel_id, {"last_activity": now, "warned_at": None})
            except Exception:
                log.exception("Failed to persist ticket activity for channel %s", channel_id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        channel_id = channel.id
        ticket = await self.store.get_ticket(channel_id)
        if ticket is None:
            self._ticket_channels.discard(channel_id)
            self._last_activity.pop(channel_id, None)
            self._last_activity_write.pop(channel_id, None)
            return
        self._ticket_channels.discard(channel_id)
        self._last_activity.pop(channel_id, None)
        self._last_activity_write.pop(channel_id, None)
        if ticket.get("status") != "closed":
            await self.store.close_ticket(
                channel_id,
                {
                    "closed_by": self.bot.user.id if self.bot.user else None,
                    "close_reason": "Channel deleted",
                },
            )

    # ------------------------------------------------------------------ #
    # auto-close
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close(self) -> None:
        try:
            if not self.store.available:
                return
            for ticket in await self.store.get_open_tickets():
                await self._auto_close_one(ticket)
        except Exception:
            log.exception("Error while running ticket auto-close")

    @auto_close.before_loop
    async def _before_auto_close(self) -> None:
        await self.bot.wait_until_ready()

    async def _auto_close_one(self, ticket: Dict[str, Any]) -> None:
        channel_id = ticket.get("channel_id")
        guild = self.bot.get_guild(ticket.get("guild_id"))
        if guild is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            await self.store.close_ticket(channel_id, {"close_reason": "Channel not found"})
            self._ticket_channels.discard(channel_id)
            return

        hours = int(ticket.get("auto_close_hours") or 0)
        # Prefer the live config value if present.
        config = await self._config(guild.id) or {}
        hours = int(config.get("auto_close_hours", hours) or 0)
        if hours <= 0:
            return

        last = self._last_activity.get(channel_id) or ticket.get("last_activity") or ticket.get("created_at")
        if not isinstance(last, datetime):
            return
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        idle = utcnow() - last
        warn_threshold = timedelta(hours=max(hours - AUTO_CLOSE_WARN_BEFORE_HOURS, 0))
        close_threshold = timedelta(hours=hours)

        if idle >= close_threshold:
            try:
                await self._perform_close(
                    guild,
                    config,
                    channel,
                    ticket,
                    closed_by="Auto-close (inactivity)",
                    closed_by_id=None,
                    reason=f"No activity for {hours}h",
                    dm_owner=True,
                )
            except Exception:
                log.exception("Auto-close failed for ticket #%s", ticket.get("number"))
        elif idle >= warn_threshold and not ticket.get("warned_at"):
            warned_at = utcnow()
            await self.store.update_ticket(channel_id, {"warned_at": warned_at})
            try:
                await channel.send(
                    f"\u26A0\uFE0F This ticket has been inactive and will be auto-closed "
                    f"<t:{int((warned_at + (close_threshold - idle)).timestamp())}:R> "
                    f"unless there is new activity."
                )
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TicketSystem(bot))
