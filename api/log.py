import asyncio
import os
import traceback
from datetime import datetime

import aiohttp
import discord
from discord import Webhook, app_commands
from discord.ext import commands


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


async def _send_exception_webhook(exception: Exception, hook: str | None = None) -> None:
    try:
        if hook is None:
            hook = os.getenv("ERROR_HOOK")
        if not hook:
            return

        error = getattr(exception, "original", exception)
        traceback_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        traceback_text = _truncate_text(traceback_text, 3800)

        embed = discord.Embed(
            color=discord.Color.red(),
            timestamp=datetime.now(),
            description=f"```py\n{traceback_text}\n```" if traceback_text else None,
        )
        embed.add_field(
            name="Error",
            value=_truncate_text(f"{type(error).__name__}: {error}", 1024),
            inline=False,
        )

        async with aiohttp.ClientSession() as session:
            webhook = Webhook.from_url(hook, session=session)
            await webhook.send(embed=embed)
    except Exception:
        return


def log_exception(exception: Exception) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_send_exception_webhook(exception))


async def UsageWebhook(embed: discord.Embed, hook: str | None = None) -> bool:
    """
    Sends a discord.Embed object to a Discord channel via a webhook.

    Args:
        embed (discord.Embed): The embed object to send.

    Returns:
        bool: True if the log was sent successfully, False otherwise.
    """
    if not isinstance(embed, discord.Embed):
        return False

    if hook is None:
        hook = os.getenv("USAGE_HOOK")
    if not hook:
        return False

    try:
        async with aiohttp.ClientSession() as session:
            webhook = Webhook.from_url(hook, session=session)
            await webhook.send(embed=embed)
        return True

    except Exception as e:
        log_exception(e)
        return False


async def ErrorWebhook(embed: discord.Embed, hook: str | None = None) -> bool:
    """
    Sends a discord.Embed object to a Discord channel via a webhook.

    Args:
        embed (discord.Embed): The embed object to send.

    Returns:
        bool: True if the log was sent successfully, False otherwise.
    """
    if not isinstance(embed, discord.Embed):
        return False

    if hook is None:
        hook = os.getenv("ERROR_HOOK")
    if not hook:
        return False

    try:
        async with aiohttp.ClientSession() as session:
            webhook = Webhook.from_url(hook, session=session)
            await webhook.send(embed=embed)
        return True

    except Exception as e:
        log_exception(e)
        return False


async def log_command_usage(ctx: commands.Context) -> None:
    """
    Log prefix command usage for on_command_completion event.

    Args:
        ctx: Discord context object
    """
    try:
        embed = discord.Embed(color=discord.Color.magenta(), timestamp=datetime.now())
        if ctx.author.avatar:
            embed.set_author(name=f"{ctx.author} | {ctx.author.id}", icon_url=ctx.author.avatar.url)
        else:
            embed.set_author(name=f"{ctx.author} | {ctx.author.id}")

        embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild and ctx.guild.icon else None)

        embed.add_field(name="Command", value=str(ctx.command), inline=False)
        embed.add_field(
            name="Server",
            value=f"{ctx.guild.name} [`{ctx.guild.id}`]" if ctx.guild else "DM",
            inline=False,
        )
        embed.add_field(name="Author", value=f"{ctx.author} [`{ctx.author.id}`]", inline=False)
        embed.add_field(
            name="Subcommand Called",
            value=str(ctx.invoked_subcommand) if ctx.invoked_subcommand else "None",
            inline=True,
        )

        await UsageWebhook(embed)

    except Exception as e:
        log_exception(e)


