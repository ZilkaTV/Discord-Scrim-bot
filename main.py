import os
import json
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timezone

# ─── Discord Intents ──────────────────────────────────────────────────────────
# Defines which Discord events the bot is allowed to receive.
# message_content: read message text | reactions: track ✅ | members: access member list

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.voice_states = True   # Required for the bot to join/leave voice channels


# ─── Channel & Role IDs ───────────────────────────────────────────────────────
# All Discord IDs used throughout the bot.
# To update: right-click a channel/role in Discord → Copy ID (Developer Mode must be on)

CHANNEL_ID             = 1466912142530969650   # Registration channel (where event posts + ✅ reactions go)
ROLE_ID                = 1466913367380726004   # Scrim registration role (given when user reacts ✅)
ACTIVE_ROLE_ID         = 1474720238695219220   # Active Scrim role (player is currently in a game VC)
SPECTATOR_ROLE_ID      = 1475139183147225240   # Spectator Scrim role (player is in the Meeting Point VC)
MENTION_ROLES          = [1467057562108039250, 1467057940409352377]  # Roles pinged on event creation
SCRIM_CHAT_ID          = 1466915521420329204   # Scrim chat channel (cleared on r!delete event)
EVENT_CHANNEL_ID       = 1467091170176929968   # Meeting Point voice channel (linked to Discord event)
GAME_LINKS_ID          = 1466911935395266641   # Game-links channel (winner messages tracked here)
LEADERBOARD_CHANNEL_ID = 1466915479661842725   # Channel where the leaderboard embed is posted


# ─── File Paths ───────────────────────────────────────────────────────────────
# JSON files used for persistent storage between bot restarts.
# DATA_DIR points to the Railway volume so files survive deployments.

DATA_DIR         = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)  # Create directory if it doesn't exist yet

ALLOWED_ROLES    = [1466913409340543027, 1466913296597909682]  # Staff, Host – required for all commands except r!stats

IDS_FILE           = os.path.join(DATA_DIR, "message_ids.json")
GUILD_CONFIGS_FILE = os.path.join(DATA_DIR, "guild_configs.json")

# Per-guild file helpers – each server gets its own data files
def guild_file(guild_id, name):
    """Returns the path to a guild-specific data file."""
    guild_dir = os.path.join(DATA_DIR, str(guild_id))
    os.makedirs(guild_dir, exist_ok=True)
    return os.path.join(guild_dir, name)

# Legacy paths (main server fallback – kept so existing data is not lost)
LEADERBOARD_FILE   = os.path.join(DATA_DIR, "leaderboard.json")
STATS_FILE         = os.path.join(DATA_DIR, "stats.json")
SEASON_FILE        = os.path.join(DATA_DIR, "season_stats.json")


# ─── Runtime State ────────────────────────────────────────────────────────────
# In-memory variables that track the current session.
# These reset on bot restart – they do NOT persist to disk.

warned_events            = set()   # Event IDs that already received the 30-minute warning
scrim_active             = False   # True once r!event update is used; activates the auto VC check loop
manually_deleting        = False   # True while r!delete event is running; prevents auto event restart
current_game_participants = set()  # User IDs who had Active Scrim role since the last r!event update
                                   # Everyone in this set counts as "has played" when a game is logged
pending_setups           = {}      # Guild ID → setup wizard state (active r!setup sessions)


# ─── Bot Initialization ───────────────────────────────────────────────────────
# Creates the bot instance with the command prefix "r!" and the intents above.

bot = commands.Bot(command_prefix="r!", intents=intents, case_insensitive=True)


# ─── Permission Check ─────────────────────────────────────────────────────────
# Reusable check that restricts a command to users with Staff or Host role.
# Applied with @bot.check or as a decorator on individual commands.
# r!stats is excluded and remains open to everyone.

def has_allowed_role():
    """Returns a command check that passes only if the user has Staff or Host role (per guild config).
    Server administrators always pass regardless of roles."""
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator:
            return True
        guild_allowed = get_cfg(ctx.guild.id, "allowed_roles")
        user_role_ids = [r.id for r in ctx.author.roles]
        if any(role_id in user_role_ids for role_id in guild_allowed):
            return True
        await ctx.send("❌ You don't have permission to use this command.")
        return False
    return commands.check(predicate)


# ─── Guild Config Helpers ─────────────────────────────────────────────────────
# Each guild stores its own channel/role IDs in guild_configs.json.
# get_cfg() returns the guild-specific value, or falls back to the hardcoded
# constants (so the main server works without any setup).

# Mapping from config key → hardcoded fallback constant
_DEFAULTS = {
    "channel_id":                   CHANNEL_ID,
    "role_id":                      ROLE_ID,
    "active_role_id":               ACTIVE_ROLE_ID,
    "spectator_role_id":            SPECTATOR_ROLE_ID,
    "mention_roles":                MENTION_ROLES,
    "scrim_chat_id":                SCRIM_CHAT_ID,
    "event_channel_id":             EVENT_CHANNEL_ID,
    "game_links_id":                GAME_LINKS_ID,
    "leaderboard_channel_id":       LEADERBOARD_CHANNEL_ID,
    "registered_teams_channel_id":  1496878585905152031,
    "allowed_roles":                ALLOWED_ROLES,
}

