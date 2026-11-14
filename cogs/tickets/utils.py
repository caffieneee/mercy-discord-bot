"""Shared helpers for the ticket system: names, colours, permissions and the
transcript renderer.

The transcript format mirrors what established ticket bots produce: a single
standalone, dark-theme HTML file (DiscordChatExporter / chat-exporter layout)
containing the full conversation, avatars, timestamps, embeds and attachment
links, plus a plain-text fallback. User supplied text is always HTML-escaped so
the transcript cannot be used for injection.
"""
from __future__ import annotations

import html as _html
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import discord

from .config import TRANSCRIPT_MESSAGE_LIMIT

# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
_CHANNEL_NAME_RE = re.compile(r"[^a-z0-9\-]+")


def sanitize_channel_name(value: str, fallback: str = "ticket") -> str:
    """Return a Discord-safe channel name (lowercase, ``a-z0-9-``)."""
    name = (value or "").strip().lower()
    name = name.replace(" ", "-")
    name = _CHANNEL_NAME_RE.sub("-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    name = name[:90].strip("-")
    return name or fallback


def parse_color(value: Optional[str]) -> Optional[int]:
    """Parse ``#2f3136`` / ``2f3136`` / ``0x2f3136`` / ``#fff`` into an int, else None."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = text.lstrip("#")
    if not text:
        return None
    try:
        # Expand the 3-digit shorthand (``#abc`` -> ``#aabbcc``) like CSS does.
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        return int(text, 16) & 0xFFFFFF
    except ValueError:
        return None


def unix_ts(value: Optional[datetime]) -> int:
    if not value:
        return 0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def format_ts(value: Optional[datetime], style: str = "F") -> str:
    ts = unix_ts(value)
    if not ts:
        return "Unknown"
    return f"<t:{ts}:{style}>"


def human_dt(value: Optional[datetime]) -> str:
    """A plain, human-readable UTC timestamp for transcripts.

    Transcripts are standalone files, so Discord's ``<t:...>`` markup must never
    be used there - it would show up as literal text in a browser or text editor.
    """
    if not isinstance(value, datetime):
        return "Unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _as_utc(value: Any) -> Optional[datetime]:
    return as_utc(value)


def as_utc(value: Any) -> Optional[datetime]:
    """Return ``value`` as a UTC-aware datetime (or None).

    MongoDB hands back naive datetimes that are already in UTC. Discord's
    ``Embed.timestamp`` setter, however, treats a naive datetime as *local* time
    and calls ``.astimezone()`` on it - silently shifting every timestamp by the
    host's UTC offset. Always normalise through here before handing a stored
    datetime to discord.py.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


# --------------------------------------------------------------------------- #
# Permission helpers
# --------------------------------------------------------------------------- #
def staff_role_ids(config: Optional[Dict[str, Any]]) -> set:
    """Role ids configured as staff, skipping anything that isn't a number."""
    if not config:
        return set()
    ids: set = set()
    for role in config.get("staff_role_ids") or []:
        try:
            ids.add(int(str(role).strip()))
        except (TypeError, ValueError):
            # A hand-edited document may hold junk; it must never make every
            # staff check raise or, worse, silently match the wrong role.
            continue
    return ids


def _is_default_role(role: Any) -> bool:
    """True for an ``@everyone`` role (which must never count as staff)."""
    fn = getattr(role, "is_default", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def member_is_staff(member: discord.Member, config: Optional[Dict[str, Any]]) -> bool:
    """Administrator, Manage Server, or any configured staff role."""
    try:
        perms = member.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_channels:
            return True
    except Exception:
        pass
    configured = staff_role_ids(config)
    if configured and any(
        role.id in configured and not _is_default_role(role)
        for role in getattr(member, "roles", [])
    ):
        return True
    return False


def parse_staff_roles(guild: discord.Guild, text: Optional[str]) -> List[discord.Role]:
    """Parse a comma/space separated list of role mentions or IDs.

    The ``@everyone`` role is skipped: treating it as a staff role would grant
    every member access to every ticket and make everyone count as staff.
    """
    if not text:
        return []
    found: List[discord.Role] = []
    seen: set = set()
    for token in re.split(r"[,\s]+", text.strip()):
        if not token:
            continue
        digits = re.sub(r"\D", "", token)
        if not digits:
            continue
        role = guild.get_role(int(digits))
        if role and role.id not in seen and not _is_default_role(role):
            seen.add(role.id)
            found.append(role)
    return found


# --------------------------------------------------------------------------- #
# Transcript: message normalisation
# --------------------------------------------------------------------------- #
async def collect_messages(
    channel: discord.TextChannel, limit: int = TRANSCRIPT_MESSAGE_LIMIT
) -> List[discord.Message]:
    messages: List[discord.Message] = []
    async for message in channel.history(limit=limit, oldest_first=True):
        messages.append(message)
    return messages


def _message_dict(message: discord.Message) -> Dict[str, Any]:
    author = message.author
    embeds: List[Dict[str, Any]] = []
    for embed in message.embeds:
        embeds.append(
            {
                "title": embed.title or "",
                "description": embed.description or "",
                "color": embed.color.value if embed.color else None,
                "fields": [
                    {"name": f.name or "", "value": f.value or ""} for f in (embed.fields or [])
                ],
                "image": embed.image.url if embed.image else "",
                "thumbnail": embed.thumbnail.url if embed.thumbnail else "",
                "footer": embed.footer.text if embed.footer else "",
            }
        )
    attachments = [
        {
            "filename": a.filename,
            "url": a.url,
            "size": a.size,
            "is_image": bool(a.content_type and a.content_type.startswith("image/")),
        }
        for a in message.attachments
    ]
    return {
        "author": getattr(author, "display_name", None) or author.name,
        "author_name": author.name,
        "author_id": author.id,
        "avatar": author.display_avatar.url if getattr(author, "display_avatar", None) else "",
        "bot": bool(getattr(author, "bot", False)),
        "timestamp": message.created_at,
        "content": message.content or "",
        "embeds": embeds,
        "attachments": attachments,
        "is_system": message.type is not discord.MessageType.default,
    }


# --------------------------------------------------------------------------- #
# Transcript: markdown -> html
# --------------------------------------------------------------------------- #
_CODE_BLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def md_to_html(raw: str) -> str:
    """Very small, safe markdown subset -> HTML (code, bold, italics, strike)."""
    if not raw:
        return ""
    placeholders: List[str] = []

    def _stash(inner: str, cls: str = "") -> str:
        cls_attr = f' class="{cls}"' if cls else ""
        placeholders.append(f"<pre{cls_attr}><code>{inner}</code></pre>")
        return f"\x00{len(placeholders) - 1}\x00"

    # 1. Fenced code blocks (escape then stash).
    def _codeblock(match: re.Match) -> str:
        return _stash(_html.escape(match.group(1).strip("\n")))

    text = _CODE_BLOCK_RE.sub(_codeblock, raw)
    # 2. Inline code (escape then stash).
    def _inline_code(match: re.Match) -> str:
        placeholders.append(f"<code>{_html.escape(match.group(1))}</code>")
        return f"\x00{len(placeholders) - 1}\x00"

    text = _INLINE_CODE_RE.sub(_inline_code, text)
    # 3. Escape the rest, then apply inline formatting.
    text = _html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text, flags=re.DOTALL)
    text = re.sub(r"(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)", r"<em>\1</em>", text, flags=re.DOTALL)
    # 4. Restore stashed code.
    def _restore(match: re.Match) -> str:
        index = int(match.group(1))
        # A message that literally contains "\x00<digits>\x00" (only possible via a
        # bot/webhook, Discord strips NUL from client input) must not raise - fall
        # back to the escaped literal text.
        if 0 <= index < len(placeholders):
            return placeholders[index]
        return match.group(0)

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    text = text.replace("\n", "<br>")
    return text


# --------------------------------------------------------------------------- #
# Transcript: HTML
# --------------------------------------------------------------------------- #
_HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #1e1f22; color: #dbdee1;
         font-family: "gg sans", "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  .wrap {{ max-width: 900px; margin: 0 auto; padding: 24px 16px 64px; }}
  .header {{ background: #2b2d31; border-radius: 10px; padding: 22px 24px; margin-bottom: 20px;
            border: 1px solid #3f4147; }}
  .header h1 {{ margin: 0 0 4px; font-size: 22px; color: #f2f3f5; }}
  .header .sub {{ color: #949ba4; font-size: 13px; margin-bottom: 16px; }}
  .meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px 24px; }}
  .meta .item {{ font-size: 13px; }}
  .meta .k {{ color: #949ba4; display: block; font-size: 11px; text-transform: uppercase;
             letter-spacing: .04em; margin-bottom: 2px; }}
  .meta .v {{ color: #f2f3f5; }}
  .messages {{ display: flex; flex-direction: column; gap: 4px; }}
  .msg {{ display: flex; gap: 14px; padding: 8px 10px; border-radius: 6px; }}
  .msg:hover {{ background: #232428; }}
  .avatar {{ width: 40px; height: 40px; border-radius: 50%; flex: 0 0 40px; background: #5865f2; }}
  .body {{ flex: 1; min-width: 0; }}
  .head {{ display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }}
  .name {{ font-weight: 600; color: #f2f3f5; }}
  .bot-tag {{ background: #5865f2; color: #fff; font-size: 10px; font-weight: 700;
             padding: 1px 4px; border-radius: 4px; text-transform: uppercase; }}
  .time {{ color: #949ba4; font-size: 12px; }}
  .content {{ color: #dbdee1; line-height: 1.45; word-wrap: break-word; white-space: normal; }}
  .content pre {{ background: #1e1f22; border: 1px solid #2b2d31; border-radius: 6px;
                 padding: 8px 10px; overflow-x: auto; }}
  .content code {{ background: #1e1f22; border-radius: 4px; padding: 1px 4px;
                  font-family: Consolas, "Courier New", monospace; font-size: 13px; }}
  .content pre code {{ border: 0; background: transparent; padding: 0; }}
  .embed {{ border-left: 4px solid {accent}; background: #2b2d31; border-radius: 4px;
           padding: 10px 14px; margin-top: 8px; max-width: 520px; }}
  .embed .etitle {{ font-weight: 600; color: #f2f3f5; margin-bottom: 4px; }}
  .embed .edesc {{ font-size: 14px; white-space: pre-wrap; }}
  .embed .efield {{ margin-top: 8px; font-size: 13px; }}
  .embed .efield .fk {{ font-weight: 600; color: #f2f3f5; }}
  .attachments {{ margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }}
  .attachments a {{ color: #00a8fc; font-size: 13px; text-decoration: none; }}
  .attachments a:hover {{ text-decoration: underline; }}
  .thumb {{ max-width: 320px; border-radius: 6px; margin-top: 8px; }}
  .system {{ color: #949ba4; font-style: italic; font-size: 13px; }}
  .footer {{ margin-top: 26px; text-align: center; color: #6d6f78; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
"""


def _embed_html(embed: Dict[str, Any]) -> str:
    color = embed.get("color")
    accent = f"#{color:06x}" if isinstance(color, int) else "#5865f2"
    parts = [f'<div class="embed" style="border-left-color:{accent}">']
    if embed.get("title"):
        parts.append(f'<div class="etitle">{_html.escape(embed["title"])}</div>')
    if embed.get("description"):
        parts.append(f'<div class="edesc">{md_to_html(embed["description"])}</div>')
    for field in embed.get("fields", []):
        parts.append(
            f'<div class="efield"><span class="fk">{_html.escape(field.get("name", ""))}</span>'
            f'<br>{md_to_html(field.get("value", ""))}</div>'
        )
    if embed.get("thumbnail"):
        parts.append(f'<img class="thumb" src="{_html.escape(embed["thumbnail"])}" alt="">')
    if embed.get("image"):
        parts.append(f'<img class="thumb" src="{_html.escape(embed["image"])}" alt="">')
    if embed.get("footer"):
        parts.append(f'<div class="efield" style="color:#949ba4">{_html.escape(embed["footer"])}</div>')
    parts.append("</div>")
    return "".join(parts)


def _attachments_html(attachments: Iterable[Dict[str, Any]]) -> str:
    items = []
    for att in attachments:
        size_kb = (att.get("size") or 0) / 1024
        items.append(
            f'<a href="{_html.escape(att["url"])}" target="_blank" rel="noopener">'
            f'\U0001F4CE {_html.escape(att["filename"])} '
            f'<span style="color:#6d6f78">({size_kb:.1f} KB)</span></a>'
        )
    if not items:
        return ""
    return f'<div class="attachments">{"".join(items)}</div>'


def render_html(messages: List[discord.Message], meta: Dict[str, Any]) -> str:
    title = f"Ticket #{meta.get('number', '')} transcript"
    sections = [_HTML_HEAD.format(title=_html.escape(title), accent="#5865f2")]

    def _val(key: str, default: str = "\u2014") -> str:
        value = meta.get(key)
        return _html.escape(str(value)) if value not in (None, "") else default

    sections.append(
        '<div class="header">'
        f'<h1>{_html.escape(title)}</h1>'
        f'<div class="sub">{_val("guild", "")}</div>'
        '<div class="meta">'
        f'<div class="item"><span class="k">Ticket</span><span class="v">#{_val("number")}</span></div>'
        f'<div class="item"><span class="k">Type</span><span class="v">{_val("type")}</span></div>'
        f'<div class="item"><span class="k">Opened by</span><span class="v">{_val("owner")}</span></div>'
        f'<div class="item"><span class="k">Priority</span><span class="v">{_val("priority")}</span></div>'
        f'<div class="item"><span class="k">Claimed by</span><span class="v">{_val("claimed_by", "Unclaimed")}</span></div>'
        f'<div class="item"><span class="k">Closed by</span><span class="v">{_val("closed_by", "\u2014")}</span></div>'
        f'<div class="item"><span class="k">Created</span><span class="v">{_val("created_at")}</span></div>'
        f'<div class="item"><span class="k">Closed</span><span class="v">{_val("closed_at")}</span></div>'
        f'<div class="item"><span class="k">Messages</span><span class="v">{_val("message_count")}</span></div>'
        f'<div class="item" style="grid-column:1/-1"><span class="k">Subject</span><span class="v">{_val("subject")}</span></div>'
        f'<div class="item" style="grid-column:1/-1"><span class="k">Close reason</span><span class="v">{_val("reason", "No reason provided")}</span></div>'
        '</div></div>'
    )

    sections.append('<div class="messages">')
    for message in messages:
        data = _message_dict(message)
        avatar = _html.escape(data["avatar"]) if data["avatar"] else ""
        bot_tag = '<span class="bot-tag">Bot</span>' if data["bot"] else ""
        name = _html.escape(data["author"])
        ts = human_dt(_as_utc(data["timestamp"]))
        if data["is_system"]:
            body = f'<div class="system">{md_to_html(data["content"])}</div>'
        else:
            body = ""
            if data["content"]:
                body += f'<div class="content">{md_to_html(data["content"])}</div>'
            for embed in data["embeds"]:
                body += _embed_html(embed)
            body += _attachments_html(data["attachments"])
        sections.append(
            '<div class="msg">'
            f'<img class="avatar" src="{avatar}" alt="">'
            '<div class="body">'
            f'<div class="head"><span class="name">{name}</span>{bot_tag}'
            f'<span class="time">{ts}</span></div>'
            f'{body}'
            '</div></div>'
        )
    sections.append("</div>")
    sections.append(
        f'<div class="footer">Transcript generated by the ticket system &middot; '
        f'{_val("message_count")} messages</div>'
    )
    sections.append("</div></body></html>")
    return "".join(sections)


# --------------------------------------------------------------------------- #
# Transcript: TXT
# --------------------------------------------------------------------------- #
def render_txt(messages: List[discord.Message], meta: Dict[str, Any]) -> str:
    line = "=" * 80
    out: List[str] = [
        line,
        f"TICKET #{meta.get('number', '')} TRANSCRIPT",
        line,
        f"Server      : {meta.get('guild', '')}",
        f"Ticket      : #{meta.get('number', '')}",
        f"Type        : {meta.get('type', '')}",
        f"Opened by   : {meta.get('owner', '')}",
        f"Priority    : {meta.get('priority', '')}",
        f"Claimed by  : {meta.get('claimed_by') or 'Unclaimed'}",
        f"Closed by   : {meta.get('closed_by') or '-'}",
        f"Created     : {meta.get('created_at', '')}",
        f"Closed      : {meta.get('closed_at', '')}",
        f"Messages    : {meta.get('message_count', '')}",
        f"Subject     : {meta.get('subject', '')}",
        f"Close reason: {meta.get('reason') or 'No reason provided'}",
        line,
        "",
    ]
    for message in messages:
        data = _message_dict(message)
        stamp = data["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if data["timestamp"] else ""
        bot_tag = " [BOT]" if data["bot"] else ""
        out.append(f"[{stamp}] {data['author']} ({data['author_id']}){bot_tag}")
        if data["content"]:
            for cline in data["content"].splitlines():
                out.append(f"  {cline}")
        for embed in data["embeds"]:
            if embed.get("title"):
                out.append(f"  [embed] {embed['title']}")
            if embed.get("description"):
                out.append(f"  {embed['description']}")
            for field in embed.get("fields", []):
                out.append(f"  {field.get('name', '')}: {field.get('value', '')}")
        for att in data["attachments"]:
            out.append(f"  [attachment] {att['filename']}: {att['url']}")
        out.append("")
    out.append(line)
    out.append("END OF TRANSCRIPT")
    return "\n".join(out)


def build_files(
    messages: List[discord.Message], meta: Dict[str, Any], number: Any
) -> tuple[discord.File, discord.File]:
    """Return ``(html_file, txt_file)`` bytes ready to attach to a message."""
    html_content = render_html(messages, meta)
    txt_content = render_txt(messages, meta)
    html_file = discord.File(
        io.BytesIO(html_content.encode("utf-8")), filename=f"transcript-{number}.html"
    )
    txt_file = discord.File(
        io.BytesIO(txt_content.encode("utf-8")), filename=f"transcript-{number}.txt"
    )
    return html_file, txt_file
