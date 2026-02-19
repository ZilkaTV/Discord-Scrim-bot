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

IDS_FILE = "message_ids.json"

bot = commands.Bot(command_prefix="r!", intents=intents)


def load_message_ids() -> set:
    if os.path.exists(IDS_FILE):
        with open(IDS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_message_ids(ids: set):
    with open(IDS_FILE, "w") as f:
        json.dump(list(ids), f)


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
            save_message_ids(message_ids)
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
    message_ids = load_message_ids()
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
                if -60 <= diff <= 0:
                    try:
                        await event.start()
                        print(f"Event {event.name} wurde gestartet!")
                        channel = bot.get_channel(CHANNEL_ID)
                        role = guild.get_role(ROLE_ID)
                        event_link = f"https://discord.com/events/{guild.id}/{event.id}"
                        embed = discord.Embed(
                            title=f"🟢 {event.name} hat begonnen!",
                            description=f"Das Event **{event.name}** ist jetzt gestartet!\n[Zum Event]({event_link})",
                            color=discord.Color.green()
                        )
                        await channel.send(content=f"{role.mention}", embed=embed)
                    except Exception as e:
                        print(f"Fehler beim Starten von {event.name}: {e}")


@check_events.before_loop
async def before_check():
    await bot.wait_until_ready()


@bot.command()
async def create(ctx, *, args):
    parts = [p.strip() for p in args.split(",")]
    if len(parts) < 3:
        await ctx.send("❌ Falsches Format! Benutze: `r!create Titel, Beschreibung, <t:TIMESTAMP:R>`")
        return

    title = parts[0]
    description = parts[1]

    try:
        raw = parts[2].strip("<>").replace("t:", "").split(":")[0]
        timestamp = int(raw)
    except ValueError:
        await ctx.send("❌ Der Timestamp ist ungültig!")
        return

    guild = ctx.guild
    event_channel = guild.get_channel(EVENT_CHANNEL_ID)

    if event_channel is None:
        await ctx.send("❌ Meeting Point Channel nicht gefunden!")
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
        await ctx.send("❌ Ich habe keine Berechtigung Events zu erstellen!")
        return
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Erstellen des Events: `{e}`")
        return

    event_link = f"https://discord.com/events/{guild.id}/{event.id}"
    channel = bot.get_channel(CHANNEL_ID)
    mentions = " ".join(f"<@&{r}>" for r in MENTION_ROLES)

    embed = discord.Embed(title=title, description=description, url=event_link)
    embed.add_field(name="Datum", value=f"<t:{timestamp}:F>", inline=False)
    embed.add_field(name="Event", value=f"[Hier klicken]({event_link})", inline=False)

    try:
        msg = await channel.send(content=mentions, embed=embed)
        await msg.add_reaction("✅")
    except Exception as e:
        await ctx.send(f"❌ Event erstellt aber Nachricht konnte nicht gepostet werden: `{e}`")
        return

    message_ids = load_message_ids()
    message_ids.add(msg.id)
    save_message_ids(message_ids)

    await ctx.send(f"✅ Event **{title}** wurde erfolgreich erstellt und gepostet! 🎉\n{event_link}")
    print(f"Created event {event.id} and message {msg.id}")


@bot.command()
async def delete(ctx, *, args):
    if args.strip().lower() != "event":
        await ctx.send("❌ Falsches Format! Benutze: `r!delete event`")
        return

    await ctx.send("⏳ Lösche Event, Nachrichten und Rollen...")

    guild = ctx.guild
    role = guild.get_role(ROLE_ID)
    register_channel = bot.get_channel(CHANNEL_ID)
    scrim_channel = bot.get_channel(SCRIM_CHAT_ID)

    try:
        for member in list(role.members):
            await member.remove_roles(role)
        print("Alle Rollen entfernt")
    except discord.Forbidden:
        await ctx.send("❌ Ich habe keine Berechtigung Rollen zu entfernen!")
        return
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Entfernen der Rollen: `{e}`")
        return

    try:
        message_ids = load_message_ids()
        for msg_id in list(message_ids):
            try:
                msg = await register_channel.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound:
                pass
        save_message_ids(set())
        async for message in register_channel.history(limit=100):
            if message.author == bot.user and message.id not in message_ids:
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
        print("Register Channel Nachrichten gelöscht")
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Löschen der Register-Nachrichten: `{e}`")
        return

    try:
        deleted = await scrim_channel.purge(limit=500)
        print(f"{len(deleted)} Nachrichten im Scrim Chat gelöscht")
    except discord.Forbidden:
        await ctx.send("❌ Ich habe keine Berechtigung Nachrichten im Scrim Chat zu löschen!")
        return
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Löschen des Scrim Chats: `{e}`")
        return

    try:
        events = await guild.fetch_scheduled_events()
        for event in events:
            if event.status == discord.EventStatus.active:
                await event.end()
        print("Event beendet")
    except Exception as e:
        await ctx.send(f"⚠️ Nachrichten und Rollen gelöscht, aber Event konnte nicht beendet werden: `{e}`")
        return

    await ctx.send("✅ Event erfolgreich beendet!\n- 🗑️ Nachrichten gelöscht\n- 👥 Rollen entfernt\n- 🧹 Scrim Chat geleert")


@bot.event
async def on_raw_reaction_add(payload):
    message_ids = load_message_ids()
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
    message_ids = load_message_ids()
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
    message_ids = load_message_ids()
    if payload.message_id not in message_ids:
        return
    print(f"Tracked message {payload.message_id} was deleted, resyncing roles...")
    message_ids.discard(payload.message_id)
    save_message_ids(message_ids)
    channel = bot.get_channel(CHANNEL_ID)
    guild = channel.guild
    role = guild.get_role(ROLE_ID)
    reacted_ids = await get_all_reacted_ids(channel, message_ids)
    await sync_roles(guild, role, reacted_ids)
    print(f"Roles resynced, now tracking {len(message_ids)} message(s)")


bot.run(os.getenv("TOKEN"))
