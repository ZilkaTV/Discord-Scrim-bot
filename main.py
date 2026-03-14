import os
import json
import discord
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

ALLOWED_ROLES    = [1466913409340543027, 1466913296597909682]  # Staff, Host – required for all commands except r!stats

IDS_FILE         = "message_ids.json"   # Maps event ID → registration message ID
LEADERBOARD_FILE = "leaderboard.json"   # Maps user ID → win count (legacy, kept for leaderboard cmd)
STATS_FILE       = "stats.json"         # Maps user ID → full stats dict
GUILD_CONFIGS_FILE = "guild_configs.json"  # Per-guild configuration (channels, roles)


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
    """Returns a command check that passes only if the user has Staff or Host role (per guild config)."""
    async def predicate(ctx):
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
    "channel_id":             CHANNEL_ID,
    "role_id":                ROLE_ID,
    "active_role_id":         ACTIVE_ROLE_ID,
    "spectator_role_id":      SPECTATOR_ROLE_ID,
    "mention_roles":          MENTION_ROLES,
    "scrim_chat_id":          SCRIM_CHAT_ID,
    "event_channel_id":       EVENT_CHANNEL_ID,
    "game_links_id":          GAME_LINKS_ID,
    "leaderboard_channel_id": LEADERBOARD_CHANNEL_ID,
    "allowed_roles":          ALLOWED_ROLES,
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
                "leaderboard_channel_id", "allowed_roles"]
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


def load_leaderboard() -> dict:
    """Load leaderboard.json → {user_id: win_count}. Returns {} if file doesn't exist."""
    if os.path.exists(LEADERBOARD_FILE):
        with open(LEADERBOARD_FILE, "r") as f:
            return json.load(f)
    return {}


def save_leaderboard(data: dict):
    """Write the given dict to leaderboard.json."""
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(data, f)


def load_stats() -> dict:
    """Load stats.json → {user_id: stats_dict}. Returns {} if file doesn't exist."""
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_stats(data: dict):
    """Write the given dict to stats.json."""
    with open(STATS_FILE, "w") as f:
        json.dump(data, f)


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
        print("Active, Spectator or Scrim registration role not found!")
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
        print(f"[log_game] No participants tracked yet, skipping ({source})")
        return None

    stats      = load_stats()
    leaderboard = load_leaderboard()

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

    save_stats(stats)
    save_leaderboard(leaderboard)

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
    for guild in bot.guilds:
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

    winner_names = await log_game(message.guild, winner_ids, source="auto")
    if winner_names is not None:
        await message.add_reaction("✅")  # Confirm the game was recorded
        print(f"[auto] Game recorded from game-links post by {message.author.display_name}")


# ─── Command: r!setup ────────────────────────────────────────────────────────
# Interactive setup wizard for new servers.
# Walks through all required channels and roles step by step.
# Usage: r!setup

