import os
import json
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

CHANNEL_ID = 1466912142530969650
ROLE_ID = 1466913367380726004
ACTIVE_ROLE_ID = 1474720238695219220
SPECTATOR_ROLE_ID = 1475139183147225240          # NEW: Spectator Scrim role
MENTION_ROLES = [1467057562108039250, 1467057940409352377]
SCRIM_CHAT_ID = 1466915521420329204
EVENT_CHANNEL_ID = 1467091170176929968           # Meeting Point channel
GAME_LINKS_ID = 1466911935395266641
LEADERBOARD_CHANNEL_ID = 1466915479661842725

IDS_FILE = "message_ids.json"
LEADERBOARD_FILE = "leaderboard.json"
STATS_FILE = "stats.json"

warned_events = set()
scrim_active = False        # NEW: True once r!event update was used
manually_deleting = False   # NEW: True while r!delete event is running

bot = commands.Bot(command_prefix="r!", intents=intents)


# ─── File helpers ────────────────────────────────────────────────────────────

def load_data() -> dict:
    if os.path.exists(IDS_FILE):
        with open(IDS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_data(data: dict):
    with open(IDS_FILE, "w") as f:
        json.dump(data, f)


def get_all_message_ids(data: dict) -> set:
    return set(data.values())


def load_leaderboard() -> dict:
    if os.path.exists(LEADERBOARD_FILE):
        with open(LEADERBOARD_FILE, "r") as f:
            return json.load(f)
    return {}


def save_leaderboard(data: dict):
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(data, f)


def load_stats() -> dict:
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_stats(data: dict):
    with open(STATS_FILE, "w") as f:
        json.dump(data, f)


def get_or_create_stats(stats: dict, user_id: str) -> dict:
    if user_id not in stats:
        stats[user_id] = {"registered": 0, "attended": 0}
    return stats[user_id]


# ─── Role / channel helpers ───────────────────────────────────────────────────

async def clear_channel(channel):
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


async def get_all_reacted_ids(channel, message_ids: set) -> set:
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
    for user_id in reacted_ids:
        member = guild.get_member(user_id)
        if member and role not in member.roles:
            await member.add_roles(role)
    for member in role.members:
        if member.id not in reacted_ids:
            await member.remove_roles(role)


async def remove_active_role_all(guild):
    active_role = guild.get_role(ACTIVE_ROLE_ID)
    if active_role:
        for member in list(active_role.members):
            try:
                await member.remove_roles(active_role)
            except Exception as e:
                print(f"Error removing active role from {member.display_name}: {e}")


async def remove_spectator_role_all(guild):
    spectator_role = guild.get_role(SPECTATOR_ROLE_ID)
    if spectator_role:
        for member in list(spectator_role.members):
            try:
                await member.remove_roles(spectator_role)
            except Exception as e:
                print(f"Error removing spectator role from {member.display_name}: {e}")


# ─── NEW: Core VC role update logic (used by both update command & auto-loop) ─

async def update_scrim_vc_roles(guild):
    """
    - Members in Meeting Point (EVENT_CHANNEL_ID) → Spectator Scrim role
    - Members in any other VC → Active Scrim role
    - Members who left all VCs → both roles removed
    """
    active_role = guild.get_role(ACTIVE_ROLE_ID)
    spectator_role = guild.get_role(SPECTATOR_ROLE_ID)

    if not active_role or not spectator_role:
        print("Active or Spectator role not found!")
        return

    members_in_meeting_point = set()
    members_in_other_vc = set()

    for vc in guild.voice_channels:
        for member in vc.members:
            if member.bot:
                continue
            if vc.id == EVENT_CHANNEL_ID:
                members_in_meeting_point.add(member.id)
            else:
                members_in_other_vc.add(member.id)

    all_in_vc = members_in_meeting_point | members_in_other_vc

    # Meeting Point members → Spectator, no Active
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

    # Other VC members → Active, no Spectator
    for member_id in members_in_other_vc:
        member = guild.get_member(member_id)
        if member:
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

    # Remove roles from anyone who left all VCs
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
        f"Other VCs: {len(members_in_other_vc)} active players"
    )


# ─── NEW: Auto VC check task ──────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def scrim_vc_check():
    if not scrim_active:
        return
    for guild in bot.guilds:
        await update_scrim_vc_roles(guild)


@scrim_vc_check.before_loop
async def before_scrim_vc_check():
    await bot.wait_until_ready()


# ─── Bot ready ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print("Bot ready")
    channel = bot.get_channel(CHANNEL_ID)
    guild = channel.guild
    role = guild.get_role(ROLE_ID)
    data = load_data()
    message_ids = get_all_message_ids(data)
    reacted_ids = await get_all_reacted_ids(channel, message_ids)
    await sync_roles(guild, role, reacted_ids)
    print(f"Roles synced across {len(message_ids)} active message(s)")
    check_events.start()
    scrim_vc_check.start()


