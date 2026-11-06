# Shivannnks Bot — Instagram Monitor

**Shivannnks Bot** is a Discord slash-command bot that monitors Instagram usernames and sends alerts when an account changes between **active** and **unavailable**.

The bot is designed for long-running hosting, with SQLite persistence, automatic monitor recovery after restarts, confirmation checks, and configurable Discord notifications.

> **Important:** `/ban` and `/unban` only start availability monitoring. They do not ban, unban, block, or otherwise modify an Instagram account.

## Features

### Account monitoring

- `/ban username` — monitor an account that is currently active and alert when it becomes unavailable.
- `/unban username` — monitor an unavailable account and alert when it becomes active again.
- `/stop username` — stop monitoring a username.
- `/status username` — check the latest known status of a username.
- `/list` — show configured monitors.
- `/history` — show recent status changes and monitoring events.
- `/account username` — show an Instagram-style profile card.
- `/watch usernames` — monitor multiple active accounts at once (space-separated).
- `/trend username` — show a followers trend graph for a monitored account.
- `/stats` — show a server dashboard summary of monitors and events.
- `/export` — export the full monitoring history as a CSV file.
- `/mute username minutes` — mute alerts for an account for a period.
- `/unmute username` — unmute alerts for an account immediately.
- `/mention username mention` — set a role/user ping sent on alerts for an account.
- `/interval username seconds` — set a per-monitor check interval (0 = global default).
- `/digest action` — send a digest now, or schedule daily/weekly digests.
- Accepts an Instagram username or a profile URL wherever a username is requested.
- Uses confirmation checks before sending a status-transition alert to reduce false positives.
- Optional stats-change alerts when an active account's followers, following, or post count moves by a configurable threshold.
- Optional profile-change alerts when an active account's bio, full name, verified badge, or external URL changes.
- Optional quiet hours window to suppress non-critical alerts during set times.
- Optional health-check HTTP endpoint for uptime monitoring on deployments.
- Stores monitors and event history in SQLite and resumes saved monitors after a restart.

### Discord notifications and presentation

- Sends automatic alerts when a confirmed status transition is detected.
- Sends rename alerts when an account reappears under a new handle (same Instagram user id) instead of a false ban alert.
- Sends stats-change alerts (followers/following/posts) without extra Instagram requests, using values already stored each cycle.
- Optional daily or weekly digest summaries of monitoring activity.
- Per-monitor alert muting and configurable role/user pings on alerts.
- Saves a profile snapshot (photo + bio) to disk on each transition for a forensic trail.
- Uses mobile-friendly embeds with clear status labels, timestamps, and useful empty and error states.
- Provides an Instagram-style profile card with a profile photo and available public statistics when Instagram makes that information accessible.

### Operational behavior

- Treats temporary network, login, and rate-limit failures as `UNKNOWN`, not as an account ban.
- Detects HTTP 429 responses and applies a backoff window instead of hammering Instagram.
- Per-monitor check intervals with a single tick-based loop (no per-account tasks).
- Concurrent checks with a bounded semaphore so cycle time stays manageable as monitors grow.
- Retries transient Discord gateway and network errors with bounded backoff.
- Stops on an invalid Discord token instead of retrying indefinitely.
- Can run directly as a Python worker or in Docker. A Render Blueprint is included for deployments that use the supplied `render.yaml`.

## Instagram status limitations

Instagram does not provide a simple official public API that reliably reports whether an arbitrary username has been banned. This bot therefore reports an **availability signal**, not a definitive account-enforcement decision.

The statuses should be interpreted as follows:

| Status | Meaning |
|---|---|
| `ACTIVE` | The checker was able to retrieve the profile using the configured checking method. |
| `UNAVAILABLE` | Instagram returned a response that the checker recognizes as an unavailable profile. This does **not** prove that Instagram banned the account; it may also be deleted, renamed, suspended, or otherwise inaccessible. |
| `UNKNOWN` | The checker could not determine the account's status because of a temporary network, rate-limit, login, or parsing problem. No ban or recovery transition should be inferred from this state. |

A detected transition is checked again before an alert is sent. This reduces false alerts, but cannot eliminate them. Instagram may change its website, login requirements, response formats, and rate limits. No third-party checker can guarantee complete accuracy or uninterrupted availability.

Use conservative polling intervals and respect Instagram's terms and rate limits. Do not treat `UNKNOWN` as `UNAVAILABLE`.

## Requirements