SETUP_STEPS = [
    ("channel_id",             "📋 **Registration Channel**\nMention the channel where event posts go (e.g. #register-for-scrim)"),
    ("scrim_chat_id",          "💬 **Scrim Chat Channel**\nMention the channel that gets cleared after each scrim (e.g. #scrim-chat)"),
    ("game_links_id",          "🔗 **Game Links Channel**\nMention the channel where winner messages are posted (e.g. #game-links)"),
    ("leaderboard_channel_id", "🏆 **Leaderboard Channel**\nMention the channel where the leaderboard embed gets posted (e.g. #scrim-leaderboard)"),
    ("event_channel_id",       "🔊 **Meeting Point Voice Channel**\nMention the voice channel that the bot joins to keep the event alive (e.g. #Meeting Point)\n> Tip: use `#` and start typing the voice channel name"),
    ("role_id",                "✅ **Scrim Registration Role**\nMention the role players receive when they react ✅ (e.g. @Scrim Player)"),
    ("active_role_id",         "🎮 **Active Scrim Role**\nMention the role given to players in game VCs (e.g. @Active Scrim)"),
    ("spectator_role_id",      "👁️ **Spectator Role**\nMention the role given to players in the Meeting Point or unregistered players (e.g. @Spectator Scrim)"),
    ("mention_roles",          "📣 **Ping Roles** (for event announcements)\nMention ALL roles that should be pinged when an event is created (e.g. @Scrim News @Everyone)\n> Separate multiple roles with spaces"),
    ("allowed_roles",          "🔒 **Staff/Host Roles**\nMention ALL roles that are allowed to use bot commands (e.g. @Staff @Host)\n> Separate multiple roles with spaces"),
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
        "channel_id":             "Registration Channel",
        "scrim_chat_id":          "Scrim Chat",
        "game_links_id":          "Game Links",
        "leaderboard_channel_id": "Leaderboard",
        "event_channel_id":       "Meeting Point VC",
        "role_id":                "Scrim Registration Role",
        "active_role_id":         "Active Scrim Role",
        "spectator_role_id":      "Spectator Role",
        "mention_roles":          "Ping Roles",
        "allowed_roles":          "Staff/Host Roles",
    }
    for key, label in key_labels.items():
        val = cfg.get(key, "_(using default)_")
        if isinstance(val, list):
            mentions = " ".join(f"<@&{v}>" for v in val)
            lines.append(f"**{label}:** {mentions}")
        elif key.endswith("_id") and isinstance(val, int):
            if "channel" in key or key == "event_channel_id":
                lines.append(f"**{label}:** <#{val}>")
            else:
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

    stats = load_stats()
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
        total_games = load_stats().get(str(next(iter(winner_ids))), {}).get("games_won", "?")
        streak_parts = []
        for uid in winner_ids:
            uid_str    = str(uid)
            user_stats = load_stats().get(uid_str, {})
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

        stats = load_stats()
        for user_id in reacted_ids:
            uid_str    = str(user_id)
            user_stats = get_or_create_stats(stats, uid_str)
            user_stats["registered"] += 1
            if user_id in all_in_vc:
                user_stats["attended"] += 1
        save_stats(stats)

        # Assign Active / Spectator roles based on current VC positions
        # (also populates current_game_participants with players in game VCs)
        await update_scrim_vc_roles(guild)

        # Bot joins Meeting Point so Discord never auto-ends the event due to empty VC
        await join_meeting_point(guild)

        # Activate the per-minute auto-check for the rest of the scrim
        scrim_active = True

        # Build a readable summary for the confirmation message
        active_names = [
            guild.get_member(uid).display_name
            for uid in members_in_other_vc
            if guild.get_member(uid) and not guild.get_member(uid).bot
        ]
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

        await ctx.send("\n".join(lines))

    elif subcommand == "leaderboard":
        await ctx.send("⏳ Scanning game links and updating leaderboard...")

        guild               = ctx.guild
        game_links_channel  = bot.get_channel(get_cfg(guild.id, "game_links_id"))
        leaderboard_channel = bot.get_channel(get_cfg(guild.id, "leaderboard_channel_id"))

        leaderboard = load_leaderboard()
        games_found = 0

        # Count wins: any message containing "winner" + a user mention counts as one game
        async for message in game_links_channel.history(limit=200):
            content_lower = message.content.lower()
            if "winner" in content_lower and message.mentions:
                for member in message.mentions:
                    if not member.bot:
                        uid = str(member.id)
                        leaderboard[uid] = leaderboard.get(uid, 0) + 1
                games_found += 1

        if games_found == 0:
            await ctx.send(
                "⚠️ No winner messages found in game-links channel!\n"
                "Who won? Use `r!winner USER_ID` to manually add a win.\n"
                "Example: `r!winner 123456789012345678`"
            )
            return

        save_leaderboard(leaderboard)

        sorted_lb   = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)
        medals      = ["🥇", "🥈", "🥉"]
        description = ""
        for i, (user_id, points) in enumerate(sorted_lb):
            member = guild.get_member(int(user_id))
            name   = member.mention if member else f"<@{user_id}>"
            if i < 3:
                prefix = medals[i]
            elif points >= 3:
                prefix = "🏅"
            else:
                prefix = "▪️"
            description += f"{prefix} {name} **{points} Point{'s' if points != 1 else ''}**\n"

        embed = discord.Embed(
            title="Scrim - Leaderboard 🏆",
            description=description,
            color=discord.Color.gold()
        )

        # Delete the previous leaderboard embed before posting a fresh one
        try:
            async for old_msg in leaderboard_channel.history(limit=20):
                if old_msg.author == bot.user:
                    await old_msg.delete()
        except Exception as e:
            await ctx.send(f"⚠️ Could not delete old leaderboard: `{e}`")

        scrim_news_role = guild.get_role(get_cfg(guild.id, "mention_roles")[1])
        mention_content = scrim_news_role.mention if scrim_news_role else ""

        await leaderboard_channel.send(content=mention_content, embed=embed)
        await ctx.send(f"✅ Leaderboard updated! Found **{games_found}** game(s) with winners.")

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
    stats       = load_stats()
    leaderboard = load_leaderboard()
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

    save_stats(stats)
    save_leaderboard(leaderboard)

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
    stats       = load_stats()
    leaderboard = load_leaderboard()
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

    save_stats(stats)
    save_leaderboard(leaderboard)

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
        title="[SCRIM] Bot — Info",
        description=(
            "The SCRIM Bot manages competitive scrim sessions on this server.\n\n"
            "**What it does:**\n"
            "- Creates and manages Discord scheduled events for scrims\n"
            "- Posts registration messages — players react with ✅ to sign up\n"
            "- Automatically assigns **Active Scrim** and **Spectator** roles based on voice channel activity\n"
            "- Tracks game results and win streaks for every player\n"
            "- Maintains a live leaderboard with points and stats\n"
            "- Sends a 30-minute warning before each event starts\n"
            "- Stays in the Meeting Point voice channel to keep the event alive\n"
            "- Cleans up all messages, roles and events at the end of a scrim\n\n"
            "**Prefix:** `r!`\n"
            "**Commands:** Use `r!cmd` for a full list\n"
            "**Permissions:** Most commands require the **Staff** or **Host** role"
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="SCRIM Bot • Made for competitive scrim management")
    await ctx.send(embed=embed)


