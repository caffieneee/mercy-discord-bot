"""Tests for the ticket system.

These cover the pieces that are easy to get subtly wrong and expensive to debug
in production: label/emoji parsing, priority handling, channel-name and colour
sanitising, permission checks, and above all the HTML/TXT transcript renderer -
which must be standalone (no Discord ``<t:...>`` markup), correctly escaped, and
complete.

Nothing here needs a Discord connection or a MongoDB server.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from cogs.tickets import config as C
from cogs.tickets import utils as U
from cogs.tickets import views as V
from cogs.tickets.ticket_system import _parse_types_param

UTC = timezone.utc


# ---------------------------------------------------------------------------
# config: labels, emoji, priority
# ---------------------------------------------------------------------------
def test_slugify():
    assert C.slugify("General Support") == "general-support"
    assert C.slugify("Hello!!  World") == "hello-world"
    assert C.slugify("") == "type"
    assert C.slugify("", fallback="x") == "x"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("\U0001F6A8 Report", ("\U0001F6A8", "Report")),
        ("Report \U0001F6A8", ("\U0001F6A8", "Report")),
        ("\U0001F39F\uFE0F General", ("\U0001F39F\uFE0F", "General")),
        ("Plain", ("", "Plain")),
        ("", ("", "")),
    ],
)
def test_extract_emoji(raw, expected):
    assert C.extract_emoji(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("\U0001F39F\uFE0F", True),
        ("\u2696\uFE0F", True),
        ("\U0001F6D2", True),
        ("Report", False),
        ("", False),
        ("a\U0001F6A8", False),
    ],
)
def test_is_emoji(raw, expected):
    assert C.is_emoji(raw) is expected


def test_priority_helpers():
    assert C.normalize_priority("urgent") == "urgent"
    assert C.normalize_priority("bogus") == "normal"
    assert C.normalize_priority(None) == "normal"
    assert C.priority_label("high") == "High"
    assert C.priority_color("high") == 0xE67E22
    assert C.priority_emoji("report") == C.PRIORITY_META["normal"]["emoji"]


def test_default_types_snapshot():
    types = C.default_types()
    assert [t.key for t in types] == [
        "general",
        "purchase",
        "appeal",
        "report",
        "partnership",
        "other",
    ]
    # The report type is the only one seeded with High priority.
    assert [t.key for t in types if t.priority == "high"] == ["report"]
    # Mutating one snapshot must not affect the next.
    types[0].label = "changed"
    assert C.default_types()[0].label == "General"


def test_config_types_and_find_type():
    assert C.config_types(None)[0].key == "general"
    assert C.config_types({})[0].key == "general"
    cfg = {"types": [{"key": "x", "label": "X"}]}
    assert C.config_types(cfg)[0].label == "X"
    assert C.find_type(cfg, "x").label == "X"
    assert C.find_type(cfg, "missing") is None
    # Bare strings are tolerated.
    assert C.config_types({"types": ["Support"]})[0].label == "Support"


# ---------------------------------------------------------------------------
# setup option parsing
# ---------------------------------------------------------------------------
def test_parse_types_full_syntax():
    parsed = _parse_types_param(
        "Tickets:\U0001F39F\uFE0F:General help; Shopping:\U0001F6D2:Buy stuff", []
    )
    assert [t.key for t in parsed] == ["tickets", "shopping"]
    assert parsed[0].emoji == "\U0001F39F\uFE0F"
    assert parsed[0].description == "General help"
    assert parsed[1].description == "Buy stuff"


def test_parse_types_label_only_and_emoji_in_label():
    assert _parse_types_param("\U0001F6A8 Report", [])[0].emoji == "\U0001F6A8"
    assert _parse_types_param("\U0001F6A8 Report", [])[0].label == "Report"
    # label:description with no emoji
    parsed = _parse_types_param("Appeal:Ban appeals", [])
    assert parsed[0].description == "Ban appeals"
    assert parsed[0].emoji == ""


def test_parse_types_deduplicates_and_falls_back():
    parsed = _parse_types_param("Dup:; Dup:; Other:", [])
    assert [t.key for t in parsed] == ["dup", "other"]
    base = C.default_types()
    assert _parse_types_param("", base) is base


# ---------------------------------------------------------------------------
# utils: names, colours, timestamps
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hello World!! 123", "hello-world-123"),
        ("  Trim  Me  ", "trim-me"),
        ("already-good", "already-good"),
        ("", "ticket"),
        ("!!!", "ticket"),
    ],
)
def test_sanitize_channel_name(raw, expected):
    assert U.sanitize_channel_name(raw) == expected


def test_sanitize_channel_name_length():
    assert len(U.sanitize_channel_name("a" * 500)) <= 90


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("#2F3136", 0x2F3136),
        ("2f3136", 0x2F3136),
        ("0x10b981", 0x10B981),
        ("  #FFF ", 0xFFFFFF),
        ("#abc", 0xAABBCC),
        ("#0a0", 0x00AA00),
        ("nope", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_color(raw, expected):
    assert U.parse_color(raw) == expected


def test_human_dt_is_plain_text():
    stamp = U.human_dt(datetime(2026, 10, 17, 12, 30, tzinfo=UTC))
    assert stamp == "2026-10-17 12:30:00 UTC"
    assert "<t:" not in stamp
    # Naive datetimes are treated as UTC, never a raw error.
    assert U.human_dt(datetime(2026, 1, 1)) == "2026-01-01 00:00:00 UTC"
    assert U.human_dt(None) == "Unknown"


# ---------------------------------------------------------------------------
# utils: permissions and role parsing
# ---------------------------------------------------------------------------
def _member(*, admin=False, manage_guild=False, manage_channels=False, roles=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(
            administrator=admin, manage_guild=manage_guild, manage_channels=manage_channels
        ),
        roles=list(roles),
        id=1,
    )


def test_member_is_staff_permissions():
    cfg = {"staff_role_ids": []}
    assert U.member_is_staff(_member(admin=True), cfg) is True
    assert U.member_is_staff(_member(manage_guild=True), cfg) is True
    assert U.member_is_staff(_member(manage_channels=True), cfg) is True
    assert U.member_is_staff(_member(), cfg) is False


def test_member_is_staff_roles_and_missing_config():
    role = SimpleNamespace(id=99)
    cfg = {"staff_role_ids": [99]}
    assert U.member_is_staff(_member(roles=[role]), cfg) is True
    assert U.member_is_staff(_member(roles=[SimpleNamespace(id=1)]), cfg) is False
    # No config at all still lets administrators through.
    assert U.member_is_staff(_member(admin=True), None) is True
    assert U.member_is_staff(_member(), None) is False


def test_parse_staff_roles():
    roles = {5: SimpleNamespace(id=5), 7: SimpleNamespace(id=7)}

    class Guild:
        def get_role(self, rid):
            return roles.get(rid)

    found = U.parse_staff_roles(Guild(), "<@&5>, <@&7> 7")
    assert [r.id for r in found] == [5, 7]
    assert U.parse_staff_roles(Guild(), None) == []
    assert U.parse_staff_roles(Guild(), "not-a-role") == []


# ---------------------------------------------------------------------------
# transcripts
# ---------------------------------------------------------------------------
def _author(name="Tester", uid=1, bot=False):
    return SimpleNamespace(
        name=name,
        display_name=name,
        id=uid,
        bot=bot,
        display_avatar=SimpleNamespace(url=f"https://cdn.test/{uid}.png"),
    )


def _message(content="", *, embeds=None, attachments=None, bot=False, name="Tester", uid=1):
    return SimpleNamespace(
        author=_author(name, uid, bot),
        content=content,
        embeds=embeds or [],
        attachments=attachments or [],
        created_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        type=discord.MessageType.default,
    )


def _meta(**over):
    meta = {
        "number": "0007",
        "type": "Report",
        "guild": "Test Guild",
        "owner": "Tester (1)",
        "claimed_by": "Staff (2)",
        "priority": "High",
        "subject": "Bad actor",
        "created_at": "2026-10-17 12:00:00 UTC",
        "closed_at": "2026-10-17 12:30:00 UTC",
        "closed_by": "Staff (2)",
        "reason": "Handled",
        "message_count": 2,
    }
    meta.update(over)
    return meta


def test_md_to_html_escapes_and_formats():
    out = U.md_to_html("**bold** *it* `code` <script>alert(1)</script>")
    assert "<strong>bold</strong>" in out
    assert "<em>it</em>" in out
    assert "<code>code</code>" in out
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_md_to_html_code_block_not_reformatted():
    out = U.md_to_html("```\n**not bold**\n```")
    assert "<pre><code>" in out
    assert "**not bold**" in out
    assert "<strong>" not in out


def test_md_to_html_ignores_foreign_placeholder_sentinel():
    # A NUL sentinel in user input must not crash the renderer (IndexError).
    out = U.md_to_html("text \x0099\x00 more")
    assert "text" in out and "more" in out


def test_render_html_is_standalone_and_safe():
    embed = discord.Embed(title="Order", description="**Paid**", color=0x57F287)
    embed.add_field(name="Amount", value="$1")
    messages = [
        _message("Hi <b>there</b>", name="Alex", uid=21),
        _message(
            "see attached",
            embeds=[embed],
            attachments=[
                SimpleNamespace(
                    filename="a.png", url="https://cdn.test/a.png", size=2048, content_type="image/png"
                )
            ],
            name="Bot",
            uid=5,
            bot=True,
        ),
    ]
    html = U.render_html(messages, _meta())
    # Self-contained + escaped
    assert html.startswith("<!DOCTYPE html>")
    assert "&lt;b&gt;there&lt;/b&gt;" in html
    assert "<b>there</b>" not in html
    # No Discord-only timestamp markup
    assert "<t:" not in html
    # Metadata + content present
    assert "Ticket #0007" in html
    assert "Report" in html
    assert "Bad actor" in html
    assert "Handled" in html
    assert "Order" in html  # embed title
    assert "a.png" in html  # attachment
    assert "Bot" in html


def test_render_txt_contains_metadata_and_messages():
    txt = U.render_txt([_message("hello world", name="Alex", uid=21)], _meta())
    assert "TICKET #0007 TRANSCRIPT" in txt
    assert "Opened by   : Tester (1)" in txt
    assert "Close reason: Handled" in txt
    assert "hello world" in txt
    assert "<t:" not in txt


def test_build_files_names_and_bytes():
    html_file, txt_file = U.build_files([_message("x")], _meta(), "0007")
    assert html_file.filename == "transcript-0007.html"
    assert txt_file.filename == "transcript-0007.txt"
    # Files are consumed when sent, so each destination rebuilds its own pair.
    html_again, txt_again = U.build_files([_message("x")], _meta(), "0007")
    assert html_again.filename == "transcript-0007.html"
    assert txt_again.filename == "transcript-0007.txt"


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------
class _Cog:
    """Stand-in cog: the views only ever call these by name."""

    async def handle_open(self, *a, **k):
        ...

    async def handle_claim(self, *a, **k):
        ...

    async def handle_manage_users(self, *a, **k):
        ...

    async def handle_rename(self, *a, **k):
        ...

    async def handle_transcript(self, *a, **k):
        ...

    async def handle_close_button(self, *a, **k):
        ...

    async def handle_priority(self, *a, **k):
        ...


def test_panel_view_custom_ids_are_guild_scoped():
    panel = V.TicketPanelView(_Cog(), 42, C.default_types())
    custom_ids = [c.custom_id for c in panel.children]
    assert custom_ids[0] == "tt:open:42:general"
    assert len(custom_ids) == 6
    assert panel.is_persistent()


def test_panel_view_unique_per_guild():
    a = V.TicketPanelView(_Cog(), 1, C.default_types())
    b = V.TicketPanelView(_Cog(), 2, C.default_types())
    assert a.children[0].custom_id != b.children[0].custom_id


def test_control_view_is_persistent_with_fixed_ids():
    control = V.TicketControlView(_Cog())
    ids = [getattr(c, "custom_id", None) for c in control.children]
    assert set(ids) == {
        "tt:claim",
        "tt:adduser",
        "tt:rename",
        "tt:transcript",
        "tt:close",
        "tt:priority",
    }
    assert control.is_persistent()


def test_modal_and_ephemeral_views_build():
    modal = V.OpenTicketModal(_Cog(), 1, "general", "General")
    assert len(modal.children) == 2
    assert all(isinstance(f, discord.ui.TextInput) for f in modal.children)
    assert modal.title == "Open a General Ticket"
    assert V.CloseReasonModal(_Cog(), 1).title == "Close Ticket"
    assert V.RenameModal(_Cog(), 1).title == "Rename Ticket"
    assert len(V.CloseConfirmView(_Cog(), 1).children) == 3
    assert len(V.ManageUsersView(_Cog(), 1).children) == 2
    assert isinstance(V.AddUserView(_Cog(), 1).children[0], discord.ui.UserSelect)


# ---------------------------------------------------------------------------
# regressions: emoji validation, custom_id length, @everyone, as_utc
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("\U0001F6A8", "\U0001F6A8"),
        ("  \U0001F6D2  ", "\U0001F6D2"),
        ("notanemoji", ""),
        ("<:foo:123>", ""),          # custom emojis are not supported
        ("a\U0001F6A8", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_safe_emoji(raw, expected):
    assert C.safe_emoji(raw) == expected


def test_from_dict_sanitises_emoji_label_length_and_key():
    long_key = "k" * 200
    parsed = C.TicketType.from_dict(
        {"key": long_key, "label": "L" * 200, "emoji": "bogus", "description": "D" * 200}
    )
    assert len(parsed.key) <= C.MAX_TYPE_KEY_LENGTH
    assert len(parsed.label) <= 80
    assert len(parsed.description) <= 100
    assert parsed.emoji == ""


def test_parse_types_truncates_and_sanitises():
    parsed = _parse_types_param("X" * 120 + ":notemoji", [])
    assert len(parsed[0].key) <= C.MAX_TYPE_KEY_LENGTH
    assert parsed[0].emoji == ""


def test_config_types_deduplicates_keys():
    cfg = {"types": [{"key": "dup", "label": "A"}, {"key": "dup", "label": "B"}]}
    assert [t.label for t in C.config_types(cfg)] == ["A"]


def test_panel_view_custom_id_never_exceeds_discord_limit():
    guild_id = 1_234_567_890_123_456_789
    # A hand-edited config could still carry an over-long key.
    bad = C.TicketType(key="k" * 200, label="Bad")
    view = V.TicketPanelView(_Cog(), guild_id, [bad, *C.default_types()])
    assert all(len(c.custom_id) <= 100 for c in view.children)
    # The too-long type is skipped, the valid defaults remain.
    assert len(view.children) == 6


def test_panel_view_drops_duplicate_and_invalid_buttons():
    dupes = [
        C.TicketType(key="same", label="One"),
        C.TicketType(key="same", label="Two"),
    ]
    view = V.TicketPanelView(_Cog(), 1, dupes)
    assert len(view.children) == 1


def test_parse_staff_roles_skips_everyone():
    class Role:
        def __init__(self, rid, default=False):
            self.id = rid
            self._default = default

        def is_default(self):
            return self._default

    roles = {10: Role(10, default=True), 11: Role(11)}

    class Guild:
        def get_role(self, rid):
            return roles.get(rid)

    found = U.parse_staff_roles(Guild(), "<@&10> <@&11>")
    assert [r.id for r in found] == [11]
    assert U.parse_staff_roles(Guild(), "10") == []


def test_member_is_staff_ignores_everyone_role():
    role = SimpleNamespace(id=1, is_default=lambda: True)
    cfg = {"staff_role_ids": [1]}
    assert U.member_is_staff(_member(roles=[role]), cfg) is False


def test_as_utc_normalises_naive_and_preserves_aware():
    naive = datetime(2026, 1, 1, 12, 0)
    aware = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert U.as_utc(naive) == aware
    assert U.as_utc(naive).tzinfo is UTC
    assert U.as_utc(aware) is aware
    assert U.as_utc(None) is None


def test_intro_embed_uses_correct_utc_timestamp():
    from cogs.tickets.ticket_system import TicketSystem

    ticket = {
        "number": 1,
        "owner_id": 2,
        "type": "general",
        "priority": "normal",
        "status": "open",
        "subject": "s",
        "description": "",
        # A naive UTC datetime, exactly as Mongo returns it.
        "created_at": datetime(2026, 1, 1, 12, 0),
    }
    embed = TicketSystem._intro_embed(SimpleNamespace(), ticket)
    assert embed.timestamp == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
