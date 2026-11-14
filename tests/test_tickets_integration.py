"""Integration tests for the ticket system, driven against fake Discord objects.

These exercise the *real* cog logic end to end - ``/ticket setup``, opening a
ticket, every staff control, and closing - without needing a Discord connection
or a MongoDB server. The store is an in-memory stand-in that implements exactly
the async interface the cog calls, and the Discord types are thin fakes that
record what was sent, edited, created and deleted.

This is where bugs that unit tests of pure helpers cannot see show up: interaction
timing (defer then reply), which channel a message went to, whether a view was
attached to the edited message, and whether state actually landed in the store.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord

import cogs.tickets.ticket_system as ts
from cogs.tickets.views import (
    CloseConfirmView,
    ManageUsersView,
    TicketControlView,
)


# --------------------------------------------------------------------------- #
# Discord fakes
# --------------------------------------------------------------------------- #
class FakeRole:
    def __init__(self, rid, name="role", default=False):
        self.id = rid
        self.name = name
        self.mention = f"<@&{rid}>"
        self._default = default

    def is_default(self):
        return self._default


class FakeMember:
    def __init__(self, mid, name="user", admin=True, bot=False):
        self.id = mid
        self.name = name
        self.display_name = name
        self.mention = f"<@{mid}>"
        self.bot = bot
        self.roles = []
        self.guild_permissions = SimpleNamespace(
            administrator=admin, manage_guild=False, manage_channels=False
        )
        self.dms = []

    async def send(self, content=None, files=None, **kwargs):
        self.dms.append({"content": content, "files": files})


class FakeMessage:
    _seq = 1000

    def __init__(self, content=None, embed=None, author=None):
        FakeMessage._seq += 1
        self.id = FakeMessage._seq
        self.content = content
        self.embeds = [embed] if embed else []
        self.author = author or FakeMember(999, "bot", bot=True)
        self.attachments = []
        self.created_at = discord.utils.utcnow()
        self.type = discord.MessageType.default
        self.jump_url = f"https://discord.test/{self.id}"
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("content") is not None:
            self.content = kwargs["content"]
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]
        return self

    async def delete(self, reason=None):
        self.deleted = True


class FakeChannel:
    _seq = 0

    def __init__(self, guild, name, cid=None):
        FakeChannel._seq += 1
        self.id = cid or FakeChannel._seq
        self.guild = guild
        self.name = name
        self.mention = f"<#{self.id}>"
        self.messages = []
        self.sent = []
        self.perms = {}
        self.deleted = False
        self.edits = []

    async def send(self, content=None, embed=None, view=None, files=None,
                   allowed_mentions=None, **kwargs):
        message = FakeMessage(content=content, embed=embed)
        self.messages.append(message)
        self.sent.append(
            {"content": content, "embed": embed, "view": view, "files": files}
        )
        return message

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.name = kwargs.get("name", self.name)
        if kwargs.get("embed") is not None:
            self.messages.append(FakeMessage(embed=kwargs["embed"]))
        return self

    async def set_permissions(self, target, *, overwrite=discord.utils.MISSING, **kwargs):
        key = getattr(target, "id", target)
        if overwrite is discord.utils.MISSING:
            if kwargs:
                self.perms[key] = dict(kwargs)
            return
        if overwrite is None:
            self.perms.pop(key, None)
        else:
            self.perms[key] = overwrite

    async def delete(self, reason=None):
        self.deleted = True

    async def history(self, limit=None, oldest_first=True):
        for message in list(self.messages):
            yield message

    async def fetch_message(self, message_id):
        for message in self.messages:
            if message.id == int(message_id):
                return message
        raise discord.NotFound(SimpleNamespace(status=404, reason=""), "message")


class FakeCategory:
    def __init__(self, cid, name="Tickets"):
        self.id = cid
        self.name = name
        self.mention = f"<#{cid}>"
        self.overwrites = {}


class FakeGuild:
    def __init__(self, gid=1, name="Test Guild"):
        self.id = gid
        self.name = name
        self.default_role = FakeRole(gid, "@everyone", default=True)
        self.me = FakeMember(500, "Bot", admin=True, bot=True)
        self.channels = {}
        self.categories = []
        self.roles = {}
        self.members = {}
        self._next = 10

    def get_channel(self, channel_id):
        return self.channels.get(int(channel_id)) if channel_id else None

    def get_role(self, role_id):
        return self.roles.get(int(role_id))

    def get_member(self, member_id):
        return self.members.get(int(member_id)) if member_id else None

    async def create_category(self, name, overwrites=None, reason=None):
        category = FakeCategory(self._next, name)
        self._next += 1
        self.categories.append(category)
        return category

    async def create_text_channel(self, name, category=None, overwrites=None,
                                  topic=None, reason=None):
        channel = FakeChannel(self, name)
        self.channels[channel.id] = channel
        return channel


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, ephemeral=True, thinking=False):
        self._done = True
        self.interaction.defer_kwargs = {"ephemeral": ephemeral, "thinking": thinking}

    async def send_message(self, content=None, embed=None, ephemeral=True,
                           view=None, **kwargs):
        self._done = True
        self.interaction.responses.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral, "view": view}
        )

    async def send_modal(self, modal):
        self._done = True
        self.interaction.modal = modal


class FakeFollowup:
    def __init__(self, interaction):
        self.interaction = interaction

    async def send(self, content=None, embed=None, ephemeral=True, view=None,
                   files=None, **kwargs):
        self.interaction.followups.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral,
             "view": view, "files": files}
        )
        return FakeMessage(content=content)


class FakeClient:
    def __init__(self):
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    def remove_view(self, view):
        if view in self.views:
            self.views.remove(view)


class FakeInteraction:
    def __init__(self, guild, channel, user, type_=discord.InteractionType.component):
        self.guild = guild
        self.channel = channel
        self.user = user
        self.type = type_
        self.client = FakeClient()
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.responses = []
        self.followups = []
        self.modal = None
        self.edits = []
        self.defer_kwargs = None

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)
        return FakeMessage()

    async def original_response(self):
        return FakeMessage()


class FakeBot:
    def __init__(self, guild):
        self.guild = guild
        self.user = FakeMember(500, "Bot", bot=True)
        self.views = []

    def get_channel(self, channel_id):
        return self.guild.get_channel(channel_id)

    def get_guild(self, guild_id):
        return self.guild if guild_id == self.guild.id else None

    def add_view(self, view):
        self.views.append(view)

    def remove_view(self, view):
        if view in self.views:
            self.views.remove(view)


# --------------------------------------------------------------------------- #
# In-memory store implementing the cog's async interface
# --------------------------------------------------------------------------- #
class FakeStore:
    def __init__(self):
        self.configs = {}
        self.tickets = {}
        self.counters = {}
        self.available = True

    async def ensure_indexes(self):
        pass

    async def get_config(self, guild_id):
        return self.configs.get(guild_id)

    async def save_config(self, guild_id, data):
        self.configs[guild_id] = dict(data)
        return True

    async def all_configs(self):
        return list(self.configs.values())

    async def next_number(self, guild_id):
        self.counters[guild_id] = self.counters.get(guild_id, 0) + 1
        return self.counters[guild_id]

    async def create_ticket(self, document):
        self.tickets[document["channel_id"]] = dict(document)
        return True

    async def get_ticket(self, channel_id):
        return self.tickets.get(channel_id)

    async def get_open_ticket_for_user(self, guild_id, user_id):
        for ticket in self.tickets.values():
            if (ticket.get("guild_id") == guild_id
                    and ticket.get("owner_id") == user_id
                    and ticket.get("status") in ("open", "claimed")):
                return ticket
        return None

    async def get_open_tickets(self, guild_id=None):
        return [t for t in self.tickets.values()
                if t.get("status") in ("open", "claimed")]

    async def update_ticket(self, channel_id, data):
        if channel_id in self.tickets:
            self.tickets[channel_id].update(data)
            return True
        return False

    async def close_ticket(self, channel_id, data):
        if channel_id in self.tickets:
            self.tickets[channel_id].update(data)
            self.tickets[channel_id]["status"] = "closed"
            return True
        return False


# --------------------------------------------------------------------------- #
# Shared wiring
# --------------------------------------------------------------------------- #
def make_cog(guild, store=None):
    """Build a TicketSystem without running its async ``cog_load`` hook."""
    cog = ts.TicketSystem.__new__(ts.TicketSystem)
    cog.bot = FakeBot(guild)
    cog.store = store if store is not None else FakeStore()
    cog._panel_views = {}
    cog._ticket_channels = set()
    cog._last_activity = {}
    cog._last_activity_write = {}
    cog._background_tasks = set()
    cog._closing = set()
    cog.control_view = TicketControlView(cog)
    return cog


def make_config(guild, panel_channel, transcript_channel, log_channel):
    return {
        "guild_id": guild.id,
        "panel_channel_id": panel_channel.id,
        "category_id": None,
        "transcript_channel_id": transcript_channel.id,
        "log_channel_id": log_channel.id,
        "staff_role_ids": [],
        "types": [t.to_dict() for t in ts.default_types()],
        "panel": {
            "title": "Support",
            "description": "d",
            "color": 0x2F3136,
            "footer": "f",
            "image": "",
        },
        "ping_staff": True,
        "auto_close_hours": 24,
    }


def ticket_world():
    """A guild with a panel, transcript and log channel plus owner + staff."""
    guild = FakeGuild(1, "Test Guild")
    panel = FakeChannel(guild, "support")
    transcripts = FakeChannel(guild, "transcripts")
    logs = FakeChannel(guild, "logs")
    guild.channels[panel.id] = panel
    guild.channels[transcripts.id] = transcripts
    guild.channels[logs.id] = logs

    owner = FakeMember(77, "Owner", admin=False)
    staff = FakeMember(88, "Staff", admin=True)
    guild.members[owner.id] = owner
    guild.members[staff.id] = staff

    store = FakeStore()
    store.configs[guild.id] = make_config(guild, panel, transcripts, logs)
    return SimpleNamespace(
        guild=guild, panel=panel, transcripts=transcripts, logs=logs,
        owner=owner, staff=staff, store=store,
    )


def ticket_channel(world):
    return next(c for c in world.guild.channels.values()
                if c.name.startswith("ticket-"))


# --------------------------------------------------------------------------- #
# Lifecycle: open -> manage -> close
# --------------------------------------------------------------------------- #
def test_full_lifecycle():
    ts.CLOSE_DELETE_DELAY_SECONDS = 0  # don't leave a sleeping task behind

    async def scenario():
        world = ticket_world()
        guild, owner, staff, store = world.guild, world.owner, world.staff, world.store
        cog = make_cog(guild, store)

        # --- opening ------------------------------------------------------ #
        it_open = FakeInteraction(guild, world.panel, owner)
        await cog.handle_open(it_open, "report")
        assert it_open.modal is not None, "open modal not shown"
        assert it_open.modal.type_key == "report"

        it_submit = FakeInteraction(guild, world.panel, owner)
        await cog.create_ticket(it_submit, guild.id, "report",
                                "My subject", "My description")
        channel = ticket_channel(world)
        document = store.tickets[channel.id]
        assert channel.name == "ticket-0001"
        assert document["number"] == 1
        assert document["priority"] == "high", "report should default to High"
        assert document["message_id"], "intro message id was not persisted on the ticket"
        assert world.logs.sent[-1]["embed"].title.endswith("Ticket opened")

        # --- duplicate open blocked --------------------------------------- #
        it_dup = FakeInteraction(guild, world.panel, owner)
        await cog.handle_open(it_dup, "general")
        assert it_dup.modal is None and it_dup.responses
        assert "already have an open ticket" in it_dup.responses[-1]["content"]

        # --- claim / unclaim ---------------------------------------------- #
        it_claim = FakeInteraction(guild, channel, staff)
        await cog.handle_claim(it_claim)
        assert store.tickets[channel.id]["claimed_by"] == staff.id
        assert store.tickets[channel.id]["status"] == "claimed"
        # It must edit the clicked message, not open a new private one.
        assert it_claim.defer_kwargs == {"ephemeral": False, "thinking": False}
        assert it_claim.edits and it_claim.edits[-1].get("embed") is not None

        await cog.handle_claim(FakeInteraction(guild, channel, staff))
        assert store.tickets[channel.id]["claimed_by"] is None

        # --- priority ------------------------------------------------------ #
        await cog.handle_priority(FakeInteraction(guild, channel, staff), "urgent")
        assert store.tickets[channel.id]["priority"] == "urgent"

        # A non-staff user gets a private refusal and does not change anything.
        it_poor = FakeInteraction(guild, channel, owner)
        await cog.handle_priority(it_poor, "low")
        assert store.tickets[channel.id]["priority"] == "urgent"
        assert it_poor.followups[-1]["ephemeral"] is True

        # --- rename -------------------------------------------------------- #
        await cog.apply_rename(FakeInteraction(guild, channel, staff),
                               "Billing Issue!!")
        assert channel.name == "billing-issue"

        # --- add / remove member ------------------------------------------ #
        extra = FakeMember(99, "Extra")
        guild.members[extra.id] = extra
        await cog.add_user(FakeInteraction(guild, channel, staff), extra.id)
        assert extra.id in store.tickets[channel.id]["added_users"]
        assert extra.id in channel.perms

        remove_view = await cog.build_remove_user_view(channel.id)
        assert remove_view is not None
        assert len(remove_view.children[0].options) == 1

        await cog.remove_user(FakeInteraction(guild, channel, staff), extra.id)
        assert extra.id not in store.tickets[channel.id]["added_users"]
        assert extra.id not in channel.perms

        # --- on-demand transcript ------------------------------------------ #
        it_tr = FakeInteraction(guild, channel, staff)
        await cog.handle_transcript(it_tr)
        assert world.transcripts.sent, "no transcript posted"
        assert len(world.transcripts.sent[-1]["files"]) == 2
        assert "Transcript created" in it_tr.followups[-1]["content"]

        # --- close --------------------------------------------------------- #
        it_close = FakeInteraction(guild, channel, staff)
        await cog.handle_close_button(it_close)
        # The handler defers first, so the confirm view arrives on the deferred
        # placeholder (the first followup), never on the ticket's intro message.
        assert it_close.defer_kwargs == {"ephemeral": True, "thinking": True}
        assert it_close.responses == [], "close must not answer directly"
        assert it_close.followups[-1]["view"] is not None, "no confirm view"

        it_final = FakeInteraction(guild, channel, staff)
        await cog.finalize_close(it_final, "resolved")
        document = store.tickets[channel.id]
        assert document["status"] == "closed"
        assert document["close_reason"] == "resolved"
        assert any(s["embed"] and "Ticket closed" in s["embed"].title
                   for s in world.logs.sent), "close log missed"
        assert any(s["embed"] and "Transcript" in s["embed"].title
                   for s in world.transcripts.sent), "transcript missed"
        assert owner.dms and owner.dms[-1]["files"], "owner did not get a DM copy"

        await asyncio.sleep(0.05)  # let the (zero-delay) delete task run
        assert channel.deleted, "channel not deleted"
        assert channel.id not in cog._ticket_channels

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# /ticket setup: first run creates, later runs edit in place
# --------------------------------------------------------------------------- #
def test_setup_command():
    async def scenario():
        world = ticket_world()
        guild, panel, store = world.guild, world.panel, world.store
        store.configs.clear()  # a genuine first run, with nothing configured yet
        role = FakeRole(123, "Staff")
        guild.roles[123] = role
        cog = make_cog(guild, store)

        setup_cb = ts.TicketSystem.ticket.commands[0].callback

        # --- first run ----------------------------------------------------- #
        it1 = FakeInteraction(guild, panel, SimpleNamespace(id=1))
        await setup_cb(cog, it1, panel_channel=panel)
        config = store.configs[guild.id]
        assert config["panel_channel_id"] == panel.id
        assert config["category_id"] == guild.categories[0].id
        assert config["transcript_channel_id"] == panel.id
        assert config["log_channel_id"] == panel.id
        assert len(config["types"]) == 6
        assert config["panel_message_id"]
        assert panel.sent[-1]["view"] is not None
        assert len(panel.sent[-1]["view"].children) == 6
        assert it1.followups and it1.followups[-1]["embed"] is not None

        # --- second run: reconfigure, edit the existing panel -------------- #
        it2 = FakeInteraction(guild, panel, SimpleNamespace(id=1))
        await setup_cb(
            cog, it2, panel_channel=panel, category=guild.categories[0],
            staff_roles="<@&123>", title="Need help?", color="#10b981",
            footer="custom footer", auto_close_hours=12,
            types="\U0001F6A8 Report:\U0001F6A8:Report a user; \U0001F4AC General:",
        )
        config = store.configs[guild.id]
        assert config["staff_role_ids"] == [123]
        assert config["panel"]["title"] == "Need help?"
        assert config["panel"]["color"] == 0x10B981
        assert config["panel"]["footer"] == "custom footer"
        assert config["auto_close_hours"] == 12
        assert [t["key"] for t in config["types"]] == ["report", "general"]
        assert len(panel.sent) == 1, "panel must be edited, not re-posted"
        assert panel.messages[0].edits, "existing panel message not edited"

        # --- third run: disabling auto-close keeps the summary intact ------ #
        it3 = FakeInteraction(guild, panel, SimpleNamespace(id=1))
        await setup_cb(cog, it3, panel_channel=panel, auto_close_hours=0)
        description = it3.followups[-1]["embed"].description
        assert "Panel:" in description, "summary lost when auto-close disabled"
        assert "disabled" in description

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Auto-close decisions
# --------------------------------------------------------------------------- #
def _autoclose_cog(world, ticket):
    """Wire a cog whose store holds the *same* ticket object (so writes show up)."""
    cog = make_cog(world.guild, world.store)
    # Mirror _auto_close_one: config wins, and ticket-level value is the fallback.
    ticket_hours = ticket.get("auto_close_hours")
    world.store.configs[world.guild.id]["auto_close_hours"] = (
        24 if ticket_hours is None else ticket_hours
    )
    world.store.tickets[ticket["channel_id"]] = ticket
    cog._ticket_channels = {ticket["channel_id"]}
    return cog


def test_auto_close_decisions():
    async def scenario():
        now = discord.utils.utcnow()
        base = {
            "channel_id": 0, "guild_id": 1, "number": 1, "status": "open",
            "owner_id": 77, "warned_at": None, "last_activity": now,
        }

        # Beyond the threshold -> closes.
        world = ticket_world()
        channel = FakeChannel(world.guild, "ticket-0001", cid=4242)
        world.guild.channels[channel.id] = channel
        ticket = dict(base, channel_id=channel.id,
                      last_activity=now - timedelta(hours=25))
        cog = _autoclose_cog(world, ticket)
        cog._last_activity[channel.id] = now - timedelta(hours=25)
        closes = []

        async def fake_close(*args, **kwargs):
            closes.append(kwargs)

        cog._perform_close = fake_close
        await cog._auto_close_one(ticket)
        assert closes and closes[0].get("reason"), "expected a close"
        assert "24h" in closes[0]["reason"]

        # Inside the warn window -> warns once, does not close.
        world = ticket_world()
        channel = FakeChannel(world.guild, "ticket-0001", cid=4243)
        world.guild.channels[channel.id] = channel
        ticket = dict(base, channel_id=channel.id,
                      last_activity=now - timedelta(hours=23, minutes=30))
        cog = _autoclose_cog(world, ticket)
        cog._last_activity[channel.id] = now - timedelta(hours=23, minutes=30)
        closes = []
        cog._perform_close = fake_close
        await cog._auto_close_one(ticket)
        assert not closes, "must not close inside the warn window"
        assert channel.sent and "auto-closed" in channel.sent[-1]["content"]
        assert ticket["warned_at"] is not None

        # Already warned -> silent, no duplicate warning.
        world = ticket_world()
        channel = FakeChannel(world.guild, "ticket-0001", cid=4244)
        world.guild.channels[channel.id] = channel
        ticket = dict(base, channel_id=channel.id,
                      last_activity=now - timedelta(hours=23, minutes=30),
                      warned_at=now)
        cog = _autoclose_cog(world, ticket)
        cog._last_activity[channel.id] = now - timedelta(hours=23, minutes=30)
        await cog._auto_close_one(ticket)
        assert not channel.sent, "duplicate warning posted"

        # Recently active -> untouched.
        world = ticket_world()
        channel = FakeChannel(world.guild, "ticket-0001", cid=4245)
        world.guild.channels[channel.id] = channel
        ticket = dict(base, channel_id=channel.id,
                      last_activity=now - timedelta(minutes=5))
        cog = _autoclose_cog(world, ticket)
        cog._last_activity[channel.id] = now - timedelta(minutes=5)
        await cog._auto_close_one(ticket)
        assert not channel.sent

        # auto_close_hours = 0 disables it entirely.
        world = ticket_world()
        channel = FakeChannel(world.guild, "ticket-0001", cid=4246)
        world.guild.channels[channel.id] = channel
        ticket = dict(base, channel_id=channel.id,
                      last_activity=now - timedelta(days=30),
                      auto_close_hours=0)
        cog = _autoclose_cog(world, ticket)
        cog._last_activity[channel.id] = now - timedelta(days=30)
        await cog._auto_close_one(ticket)
        assert not channel.sent, "auto-close should be disabled"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Regressions for audited edge cases
# --------------------------------------------------------------------------- #
def test_setup_moving_panel_channel_removes_stale_panel():
    """Re-running setup in a new channel must not leave two live panels behind."""
    async def scenario():
        world = ticket_world()
        guild, panel, store = world.guild, world.panel, world.store
        store.configs.clear()
        cog = make_cog(guild, store)
        setup_cb = ts.TicketSystem.ticket.commands[0].callback

        await setup_cb(cog, FakeInteraction(guild, panel, SimpleNamespace(id=1)),
                       panel_channel=panel)
        old_message = panel.messages[-1]
        assert not old_message.deleted

        panel2 = FakeChannel(guild, "support-2", cid=999)
        guild.channels[panel2.id] = panel2
        await setup_cb(cog, FakeInteraction(guild, panel2, SimpleNamespace(id=1)),
                       panel_channel=panel2)

        assert old_message.deleted, "stale panel left behind when channel changed"
        assert store.configs[guild.id]["panel_channel_id"] == panel2.id
        assert panel2.messages, "new panel not posted in the new channel"

    asyncio.run(scenario())


def test_create_ticket_db_failure_removes_orphan_channel():
    """A ticket whose document can't be saved must be cleaned up, not orphaned."""
    async def scenario():
        world = ticket_world()
        guild, owner, store = world.guild, world.owner, world.store
        cog = make_cog(guild, store)

        async def failing_create(document):
            return False

        store.create_ticket = failing_create

        it = FakeInteraction(guild, world.panel, owner)
        await cog.create_ticket(it, guild.id, "general", "subj", "desc")

        created = [c for c in guild.channels.values() if c.name.startswith("ticket-")]
        assert created and created[-1].deleted, "orphan ticket channel was not removed"
        assert not created[-1].sent, "intro was posted before the ticket was persisted"
        assert it.followups, "user was not told the ticket could not be saved"
        assert "couldn't save" in it.followups[-1]["content"]

    asyncio.run(scenario())


