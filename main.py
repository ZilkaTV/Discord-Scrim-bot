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
MENTION_ROLES = [1467057562108039250, 1467057940409352377]
SCRIM_CHAT_ID = 1466915521420329204
EVENT_CHANNEL_ID = 1467091170176929968
GAME_LINKS_ID = 1466911935395266641
LEADERBOARD_CHANNEL_ID = 1466915479661842725

IDS_FILE = "message_ids.json"
LEADERBOARD_FILE = "leaderboard.json"

warned_events = set()

bot = commands.Bot(command_prefix="r!", intents=intents)


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


@tasks.loop(minutes=1)
async def check_events():
    now = datetime.now(tz=timezone.utc)
    for guild in bot.guilds:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.scheduled:
                diff = (event.start_time - now).total_seconds()

                # 30 minute warning
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

                # Auto start event
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
    if args.strip().lower() != "event":
        await ctx.send("❌ Wrong format! Use: `r!delete event`")
        return

    await ctx.send("⏳ Deleting event, messages and roles...")

    guild = ctx.guild
    role = guild.get_role(ROLE_ID)
    register_channel = bot.get_channel(CHANNEL_ID)
    scrim_channel = bot.get_channel(SCRIM_CHAT_ID)
    game_links_channel = bot.get_channel(GAME_LINKS_ID)

    # Find active event (optional - continue even if not found)
    active_event = None
    try:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.active:
                active_event = event
                break
    except Exception as e:
        await ctx.send(f"⚠️ Could not check for active event: `{e}` - continuing cleanup...")

    # Remove roles only from members not signed up for other events
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
        await ctx.send("❌ I don't have permission to remove roles!")
        return
    except Exception as e:
        await ctx.send(f"❌ Error syncing roles: `{e}`")
        return

    # Delete only the message of the active event
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
        await ctx.send(f"❌ Error deleting register messages: `{e}`")
        return

    # Clear scrim chat
    try:
        await clear_channel(scrim_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing scrim chat: `{e}`")

    # Clear game links channel
    try:
        await clear_channel(game_links_channel)
    except Exception as e:
        await ctx.send(f"❌ Error clearing game links: `{e}`")

    # End active event if found
    if active_event:
        try:
            await active_event.end()
            print("Event ended")
        except Exception as e:
            await ctx.send(f"⚠️ Could not end event: `{e}`")

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
    parts = [p.strip() for p in args.split(" ", 1)]
    subcommand = parts[0].lower()

    # r!event update
    if subcommand == "update":
        await ctx.send("⏳ Checking voice channels and updating reactions...")

        guild = ctx.guild
        role = guild.get_role(ROLE_ID)
        register_channel = bot.get_channel(CHANNEL_ID)

        # Get all members currently in any voice channel
        members_in_voice = set()
        for vc in guild.voice_channels:
            for member in vc.members:
                members_in_voice.add(member.id)

        if not members_in_voice:
            await ctx.send("❌ No members found in any voice channel!")
            return

        data = load_data()
        message_ids = get_all_message_ids(data)
        reacted_ids = await get_all_reacted_ids(register_channel, message_ids)

        removed = []
        for user_id in reacted_ids:
            if user_id not in members_in_voice:
                member = guild.get_member(user_id)
                if member is None:
                    continue
                for msg_id in message_ids:
                    try:
                        msg = await register_channel.fetch_message(msg_id)
                        await msg.remove_reaction("✅", member)
                    except Exception:
                        pass
                if role in member.roles:
                    try:
                        await member.remove_roles(role)
                        removed.append(member.display_name)
                    except Exception:
                        pass

        if removed:
            await ctx.send(f"✅ Update complete! Removed **{len(removed)}** player(s) not in voice:\n" + ", ".join(removed))
        else:
            await ctx.send("✅ Update complete! All signed up players are in voice.")

    # r!event leaderboard
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
