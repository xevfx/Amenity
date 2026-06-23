from __future__ import annotations

import inspect
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from api.emojis import Emoji
from core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

HELP_CACHE_KEY = "help:commands"
HELP_CACHE_TTL = 300


@dataclass(frozen=True)
class HelpCommandInfo:
    name: str
    description: str
    cog_name: str
    cog_display_name: str
    aliases: Sequence[str]
    is_group: bool


def _normalize_description(description: str | None) -> str:
    if not description:
        return "No description provided."
    return description.strip()


def _cog_display_name(cog: commands.Cog | None) -> str:
    if not cog:
        return "Uncategorized"
    return getattr(cog, "display_name", cog.qualified_name or "Uncategorized")


def _cog_group_name(cog: commands.Cog | None) -> str:
    if not cog:
        return "other"
    group_name = getattr(cog, "group_name", None)
    if group_name:
        return str(group_name).strip() or "other"
    if cog.__module__ and cog.__module__.startswith("cogs."):
        return cog.__module__.split(".", 1)[1].replace("_", " ")
    return cog.qualified_name or "other"


def _is_group(command: commands.Command) -> bool:
    return bool(getattr(command, "commands", None))


def _get_command_description(command: commands.Command) -> str:
    desc = command.help or command.brief or command.description or command.short_doc or inspect.getdoc(command.callback)
    return _normalize_description(desc)


def _is_help_command(command: commands.Command) -> bool:
    return command.qualified_name == "help" or command.name == "help"


def _build_signature(prefix: str, command: commands.Command) -> str:
    signature = f"{prefix}{command.qualified_name}"
    for name, param in command.params.items():
        if name in ("self", "ctx"):
            continue
        if param.default != param.empty:
            signature += f" [{name}]"
        else:
            signature += f" <{name}>"
    return signature


def _format_params(command: commands.Command) -> list[str]:
    params: list[str] = []
    for name, param in command.params.items():
        if name in ("self", "ctx"):
            continue
        desc = _describe_param(param)
        if param.default != param.empty:
            desc = f"{desc} (optional)" if param.default is None else f"{desc} (default: {param.default})"
        params.append(f"`{name}` - {desc}")
    return params


async def _build_command_embed(
    *,
    command: commands.Command,
    index: HelpIndex,
    author: discord.abc.User,
    prefix: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=command.qualified_name,
        description=_get_command_description(command),
        color=0x2F3136,
    )

    if command.parent:
        embed.add_field(
            name="Parent",
            value=f"`{command.parent.qualified_name}`",
            inline=True,
        )

    if hasattr(command, "usage") and command.usage:
        usage = f"{prefix}{command.qualified_name} {command.usage}"
    else:
        usage = _build_signature(prefix, command)
    embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

    if command.aliases:
        embed.add_field(
            name="Aliases",
            value=", ".join(f"`{alias}`" for alias in command.aliases),
            inline=True,
        )

    if command.cog:
        embed.add_field(
            name="Group",
            value=_cog_group_name(command.cog),
            inline=True,
        )
        embed.add_field(
            name="Category",
            value=_cog_display_name(command.cog),
            inline=True,
        )

    if _is_group(command):
        visible_subcommands = []
        for sub in sorted(command.commands, key=lambda c: c.name):
            if await index.can_see_command(sub, author):
                visible_subcommands.append(f"`{sub.name}` - {_get_command_description(sub)}")
        if visible_subcommands:
            embed.add_field(
                name="Subcommands",
                value="\n".join(visible_subcommands[:10]),
                inline=False,
            )

    params = _format_params(command)
    if params:
        embed.add_field(name="Parameters", value="\n".join(params), inline=False)

    embed.set_footer(text="<> required, [] optional")
    return embed