def load_guild_configs() -> dict:
    """Load guild_configs.json → {guild_id: {key: value}}. Returns {} if file doesn't exist."""
    if os.path.exists(GUILD_CONFIGS_FILE):
        with open(GUILD_CONFIGS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_guild_configs(data: dict):
    """Write the given dict to guild_configs.json."""
    with open(GUILD_CONFIGS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_cfg(guild_id, key):
    """Return the guild-specific config value, or fall back to the hardcoded constant."""
    configs = load_guild_configs()
    guild_cfg = configs.get(str(guild_id), {})
    return guild_cfg.get(key, _DEFAULTS.get(key))

def set_cfg(guild_id, key, value):
    """Save a single config value for a guild."""
    configs = load_guild_configs()
    gid = str(guild_id)
    if gid not in configs:
        configs[gid] = {}
    configs[gid][key] = value
    save_guild_configs(configs)

def is_configured(guild_id) -> bool:
    """Returns True if the guild has a completed setup config."""
    configs = load_guild_configs()
    cfg = configs.get(str(guild_id), {})
    required = ["channel_id", "role_id", "active_role_id", "spectator_role_id",
                "scrim_chat_id", "event_channel_id", "game_links_id",
                "leaderboard_channel_id", "registered_teams_channel_id", "allowed_roles"]
    return all(k in cfg for k in required)


# ─── File Helpers ─────────────────────────────────────────────────────────────
# Functions for reading and writing the JSON storage files.

def load_data() -> dict:
    """Load message_ids.json → {event_id: message_id}. Returns {} if file doesn't exist."""
    if os.path.exists(IDS_FILE):
        with open(IDS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    """Write the given dict to message_ids.json."""
    with open(IDS_FILE, "w") as f:
        json.dump(data, f)


def get_all_message_ids(data: dict) -> set:
    """Extract just the message IDs (values) from the data dict as a set."""
    return set(data.values())


def load_leaderboard(guild_id=None) -> dict:
    """Load leaderboard for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "leaderboard.json") if guild_id else LEADERBOARD_FILE
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    # Fallback: if per-guild file missing, try legacy global file
    if guild_id and os.path.exists(LEADERBOARD_FILE):
        return {}  # Don't migrate old data – start fresh per guild
    return {}


def save_leaderboard(data: dict, guild_id=None):
    """Write leaderboard for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "leaderboard.json") if guild_id else LEADERBOARD_FILE
    with open(path, "w") as f:
        json.dump(data, f)


def load_stats(guild_id=None) -> dict:
    """Load stats for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "stats.json") if guild_id else STATS_FILE
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_stats(data: dict, guild_id=None):
    """Write stats for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "stats.json") if guild_id else STATS_FILE
    with open(path, "w") as f:
        json.dump(data, f)


def load_season(guild_id=None) -> dict:
    """Load season stats for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "season_stats.json") if guild_id else SEASON_FILE
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_season(data: dict, guild_id=None):
    """Write season stats for a specific guild, or the legacy global file."""
    path = guild_file(guild_id, "season_stats.json") if guild_id else SEASON_FILE
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_or_create_season(season: dict, user_id) -> dict:
    """Return the all-time season entry for a user, creating a default if missing."""
    user_id = str(user_id)
    if user_id not in season or not isinstance(season[user_id], dict):
        season[user_id] = {"wins": 0, "games_played": 0, "quarters": []}
    else:
        for field, default in [("wins", 0), ("games_played", 0), ("quarters", [])]:
            if field not in season[user_id]:
                season[user_id][field] = default
    return season[user_id]


def get_or_create_stats(stats: dict, user_id) -> dict:
    """
    Return the stats entry for a user, creating a full default entry if it doesn't exist.
    Fields:
      registered    – how many times r!event update was run while they had ✅
      attended      – how many times they were in a VC during r!event update
      games_played  – total games counted while they had Active Scrim role
      games_won     – total games where they were listed as winner
      win_streak    – current consecutive win streak
      best_streak   – personal best consecutive win streak
    """
    user_id = str(user_id)  # Always ensure string key
    default = {
        "registered":   0,
        "attended":     0,
        "games_played": 0,
        "games_won":    0,
        "win_streak":   0,
        "best_streak":  0,
    }
    # Create if missing, or reset if the entry is not a dict (corrupt data)
    if user_id not in stats or not isinstance(stats[user_id], dict):
        stats[user_id] = default
    else:
        # Migrate older entries that are missing the new game-tracking fields
        for field, default_val in default.items():
            if field not in stats[user_id]:
                stats[user_id][field] = default_val
    return stats[user_id]


# ─── Channel Helpers ──────────────────────────────────────────────────────────
# Utility for bulk-deleting messages in a channel (used during cleanup).

async def clear_channel(channel):
    """Delete up to 500 messages in a channel. Falls back to one-by-one if bulk purge fails."""
    try:
        deleted = await channel.purge(limit=500)
        print(f"{len(deleted)} messages deleted in {channel.name}")
    except Exception:
        try:
            async for message in channel.history(limit=500):
                try:
                    await message.delete()
                except Exception:
                    pass
            print(f"Channel {channel.name} cleared one by one")
        except Exception as e:
            print(f"Error clearing {channel.name}: {e}")
            raise e


# ─── Reaction / Role Helpers ──────────────────────────────────────────────────
# Functions for reading ✅ reactions and syncing the registration role.

async def get_all_reacted_ids(channel, message_ids: set) -> set:
    """
    Fetch all tracked messages and return a set of user IDs that reacted with ✅.
    Automatically removes message IDs that no longer exist (deleted messages).
    """
    reacted_ids = set()
    for msg_id in list(message_ids):
        try:
            msg = await channel.fetch_message(msg_id)
            for r in msg.reactions:
                if str(r.emoji) == "✅":
                    async for user in r.users():
                        if not user.bot:
                            reacted_ids.add(user.id)
        except discord.NotFound:
            print(f"Message {msg_id} not found, removing from active list")
            message_ids.discard(msg_id)
    return reacted_ids


async def sync_roles(guild, role, reacted_ids: set):
    """
    Add the registration role to everyone in reacted_ids.
    Remove it from anyone who is no longer in reacted_ids.
    """
    for user_id in reacted_ids:
        member = guild.get_member(user_id)
        if member and role not in member.roles:
            await member.add_roles(role)
    for member in role.members:
        if member.id not in reacted_ids:
            await member.remove_roles(role)


async def remove_active_role_all(guild):
    """Strip the Active Scrim role from every member who currently has it."""
    active_role = guild.get_role(get_cfg(guild.id, "active_role_id"))
    if active_role:
        for member in list(active_role.members):
            try:
                await member.remove_roles(active_role)
            except Exception as e:
                print(f"Error removing active role from {member.display_name}: {e}")


async def remove_spectator_role_all(guild):
    """Strip the Spectator Scrim role from every member who currently has it."""
    spectator_role = guild.get_role(get_cfg(guild.id, "spectator_role_id"))
    if spectator_role:
        for member in list(spectator_role.members):
            try:
                await member.remove_roles(spectator_role)
            except Exception as e:
                print(f"Error removing spectator role from {member.display_name}: {e}")


# ─── Bot Voice Channel Helpers ───────────────────────────────────────────────
# The bot joins the Meeting Point voice channel during an active scrim so Discord
# never auto-ends the scheduled event (Discord ends voice events when the VC is empty).
# The bot sits silently in the channel – it plays no audio and has no effect on users.

async def join_meeting_point(guild):
    """Connect the bot to the Meeting Point VC. Moves it there if already in another VC."""
    channel = guild.get_channel(get_cfg(guild.id, "event_channel_id"))
    if channel is None:
        print("Meeting Point channel not found, cannot join.")
        return False
    vc = guild.voice_client
    try:
        if vc and vc.is_connected():
            if vc.channel.id == get_cfg(guild.id, "event_channel_id"):
                return True  # Already in the right channel
            await vc.move_to(channel)
        else:
            await channel.connect(self_deaf=True, self_mute=True, reconnect=True)
        print(f"Bot joined Meeting Point: {channel.name}")
        return True
    except discord.Forbidden:
        print("Missing permissions to join Meeting Point!")
        return False
    except Exception as e:
        print(f"Error joining Meeting Point: {e}")
        return False


async def leave_voice(guild):
    """Disconnect the bot from whichever voice channel it is currently in."""
    vc = guild.voice_client
    if vc and vc.is_connected():
        try:
            await vc.disconnect(force=True)
            print("Bot left voice channel.")
        except Exception as e:
            print(f"Error leaving voice channel: {e}")



# Core function that decides who gets Active Scrim vs Spectator based on their VC.
# Also updates current_game_participants so game tracking always knows who is playing.
# Called both manually (r!event update) and automatically every minute (scrim_vc_check).

async def update_scrim_vc_roles(guild):
    """
    Scans all voice channels and assigns roles accordingly:
      - Meeting Point (EVENT_CHANNEL_ID)              → Spectator Scrim role (remove Active)
      - Other VC + has Scrim registration role        → Active Scrim role    (remove Spectator)
      - Other VC + NO Scrim registration role         → Spectator role       (remove Active)
      - Not in any voice channel                      → both roles removed

    Only registered players (with ROLE_ID) receive the Active Scrim role.
    Unregistered players in game VCs are treated as spectators.
    """
    global current_game_participants

    active_role    = guild.get_role(get_cfg(guild.id, "active_role_id"))
    spectator_role = guild.get_role(get_cfg(guild.id, "spectator_role_id"))
    scrim_role     = guild.get_role(get_cfg(guild.id, "role_id"))

    if not active_role or not spectator_role or not scrim_role:
        print(f"[update_scrim_vc_roles] Role not found! active={active_role} spectator={spectator_role} scrim={scrim_role}")
        print(f"[update_scrim_vc_roles] Config: active_role_id={get_cfg(guild.id,'active_role_id')} spectator_role_id={get_cfg(guild.id,'spectator_role_id')} role_id={get_cfg(guild.id,'role_id')}")
        return

    members_in_meeting_point = set()
    members_in_other_vc      = set()
    event_vc_id              = get_cfg(guild.id, "event_channel_id")

    for vc in guild.voice_channels:
        for member in vc.members:
            if member.bot:
                continue
            if vc.id == event_vc_id:
                members_in_meeting_point.add(member.id)
            else:
                members_in_other_vc.add(member.id)

    all_in_vc = members_in_meeting_point | members_in_other_vc

    # Meeting Point → Spectator, remove Active
    for member_id in members_in_meeting_point:
        member = guild.get_member(member_id)
        if member:
            if spectator_role not in member.roles:
                try:
                    await member.add_roles(spectator_role)
                except Exception as e:
                    print(f"Error adding spectator role to {member.display_name}: {e}")
            if active_role in member.roles:
                try:
                    await member.remove_roles(active_role)
                except Exception as e:
                    print(f"Error removing active role from {member.display_name}: {e}")

    # Other VCs → Active if registered, Spectator if not
    for member_id in members_in_other_vc:
        member = guild.get_member(member_id)
        if not member:
            continue
        has_scrim = scrim_role in member.roles
        print(f"[vc_roles] {member.display_name} in game VC | has_scrim_role={has_scrim}")
        if scrim_role in member.roles:
            # Registered → Active Scrim
            if active_role not in member.roles:
                try:
                    await member.add_roles(active_role)
                except Exception as e:
                    print(f"Error adding active role to {member.display_name}: {e}")
            if spectator_role in member.roles:
                try:
                    await member.remove_roles(spectator_role)
                except Exception as e:
                    print(f"Error removing spectator role from {member.display_name}: {e}")
            current_game_participants.add(member_id)
        else:
            # Not registered → Spectator only
            if spectator_role not in member.roles:
                try:
                    await member.add_roles(spectator_role)
                except Exception as e:
                    print(f"Error adding spectator role to {member.display_name}: {e}")
            if active_role in member.roles:
                try:
                    await member.remove_roles(active_role)
                except Exception as e:
                    print(f"Error removing active role from {member.display_name}: {e}")

    # Left all VCs → remove both roles
    for member in list(active_role.members):
        if member.id not in all_in_vc:
            try:
                await member.remove_roles(active_role)
            except Exception as e:
                print(f"Error removing active role from {member.display_name}: {e}")

    for member in list(spectator_role.members):
        if member.id not in all_in_vc:
            try:
                await member.remove_roles(spectator_role)
            except Exception as e:
                print(f"Error removing spectator role from {member.display_name}: {e}")

    print(
        f"[scrim_vc_check] Meeting Point: {len(members_in_meeting_point)} spectators | "
        f"Other VCs: {len(members_in_other_vc)} in game VCs | "
        f"Participant pool: {len(current_game_participants)}"
    )


# ─── Game Logging Helper ──────────────────────────────────────────────────────
# Central function for recording a game result.
# Called by both r!game winner and the on_message auto-detection.

async def log_game(guild, winner_ids: set, source: str = "manual"):
    """
    Records a completed game:
      - Gives games_played +1 to everyone in current_game_participants
      - Gives games_won +1 and updates win_streak/best_streak for each winner
      - Resets win_streak to 0 for participants who did NOT win
      - Also updates the legacy leaderboard.json for r!event leaderboard compatibility

    Parameters:
      guild       – the Discord guild object
      winner_ids  – set of user IDs that won this game
      source      – "manual" (r!game winner) or "auto" (game-links message)
    """
    if not current_game_participants:
        # Fallback: use everyone currently in game VCs (happens after bot restart)
        event_vc_id = get_cfg(guild.id, "event_channel_id")
        for vc in guild.voice_channels:
            if vc.id != event_vc_id:
                for member in vc.members:
                    if not member.bot:
                        current_game_participants.add(member.id)
        print(f"[log_game] Participant pool was empty – rebuilt from current VCs: {len(current_game_participants)} players")

    if not current_game_participants:
        print(f"[log_game] No participants found anywhere, skipping ({source})")
        return None

    stats      = load_stats(guild.id)
    leaderboard = load_leaderboard(guild.id)

    for user_id in current_game_participants:
        uid_str    = str(user_id)
        user_stats = get_or_create_stats(stats, uid_str)
        user_stats["games_played"] += 1

        if user_id in winner_ids:
            user_stats["games_won"]   += 1
            user_stats["win_streak"]  += 1
            if user_stats["win_streak"] > user_stats["best_streak"]:
                user_stats["best_streak"] = user_stats["win_streak"]
            # Also update legacy leaderboard
            leaderboard[uid_str] = leaderboard.get(uid_str, 0) + 1
        else:
            user_stats["win_streak"] = 0  # Loss or no-show breaks the streak

    save_stats(stats, guild.id)
    save_leaderboard(leaderboard, guild.id)

    winner_names = []
    for uid in winner_ids:
        member = guild.get_member(uid)
        if member:
            winner_names.append(member.display_name)

    print(
        f"[log_game] Game logged ({source}) | "
        f"Participants: {len(current_game_participants)} | "
        f"Winners: {', '.join(winner_names) or 'none'}"
    )
    return winner_names


# ─── Auto VC Check Task ───────────────────────────────────────────────────────
# Runs every 60 seconds once scrim_active is True (set by r!event update).
# Automatically keeps Active/Spectator roles up to date as people move between VCs.

@tasks.loop(minutes=1)
async def scrim_vc_check():
    """Every minute: if a scrim is active, re-evaluate all VC roles across all guilds."""
    if not scrim_active:
        return
    # Only update guilds that have a valid configuration
    for guild in bot.guilds:
        channel_id = get_cfg(guild.id, "channel_id")
        if channel_id and bot.get_channel(channel_id):
            await update_scrim_vc_roles(guild)


@scrim_vc_check.before_loop
async def before_scrim_vc_check():
    """Wait until the bot is fully connected before starting the loop."""
    await bot.wait_until_ready()


# ─── Bot Ready Event ──────────────────────────────────────────────────────────
# Runs once when the bot successfully connects to Discord.
# Syncs the registration role based on existing ✅ reactions, then starts the background loops.

@bot.event
async def on_ready():
    print("Bot ready")
    for guild in bot.guilds:
        try:
            channel_id = get_cfg(guild.id, "channel_id")
            role_id    = get_cfg(guild.id, "role_id")
            channel    = bot.get_channel(channel_id)
            if channel is None:
                print(f"[{guild.name}] Registration channel not found – skipping role sync (run r!setup)")
                continue
            role        = guild.get_role(role_id)
            data        = load_data()
            message_ids = get_all_message_ids(data)
            reacted_ids = await get_all_reacted_ids(channel, message_ids)
            await sync_roles(guild, role, reacted_ids)
            print(f"[{guild.name}] Roles synced across {len(message_ids)} active message(s)")
        except Exception as e:
            print(f"[{guild.name}] Error during startup sync: {e}")

    # Sync slash commands with Discord
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Error syncing slash commands: {e}")

    # Load persisted ticket states
    active_tickets.update(load_active_tickets())
    print(f"Loaded {len(active_tickets)} active ticket(s)")

    # Start loops only if not already running
    if not check_events.is_running():
        check_events.start()
        print("check_events loop started")
    if not scrim_vc_check.is_running():
        scrim_vc_check.start()
        print("scrim_vc_check loop started")


@bot.event
async def on_resumed():
    """Fires after every reconnect. Restarts any loops that may have stopped."""
    print("Bot resumed – checking loops...")
    if not check_events.is_running():
        check_events.start()
        print("check_events loop restarted after resume")
    if not scrim_vc_check.is_running():
        scrim_vc_check.start()
        print("scrim_vc_check loop restarted after resume")


# ─── Event Warning & Auto-Start Loop ─────────────────────────────────────────
# Runs every minute. Handles two things:
#   1. Sends a 30-minute warning embed to the registration channel before an event starts.
#   2. Automatically calls event.start() when the scheduled start time is reached.

@tasks.loop(minutes=1)
async def check_events():
    now = datetime.now(tz=timezone.utc)
    print(f"[check_events] tick at {now.strftime('%H:%M:%S')} UTC")
    for guild in bot.guilds:
        try:
            events = await guild.fetch_scheduled_events()
        except discord.errors.DiscordServerError as e:
            print(f"[check_events] Discord API unavailable (503), skipping tick: {e}")
            return
        except Exception as e:
            print(f"[check_events] Error fetching events, skipping tick: {e}")
            return
        for event in events:
            if event.status == discord.EventStatus.scheduled:
                diff = (event.start_time - now).total_seconds()

                # 30-minute warning (fires once per event, between 28–32 min remaining)
                # Wide window so a reconnect during that minute doesn't cause it to be missed
                if 1680 <= diff <= 1920 and event.id not in warned_events:
                    try:
                        channel    = bot.get_channel(get_cfg(guild.id, "channel_id"))
                        role       = guild.get_role(get_cfg(guild.id, "role_id"))
                        event_link = f"https://discord.com/events/{guild.id}/{event.id}"

                        # Check if this is a tournament event
                        t_data = load_tournament(guild.id)
                        is_tournament = t_data and t_data.get("event_id") == event.id

                        if is_tournament:
                            accepted = [t for t in t_data.get("teams", []) if t["status"] == "accepted"]
                            teams_list = "\n".join(
                                f"🏅 **{t['tag']} {t['name']}**" for t in accepted
                            ) or "No teams registered yet"
                            embed = discord.Embed(
                                title=f"⏰ {event.name} starts in 30 minutes!",
                                description=(
                                    f"The **{t_data['format'].upper()}** team scrim starts soon!\n"
                                    f"[View Event]({event_link})\n\n"
                                    f"**Registered Teams:**\n{teams_list}"
                                ),
                                color=discord.Color.yellow()
                            )
                        else:
                            embed = discord.Embed(
                                title=f"⏰ {event.name} starts in 30 minutes!",
                                description=f"Get ready! The event **{event.name}** starts in 30 minutes.\n[View Event]({event_link})",
                                color=discord.Color.yellow()
                            )

                        await channel.send(content=f"{role.mention}", embed=embed)
                        warned_events.add(event.id)
                        print(f"30 minute warning sent for {event.name}")
                    except Exception as e:
                        print(f"Error sending 30 minute warning: {e}")

                # Auto-start (fires within 60 seconds of scheduled start time)
                if -60 <= diff <= 0:
                    try:
                        await event.start()
                        print(f"Event {event.name} started!")
                        channel    = bot.get_channel(get_cfg(guild.id, "channel_id"))
                        role       = guild.get_role(get_cfg(guild.id, "role_id"))
                        event_link = f"https://discord.com/events/{guild.id}/{event.id}"
                        embed = discord.Embed(
                            title=f"🟢 {event.name} has started!",
                            description=f"The event **{event.name}** is now live!\n[Join Event]({event_link})",
                            color=discord.Color.green()
                        )
                        await channel.send(content=f"{role.mention}", embed=embed)
                    except Exception as e:
                        print(f"Error starting event {event.name}: {e}")


@check_events.before_loop
async def before_check():
    """Wait until the bot is fully connected before starting the loop."""
    await bot.wait_until_ready()


# ─── Auto Event Restart Guard ─────────────────────────────────────────────────
# Discord automatically ends a voice-channel event when the last person leaves the VC.
# This listener detects that and immediately recreates + restarts the event so the scrim
# stays active until someone explicitly runs r!delete event.
# The manually_deleting flag prevents this from firing during an intentional r!delete.

@bot.event
async def on_scheduled_event_update(before, after):
    global manually_deleting

    # Ignore if r!delete is currently running
    if manually_deleting:
        return

    # Only react to events that just ended/completed
    if after.status not in (discord.EventStatus.ended, discord.EventStatus.completed):
        return

    # Only restart if a scrim session is currently active
    if not scrim_active:
        return

    # Only restart events that are tracked (i.e. created via r!create)
    data = load_data()
    if str(after.id) not in data:
        return

    print(f"Event '{after.name}' was auto-ended by Discord – restarting it...")

    guild = after.guild
    try:
        new_event = await guild.create_scheduled_event(
            name=after.name,
            description=after.description or "",
            start_time=datetime.now(tz=timezone.utc),
            channel=guild.get_channel(get_cfg(guild.id, "event_channel_id")),
            entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only
        )
        await new_event.start()

        # Re-link the original registration message to the new event ID
        old_msg_id = data.pop(str(after.id))
        data[str(new_event.id)] = old_msg_id
        save_data(data)

        print(f"Event restarted as '{new_event.name}' (id: {new_event.id})")
    except Exception as e:
        print(f"Error restarting event: {e}")


# ─── Auto Game Detection (on_message) ────────────────────────────────────────
# Listens for messages posted in the game-links channel.
# If a message contains "winner" and at least one user mention, it is treated as a
# game result and logged automatically – no command needed.
# Format example: "winner @PlayerA @PlayerB"

@bot.event
async def on_message(message):
    # Always process commands first so other commands still work
    await bot.process_commands(message)

    # Forward to setup wizard if a setup is in progress for this guild
    if message.guild and message.guild.id in pending_setups:
        await on_message_setup(message)
        return

    # Handle active ticket conversations
    if message.guild and message.channel.id in active_tickets:
        await maybe_handle_ticket(message)
        return

    # Only react to messages in the game-links channel, not from the bot itself
    if not message.guild or message.channel.id != get_cfg(message.guild.id, "game_links_id") or message.author.bot:
        return

    content_lower = message.content.lower()
    if "winner" not in content_lower:
        return

    winner_ids = {m.id for m in message.mentions if not m.bot}
    if not winner_ids:
        return

    if not scrim_active:
        return  # Only track games during an active scrim session

    # Mark this message as counted immediately so r!event leaderboard won't double-count
    counted_file = guild_file(message.guild.id, "counted_messages.json")
    try:
        counted = set()
        if os.path.exists(counted_file):
            with open(counted_file, "r") as f:
                counted = set(json.load(f))
        counted.add(message.id)
        with open(counted_file, "w") as f:
            json.dump(list(counted), f)
    except Exception as e:
        print(f"[auto] Could not mark message as counted: {e}")

    winner_names = await log_game(message.guild, winner_ids, source="auto")
    if winner_names is not None:
        await message.add_reaction("✅")  # Confirm the game was recorded
        print(f"[auto] Game recorded from game-links post by {message.author.display_name}")


# ─── Command: r!setup ────────────────────────────────────────────────────────
# Interactive setup wizard for new servers.
# Walks through all required channels and roles step by step.
# Usage: r!setup

SETUP_STEPS = [
    ("channel_id",                "📋 **Registration Channel**\nMention the channel where event posts go (e.g. #register-for-scrim)"),
    ("scrim_chat_id",             "💬 **Scrim Chat Channel**\nMention the channel that gets cleared after each scrim (e.g. #scrim-chat)"),
    ("game_links_id",             "🔗 **Game Links Channel**\nMention the channel where winner messages are posted (e.g. #game-links)"),
    ("leaderboard_channel_id",    "🏆 **Leaderboard Channel**\nMention the channel where the leaderboard embed gets posted (e.g. #scrim-leaderboard)"),
    ("registered_teams_channel_id", "🎟️ **Registered Teams Channel**\nMention the channel where accepted tournament teams are listed (e.g. #registered-teams)"),
    ("event_channel_id",          "🔊 **Meeting Point Voice Channel**\nMention the voice channel that the bot joins to keep the event alive.\n> Voice channels can't be mentioned with # like text channels.\n> Use this format instead: `<#CHANNEL_ID>`\n> To get the ID: right-click the voice channel → Copy Channel ID, then type `<#` + the ID + `>`\n> Example: `<#1467091170176929968>`"),
    ("role_id",                   "✅ **Scrim Registration Role**\nMention the role players receive when they react ✅ (e.g. @Scrim Player)"),
    ("active_role_id",            "🎮 **Active Scrim Role**\nMention the role given to players in game VCs (e.g. @Active Scrim)"),
    ("spectator_role_id",         "👁️ **Spectator Role**\nMention the role given to players in the Meeting Point or unregistered players (e.g. @Spectator Scrim)"),
    ("mention_roles",             "📣 **Ping Roles** (for event announcements)\nMention ALL roles that should be pinged when an event is created (e.g. @Scrim News @Everyone)\n> Separate multiple roles with spaces"),
    ("allowed_roles",             "🔒 **Staff/Host Roles**\nMention ALL roles that are allowed to use bot commands (e.g. @Staff @Host)\n> Separate multiple roles with spaces"),
]


@bot.command()
async def setup(ctx):
    """Interactive setup wizard – walks through all required channels and roles."""
    guild = ctx.guild

    # Only admins can run setup
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only server administrators can run `r!setup`.")
        return

    if is_configured(guild.id):
        await ctx.send(
            f"⚠️ This server already has a configuration.\n"
            f"Use `r!setup reset` to start over, or `r!setupshow` to see current settings."
        )
        return

    pending_setups[guild.id] = {"step": 0, "data": {}, "channel": ctx.channel.id, "user": ctx.author.id}

    embed = discord.Embed(
        title="⚙️ SCRIM Bot Setup",
        description=(
            "Welcome! I'll walk you through setting up the SCRIM Bot on this server.\n\n"
            f"There are **{len(SETUP_STEPS)} steps**. For each step, just mention the channel or role.\n"
            "Type `r!setup cancel` at any time to stop.\n\n"
            "Let's start!"
        ),
        color=discord.Color.blurple()
    )
    await ctx.send(embed=embed)
    await _send_setup_step(ctx.channel, guild.id)


@bot.command(name="setupshow")
async def setup_show(ctx):
    """Shows the current configuration for this server."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only administrators can view the server configuration.")
        return

    guild   = ctx.guild
    configs = load_guild_configs()
    cfg     = configs.get(str(guild.id), {})

    if not cfg:
        await ctx.send("⚠️ No configuration found for this server. Run `r!setup` to set it up.")
        return

    lines = []
    key_labels = {
        "channel_id":                   "Registration Channel",
        "scrim_chat_id":                "Scrim Chat",
        "game_links_id":                "Game Links",
        "leaderboard_channel_id":       "Leaderboard",
        "registered_teams_channel_id":  "Registered Teams",
        "event_channel_id":             "Meeting Point VC",
        "role_id":                      "Scrim Registration Role",
        "active_role_id":               "Active Scrim Role",
        "spectator_role_id":            "Spectator Role",
        "mention_roles":                "Ping Roles",
        "allowed_roles":                "Staff/Host Roles",
    }
    channel_keys = {"channel_id", "scrim_chat_id", "game_links_id", "leaderboard_channel_id",
                    "registered_teams_channel_id", "event_channel_id"}
    role_keys    = {"role_id", "active_role_id", "spectator_role_id"}
    list_keys    = {"mention_roles", "allowed_roles"}

    for key, label in key_labels.items():
        val = cfg.get(key, "_(using default)_")
        if key in list_keys and isinstance(val, list):
            mentions = " ".join(f"<@&{v}>" for v in val)
            lines.append(f"**{label}:** {mentions}")
        elif key in channel_keys and isinstance(val, int):
            lines.append(f"**{label}:** <#{val}>")
        elif key in role_keys and isinstance(val, int):
            lines.append(f"**{label}:** <@&{val}>")
        else:
            lines.append(f"**{label}:** {val}")

    embed = discord.Embed(title="⚙️ Server Configuration", description="\n".join(lines), color=discord.Color.blurple())
    await ctx.send(embed=embed)


@bot.command(name="setupreset")
async def setup_reset(ctx):
    """Clears the configuration for this server so r!setup can be run again."""
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("❌ Only administrators can reset the configuration.")
        return

    configs = load_guild_configs()
    gid     = str(ctx.guild.id)
    if gid in configs:
        del configs[gid]
        save_guild_configs(configs)
        await ctx.send("✅ Configuration cleared. Run `r!setup` to configure the bot again.")
    else:
        await ctx.send("⚠️ No configuration found for this server.")


