import os
import asyncio
import csv
import io
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from database import Database
from instagram import InstagramChecker, AccountState
from cards import create_profile_card

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")
BRAND_NAME = os.getenv("BRAND_NAME", "Shivannnks Bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("shivannnks_bot")

def env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        log.warning("Invalid %s value; using %d.", name, default)
        return default

CHECK_INTERVAL = env_int("CHECK_INTERVAL_SECONDS", 120, 30)
# The monitor loop ticks at this short fixed interval and only checks monitors
# whose per-monitor interval (or the global default) is due. Enables per-monitor
# intervals without running a separate task per account.
CYCLE_TICK = env_int("CYCLE_TICK_SECONDS", 30, 10)
CONFIRMATION_CHECKS = env_int("CONFIRMATION_CHECKS", 2, 1)
MAX_CONCURRENT_CHECKS = env_int("MAX_CONCURRENT_CHECKS", 3, 1)
STATS_ALERT_MIN_DELTA = env_int("STATS_ALERT_MIN_DELTA", 10, 1)
STATS_ALERT_COOLDOWN_SECONDS = env_int("STATS_ALERT_COOLDOWN_SECONDS", 1800, 0)
STATS_ALERTS_ENABLED = os.getenv("STATS_ALERTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Bio/fullname/verified/external_url change alerts while an account stays active.
PROFILE_CHANGE_ALERTS_ENABLED = os.getenv("PROFILE_CHANGE_ALERTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Quiet hours in the server's local time, e.g. "23:00-08:00". Alerts are queued/suppressed.
QUIET_HOURS = os.getenv("QUIET_HOURS", "").strip()
# Where on-disk profile snapshots (pic + bio) are written on transitions.
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "snapshots")
# Optional health-check HTTP server. Set HEALTH_PORT to enable.
HEALTH_PORT = env_int("HEALTH_PORT", 0, 0)
# Digest scheduling: "daily", "weekly", or "" (disabled). Sent to the channel of
# the first monitor at startup, or configure per-server with /digest.
DIGEST_SCHEDULE = os.getenv("DIGEST_SCHEDULE", "").strip().lower()

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
db = Database(os.getenv("STATE_FILE", "monitor.db"))
checker = InstagramChecker()

# In-memory cooldown so one account can't spam stats alerts on every cycle.
# Maps username -> cooldown deadline (monotonic seconds from loop.time()).
_stats_cooldown: dict[str, float] = {}
# Lightweight runtime metrics surfaced by the health-check endpoint.
_health = {
    "last_cycle_at": None,
    "last_cycle_monitors": 0,
    "cycles": 0,
    "started_at": datetime.now(timezone.utc).isoformat(),
}

def utcnow():
    return datetime.now(timezone.utc)

def elapsed_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def clean_username(value: str) -> str:
    value = value.strip()
    value = value.rstrip("/")
    if "instagram.com/" in value:
        value = value.split("instagram.com/", 1)[1].split("?", 1)[0].split("/", 1)[0]
    value = value.lstrip("@").strip()
    value = value.lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", value):
        return ""
    return value

def in_quiet_hours(now: datetime | None = None) -> bool:
    """True if `now` (server local time) falls inside the QUIET_HOURS window."""
    if not QUIET_HOURS or "-" not in QUIET_HOURS:
        return False
    try:
        start_s, end_s = QUIET_HOURS.split("-", 1)
        now = now or datetime.now()
        start_h, start_m = map(int, start_s.strip().split(":"))
        end_h, end_m = map(int, end_s.strip().split(":"))
        cur = now.hour * 60 + now.minute
        start = start_h * 60 + start_m
        end = end_h * 60 + end_m
        if start == end:
            return False
        if start < end:
            return start <= cur < end
        # Window crosses midnight, e.g. 23:00-08:00.
        return cur >= start or cur < end
    except (ValueError, AttributeError):
        return False

def save_snapshot(profile, username, event: str) -> str | None:
    """Persist the profile pic bytes + a bio text file on a transition.
    Returns the directory path so it can be mentioned in the alert."""
    if not profile:
        return None
    try:
        base = Path(SNAPSHOT_DIR) / username
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        folder = base / f"{event}_{stamp}"
        folder.mkdir(parents=True, exist_ok=True)
        # Bio / metadata text
        meta = [
            f"username: {username}",
            f"event: {event}",
            f"detected_at: {datetime.now(timezone.utc).isoformat()}",
            f"followers: {profile.followers}",
            f"following: {profile.following}",
            f"posts: {profile.posts}",
            f"is_verified: {profile.is_verified}",
            f"fullname: {profile.fullname or ''}",
            f"external_url: {profile.external_url or ''}",
            f"userid: {profile.userid or ''}",
            "",
            "biography:",
            profile.bio or "",
        ]
        (folder / "profile.txt").write_text("\n".join(meta), encoding="utf-8")
        # Profile pic bytes
        if profile.profile_pic_url:
            import urllib.request
            req = urllib.request.Request(profile.profile_pic_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            })
            try:
                with urllib.request.urlopen(req, timeout=12) as r:
                    (folder / "profile_pic.jpg").write_bytes(r.read())
            except Exception:
                pass
        return str(folder)
    except Exception:
        log.exception("Snapshot save failed for @%s", username)
        return None

def render_trend(username: str, rows) -> str:
    """Render a followers sparkline PNG from stats_history rows (newest first)."""
    from PIL import Image, ImageDraw, ImageFont
    points = list(reversed(rows))
    followers = [r["followers"] for r in points]
    W, H = 760, 320
    pad_l, pad_r, pad_t, pad_b = 70, 30, 40, 50
    bg = Image.new("RGB", (W, H), (24, 24, 24))
    draw = ImageDraw.Draw(bg)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 18)
        small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
        small = font
    n = len(followers)
    lo, hi = min(followers), max(followers)
    span = max(1, hi - lo)
    def x(i):
        return pad_l + (W - pad_l - pad_r) * (i / max(1, n - 1))
    def y(v):
        return pad_t + (H - pad_t - pad_b) * (1 - (v - lo) / span)
    # Grid + axis labels
    draw.line((pad_l, H - pad_b, W - pad_r, H - pad_b), fill=(80, 80, 80), width=2)
    draw.line((pad_l, pad_t, pad_l, H - pad_b), fill=(80, 80, 80), width=2)
    draw.text((10, pad_t - 6), f"{hi:,}", fill=(180, 180, 180), font=small)
    draw.text((10, H - pad_b - 6), f"{lo:,}", fill=(180, 180, 180), font=small)
    # Polyline
    pts = [(x(i), y(v)) for i, v in enumerate(followers)]
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill=(20, 145, 245), width=3)
    for p in pts:
        draw.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=(20, 145, 245))
    draw.text((pad_l, 8), f"@{username} — followers", fill=(245, 245, 245), font=font)
    draw.text((pad_l, H - pad_b + 12), f"{n} checks", fill=(140, 140, 140), font=small)
    out = Path(tempfile.gettempdir()) / f"trend_{username}.png"
    bg.save(out, "PNG")
    return str(out)

