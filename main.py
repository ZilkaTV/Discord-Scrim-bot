import os
import json
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

CHANNEL_ID = 1466912142530969650
ROLE_ID = 1466913367380726004
MENTION_ROLES = [1467057562108039250, 1467057940409352377]
 
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


@bot.command()
async def create(ctx, *, args):
    parts = args.split(",")
    name = parts[0].strip()
    timestamp = parts[1].strip()
    description = parts[2].strip()

    embed = discord.Embed(title=name, description=description)
    embed.add_field(name="Datum", value=f"<t:{timestamp}:F>", inline=False)
    channel = bot.get_channel(CHANNEL_ID)
    mentions = " ".join(f"<@&{r}>" for r in MENTION_ROLES) or None
    msg = await channel.send(content=mentions, embed=embed)
    
    await msg.add_reaction("✅")

    message_ids = load_message_ids()
    message_ids.add(msg.id)
    save_message_ids(message_ids)
    print(f"Created message {msg.id}, now tracking {len(message_ids)} message(s)")


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
