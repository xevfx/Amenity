import logging
import os

import discord
from discord import app_commands

#import asyncio
from discord.ext import commands

from api.buttons import BotLinks
from api.log import (
    log_app_command_error,
    log_app_command_usage,
    log_command_error,
    log_command_usage,
)

logger = logging.getLogger(__name__)

os.environ["JISHAKU_HIDE"] = "True"
os.environ["JISHAKU_NO_UNDERSCORE"] = "True"
os.environ["JISHAKU_FORCE_PAGINATOR"] = "True"

class Amenity(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = True
        super().__init__(
            command_prefix="",
            intents=intents,
            case_insensitive=True,
            help_command=None,
            owner_id=931347423773741097,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


    async def on_connect(self) -> None:
        """Called when bot connects to Discord gateway."""
        await self.change_presence(
            status=discord.Status.idle,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="/help"
            )
        )

    async def setup_hook(self) -> None:
        try:
            await self.load_extension("jishaku")
            await self.load_extension("cogs.reminder")
        except Exception as e:
            logger.error(f"Failed to load jishaku: {e}")


        # guild_id: int = os.getenv("GUILD_ID")
        # if guild_id:
        #     guild = discord.Object(id=int(guild_id))
        #     self.tree.copy_global_to(guild=guild)
        #     await self.tree.sync(guild=guild)
        # else:
        await self.tree.sync()

    async def on_ready(self) -> None:
        # if not self.user:
        #     return
        # app_info = await self.application_info()
        # install_scope = (
        #     "users"
        #     if app_info.install_params and app_info.install_params.scopes
        #     else "unknown"
        # )
        # print(
        #     f"Logged in as {self.user} (ID: {self.user.id}) | install scope: {install_scope}"
        # )
        logger.info(f"[+] | LOGGED IN AS {self.user}")
        #logger.info(f"[+] | WATCHING {self.users}")


    async def on_command_error(
        self,
        context: commands.Context,
        exception: Exception,
    ) -> None:
        if isinstance(exception, commands.CommandNotFound):
            return

        if isinstance(exception, commands.CommandOnCooldown):
            await context.reply(
                f"Command on cooldown. Try again after {exception.retry_after:.2f} seconds.",
                ephemeral=True,
                mention_author=False,
                delete_after=5
            )
            return

        if isinstance(exception, commands.BadArgument):
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.NoPrivateMessage):
            await context.reply(
                "This command can only be used in a server.",
                ephemeral=True,
                mention_author=False,
                delete_after=5
            )
            return
        if isinstance(exception, commands.MissingRequiredArgument):
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.CheckFailure):
            await context.reply(
                "You don't have permission to use this command.",
                ephemeral=True,
                mention_author=False,
                delete_after=5
            )
            return

        if isinstance(exception, commands.UserInputError):
            await context.send_help(context.command)
            return

        if isinstance(exception, commands.MaxConcurrencyReached):
            await context.reply(
                "This command is currently being used by too many people. Please try again later.",
                ephemeral=True,
                mention_author=False,
                delete_after=5
            )
            return
        await log_command_error(context, exception)
        raise exception

    async def on_command_completion(self, context: commands.Context) -> None:
        await log_command_usage(context)

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.AppCommand,
    ) -> None:
        await log_app_command_usage(interaction, command)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        exception: Exception,
    ) -> None:
        # Helper to send response (handles both response and followup)
        async def send_error(
            embed: discord.Embed,
            ephemeral: bool = True,
            view: discord.ui.View | None = None,
        ) -> None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        embed=embed,
                        ephemeral=ephemeral,
                        view=view,
                    )
                else:
                    await interaction.response.send_message(
                        embed=embed,
                        ephemeral=ephemeral,
                        view=view,
                    )
            except discord.HTTPException:
                pass  # Interaction may have expired


        if isinstance(exception, app_commands.CommandOnCooldown):
            embed = discord.Embed(
                description=(
                    "Command on cooldown. Try again after "
                    f"{exception.retry_after:.2f} seconds."
                ),
                color=discord.Color.red()
            )
            await send_error(embed, ephemeral=True)
            return

        if isinstance(exception, app_commands.TransformerError):
            embed = discord.Embed(
                description="Invalid argument provided. Please check your input.",
                color=discord.Color.red()
            )
            await send_error(embed, ephemeral=True)
            return

        if isinstance(exception, app_commands.NoPrivateMessage):
            embed = discord.Embed(
                description="This command can only be used in a server.",
                color=discord.Color.red()
            )
            await send_error(embed, ephemeral=True)
            return

        if isinstance(exception, app_commands.CheckFailure):
            embed = discord.Embed(
                description="You don't have permission to use this command.",
                color=discord.Color.red()
            )
            await send_error(embed, ephemeral=True)
            return

        embed = discord.Embed(
            description="An unexpected error occurred. Please try again later.",
            color=discord.Color.red()
        )
        await send_error(embed, ephemeral=True, view=BotLinks().support())
        await log_app_command_error(interaction, exception)
        raise exception


    async def invoke_help_command(self, ctx: commands.Context) -> None:
        """Send help for the current command."""
        await ctx.send_help(ctx.command)