async def get_channel(channel_id: int):
    """Resolve a channel, falling back to a fetch if it isn't in the cache."""
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        channel = await bot.fetch_channel(channel_id)
    except discord.NotFound:
        return None
    except discord.Forbidden:
        log.warning("Missing permissions to fetch channel %s", channel_id)
        return None
    except Exception:
        log.exception("Failed to fetch channel %s", channel_id)
        return None
    return channel

def stats_delta(monitor, profile):
    """Return a dict of fields whose absolute change >= the configured threshold,
    or None if nothing meaningful changed."""
    if not profile:
        return None
    fields = {
        "Followers": (monitor.followers, profile.followers),
        "Following": (monitor.following, profile.following),
        "Posts": (monitor.posts, profile.posts),
    }
    deltas = {}
    for name, (old, new) in fields.items():
        old = old or 0
        new = new or 0
        if abs(new - old) >= STATS_ALERT_MIN_DELTA and old != 0:
            deltas[name] = (old, new)
    return deltas or None

async def fetch_confirmed(username: str):
    """Require repeated identical results to reduce false ban/recovery alerts."""
    last = None
    stable = 0
    result = None
    for attempt in range(CONFIRMATION_CHECKS):
        result = await checker.check(username)
        if result.state == AccountState.UNKNOWN:
            return result
        if last == result.state:
            stable += 1
        else:
            stable = 1
            last = result.state
        if stable >= CONFIRMATION_CHECKS:
            return result
        if attempt < CONFIRMATION_CHECKS - 1:
            await asyncio.sleep(5)
    return result