async def log_command_error(ctx: commands.Context, exception: Exception) -> None:
    """
    Log prefix command errors and tracebacks for on_command_error event.

    Args:
        ctx: Discord context object
        exception: Exception raised by the command
    """
    try:
        error = getattr(exception, "original", exception)
        traceback_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        traceback_text = _truncate_text(traceback_text, 3800)

        embed = discord.Embed(
            color=discord.Color.red(),
            timestamp=datetime.now(),
            description=f"```py\n{traceback_text}\n```" if traceback_text else None,
        )
        if ctx.author.avatar:
            embed.set_author(
                name=f"{ctx.author} | {ctx.author.id}",
                icon_url=ctx.author.avatar.url,
            )
        else:
            embed.set_author(name=f"{ctx.author} | {ctx.author.id}")

        embed.set_thumbnail(url=ctx.guild.icon.url if ctx.guild and ctx.guild.icon else None)

        embed.add_field(name="Command", value=str(ctx.command), inline=False)
        embed.add_field(
            name="Server",
            value=f"{ctx.guild.name} [`{ctx.guild.id}`]" if ctx.guild else "DM",
            inline=False,
        )
        embed.add_field(
            name="Channel",
            value=f"{ctx.channel} [`{ctx.channel.id}`]" if ctx.guild else "DM",
            inline=False,
        )
        embed.add_field(
            name="Author",
            value=f"{ctx.author} [`{ctx.author.id}`]",
            inline=False,
        )
        embed.add_field(
            name="Error",
            value=_truncate_text(f"{type(error).__name__}: {error}", 1024),
            inline=False,
        )

        await ErrorWebhook(embed)

    except Exception as e:
        log_exception(e)


def _get_app_command_name(command: app_commands.Command | None) -> str:
    if command is None:
        return "Unknown"
    name = getattr(command, "qualified_name", None)
    return name or str(command)


async def log_app_command_usage(interaction: discord.Interaction, command: app_commands.Command | None) -> None:
    """
    Log app command usage for on_app_command_completion event.

    Args:
        interaction: Discord interaction object
        command: App command object
    """
    try:
        user = interaction.user
        channel = interaction.channel

        embed = discord.Embed(
            color=discord.Color.magenta(),
            timestamp=datetime.now(),
        )
        if user and user.avatar:
            embed.set_author(
                name=f"{user} | {user.id}",
                icon_url=user.avatar.url,
            )
        elif user:
            embed.set_author(name=f"{user} | {user.id}")

        if interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        embed.add_field(
            name="App Command",
            value=_get_app_command_name(command),
            inline=False,
        )
        embed.add_field(
            name="Server",
            value=(f"{interaction.guild.name} [`{interaction.guild.id}`]" if interaction.guild else "DM"),
            inline=False,
        )
        embed.add_field(
            name="Channel",
            value=(f"{channel} [`{channel.id}`]" if channel and interaction.guild else "DM"),
            inline=False,
        )
        if user:
            embed.add_field(
                name="Author",
                value=f"{user} [`{user.id}`]",
                inline=False,
            )

        await UsageWebhook(embed)

    except Exception as e:
        log_exception(e)


async def log_app_command_error(interaction: discord.Interaction, exception: Exception) -> None:
    """
    Log app command errors and tracebacks for on_app_command_error event.

    Args:
        interaction: Discord interaction object
        exception: Exception raised by the command
    """
    try:
        error = getattr(exception, "original", exception)
        traceback_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        traceback_text = _truncate_text(traceback_text, 3800)

        user = interaction.user
        channel = interaction.channel
        command = getattr(interaction, "command", None)

        embed = discord.Embed(
            color=discord.Color.red(),
            timestamp=datetime.now(),
            description=f"```py\n{traceback_text}\n```" if traceback_text else None,
        )
        if user and user.avatar:
            embed.set_author(
                name=f"{user} | {user.id}",
                icon_url=user.avatar.url,
            )
        elif user:
            embed.set_author(name=f"{user} | {user.id}")

        if interaction.guild and interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        embed.add_field(
            name="App Command",
            value=_get_app_command_name(command),
            inline=False,
        )
        embed.add_field(
            name="Server",
            value=(f"{interaction.guild.name} [`{interaction.guild.id}`]" if interaction.guild else "DM"),
            inline=False,
        )
        embed.add_field(
            name="Channel",
            value=(f"{channel} [`{channel.id}`]" if channel and interaction.guild else "DM"),
            inline=False,
        )
        if user:
            embed.add_field(
                name="Author",
                value=f"{user} [`{user.id}`]",
                inline=False,
            )
        embed.add_field(
            name="Error",
            value=_truncate_text(f"{type(error).__name__}: {error}", 1024),
            inline=False,
        )

        await ErrorWebhook(embed)

    except Exception as e:
        log_exception(e)