async def _send_setup_step(channel, guild_id):
    """Send the prompt for the current setup step."""
    state = pending_setups.get(guild_id)
    if not state:
        return
    step_index = state["step"]
    if step_index >= len(SETUP_STEPS):
        return
    key, prompt = SETUP_STEPS[step_index]
    embed = discord.Embed(
        title=f"Step {step_index + 1} of {len(SETUP_STEPS)}",
        description=prompt,
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Mention the channel or role | r!setup cancel to stop")
    await channel.send(embed=embed)


@bot.event
async def on_message_setup(message):
    """Handles setup wizard responses – called from on_message."""
    if message.author.bot:
        return
    guild_id = message.guild.id if message.guild else None
    if not guild_id or guild_id not in pending_setups:
        return

    state = pending_setups[guild_id]

    # Must be same channel and same user who started setup
    if message.channel.id != state["channel"] or message.author.id != state["user"]:
        return

    # Ignore bot commands so r!setup itself doesn't trigger the wizard
    if message.content.strip().lower().startswith("r!"):
        return

    # Cancel
    if message.content.strip().lower() in ("r!setup cancel",):
        del pending_setups[guild_id]
        await message.channel.send("❌ Setup cancelled.")
        return

    step_index = state["step"]
    key, _     = SETUP_STEPS[step_index]
    is_list    = key in ("mention_roles", "allowed_roles")

    # Parse roles or channels from mentions
    if is_list:
        ids = [r.id for r in message.role_mentions]
        if not ids:
            await message.channel.send("❌ No roles mentioned. Please mention at least one role (e.g. @Staff).")
            return
        state["data"][key] = ids
    elif message.role_mentions:
        state["data"][key] = message.role_mentions[0].id
    elif message.channel_mentions:
        state["data"][key] = message.channel_mentions[0].id
    else:
        # Try raw ID
        raw = message.content.strip().strip("<#@&>")
        if raw.isdigit():
            state["data"][key] = int(raw)
        else:
            await message.channel.send("❌ Couldn't read that. Please mention the channel or role directly.")
            return

    state["step"] += 1

    if state["step"] >= len(SETUP_STEPS):
        # Save config
        configs  = load_guild_configs()
        gid      = str(guild_id)
        configs[gid] = state["data"]
        save_guild_configs(configs)
        del pending_setups[guild_id]

        embed = discord.Embed(
            title="✅ Setup Complete!",
            description=(
                "The SCRIM Bot is now configured for this server.\n\n"
                "Use `r!setupshow` to review your settings.\n"
                "Use `r!cmd` to see all available commands.\n"
                "Use `r!setupreset` to start over if needed."
            ),
            color=discord.Color.green()
        )
        await message.channel.send(embed=embed)
    else:
        await _send_setup_step(message.channel, guild_id)


# ─── Command: r!game winner @player1 @player2 ... ────────────────────────────
# Manually logs a game result. Mention all players who won.
# All current_game_participants count as having played.
# Usage: r!game winner @PlayerA @PlayerB

@bot.command()
@has_allowed_role()
async def game(ctx, subcommand: str = None, *, args=None):
    global current_game_participants

    if subcommand is None or subcommand.lower() != "winner":
        await ctx.send(
            "❌ Wrong format!\n"
            "Use: `r!game winner @Player1 @Player2 ...`\n"
            "Mention all players who **won** the game."
        )
        return

    if not scrim_active:
        await ctx.send("❌ No active scrim session! Use `r!event update` first.")
        return

    winner_ids = {m.id for m in ctx.message.mentions if not m.bot}
    if not winner_ids:
        await ctx.send("❌ Please mention at least one winner!")
        return

    if not current_game_participants:
        await ctx.send("❌ No participants tracked yet! Make sure `r!event update` was used and players are in VCs.")
        return

    winner_names = await log_game(ctx.guild, winner_ids, source="manual")

    if winner_names is None:
        await ctx.send("⚠️ Game could not be logged, no participants tracked.")
        return

    stats = load_stats(guild.id)
    loser_names = []
    for uid in current_game_participants:
        if uid not in winner_ids:
            member = ctx.guild.get_member(uid)
            if member:
                loser_names.append(member.display_name)

    embed = discord.Embed(
        title="🎮 Game Logged!",
        color=discord.Color.green()
    )
    embed.add_field(
        name="🏆 Winners",
        value=", ".join(winner_names) if winner_names else "—",
        inline=False
    )
    embed.add_field(
        name="❌ Losses recorded for",
        value=", ".join(loser_names) if loser_names else "—",
        inline=False
    )
    embed.add_field(
        name="👥 Total participants",
        value=str(len(current_game_participants)),
        inline=True
    )

    # Show updated streaks for winners
    streak_lines = []
    for uid in winner_ids:
        member = ctx.guild.get_member(uid)
        if member:
            uid_str    = str(uid)
            user_stats = stats.get(uid_str, {})
            streak     = user_stats.get("win_streak", 0)
            best       = user_stats.get("best_streak", 0)
            fire       = " 🔥" if streak >= 3 else ""
            streak_lines.append(f"{member.display_name}: {streak} streak{fire} (best: {best})")
    if streak_lines:
        embed.add_field(name="📈 Win Streaks", value="\n".join(streak_lines), inline=False)

    await ctx.send(embed=embed)

    # Post a winner announcement in the game-links channel
    game_links_channel = bot.get_channel(get_cfg(guild.id, "game_links_id"))
    if game_links_channel:
        winner_mentions = " ".join(f"<@{uid}>" for uid in winner_ids)
        total_games = load_stats(guild.id).get(str(next(iter(winner_ids))), {}).get("games_won", "?")
        streak_parts = []
        for uid in winner_ids:
            uid_str    = str(uid)
            user_stats = load_stats(guild.id).get(uid_str, {})
            streak     = user_stats.get("win_streak", 0)
            fire       = " 🔥" if streak >= 3 else ""
            member     = ctx.guild.get_member(uid)
            if member:
                streak_parts.append(f"{member.display_name} ({streak} streak{fire})")

        announcement = discord.Embed(
            title="🏆 Game Result",
            color=discord.Color.gold()
        )
        announcement.add_field(
            name="Winners",
            value=winner_mentions,
            inline=False
        )
        if streak_parts:
            announcement.add_field(
                name="📈 Current Streaks",
                value="\n".join(streak_parts),
                inline=False
            )
        announcement.set_footer(text=f"Logged by {ctx.author.display_name}")
        await game_links_channel.send(embed=announcement)


# ─── Command: r!create ────────────────────────────────────────────────────────
# Creates a new Discord scheduled event and posts a registration message with ✅ reaction.
# Usage: r!create Title, Description, <t:TIMESTAMP:R>

@bot.command()
@has_allowed_role()
async def create(ctx, *, args):
    parts = [p.strip() for p in args.split(",")]
    if len(parts) < 3:
        await ctx.send("❌ Wrong format! Use: `r!create Title, Description, <t:TIMESTAMP:R>`")
        return

    title       = parts[0]
    description = parts[1]

    try:
        raw       = parts[2].strip("<>").replace("t:", "").split(":")[0]
        timestamp = int(raw)
    except ValueError:
        await ctx.send("❌ Invalid timestamp!")
        return

    guild         = ctx.guild
    event_channel = guild.get_channel(get_cfg(guild.id, "event_channel_id"))

    if event_channel is None:
        await ctx.send("❌ Meeting Point channel not found!")
        return

    start_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)

    try:
        event = await guild.create_scheduled_event(
            name=title,
            description=description,
            start_time=start_time,
            channel=event_channel,
            entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only
        )
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create events! Give me the `Manage Events` permission.")
        return
    except Exception as e:
        await ctx.send(f"❌ Error creating event: `{e}`")
        return

    event_link = f"https://discord.com/events/{guild.id}/{event.id}"
    channel    = bot.get_channel(get_cfg(guild.id, "channel_id"))
    mentions   = " ".join(f"<@&{r}>" for r in get_cfg(guild.id, "mention_roles"))

    embed = discord.Embed(title=title, description=description, url=event_link)
    embed.add_field(name="Date",  value=f"<t:{timestamp}:F>", inline=False)
    embed.add_field(name="Event", value=f"[Click here]({event_link})", inline=False)

    try:
        msg = await channel.send(content=mentions, embed=embed)
        await msg.add_reaction("✅")
    except Exception as e:
        await ctx.send(f"❌ Event created but message could not be posted: `{e}`")
        return

    # Save the event ID → message ID mapping so reactions can be tracked
    data = load_data()
    data[str(event.id)] = msg.id
    save_data(data)

    await ctx.send(f"✅ Event **{title}** successfully created and posted! 🎉\n{event_link}")
    print(f"Created event {event.id} and message {msg.id}")


# ─── Command: r!delete event ─────────────────────────────────────────────────
# Ends the active Discord event, removes Active/Spectator roles, deletes registration
# messages, and clears the scrim chat and game-links channel.
# Also resets the game participant pool for the next scrim session.
# Usage: r!delete event

@bot.command()
@has_allowed_role()
async def delete(ctx, *, args):
    global manually_deleting, scrim_active, current_game_participants

    if args.strip().lower() != "event":
        await ctx.send("❌ Wrong format! Use: `r!delete event`")
        return

    # Set flags before doing anything so the auto-restart guard doesn't fire
    manually_deleting        = True
    scrim_active             = False
    current_game_participants = set()  # Reset participant pool for next session

    await ctx.send("⏳ Deleting event, messages and roles...")

    guild              = ctx.guild
    role               = guild.get_role(get_cfg(guild.id, "role_id"))
    register_channel   = bot.get_channel(get_cfg(guild.id, "channel_id"))
    scrim_channel      = bot.get_channel(get_cfg(guild.id, "scrim_chat_id"))
    game_links_channel = bot.get_channel(get_cfg(guild.id, "game_links_id"))

    # Find the currently active event
    active_event = None
    try:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.active:
                active_event = event
                break
    except Exception as e:
        await ctx.send(f"⚠️ Could not check for active event: `{e}` - continuing cleanup...")

    # Bot leaves the Meeting Point voice channel
    await leave_voice(guild)

    # Remove all Active and Spectator roles first
    await remove_active_role_all(guild)
    await remove_spectator_role_all(guild)

    # Re-sync the registration role (without the just-ended event)
    try:
        data = load_data()
        if active_event:
            event_id_str = str(active_event.id)
            if event_id_str in data:
                del data[event_id_str]
        remaining_message_ids = get_all_message_ids(data)
        reacted_ids           = await get_all_reacted_ids(register_channel, remaining_message_ids)
        await sync_roles(guild, role, reacted_ids)
        print("Roles synced after event deletion")
    except discord.Forbidden:
        manually_deleting = False
        await ctx.send("❌ I don't have permission to remove roles!")
        return
    except Exception as e:
        manually_deleting = False
        await ctx.send(f"❌ Error syncing roles: `{e}`")
        return

    # Delete only the tracked registration messages for this scrim session
    try:
        data = load_data()
        all_tracked_msg_ids = list(get_all_message_ids(data))

        for msg_id in all_tracked_msg_ids:
            try:
                msg = await register_channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"Error deleting tracked message {msg_id}: {e}")

        # Also delete any untracked bot messages from this session
        # (e.g. the 30-min warning and start notification)
        async for message in register_channel.history(limit=100):
            if message.author == bot.user and message.id not in all_tracked_msg_ids:
                # Only delete if it's NOT a registration message for a future event
                remaining_data = {k: v for k, v in data.items() if v not in all_tracked_msg_ids}
                if message.id not in get_all_message_ids(remaining_data):
                    try:
                        await message.delete()
                    except discord.NotFound:
                        pass

        # Remove only the active event from data, keep future events intact
        if active_event:
            event_id_str = str(active_event.id)
            if event_id_str in data:
                del data[event_id_str]
        # Also remove any stale entries whose messages were just deleted
        for msg_id in all_tracked_msg_ids:
            for eid, mid in list(data.items()):
                if mid == msg_id:
                    del data[eid]
                    break
        save_data(data)
        print("Register channel messages deleted")
    except Exception as e:
        manually_deleting = False
        await ctx.send(f"❌ Error deleting register messages: `{e}`")
        return

    # Clear the scrim chat and game-links channels
    try:
        await clear_channel(scrim_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing scrim chat: `{e}`")

    try:
        await clear_channel(game_links_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing game links: `{e}`")

    # End the active Discord event
    if active_event:
        try:
            await active_event.end()
            print("Event ended")
        except Exception as e:
            await ctx.send(f"⚠️ Could not end event: `{e}`")

    # Delete any leftover past events (ended/completed) that are still on the server
    try:
        events = await guild.fetch_scheduled_events()
        past_count = 0
        for event in events:
            if event.status in (discord.EventStatus.ended, discord.EventStatus.completed):
                try:
                    await event.delete()
                    past_count += 1
                    print(f"Deleted past event: {event.name}")
                except Exception as e:
                    print(f"Could not delete past event {event.name}: {e}")
        if past_count:
            print(f"Cleaned up {past_count} past event(s)")
    except Exception as e:
        await ctx.send(f"⚠️ Could not clean up past events: `{e}`")

    manually_deleting = False  # Reset flag – cleanup complete

    await ctx.send("✅ Cleanup complete!\n- 🗑️ Messages deleted\n- 👥 Roles updated\n- 🧹 Scrim chat cleared\n- 🔗 Game links cleared")


# ─── Command: r!cancel event, EVENT_ID ───────────────────────────────────────
# Cancels a scheduled (not yet started) event and removes its registration message.
# Usage: r!cancel event, 1234567890

@bot.command()
@has_allowed_role()
async def cancel(ctx, *, args):
    global scrim_active, current_game_participants

    parts = [p.strip() for p in args.split(",")]
    if len(parts) < 2 or parts[0].lower() != "event":
        await ctx.send("❌ Wrong format! Use: `r!cancel event, EVENT_ID`")
        return

    try:
        event_id = int(parts[1])
    except ValueError:
        await ctx.send("❌ Invalid event ID! It must be a number.")
        return

    await ctx.send("⏳ Cancelling event and deleting messages...")

    guild            = ctx.guild
    role             = guild.get_role(get_cfg(guild.id, "role_id"))
    register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))

    target_event = None
    try:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.id == event_id:
                target_event = event
                break
    except Exception as e:
        await ctx.send(f"❌ Error finding event: `{e}`")
        return

    if target_event is None:
        await ctx.send("❌ Event not found! Make sure the ID is correct.")
        return

    if target_event.status == discord.EventStatus.active:
        await ctx.send("❌ This event is already active! Use `r!delete event` instead.")
        return

    try:
        await target_event.cancel()
        scrim_active = False
        current_game_participants = set()
        print(f"Event {target_event.name} cancelled!")
    except Exception as e:
        await ctx.send(f"❌ Error cancelling event: `{e}`")
        return

    try:
        data         = load_data()
        event_id_str = str(event_id)
        if event_id_str in data:
            msg_id = data[event_id_str]
            try:
                msg = await register_channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
            del data[event_id_str]
            save_data(data)
        else:
            await ctx.send("⚠️ Event cancelled but no linked message was found.")
            return
    except Exception as e:
        await ctx.send(f"❌ Event cancelled but error deleting message: `{e}`")
        return

    try:
        remaining_message_ids = get_all_message_ids(data)
        reacted_ids           = await get_all_reacted_ids(register_channel, remaining_message_ids)
        await sync_roles(guild, role, reacted_ids)
        print("Roles synced after cancellation")
    except Exception as e:
        await ctx.send(f"⚠️ Event cancelled but error syncing roles: `{e}`")
        return

    await ctx.send(f"✅ Event **{target_event.name}** has been cancelled!\n- 🗑️ Message deleted\n- 👥 Roles updated")


# ─── Command: r!event update ─────────────────────────────────────────────────
# Scans all voice channels, assigns Active/Spectator roles, and starts the 1-minute
# auto-check loop so roles stay updated for the rest of the scrim session.
# Also resets current_game_participants so each update starts a fresh game tracking pool.
# Also records attendance stats for registered players.
#
# ─── Command: r!event leaderboard ────────────────────────────────────────────
# Reads winner messages from game-links channel, tallies wins per player, and posts
# an updated leaderboard embed to the leaderboard channel.

@bot.command()
@has_allowed_role()
async def event(ctx, *, args):
    global scrim_active, current_game_participants

    parts      = [p.strip() for p in args.split(" ", 1)]
    subcommand = parts[0].lower()

    if subcommand == "update":
        await ctx.send("⏳ Checking voice channels and assigning roles...")

        guild            = ctx.guild
        active_role      = guild.get_role(get_cfg(guild.id, "active_role_id"))
        spectator_role   = guild.get_role(get_cfg(guild.id, "spectator_role_id"))
        register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))

        if active_role is None:
            await ctx.send("❌ Active Scrim role not found!")
            return
        if spectator_role is None:
            await ctx.send("❌ Spectator Scrim role not found!")
            return

        # Reset participant pool so this update starts a clean tracking window
        current_game_participants = set()

        # Collect which members are where
        members_in_meeting_point = set()
        members_in_other_vc      = set()
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                if vc.id == get_cfg(guild.id, "event_channel_id"):
                    members_in_meeting_point.add(member.id)
                else:
                    members_in_other_vc.add(member.id)

        # Record attendance for registered players
        data        = load_data()
        message_ids = get_all_message_ids(data)
        reacted_ids = await get_all_reacted_ids(register_channel, message_ids)
        all_in_vc   = members_in_meeting_point | members_in_other_vc

        stats = load_stats(guild.id)
        for user_id in reacted_ids:
            uid_str    = str(user_id)
            user_stats = get_or_create_stats(stats, uid_str)
            user_stats["registered"] += 1
            if user_id in all_in_vc:
                user_stats["attended"] += 1
        save_stats(stats, guild.id)

        # Assign Active / Spectator roles based on current VC positions
        # (also populates current_game_participants with players in game VCs)
        await update_scrim_vc_roles(guild)

        # Bot joins Meeting Point so Discord never auto-ends the event due to empty VC
        await join_meeting_point(guild)

        # Activate the per-minute auto-check for the rest of the scrim
        scrim_active = True

        # Build a readable summary for the confirmation message
        scrim_role = guild.get_role(get_cfg(guild.id, "role_id"))

        active_names       = []
        not_registered     = []

        for uid in members_in_other_vc:
            member = guild.get_member(uid)
            if not member or member.bot:
                continue
            if scrim_role and scrim_role in member.roles:
                active_names.append(member.display_name)
            else:
                not_registered.append(member.display_name)

        spectator_names = [
            guild.get_member(uid).display_name
            for uid in members_in_meeting_point
            if guild.get_member(uid) and not guild.get_member(uid).bot
        ]

        lines = ["✅ Update complete! Auto-check every minute is now **active**."]

        if active_names:
            lines.append(f"🎮 **Active Scrim** ({len(active_names)}): {', '.join(active_names)}")
        else:
            lines.append("🎮 **Active Scrim**: nobody in game VCs")

        if spectator_names:
            lines.append(f"👁️ **Spectator** ({len(spectator_names)}): {', '.join(spectator_names)}")
        else:
            lines.append("👁️ **Spectator**: nobody in Meeting Point")

        if not_registered:
            lines.append(f"⚠️ **Not Registered** ({len(not_registered)}): {', '.join(not_registered)} — in game VC but haven't signed up!")

        await ctx.send("\n".join(lines))

    elif subcommand == "leaderboard":
        await ctx.send("⏳ Building leaderboard...")

        guild               = ctx.guild
        game_links_channel  = bot.get_channel(get_cfg(guild.id, "game_links_id"))
        leaderboard_channel = bot.get_channel(get_cfg(guild.id, "leaderboard_channel_id"))
        stats               = load_stats(guild.id)
        leaderboard         = load_leaderboard(guild.id)  # Source of truth – updated by log_game in real time

        # Also scan game-links for any uncounted messages (fallback for manually posted wins)
        counted_file = guild_file(guild.id, "counted_messages.json")
        if os.path.exists(counted_file):
            with open(counted_file, "r") as f:
                counted_ids = set(json.load(f))
        else:
            counted_ids = set()

        new_found = 0
        async for message in game_links_channel.history(limit=500):
            if message.id in counted_ids or message.author == bot.user:
                continue
            content_lower = message.content.lower()
            if "winner" in content_lower and message.mentions:
                for member in message.mentions:
                    if not member.bot:
                        uid = str(member.id)
                        leaderboard[uid] = leaderboard.get(uid, 0) + 1
                counted_ids.add(message.id)
                new_found += 1

        if not leaderboard:
            await ctx.send(
                "⚠️ No wins recorded yet!\n"
                "Use `r!winner @Player` to manually add wins."
            )
            return

        save_leaderboard(leaderboard, guild.id)
        with open(counted_file, "w") as f:
            json.dump(list(counted_ids), f)

        # Sync stats.json games_won to match leaderboard
        for uid, wins in leaderboard.items():
            user_stats = get_or_create_stats(stats, uid)
            user_stats["games_won"] = wins
            if user_stats.get("games_played", 0) < wins:
                user_stats["games_played"] = wins
        save_stats(stats, guild.id)

        def build_lb_description(entries, guild):
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines  = []
            for i, (user_id, wins, games) in enumerate(entries):
                pos    = i + 1
                member = guild.get_member(int(user_id))
                name   = member.display_name if member else f"<@{user_id}>"
                prefix = medals.get(pos, f"**#{pos}**")
                lines.append(f"{prefix} **{name}** — {wins} pt{'s' if wins != 1 else ''}")
            return "\n".join(lines) if lines else "No data yet."

        # ── Quarter embed ──────────────────────────────────────────────────────
        quarter_entries = []
        for uid, wins in leaderboard.items():
            user_stats  = get_or_create_stats(stats, uid)
            games       = max(user_stats.get("games_played", 0), wins)  # at minimum played as many as won
            quarter_entries.append((uid, wins, games))
        quarter_entries.sort(key=lambda x: x[1], reverse=True)
        quarter_entries = quarter_entries[:25]

        quarter_embed = discord.Embed(
            title="📊 Current Quarter — Leaderboard",
            description=build_lb_description(quarter_entries, guild),
            color=discord.Color.gold()
        )
        quarter_embed.set_footer(text="1 Win = 1 Point  |  Use r!stats @Player for detailed stats")

        # ── All-time embed ─────────────────────────────────────────────────────
        season = load_season(guild.id)
        alltime_entries = []
        for uid, sdata in season.items():
            if isinstance(sdata, dict):
                alltime_entries.append((uid, sdata.get("wins", 0), sdata.get("games_played", 0)))
        alltime_entries.sort(key=lambda x: x[1], reverse=True)
        alltime_entries = alltime_entries[:25]

        if alltime_entries:
            alltime_desc = build_lb_description(alltime_entries, guild)
        else:
            alltime_desc = "No all-time data yet. Use `r!season reset` at the end of a quarter to save results."

        alltime_embed = discord.Embed(
            title="🏆 All-Time — Overall Leaderboard",
            description=alltime_desc,
            color=discord.Color.blurple()
        )
        alltime_embed.set_footer(text="Accumulated across all quarters")

        # Delete old leaderboard embeds and post fresh ones
        try:
            async for old_msg in leaderboard_channel.history(limit=20):
                if old_msg.author == bot.user:
                    await old_msg.delete()
        except Exception as e:
            await ctx.send(f"⚠️ Could not delete old leaderboard: `{e}`")

        mention_roles   = get_cfg(guild.id, "mention_roles")
        scrim_news_role = guild.get_role(mention_roles[-1]) if mention_roles else None
        mention_content = scrim_news_role.mention if scrim_news_role else ""

        await leaderboard_channel.send(content=mention_content, embed=quarter_embed)
        if alltime_entries:
            await leaderboard_channel.send(embed=alltime_embed)
        await ctx.send(f"✅ Leaderboard updated! Found **{new_found}** new game(s) with winners.")

    else:
        await ctx.send("❌ Unknown subcommand! Available: `r!event update`, `r!event leaderboard`")


