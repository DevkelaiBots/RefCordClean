# bot.py
# RefCord — Discord referral/invites tracker bot
# ----------------------------------------------
# Requirements:
#   pip install -U discord.py aiosqlite python-dotenv
#
# Environment variables:
#   DISCORD_TOKEN          -> your bot token (required)
#   REFERRAL_REWARDS_JSON  -> optional JSON { "GUILD_ID": { "5": ROLE_ID, "10": ROLE_ID } }
#   DB_PATH                -> optional DB file path (e.g. /data/referrals.db on Railway)

from dotenv import load_dotenv
load_dotenv()

import os
import json
from typing import Dict, Optional, Tuple

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands

import logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger("refcord")

# ------------ Intents & Bot ------------
INTENTS = discord.Intents.default()
INTENTS.members = True  # to receive on_member_join

BOT = commands.Bot(command_prefix="!", intents=INTENTS)
TREE = BOT.tree

# ------------ Database ------------
DB_PATH = os.getenv("DB_PATH", "referrals.db")

CREATE_TABLES_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS user_invites (
  guild_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  code     TEXT    NOT NULL UNIQUE,
  uses     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(code)
);

CREATE TABLE IF NOT EXISTS join_events (
  guild_id INTEGER NOT NULL,
  joined_user_id INTEGER NOT NULL,
  inviter_code TEXT NOT NULL,
  ts INTEGER NOT NULL
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES_SQL)
        await db.commit()

# ------------ Invites cache ------------
invites_cache: Dict[int, Dict[str, int]] = {}

async def refresh_guild_invites(guild: discord.Guild):
    try:
        invites = await guild.invites()
        invites_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in invites}
        log.info(f"Refreshed invites for guild {guild.id} ({guild.name})")
    except discord.Forbidden:
        invites_cache[guild.id] = {}
        log.warning("Missing 'Manage Server' to read invites accurately.")
    except Exception as e:
        log.exception(f"Failed to refresh invites for guild {guild.id}: {e}")