class HelpIndex:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_command_count = 0

    async def is_owner(self, user: discord.abc.User) -> bool:
        return await self.bot.is_owner(user)

    async def can_see_cog(self, cog: commands.Cog, user: discord.abc.User) -> bool:
        if not hasattr(cog, "__module__") or not cog.__module__.startswith("cogs."):
            return False
        if getattr(cog, "hidden", False):
            return False
        if getattr(cog, "owner_only", False):
            return await self.is_owner(user)
        return True

    async def can_see_command(self, command: commands.Command, user: discord.abc.User) -> bool:
        if _is_help_command(command):
            return False
        if getattr(command, "hidden", False):
            return False
        if getattr(command, "owner_only", False):
            return await self.is_owner(user)
        if command.cog:
            return await self.can_see_cog(command.cog, user)
        return True

    async def visible_cogs(
        self,
        user: discord.abc.User,
    ) -> dict[commands.Cog, list[commands.Command]]:
        result: dict[commands.Cog, list[commands.Command]] = {}
        for cog in self.bot.cogs.values():
            if not await self.can_see_cog(cog, user):
                continue
            commands_list = [cmd for cmd in cog.walk_commands() if await self.can_see_command(cmd, user)]
            if commands_list:
                result[cog] = sorted(commands_list, key=lambda c: c.qualified_name)
        return result

    async def visible_uncategorized(self, user: discord.abc.User) -> list[commands.Command]:
        result: list[commands.Command] = []
        for cmd in self.bot.commands:
            if cmd.cog is None and await self.can_see_command(cmd, user):
                result.append(cmd)
        return sorted(result, key=lambda c: c.qualified_name)

    async def build_cache(self) -> None:
        items: list[HelpCommandInfo] = []
        for cmd in self.bot.walk_commands():
            if _is_help_command(cmd):
                continue
            cog = cmd.cog
            items.append(
                HelpCommandInfo(
                    name=cmd.qualified_name,
                    description=_get_command_description(cmd),
                    cog_name=cog.qualified_name if cog else "Uncategorized",
                    cog_display_name=_cog_display_name(cog),
                    aliases=tuple(cmd.aliases),
                    is_group=_is_group(cmd),
                )
            )
        cache.set(HELP_CACHE_KEY, items, ttl=HELP_CACHE_TTL)
        self._last_command_count = len(items)

    def get_cached(self) -> list[HelpCommandInfo]:
        cached = cache.get(HELP_CACHE_KEY)
        if cached is None:
            return []
        return list(cached)

    async def refresh_cache_if_needed(self) -> None:
        cached = self.get_cached()
        if not cached or len(list(self.bot.walk_commands())) != self._last_command_count:
            await self.build_cache()