# ─── Command: r!join ─────────────────────────────────────────────────────────
# Makes the bot manually join the Meeting Point voice channel.
# Use this if the bot failed to join automatically after r!event update,
# or if it got disconnected during a scrim session.
# Usage: r!join

@bot.command()
@has_allowed_role()
async def join(ctx):
    guild   = ctx.guild
    channel = guild.get_channel(get_cfg(guild.id, "event_channel_id"))

    if channel is None:
        await ctx.send("❌ Meeting Point channel not found!")
        return

    perms = channel.permissions_for(guild.me)
    if not perms.connect:
        await ctx.send("❌ I don't have **Connect** permission in the Meeting Point channel!")
        return
    if not perms.view_channel:
        await ctx.send("❌ I don't have **View Channel** permission in the Meeting Point channel!")
        return

    vc = guild.voice_client
    if vc and vc.is_connected():
        if vc.channel.id == get_cfg(guild.id, "event_channel_id"):
            await ctx.send(f"✅ Already in 🔊 | **{channel.name}**!")
            return
        # Move to Meeting Point if in a different channel
        try:
            await vc.move_to(channel)
            await ctx.send(f"✅ Bot moved to 🔊 | **{channel.name}**! The event will stay active.")
        except Exception as e:
            await ctx.send(f"❌ Failed to move: `{e}`")
        return

    # Not connected at all – join fresh
    try:
        await channel.connect(self_deaf=True, self_mute=True)
        await ctx.send(f"✅ Bot joined 🔊 | **{channel.name}**! The event will stay active.")
    except discord.Forbidden:
        await ctx.send("❌ Missing **Connect** permission in the Meeting Point channel!")
    except Exception as e:
        await ctx.send(f"❌ Failed to join: `{e}`")


# ─── Command: r!leave ────────────────────────────────────────────────────────
# Manually disconnects the bot from the voice channel it is currently in.
# Use this if the bot is stuck in a channel or needs to be moved manually.
# Usage: r!leave

@bot.command()
@has_allowed_role()
async def leave(ctx):
    guild = ctx.guild
    vc    = guild.voice_client

    if vc and vc.is_connected():
        channel_name = vc.channel.name
        await leave_voice(guild)
        await ctx.send(f"✅ Bot left 🔊 | **{channel_name}**.")
    else:
        await ctx.send("⚠️ Bot is not in any voice channel.")


# ─── Command: r!stopupdate ───────────────────────────────────────────────────
# Manually stops the automatic VC role check loop without ending the event.
# Use this if you want to pause role tracking mid-scrim without a full cleanup.
# Usage: r!stopupdate

@bot.command()
@has_allowed_role()
async def stopupdate(ctx):
    global scrim_active, current_game_participants

    if not scrim_active:
        await ctx.send("⚠️ Auto-check is not currently active.")
        return

    scrim_active              = False
    current_game_participants = set()

    # Remove Active Scrim and Spectator roles from everyone
    guild = ctx.guild
    await remove_active_role_all(guild)
    await remove_spectator_role_all(guild)

    await ctx.send("✅ Auto VC check stopped. Active Scrim & Spectator roles removed. Participant tracking reset.\nThe event is still active – use `r!delete event` to fully end the scrim.")


# ─── Command: r!removewins @Player [amount] ──────────────────────────────────
# Removes wins from a player. Defaults to 1 win if no amount is specified.
# Also adjusts games_won in stats and the leaderboard.
# Usage:
#   r!removewins @Player       → removes 1 win
#   r!removewins @Player 3     → removes 3 wins
#   r!removewins 123456789     → also works with User ID

