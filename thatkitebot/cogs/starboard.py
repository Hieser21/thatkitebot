import re

import aioredis
import discord
from discord.ext import commands, bridge

from thatkitebot.cogs.settings import can_change_settings
from thatkitebot.cogs.imagecog import get_image_url


def check_emoji(emoji):
    emoji_regex = r"<\S+:\d+>"
    if len(emoji) == 1:
        return True
    elif re.match(emoji_regex, emoji):
        return True
    else:
        return False


async def generate_embed(message: discord.message, count, star_emoji, return_file=False, aiohttp_session=None):
    embed = discord.Embed(title=f"{message.author.name}",
                          description=f"**Click [here]({message.jump_url}) to Jump to the message**")
    try:
        url, embed_type = await get_image_url(message, video=False, gifv=True)
    except TypeError:
        url, embed_type = None, None

    if url and ("image" in embed_type or embed_type == "rich"):
        embed.set_image(url=url)
    elif url and "video" in embed_type:
        embed.add_field(name="[Video]", value=f"[Content]({url}) is a video which bots cannot display")

    content = message.clean_content or ""
    if content and content != url:
        embed.description += f"\n\n{content}"

    embed.set_thumbnail(url=message.author.avatar.url)
    embed.set_footer(text=f"{count} {star_emoji}")
    embed.color = discord.Color.gold()
    embed.timestamp = message.created_at
    if return_file and aiohttp_session:
        async with aiohttp_session.get(url) as resp:
            file = discord.File(await resp.read(), filename=f"{message.id}.{resp.content_type.split('/')[1]}")
            return embed, file

    return embed


async def set_starboard(redis, channel_id, mode, threshold, emoji, guild_id, channel_list=None):
    redis: aioredis.Redis
    """
    Set the starboard settings for a guild
    """
    key = f"starboard_settings:{guild_id}"
    setdict = {
        "channel_id": channel_id,
        "mode": mode,
        "threshold": threshold,
        "star_emoji": emoji.strip(),
        "channels": ";".join(channel_list) if channel_list else "None"
    }
    await redis.hmset(key, setdict)


async def check_permissions(ctx, channel: discord.TextChannel):
    if not channel.permissions_for(ctx.me).send_messages:
        await ctx.respond("I don't have permission to send messages in that channel.")
        return False
    elif not channel.permissions_for(ctx.me).embed_links:
        await ctx.respond("I don't have permission to embed links in that channel.")
        return False
    elif not channel.permissions_for(ctx.me).manage_messages:
        await ctx.respond("I don't have permission to manage messages in that channel.")
        return False
    return True


async def check_if_already_posted(message: discord.Message, starboard_channel: discord.TextChannel, bot_id: int):
    """
    Check if the message has already been posted to the starboard
    """
    async for starmsg in starboard_channel.history().filter(lambda m: m.embeds and m.author.bot):
        if not starmsg.author == bot_id:
            continue
        if message.jump_url in starmsg.embeds[0].description:
            starboard_message = starmsg  # the starboard message id
            return starboard_message
        else:
            continue

    return None


class StarBoard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.redis: aioredis.Redis = bot.redis

    # read the starboard blacklist from redis
    async def get_blacklist(self, guild_id):
        key = f"starboard_blacklist:{guild_id}"
        return await self.redis.hgetall(key)

    # starboard modes:
    # 1: threshold mode (guild global)
    # 2: top message mode (guild global)
    # 3: threshold mode (channel specific)
    # 4: top message mode (channel specific)

    # starboard channel setting
    # redis: key = "starboard:<channel_id>" values = "mode", "threshold", "message_id" (if in thereshold mode)
    # as well as a list of channels if in channel specific mode (list of channel ids) and the star emoji
    # and the starboard channel
    # example:

    # starboard:424394851170385921
    # >mode = 1
    # >threshold = 5
    # >starboard_channel = 586312434592120851
    # >star_emoji = ⭐
    # >channels = None

    # databases
    # settings: settings redis database
    # messages: message cache redis database with star count

    # the channel specific modes will override the global modes for that channel

    # command for adding a global threshold starboard
    @commands.check(can_change_settings)
    @bridge.bridge_command(name="starboard", aliases=["sb"], description="Set the starboard settings for this guild")
    async def starboard(self, ctx: bridge.BridgeContext, threshold: int, channel: discord.TextChannel, emoji: str):
        if not can_change_settings(ctx):
            return

        if not await check_permissions(ctx, channel):
            return

        if threshold < 1:
            await ctx.respond("The threshold must be at least 1.")
            return

        assert check_emoji(emoji)
        await set_starboard(self.redis, channel.id, 1, threshold, emoji, ctx.guild.id, [])
        await ctx.respond(
            f"Starboard set to threshold mode for {channel.mention} with threshold {threshold} and emoji {emoji}.")

    @commands.check(can_change_settings)
    @bridge.bridge_command(name="starboard_blacklist", aliases=["sbblacklist", "sbb"],
                           description="Set the starboard blacklist for this guild")
    async def starboard_blacklist(self, ctx: bridge.BridgeContext, channel: discord.TextChannel, add: bool = True):
        if not can_change_settings(ctx):
            return
        """
        Add or remove a channel from the blacklist. Blacklisted channels will be ignored by the starboard.
        **Only usable by guild administrators or the bot owner.**
        If the add argument is *True* then the channel will be added to the blacklist.
        If the add argument is *False* then the channel will be removed from the blacklist.
        """
        key = f"starboard_blacklist:{ctx.guild.id}"
        if add:
            await self.redis.sadd(key, channel.id)
            await ctx.respond(f"Added {channel.mention} to the starboard blacklist.")
        else:
            try:
                await self.redis.srem(key, channel.id)
            # notify the user if the channel is not in the blacklist
            except aioredis.ResponseError:
                await ctx.respond(f"{channel.mention} is not in the starboard blacklist.")

            await ctx.respond(f"Removed {channel.mention} from the starboard blacklist.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        channel: discord.TextChannel = await self.bot.fetch_channel(payload.channel_id)
        # check if the channel is blacklisted
        if await self.redis.sismember(f"starboard_blacklist:{channel.guild.id}", channel.id):
            return

        # load the starboard settings
        starboard_settings = await self.redis.hgetall(f"starboard_settings:{payload.guild_id}")
        mode = int(starboard_settings["mode"])
        star_emoji = starboard_settings["star_emoji"]
        threshold = int(starboard_settings["threshold"])
        star_channel = await self.bot.fetch_channel(starboard_settings["channel_id"])
        reaction_emoji = str(payload.emoji)

        # load the message into the internal cache
        message = await channel.fetch_message(payload.message_id)

        # make sure nothing in the starboard channel is starred and put onto the starboard
        if star_channel == channel or not starboard_settings:
            return

        match mode:
            case 1:
                # threshold mode
                # check if the star emoji is the same as the starboard emoji
                if reaction_emoji == star_emoji:
                    # sort the reactions to get the reaction count for the star emoji
                    message.reactions.sort(key=lambda e: str(e) == str(payload.emoji), reverse=True)
                    count = message.reactions[0].count

                    # check if the count is greater than the threshold, else return
                    if not count >= threshold:
                        return

                    already_posted = await check_if_already_posted(message, star_channel)

                    if not already_posted:
                        await star_channel.send(embed=await generate_embed(message, count, star_emoji))
                    else:
                        # update the starboard message
                        await already_posted.edit(embed=await generate_embed(message, count, star_emoji))
                else:
                    return

            case _:
                return


def setup(bot):
    bot.add_cog(StarBoard(bot))