def is_muted(monitor) -> bool:
    """True if a monitor is currently muted (its muted_until is in the future)."""
    until = getattr(monitor, "muted_until", None)
    if not until:
        return False
    try:
        return utcnow() < until
    except Exception:
        return False

async def send_event(channel: discord.abc.Messageable, monitor, event: str, profile, mention_override: str | None = None):
    elapsed = elapsed_text(int((utcnow() - monitor.started_at).total_seconds()))
    if event == "banned":
        title = f"❌ Account Banned Successfully — @{monitor.username} ❌"
        line = f"⏱️ Time Elapsed: {elapsed}"
        accent = "red"
    else:
        followers = profile.followers_display if profile else "Unknown"
        title = f"🏆 Account Recovered | @{monitor.username} ✅"
        line = f"👥 Followers: {followers} | ⏱️ Time Elapsed: {elapsed}"
        accent = "green"

    embed = discord.Embed(title=title, description=line, color=discord.Color.red() if accent == "red" else discord.Color.green())
    if profile:
        embed.set_thumbnail(url=profile.profile_pic_url) if profile.profile_pic_url else None
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Following", value=profile.following_display, inline=True)
        if profile.bio:
            embed.add_field(name="Bio", value=profile.bio[:200] or "—", inline=False)
        embed.set_footer(text=f"{BRAND_NAME} • automatic detection")

    # Role/user ping: explicit override wins, else the monitor's stored mention.
    mention = mention_override or getattr(monitor, "alert_mention", None)
    content = mention if mention else None

    card_path = None
    try:
        card_path = create_profile_card(profile, monitor.username, unavailable=(event == "banned"))
        if card_path:
            file = discord.File(card_path, filename="instagram-profile.png")
            embed.set_image(url="attachment://instagram-profile.png")
            await channel.send(content=content, embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
        else:
            await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Failed to create/send profile card")
        await channel.send(content=content, embed=embed, allowed_mentions=discord.AllowedMentions.none())
    finally:
        if card_path:
            try:
                os.remove(card_path)
            except OSError:
                pass

async def send_profile_change_alert(channel, monitor, changes, profile):
    """Alert when bio/fullname/verified/external_url changed while staying active."""
    parts = [f"📝 **Profile changed** for **@{monitor.username}**"]
    for label, (old, new) in changes.items():
        old_disp = (old or "—")[:120]
        new_disp = (new or "—")[:120]
        parts.append(f"• {label}: `{old_disp}` → `{new_disp}`")
    embed = discord.Embed(
        title=f"📝 Profile Change — @{monitor.username}",
        description="\n".join(parts),
        color=discord.Color.gold(),
    )
    if profile and profile.profile_pic_url:
        embed.set_thumbnail(url=profile.profile_pic_url)
    embed.set_footer(text=f"{BRAND_NAME} • automatic detection")
    mention = getattr(monitor, "alert_mention", None)
    await channel.send(content=mention, embed=embed, allowed_mentions=discord.AllowedMentions.none())

async def send_rename_alert(channel, monitor, new_username, profile):
    """Alert that an account was renamed rather than banned (same user id, new handle)."""
    embed = discord.Embed(
        title=f"🔀 Account Renamed — @{monitor.username} → @{new_username}",
        description=(
            f"**@{monitor.username}** is no longer available, but the account is still "
            f"active under a new handle.\n\n"
            f"➡️ New username: **@{new_username}**"
        ),
        color=discord.Color.purple(),
    )
    if profile:
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        if profile.profile_pic_url:
            embed.set_thumbnail(url=profile.profile_pic_url)
    embed.set_footer(text=f"{BRAND_NAME} • automatic detection")
    mention = getattr(monitor, "alert_mention", None)
    await channel.send(content=mention, embed=embed, allowed_mentions=discord.AllowedMentions.none())

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    await bot.change_presence(activity=discord.Game(name=f"{BRAND_NAME} | Instagram monitor"))
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d guild commands.", len(synced))
        else:
            synced = await bot.tree.sync()
            log.info("Synced %d global commands.", len(synced))
    except Exception:
        log.exception("Slash command sync failed")
    if not monitor_loop.is_running():
        monitor_loop.start()
    if DIGEST_SCHEDULE == "daily" and not digest_daily_loop.is_running():
        digest_daily_loop.start()
    if DIGEST_SCHEDULE == "weekly" and not digest_weekly_loop.is_running():
        digest_weekly_loop.start()

@bot.tree.command(name="ban", description="Monitor an active Instagram account until it becomes unavailable.")
@app_commands.describe(username="Instagram username or profile URL")
async def ban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)

    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(
            f"⚠️ I couldn't verify @{username} right now. Try again in a moment."
        )
    if result.state == AccountState.UNAVAILABLE:
        return await interaction.followup.send(
            f"⚠️ @{username} already appears unavailable. Use `/unban {username}` to monitor recovery."
        )

    db.add_monitor(username, "ban", interaction.channel_id, interaction.user.id, result)
    await interaction.followup.send(
        f"🔴 **Ban monitoring started** for **@{username}**.\n"
        f"Current status: 🟢 Active\n"
        f"I'll automatically alert this channel when the account becomes unavailable."
    )

