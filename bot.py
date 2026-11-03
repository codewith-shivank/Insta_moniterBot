import os
import asyncio
import logging
import re
from datetime import datetime, timezone

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
CONFIRMATION_CHECKS = env_int("CONFIRMATION_CHECKS", 2, 1)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
db = Database(os.getenv("STATE_FILE", "monitor.db"))
checker = InstagramChecker()

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

async def fetch_confirmed(username: str, expected: AccountState | None = None):
    """Require repeated identical results to reduce false ban/recovery alerts."""
    last = None
    stable = 0
    for _ in range(CONFIRMATION_CHECKS):
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
        await asyncio.sleep(5)
    return result

async def send_event(channel: discord.abc.Messageable, monitor, event: str, profile):
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
        embed.set_footer(text=f"{BRAND_NAME} • automatic detection")

    card_path = None
    try:
        card_path = create_profile_card(profile, monitor.username, unavailable=(event == "banned"))
        if card_path:
            file = discord.File(card_path, filename="instagram-profile.png")
            embed.set_image(url="attachment://instagram-profile.png")
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)
    except Exception:
        log.exception("Failed to create/send profile card")
        await channel.send(embed=embed)

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
    await interaction.followup.send(
        content=f"📱 @{username} — {'Active' if result.state == AccountState.ACTIVE else 'Unavailable'}",
        file=discord.File(card_path, filename="instagram-profile.png"),
    )

@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_loop():
    monitors = db.get_monitors()
    for monitor in monitors:
        try:
            result = await checker.check(monitor.username)
            if result.state == AccountState.UNKNOWN:
                log.warning("Could not verify @%s", monitor.username)
                continue

            previous = monitor.last_state

            if monitor.mode == "ban" and previous == AccountState.ACTIVE and result.state == AccountState.UNAVAILABLE:
                # Confirm transition before alerting.
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.UNAVAILABLE:
                    continue
                db.update_last_check(monitor.username, confirmed)
                channel = bot.get_channel(monitor.channel_id)
                if channel:
                    await send_event(channel, monitor, "banned", confirmed.profile)
                db.add_history(monitor.username, "banned", monitor.started_at, confirmed.profile)
                db.stop_monitor(monitor.username)

            elif monitor.mode == "unban" and previous == AccountState.UNAVAILABLE and result.state == AccountState.ACTIVE:
                confirmed = await fetch_confirmed(monitor.username)
                if confirmed.state != AccountState.ACTIVE:
                    continue
                db.update_last_check(monitor.username, confirmed)
                channel = bot.get_channel(monitor.channel_id)
                if channel:
                    await send_event(channel, monitor, "recovered", confirmed.profile)
                db.add_history(monitor.username, "recovered", monitor.started_at, confirmed.profile)
                db.stop_monitor(monitor.username)
            else:
                db.update_last_check(monitor.username, result)
        except Exception:
            log.exception("Monitor error for @%s", monitor.username)

@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()

def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing from .env or the environment.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