# ─── 30-min warning + auto-start loop ────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_events():
    now = datetime.now(tz=timezone.utc)
    for guild in bot.guilds:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.scheduled:
                diff = (event.start_time - now).total_seconds()

                if 1740 <= diff <= 1800 and event.id not in warned_events:
                    try:
                        channel = bot.get_channel(CHANNEL_ID)
                        role = guild.get_role(ROLE_ID)
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

                if -60 <= diff <= 0:
                    try:
                        await event.start()
                        print(f"Event {event.name} started!")
                        channel = bot.get_channel(CHANNEL_ID)
                        role = guild.get_role(ROLE_ID)
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
    await bot.wait_until_ready()


# ─── NEW: Prevent Discord from auto-ending the event when VC empties ─────────

@bot.event
async def on_scheduled_event_update(before, after):
    """
    Discord automatically ends a voice-channel event when the last person leaves.
    If that happens and the scrim is still active (no r!delete was called),
    we recreate and immediately start a new event so the scrim continues.
    """
    global manually_deleting

    if manually_deleting:
        return  # Intentional deletion – don't restart

    if after.status not in (discord.EventStatus.ended, discord.EventStatus.completed):
        return

    if not scrim_active:
        return

    data = load_data()
    if str(after.id) not in data:
        return  # Not a tracked scrim event

    print(f"Event '{after.name}' was auto-ended by Discord – restarting it...")

    guild = after.guild
    try:
        new_event = await guild.create_scheduled_event(
            name=after.name,
            description=after.description or "",
            start_time=datetime.now(tz=timezone.utc),
            channel=guild.get_channel(EVENT_CHANNEL_ID),
            entity_type=discord.EntityType.voice,
            privacy_level=discord.PrivacyLevel.guild_only
        )
        await new_event.start()

        # Re-link the original registration message to the new event
        old_msg_id = data.pop(str(after.id))
        data[str(new_event.id)] = old_msg_id
        save_data(data)

        print(f"Event restarted as '{new_event.name}' (id: {new_event.id})")
    except Exception as e:
        print(f"Error restarting event: {e}")


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.command()
async def create(ctx, *, args):
    parts = [p.strip() for p in args.split(",")]
    if len(parts) < 3:
        await ctx.send("❌ Wrong format! Use: `r!create Title, Description, <t:TIMESTAMP:R>`")
        return

    title = parts[0]
    description = parts[1]

    try:
        raw = parts[2].strip("<>").replace("t:", "").split(":")[0]
        timestamp = int(raw)
    except ValueError:
        await ctx.send("❌ Invalid timestamp!")
        return

    guild = ctx.guild
    event_channel = guild.get_channel(EVENT_CHANNEL_ID)

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
    channel = bot.get_channel(CHANNEL_ID)
    mentions = " ".join(f"<@&{r}>" for r in MENTION_ROLES)

    embed = discord.Embed(title=title, description=description, url=event_link)
    embed.add_field(name="Date", value=f"<t:{timestamp}:F>", inline=False)
    embed.add_field(name="Event", value=f"[Click here]({event_link})", inline=False)

    try:
        msg = await channel.send(content=mentions, embed=embed)
        await msg.add_reaction("✅")
    except Exception as e:
        await ctx.send(f"❌ Event created but message could not be posted: `{e}`")
        return

    data = load_data()
    data[str(event.id)] = msg.id
    save_data(data)

    await ctx.send(f"✅ Event **{title}** successfully created and posted! 🎉\n{event_link}")
    print(f"Created event {event.id} and message {msg.id}")


