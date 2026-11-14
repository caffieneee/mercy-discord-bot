# Ticket System

A complete, first-class ticket system for the bot, configured and operated with a
**single slash command**: `/ticket setup`. Everything else is done through
buttons, selects and modals - there are no other commands to learn.

```
cogs/tickets/
├── config.py         # constants, enums, ticket types, panel defaults
├── store.py          # MongoDB access layer (Motor)
├── utils.py          # permissions, parsing, HTML/TXT transcript rendering
├── views.py          # persistent + ephemeral Discord UI components
├── ticket_system.py  # the cog: /ticket setup + all handlers  (only loaded file)
├── __init__.py
└── README.md
```

Only `ticket_system.py` is registered as a cog; the rest are helper modules and
are listed in `main.py`'s loader skip list.

## Quick start

1. `/ticket setup panel_channel:#support`
2. That's it. A category named **Tickets** is created automatically, the panel is
   posted in `#support`, and tickets can be opened immediately.

Re-run the exact same command any time to change settings - the panel message is
**edited in place**, never duplicated.

## The setup command

`/ticket setup` (Administrator only). Only `panel_channel` is required.

| Option | Description | Default |
| --- | --- | --- |
| `panel_channel` | Where the panel with the open buttons is posted | *(required)* |
| `category` | Category that holds ticket channels | auto-created **Tickets** |
| `staff_roles` | Roles that can manage tickets (comma-separated mentions/IDs) | none (admins always can) |
| `transcript_channel` | Receives closed-ticket transcripts | panel channel |
| `log_channel` | Receives ticket event logs | panel channel |
| `title` / `description` | Panel embed title / body | built-in generic text |
| `color` / `footer` / `image` | Panel embed colour (`#hex`), footer, image URL | built-in |
| `ping_staff` | Ping staff roles when a ticket opens | `true` |
| `auto_close_hours` | Close after this many idle hours (`0` disables) | `24` |
| `types` | Replace the panel buttons | 6 built-in types |

Notes:

- The `@everyone` role is ignored in `staff_roles` - treating it as staff would
  expose every ticket to every member.
- Only plain **Unicode emojis** are accepted in `types`; an unrecognised emoji is
  dropped (the button is posted without it) rather than failing the whole setup.
- Re-running setup with a different `panel_channel` edits the panel in the new
  channel and deletes the old one, so you never end up with two panels.

Example of replacing the panel types:

```
/ticket setup panel_channel:#support types:"🚨 Report:🚨:Report a rule breaker; 🛒 Purchase:🛒:Buy something; 📩 Other"
```

### Default ticket types

| Key | Button | Default priority |
| --- | --- | --- |
| `general` | 🎟️ General | Normal |
| `purchase` | 🛒 Purchase | Normal |
| `appeal` | ⚖️ Appeal | Normal |
| `report` | 🚨 Report | **High** |
| `partnership` | 🤝 Partnership | Normal |
| `other` | 📩 Other | Normal |

## Opening a ticket

1. A user presses a type button on the panel.
2. They fill in a modal: **Subject** and **Describe the issue**.
3. The bot creates `#ticket-0001` (sequential per server) under the ticket
   category, restricted to the opener, staff roles and the bot.
4. A staff ping is posted (if enabled) and an intro embed with the control bar.
5. If the user already has an open ticket, the attempt is refused with a link to
   the existing one. Only **one open ticket per user** is allowed.

## Staff controls (buttons inside the ticket)

| Button | Action |
| --- | --- |
| **Claim** | Claim the ticket (shows you as handler); press again to release |
| **Add / Remove** | Add or remove a member's access to the ticket |
| **Rename** | Rename the channel (sanitised to a valid name) |
| **Priority** | Set priority: Low / Normal / High / Urgent (recolours the intro) |
| **Transcript** | Generate an HTML + TXT transcript on demand, without closing |
| **Close** | Confirm (optionally with a reason) → transcript → delete |

Staff = anyone with **Administrator / Manage Server / Manage Channels** or any of
the configured `staff_roles`.

## Closing & transcripts

Closing (by the owner or staff) does the following:

1. Posts a **standalone dark-theme HTML transcript** plus a **plain-text copy**
   to the transcript channel, with a summary embed (opener, type, priority,
   claimer, closer, reason, message count).
2. DMs a copy of both files to the ticket opener (silently skipped if DMs are
   closed).
3. Writes a "Ticket closed" entry to the log channel.
4. Marks the ticket closed in the database and deletes the channel a few seconds
   later.

Transcripts are capped at **1000 messages** and are fully self-contained: all
user text is HTML-escaped, avatars/timestamps/embeds/attachments are included,
and no external assets are required. `ticket-0001` → `transcript-0001.html` /
`transcript-0001.txt`.

## Auto-close

A background task checks open tickets every 15 minutes:

- **24h** (configurable) of no messages → the ticket is auto-closed with a
  transcript, exactly like a manual close.
- **1h before** that → a warning is posted in the channel (once).

Activity is tracked on every human message; the idle timer is seeded from the
database and survives restarts. Set `auto_close_hours:0` to disable.

## Data model

Database `discord_bot`, collections:

- `ticket_configs` - one document per server (panel, category, channels, roles,
  types, auto-close).
- `tickets` - one document per ticket (owner, type, priority, status, claimer,
  added users, timestamps, close reason).
- `ticket_counters` - per-server sequential counter for `ticket-0001` naming.

Indexes are created on startup. If MongoDB is unavailable the system degrades to
clean ephemeral error messages rather than failing.

## Persistence

The panel and control bar are **persistent views** with static `custom_id`s, so
buttons keep working after a restart - state is always read from the database
and the channel at click time. Panel buttons are namespaced per server
(`tt:open:<guild_id>:<type>`), so any number of servers can use the system at
once without collisions.

## Notes

- Custom (server) emojis are intentionally avoided - only Unicode emojis are
  used, so nothing breaks on servers that don't have a given emoji.
- The opener cannot be removed from their own ticket.
- `main.py` requires no changes; the package is picked up by the recursive cog
  loader.
