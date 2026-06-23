import discord
from discord import app_commands
from discord.ext import commands

from api.emojis import Emoji
from core.amenity import Amenity


class ServerUtility(commands.Cog):
    display_name = "Server Utility"
    group_name = "Utilities"

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot

    def _get_server_embed(self, ctx: commands.Context, guild: discord.Guild) -> discord.Embed:
        # Determine if bot is in guild
        is_bot_in_guild = guild.me is not None if guild else False

        embed = discord.Embed(title=guild.name, color=0x00FFFF)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        created_timestamp = int(guild.created_at.timestamp())
        created_str = f"<t:{created_timestamp}:F> (<t:{created_timestamp}:R>)"

        # General Info
        embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
        if guild.owner_id:
            embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Created At", value=created_str, inline=False)

        if is_bot_in_guild:
            # Full info is available
            # Members
            if self.bot.intents.members:
                total_members = guild.member_count or len(guild.members)
                bots = len([m for m in guild.members if m.bot])
                humans = total_members - bots
                embed.add_field(
                    name="Members",
                    value=(f"👥 **Total**: {total_members:,}\n👤 **Humans**: {humans:,}\n🤖 **Bots**: {bots:,}"),
                    inline=True,
                )
            else:
                total_members = guild.member_count or len(guild.members)
                if total_members:
                    embed.add_field(
                        name="Members",
                        value=f"👥 **Total**: {total_members:,}",
                        inline=True,
                    )

            # Channels
            chan_lines = []
            categories = len(guild.categories)
            text_chans = len(guild.text_channels)
            voice_chans = len(guild.voice_channels)
            threads = len(guild.threads)
            total_channels = len(guild.channels)
            if categories:
                chan_lines.append(f"{Emoji.FILE.value} **Categories**: {categories}")
            if text_chans:
                chan_lines.append(f"💬 **Text**: {text_chans}")
            if voice_chans:
                chan_lines.append(f"🔊 **Voice**: {voice_chans}")
            if threads:
                chan_lines.append(f"🧵 **Threads**: {threads}")
            if total_channels:
                chan_lines.append(f"📊 **Total**: {total_channels}")
            if chan_lines:
                embed.add_field(
                    name="Channels & Threads",
                    value="\n".join(chan_lines),
                    inline=True,
                )

            # Security & Boosts
            sec_lines = []
            verification = guild.verification_level
            if verification is not None:
                sec_lines.append(f"🔒 **Verification**: {str(verification).title()}")
            boosts = guild.premium_subscription_count
            tier = guild.premium_tier
            if boosts:
                sec_lines.append(f"💎 **Boosts**: {boosts} (Level {tier})")
            if sec_lines:
                embed.add_field(
                    name="Security & Boosts",
                    value="\n".join(sec_lines),
                    inline=True,
                )

            # Roles & Assets
            asset_lines = []
            roles_count = len(guild.roles)
            emojis_count = len(guild.emojis)
            stickers_count = len(guild.stickers)
            if roles_count:
                asset_lines.append(f"🛡️ **Roles**: {roles_count}")
            if emojis_count:
                asset_lines.append(f"😀 **Emojis**: {emojis_count}")
            if stickers_count:
                asset_lines.append(f"🏷️ **Stickers**: {stickers_count}")
            if asset_lines:
                embed.add_field(
                    name="Roles & Assets",
                    value="\n".join(asset_lines),
                    inline=True,
                )
        else:
            # User App Mode
            member_count = getattr(guild, "member_count", None)
            if member_count is not None:
                embed.add_field(name="Members", value=f"👥 **Total**: {member_count:,}", inline=True)

            features = guild.features
            if features:
                # Format features nicely
                feat_list = [f.replace("_", " ").title() for f in features[:5]]
                feat_str = ", ".join(feat_list)
                if len(features) > 5:
                    feat_str += f" and {len(features) - 5} more..."
                embed.add_field(name="Features", value=feat_str, inline=True)

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        return embed

    @commands.hybrid_group(name="server", description="Server utility commands", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def server_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @server_group.command(name="info", description="Get information about the server.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def server_info(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.reply(
                embed=discord.Embed(
                    description="This command can only be used in a server.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        c_at = int(ctx.guild.created_at.timestamp())

        guild: discord.Guild = ctx.guild

        embed = (
            discord.Embed(color=discord.Color.dark_magenta())
            .set_author(
                name=f"{Emoji.HOUSE.value} Server info:",
                icon_url=guild.me.display_avatar.url if guild.icon is None else guild.icon.url,
            )
            .set_footer(
                text=f"Requested By {ctx.author}",
                icon_url=ctx.author.avatar.url if ctx.author.avatar else ctx.author.default_avatar.url,
            )
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
            embed.timestamp = discord.utils.utcnow()

        embed.add_field(
            name="**__About__**",
            value=f"""
            **Name : ** {"N/A" if not guild.name else guild.name}\n
            **ID :** {guild.id}\n**Created At : ** <t:{c_at}:F>
            """,
            inline=False,
        )
        ftrs = ""
        if guild.features:
            ftrs = ("\n").join([f"> {feature.replace('_', ' ').title()}" for feature in guild.features])

        embed.add_field(
            name="**__Features__**",
            value=f"{ftrs if ftrs and len(ftrs) <= 1024 else (ftrs[0:1000] + 'and more...' if ftrs else 'None')}",
        )

        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_group(name="role", description="Role utility commands", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def role_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @role_group.command(name="info", description="Get details of a specific role.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(role="The role to fetch information for.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def role_info(self, ctx: commands.Context, role: discord.Role) -> None:
        if ctx.guild is None:
            await ctx.reply(
                embed=discord.Embed(
                    description="This command can only be used in a server.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"{Emoji.SEARCH.value} Role Info: @{role.name}",
            color=role.color if role.color.value != 0 else 0x00FFFF,
        )
        created_timestamp = int(role.created_at.timestamp())
        created_str = f"<t:{created_timestamp}:F> (<t:{created_timestamp}:R>)"

        embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
        if role.color.value != 0:
            embed.add_field(name="Hex Code", value=f"`{str(role.color).upper()}`", inline=True)
        embed.add_field(name="Created At", value=created_str, inline=False)

        if role.mentionable:
            embed.add_field(name="Mentionable", value="Yes", inline=True)
        if role.hoist:
            embed.add_field(name="Hoisted (Separated)", value="Yes", inline=True)
        embed.add_field(name="Position", value=str(role.position), inline=True)

        # Key permissions
        enabled_perms = [perm_name.replace("_", " ").title() for perm_name, enabled in role.permissions if enabled]
        if enabled_perms:
            perms_str = ", ".join(enabled_perms[:10])
            if len(enabled_perms) > 10:
                perms_str += f" and {len(enabled_perms) - 10} more..."
            embed.add_field(name="Key Permissions", value=perms_str, inline=False)

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_group(name="member", description="Member utility commands", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def member_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @member_group.command(name="info", description="Get information about a specific member.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(user="The member to fetch information for.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def member_info(self, ctx: commands.Context, user: discord.Member = None) -> None:
        # Default to author
        target = user or ctx.author

        # Try to promote User to Member if in a guild
        if ctx.guild and isinstance(target, discord.User):
            member = ctx.guild.get_member(target.id)
            if member is not None:
                target = member

        embed = discord.Embed(title=f"User Info: {target}", color=0x00FFFF)
        embed.set_thumbnail(url=target.display_avatar.url)

        created_timestamp = int(target.created_at.timestamp())
        created_str = f"<t:{created_timestamp}:F> (<t:{created_timestamp}:R>)"

        embed.add_field(name="Username", value=f"`{target.name}`", inline=True)
        embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
        if target.bot:
            embed.add_field(name="Bot", value="Yes", inline=True)
        embed.add_field(name="Account Created At", value=created_str, inline=False)

        # If target is a Member (we are in a guild and target is present in the guild)
        if isinstance(target, discord.Member):
            if target.nick:
                embed.add_field(name="Nickname", value=f"`{target.nick}`", inline=True)

            if target.joined_at:
                joined_timestamp = int(target.joined_at.timestamp())
                joined_str = f"<t:{joined_timestamp}:F> (<t:{joined_timestamp}:R>)"
                embed.add_field(name="Joined Server", value=joined_str, inline=False)

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    @commands.hybrid_group(name="channel", description="Channel utility commands", invoke_without_command=True)
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def channel_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @channel_group.command(name="info", description="Get information about a specific channel.")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    @app_commands.describe(channel="The channel to fetch information for.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def channel_info(
        self,
        ctx: commands.Context,
        channel: discord.abc.GuildChannel | discord.Thread = None,
    ) -> None:
        target = channel or ctx.channel

        if isinstance(target, (discord.DMChannel, discord.GroupChannel)):
            embed = discord.Embed(title="Channel Info: DM Channel", color=0x00FFFF)
            embed.add_field(name="Channel ID", value=f"`{target.id}`", inline=True)
            embed.add_field(name="Channel Type", value=str(target.type).upper(), inline=True)
            created_timestamp = int(target.created_at.timestamp())
            created_str = f"<t:{created_timestamp}:F> (<t:{created_timestamp}:R>)"
            embed.add_field(name="Created At", value=created_str, inline=False)

            embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
            await ctx.reply(embed=embed, mention_author=False)
            return

        # Guild channel
        embed = discord.Embed(title=f"Channel Info: #{target.name}", color=0x00FFFF)

        created_timestamp = int(target.created_at.timestamp())
        created_str = f"<t:{created_timestamp}:F> (<t:{created_timestamp}:R>)"

        embed.add_field(name="Channel ID", value=f"`{target.id}`", inline=True)

        # Determine channel type string
        chan_type = "Unknown"
        if isinstance(target, discord.TextChannel):
            chan_type = "Text Channel"
        elif isinstance(target, discord.VoiceChannel):
            chan_type = "Voice Channel"
        elif isinstance(target, discord.CategoryChannel):
            chan_type = "Category"
        elif isinstance(target, discord.StageChannel):
            chan_type = "Stage Channel"
        elif isinstance(target, discord.Thread):
            chan_type = "Thread"
        elif isinstance(target, discord.ForumChannel):
            chan_type = "Forum"

        embed.add_field(name="Channel Type", value=chan_type, inline=True)
        embed.add_field(name="Mention", value=target.mention, inline=True)
        embed.add_field(name="Created At", value=created_str, inline=False)

        # Category Parent
        if hasattr(target, "category") and target.category:
            embed.add_field(name="Category", value=f"`{target.category.name}`", inline=True)

        # Position
        if hasattr(target, "position") and target.position is not None:
            embed.add_field(name="Position", value=str(target.position), inline=True)

        # Topic
        if isinstance(target, discord.TextChannel) and target.topic:
            embed.add_field(name="Topic", value=target.topic, inline=False)

        # NSFW
        if isinstance(target, discord.TextChannel) and target.nsfw:
            embed.add_field(name="NSFW", value="Yes", inline=True)

        # Slowmode
        if isinstance(target, discord.TextChannel) and target.slowmode_delay is not None and target.slowmode_delay > 0:
            embed.add_field(
                name="Slowmode Delay",
                value=f"{target.slowmode_delay} seconds",
                inline=True,
            )

        # Bitrate / User Limit (Voice)
        if isinstance(target, discord.VoiceChannel):
            if target.bitrate is not None and target.bitrate > 0:
                embed.add_field(name="Bitrate", value=f"{target.bitrate // 1000} kbps", inline=True)
            if target.user_limit is not None and target.user_limit > 0:
                embed.add_field(
                    name="User Limit",
                    value=str(target.user_limit),
                    inline=True,
                )

        # Thread specifics
        if isinstance(target, discord.Thread):
            if target.parent:
                embed.add_field(name="Parent Channel", value=target.parent.mention, inline=True)
            if target.owner_id:
                embed.add_field(name="Owner", value=f"<@{target.owner_id}>", inline=True)
            if target.message_count is not None:
                embed.add_field(name="Messages Count", value=str(target.message_count), inline=True)

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(ServerUtility(bot))