@bot.command()
@has_allowed_role()
async def removewins(ctx, *, args=None):
    if args is None:
        await ctx.send(
            "❌ Please provide a user!\n"
            "Usage: `r!removewins @Player` or `r!removewins @Player 3`"
        )
        return

    # Parse mentions and/or raw IDs
    tokens    = args.split()
    member    = None
    amount    = 1

    # Check if last token is a number (the amount to remove)
    if tokens and tokens[-1].isdigit():
        amount = int(tokens[-1])
        tokens = tokens[:-1]

    # Get member from mention or ID
    if ctx.message.mentions:
        member = ctx.message.mentions[0]
    else:
        for token in tokens:
            token = token.strip("<@!>")
            if token.isdigit():
                try:
                    member = ctx.guild.get_member(int(token)) or await ctx.guild.fetch_member(int(token))
                except discord.NotFound:
                    await ctx.send(f"❌ No user found with ID `{token}`.")
                    return
                break

    if member is None:
        await ctx.send("❌ Could not find the user. Use a mention or User ID.")
        return

    if member.bot:
        await ctx.send("❌ Can't remove wins from a bot.")
        return

    if amount < 1:
        await ctx.send("❌ Amount must be at least 1.")
        return

    guild       = ctx.guild
    stats       = load_stats(guild.id)
    leaderboard = load_leaderboard(guild.id)
    uid_str     = str(member.id)
    user_stats  = get_or_create_stats(stats, uid_str)

    # Clamp so we don't go below 0
    old_wins           = user_stats["games_won"]
    removed            = min(amount, old_wins)
    user_stats["games_won"] = old_wins - removed

    old_lb             = leaderboard.get(uid_str, 0)
    leaderboard[uid_str] = max(0, old_lb - removed)

    # Reset win streak if it's now higher than remaining wins (sanity clamp)
    if user_stats["win_streak"] > user_stats["games_won"]:
        user_stats["win_streak"] = user_stats["games_won"]

    save_stats(stats, guild.id)
    save_leaderboard(leaderboard, guild.id)

    embed = discord.Embed(title="➖ Wins Removed", color=discord.Color.red())
    embed.add_field(name="Player",       value=member.mention,                   inline=True)
    embed.add_field(name="Removed",      value=f"-{removed}",                    inline=True)
    embed.add_field(name="Wins Left",    value=str(user_stats["games_won"]),     inline=True)
    embed.add_field(name="Leaderboard",  value=str(leaderboard[uid_str]),        inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Adjusted by {ctx.author.display_name}")
    await ctx.send(embed=embed)
    print(f"[r!removewins] Removed {removed} win(s) from {member.display_name} by {ctx.author.display_name}")



# Manually adds a win to a player by their Discord User ID.
# Useful when r!event leaderboard finds no winner messages in game-links.
# Also updates games_won, win_streak and best_streak in stats.json.
# Usage: r!winner 123456789012345678

@bot.command()
@has_allowed_role()
async def winner(ctx, *, args=None):
    if args is None:
        await ctx.send(
            "❌ Please provide at least one user!\n"
            "Usage: `r!winner @Player1 @Player2` or `r!winner 123456789 987654321`"
        )
        return

    # Collect IDs from mentions first, then parse any raw IDs from the text
    member_ids = {m.id for m in ctx.message.mentions if not m.bot}

    # Also parse any plain number IDs in the args (not already covered by mentions)
    for token in args.split():
        token = token.strip("<@!>")
        if token.isdigit():
            member_ids.add(int(token))

    if not member_ids:
        await ctx.send("❌ No valid users found! Use mentions or User IDs.")
        return

    guild       = ctx.guild
    stats       = load_stats(guild.id)
    leaderboard = load_leaderboard(guild.id)
    results     = []

    for user_id_int in member_ids:
        member = guild.get_member(user_id_int)
        if member is None:
            try:
                member = await guild.fetch_member(user_id_int)
            except discord.NotFound:
                await ctx.send(f"⚠️ No user found with ID `{user_id_int}` – skipping.")
                continue
            except Exception as e:
                await ctx.send(f"⚠️ Error fetching `{user_id_int}`: `{e}` – skipping.")
                continue

        if member.bot:
            continue

        uid_str    = str(user_id_int)
        user_stats = get_or_create_stats(stats, uid_str)
        user_stats["games_won"]  += 1
        user_stats["win_streak"] += 1
        if user_stats["win_streak"] > user_stats["best_streak"]:
            user_stats["best_streak"] = user_stats["win_streak"]
        leaderboard[uid_str] = leaderboard.get(uid_str, 0) + 1

        streak = user_stats["win_streak"]
        fire   = " 🔥" if streak >= 3 else ""
        results.append(f"{member.mention} — {user_stats['games_won']} wins | streak: {streak}{fire}")

    save_stats(stats, guild.id)
    save_leaderboard(leaderboard, guild.id)

    if not results:
        await ctx.send("❌ No valid users were updated.")
        return

    embed = discord.Embed(title="🏆 Win(s) Added!", color=discord.Color.gold())
    embed.add_field(name="Updated Players", value="\n".join(results), inline=False)
    embed.set_footer(text=f"Added manually by {ctx.author.display_name}")
    await ctx.send(embed=embed)
    print(f"[r!winner] Wins added to {len(results)} player(s) by {ctx.author.display_name}")



# ─── Command: r!info ─────────────────────────────────────────────────────────
# Describes the bot and its purpose.
# Usage: r!info

@bot.command()
async def info(ctx):
    embed = discord.Embed(
        title="🤖 SCRIM Bot — Info",
        description=(
            "The SCRIM Bot manages competitive scrim sessions on this server.\n\n"
            "**Regular Scrims:**\n"
            "- Creates Discord scheduled events with ✅ reaction sign-up\n"
            "- Assigns **Active Scrim** and **Spectator** roles based on voice channels\n"
            "- Tracks game results, win streaks, winrates and attendance\n"
            "- Maintains a quarterly leaderboard with all-time stats\n"
            "- Sends a 30-minute warning before events start\n"
            "- Stays in the Meeting Point VC to keep events alive\n\n"
            "**Tournament Scrims:**\n"
            "- Supports team formats (`3v3`, `3v3v3v3`), solo formats (`1v1`, `1v1v1`), FFA (`ffa8`, `ffa16`) and multiple groups\n"
            "- Players register via button → private ticket channel → guided step-by-step form\n"
            "- Ticket asks for: Captain, Starters, Substitutes (optional), Coaches (optional, max 3)\n"
            "- Staff accepts/rejects teams via buttons — team gets notified in ticket\n"
            "- Coaches receive Spectator role + access to game-links & scrim-chat\n"
            "- Creates private voice channels per team at start (team + subs + coaches)\n"
            "- Solo/FFA formats skip voice channels, substitutes and coaches\n"
            "- Registered teams are displayed in a dedicated channel with live updates\n\n"
            "**Multi-Server:** Each server has its own config, stats and leaderboard.\n"
            "**New Server Setup:** Use `/setup` or `r!setup` *(Admin only)*\n"
            "**All Commands:** Use `/cmd` or `r!cmd`\n"
            "**Permissions:** Most commands require **Staff** or **Host** role"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="SCRIM Bot • Competitive scrim management")
    await ctx.send(embed=embed)


# ─── Command: r!cmd ──────────────────────────────────────────────────────────

@bot.command()
async def cmd(ctx):
    embed = discord.Embed(title="🤖 SCRIM Bot — Commands", color=discord.Color.blurple())

    embed.add_field(name="⚙️ Setup *(Admin only)*", value=(
        "`r!setup` — Step-by-step server configuration wizard\n"
        "`r!setupshow` — Show current server configuration\n"
        "`r!setupreset` — Reset server configuration"
    ), inline=False)

    embed.add_field(name="📅 Regular Scrim", value=(
        "`r!create Title, Desc, <t:TS:R>` — Create event & registration post\n"
        "`r!cancel event, ID` — Cancel a scheduled event\n"
        "`r!delete event` — End scrim & full cleanup\n"
        "`r!join` / `r!leave` — Bot joins/leaves Meeting Point VC"
    ), inline=False)

    embed.add_field(name="🎟️ Tournament Scrim", value=(
        "`r!tournament create FORMAT, GROUPS, SUBS, Title, Desc, <t:TS:R>`\n"
        "Formats: `3v3`, `3v3v3v3`, `1v1`, `ffa8`, `ffa16` …\n"
        "`r!tournament list` — Show all registered teams\n"
        "`r!tournament close` — Close registrations\n"
        "`r!tournament start` — Assign roles & create team VCs\n"
        "`r!tournament delete` — End tournament & full cleanup"
    ), inline=False)

    embed.add_field(name="🎮 Scrim Session", value=(
        "`r!event update` — Assign Active/Spectator roles\n"
        "`r!event leaderboard` — Post updated leaderboard\n"
        "`r!stopupdate` — Stop auto-check & remove roles"
    ), inline=False)

    embed.add_field(name="🏆 Game Tracking", value=(
        "`r!game winner @A @B` — Log a game result\n"
        "`r!winner @A` — Add wins manually\n"
        "`r!removewins @Player [amount]` — Remove wins"
    ), inline=False)

    embed.add_field(name="📊 Stats & Season", value=(
        "`r!stats` / `r!stats @Player` / `r!stats top`\n"
        "`r!season reset Q2 2026` — Save quarter to all-time & reset\n"
        "`r!season show` — All-time leaderboard"
    ), inline=False)

    embed.add_field(name="🔄 Regular Workflow", value=(
        "`r!create` → `r!join` → `r!event update` → log games → `r!event leaderboard` → `r!delete event`"
    ), inline=False)

    embed.add_field(name="🎟️ Tournament Workflow", value=(
        "`r!tournament create` → players register via button → staff accepts → `r!tournament start` → `r!tournament delete`"
    ), inline=False)

    embed.set_footer(text="Staff & Host only • except r!stats, r!info, r!cmd, r!setup")
    await ctx.send(embed=embed)


# ─── Command: r!season ───────────────────────────────────────────────────────
# Manages quarterly resets and the all-time leaderboard.
# Usage:
#   r!season reset Q2 2026   → saves current stats to all-time, then resets
#   r!season show            → shows the all-time leaderboard in chat

@bot.command()
@has_allowed_role()
async def season(ctx, subcommand: str = None, *, args=None):
    guild = ctx.guild

    if subcommand is None:
        await ctx.send("❌ Usage: `r!season reset Q2 2026` or `r!season show`")
        return

    if subcommand.lower() == "show":
        season_data = load_season(guild.id)
        if not season_data:
            await ctx.send("⚠️ No all-time data yet. Run `r!season reset` at the end of a quarter to save results.")
            return

        medals  = ["🥇", "🥈", "🥉"]
        entries = []
        for uid, sdata in season_data.items():
            if isinstance(sdata, dict):
                entries.append((uid, sdata.get("wins", 0), sdata.get("games_played", 0)))
        entries.sort(key=lambda x: x[1], reverse=True)

        lines = []
        for i, (uid, wins, games) in enumerate(entries[:15]):
            member  = guild.get_member(int(uid))
            name    = member.display_name if member else f"<@{uid}>"
            winrate = f"{(wins/games*100):.0f}%" if games > 0 else "0%"
            prefix  = medals[i] if i < 3 else ("🏅" if wins >= 3 else "▪️")
            lines.append(f"{prefix} **{name}** — {wins} pts · {winrate} WR · {games} games")

        embed = discord.Embed(
            title="🏆 All-Time Overall Leaderboard",
            description="\n".join(lines),
            color=discord.Color.blurple()
        )
        embed.set_footer(text="Accumulated across all quarters")
        await ctx.send(embed=embed)
        return

    if subcommand.lower() == "reset":
        quarter_name = args.strip() if args else "Unknown Quarter"

        await ctx.send(f"⏳ Saving current quarter **{quarter_name}** to all-time stats and resetting...")

        stats       = load_stats(guild.id)
        leaderboard = load_leaderboard(guild.id)
        season_data = load_season(guild.id)

        # Merge current quarter into all-time
        for uid, wins in leaderboard.items():
            user_stats  = get_or_create_stats(stats, uid)
            games       = user_stats.get("games_played", 0)
            season_entry = get_or_create_season(season_data, uid)
            season_entry["wins"]         += wins
            season_entry["games_played"] += games
            season_entry["quarters"].append({
                "name":         quarter_name,
                "wins":         wins,
                "games_played": games,
                "winrate":      f"{(wins/games*100):.1f}%" if games > 0 else "0%"
            })

        save_season(season_data, guild.id)

        # Reset current quarter stats
        for uid in stats:
            if isinstance(stats[uid], dict):
                stats[uid]["games_played"] = 0
                stats[uid]["games_won"]    = 0
                stats[uid]["win_streak"]   = 0
                stats[uid]["registered"]   = 0
                stats[uid]["attended"]     = 0
                # best_streak is kept intentionally

        save_stats(stats, guild.id)

        # Reset leaderboard and counted messages for new quarter
        save_leaderboard({}, guild.id)
        counted_file = guild_file(guild.id, "counted_messages.json")
        if os.path.exists(counted_file):
            os.remove(counted_file)

        embed = discord.Embed(
            title=f"✅ Quarter Reset — {quarter_name}",
            description=(
                f"All stats from **{quarter_name}** have been saved to the all-time leaderboard.\n"
                f"Current quarter stats and points have been reset to 0.\n\n"
                f"Use `r!event leaderboard` after the next scrim to see the new standings.\n"
                f"Use `r!season show` to view the all-time leaderboard."
            ),
            color=discord.Color.green()
        )
        embed.set_footer(text=f"Reset by {ctx.author.display_name}")
        await ctx.send(embed=embed)

    else:
        await ctx.send("❌ Unknown subcommand! Use `r!season reset Q2 2026` or `r!season show`")


# ─── Command: r!stats ────────────────────────────────────────────────────────
# Shows full stats for a player including game tracking fields.
# Usage:
#   r!stats           → own stats
#   r!stats @player   → stats for another player
#   r!stats top       → top 10 by attendance rate

@bot.command()
async def stats(ctx, *, args=None):
    guild       = ctx.guild
    stats       = load_stats(guild.id)
    leaderboard = load_leaderboard(guild.id)

    if args and args.strip().lower() == "top":
        if not stats:
            await ctx.send("❌ No stats available yet!")
            return

        sorted_stats = []
        for uid, s in stats.items():
            rate = (s["attended"] / s["registered"] * 100) if s.get("registered", 0) > 0 else 0
            sorted_stats.append((uid, s, rate))
        sorted_stats.sort(key=lambda x: x[2], reverse=True)

        description = ""
        for i, (uid, s, rate) in enumerate(sorted_stats[:10]):
            member       = guild.get_member(int(uid))
            name         = member.display_name if member else f"<@{uid}>"
            points       = leaderboard.get(uid, 0)
            games_played = s.get("games_played", 0)
            games_won    = s.get("games_won", 0)
            winrate      = (games_won / games_played * 100) if games_played > 0 else 0
            description += (
                f"**{i+1}.** {name} — "
                f"{rate:.0f}% attendance ({s.get('attended',0)}/{s.get('registered',0)}) | "
                f"{winrate:.0f}% WR ({games_won}W/{games_played - games_won}L) | "
                f"{points} pts\n"
            )

        embed = discord.Embed(
            title="🏅 Top 10 - Attendance Rate",
            description=description,
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        return

    # Single player stats (mentioned user or self)
    if ctx.message.mentions:
        target = ctx.message.mentions[0]
    else:
        target = ctx.author

    uid_str    = str(target.id)
    user_stats = stats.get(uid_str, {})
    points     = leaderboard.get(uid_str, 0)

    registered   = user_stats.get("registered", 0)
    attended     = user_stats.get("attended", 0)
    games_played = user_stats.get("games_played", 0)
    games_won    = user_stats.get("games_won", 0)
    games_lost   = games_played - games_won
    win_streak   = user_stats.get("win_streak", 0)
    best_streak  = user_stats.get("best_streak", 0)

    attend_rate = (attended / registered * 100) if registered > 0 else 0
    winrate     = (games_won / games_played * 100) if games_played > 0 else 0

    if attend_rate >= 80:
        attend_emoji = "🟢"
    elif attend_rate >= 50:
        attend_emoji = "🟡"
    else:
        attend_emoji = "🔴"

    if winrate >= 60:
        wr_emoji = "🟢"
    elif winrate >= 40:
        wr_emoji = "🟡"
    else:
        wr_emoji = "🔴"

    streak_display = f"{win_streak} 🔥" if win_streak >= 3 else str(win_streak)

    embed = discord.Embed(title=f"📊 Stats - {target.display_name}", color=discord.Color.blue())
    embed.add_field(name="🏆 Points",                       value=str(points),            inline=True)
    embed.add_field(name="📋 Registered",                   value=str(registered),        inline=True)
    embed.add_field(name=f"{attend_emoji} Attendance Rate", value=f"{attend_rate:.0f}%",  inline=True)
    embed.add_field(name="🎮 Games Played",                 value=str(games_played),      inline=True)
    embed.add_field(name="✅ Games Won",                    value=str(games_won),         inline=True)
    embed.add_field(name="❌ Games Lost",                   value=str(games_lost),        inline=True)
    embed.add_field(name=f"{wr_emoji} Winrate",             value=f"{winrate:.0f}%",      inline=True)
    embed.add_field(name="🔥 Current Streak",               value=streak_display,         inline=True)
    embed.add_field(name="⭐ Best Streak",                  value=str(best_streak),       inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)

    await ctx.send(embed=embed)


# ─── Reaction Events ──────────────────────────────────────────────────────────
# These events fire whenever someone adds or removes a ✅ on a tracked registration message.
# They keep the registration role (ROLE_ID) in sync in real time.

@bot.event
async def on_raw_reaction_add(payload):
    """Give the registration role when a user reacts ✅ to a tracked message."""
    data        = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return
    if str(payload.emoji) != "✅":
        return
    guild  = bot.get_guild(payload.guild_id)
    role   = guild.get_role(get_cfg(guild.id, "role_id"))
    member = guild.get_member(payload.user_id)
    if member and not member.bot:
        await member.add_roles(role)


@bot.event
async def on_raw_reaction_remove(payload):
    """Remove the registration role when a user un-reacts ✅, unless they reacted on another tracked message."""
    data        = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return
    if str(payload.emoji) != "✅":
        return
    guild   = bot.get_guild(payload.guild_id)
    role    = guild.get_role(get_cfg(guild.id, "role_id"))
    channel = bot.get_channel(get_cfg(guild.id, "channel_id"))
    member  = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return

    # Check if the user still has ✅ on any other tracked message before removing the role
    still_reacted = False
    for msg_id in message_ids:
        if msg_id == payload.message_id:
            continue
        try:
            msg = await channel.fetch_message(msg_id)
            for r in msg.reactions:
                if str(r.emoji) == "✅":
                    async for user in r.users():
                        if user.id == member.id:
                            still_reacted = True
                            break
                if still_reacted:
                    break
        except discord.NotFound:
            pass
        if still_reacted:
            break

    if not still_reacted:
        await member.remove_roles(role)


# ─── Message Delete Event ─────────────────────────────────────────────────────
# If a tracked registration message is deleted externally (not by the bot),
# this removes it from storage and re-syncs all roles.

@bot.event
async def on_raw_message_delete(payload):
    data        = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return

    print(f"Tracked message {payload.message_id} was deleted, resyncing roles...")

    # Remove the deleted message from storage
    for event_id, msg_id in list(data.items()):
        if msg_id == payload.message_id:
            del data[event_id]
            break
    save_data(data)

    guild                 = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    channel               = bot.get_channel(get_cfg(guild.id, "channel_id"))
    if channel is None:
        return
    role                  = guild.get_role(get_cfg(guild.id, "role_id"))
    remaining_message_ids = get_all_message_ids(data)
    reacted_ids           = await get_all_reacted_ids(channel, remaining_message_ids)
    await sync_roles(guild, role, reacted_ids)
    print(f"Roles resynced, now tracking {len(remaining_message_ids)} message(s)")


# ═══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS  (/ prefix)
# All commands here mirror the r! prefix commands above.
# Both versions call the same internal logic.
# ═══════════════════════════════════════════════════════════════════════════════

def slash_has_role():
    """app_commands check: user must have Staff or Host role, or be a server administrator."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        allowed = get_cfg(interaction.guild_id, "allowed_roles")
        user_role_ids = [r.id for r in interaction.user.roles]
        if any(r in user_role_ids for r in allowed):
            return True
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return False
    return app_commands.check(predicate)


# ── Setup ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="setup", description="Start the interactive setup wizard for this server (Admin only)")
async def slash_setup(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await setup(ctx)

@bot.tree.command(name="setupshow", description="Show the current bot configuration for this server")
async def slash_setupshow(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await setup_show(ctx)

@bot.tree.command(name="setupreset", description="Clear the server configuration so setup can be run again")
async def slash_setupreset(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await setup_reset(ctx)


# ── Event Management ───────────────────────────────────────────────────────────

@bot.tree.command(name="create", description="Create a scrim event and post the registration message")
@slash_has_role()
@app_commands.describe(
    title="Event title (e.g. Friday Scrim)",
    description="Short description (e.g. Competitive 5v5)",
    timestamp="Discord timestamp — generate at discordtimestamp.com (e.g. <t:1700000000:R>)"
)
async def slash_create(interaction: discord.Interaction, title: str, description: str, timestamp: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await create(ctx, args=f"{title}, {description}, {timestamp}")

@bot.tree.command(name="delete_event", description="End the scrim and run full cleanup")
@slash_has_role()
async def slash_delete(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await delete(ctx, args="event")

@bot.tree.command(name="cancel_event", description="Cancel a scheduled event that hasn't started yet")
@slash_has_role()
@app_commands.describe(event_id="The Discord Event ID (right-click event → Copy Event Link → last number)")
async def slash_cancel(interaction: discord.Interaction, event_id: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await cancel(ctx, args=f"event, {event_id}")


# ── Voice ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="join", description="Bot joins the Meeting Point voice channel")
@slash_has_role()
async def slash_join(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await join(ctx)

@bot.tree.command(name="leave", description="Bot leaves the voice channel it's currently in")
@slash_has_role()
async def slash_leave(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await leave(ctx)


# ── Scrim Session ──────────────────────────────────────────────────────────────

@bot.tree.command(name="event_update", description="Assign Active/Spectator roles and start auto VC check")
@slash_has_role()
async def slash_event_update(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await event(ctx, args="update")

@bot.tree.command(name="event_leaderboard", description="Scan game-links and post the updated leaderboard")
@slash_has_role()
async def slash_event_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await event(ctx, args="leaderboard")

@bot.tree.command(name="stopupdate", description="Stop auto VC check and remove Active/Spectator roles")
@slash_has_role()
async def slash_stopupdate(interaction: discord.Interaction):
    global scrim_active, current_game_participants

    if not scrim_active:
        await interaction.response.send_message("⚠️ Auto-check is not currently active.", ephemeral=True)
        return

    await interaction.response.defer()
    guild = interaction.guild
    scrim_active              = False
    current_game_participants = set()
    await remove_active_role_all(guild)
    await remove_spectator_role_all(guild)
    await interaction.followup.send("✅ Auto VC check stopped. Active Scrim & Spectator roles removed. Participant tracking reset.\nThe event is still active – use `/delete_event` to fully end the scrim.")


# ── Game Tracking ──────────────────────────────────────────────────────────────

@bot.tree.command(name="game_winner", description="Log a game result — mentioned players are winners")
@slash_has_role()
@app_commands.describe(players="Mention all winners (e.g. @PlayerA @PlayerB)")
async def slash_game_winner(interaction: discord.Interaction, players: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await game(ctx, subcommand="winner", args=players)

@bot.tree.command(name="winner", description="Manually add a win to one or more players")
@slash_has_role()
@app_commands.describe(players="Mention players or paste User IDs separated by spaces")
async def slash_winner(interaction: discord.Interaction, players: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await winner(ctx, args=players)

@bot.tree.command(name="removewins", description="Remove wins from a player")
@slash_has_role()
@app_commands.describe(
    player="Mention the player or paste their User ID",
    amount="Number of wins to remove (default: 1)"
)
async def slash_removewins(interaction: discord.Interaction, player: str, amount: int = 1):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await removewins(ctx, args=f"{player} {amount}")


# ── Stats ──────────────────────────────────────────────────────────────────────

@bot.tree.command(name="stats", description="Show player stats")
@app_commands.describe(player="Mention a player to see their stats, or leave empty for your own")
async def slash_stats(interaction: discord.Interaction, player: str = None):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await stats(ctx, args=player)

@bot.tree.command(name="stats_top", description="Show the top 10 players by attendance rate")
async def slash_stats_top(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await stats(ctx, args="top")


# ── Season ─────────────────────────────────────────────────────────────────────

@bot.tree.command(name="season_reset", description="Save current quarter to all-time stats and reset for new quarter")
@slash_has_role()
@app_commands.describe(quarter="Quarter name (e.g. Q2 2026)")
async def slash_season_reset(interaction: discord.Interaction, quarter: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await season(ctx, subcommand="reset", args=quarter)

@bot.tree.command(name="season_show", description="Show the all-time overall leaderboard")
async def slash_season_show(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await season(ctx, subcommand="show")


# ── General ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="info", description="What the SCRIM Bot does and how it works")
async def slash_info(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await info(ctx)

@bot.tree.command(name="cmd", description="Show all available commands")
async def slash_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 SCRIM Bot — Commands", color=discord.Color.blurple())

    embed.add_field(name="⚙️ Setup *(Admin only)*", value=(
        "`/setup` — Step-by-step server configuration wizard\n"
        "`/setupshow` — Show current server configuration\n"
        "`/setupreset` — Reset server configuration"
    ), inline=False)

    embed.add_field(name="📅 Regular Scrim", value=(
        "`/create` — Create event & registration post\n"
        "`/cancel_event` — Cancel a scheduled event\n"
        "`/delete_event` — End scrim & full cleanup\n"
        "`/join` / `/leave` — Bot joins/leaves Meeting Point VC"
    ), inline=False)

    embed.add_field(name="🎟️ Tournament Scrim", value=(
        "`/tournament_create` — Create tournament (team, solo, FFA, multi-group)\n"
        "Formats: `3v3`, `3v3v3v3`, `1v1`, `ffa8`, `ffa16` …\n"
        "`/tournament_list` — Show all registered teams\n"
        "`/tournament_close` — Close registrations\n"
        "`/tournament_start` — Assign roles & create team VCs\n"
        "`/tournament_delete` — End tournament & full cleanup"
    ), inline=False)

    embed.add_field(name="🎮 Scrim Session", value=(
        "`/event_update` — Assign Active/Spectator roles\n"
        "`/event_leaderboard` — Post updated leaderboard\n"
        "`/stopupdate` — Stop auto-check & remove roles"
    ), inline=False)

    embed.add_field(name="🏆 Game Tracking", value=(
        "`/game_winner` — Log a game result\n"
        "`/winner` — Add wins manually\n"
        "`/removewins` — Remove wins from a player"
    ), inline=False)

    embed.add_field(name="📊 Stats & Season", value=(
        "`/stats` / `/stats_top` — Player stats\n"
        "`/season_reset` — Save quarter to all-time & reset\n"
        "`/season_show` — All-time leaderboard"
    ), inline=False)

    embed.add_field(name="🔄 Regular Workflow", value=(
        "`/create` → `/join` → `/event_update` → log games → `/event_leaderboard` → `/delete_event`"
    ), inline=False)

    embed.add_field(name="🎟️ Tournament Workflow", value=(
        "`/tournament_create` → players register via button → staff accepts → `/tournament_start` → `/tournament_delete`"
    ), inline=False)

    embed.set_footer(text="Staff & Host only • except /stats, /info, /cmd, /setup")
    await interaction.response.send_message(embed=embed)


# ─── Run Bot ──────────────────────────────────────────────────────────────────
# TOKEN is read from the environment variable to keep it out of the source code.
# Set it with: export TOKEN=your_bot_token  (or via your hosting platform's secrets)

# ─── TEMP: r!lbreset & r!lbwins ──────────────────────────────────────────────
# Manual leaderboard management commands.
# r!lbreset          — clears the leaderboard completely
# r!lbwins @Player 3 — sets a player's wins to the given number

@bot.command()
@has_allowed_role()
async def lbreset(ctx):
    """Clears the leaderboard for this server completely."""
    guild = ctx.guild
    save_leaderboard({}, guild.id)
    stats = load_stats(guild.id)
    for uid in stats:
        if isinstance(stats[uid], dict):
            stats[uid]["games_won"]  = 0
            stats[uid]["win_streak"] = 0
    save_stats(stats, guild.id)
    # Also clear counted messages so next scan starts fresh
    counted_file = guild_file(guild.id, "counted_messages.json")
    if os.path.exists(counted_file):
        os.remove(counted_file)
    await ctx.send("✅ Leaderboard reset to 0 for all players.")


@bot.command()
@has_allowed_role()
async def lbwins(ctx, user_id: str = None, wins: int = None):
    """Sets a player's wins manually. Usage: r!lbwins USER_ID WINS"""
    if not user_id or wins is None:
        await ctx.send(
            "❌ Usage: `r!lbwins USER_ID WINS`\n"
            "Example: `r!lbwins 268855437493796874 3`\n"
            "Also works with a mention: `r!lbwins @Zilka 3`"
        )
        return

    # Accept mention format
    user_id = user_id.strip("<@!>")
    if not user_id.isdigit():
        if ctx.message.mentions:
            user_id = str(ctx.message.mentions[0].id)
        else:
            await ctx.send("❌ Invalid User ID.")
            return

    if wins < 0:
        await ctx.send("❌ Wins cannot be negative.")
        return

    guild  = ctx.guild
    member = guild.get_member(int(user_id))
    if not member:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception:
            await ctx.send(f"⚠️ User `{user_id}` not found on this server — setting wins anyway.")

    leaderboard = load_leaderboard(guild.id)
    stats       = load_stats(guild.id)
    user_stats  = get_or_create_stats(stats, user_id)

    leaderboard[user_id]         = wins
    user_stats["games_won"]      = wins
    user_stats["games_played"]   = max(user_stats.get("games_played", 0), wins)

    save_leaderboard(leaderboard, guild.id)
    save_stats(stats, guild.id)

    name = member.display_name if member else user_id
    await ctx.send(f"✅ Set **{name}** to **{wins}** win{'s' if wins != 1 else ''}.")


@bot.tree.command(name="lbreset", description="Reset the leaderboard to 0 for all players")
@slash_has_role()
async def slash_lbreset(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await lbreset(ctx)


@bot.tree.command(name="lbwins", description="Manually set a player's wins on the leaderboard")
@slash_has_role()
@app_commands.describe(
    user_id="Discord User ID or mention",
    wins="Number of wins to set"
)
async def slash_lbwins(interaction: discord.Interaction, user_id: str, wins: int):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await lbwins(ctx, user_id=user_id, wins=wins)


# Halves all leaderboard points to correct the double-counting bug.
# Run ONCE, then delete this command.

@bot.command()
@has_allowed_role()
async def fixdouble(ctx):
    guild       = ctx.guild
    leaderboard = load_leaderboard(guild.id)
    stats       = load_stats(guild.id)

    if not leaderboard:
        await ctx.send("❌ No leaderboard data found.")
        return

    updated = 0
    for uid in leaderboard:
        old = leaderboard[uid]
        leaderboard[uid] = old // 2
        user_stats = get_or_create_stats(stats, uid)
        user_stats["games_won"] = leaderboard[uid]
        updated += 1

    save_leaderboard(leaderboard, guild.id)
    save_stats(stats, guild.id)
    await ctx.send(f"✅ Halved points for **{updated}** players. Run `r!event leaderboard` to update the display.")


# Syncs leaderboard points → games_won in stats.json for all players.
# Run once to fix mismatched data, then delete.

@bot.command()
@has_allowed_role()
async def syncstats(ctx):
    guild       = ctx.guild
    leaderboard = load_leaderboard(guild.id)
    stats       = load_stats(guild.id)

    if not leaderboard:
        await ctx.send("❌ No leaderboard data found for this server.")
        return

    updated = 0
    for uid, wins in leaderboard.items():
        user_stats = get_or_create_stats(stats, uid)
        user_stats["games_won"]    = wins
        user_stats["games_played"] = max(user_stats.get("games_played", 0), wins)
        updated += 1

    save_stats(stats, guild.id)
    await ctx.send(f"✅ Synced `games_won` to match leaderboard points for **{updated}** players.")




# ═══════════════════════════════════════════════════════════════════════════════
# TOURNAMENT / TEAM SCRIM SYSTEM  (redesigned)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Flow:
#  1. r!tournament create 3v3v3v3, Title, Description, <t:TS:R>
#     → Creates Discord event + registration post with Register button
#  2. Player clicks Register button
#     → Bot creates a private ticket channel for that player
#     → Bot asks step-by-step: Team Name & Tag, Player Names, Discord User IDs
#  3. Submission posted in ticket channel with Accept/Reject for Staff
#  4. r!tournament close    → closes registration
#  5. r!tournament start    → creates private team channels
#  6. r!tournament delete   → cleans up everything

# ── File helpers ──────────────────────────────────────────────────────────────

def load_tournament(guild_id) -> dict:
    path = guild_file(guild_id, "tournament.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}

def save_tournament(guild_id, data: dict):
    path = guild_file(guild_id, "tournament.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def clear_tournament(guild_id):
    path = guild_file(guild_id, "tournament.json")
    if os.path.exists(path):
        os.remove(path)

# Active ticket conversations: {ticket_channel_id: {step, data, guild_id, user_id}}
active_tickets: dict = {}


def load_active_tickets() -> dict:
    path = os.path.join(DATA_DIR, "active_tickets.json")
    if os.path.exists(path):
        with open(path, "r") as f:
            return {int(k): v for k, v in json.load(f).items()}
    return {}


def save_active_tickets():
    path = os.path.join(DATA_DIR, "active_tickets.json")
    with open(path, "w") as f:
        json.dump({str(k): v for k, v in active_tickets.items()}, f, indent=2)


def remove_active_ticket(channel_id: int):
    if channel_id in active_tickets:
        del active_tickets[channel_id]
    save_active_tickets()


async def update_tournament_embeds(guild: discord.Guild, t_data: dict):
    """Update the registration post slots counter and refresh the roster channel."""
    accepted = [t for t in t_data.get("teams", []) if t["status"] == "accepted"]

    # ── Update registration embed ──────────────────────────────────────────────
    reg_msg_id = t_data.get("register_message_id")
    reg_ch     = bot.get_channel(get_cfg(guild.id, "channel_id"))
    if reg_msg_id and reg_ch:
        try:
            msg   = await reg_ch.fetch_message(reg_msg_id)
            embed = msg.embeds[0] if msg.embeds else None
            if embed:
                new_embed = embed.copy()
                new_embed.clear_fields()
                new_embed.add_field(
                    name="📊 Slots",
                    value=f"{len(accepted)} / {t_data['max_teams']} teams",
                    inline=True
                )
                status_val = "🔴 Closed" if t_data.get("closed") else "🟢 Open"
                status_name = "🔒 Status" if t_data.get("closed") else "🔓 Status"
                new_embed.add_field(
                    name=status_name,
                    value=status_val,
                    inline=True
                )
                await msg.edit(embed=new_embed)
        except Exception as e:
            print(f"[tournament] Could not update register embed: {e}")

    # ── Update roster channel ──────────────────────────────────────────────────
    roster_ch_id = t_data.get("roster_channel_id")
    if not roster_ch_id:
        return
    roster_ch = guild.get_channel(roster_ch_id)
    if not roster_ch:
        return

    if not accepted:
        roster_embed = discord.Embed(
            title=f"🏆 {t_data.get('title', 'Team Scrim')} — Registered Teams",
            description="No teams registered yet.",
            color=discord.Color.gold()
        )
    else:
        is_ffa_roster = t_data.get("format", "").upper().startswith("FFA")
        if is_ffa_roster:
            # FFA: all players in one combined list
            player_lines = "\n".join(
                f"**{i+1}.** **{t.get('captain_name', '?')}** — <@{t.get('captain_id', '')}>"
                for i, t in enumerate(accepted)
            )
            roster_embed = discord.Embed(
                title=f"🏆 {t_data.get('title', 'FFA Scrim')} — Registered Players",
                description=player_lines or "No players yet.",
                color=discord.Color.gold()
            )
        else:
            lines = []
            for i, team in enumerate(accepted):
                coaches  = team.get("coaches", [])
                subs     = team.get("substitutes_list", [])
                cap_id   = team.get("captain_id", "")
                starters = [p for p in team["players"] if p["discord_id"] != cap_id]

                seen_ids = set()
                all_pings = []
                for p in team["players"] + subs + coaches:
                    uid = p.get("discord_id") or p.get("discord_id", "")
                    member = guild.get_member(int(uid)) if uid else None
                    if uid and uid not in seen_ids and (not member or not member.bot):
                        all_pings.append(f"<@{uid}>")
                        seen_ids.add(uid)

                line = f"**{i+1}. {team['tag']} {team['name']}** — {' '.join(all_pings)}\n"
                if coaches:
                    line += "🧑‍🏫 Coach: " + " / ".join(f"**{c['name']}**" for c in coaches) + "\n"
                line += f"👤 Captain: **{team.get('captain_name', '?')}**\n"
                if starters:
                    line += "🎮 Starter: " + " / ".join(f"**{p['name']}**" for p in starters) + "\n"
                if subs:
                    line += "🔄 Substitute: " + " / ".join(f"**{p['name']}**" for p in subs) + "\n"
                lines.append(line.strip())
            roster_embed = discord.Embed(
                title=f"🏆 {t_data.get('title', 'Team Scrim')} — Registered Teams",
                description="\n\n".join(lines),
                color=discord.Color.gold()
            )
    roster_embed.set_footer(
        text=f"{len(accepted)}/{t_data['max_teams']} teams · "
             + ("🔴 Closed" if t_data.get("closed") else "🟢 Open for registration")
    )

    roster_msg_id = t_data.get("roster_message_id")
    if roster_msg_id:
        try:
            old = await roster_ch.fetch_message(roster_msg_id)
            await old.delete()
        except Exception:
            pass

    new_msg = await roster_ch.send(embed=roster_embed)
    t_data["roster_message_id"] = new_msg.id
    save_tournament(guild.id, t_data)


async def give_scrim_role_to_team(guild: discord.Guild, team: dict, guild_id: int):
    """Give the Scrim Player role to all accepted team members including substitutes."""
    scrim_role = guild.get_role(get_cfg(guild_id, "role_id"))
    if not scrim_role:
        return
    all_players = team.get("players", []) + team.get("substitutes_list", [])
    for player in all_players:
        member = guild.get_member(int(player["discord_id"]))
        if member and scrim_role not in member.roles:
            try:
                await member.add_roles(scrim_role)
            except Exception as e:
                print(f"Could not give scrim role to {player['discord_id']}: {e}")


# ── Register Button View ───────────────────────────────────────────────────────

class TournamentRegisterView(discord.ui.View):
    """Persistent view attached to the registration post."""
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="🎟️ Register your Team", style=discord.ButtonStyle.primary, custom_id="tourn_reg_btn")
    async def register_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        t_data = load_tournament(self.guild_id)
        if not t_data:
            await interaction.response.send_message("❌ No active tournament found.", ephemeral=True)
            return
        if t_data.get("closed"):
            await interaction.response.send_message("❌ Registration is closed for this tournament.", ephemeral=True)
            return

        guild = interaction.guild
        user  = interaction.user

        # Check if user already has an open ticket
        for ch_id, state in active_tickets.items():
            if state.get("user_id") == user.id and state.get("guild_id") == guild.id:
                ch = guild.get_channel(ch_id)
                if ch:
                    await interaction.response.send_message(
                        f"❌ You already have an open ticket: {ch.mention}", ephemeral=True
                    )
                    return

        # Create private ticket channel
        allowed_role_ids = get_cfg(guild.id, "allowed_roles")
        allowed_roles    = [guild.get_role(rid) for rid in allowed_role_ids if guild.get_role(rid)]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for role in allowed_roles:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        # Find or create ticket category
        cat_name = "📋 SCRIM TICKETS"
        category = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)

        ticket_ch = await guild.create_text_channel(
            name=f"[ticket]-{user.display_name.lower().replace(' ', '-')[:20]}",
            category=category,
            overwrites=overwrites,
            topic=f"Team registration ticket for {user.display_name}"
        )

        # Initialize ticket state
        active_tickets[ticket_ch.id] = {
            "guild_id": guild.id,
            "user_id":  user.id,
            "step":     1,
            "data":     {}
        }
        save_active_tickets()

        # Send first question — FFA gets a different prompt
        team_size = t_data["team_size"]
        is_ffa_ticket = t_data.get("format", "").upper().startswith("FFA")

        if is_ffa_ticket:
            # For FFA, go directly to name step
            active_tickets[ticket_ch.id]["step"] = "ffa_name"
            save_active_tickets()
            embed = discord.Embed(
                title="🎟️ FFA Registration — Step 1 / 2",
                color=discord.Color.blurple()
            )
            embed.add_field(
                name="🎮 Your In-Game Name",
                value=f"Welcome {user.mention}! Register for the **{t_data['format'].upper()}** scrim.\n\nEnter your **in-game name**.\n\nExample: `Biffeur`",
                inline=False
            )
            embed.set_footer(text="Type cancel at any time to abort")
        else:
            embed = discord.Embed(
                title="🎟️ Team Registration",
                description=(
                    f"Welcome {user.mention}! Let's register your team for the **{t_data['format'].upper()}** scrim.\n\n"
                    f"**Step 1 of 5**\n"
                    f"Please enter your **Team Name and Tag**.\n\n"
                    f"Example: `Alpha Squad [ALF]`\n\n"
                    f"*(Type `cancel` at any time to abort)*"
                ),
                color=discord.Color.blurple()
            )
        await ticket_ch.send(embed=embed)
        await interaction.response.send_message(
            f"✅ Your ticket has been created: {ticket_ch.mention}", ephemeral=True
        )


# ── Accept/Reject View ────────────────────────────────────────────────────────

class TeamApprovalView(discord.ui.View):
    def __init__(self, guild_id: int, team_id: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.team_id  = team_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="tourn_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed = get_cfg(self.guild_id, "allowed_roles")
        user_role_ids = [r.id for r in interaction.user.roles]
        if not interaction.user.guild_permissions.administrator and not any(r in user_role_ids for r in allowed):
            await interaction.response.send_message("❌ Only Staff/Host can accept teams.", ephemeral=True)
            return

        t_data = load_tournament(self.guild_id)
        team   = next((t for t in t_data.get("teams", []) if t["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("❌ Team not found.", ephemeral=True)
            return
        if team["status"] == "accepted":
            await interaction.response.send_message("⚠️ Already accepted.", ephemeral=True)
            return

        team["status"] = "accepted"
        save_tournament(self.guild_id, t_data)

        accepted_count = len([t for t in t_data["teams"] if t["status"] == "accepted"])
        slots_left     = t_data["max_teams"] - accepted_count

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        player_mentions = " ".join(f"<@{p['discord_id']}>" for p in team["players"])
        await interaction.response.send_message(
            f"✅ **{team['tag']} {team['name']}** accepted by {interaction.user.mention}!\n"
            f"Players: {player_mentions}\n"
            f"Slots remaining: **{slots_left}/{t_data['max_teams']}**"
        )

        # Update registration embed and roster channel
        await update_tournament_embeds(interaction.guild, t_data)

        # Give Scrim Player role to accepted team members immediately
        await give_scrim_role_to_team(interaction.guild, team, self.guild_id)

        # Give all team members (starters, subs, coaches) access to the ticket channel
        ticket_ch = interaction.guild.get_channel(team.get("ticket_channel_id"))
        if ticket_ch:
            all_team_members = (
                [p["discord_id"] for p in team.get("players", [])] +
                [p["discord_id"] for p in team.get("substitutes_list", [])] +
                [p["discord_id"] for p in team.get("coaches", [])]
            )
            for uid in all_team_members:
                member = interaction.guild.get_member(int(uid))
                if member:
                    try:
                        await ticket_ch.set_permissions(
                            member,
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True
                        )
                    except Exception as e:
                        print(f"Could not give ticket access to {uid}: {e}")

            # Notify the team in their ticket channel (now everyone can see it)
            player_pings = " ".join(f"<@{uid}>" for uid in all_team_members)
            await ticket_ch.send(
                f"🎉 {player_pings}\n"
                f"Your team **{team['tag']} {team['name']}** has been **accepted**! "
                f"You're in the scrim. Good luck! 🏆"
            )
        spectator_role   = interaction.guild.get_role(get_cfg(self.guild_id, "spectator_role_id"))
        game_links_ch    = interaction.guild.get_channel(get_cfg(self.guild_id, "game_links_id"))
        scrim_chat_ch    = interaction.guild.get_channel(get_cfg(self.guild_id, "scrim_chat_id"))
        for coach in team.get("coaches", []):
            member = interaction.guild.get_member(int(coach["discord_id"]))
            if not member:
                continue
            if spectator_role and spectator_role not in member.roles:
                try:
                    await member.add_roles(spectator_role)
                except Exception as e:
                    print(f"Could not give spectator role to coach {coach['discord_id']}: {e}")
            if game_links_ch:
                try:
                    await game_links_ch.set_permissions(member, view_channel=True, send_messages=False, read_message_history=True)
                except Exception as e:
                    print(f"Could not set game-links perms for coach: {e}")
            if scrim_chat_ch:
                try:
                    await scrim_chat_ch.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
                except Exception as e:
                    print(f"Could not set scrim-chat perms for coach: {e}")

        # Auto-close if full
        if slots_left <= 0:
            t_data["closed"] = True
            save_tournament(self.guild_id, t_data)
            # Update registration embed to show closed status
            await update_tournament_embeds(interaction.guild, t_data)
            register_channel = bot.get_channel(get_cfg(self.guild_id, "channel_id"))
            if register_channel:
                await register_channel.send(
                    "🔒 **Registration is now CLOSED!** All team slots are filled. "
                    "No more teams can register for this scrim."
                )
            await interaction.followup.send("🔒 Tournament is **full** – registration automatically closed!")

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="tourn_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed = get_cfg(self.guild_id, "allowed_roles")
        user_role_ids = [r.id for r in interaction.user.roles]
        if not interaction.user.guild_permissions.administrator and not any(r in user_role_ids for r in allowed):
            await interaction.response.send_message("❌ Only Staff/Host can reject teams.", ephemeral=True)
            return

        t_data = load_tournament(self.guild_id)
        team   = next((t for t in t_data.get("teams", []) if t["team_id"] == self.team_id), None)
        if not team:
            await interaction.response.send_message("❌ Team not found.", ephemeral=True)
            return

        team["status"] = "rejected"
        save_tournament(self.guild_id, t_data)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        player_mentions = " ".join(f"<@{p['discord_id']}>" for p in team["players"])
        await interaction.response.send_message(
            f"❌ **{team['tag']} {team['name']}** rejected by {interaction.user.mention}.\n"
            f"Players: {player_mentions}"
        )

        # Notify in ticket channel
        ticket_ch_id = team.get("ticket_channel_id")
        if ticket_ch_id:
            ticket_ch = interaction.guild.get_channel(ticket_ch_id)
            if ticket_ch:
                await ticket_ch.send(
                    f"❌ Unfortunately your team **{team['tag']} {team['name']}** was **rejected**.\n"
                    f"Please contact a Host for more information."
                )


# ── Ticket conversation handler (called from on_message) ──────────────────────

def parse_name_id_lines(text, count):
    """Parse 'Name: USER_ID' lines. Returns (list of {name,discord_id}, errors)."""
    lines   = [l.strip() for l in text.split('\n') if l.strip()]
    results = []
    errors  = []
    for line in lines:
        if ':' in line:
            nm, uid = line.split(':', 1)
            uid = uid.strip()
            if uid.isdigit():
                results.append({'name': nm.strip(), 'discord_id': uid})
            else:
                errors.append(f'Invalid ID for `{nm.strip()}`: `{uid}`')
        else:
            errors.append(f'Invalid format: `{line}` — use `Name: USER_ID`')
    return results, errors


async def handle_ticket_message(message):
    """Handles step-by-step registration conversation in ticket channels.
    Steps:
      1 – Team Name & Tag
      2 – Captain (Name: USER_ID)
      3 – Remaining starters (Name: USER_ID per line, team_size-1 entries)
      4 – Substitutes (Name: USER_ID, optional)
      5 – Coaches (Name: USER_ID, optional, max 3)
    """
    state = active_tickets.get(message.channel.id)
    if not state or message.author.id != state['user_id']:
        return

    guild_id    = state['guild_id']
    t_data      = load_tournament(guild_id)
    team_size   = t_data.get('team_size', 1) if t_data else 1
    substitutes = t_data.get('substitutes', 0) if t_data else 0
    content     = message.content.strip()

    if content.lower() == 'cancel':
        remove_active_ticket(message.channel.id)
        await message.channel.send('❌ Registration cancelled. This channel will be deleted in 10 seconds.')
        await asyncio.sleep(10)
        try:
            await message.channel.delete()
        except Exception:
            pass
        return

    step    = state['step']
    is_solo = (team_size == 1)
    is_ffa  = t_data.get("format", "").upper().startswith("FFA") if t_data else False

    # ── Step 1: Team Name & Tag  (or FFA: just player name + ID) ─────────────
    if step == 1:
        if is_ffa:
            # FFA: ask for in-game name and Discord User ID in one step
            state['step'] = 'ffa_name'
            save_active_tickets()
            embed = discord.Embed(title='🎟️ FFA Registration — Step 1 / 2', color=discord.Color.blurple())
            embed.add_field(
                name='🎮 Your In-Game Name',
                value='Enter your **in-game name**.\n\nExample: `Biffeur`',
                inline=False
            )
            embed.set_footer(text='Type cancel at any time to abort')
            await message.channel.send(embed=embed)
            return

        import re
        tag_match = re.search(r'\[(.+?)\]', content)
        if not tag_match:
            await message.channel.send(
                '❌ Please include your **team tag in brackets**.\n'
                'Example: `Alpha Squad [ALF]` or `[CYN] Cynosure`'
            )
            return
        tag  = f'[{tag_match.group(1)}]'
        name = content.replace(tag_match.group(0), '').strip()
        if not name:
            await message.channel.send('❌ Please also include a **team name** alongside the tag.\nExample: `Alpha Squad [ALF]`')
            return
        state['data']['team_name'] = name
        state['data']['team_tag']  = tag
        state['step'] = 2
        save_active_tickets()

        embed = discord.Embed(title='🎟️ Team Registration — Step 2 / 5', color=discord.Color.blurple())
        embed.add_field(name='✅ Team', value=f'**{tag} {name}**', inline=False)
        embed.add_field(
            name='👤 Captain (Name + ID)',
            value=(
                'Enter your **captain** in this format:\n'
                '`Name: USER_ID`\n\n'
                'The captain fills **1 player slot**.\n'
                'Right-click the user → **Copy User ID** *(Developer Mode on)*\n\n'
                'Example: `Biffeur: 123456789012345678`'
            ),
            inline=False
        )
        embed.set_footer(text='Type cancel at any time to abort')
        await message.channel.send(embed=embed)

    # ── FFA Step: In-game name ─────────────────────────────────────────────────
    elif step == 'ffa_name':
        if not content:
            await message.channel.send('❌ Please enter your in-game name.')
            return
        state['data']['captain_name'] = content
        state['step'] = 'ffa_id'
        save_active_tickets()
        embed = discord.Embed(title='🎟️ FFA Registration — Step 2 / 2', color=discord.Color.blurple())
        embed.add_field(name='✅ Name', value=f'**{content}**', inline=False)
        embed.add_field(
            name='🆔 Discord User ID',
            value='Enter your **Discord User ID**.\n\nRight-click yourself → **Copy User ID**\n*(Developer Mode must be on)*\n\nExample: `123456789012345678`',
            inline=False
        )
        embed.set_footer(text='Type cancel at any time to abort')
        await message.channel.send(embed=embed)

    # ── FFA Step: Discord ID → submit ──────────────────────────────────────────
    elif step == 'ffa_id':
        if not content.isdigit():
            await message.channel.send('❌ Please enter a valid Discord User ID (numbers only).')
            return

        # Check duplicate in existing accepted players
        existing_ids = []
        for t in t_data.get('teams', []):
            if t.get('status') == 'accepted':
                existing_ids.extend([p['discord_id'] for p in t.get('players', [])])
        if content in existing_ids:
            await message.channel.send('❌ This Discord ID is already registered in an accepted slot.')
            return

        state['data']['captain_id'] = content
        state['data']['team_name']  = state['data']['captain_name']
        state['data']['team_tag']   = '[FFA]'
        state['data']['player_names'] = []
        state['data']['player_ids']   = []
        state['data']['subs']         = []
        state['data']['coaches']      = []
        await _submit_team(message, state, t_data, guild_id, team_size)

    # ── Step 2: Captain (Name: ID) ─────────────────────────────────────────────
    elif step == 2:
        parsed, errors = parse_name_id_lines(content, 1)
        if errors or len(parsed) != 1:
            await message.channel.send(
                '❌ Please enter exactly **1** captain in format `Name: USER_ID`.\n'
                + ('\n'.join(errors) if errors else '')
            )
            return
        state['data']['captain_name'] = parsed[0]['name']
        state['data']['captain_id']   = parsed[0]['discord_id']
        state['data']['player_names'] = []
        state['data']['player_ids']   = []
        state['data']['subs']         = []
        state['data']['coaches']      = []

        if is_solo:
            # Solo: captain is the only player — submit immediately
            state['step'] = 99  # mark as done
            save_active_tickets()
            await _submit_team(message, state, t_data, guild_id, team_size)
            return

        remaining = team_size - 1
        state['step'] = 3
        save_active_tickets()
        if remaining > 0:
            embed = discord.Embed(title='🎟️ Team Registration — Step 3 / 5', color=discord.Color.blurple())
            embed.add_field(name='✅ Captain', value=f'**{parsed[0]["name"]}** — <@{parsed[0]["discord_id"]}>', inline=False)
            embed.add_field(
                name=f'🎮 Remaining Starters ({remaining})',
                value=(
                    f'The captain fills **1 slot**. Enter the remaining **{remaining}** starter(s):\n'
                    f'One per line as `Name: USER_ID`\n\n'
                    f'Example:\n```\nPlayerTwo: 987654321098765432\nPlayerThree: 112233445566778899\n```'
                ),
                inline=False
            )
            if substitutes > 0:
                embed.set_footer(text=f'After this: substitutes ({substitutes}) → coaches | Type cancel to abort')
            else:
                embed.set_footer(text='After this: coaches (optional) | Type cancel to abort')
            await message.channel.send(embed=embed)
        else:
            # 1v1 — no additional starters
            state['data']['player_names'] = []
            state['data']['player_ids']   = []
            state['step'] = 4 if substitutes > 0 else 5
            save_active_tickets()
            if substitutes > 0:
                embed = discord.Embed(title='🎟️ Team Registration — Step 4 / 5', color=discord.Color.blurple())
                embed.add_field(name=f'🔄 Substitutes ({substitutes})', value=f'Enter **{substitutes}** substitute(s) as `Name: USER_ID`, one per line.', inline=False)
                embed.set_footer(text='After this: coaches (optional) | Type cancel to abort')
            else:
                embed = discord.Embed(title='🎟️ Team Registration — Coaches (Optional)', color=discord.Color.blurple())
                embed.add_field(name='🧑‍🏫 Coaches (max 3)', value='Enter coach(es) as `Name: USER_ID`, one per line.\nType `none` if none.\n\nExample:\n```\nCoachMike: 123456789012345678\n```', inline=False)
                embed.set_footer(text='Coaches get Spectator role + access to game-links & scrim-chat')
            await message.channel.send(embed=embed)

    # ── Step 3: Remaining starters ─────────────────────────────────────────────
    elif step == 3:
        remaining = team_size - 1
        parsed, errors = parse_name_id_lines(content, remaining)
        if errors:
            await message.channel.send('❌ Errors:\n' + '\n'.join(errors) + '\nPlease re-enter all starters.')
            return
        if len(parsed) != remaining:
            await message.channel.send(f'❌ Please enter exactly **{remaining}** starter(s). You entered {len(parsed)}.')
            return

        captain_id = state['data']['captain_id']
        all_ids    = [captain_id] + [p['discord_id'] for p in parsed]
        if len(set(all_ids)) < len(all_ids):
            await message.channel.send('❌ Duplicate User IDs detected! Each player can only appear once.')
            return

        state['data']['player_names'] = [p['name'] for p in parsed]
        state['data']['player_ids']   = [p['discord_id'] for p in parsed]
        state['step'] = 4 if substitutes > 0 else 5
        save_active_tickets()

        if substitutes > 0:
            embed = discord.Embed(title='🎟️ Team Registration — Step 4 / 5', color=discord.Color.blurple())
            embed.add_field(
                name=f'🔄 Substitutes ({substitutes})',
                value=f'Enter **{substitutes}** substitute(s) as `Name: USER_ID`, one per line.\n\nExample:\n```\nSubOne: 112233445566778899\n```',
                inline=False
            )
            embed.set_footer(text='After this: coaches (optional) | Type cancel to abort')
        else:
            embed = discord.Embed(title='🎟️ Team Registration — Coaches (Optional)', color=discord.Color.blurple())
            embed.add_field(name='🧑‍🏫 Coaches (max 3)', value='Enter coach(es) as `Name: USER_ID`, one per line.\nType `none` if none.\n\nExample:\n```\nCoachMike: 123456789012345678\n```', inline=False)
            embed.set_footer(text='Coaches get Spectator role + access to game-links & scrim-chat')
        await message.channel.send(embed=embed)

    # ── Step 4: Substitutes ────────────────────────────────────────────────────
    elif step == 4:
        parsed, errors = parse_name_id_lines(content, substitutes)
        if errors:
            await message.channel.send('❌ Errors:\n' + '\n'.join(errors) + '\nPlease re-enter all substitutes.')
            return
        if len(parsed) != substitutes:
            await message.channel.send(f'❌ Please enter exactly **{substitutes}** substitute(s). You entered {len(parsed)}.')
            return

        captain_id = state['data']['captain_id']
        all_ids    = [captain_id] + state['data']['player_ids'] + [p['discord_id'] for p in parsed]
        if len(set(all_ids)) < len(all_ids):
            await message.channel.send('❌ Duplicate User IDs detected! Each player can only appear once.')
            return

        state['data']['subs'] = parsed
        state['step'] = 5
        save_active_tickets()

        embed = discord.Embed(title='🎟️ Team Registration — Coaches (Optional)', color=discord.Color.blurple())
        embed.add_field(name='🧑‍🏫 Coaches (max 3)', value='Enter coach(es) as `Name: USER_ID`, one per line.\nType `none` if none.\n\nExample:\n```\nCoachMike: 123456789012345678\n```', inline=False)
        embed.set_footer(text='Coaches get Spectator role + access to game-links & scrim-chat')
        await message.channel.send(embed=embed)

    # ── Step 5: Coaches ────────────────────────────────────────────────────────
    elif step == 5:
        coaches = []
        if content.lower() != 'none':
            parsed, errors = parse_name_id_lines(content, 0)
            if errors:
                await message.channel.send('❌ Errors:\n' + '\n'.join(errors) + '\nRe-enter coaches or type `none`.')
                return
            if len(parsed) > 3:
                await message.channel.send('❌ Maximum **3 coaches** allowed.')
                return
            coach_ids = [c['discord_id'] for c in parsed]
            if len(set(coach_ids)) < len(coach_ids):
                await message.channel.send('❌ Duplicate coach IDs detected!')
                return
            captain_id = state['data'].get('captain_id', '')
            all_ids    = [captain_id] + state['data'].get('player_ids', []) + [p['discord_id'] for p in state['data'].get('subs', [])] + coach_ids
            if len(set(all_ids)) < len(all_ids):
                await message.channel.send('❌ A coach ID matches an existing player ID. Each person can only appear once.')
                return
            coaches = parsed

        state['data']['coaches'] = coaches
        await _submit_team(message, state, t_data, guild_id, team_size)


async def _submit_team(message, state, t_data, guild_id, team_size):
    """Builds and submits the team entry, sends summary and review embeds."""
    names      = state['data']['player_names']
    ids        = state['data']['player_ids']
    captain_id = state['data']['captain_id']
    subs       = state['data'].get('subs', [])
    coaches    = state['data'].get('coaches', [])

    # Check duplicates against accepted teams
    existing_ids = []
    for t in t_data.get('teams', []):
        if t.get('status') == 'accepted':
            existing_ids.extend([p['discord_id'] for p in t.get('players', [])])
            existing_ids.extend([p['discord_id'] for p in t.get('substitutes_list', [])])
            existing_ids.extend([p['discord_id'] for p in t.get('coaches', [])])
            existing_ids.append(t.get('captain_id', ''))
    all_new = [captain_id] + ids + [s['discord_id'] for s in subs] + [c['discord_id'] for c in coaches]
    if any(pid in existing_ids for pid in all_new):
        await message.channel.send('❌ One or more players are already registered in an accepted team.')
        return

    starters = [{'name': state['data']['captain_name'], 'discord_id': captain_id}] + \
               [{'name': names[i], 'discord_id': ids[i]} for i in range(len(names))]

    team_id = f"team_{len(t_data.get('teams', []))}"
    team = {
        'team_id': team_id, 'name': state['data']['team_name'], 'tag': state['data']['team_tag'],
        'captain_name': state['data']['captain_name'], 'captain_id': captain_id,
        'players': starters, 'substitutes_list': subs, 'coaches': coaches,
        'status': 'pending', 'submitter': str(state['user_id']),
        'ticket_channel_id': message.channel.id, 'channel_id': None
    }
    t_data.setdefault('teams', []).append(team)
    save_tournament(guild_id, t_data)

    # Summary embed
    summary = discord.Embed(title='📋 Registration Summary', description=f'**{team["tag"]} {team["name"]}**', color=discord.Color.green())
    if coaches:
        summary.add_field(name=f'🧑‍🏫 Coaches ({len(coaches)})', value='\n'.join(f'• **{c["name"]}** — <@{c["discord_id"]}>' for c in coaches), inline=False)
    summary.add_field(name='👤 Captain', value=f'• **{team["captain_name"]}** — <@{captain_id}>', inline=False)
    summary.add_field(name=f'🎮 Starters ({len(starters)})', value='\n'.join(f'• **{p["name"]}** — <@{p["discord_id"]}>' for p in starters), inline=False)
    if subs:
        summary.add_field(name=f'🔄 Substitutes ({len(subs)})', value='\n'.join(f'• **{p["name"]}** — <@{p["discord_id"]}>' for p in subs), inline=False)
    summary.add_field(name='📬 Status', value='Submitted — waiting for host review.', inline=False)
    summary.set_footer(text='A host will accept or reject your team shortly.')
    await message.channel.send(embed=summary)

    # Staff review embed
    guild        = message.guild
    review_ch_id = t_data.get('review_channel_id')
    review_ch    = guild.get_channel(review_ch_id) if review_ch_id else bot.get_channel(get_cfg(guild_id, 'scrim_chat_id'))
    if review_ch:
        review = discord.Embed(title=f'📋 New Team — {team["tag"]} {team["name"]}', color=discord.Color.orange())
        if coaches:
            review.add_field(name=f'🧑‍🏫 Coaches ({len(coaches)})', value='\n'.join(f'**{c["name"]}** — <@{c["discord_id"]}>' for c in coaches), inline=False)
        review.add_field(name='👤 Captain', value=f'**{team["captain_name"]}** — <@{captain_id}>', inline=False)
        review.add_field(name=f'🎮 Starters ({len(starters)})', value='\n'.join(f'**{p["name"]}** — <@{p["discord_id"]}>' for p in starters), inline=False)
        if subs:
            review.add_field(name=f'🔄 Substitutes ({len(subs)})', value='\n'.join(f'**{p["name"]}** — <@{p["discord_id"]}>' for p in subs), inline=False)
        review.add_field(name='Submitted by', value=f'<@{state["user_id"]}>', inline=False)
        review.set_footer(text=f'Team ID: {team_id}')
        view = TeamApprovalView(guild_id=guild_id, team_id=team_id)
        await review_ch.send(embed=review, view=view)

    remove_active_ticket(message.channel.id)


# ── r!tournament command ───────────────────────────────────────────────────────

@bot.command()
@has_allowed_role()
async def tournament(ctx, subcommand: str = None, *, args=None):
    guild = ctx.guild

    # ── CREATE ────────────────────────────────────────────────────────────────
    if subcommand == "create":
        if not args:
            await ctx.send(
                "❌ Usage: `r!tournament create FORMAT, GROUPS, SUBSTITUTES, Title, Description, <t:TIMESTAMP:R>`\n"
                "Examples:\n"
                "• `r!tournament create 3v3, 1, 0, Friday Scrim, Competitive, <t:1700000000:R>` — 2 teams of 3\n"
                "• `r!tournament create 3v3v3v3, 2, 1, Friday Scrim, 24 Players, <t:1700000000:R>` — 2 groups of 4 teams\n"
                "• `r!tournament create ffa, 8, 0, FFA Event, 8 Players FFA, <t:1700000000:R>` — 8-player FFA (using GROUPS)\n"
                "• `r!tournament create ffa16, 1, 0, FFA Event, 16 Players FFA, <t:1700000000:R>` — 16-player FFA\n"
                "**FORMAT** = `3v3`, `4v4v4`, `3v3v3v3`, `ffa8`, `ffa16` etc.\n"
                "**GROUPS** = number of groups (1 = single group)\n"
                "**SUBSTITUTES** = reserve players per team (0 for solo/FFA)"
            )
            return

        parts = [p.strip() for p in args.split(",", 6)]
        if len(parts) < 6:
            await ctx.send(
                "❌ Please provide all values: FORMAT, GROUPS, SUBSTITUTES, Title, Description, Timestamp\n"
                "Example: `r!tournament create 3v3v3v3, 2, 1, Friday Scrim, Competitive, <t:1700000000:R>`"
            )
            return

        fmt_raw     = parts[0].lower().strip()
        groups_str  = parts[1].strip()
        sub_str     = parts[2].strip()
        title       = parts[3].strip()
        description = parts[4].strip()
        time_str    = parts[5].strip()

        # Parse groups and substitutes first
        try:
            groups      = int(groups_str)
            substitutes = int(sub_str)
        except ValueError:
            await ctx.send("❌ GROUPS and SUBSTITUTES must be numbers. Example: `2, 1`")
            return

        # Parse format string — supports "3v3v3v3" and "ffa8" / "ffa" (with groups as player count)
        import re
        is_ffa = fmt_raw.startswith("ffa")

        if is_ffa:
            ffa_str = fmt_raw[3:]  # e.g. "ffa8" → "8", "ffa" → ""
            if ffa_str == "":
                # Plain "ffa" — use groups as total player count
                if groups < 2:
                    await ctx.send(
                        "❌ For `ffa` format please specify the number of players via **GROUPS**.\n"
                        "Example: `r!tournament create ffa, 8, 0, Title, Desc, <t:TS:R>` — 8-player FFA\n"
                        "Or use the number directly: `ffa8`"
                    )
                    return
                team_size       = 1
                teams_per_group = groups
                max_teams       = groups
                groups          = 1   # treat as single group
                fmt             = f"FFA{teams_per_group}"
            elif ffa_str.isdigit() and int(ffa_str) >= 2:
                team_size       = 1
                teams_per_group = int(ffa_str)
                fmt             = f"FFA{ffa_str}"
            else:
                await ctx.send(
                    "❌ Invalid FFA format. Use `ffa` and specify players via GROUPS, or `ffa8`, `ffa16` etc.\n"
                    "Example: `r!tournament create ffa, 8, 0, Title, Desc, <t:TS:R>`"
                )
                return
        else:
            segments = fmt_raw.split("v")
            if len(segments) < 2 or not all(s.isdigit() for s in segments):
                await ctx.send("❌ Invalid format. Use `3v3`, `4v4v4`, `3v3v3v3`, or `ffa8` etc.")
                return
            team_sizes = [int(s) for s in segments]
            if len(set(team_sizes)) > 1:
                await ctx.send("❌ All team sizes must be equal (e.g. `3v3v3v3`, not `2v3v4`).")
                return
            team_size       = team_sizes[0]
            teams_per_group = len(team_sizes)
            fmt             = fmt_raw

        if team_size < 1 or teams_per_group < 2 or groups < 1 or substitutes < 0:
            await ctx.send("❌ Invalid values. Team size ≥1, format must have at least 2 teams, groups ≥1.")
            return

        # Solo or FFA format (team_size == 1) cannot have substitutes or coaches
        if team_size == 1 and substitutes > 0:
            fmt_display = fmt.upper() if is_ffa else fmt.upper()
            await ctx.send(
                f"❌ **{fmt_display}** format (1 player per team) cannot have substitutes.\n"
                f"Please use `0` for substitutes."
            )
            return

        max_teams   = teams_per_group * groups
        max_players = team_size * max_teams

        # Validate: max_players must be divisible by team_size × teams_per_group
        if max_players % team_size != 0:
            await ctx.send(
                f"❌ Invalid player count! With format **{fmt.upper()}** and **{groups}** group(s):\n"
                f"• Teams per group: {teams_per_group}\n"
                f"• Total teams: {max_teams}\n"
                f"• Required players: **{max_players}** ({team_size} × {max_teams})\n\n"
                f"The numbers don't add up. Adjust your format or group count."
            )
            return

        # Inform if groups > 1
        groups_info = f" across **{groups} groups** ({teams_per_group} teams each)" if groups > 1 else ""

        if load_tournament(guild.id):
            await ctx.send("❌ A tournament is already active! Use `r!tournament delete` first.")
            return

        # Extract timestamp
        ts_match = re.search(r'<t:(\d+)', time_str)
        if not ts_match:
            await ctx.send("❌ Invalid timestamp. Generate one at discordtimestamp.com and use `<t:...:R>` format.")
            return
        timestamp = int(ts_match.group(1))
        start_dt  = datetime.fromtimestamp(timestamp, tz=timezone.utc)

        # Create Discord event
        event_channel = guild.get_channel(get_cfg(guild.id, "event_channel_id"))
        try:
            event = await guild.create_scheduled_event(
                name=title,
                description=f"{description}\nFormat: {fmt.upper()} | {max_teams} Teams of {team_size}",
                start_time=start_dt,
                end_time=datetime.fromtimestamp(timestamp + 7200, tz=timezone.utc),
                entity_type=discord.EntityType.voice,
                channel=event_channel,
                privacy_level=discord.PrivacyLevel.guild_only,
            )
        except Exception as e:
            await ctx.send(f"⚠️ Could not create Discord event: `{e}`\nContinuing with registration post...")
            event = None

        # Find/create review channel for staff
        cat_name   = "📋 SCRIM TICKETS"
        category   = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)

        allowed_role_ids = get_cfg(guild.id, "allowed_roles")
        allowed_roles    = [guild.get_role(rid) for rid in allowed_role_ids if guild.get_role(rid)]
        review_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        for role in allowed_roles:
            review_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        review_ch = await guild.create_text_channel(
            name="team-registrations",
            category=category,
            overwrites=review_overwrites,
            topic="Team registration reviews for Staff/Host"
        )

        # Save tournament
        t_data = {
            "format":            fmt,
            "title":             title,
            "team_size":         team_size,
            "teams_per_group":   teams_per_group,
            "groups":            groups,
            "max_teams":         max_teams,
            "max_players":       max_players,
            "substitutes":       substitutes,
            "closed":            False,
            "event_id":          event.id if event else None,
            "review_channel_id": review_ch.id,
            "roster_channel_id": get_cfg(guild.id, "registered_teams_channel_id"),
            "roster_message_id": None,
            "register_message_id": None,
            "teams":             []
        }
        save_tournament(guild.id, t_data)

        # Post registration embed
        register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))
        event_link = f"https://discord.com/events/{guild.id}/{event.id}" if event else ""
        mention_roles = get_cfg(guild.id, "mention_roles")
        mentions = " ".join(f"<@&{r}>" for r in mention_roles)

        sub_line = f"**Substitutes:** {substitutes} per team\n" if substitutes > 0 else ""
        groups_line = f"**Groups:** {groups} (à {teams_per_group} teams)\n" if groups > 1 else ""
        embed = discord.Embed(
            title=f"🏆 {title}",
            description=(
                f"{description}\n\n"
                f"**Format:** {fmt.upper()}\n"
                f"**Team Size:** {team_size} players per team\n"
                + groups_line + sub_line +
                f"**Total Teams:** {max_teams}\n"
                f"**Total Players:** {max_players}\n"
                f"**Date:** {time_str.strip()}\n\n"
                + (f"[📅 View Event]({event_link})\n\n" if event_link else "")
                + f"Click the button below to register your team!"
            ),
            color=discord.Color.gold()
        )
        embed.add_field(name="📊 Slots", value=f"0 / {max_teams} teams", inline=True)
        embed.add_field(name="🔓 Status", value="🟢 Open", inline=True)

        view = TournamentRegisterView(guild_id=guild.id)
        msg  = await register_channel.send(content=mentions, embed=embed, view=view)
        t_data["register_message_id"] = msg.id
        save_tournament(guild.id, t_data)

        await ctx.send(
            f"✅ Tournament **{fmt.upper()} — {title}** created!\n"
            f"• Registration post: {register_channel.mention}\n"
            f"• Staff review channel: {review_ch.mention}"
        )

    # ── LIST ──────────────────────────────────────────────────────────────────
    elif subcommand == "list":
        t_data = load_tournament(guild.id)
        if not t_data:
            await ctx.send("❌ No active tournament.")
            return
        teams    = t_data.get("teams", [])
        accepted = [t for t in teams if t["status"] == "accepted"]
        pending  = [t for t in teams if t["status"] == "pending"]
        rejected = [t for t in teams if t["status"] == "rejected"]

        embed = discord.Embed(
            title=f"🏆 Tournament — {t_data['format'].upper()} | {t_data.get('title', '')}",
            color=discord.Color.gold()
        )

        # Accepted teams
        if accepted:
            is_ffa_list = t_data.get("format", "").upper().startswith("FFA")
            if is_ffa_list:
                player_lines = "\n".join(
                    f"**{i+1}.** **{t.get('captain_name', '?')}** — <@{t.get('captain_id', '')}>"
                    for i, t in enumerate(accepted)
                )
                embed.add_field(
                    name=f"✅ Accepted Players ({len(accepted)}/{t_data['max_teams']})",
                    value=player_lines or "None yet",
                    inline=False
                )
            else:
                acc_lines = []
                for t in accepted:
                    coaches  = t.get("coaches", [])
                    subs     = t.get("substitutes_list", [])
                    cap_id   = t.get("captain_id", "")
                    starters = [p for p in t["players"] if p["discord_id"] != cap_id]
                    line = f"**{t['tag']} {t['name']}**\n"
                    if coaches:
                        line += "🧑‍🏫 Coach: " + " / ".join(f"**{c['name']}**" for c in coaches) + "\n"
                    line += f"👤 Captain: **{t.get('captain_name', '?')}**\n"
                    if starters:
                        line += "🎮 Starter: " + " / ".join(f"**{p['name']}**" for p in starters) + "\n"
                    if subs:
                        line += "🔄 Substitute: " + " / ".join(f"**{p['name']}**" for p in subs)
                    acc_lines.append(line.strip())
                embed.add_field(
                    name=f"✅ Accepted ({len(accepted)}/{t_data['max_teams']})",
                    value="\n\n".join(acc_lines),
                    inline=False
                )
        else:
            embed.add_field(name=f"✅ Accepted (0/{t_data['max_teams']})", value="None yet", inline=False)

        if pending:
            embed.add_field(
                name=f"⏳ Pending ({len(pending)})",
                value="\n".join(f"**{t['tag']} {t['name']}**" for t in pending),
                inline=False
            )
        if rejected:
            embed.add_field(
                name=f"❌ Rejected ({len(rejected)})",
                value="\n".join(f"**{t['tag']} {t['name']}**" for t in rejected),
                inline=False
            )

        embed.set_footer(text="🔴 Closed" if t_data.get("closed") else "🟢 Open for registration")
        await ctx.send(embed=embed)

    # ── CLOSE ─────────────────────────────────────────────────────────────────
    elif subcommand == "close":
        t_data = load_tournament(guild.id)
        if not t_data:
            await ctx.send("❌ No active tournament.")
            return
        if t_data.get("closed"):
            await ctx.send("⚠️ Already closed.")
            return
        t_data["closed"] = True
        save_tournament(guild.id, t_data)
        accepted = [t for t in t_data.get("teams", []) if t["status"] == "accepted"]

        # Update the registration embed (slots + status)
        await update_tournament_embeds(guild, t_data)

        register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))
        if register_channel:
            await register_channel.send(
                f"🔒 **Registration is now CLOSED!** "
                f"**{len(accepted)}/{t_data['max_teams']}** teams registered."
            )
        await ctx.send("✅ Registration closed.")

    # ── START ─────────────────────────────────────────────────────────────────
    elif subcommand == "start":
        t_data = load_tournament(guild.id)
        if not t_data:
            await ctx.send("❌ No active tournament.")
            return
        accepted = [t for t in t_data.get("teams", []) if t["status"] == "accepted"]
        if not accepted:
            await ctx.send("❌ No accepted teams yet.")
            return

        await ctx.send(f"⏳ Creating private channels for **{len(accepted)}** teams...")

        cat_name  = f"🏆 SCRIM — {t_data['format'].upper()}"
        category  = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)

        allowed_role_ids = get_cfg(guild.id, "allowed_roles")
        allowed_roles    = [guild.get_role(rid) for rid in allowed_role_ids if guild.get_role(rid)]

        is_solo = (t_data.get("team_size", 1) == 1)
        is_ffa  = t_data.get("format", "").upper().startswith("FFA")

        if is_ffa:
            # FFA: one shared voice channel for all accepted players
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            for role in allowed_roles:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
            for team in accepted:
                member = guild.get_member(int(team["captain_id"]))
                if member:
                    overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)

            ffa_ch = await guild.create_voice_channel(
                f"ffa-{t_data.get('title', 'scrim').lower().replace(' ', '-')[:20]}",
                category=category,
                overwrites=overwrites
            )

            for team in accepted:
                team["channel_id"] = ffa_ch.id
                ticket_ch = guild.get_channel(team.get("ticket_channel_id"))
                if ticket_ch:
                    await ticket_ch.send(
                        f"🔊 <@{team['captain_id']}>\n"
                        f"The **{t_data['format'].upper()}** scrim is starting! "
                        f"Join the voice channel: {ffa_ch.mention} 🏆"
                    )

            register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))
            scrim_role_obj   = guild.get_role(get_cfg(guild.id, "role_id"))
            if register_channel and scrim_role_obj:
                all_mentions = " ".join(f"<@{t['captain_id']}>" for t in accepted)
                embed = discord.Embed(
                    title=f"🏆 {t_data.get('title', 'FFA Scrim')} — Starting Now!",
                    description=(
                        f"The **{t_data['format'].upper()}** scrim is live!\n\n"
                        f"**{len(accepted)} Players:** {all_mentions}\n\n"
                        f"Join the voice channel: {ffa_ch.mention}"
                    ),
                    color=discord.Color.green()
                )
                await register_channel.send(content=scrim_role_obj.mention, embed=embed)

            save_tournament(guild.id, t_data)

            if scrim_role_obj:
                for team in accepted:
                    member = guild.get_member(int(team["captain_id"]))
                    if member and scrim_role_obj not in member.roles:
                        try:
                            await member.add_roles(scrim_role_obj)
                        except Exception:
                            pass

            await ctx.send(
                f"✅ **{t_data['format'].upper()}** FFA started! Shared voice channel {ffa_ch.mention} "
                f"created for **{len(accepted)}** players.\n"
                f"Now use `r!event update` to assign Active Scrim roles."
            )
            return

        for i, team in enumerate(accepted):
            members       = [guild.get_member(int(p["discord_id"])) for p in team["players"]]
            members       = [m for m in members if m]
            sub_members   = [guild.get_member(int(p["discord_id"])) for p in team.get("substitutes_list", []) if guild.get_member(int(p["discord_id"]))]
            coach_members = [guild.get_member(int(p["discord_id"])) for p in team.get("coaches", []) if guild.get_member(int(p["discord_id"]))]
            all_members   = members + sub_members + coach_members

            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            for role in allowed_roles:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
            for member in all_members:
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

            if is_solo:
                team["channel_id"] = None
                continue

            ch_name = f"team-{i+1}-{team['name'].lower().replace(' ', '-')[:20]}"
            ch = await guild.create_voice_channel(
                ch_name, category=category, overwrites=overwrites
            )
            team["channel_id"] = ch.id

            # Notify all team members (players, subs, coaches) in their ticket channel
            ticket_ch = guild.get_channel(team.get("ticket_channel_id"))
            if ticket_ch:
                mentions = " ".join(
                    f"<@{p['discord_id']}>" for p in
                    team["players"] + team.get("substitutes_list", []) + team.get("coaches", [])
                )
                await ticket_ch.send(
                    f"🔊 {mentions}\n"
                    f"Your team **{team['tag']} {team['name']}** has been assigned a private voice channel: {ch.mention}\n"
                    f"Good luck in the **{t_data['format']}** scrim! 🏆"
                )

        save_tournament(guild.id, t_data)

        # Give all accepted players the Scrim registration role
        scrim_role  = guild.get_role(get_cfg(guild.id, "role_id"))
        role_count  = 0
        if scrim_role:
            for team in accepted:
                for player in team["players"]:
                    member = guild.get_member(int(player["discord_id"]))
                    if member and scrim_role not in member.roles:
                        try:
                            await member.add_roles(scrim_role)
                            role_count += 1
                        except Exception as e:
                            print(f"Could not give scrim role to {player['discord_id']}: {e}")

        if is_solo:
            await ctx.send(
                f"✅ Solo format — no private voice channels created.\n"
                f"🎮 **{role_count}** players received the Scrim Player role.\n"
                f"Now use `r!event update` to assign Active Scrim and Spectator roles."
            )
        else:
            await ctx.send(
                f"✅ Created **{len(accepted)}** private team voice channels under **{cat_name}**!\n"
                f"🎮 **{role_count}** players received the Scrim Player role.\n"
                f"Now use `r!event update` to assign Active Scrim and Spectator roles."
            )

        # Announce tournament start in register channel
        register_channel = bot.get_channel(get_cfg(guild.id, "channel_id"))
        scrim_role       = guild.get_role(get_cfg(guild.id, "role_id"))
        if register_channel and scrim_role:
            teams_list = "\n".join(
                f"🔊 **{t['tag']} {t['name']}** — " + ", ".join(p["name"] for p in t["players"])
                for t in accepted
            )
            embed = discord.Embed(
                title=f"🏆 {t_data.get('title', 'Team Scrim')} — The Scrim has started!",
                description=(
                    f"The **{t_data['format'].upper()}** scrim is now live!\n\n"
                    f"**Participating Teams:**\n{teams_list}\n\n"
                    f"Join your private voice channel and good luck! 🎮"
                ),
                color=discord.Color.green()
            )
            await register_channel.send(content=scrim_role.mention, embed=embed)

    # ── DELETE ────────────────────────────────────────────────────────────────
    elif subcommand == "delete":
        t_data = load_tournament(guild.id)
        if not t_data:
            await ctx.send("❌ No active tournament.")
            return

        await ctx.send("⏳ Cleaning up tournament...")
        deleted = 0

        # End/delete the Discord scheduled event (active, future, or any status)
        event_id = t_data.get("event_id")
        if event_id:
            try:
                events = await guild.fetch_scheduled_events()
                for ev in events:
                    if ev.id == event_id:
                        if ev.status == discord.EventStatus.active:
                            await ev.end()
                        elif ev.status in (discord.EventStatus.scheduled, discord.EventStatus.completed):
                            await ev.cancel()
                        print(f"Tournament event cancelled: {ev.name}")
                        break
            except Exception as e:
                print(f"Could not cancel tournament event: {e}")

        # Close any open ticket conversations and notify players
        to_remove = [cid for cid, s in active_tickets.items() if s.get("guild_id") == guild.id]
        for cid in to_remove:
            ch = guild.get_channel(cid)
            if ch:
                try:
                    await ch.send("❌ The tournament has been cancelled. This ticket will be deleted in 10 seconds.")
                    await asyncio.sleep(10)
                    await ch.delete()
                except Exception:
                    pass
            if cid in active_tickets:
                del active_tickets[cid]
        save_active_tickets()

        # Delete team voice channels
        for team in t_data.get("teams", []):
            if team.get("channel_id"):
                ch = guild.get_channel(team["channel_id"])
                if ch:
                    try:
                        await ch.delete()
                        deleted += 1
                    except Exception as e:
                        print(f"Error deleting team channel: {e}")
            # Delete ticket channels that weren't already deleted above
            if team.get("ticket_channel_id"):
                ch = guild.get_channel(team["ticket_channel_id"])
                if ch:
                    try:
                        await ch.send("❌ The tournament has been cancelled.")
                        await asyncio.sleep(2)
                        await ch.delete()
                    except Exception:
                        pass

        # Clear roster channel
        roster_ch = guild.get_channel(t_data.get("roster_channel_id"))
        if roster_ch:
            try:
                async for msg in roster_ch.history(limit=50):
                    if msg.author == bot.user:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error clearing roster channel: {e}")

        # Delete review channel and ticket category
        review_ch = guild.get_channel(t_data.get("review_channel_id"))
        if review_ch:
            try:
                await review_ch.delete()
            except Exception:
                pass

        # Delete scrim category if empty
        for cat_name in [f"🏆 SCRIM — {t_data['format'].upper()}", "📋 SCRIM TICKETS"]:
            cat = discord.utils.get(guild.categories, name=cat_name)
            if cat and len(cat.channels) == 0:
                try:
                    await cat.delete()
                except Exception:
                    pass

        # Delete ALL bot messages in register channel (registration post, closed messages, start announcements)
        reg_ch = bot.get_channel(get_cfg(guild.id, "channel_id"))
        if reg_ch:
            try:
                async for msg in reg_ch.history(limit=200):
                    if msg.author == bot.user:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error cleaning register channel: {e}")

        # Remove Scrim Player/Spectator roles and coach channel perms from all accepted players
        scrim_role     = guild.get_role(get_cfg(guild.id, "role_id"))
        spectator_role = guild.get_role(get_cfg(guild.id, "spectator_role_id"))
        game_links_ch  = guild.get_channel(get_cfg(guild.id, "game_links_id"))
        scrim_chat_ch  = guild.get_channel(get_cfg(guild.id, "scrim_chat_id"))

        if t_data.get("teams"):
            for team in t_data["teams"]:
                if team.get("status") == "accepted":
                    all_players = team.get("players", []) + team.get("substitutes_list", [])
                    for player in all_players:
                        member = guild.get_member(int(player["discord_id"]))
                        if member and scrim_role and scrim_role in member.roles:
                            try:
                                await member.remove_roles(scrim_role)
                            except Exception:
                                pass
                    for coach in team.get("coaches", []):
                        member = guild.get_member(int(coach["discord_id"]))
                        if not member:
                            continue
                        if spectator_role and spectator_role in member.roles:
                            try:
                                await member.remove_roles(spectator_role)
                            except Exception:
                                pass
                        if game_links_ch:
                            try:
                                await game_links_ch.set_permissions(member, overwrite=None)
                            except Exception:
                                pass
                        if scrim_chat_ch:
                            try:
                                await scrim_chat_ch.set_permissions(member, overwrite=None)
                            except Exception:
                                pass

        clear_tournament(guild.id)
        await ctx.send(f"✅ Tournament deleted. Removed **{deleted}** team channel(s).")

    else:
        await ctx.send(
            "❌ Unknown subcommand!\n"
            "Available: `r!tournament create FORMAT, Title, Desc, Timestamp` · `r!tournament list` · "
            "`r!tournament close` · `r!tournament start` · `r!tournament delete`"
        )