@bot.tree.command(name="unban", description="Monitor an unavailable Instagram account until it recovers.")
@app_commands.describe(username="Instagram username or profile URL")
async def unban(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)

    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(
            f"⚠️ I couldn't verify @{username} right now. Try again in a moment."
        )
    if result.state == AccountState.ACTIVE:
        return await interaction.followup.send(
            f"⚠️ @{username} already appears active. Use `/ban {username}` to monitor a future ban."
        )

    db.add_monitor(username, "unban", interaction.channel_id, interaction.user.id, result)
    await interaction.followup.send(
        f"🟢 **Recovery monitoring started** for **@{username}**.\n"
        f"Current status: 🔴 Unavailable\n"
        f"I'll automatically alert this channel when the account becomes available again."
    )

@bot.tree.command(name="stop", description="Stop monitoring an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def stop(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    removed = db.stop_monitor(username)
    if not removed:
        return await interaction.response.send_message(
            f"⚠️ @{username} is not currently being monitored.", ephemeral=True
        )
    await interaction.response.send_message(f"🛑 Stopped monitoring **@{username}**.")

@bot.tree.command(name="list", description="List all active Instagram monitors.")
async def list_monitors(interaction: discord.Interaction):
    monitors = db.get_monitors()
    if not monitors:
        return await interaction.response.send_message("📱 No active monitors.")

    ban_items = [m for m in monitors if m.mode == "ban"]
    unban_items = [m for m in monitors if m.mode == "unban"]
    lines = [f"📱 **{BRAND_NAME} — Instagram Monitors**", ""]
    lines.append("🔴 **BAN MONITORS**")
    lines.extend([f"• @{m.username}" for m in ban_items] or ["• None"])
    lines.append("")
    lines.append("🟢 **RECOVERY MONITORS**")
    lines.extend([f"• @{m.username}" for m in unban_items] or ["• None"])
    lines.append("")
    lines.append(f"**Total:** {len(monitors)}")
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="history", description="Show recent monitoring events.")
@app_commands.describe(limit="Number of events to show (1-20)")
async def history(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
    rows = db.get_history(limit)
    if not rows:
        return await interaction.response.send_message("📜 No monitoring history yet.")
    lines = [f"📜 **{BRAND_NAME} — Monitoring History**", ""]
    for row in rows:
        emoji = "❌" if row["event"] == "banned" else "🏆"
        lines.append(
            f"{emoji} **@{row['username']}** — {row['event'].title()} — "
            f"{elapsed_text(row['elapsed_seconds'])}"
        )
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="status", description="Check the current status of an Instagram account.")
@app_commands.describe(username="Instagram username or profile URL")
async def status(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(f"⚠️ Could not verify @{username} right now.")
    profile = result.profile
    embed = discord.Embed(
        title=f"📱 {BRAND_NAME} — Instagram Status — @{username}",
        color=discord.Color.green() if result.state == AccountState.ACTIVE else discord.Color.red()
    )
    embed.set_footer(text=BRAND_NAME)
    embed.add_field(name="Status", value="🟢 Active" if result.state == AccountState.ACTIVE else "🔴 Unavailable", inline=False)
    if profile:
        embed.add_field(name="Posts", value=str(profile.posts), inline=True)
        embed.add_field(name="Followers", value=profile.followers_display, inline=True)
        embed.add_field(name="Following", value=profile.following_display, inline=True)
        if profile.profile_pic_url:
            embed.set_thumbnail(url=profile.profile_pic_url)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="account", description="Show an Instagram profile card.")