- Python 3.11 or newer
- A Discord application and bot token
- Outbound DNS and HTTPS access to Discord (`discord.com:443`)
- Persistent storage for `monitor.db` when deploying on a host with an ephemeral filesystem

## 1. Create and invite the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a new application.
3. Open **Bot** and create the bot user.
4. Copy the bot token and store it securely. Do not publish it or commit it to Git.
5. Under **OAuth2 → URL Generator** or **Installation**, select these scopes:
   - `bot`
   - `applications.commands`
6. Grant the bot the permissions it needs in your server and alert channels, including:
   - View Channels
   - Send Messages
   - Embed Links
   - Attach Files (if the profile-card implementation sends image files)
   - Mention Everyone (only if you intend to use `@everyone` alerts)
7. Open the generated invite URL and add the bot to your server.

Grant only the permissions the bot actually needs. `@everyone` mentions also depend on the destination channel's permission overwrites.

### Message Content Intent

Slash commands do not require the Message Content privileged intent. Enable **Bot → Privileged Gateway Intents → Message Content Intent** only if this project version also reads message content or supports prefix commands. If the code explicitly enables that intent, the setting must also be enabled in the Developer Portal.

## 2. Install the project

Clone the repository and open its directory:

```bash
git clone <REPOSITORY_URL>
cd <REPOSITORY_DIRECTORY>
```

Create a virtual environment:

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Activate it on Linux or macOS:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## 3. Configure environment variables

Copy `.env.example` to `.env`.

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Linux or macOS:

```bash
cp .env.example .env
```

At minimum, configure the Discord bot token:

```env
DISCORD_TOKEN=your_discord_bot_token
```

The visible branding defaults to `Shivannnks Bot` and can be changed with `BRAND_NAME`.

For faster slash-command updates while developing, you can also set your test server ID:

```env
DISCORD_GUILD_ID=your_discord_server_id
```

`DISCORD_GUILD_ID` is optional. When it is set, the bot syncs its current application-command definitions to that server. Guild commands are generally available more quickly during development. Without it, the bot syncs globally, and command updates may take longer to appear across Discord.

### Optional Instagram and monitoring settings

The following settings are optional. Use only the variables supported by your checked-out version of the project; see `.env.example` for the authoritative list.

```env
# Optional logged-in Instagram session cookie
INSTAGRAM_SESSIONID=your_sessionid

# Example tuning values
INSTAGRAM_RETRY_COUNT=2
CONFIRMATION_CHECKS=3
CHECK_INTERVAL_SECONDS=180
INSTAGRAM_CHECK_TIMEOUT_SECONDS=25

# How many monitors are checked in parallel each cycle
MAX_CONCURRENT_CHECKS=3

# The loop tick interval (affects per-monitor interval granularity)
CYCLE_TICK_SECONDS=30

# Stats-change alerts (notify when followers/following/posts move materially)
STATS_ALERTS_ENABLED=true
STATS_ALERT_MIN_DELTA=10
STATS_ALERT_COOLDOWN_SECONDS=1800

# Profile-metadata change alerts (bio/fullname/verified/external URL)
PROFILE_CHANGE_ALERTS_ENABLED=true

# Quiet hours in server local time, e.g. "23:00-08:00". Empty = always alert.
QUIET_HOURS=

# On-disk profile snapshots (photo + bio) on transitions
SNAPSHOT_DIR=snapshots

# Health-check HTTP server. Set to a port to enable.
HEALTH_PORT=0

# Digest scheduling: "daily", "weekly", or "" (disabled)
DIGEST_SCHEDULE=

# Instagram rate-limit backoff (seconds)
INSTAGRAM_RATE_LIMIT_BACKOFF_SECONDS=120
```

### Stats-change alerts

In addition to ban/recovery transitions, the bot can alert when an active account's followers, following, or post count changes by at least `STATS_ALERT_MIN_DELTA` between checks. These alerts are rate-limited per username by `STATS_ALERT_COOLDOWN_SECONDS` and only fire while an account stays in the same state, so they don't interfere with ban/recovery detection. Set `STATS_ALERTS_ENABLED=false` to disable them entirely. The comparison uses the stats already stored on each check, so no extra Instagram requests are made.

Keep `.env` private. An Instagram session cookie can grant access to your account, so treat it like a password. Do not paste it into public issues, screenshots, logs, or source control. A logged-in session may improve access in some cases, but it does not guarantee that Instagram checks will work.

## 4. Run the bot

Activate the virtual environment, then run:

