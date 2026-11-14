"""Configuration constants, enums and defaults for the ticket system.

Nothing in here touches Discord or the database, so it is safe to import from
any of the other ticket modules. The file name ``config.py`` is in the bot's
cog-loader skip list, so it is never mistakenly loaded as a cog.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
DB_NAME = "discord_bot"
CONFIG_COLLECTION = "ticket_configs"
TICKETS_COLLECTION = "tickets"
COUNTERS_COLLECTION = "ticket_counters"

# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #
EMBED_COLOR = 0x2F3136          # matches the rest of the bot (dark grey)
SUCCESS_COLOR = 0x57F287
ERROR_COLOR = 0xED4245
WARNING_COLOR = 0xFEE75C
INFO_COLOR = 0x5865F2
TRANSCRIPT_COLOR = 0x5865F2

# --------------------------------------------------------------------------- #
# Limits / timings
# --------------------------------------------------------------------------- #
TRANSCRIPT_MESSAGE_LIMIT = 1000          # industry standard (DiscordChatExporter/Ticket Tool)
DEFAULT_AUTO_CLOSE_HOURS = 24
AUTO_CLOSE_WARN_BEFORE_HOURS = 1         # warn one hour before auto-close
CLOSE_DELETE_DELAY_SECONDS = 5
ACTIVITY_WRITE_THROTTLE_SECONDS = 60     # min seconds between activity writes per ticket
AUTO_CLOSE_CHECK_MINUTES = 15
MAX_PANEL_TYPES = 25                     # Discord allows 25 buttons in a view
MAX_TYPE_KEY_LENGTH = 40                 # keeps "tt:open:<guild>:<key>" well under Discord's 100-char custom_id cap

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class TicketStatus(str, Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    CLOSED = "closed"


class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


PRIORITY_ORDER: List[str] = ["low", "normal", "high", "urgent"]

PRIORITY_META: Dict[str, Dict[str, Any]] = {
    "low": {"label": "Low", "emoji": "\U0001F7E2", "color": SUCCESS_COLOR},
    "normal": {"label": "Normal", "emoji": "\U0001F535", "color": INFO_COLOR},
    "high": {"label": "High", "emoji": "\U0001F7E0", "color": 0xE67E22},
    "urgent": {"label": "Urgent", "emoji": "\U0001F534", "color": ERROR_COLOR},
}


def normalize_priority(value: Optional[str]) -> str:
    """Return a valid priority key, defaulting to ``normal``."""
    if value in PRIORITY_META:
        return value  # type: ignore[return-value]
    return Priority.NORMAL.value


def priority_label(value: Optional[str]) -> str:
    return PRIORITY_META[normalize_priority(value)]["label"]


def priority_emoji(value: Optional[str]) -> str:
    return PRIORITY_META[normalize_priority(value)]["emoji"]


def priority_color(value: Optional[str]) -> int:
    return int(PRIORITY_META[normalize_priority(value)]["color"])


# --------------------------------------------------------------------------- #
# Ticket types
# --------------------------------------------------------------------------- #
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


def slugify(value: str, fallback: str = "type") -> str:
    """Turn an arbitrary label into a safe, stable custom_id / key segment."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or fallback


def is_emoji(value: str) -> bool:
    """True when the whole string is a single emoji/emoji sequence."""
    text = (value or "").strip()
    if not text:
        return False
    return _EMOJI_RE.fullmatch(text) is not None


def safe_emoji(value: Any) -> str:
    """Return ``value`` if it is a usable unicode emoji, else ``""``.

    Discord rejects a component whose ``emoji.name`` is not an actual emoji with
    an HTTP 400. discord.py does not validate it for us, so a typo in the setup
    command (``Label:notanemoji``) would make the whole panel fail to post. We
    only accept plain unicode emojis - custom ``<:name:id>`` emojis are not
    supported and are dropped to an emoji-less button rather than failing.
    """
    text = str(value or "").strip()
    return text if text and is_emoji(text) else ""