# ─── Command: r!cmd ──────────────────────────────────────────────────────────
# Lists all commands with a short description.
# Usage: r!cmd

@bot.command()
async def cmd(ctx):
    embed = discord.Embed(
        title="[SCRIM] Bot — Commands",
        color=discord.Color.blurple()
    )

    embed.add_field(name="📅 Event Management", value=(
        "`r!create` — Create an event & registration post\n"
        "`r!cancel event, ID` — Cancel a scheduled event\n"
        "`r!delete event` — End scrim & full cleanup"
    ), inline=False)

    embed.add_field(name="🔊 Voice Channel", value=(
        "`r!join` — Bot joins Meeting Point VC\n"
        "`r!leave` — Bot leaves voice channel"
    ), inline=False)

    embed.add_field(name="🎮 Scrim Session", value=(
        "`r!event update` — Assign roles, start auto-check\n"
        "`r!event leaderboard` — Post updated leaderboard\n"
        "`r!stopupdate` — Stop auto-check & remove scrim roles"
    ), inline=False)

    embed.add_field(name="🏆 Game Tracking", value=(
        "`r!game winner @A @B` — Log a game result manually\n"
        "`r!winner @A @B` — Add wins manually (mentions or IDs)\n"
        "`r!removewins @Player [amount]` — Remove wins from a player"
    ), inline=False)

    embed.add_field(name="📊 Stats", value=(
        "`r!stats` — Your own stats\n"
        "`r!stats @Player` — Another player's stats\n"
        "`r!stats top` — Top 10 by attendance rate"
    ), inline=False)

    embed.add_field(name="ℹ️ General", value=(
        "`r!info` — About this bot\n"
        "`r!cmd` — This command list"
    ), inline=False)

    embed.add_field(name="🔄 Workflow", value=(
        "`r!create` → `r!join` → `r!event update` → log games → `r!event leaderboard` → `r!delete event`"
    ), inline=False)

    embed.set_footer(text="Staff & Host only • except r!stats, r!info, r!cmd")
    await ctx.send(embed=embed)


# ─── Command: r!stats ────────────────────────────────────────────────────────
# Shows full stats for a player including game tracking fields.
# Usage:
#   r!stats           → own stats
#   r!stats @player   → stats for another player
#   r!stats top       → top 10 by attendance rate

@bot.command()
async def stats(ctx, *, args=None):
    guild       = ctx.guild
    stats       = load_stats()
    leaderboard = load_leaderboard()

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

    channel               = bot.get_channel(get_cfg(guild.id, "channel_id"))
    guild                 = channel.guild
    role                  = guild.get_role(get_cfg(guild.id, "role_id"))
    remaining_message_ids = get_all_message_ids(data)
    reacted_ids           = await get_all_reacted_ids(channel, remaining_message_ids)
    await sync_roles(guild, role, reacted_ids)
    print(f"Roles resynced, now tracking {len(remaining_message_ids)} message(s)")


# ─── Run Bot ──────────────────────────────────────────────────────────────────
# TOKEN is read from the environment variable to keep it out of the source code.
# Set it with: export TOKEN=your_bot_token  (or via your hosting platform's secrets)

bot.run(os.getenv("TOKEN"))