@bot.command()
async def delete(ctx, *, args):
    global manually_deleting, scrim_active

    if args.strip().lower() != "event":
        await ctx.send("❌ Wrong format! Use: `r!delete event`")
        return

    manually_deleting = True  # Signal: don't restart the event
    scrim_active = False       # Stop the auto VC check loop

    await ctx.send("⏳ Deleting event, messages and roles...")

    guild = ctx.guild
    role = guild.get_role(ROLE_ID)
    register_channel = bot.get_channel(CHANNEL_ID)
    scrim_channel = bot.get_channel(SCRIM_CHAT_ID)
    game_links_channel = bot.get_channel(GAME_LINKS_ID)

    active_event = None
    try:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.active:
                active_event = event
                break
    except Exception as e:
        await ctx.send(f"⚠️ Could not check for active event: `{e}` - continuing cleanup...")

    await remove_active_role_all(guild)
    await remove_spectator_role_all(guild)

    try:
        data = load_data()
        if active_event:
            event_id_str = str(active_event.id)
            if event_id_str in data:
                del data[event_id_str]
        remaining_message_ids = get_all_message_ids(data)
        reacted_ids = await get_all_reacted_ids(register_channel, remaining_message_ids)
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

    try:
        data = load_data()
        if active_event:
            event_id_str = str(active_event.id)
            if event_id_str in data:
                msg_id = data[event_id_str]
                try:
                    msg = await register_channel.fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    pass
                del data[event_id_str]
                save_data(data)

        remaining_message_ids = get_all_message_ids(data)
        async for message in register_channel.history(limit=100):
            if message.author == bot.user and message.id not in remaining_message_ids:
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
        print("Register channel messages deleted")
    except Exception as e:
        manually_deleting = False
        await ctx.send(f"❌ Error deleting register messages: `{e}`")
        return

    try:
        await clear_channel(scrim_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing scrim chat: `{e}`")

    try:
        await clear_channel(game_links_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing game links: `{e}`")

    if active_event:
        try:
            await active_event.end()
            print("Event ended")
        except Exception as e:
            await ctx.send(f"⚠️ Could not end event: `{e}`")

    manually_deleting = False  # Reset flag

    await ctx.send("✅ Cleanup complete!\n- 🗑️ Messages deleted\n- 👥 Roles updated\n- 🧹 Scrim chat cleared\n- 🔗 Game links cleared")


@bot.command()
async def cancel(ctx, *, args):
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

    guild = ctx.guild
    role = guild.get_role(ROLE_ID)
    register_channel = bot.get_channel(CHANNEL_ID)

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
        print(f"Event {target_event.name} cancelled!")
    except Exception as e:
        await ctx.send(f"❌ Error cancelling event: `{e}`")
        return

    try:
        data = load_data()
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
        reacted_ids = await get_all_reacted_ids(register_channel, remaining_message_ids)
        await sync_roles(guild, role, reacted_ids)
        print("Roles synced after cancellation")
    except Exception as e:
        await ctx.send(f"⚠️ Event cancelled but error syncing roles: `{e}`")
        return

    await ctx.send(f"✅ Event **{target_event.name}** has been cancelled!\n- 🗑️ Message deleted\n- 👥 Roles updated")


@bot.command()
async def event(ctx, *, args):
    global scrim_active

    parts = [p.strip() for p in args.split(" ", 1)]
    subcommand = parts[0].lower()

    if subcommand == "update":
        await ctx.send("⏳ Checking voice channels and assigning roles...")

        guild = ctx.guild
        active_role = guild.get_role(ACTIVE_ROLE_ID)
        spectator_role = guild.get_role(SPECTATOR_ROLE_ID)
        register_channel = bot.get_channel(CHANNEL_ID)

        if active_role is None:
            await ctx.send("❌ Active Scrim role not found!")
            return
        if spectator_role is None:
            await ctx.send("❌ Spectator Scrim role not found!")
            return

        # Count members per zone for the stats block
        data = load_data()
        message_ids = get_all_message_ids(data)
        reacted_ids = await get_all_reacted_ids(register_channel, message_ids)

        members_in_meeting_point = set()
        members_in_other_vc = set()
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                if vc.id == EVENT_CHANNEL_ID:
                    members_in_meeting_point.add(member.id)
                else:
                    members_in_other_vc.add(member.id)

        # Track attendance in stats (only registered players)
        stats = load_stats()
        all_in_vc = members_in_meeting_point | members_in_other_vc
        for user_id in reacted_ids:
            uid_str = str(user_id)
            user_stats = get_or_create_stats(stats, uid_str)
            user_stats["registered"] += 1
            if user_id in all_in_vc:
                user_stats["attended"] += 1
        save_stats(stats)

        # Assign roles
        await update_scrim_vc_roles(guild)

        # Activate the auto-check loop
        scrim_active = True

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

        guild = ctx.guild
        game_links_channel = bot.get_channel(GAME_LINKS_ID)
        leaderboard_channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)

        leaderboard = load_leaderboard()
        games_found = 0

        async for message in game_links_channel.history(limit=200):
            content_lower = message.content.lower()
            if "winner" in content_lower and message.mentions:
                for member in message.mentions:
                    if not member.bot:
                        uid = str(member.id)
                        leaderboard[uid] = leaderboard.get(uid, 0) + 1
                games_found += 1

        if games_found == 0:
            await ctx.send("❌ No winner messages found in game-links channel!")
            return

        save_leaderboard(leaderboard)

        sorted_lb = sorted(leaderboard.items(), key=lambda x: x[1], reverse=True)

        medals = ["🥇", "🥈", "🥉"]
        description = ""
        for i, (user_id, points) in enumerate(sorted_lb):
            member = guild.get_member(int(user_id))
            name = member.mention if member else f"<@{user_id}>"
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

        try:
            async for old_msg in leaderboard_channel.history(limit=20):
                if old_msg.author == bot.user:
                    await old_msg.delete()
        except Exception as e:
            await ctx.send(f"⚠️ Could not delete old leaderboard: `{e}`")

        scrim_news_role = guild.get_role(MENTION_ROLES[1])
        mention_content = scrim_news_role.mention if scrim_news_role else ""

        await leaderboard_channel.send(content=mention_content, embed=embed)
        await ctx.send(f"✅ Leaderboard updated! Found **{games_found}** game(s) with winners.")

    else:
        await ctx.send("❌ Unknown subcommand! Available: `r!event update`, `r!event leaderboard`")


@bot.command()
async def stats(ctx, *, args=None):
    guild = ctx.guild
    stats = load_stats()
    leaderboard = load_leaderboard()

    if args and args.strip().lower() == "top":
        if not stats:
            await ctx.send("❌ No stats available yet!")
            return

        sorted_stats = []
        for uid, s in stats.items():
            rate = (s["attended"] / s["registered"] * 100) if s["registered"] > 0 else 0
            sorted_stats.append((uid, s, rate))
        sorted_stats.sort(key=lambda x: x[2], reverse=True)

        description = ""
        for i, (uid, s, rate) in enumerate(sorted_stats[:10]):
            member = guild.get_member(int(uid))
            name = member.display_name if member else f"<@{uid}>"
            points = leaderboard.get(uid, 0)
            description += f"**{i+1}.** {name} — {rate:.0f}% attendance ({s['attended']}/{s['registered']}) | {points} pts\n"

        embed = discord.Embed(
            title="🏅 Top 10 - Attendance Rate",
            description=description,
            color=discord.Color.blue()
        )
        await ctx.send(embed=embed)
        return

    if ctx.message.mentions:
        target = ctx.message.mentions[0]
    else:
        target = ctx.author

    uid_str = str(target.id)
    user_stats = stats.get(uid_str, {"registered": 0, "attended": 0})
    points = leaderboard.get(uid_str, 0)

    registered = user_stats["registered"]
    attended = user_stats["attended"]
    rate = (attended / registered * 100) if registered > 0 else 0

    if rate >= 80:
        rate_emoji = "🟢"
    elif rate >= 50:
        rate_emoji = "🟡"
    else:
        rate_emoji = "🔴"

    embed = discord.Embed(
        title=f"📊 Stats - {target.display_name}",
        color=discord.Color.blue()
    )
    embed.add_field(name="🏆 Points", value=str(points), inline=True)
    embed.add_field(name="📋 Registered", value=str(registered), inline=True)
    embed.add_field(name="✅ Attended", value=str(attended), inline=True)
    embed.add_field(name=f"{rate_emoji} Attendance Rate", value=f"{rate:.0f}%", inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)

    await ctx.send(embed=embed)


# ─── Reaction events ──────────────────────────────────────────────────────────

@bot.event
async def on_raw_reaction_add(payload):
    data = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return
    if str(payload.emoji) != "✅":
        return
    guild = bot.get_guild(payload.guild_id)
    role = guild.get_role(ROLE_ID)
    member = guild.get_member(payload.user_id)
    if member and not member.bot:
        await member.add_roles(role)


@bot.event
async def on_raw_reaction_remove(payload):
    data = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return
    if str(payload.emoji) != "✅":
        return
    guild = bot.get_guild(payload.guild_id)
    role = guild.get_role(ROLE_ID)
    channel = bot.get_channel(CHANNEL_ID)
    member = guild.get_member(payload.user_id)
    if member is None or member.bot:
        return
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


@bot.event
async def on_raw_message_delete(payload):
    data = load_data()
    message_ids = get_all_message_ids(data)
    if payload.message_id not in message_ids:
        return
    print(f"Tracked message {payload.message_id} was deleted, resyncing roles...")
    for event_id, msg_id in list(data.items()):
        if msg_id == payload.message_id:
            del data[event_id]
            break
    save_data(data)
    channel = bot.get_channel(CHANNEL_ID)
    guild = channel.guild
    role = guild.get_role(ROLE_ID)
    remaining_message_ids = get_all_message_ids(data)
    reacted_ids = await get_all_reacted_ids(channel, remaining_message_ids)
    await sync_roles(guild, role, reacted_ids)
    print(f"Roles resynced, now tracking {len(remaining_message_ids)} message(s)")


bot.run(os.getenv("TOKEN"))