def extract_emoji(value: str) -> tuple[str, str]:
    """Split a leading (or trailing) emoji from a label.

    Returns ``(emoji, remainder)``. Used to let the setup command accept
    ``"Report 🚨"`` as well as ``"🚨 Report"``.
    """
    value = (value or "").strip()
    if not value:
        return "", ""
    match = _EMOJI_RE.match(value)
    if match:
        emoji = match.group(0)
        remainder = value[match.end():].strip()
        return emoji, remainder
    # Trailing emoji, e.g. "Report 🚨"
    match = _EMOJI_RE.search(value)
    if match and match.end() == len(value):
        emoji = match.group(0)
        remainder = value[: match.start()].strip()
        return emoji, remainder
    return "", value


@dataclass
class TicketType:
    """A single button on the ticket panel."""

    key: str
    label: str
    emoji: str = ""
    description: str = ""
    priority: str = "normal"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TicketType":
        label = str(data.get("label") or data.get("key") or "Ticket").strip() or "Ticket"
        key = str(data.get("key") or "").strip() or slugify(label)
        return cls(
            key=key[:MAX_TYPE_KEY_LENGTH] or "type",
            label=label[:80],
            emoji=safe_emoji(data.get("emoji")),
            description=str(data.get("description") or "")[:100],
            priority=normalize_priority(data.get("priority")),
        )


DEFAULT_TYPES: List[TicketType] = [
    TicketType("general", "General", "\U0001F39F\uFE0F", "General questions and support"),
    TicketType("purchase", "Purchase", "\U0001F6D2", "Purchases, orders and billing"),
    TicketType("appeal", "Appeal", "\u2696\uFE0F", "Ban, mute or quarantine appeals"),
    TicketType("report", "Report", "\U0001F6A8", "Report a rule breaker", priority="high"),
    TicketType("partnership", "Partnership", "\U0001F91D", "Partnerships and staff applications"),
    TicketType("other", "Other", "\U0001F4E9", "Anything else"),
]


def default_types() -> List[TicketType]:
    """Return a fresh copy of the default type list."""
    return [TicketType.from_dict(t.to_dict()) for t in DEFAULT_TYPES]


# --------------------------------------------------------------------------- #
# Panel defaults
# --------------------------------------------------------------------------- #
DEFAULT_PANEL_TITLE = "Support Tickets"
DEFAULT_PANEL_DESCRIPTION = (
    "Need a hand? Pick the option below that best matches your request and a "
    "private ticket will be opened for you and our staff team.\n\n"
    "Please provide as much detail as possible so we can help you quickly."
)
DEFAULT_PANEL_FOOTER = "One open ticket per user"
DEFAULT_PANEL_COLOR = EMBED_COLOR

# Ticket intro embed
INTRO_TITLE_PREFIX = "Ticket"


def build_config_defaults() -> Dict[str, Any]:
    """A pristine config payload (minus guild/ids) used when creating configs."""
    return {
        "types": [t.to_dict() for t in default_types()],
        "panel": {
            "title": DEFAULT_PANEL_TITLE,
            "description": DEFAULT_PANEL_DESCRIPTION,
            "color": DEFAULT_PANEL_COLOR,
            "footer": DEFAULT_PANEL_FOOTER,
            "image": "",
        },
        "staff_role_ids": [],
        "transcript_channel_id": None,
        "log_channel_id": None,
        "ping_staff": True,
        "auto_close_hours": DEFAULT_AUTO_CLOSE_HOURS,
    }


def config_types(config: Optional[Dict[str, Any]]) -> List[TicketType]:
    """Read the type list from a config document, falling back to defaults.

    Duplicate keys are collapsed (Discord rejects duplicate component ids) and
    every key is length-capped so it can be embedded in a panel ``custom_id``.
    """
    if not config:
        return default_types()
    raw = config.get("types")
    if not raw:
        return default_types()
    types: List[TicketType] = []
    seen: set = set()
    for item in raw:
        if isinstance(item, dict):
            ticket_type = TicketType.from_dict(item)
        elif isinstance(item, str):
            ticket_type = TicketType(
                key=slugify(item)[:MAX_TYPE_KEY_LENGTH] or "type",
                label=item[:80],
            )
        else:
            continue
        if ticket_type.key in seen:
            continue
        seen.add(ticket_type.key)
        types.append(ticket_type)
    return types or default_types()


def find_type(config: Optional[Dict[str, Any]], key: str) -> Optional[TicketType]:
    for t in config_types(config):
        if t.key == key:
            return t
    return None