@app_commands.describe(username="Instagram username or profile URL")
async def account(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)

    await interaction.response.defer()
    result = await checker.check(username)
    if result.state == AccountState.UNKNOWN:
        return await interaction.followup.send(f"⚠️ Could not load @{username} right now.")

    card_path = create_profile_card(
        result.profile,
        username,
        unavailable=result.state == AccountState.UNAVAILABLE,
    )
    if not card_path:
        return await interaction.followup.send(f"📱 @{username} is {result.state.value}.")
    try:
        await interaction.followup.send(
            content=f"📱 @{username} — {'Active' if result.state == AccountState.ACTIVE else 'Unavailable'}",
            file=discord.File(card_path, filename="instagram-profile.png"),
        )
    finally:
        try:
            os.remove(card_path)
        except OSError:
            pass

@bot.tree.command(name="mute", description="Mute alerts for a monitored account for a period.")
@app_commands.describe(username="Instagram username or profile URL", minutes="Minutes to mute (1-10080, default 60)")
async def mute(interaction: discord.Interaction, username: str, minutes: app_commands.Range[int, 1, 10080] = 60):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    monitors = {m.username: m for m in db.get_monitors()}
    if username not in monitors:
        return await interaction.response.send_message(f"⚠️ @{username} is not currently being monitored.", ephemeral=True)
    until = utcnow() + timedelta(minutes=minutes)
    db.set_muted_until(username, until)
    await interaction.response.send_message(f"🔇 Muted **@{username}** for {minutes} minute(s). Alerts resume {until.isoformat()} UTC.")