class HelpView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: commands.Bot,
        index: HelpIndex,
        author: discord.abc.User,
        prefix: str,
        mapping: dict[commands.Cog, list[commands.Command]],
        uncategorized: list[commands.Command],
        message: discord.Message | None = None,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.index = index
        self.author = author
        self.prefix = prefix
        self.mapping = mapping
        self.uncategorized = uncategorized
        self.message = message
        self.current_cog: commands.Cog | None = None
        self.current_page = 0
        self.total_pages = 0
        self.mode = "home"
        self._update_components()

    def _update_components(self) -> None:
        self.clear_items()
        options = []
        for cog in self.mapping:
            options.append(
                discord.SelectOption(
                    label=_cog_display_name(cog),
                    value=cog.qualified_name,
                    description=f"{len(self.mapping[cog])} commands",
                )
            )
        if self.uncategorized:
            options.append(
                discord.SelectOption(
                    label="Uncategorized",
                    value="__uncategorized__",
                    description=f"{len(self.uncategorized)} commands",
                )
            )
        if options:
            self.add_item(HelpCategorySelect(self, options[:25]))

        if self.mode != "home":
            self.add_item(HelpHomeButton(self))
        if self.total_pages > 1:
            self.add_item(HelpPrevButton(self))
            self.add_item(HelpPageIndicator(self))
            self.add_item(HelpNextButton(self))
        self.add_item(HelpSearchButton(self))
        self.add_item(HelpCloseButton(self))

    async def create_home_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=f"{Emoji.CROWN.value} Amenity Commands",
            description="Select a category from the dropdown to view commands.",
            color=0x2F3136,
        )

        grouped: dict[str, list[str]] = {}
        for cog, commands_list in self.mapping.items():
            group = _cog_group_name(cog)
            grouped.setdefault(group, []).append(f"{_cog_display_name(cog)} ({len(commands_list)})")
        if self.uncategorized:
            grouped.setdefault("other", []).append(f"Uncategorized ({len(self.uncategorized)})")

        for group_name in sorted(grouped.keys()):
            label = group_name.replace("_", " ").title()
            embed.add_field(
                name=label,
                value="\n".join(sorted(grouped[group_name])),
                inline=False,
            )

        embed.add_field(
            name="Usage",
            value=f"Use `{self.prefix}help <command>` for details.",
            inline=False,
        )
        return embed

    async def create_cog_embed(self, cog: commands.Cog | None) -> discord.Embed:
        if cog is None:
            commands_list = self.uncategorized
            title = "Uncategorized"
            description = "Commands without a category."
        else:
            commands_list = self.mapping.get(cog, [])
            title = _cog_display_name(cog)
            description = _normalize_description(getattr(cog, "description", None))

        embed = discord.Embed(
            title=f"{Emoji.COMMAND.value} {title} Commands",
            description=description,
            color=0x2F3136,
        )

        if not commands_list:
            embed.add_field(
                name="No Commands",
                value="No commands are currently available in this category.",
                inline=False,
            )
            return embed

        commands_list = sorted(commands_list, key=lambda c: c.qualified_name)
        per_page = 18
        self.total_pages = (len(commands_list) + per_page - 1) // per_page
        start = self.current_page * per_page
        end = start + per_page
        page_commands = commands_list[start:end]
        lines = []
        for cmd in page_commands:
            suffix = " (group)" if _is_group(cmd) else ""
            lines.append(f"`{cmd.qualified_name}`{suffix}")
        embed.add_field(name="Commands", value=", ".join(lines), inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.total_pages}")
        return embed

    async def create_command_embed(self, command: commands.Command) -> discord.Embed:
        return await _build_command_embed(
            command=command,
            index=self.index,
            author=self.author,
            prefix=self.prefix,
        )

    async def show_home(self, interaction: discord.Interaction) -> None:
        self.current_cog = None
        self.current_page = 0
        self.total_pages = 0
        self.mode = "home"
        embed = await self.create_home_embed()
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def show_cog(self, interaction: discord.Interaction, cog: commands.Cog | None) -> None:
        self.current_cog = cog
        self.current_page = 0
        self.mode = "cog"
        embed = await self.create_cog_embed(cog)
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def show_command(
        self,
        interaction: discord.Interaction,
        command: commands.Command,
    ) -> None:
        self.current_page = 0
        self.total_pages = 0
        self.mode = "command"
        embed = await self.create_command_embed(command)
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def change_page(self, interaction: discord.Interaction, delta: int) -> None:
        self.current_page = max(0, min(self.current_page + delta, self.total_pages - 1))
        embed = await self.create_cog_embed(self.current_cog)
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "This help menu is not for you.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class HelpCategorySelect(discord.ui.Select):
    def __init__(
        self,
        help_view: HelpView,
        options: Iterable[discord.SelectOption],
    ) -> None:
        self.help_view = help_view
        super().__init__(
            placeholder="Select a category...",
            options=list(options),
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "__uncategorized__":
            await self.help_view.show_cog(interaction, None)
            return
        cog = self.help_view.bot.get_cog(value)
        if cog:
            await self.help_view.show_cog(interaction, cog)


class HelpSearchModal(discord.ui.Modal, title="Search Commands"):
    query = discord.ui.TextInput(
        label="Search",
        placeholder="Type a command name or keyword",
        min_length=1,
        max_length=50,
    )

    def __init__(self, help_view: HelpView) -> None:
        super().__init__()
        self.help_view = help_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.query.value.lower()
        await self.help_view.index.refresh_cache_if_needed()
        cached = self.help_view.index.get_cached()

        results = []
        for info in cached:
            if (
                text in info.name.lower()
                or text in info.description.lower()
                or any(text in alias.lower() for alias in info.aliases)
            ):
                cmd = self.help_view.bot.get_command(info.name)
                if cmd and await self.help_view.index.can_see_command(cmd, interaction.user):
                    results.append(cmd)

        visible = sorted(results, key=lambda c: c.qualified_name)[:20]

        if not visible:
            embed = discord.Embed(
                title="Search Results",
                description=f"No commands found for `{self.query.value}`.",
                color=discord.Color.red(),
            )
        else:
            lines = [f"`{cmd.qualified_name}` - {_get_command_description(cmd)}" for cmd in visible]
            embed = discord.Embed(
                title="Search Results",
                description="\n".join(lines),
                color=0x2F3136,
            )
        self.help_view.current_cog = None
        self.help_view.current_page = 0
        self.help_view.total_pages = 0
        self.help_view.mode = "search"
        self.help_view._update_components()
        await interaction.response.edit_message(embed=embed, view=self.help_view)


class HelpHomeButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="Home", row=1)
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.show_home(interaction)


class HelpPrevButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Prev",
            row=1,
            disabled=help_view.current_page <= 0,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.change_page(interaction, -1)


class HelpNextButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Next",
            row=1,
            disabled=help_view.current_page >= help_view.total_pages - 1,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.change_page(interaction, 1)


class HelpPageIndicator(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"{help_view.current_page + 1}/{max(help_view.total_pages, 1)}",
            disabled=True,
            row=1,
        )


class HelpSearchButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Search", row=1)
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(HelpSearchModal(self.help_view))


class HelpCloseButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(style=discord.ButtonStyle.danger, label="Close", row=2)
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        with suppress(discord.HTTPException):
            if not interaction.response.is_done():
                await interaction.response.defer()
            await interaction.delete_original_response()
        self.help_view.stop()


def _describe_param(param: inspect.Parameter) -> str:
    mapping = {
        "str": "text",
        "int": "number",
        "float": "decimal number",
        "bool": "true/false",
        "discord.Member": "member",
        "discord.User": "user",
        "discord.TextChannel": "text channel",
        "discord.VoiceChannel": "voice channel",
        "discord.Role": "role",
        "commands.Greedy": "multiple values",
        "typing.Union": "multiple types",
        "typing.Optional": "optional",
        "discord.abc.GuildChannel": "channel",
    }
    annotation = param.annotation
    if annotation == param.empty:
        return "text"
    annotation_str = str(annotation)
    if "Optional" in annotation_str or ("Union" in annotation_str and "None" in annotation_str):
        if "Optional[" in annotation_str:
            inner_type = annotation_str.split("Optional[")[1].split("]")[0]
        else:
            types = annotation_str.split("Union[")[1].split("]")[0].split(",")
            inner_type = [t.strip() for t in types if "None" not in t][0]
        for py_type, friendly in mapping.items():
            if py_type in inner_type:
                return friendly
        return "text"
    for py_type, friendly in mapping.items():
        if py_type in annotation_str:
            return friendly
    if "List" in annotation_str or "list" in annotation_str:
        return "list of items"
    return "text"