```bash
python bot.py
```

When the bot connects, it loads the SQLite database and resumes saved monitors. Keep the process running continuously for monitoring and alerts to work.

## 5. Available slash commands

| Command | Description |
|---|---|
| `/ban username` | Start monitoring an active account until it becomes unavailable. |
| `/unban username` | Start monitoring an unavailable account until it becomes active again. |
| `/stop username` | Stop monitoring the specified username. |
| `/list` | Display configured monitors. |
| `/history` | Display recent monitoring events. |
| `/status username` | Show the latest known status for a username. |
| `/account username` | Show an Instagram-style profile card. |

You can enter a username or Instagram profile URL in commands that accept a username.

## 6. Hosting

The bot is a long-running worker. It is not a conventional request/response web application. The host must keep the Python process running and allow outbound DNS and HTTPS connections.

### Persistent SQLite storage

The default database is `monitor.db` (unless the project configuration specifies another path). If the host uses an ephemeral filesystem, the database may be lost when the instance is recreated or redeployed. Attach persistent storage and make sure the database file is written inside the mounted directory.

For deployments that need multiple bot instances or more demanding database operations, consider replacing SQLite with a managed database such as PostgreSQL. The database layer would need to be updated accordingly.

## 7. Troubleshooting

### Slash commands do not appear

- Confirm the bot was invited with both `bot` and `applications.commands` scopes.
- Set `DISCORD_GUILD_ID` to the ID of the server where you are testing.
- Restart the worker after changing the environment variable.
- Check the logs for a successful command sync.
- Confirm that you are testing in the same server whose ID is configured.

If `DISCORD_GUILD_ID` is not set, the bot uses global command sync. Global command updates can take longer to propagate.

### `401 Unauthorized` or `Improper token has been passed`

The Discord token is invalid, expired, or incorrectly copied.

1. Open the Discord Developer Portal and reset the bot token if needed.
2. Update `DISCORD_TOKEN` in the host's environment settings.
3. Paste the raw token value only. Do not include `DISCORD_TOKEN=`, quotes, or backticks.
4. Restart the worker.

Never share the token in logs or screenshots.

### `Cannot connect to host discord.com`, `socket.gaierror`, or gateway timeouts

These errors usually indicate a DNS or network problem on the hosting server, rather than an Instagram status-check result.

The bot retries supported transient connection failures with bounded backoff. Confirm that the host has working outbound DNS and HTTPS access to `discord.com:443`. Restart the worker after a temporary outage. If the problem continues, contact the hosting provider or move the worker to a host with working outbound connectivity.

### Instagram status is `UNKNOWN`

Possible causes include rate limiting, login requirements, temporary network failures, or changes to Instagram's response format. `UNKNOWN` is not a ban. Avoid reducing the polling interval to force a result; more frequent requests can increase rate limiting.

If the project supports it, configure a valid session cookie and use a conservative cache and retry configuration. Session cookies must be stored as secrets.

### Too many false transition alerts

Increase the number of confirmation checks and use a less aggressive polling interval. For example:

```env
CONFIRMATION_CHECKS=3
CHECK_INTERVAL_SECONDS=180
INSTAGRAM_CHECK_TIMEOUT_SECONDS=25
```

These values are examples, not guaranteed optimal settings. Increasing the interval reduces request frequency but may make alerts slower. Confirm that the variables are supported by your project version.

### Profile picture or profile card is missing

Instagram may not provide a usable public profile-picture URL or profile data for the current request. The bot should still send the text alert when a transition is confirmed. Profile cards depend on data being available and may not work for every account.

### Database or monitor history disappeared after redeployment

The database may have been stored on an ephemeral filesystem or outside the mounted persistent volume. Check the configured SQLite path and ensure that the directory containing `monitor.db` is persisted and writable by the bot process.

## 8. Security and GitHub

Do not commit secrets or runtime data to GitHub. At minimum, keep these files out of source control:

```text
.env
monitor.db
```

Also exclude any backups containing account data, session cookies, webhook URLs, or logs with sensitive information. The repository's `.gitignore` should cover these files.

If a Discord token, Instagram session cookie, or webhook URL is accidentally exposed, revoke or rotate it immediately. Removing it from the latest commit is not enough if it remains in Git history.

## 9. License and responsible use

Use and modify this project for your own Discord server. Respect Instagram's Terms of Use, rate limits, privacy settings, and applicable laws. Do not use the bot to harass, target, or make consequential decisions about people based on an inferred account status.