@bot.tree.command(name="unmute", description="Unmute alerts for a monitored account immediately.")
@app_commands.describe(username="Instagram username or profile URL")
async def unmute(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    monitors = {m.username: m for m in db.get_monitors()}
    if username not in monitors:
        return await interaction.response.send_message(f"⚠️ @{username} is not currently being monitored.", ephemeral=True)
    db.set_muted_until(username, None)
    await interaction.response.send_message(f"🔊 Unmuted **@{username}**.")

@bot.tree.command(name="mention", description="Set a role/user ping sent on alerts for a monitored account.")
@app_commands.describe(username="Instagram username or profile URL", mention="Role/user mention (e.g. @mods), or 'none' to clear")
async def mention(interaction: discord.Interaction, username: str, mention: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    raw = mention.strip()
    if raw.lower() in ("none", "off", "clear"):
        raw = ""
    db.set_alert_mention(username, raw)
    label = raw if raw else "no mention"
    await interaction.response.send_message(f"🔔 Alert mention for **@{username}** set to {label}.")

@bot.tree.command(name="interval", description="Set a per-monitor check interval (seconds). 0 = use global default.")
@app_commands.describe(username="Instagram username or profile URL", seconds="Seconds between checks (0-3600, 0 = default)")
async def interval(interaction: discord.Interaction, username: str, seconds: app_commands.Range[int, 0, 3600]):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    monitors = {m.username: m for m in db.get_monitors()}
    if username not in monitors:
        return await interaction.response.send_message(f"⚠️ @{username} is not currently being monitored.", ephemeral=True)
    db.set_check_interval(username, seconds)
    eff = seconds if seconds else CHECK_INTERVAL
    await interaction.response.send_message(f"⏱️ Check interval for **@{username}** set to {eff}s.")

@bot.tree.command(name="watch", description="Monitor multiple active accounts at once (batch add).")
@app_commands.describe(usernames="Space-separated Instagram usernames or profile URLs")
async def watch(interaction: discord.Interaction, usernames: str):
    raw = [clean_username(u) for u in usernames.split()]
    valid = [u for u in raw if u]
    if not valid:
        return await interaction.response.send_message("❌ No valid usernames found.", ephemeral=True)
    await interaction.response.defer()
    started, skipped, failed = [], [], []
    for username in valid:
        result = await checker.check(username)
        if result.state == AccountState.UNKNOWN:
            failed.append(username)
            continue
        if result.state == AccountState.UNAVAILABLE:
            skipped.append(username)
            continue
        db.add_monitor(username, "ban", interaction.channel_id, interaction.user.id, result)
        started.append(username)
    lines = [f"📥 **Batch add results**"]
    lines.append(f"✅ Started ban monitoring: {', '.join('@'+u for u in started) or 'None'}")
    lines.append(f"⏭️ Skipped (already unavailable): {', '.join('@'+u for u in skipped) or 'None'}")
    lines.append(f"⚠️ Couldn't verify: {', '.join('@'+u for u in failed) or 'None'}")
    await interaction.followup.send("\n".join(lines))

@bot.tree.command(name="trend", description="Show a followers trend graph for a monitored account.")
@app_commands.describe(username="Instagram username or profile URL")
async def trend(interaction: discord.Interaction, username: str):
    username = clean_username(username)
    if not username:
        return await interaction.response.send_message("❌ Invalid Instagram username.", ephemeral=True)
    rows = db.get_stats_history(username, limit=60)
    if len(rows) < 2:
        return await interaction.response.send_message(f"📈 Not enough data yet for **@{username}**. Try again after a few check cycles.")
    await interaction.response.defer()
    path = render_trend(username, rows)
    try:
        await interaction.followup.send(
            content=f"📈 Followers trend for **@{username}** ({len(rows)} points)",
            file=discord.File(path, filename="trend.png"),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

@bot.tree.command(name="stats", description="Show a server dashboard summary of monitors and events.")
async def stats(interaction: discord.Interaction):
    s = db.get_dashboard_stats()
    lines = [
        f"📊 **{BRAND_NAME} — Dashboard**",
        "",
        f"📱 Active monitors: **{s['monitors']}** ({s['ban']} ban / {s['unban']} recovery)",
        f"🔄 Total transitions logged: **{s['transitions']}**",
        f"   • ❌ Banned events: {s['banned']}",
        f"   • 🏆 Recovered events: {s['recovered']}",
        f"🕒 Last event: {s['last_event'] or 'none'}",
    ]
    await interaction.response.send_message("\n".join(lines))

@bot.tree.command(name="export", description="Export monitoring history as a CSV file.")
async def export_history(interaction: discord.Interaction):
    rows = db.get_all_history()
    if not rows:
        return await interaction.response.send_message("📜 No monitoring history to export.")
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "username", "event", "detected_at", "elapsed_seconds", "followers", "following", "posts"])
    for r in rows:
        writer.writerow([r["id"], r["username"], r["event"], r["detected_at"], r["elapsed_seconds"], r["followers"], r["following"], r["posts"]])
    data = buf.getvalue().encode("utf-8")
    file = discord.File(io.BytesIO(data), filename="history.csv")
    await interaction.response.send_message("📄 Here's the full monitoring history as CSV.", file=file)

async def send_stats_alert(channel, monitor, deltas, profile):
    """Notify when an active account's followers/following/posts changed materially."""
    parts = [f"📊 **Stats changed** for **@{monitor.username}**"]
    for name, (old, new) in deltas.items():
        sign = "+" if new > old else ""
        parts.append(f"• {name}: {old:,} → {new:,} ({sign}{new - old:,})")
    if profile:
        parts.append(f"\n👥 Followers: {profile.followers_display}")
    embed = discord.Embed(
        title=f"📊 Stats Change — @{monitor.username}",
        description="\n".join(parts),
        color=discord.Color.blurple(),
    )
    if profile and profile.profile_pic_url:
        embed.set_thumbnail(url=profile.profile_pic_url)
    embed.set_footer(text=f"{BRAND_NAME} • automatic detection")
    await channel.send(embed=embed)

def _profile_changes(monitor, profile):
    """Return a dict of {label: (old, new)} for bio/fullname/verified/external_url
    changes, or None if nothing changed."""
    if not profile:
        return None
    candidates = {
        "Bio": (monitor.bio, profile.bio),
        "Full name": (monitor.fullname, profile.fullname),
        "Verified": (str(bool(monitor.is_verified)), str(bool(profile.is_verified))),
        "External URL": (monitor.external_url, profile.external_url),
    }
    changes = {}
    for label, (old, new) in candidates.items():
        if (old or "") != (new or ""):
            changes[label] = (old, new)
    return changes or None

async def _check_one_monitor(monitor, semaphore):
    """Process a single monitor within a bounded-concurrency slot."""
    async with semaphore:
        try:
            result = await checker.check(monitor.username)
            if result.state == AccountState.UNKNOWN:
                log.warning("Could not verify @%s", monitor.username)
                return

            previous = monitor.last_state

            if monitor.mode == "ban" and previous == AccountState.ACTIVE and result.state == AccountState.UNAVAILABLE:
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.UNAVAILABLE:
                    return
                db.update_last_check(monitor.username, confirmed)
                # Rename detection: if we have a stored userid, look it up by id.
                # If the same identity is active under a new handle, it was renamed.
                renamed_to = None
                if monitor.userid:
                    by_id = await checker.check_by_userid(monitor.userid)
                    if by_id.state == AccountState.ACTIVE and by_id.profile and by_id.profile.username != monitor.username:
                        renamed_to = by_id.profile.username
                # Save a forensic snapshot (photo + bio) to disk before alerting.
                save_snapshot(confirmed.profile, monitor.username, "banned")
                channel = await get_channel(monitor.channel_id)
                if channel and not is_muted(monitor):
                    if renamed_to:
                        await send_rename_alert(channel, monitor, renamed_to, confirmed.profile)
                    else:
                        await send_event(channel, monitor, "banned", confirmed.profile)
                event_label = "renamed" if renamed_to else "banned"
                db.add_history(monitor.username, event_label, monitor.started_at, confirmed.profile)
                db.stop_monitor(monitor.username)
                return

            if monitor.mode == "unban" and previous == AccountState.UNAVAILABLE and result.state == AccountState.ACTIVE:
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.ACTIVE:
                    return
                db.update_last_check(monitor.username, confirmed)
                save_snapshot(confirmed.profile, monitor.username, "recovered")
                channel = await get_channel(monitor.channel_id)
                if channel and not is_muted(monitor):
                    await send_event(channel, monitor, "recovered", confirmed.profile)
                db.add_history(monitor.username, "recovered", monitor.started_at, confirmed.profile)
                db.stop_monitor(monitor.username)
                return

            # No ban/recovery transition. While the account stays active, watch for
            # meaningful stats changes and profile-metadata changes.
            if result.state == AccountState.ACTIVE and previous == AccountState.ACTIVE:
                # Always record a stats snapshot for trend graphs (cheap, local).
                db.add_stats_history(monitor.username, result.profile)

                # Stats-change alerts (rate-limited per username).
                if STATS_ALERTS_ENABLED and not is_muted(monitor):
                    deltas = stats_delta(monitor, result.profile)
                    if deltas:
                        loop = asyncio.get_running_loop()
                        deadline = _stats_cooldown.get(monitor.username, 0.0)
                        if loop.time() >= deadline:
                            _stats_cooldown[monitor.username] = loop.time() + STATS_ALERT_COOLDOWN_SECONDS
                            if not in_quiet_hours():
                                channel = await get_channel(monitor.channel_id)
                                if channel:
                                    try:
                                        await send_stats_alert(channel, monitor, deltas, result.profile)
                                    except Exception:
                                        log.exception("Stats alert failed for @%s", monitor.username)

                # Profile-metadata change alerts (bio/fullname/verified/external_url).
                if PROFILE_CHANGE_ALERTS_ENABLED and not is_muted(monitor):
                    changes = _profile_changes(monitor, result.profile)
                    if changes:
                        if not in_quiet_hours():
                            channel = await get_channel(monitor.channel_id)
                            if channel:
                                try:
                                    await send_profile_change_alert(channel, monitor, changes, result.profile)
                                except Exception:
                                    log.exception("Profile change alert failed for @%s", monitor.username)

            db.update_last_check(monitor.username, result)
        except Exception:
            log.exception("Monitor error for @%s", monitor.username)

def _monitor_due(monitor, now_ts: float) -> bool:
    """True if this monitor should be checked on the current tick, based on its
    per-monitor interval or the global default."""
    interval = monitor.check_interval or CHECK_INTERVAL
    if interval <= 0:
        interval = CHECK_INTERVAL
    # `last_checked` is stored as a datetime; fall back to "due now" if unknown.
    last = getattr(monitor, "last_checked", None)
    if not last:
        return True
    last_ts = last.timestamp() if hasattr(last, "timestamp") else 0
    return (now_ts - last_ts) >= interval

@tasks.loop(seconds=CYCLE_TICK)
async def monitor_loop():
    monitors = db.get_monitors()
    if not monitors:
        return
    now_ts = utcnow().timestamp()
    # Only check monitors whose interval is due this tick.
    due = [m for m in monitors if _monitor_due(m, now_ts)]
    if not due:
        return
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    await asyncio.gather(*(_check_one_monitor(m, semaphore) for m in due))
    _health["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
    _health["last_cycle_monitors"] = len(due)
    _health["cycles"] += 1

@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()

async def _health_server():
    """Minimal HTTP server returning JSON status for uptime monitors."""
    from aiohttp import web

    async def handler(_request):
        s = db.get_dashboard_stats()
        body = {
            "ok": True,
            "started_at": _health["started_at"],
            "last_cycle_at": _health["last_cycle_at"],
            "last_cycle_monitors": _health["last_cycle_monitors"],
            "cycles": _health["cycles"],
            "monitors": s["monitors"],
            "transitions": s["transitions"],
        }
        return web.json_response(body)

    app = web.Application()
    app.router.add_get("/", handler)
    app.router.add_get("/health", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()
    log.info("Health endpoint listening on port %d", HEALTH_PORT)
    # Keep running for the lifetime of the bot.
    try:
        while not bot.is_closed():
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()

async def send_digest(channel):
    """Send a summary of recent monitoring activity to a channel."""
    s = db.get_dashboard_stats()
    rows = db.get_history(limit=20)
    lines = [
        f"📋 **{BRAND_NAME} — Digest**",
        "",
        f"📱 Active monitors: **{s['monitors']}** ({s['ban']} ban / {s['unban']} recovery)",
        f"🔄 Total transitions logged: **{s['transitions']}**",
    ]
    if rows:
        lines.append("")
        lines.append("**Recent events:**")
        for row in rows[:5]:
            emoji = "❌" if row["event"] in ("banned",) else ("🔀" if row["event"] == "renamed" else "🏆")
            lines.append(f"{emoji} @{row['username']} — {row['event'].title()} — {elapsed_text(row['elapsed_seconds'])}")
    else:
        lines.append("No events recorded yet.")
    await channel.send("\n".join(lines))

@bot.tree.command(name="digest", description="Send a monitoring digest summary now, or configure scheduled digests.")
@app_commands.describe(action="now to send immediately, daily or weekly to schedule")
async def digest(interaction: discord.Interaction, action: str = "now"):
    action = action.strip().lower()
    if action == "now":
        await interaction.response.defer()
        await send_digest(interaction.channel)
        await interaction.followup.send("📋 Digest sent above.")
    elif action in ("daily", "weekly"):
        global DIGEST_SCHEDULE
        DIGEST_SCHEDULE = action
        # Start the matching loop now, not just at boot. Without this, enabling
        # a schedule at runtime would set the flag but never spin up the task,
        # so digests would silently never fire.
        if action == "daily" and not digest_daily_loop.is_running():
            digest_daily_loop.start()
        if action == "weekly" and not digest_weekly_loop.is_running():
            digest_weekly_loop.start()
        await interaction.response.send_message(f"📋 Scheduled {action} digests enabled. They'll be posted to this channel.")
    else:
        await interaction.response.send_message("❌ Use `now`, `daily`, or `weekly`.", ephemeral=True)

@tasks.loop(hours=24)
async def digest_daily_loop():
    if DIGEST_SCHEDULE != "daily":
        return
    monitors = db.get_monitors()
    if not monitors:
        return
    channel = await get_channel(monitors[0].channel_id)
    if channel:
        await send_digest(channel)

@tasks.loop(hours=168)
async def digest_weekly_loop():
    if DIGEST_SCHEDULE != "weekly":
        return
    monitors = db.get_monitors()
    if not monitors:
        return
    channel = await get_channel(monitors[0].channel_id)
    if channel:
        await send_digest(channel)

@digest_daily_loop.before_loop
async def before_digest_daily():
    await bot.wait_until_ready()

@digest_weekly_loop.before_loop
async def before_digest_weekly():
    await bot.wait_until_ready()

def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from .env or the environment.")
    if HEALTH_PORT:
        bot.loop.create_task(_health_server())
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
