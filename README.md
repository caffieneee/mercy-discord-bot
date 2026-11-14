# Discord Bot - Mercy

A feature-rich Discord bot built with discord.py, featuring multiple cogs for server management, entertainment, and community engagement.

## Features

- **Leaderboards**: Chat and voice activity tracking with weekly star recognition
- **Giveaways**: Advanced giveaway system with role requirements
- **Moderation**: Ban, purge, quarantine, and other moderation tools
- **Utility**: AFK status, snipe, sticky messages, thread management
- **Reminders**: interactive admin dashboard for one-off and recurring scheduled reminders with timezone support and role pings
- **Tickets**: one-command ticket system with a configurable panel, staff controls, HTML + TXT transcripts and auto-close
- **Fun**: Media commands, autoresponder, confession system
- **Voice**: Voice channel management and matchmaking
- **Counting & Skullboard**: Community engagement features

## Setup

### Prerequisites

- Python 3.8+
- MongoDB database
- Discord Bot Token

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd mercy
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file with the following variables:
```env
DISCORD_TOKEN=your_discord_bot_token
WEBHOOK_URL=your_webhook_url
MONGO_URL=your_mongodb_connection_string
OWNER_IDS=your_discord_user_id
AUTO_SYNC_COMMANDS=true
```

4. Run the bot:
```bash
python main.py
```

## Deployment (GitHub Actions)

The bot is hosted by [`.github/workflows/mercy-bot.yml`](.github/workflows/mercy-bot.yml)
on **`ubuntu-latest`** — no Windows runner and no RDP session.

### Secrets

Add these under **Settings → Secrets and variables → Actions** in the repo:

| Secret          | Required | Purpose                                                        |
| --------------- | -------- | -------------------------------------------------------------- |
| `DISCORD_TOKEN` | yes      | Bot token from the Discord Developer Portal                     |
| `WEBHOOK_URL`   | yes      | Webhook used for startup/crash/chain-stop alerts                |
| `MONGO_URL`     | yes      | MongoDB connection string (leaderboards, tickets, reminders...) |
| `OWNER_IDS`     | no       | Comma-separated Discord user IDs with owner commands            |

### How it stays online

The workflow has **no cron trigger**. Start it once from
**Actions → Mercy Bot → Run workflow**; every run queues its own successor, so
sessions chain back-to-back. Each session keeps the bot alive for ~5.5 hours
inside GitHub's 6-hour job limit.

- A session that runs its full keep-alive resets the retry counter.
- A bot that fails to start, crashes, or exits early counts up; after 5 links
  the chain stops and a Discord alert is posted instead of restarting forever.
- Failed runs upload `bot.log` as an artifact (3-day retention) for debugging.

## Bot Commands

### Slash Commands

#### Reminders (`/reminder` · Manage Server required)

- `/reminder dashboard` - Interactive dashboard: create, manage, pause and resume reminders
- `/reminder create` - Six-step wizard (details, frequency, timing, timezone, channel, role)
- `/reminder manage` - Pick a reminder to edit, reschedule, preview, test, pause or delete
- `/reminder list` - Paginated listing of every reminder in the server
- `/reminder preview <reminder>` - Show the exact message without posting it
- `/reminder pause|resume|delete <reminder>` - Per-reminder actions with confirmation

Reminders are stored in MongoDB, survive restarts, support one-off and recurring
schedules (interval, daily, weekly, monthly) with IANA timezones, and ping an
optional role. See [cogs/reminders/README.md](cogs/reminders/README.md).

#### Tickets (`/ticket setup` · Administrator required)

A single command configures the entire ticket system and posts (or refreshes)
the public panel. Everything after that is button-driven:

- **Opening** - a user presses a panel button, picks the matching type, and
  fills in a short "Subject" / "Describe the issue" modal. Channels are named
  `ticket-0001`, `ticket-0002`, ... and only one open ticket per user is allowed.
- **Managing (staff)** - in-ticket buttons for Claim/Unclaim, Add/Remove member,
  Rename, Priority, on-demand Transcript, and Close.
- **Closing** - a confirmation step (optionally with a reason), then an HTML
  **and** TXT transcript is posted to the transcript channel and DMed to the
  opener, the event is logged, and the channel is deleted.
- **Auto-close** - tickets close automatically after 24h of inactivity
  (configurable) with a warning an hour beforehand.

Re-run `/ticket setup` at any time to change any setting - the existing panel is
edited in place rather than duplicated. See
[cogs/tickets/README.md](cogs/tickets/README.md).

### Prefix Commands (`.`)

- `.ping` - Check bot latency and database status
- `.sync` - Manually sync slash commands (owner only)
- `.reload [cog]` - Reload specific cog or all cogs (owner only)
- `.cogs` - List all loaded cogs (owner only)
- `.syncstatus` - Check command sync status (owner only)
- `.listcommands` - List all registered slash commands (owner only)
- `.clearglobal` - Clear all global slash commands (owner only)

## Project Structure

```
mercy/
├── main.py              # Bot entry point
├── cogs/                # Feature modules
│   ├── leaderboard/     # Activity tracking
│   ├── giveaways/       # Giveaway system
│   ├── quarantine/      # Moderation system
│   ├── purge/           # Message purging
│   ├── counting/        # Counting game
│   ├── skullboard/      # Message highlighting
│   ├── reminders/       # Scheduled reminders + interactive dashboard
│   ├── tickets/         # Ticket system (panel, staff controls, transcripts)
│   └── ...              # Other features
├── logs/                # Bot logs
└── database/            # Local database files
```

## Tests

```bash
python -m pytest tests -q
```

## Contributing

This is a private project. Contact the owner for contribution guidelines.

## License

Private - All Rights Reserved