def test_finalize_close_is_guarded_against_double_close():
    """A second confirm while a close is in flight must be a no-op."""
    async def scenario():
        world = ticket_world()
        guild, owner, staff, store = world.guild, world.owner, world.staff, world.store
        cog = make_cog(guild, store)
        await cog.create_ticket(
            FakeInteraction(guild, world.panel, owner), guild.id, "general", "s", "d"
        )
        channel = ticket_channel(world)
        cog._closing.add(channel.id)

        it = FakeInteraction(guild, channel, staff)
        await cog.finalize_close(it, "resolved")
        assert store.tickets[channel.id]["status"] != "closed", "double close went through"
        assert it.followups[-1]["content"] == "This ticket is already closed."

    asyncio.run(scenario())


def test_ephemeral_component_callbacks_edit_in_place():
    """Buttons on an ephemeral view must update *that* message.

    ``safe_defer(ephemeral=True)`` on a component switches to a brand new
    ephemeral message, which would leave the original buttons stuck on screen.
    On a component callback the right move is ``ephemeral=False``, i.e. a
    deferred_message_update, so the edit lands on the message the button was on.
    """
    async def scenario():
        world = ticket_world()
        guild, owner, staff, store = world.guild, world.owner, world.staff, world.store
        cog = make_cog(guild, store)
        await cog.create_ticket(
            FakeInteraction(guild, world.panel, owner), guild.id, "general", "s", "d"
        )
        channel = ticket_channel(world)

        manage = ManageUsersView(cog, channel.id)

        # "Add User" swaps itself for the member picker, in place.
        it_add = FakeInteraction(guild, channel, staff)
        await manage.add.callback(it_add)
        assert it_add.defer_kwargs == {"ephemeral": False, "thinking": False}
        assert it_add.edits and it_add.edits[-1].get("view") is not None
        assert it_add.responses == [] and it_add.followups == []

        # "Remove User" with no members: still an in-place edit, no new message.
        it_rm = FakeInteraction(guild, channel, staff)
        await manage.remove.callback(it_rm)
        assert it_rm.defer_kwargs == {"ephemeral": False, "thinking": False}
        assert it_rm.edits and it_rm.responses == [] and it_rm.followups == []

        # "Cancel" clears its own buttons by editing an empty view in place.
        it_cancel = FakeInteraction(guild, channel, staff)
        await CloseConfirmView(cog, channel.id).cancel.callback(it_cancel)
        assert it_cancel.defer_kwargs == {"ephemeral": False, "thinking": False}
        assert it_cancel.edits, "cancel did not edit its own message"
        assert len(it_cancel.edits[-1]["view"].children) == 0

    asyncio.run(scenario())