class AmenityHelpCommand(commands.HelpCommand):
    def __init__(self) -> None:
        super().__init__()
        self.index: HelpIndex | None = None

    def _ensure_index(self) -> HelpIndex:
        if self.index is None:
            self.index = HelpIndex(self.context.bot)
        return self.index

    async def command_callback(self, ctx: commands.Context, *, command: str | None = None) -> None:
        if command is None:
            mapping = self.get_bot_mapping()
            await self.send_bot_help(mapping)
            return

        query = command.strip()
        if not query:
            mapping = self.get_bot_mapping()
            await self.send_bot_help(mapping)
            return

        found = ctx.bot.get_command(query)
        if found:
            await self.send_command_help(found)
            return

        cog = ctx.bot.get_cog(query)
        if not cog:
            lowered = query.lower()
            for candidate in ctx.bot.cogs.values():
                if candidate.qualified_name.lower() == lowered or _cog_display_name(candidate).lower() == lowered:
                    cog = candidate
                    break

        if cog:
            await self.send_cog_help(cog)
            return

        await self.send_error_message(f"No command called `{query}` found.")

    async def send_group_help(self, group: commands.Group) -> None:
        await self.send_command_help(group)

    async def send_cog_help(self, cog: commands.Cog) -> None:
        index = self._ensure_index()
        if not await index.can_see_cog(cog, self.context.author):
            await self.send_error_message("You don't have permission to view this category.")
            return
        commands_list = [cmd for cmd in cog.walk_commands() if await index.can_see_command(cmd, self.context.author)]
        view = HelpView(
            bot=self.context.bot,
            index=index,
            author=self.context.author,
            prefix=self.context.prefix,
            mapping={cog: commands_list},
            uncategorized=[],
        )
        embed = await view.create_cog_embed(cog)
        await self.get_destination().send(embed=embed, view=view)

    async def send_command_help(self, command: commands.Command) -> None:
        index = self._ensure_index()
        if _is_help_command(command):
            await self.send_error_message("No command called `help` found.")
            return
        if not await index.can_see_command(command, self.context.author):
            await self.send_error_message("You don't have permission to view this command.")
            return
        embed = await _build_command_embed(
            command=command,
            index=index,
            author=self.context.author,
            prefix=self.context.prefix,
        )
        await self.get_destination().send(embed=embed)
        await index.refresh_cache_if_needed()

    async def send_bot_help(self, mapping: dict[commands.Cog, list[commands.Command]]) -> None:
        index = self._ensure_index()
        user = self.context.author
        filtered = await index.visible_cogs(user)
        uncategorized = await index.visible_uncategorized(user)
        view = HelpView(
            bot=self.context.bot,
            index=index,
            author=user,
            prefix=self.context.prefix,
            mapping=filtered,
            uncategorized=uncategorized,
        )
        embed = await view.create_home_embed()
        channel = self.get_destination()
        message = await channel.send(embed=embed, view=view)
        view.message = message
        await index.refresh_cache_if_needed()

    async def send_error_message(self, error: str) -> None:
        embed = discord.Embed(
            title="Help Error",
            description=error,
            color=discord.Color.red(),
        )
        await self.get_destination().send(embed=embed)

    async def command_not_found(self, string: str) -> str:
        return f"No command called `{string}` found."


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.index = HelpIndex(bot)

    async def cog_load(self) -> None:
        await self.index.build_cache()

    @app_commands.command(name="help", description="Show the help menu")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Command or category")
    async def help(self, interaction: discord.Interaction, query: str | None = None) -> None:
        index = self.index
        mapping = await index.visible_cogs(interaction.user)
        uncategorized = await index.visible_uncategorized(interaction.user)
        view = HelpView(
            bot=self.bot,
            index=index,
            author=interaction.user,
            prefix="/",
            mapping=mapping,
            uncategorized=uncategorized,
        )

        if query:
            command = self.bot.get_command(query)
            if command and await index.can_see_command(command, interaction.user):
                embed = await _build_command_embed(
                    command=command,
                    index=index,
                    author=interaction.user,
                    prefix="/",
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                view.message = await interaction.original_response()
                return

            lowered = query.lower()
            cog = None
            for candidate in mapping:
                if candidate.qualified_name.lower() == lowered or _cog_display_name(candidate).lower() == lowered:
                    cog = candidate
                    break
            if cog:
                embed = await view.create_cog_embed(cog)
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                view.message = await interaction.original_response()
                return

            embed = discord.Embed(
                title="Help Error",
                description=f"No command or category named `{query}` found.",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = await view.create_home_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