# ------------ Rewards config ------------
def load_rewards_config() -> Dict[str, Dict[str, int]]:
    raw = os.getenv("REFERRAL_REWARDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        for g, mapping in list(data.items()):
            data[g] = {str(k): int(v) for k, v in mapping.items()}
        return data
    except Exception as e:
        log.error(f"Invalid REFERRAL_REWARDS_JSON: {e}")
        return {}

REFERRAL_REWARDS = load_rewards_config()

def next_award_roles_for(guild_id: int, total_referrals: int) -> Tuple[Optional[int], list]:
    mapping = REFERRAL_REWARDS.get(str(guild_id)) or {}
    if not mapping:
        return (None, [])
    thresholds = sorted((int(t) for t in mapping.keys()))
    eligible = [t for t in thresholds if t <= total_referrals]
    if not eligible:
        return (None, [])
    best = max(eligible)
    return (mapping[str(best)], [mapping[str(t)] for t in eligible])

# ------------ Utilities ------------
async def get_member_total_referrals(guild_id: int, user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(uses), 0) FROM user_invites WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        return row[0] or 0

async def add_or_upsert_invite_owner(guild_id: int, user_id: int, code: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO user_invites (guild_id, user_id, code, uses)
            VALUES (?, ?, ?, COALESCE((SELECT uses FROM user_invites WHERE code=?), 0))
            ON CONFLICT(code) DO UPDATE SET user_id=excluded.user_id, guild_id=excluded.guild_id
            """,
            (guild_id, user_id, code, code),
        )
        await db.commit()

async def increment_code_use(guild_id: int, used_code: str, joined_user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_invites SET uses = uses + 1 WHERE guild_id=? AND code=?",
            (guild_id, used_code),
        )
        await db.execute(
            "INSERT INTO join_events (guild_id, joined_user_id, inviter_code, ts) VALUES (?, ?, ?, strftime('%s','now'))",
            (guild_id, joined_user_id, used_code),
        )
        await db.commit()

def format_leaderboard_line(rank: int, name: str, total: int) -> str:
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
    return f"{medal} **{name}** — {total} referral(s)"

# ------------ Events ------------
@BOT.event
async def on_ready():
    log.info(f"Logged in as {BOT.user} (ID: {BOT.user.id})")
    await init_db()
    for guild in BOT.guilds:
        await refresh_guild_invites(guild)
    try:
        await TREE.sync()
        log.info("Slash commands synced.")
    except Exception as e:
        log.exception(f"Slash sync error: {e}")

@BOT.event
async def on_guild_join(guild: discord.Guild):
    await refresh_guild_invites(guild)

@BOT.event
async def on_invite_create(invite: discord.Invite):
    g = invite.guild
    invites_cache.setdefault(g.id, {})
    invites_cache[g.id][invite.code] = invite.uses or 0

@BOT.event
async def on_invite_delete(invite: discord.Invite):
    g = invite.guild
    if g and g.id in invites_cache and invite.code in invites_cache[g.id]:
        invites_cache[g.id].pop(invite.code, None)

@BOT.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    before = invites_cache.get(guild.id, {}).copy()
    try:
        after_invites = await guild.invites()
    except discord.Forbidden:
        log.warning("Missing permission to read invites on member join.")
        return
    except Exception as e:
        log.exception(f"Error fetching invites on member join: {e}")
        return

    used_code = None
    for inv in after_invites:
        prev = before.get(inv.code, 0)
        if (inv.uses or 0) > prev:
            used_code = inv.code
            break

    invites_cache[guild.id] = {inv.code: (inv.uses or 0) for inv in after_invites}

    if not used_code:
        log.info(f"Join in {guild.name}, but invite code was not determined (vanity or uncached).")
        return

    await increment_code_use(guild.id, used_code, member.id)

    inviter_user_id: Optional[int] = None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM user_invites WHERE guild_id=? AND code=?",
            (guild.id, used_code),
        )
        row = await cur.fetchone()
        if row:
            inviter_user_id = int(row[0])

    if inviter_user_id:
        total = await get_member_total_referrals(guild.id, inviter_user_id)
        new_role_id, _eligible_roles = next_award_roles_for(guild.id, total)
        if new_role_id:
            role = guild.get_role(new_role_id)
            member_inviter = guild.get_member(inviter_user_id)
            if role and member_inviter:
                try:
                    if role not in member_inviter.roles:
                        await member_inviter.add_roles(role, reason=f"RefCord: {total} referrals")
                        log.info(f"Granted role {role.name} to {member_inviter} for {total} referrals.")
                except discord.Forbidden:
                    log.warning("Cannot add role; ensure bot role is above target role.")
                except Exception as e:
                    log.exception(f"Error granting role: {e}")

# ------------ Errors ------------
@BOT.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception(f"Slash command error: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ Error while processing your command.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Error while processing your command.", ephemeral=True)
    except Exception:
        pass

# ------------ Slash Commands ------------
@TREE.command(name="ping", description="Health check.")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong 🏓", ephemeral=True)

@TREE.command(name="create_personal_invite", description="Create your personal, trackable invite link.")
@app_commands.describe(
    channel="Channel where the invite points to.",
    max_uses="Max uses (0 = unlimited).",
    max_age_minutes="Validity in minutes (0 = never expires).",
)
async def create_personal_invite(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    max_uses: app_commands.Range[int, 0, 1000] = 0,
    max_age_minutes: app_commands.Range[int, 0, 7 * 24 * 60] = 0,
):
    await interaction.response.defer(ephemeral=True)
    try:
        invite = await channel.create_invite(
            max_uses=None if max_uses == 0 else max_uses,
            max_age=0 if max_age_minutes == 0 else max_age_minutes * 60,
            unique=True,
            reason=f"RefCord personal referral for {interaction.user}",
        )
    except discord.Forbidden:
        return await interaction.followup.send("I need **Create Invite** in that channel.", ephemeral=True)
    except Exception as e:
        log.exception(f"Failed to create invite: {e}")
        return await interaction.followup.send("Failed to create invite.", ephemeral=True)

    await add_or_upsert_invite_owner(interaction.guild_id, interaction.user.id, invite.code)
    invites_cache.setdefault(interaction.guild_id, {})
    invites_cache[interaction.guild_id][invite.code] = invite.uses or 0

    await interaction.followup.send(
        f"✅ Your personal invite:\n{invite.url}\n"
        f"- Max uses: {'Unlimited' if max_uses == 0 else max_uses}\n"
        f"- Expires: {'Never' if max_age_minutes == 0 else f'in {max_age_minutes} minute(s)'}\n\n"
        "Share it—joins via this link will count for you.",
        ephemeral=True,
    )

@TREE.command(name="my_referrals", description="See how many members you brought.")
async def my_referrals(interaction: discord.Interaction):
    total = await get_member_total_referrals(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message(f"👥 Your referrals: **{total}**", ephemeral=True)

@TREE.command(name="top_referrals", description="Show the leaderboard of top referrers.")
@app_commands.describe(limit="Number of results (1–25).")
async def top_referrals(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT user_id, SUM(uses) AS total
            FROM user_invites
            WHERE guild_id=?
            GROUP BY user_id
            ORDER BY total DESC
            LIMIT ?
            """,
            (interaction.guild_id, limit),
        )
        rows = await cur.fetchall()

    if not rows:
        return await interaction.response.send_message("No referral data yet.", ephemeral=True)

    lines = []
    for i, (user_id, total) in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(user_id))
        name = member.display_name if member else f"User({user_id})"
        lines.append(format_leaderboard_line(i, name, int(total)))

    embed = discord.Embed(
        title="🏆 Referral Leaderboard",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)

@TREE.command(name="create_public_invite", description="Create a public invite with custom limits.")
@app_commands.describe(
    channel="Target channel for the invite.",
    max_uses="Max uses (0 = unlimited).",
    max_age_minutes="Validity in minutes (0 = never expires).",
)
@commands.has_permissions(create_instant_invite=True)
async def create_public_invite(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    max_uses: app_commands.Range[int, 0, 1000] = 0,
    max_age_minutes: app_commands.Range[int, 0, 7 * 24 * 60] = 0,
):
    await interaction.response.defer(ephemeral=True)
    try:
        invite = await channel.create_invite(
            max_uses=None if max_uses == 0 else max_uses,
            max_age=0 if max_age_minutes == 0 else max_age_minutes * 60,
            unique=True,
        )
    except discord.Forbidden:
        return await interaction.followup.send("I need **Create Invite** in that channel.", ephemeral=True)
    except Exception as e:
        log.exception(f"Failed to create public invite: {e}")
        return await interaction.followup.send("Failed to create invite.", ephemeral=True)

    invites_cache.setdefault(interaction.guild_id, {})
    invites_cache[interaction.guild_id][invite.code] = invite.uses or 0

    await interaction.followup.send(f"🔗 Public invite created:\n{invite.url}", ephemeral=True)

@TREE.command(name="reload_rewards", description="Owner only: reload rewards config from env.")
async def reload_rewards(interaction: discord.Interaction):
    app = await BOT.application_info()
    if interaction.user.id != app.owner.id:
        return await interaction.response.send_message("Owner only.", ephemeral=True)
    global REFERRAL_REWARDS
    REFERRAL_REWARDS = load_rewards_config()
    await interaction.response.send_message("✅ Rewards config reloaded.", ephemeral=True)

def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable with your bot token.")
    BOT.run(token)

if __name__ == "__main__":
    main()