# ── Hook ticket handler into on_message ───────────────────────────────────────
# (Called from the existing on_message event)

async def maybe_handle_ticket(message: discord.Message):
    """Call from on_message to handle active ticket conversations."""
    if message.author.bot:
        return
    if message.channel.id in active_tickets:
        await handle_ticket_message(message)


# ── Slash commands for tournament ─────────────────────────────────────────────

@bot.tree.command(name="tournament_create", description="Create a team scrim event with registration")
@slash_has_role()
@app_commands.describe(
    format="Format e.g. 3v3, 4v4v4, 3v3v3v3, ffa8, ffa16",
    groups="Number of groups (1 = single group, 2 = two groups etc.)",
    substitutes="Reserve players per team (0 if none)",
    title="Event title",
    description="Short description",
    timestamp="Discord timestamp from discordtimestamp.com e.g. <t:1700000000:R>"
)
async def slash_tournament_create(interaction: discord.Interaction, format: str, groups: int, substitutes: int, title: str, description: str, timestamp: str):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await tournament(ctx, subcommand="create", args=f"{format}, {groups}, {substitutes}, {title}, {description}, {timestamp}")

@bot.tree.command(name="tournament_list", description="Show all registered teams")
@slash_has_role()
async def slash_tournament_list(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await tournament(ctx, subcommand="list")

@bot.tree.command(name="tournament_close", description="Close team registrations")
@slash_has_role()
async def slash_tournament_close(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await tournament(ctx, subcommand="close")

@bot.tree.command(name="tournament_start", description="Create private team channels")
@slash_has_role()
async def slash_tournament_start(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await tournament(ctx, subcommand="start")

@bot.tree.command(name="tournament_delete", description="End the tournament and delete all channels")
@slash_has_role()
async def slash_tournament_delete(interaction: discord.Interaction):
    await interaction.response.defer()
    ctx = await commands.Context.from_interaction(interaction)
    await tournament(ctx, subcommand="delete")


bot.run(os.getenv("TOKEN"))
