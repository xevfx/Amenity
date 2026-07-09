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
GROUP_ORDER = {
    "Fun": 0,
    "Games": 1,
    "AI": 2,
    "Crypto": 3,
    "GitHub": 4,
    "Tools": 5,
    "Reminders": 6,
    "Templates": 7,
    "Utilities": 8,
    "other": 99,
}
GROUP_EMOJIS = {
    "AI": Emoji.SHINE.value,
    "Crypto": Emoji.CRYPTO.value,
    "Fun": Emoji.FIRE.value,
    "Games": Emoji.GIVEAWAY.value,
    "GitHub": Emoji.GITHUB.value,
    "Reminders": Emoji.TIME.value,
    "Templates": Emoji.FILE.value,
    "Tools": Emoji.UTILITIES.value,
    "Utilities": Emoji.UTILITIES.value,
    "other": Emoji.COMMAND.value,
}
COG_EMOJIS = {
    "AI": Emoji.SHINE.value,
    "Crypto": Emoji.CRYPTO.value,
    "Fun": Emoji.TADA.value,
    "Games": Emoji.GIVEAWAY.value,
    "Github": Emoji.GITHUB.value,
    "Reminder": Emoji.TIME.value,
    "ServerUtility": Emoji.HOUSE.value,
    "Template": Emoji.FILE.value,
    "Tools": Emoji.UTILITIES.value,
    "UserUtility": Emoji.AT.value,
}


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


def _group_label(group_name: str) -> str:
    if group_name == "other":
        return "Other"
    return group_name.replace("_", " ").title()


def _group_emoji(group_name: str) -> str:
    return GROUP_EMOJIS.get(group_name, Emoji.COMMAND.value)


def _group_sort_key(group_name: str) -> tuple[int, str]:
    return GROUP_ORDER.get(group_name, 50), _group_label(group_name).lower()


def _cog_emoji(cog: commands.Cog | None) -> str:
    if cog is None:
        return Emoji.COMMAND.value
    return COG_EMOJIS.get(cog.qualified_name, Emoji.COMMAND.value)


def _lookup_key(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())


def _singular_lookup_key(value: str) -> str:
    normalized = _lookup_key(value)
    if normalized.endswith("ies"):
        return f"{normalized[:-3]}y"
    if normalized.endswith("s"):
        return normalized[:-1]
    return normalized


def _matches_lookup(value: str, query: str) -> bool:
    return _lookup_key(value) == _lookup_key(query) or _singular_lookup_key(value) == _singular_lookup_key(query)


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


def _top_level_commands(commands_list: Sequence[commands.Command]) -> list[commands.Command]:
    return sorted([cmd for cmd in commands_list if cmd.parent is None], key=lambda command: command.qualified_name)


def _format_command_names(command: commands.Command) -> list[str]:
    children = sorted(getattr(command, "commands", []) or [], key=lambda child: child.qualified_name)
    if not children:
        return [f"`/{command.qualified_name}`"]
    return [f"`/{child.qualified_name}`" for child in children]


def _format_command_mentions(commands_list: Sequence[commands.Command], *, limit: int | None = 6) -> str:
    names = [
        command_name
        for command in _top_level_commands(commands_list)
        for command_name in _format_command_names(command)
    ]
    if not names:
        return "No command groups."
    shown = names if limit is None else names[:limit]
    if limit is not None and len(names) > limit:
        shown.append(f"+{len(names) - limit} more")
    return ", ".join(shown)


def _find_group_name(cogs: Iterable[commands.Cog], query: str) -> str | None:
    for group_name in {_cog_group_name(cog) for cog in cogs}:
        if _matches_lookup(group_name, query) or _matches_lookup(_group_label(group_name), query):
            return group_name
    if _matches_lookup("other", query) or _matches_lookup("uncategorized", query):
        return "other"
    return None


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
        group_name = _cog_group_name(command.cog)
        group_path = f"{_group_emoji(group_name)} {_group_label(group_name)}"
        category_path = f"{_cog_emoji(command.cog)} {_cog_display_name(command.cog)}"
        embed.add_field(
            name="Group",
            value=f"{group_path} > {category_path}",
            inline=False,
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
        self.current_group: str | None = None
        self.current_page = 0
        self.total_pages = 0
        self.mode = "home"
        self._update_components()

    def _grouped_cogs(self) -> dict[str, list[commands.Cog]]:
        grouped: dict[str, list[commands.Cog]] = {}
        for cog in self.mapping:
            grouped.setdefault(_cog_group_name(cog), []).append(cog)
        if self.uncategorized:
            grouped.setdefault("other", [])
        return {
            group_name: sorted(cogs, key=lambda cog: _cog_display_name(cog).lower())
            for group_name, cogs in grouped.items()
        }

    def _group_command_count(self, group_name: str) -> int:
        if group_name == "other":
            return len(self.uncategorized)
        return sum(len(self.mapping[cog]) for cog in self._grouped_cogs().get(group_name, []))

    def _group_options(self) -> list[discord.SelectOption]:
        options = []
        for group_name, _cogs in sorted(self._grouped_cogs().items(), key=lambda item: _group_sort_key(item[0])):
            options.append(
                discord.SelectOption(
                    label=_group_label(group_name),
                    value=f"group:{group_name}",
                    description=f"{self._group_command_count(group_name)} commands",
                    emoji=_group_emoji(group_name),
                )
            )
        return options

    def _cog_options(self, group_name: str) -> list[discord.SelectOption]:
        if group_name == "other":
            return [
                discord.SelectOption(
                    label="Uncategorized",
                    value="uncategorized",
                    description=f"{len(self.uncategorized)} commands",
                    emoji=Emoji.COMMAND.value,
                )
            ]

        options = []
        for cog in self._grouped_cogs().get(group_name, []):
            preview = _format_command_mentions(self.mapping[cog], limit=3)
            options.append(
                discord.SelectOption(
                    label=_cog_display_name(cog),
                    value=f"cog:{cog.qualified_name}",
                    description=preview.replace("`", "")[:100],
                    emoji=_cog_emoji(cog),
                )
            )
        return options

    def _select_options(self) -> tuple[list[discord.SelectOption], str]:
        if (
            self.mode in {"group", "cog"}
            and self.current_group
            and len(self._grouped_cogs().get(self.current_group, [])) > 1
        ):
            return self._cog_options(self.current_group), f"Select a {_group_label(self.current_group)} category..."
        return self._group_options(), "Select a command group..."

    def _update_components(self) -> None:
        self.clear_items()
        options, placeholder = self._select_options()
        if options:
            self.add_item(HelpCategorySelect(self, options[:25], placeholder=placeholder))

        if self.mode != "home":
            self.add_item(HelpHomeButton(self))
        if self.total_pages > 1:
            self.add_item(HelpPrevButton(self))
            self.add_item(HelpPageIndicator(self))
            self.add_item(HelpNextButton(self))
        self.add_item(HelpSearchButton(self))
        self.add_item(HelpCloseButton(self))

    async def create_home_embed(self) -> discord.Embed:
        group_lines = []
        for group_name, _cogs in sorted(self._grouped_cogs().items(), key=lambda item: _group_sort_key(item[0])):
            command_count = self._group_command_count(group_name)
            group_lines.append(f"{_group_emoji(group_name)} **{_group_label(group_name)}** - {command_count} commands")

        embed = discord.Embed(
            title=f"{Emoji.CROWN.value} Amenity Commands",
            description="\n".join(
                [
                    "Select a command group from the dropdown.\n",
                    *group_lines,
                    f"\nUse `{self.prefix}help <command>` for details.",
                ]
            ),
            color=0x2F3136,
        )
        return embed

    async def create_group_embed(self, group_name: str) -> discord.Embed:
        label = _group_label(group_name)
        command_count = self._group_command_count(group_name)
        embed = discord.Embed(
            title=f"{_group_emoji(group_name)} {label} - {command_count} commands",
            description="Categories in this group.",
            color=0x2F3136,
        )

        if group_name == "other":
            if self.uncategorized:
                embed.add_field(
                    name=f"{Emoji.COMMAND.value} Uncategorized - {len(self.uncategorized)} commands",
                    value=_format_command_mentions(self.uncategorized, limit=None),
                    inline=False,
                )
            return embed

        for cog in self._grouped_cogs().get(group_name, []):
            commands_list = self.mapping[cog]
            embed.add_field(
                name=f"{_cog_emoji(cog)} {_cog_display_name(cog)} - {len(commands_list)} commands",
                value=_format_command_mentions(commands_list),
                inline=False,
            )
        return embed

    async def create_cog_embed(self, cog: commands.Cog | None) -> discord.Embed:
        if cog is None:
            commands_list = self.uncategorized
            title = "Uncategorized"
            icon = Emoji.COMMAND.value
            description = "Commands without a category."
        else:
            commands_list = self.mapping.get(cog, [])
            group_name = _cog_group_name(cog)
            group_label = _group_label(group_name)
            cog_label = _cog_display_name(cog)
            title = cog_label if group_label == cog_label else f"{group_label} > {cog_label}"
            icon = _cog_emoji(cog)
            description = _normalize_description(getattr(cog, "description", None))

        embed = discord.Embed(
            title=f"{icon} {title} Commands",
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

        top_commands = _top_level_commands(commands_list) or sorted(commands_list, key=lambda c: c.qualified_name)
        per_page = 10
        self.total_pages = max(1, (len(top_commands) + per_page - 1) // per_page)
        start = self.current_page * per_page
        end = start + per_page
        page_commands = top_commands[start:end]
        command_names = ", ".join(
            command_name
            for command in page_commands
            for command_name in _format_command_names(command)
        )
        embed.add_field(name="Commands", value=command_names, inline=False)
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
        self.current_group = None
        self.current_page = 0
        self.total_pages = 0
        self.mode = "home"
        embed = await self.create_home_embed()
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def show_group(self, interaction: discord.Interaction, group_name: str) -> None:
        cogs = self._grouped_cogs().get(group_name, [])
        if len(cogs) == 1:
            await self.show_cog(interaction, cogs[0])
            return

        self.current_cog = None
        self.current_group = group_name
        self.current_page = 0
        self.total_pages = 0
        self.mode = "group"
        embed = await self.create_group_embed(group_name)
        self._update_components()
        await interaction.response.edit_message(embed=embed, view=self)

    async def show_cog(self, interaction: discord.Interaction, cog: commands.Cog | None) -> None:
        self.current_cog = cog
        self.current_group = _cog_group_name(cog)
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
        *,
        placeholder: str,
    ) -> None:
        self.help_view = help_view
        super().__init__(
            placeholder=placeholder,
            options=list(options),
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value.startswith("group:"):
            await self.help_view.show_group(interaction, value.removeprefix("group:"))
            return
        if value == "uncategorized":
            await self.help_view.show_cog(interaction, None)
            return
        cog_name = value.removeprefix("cog:")
        cog = self.help_view.bot.get_cog(cog_name)
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

        group_name = _find_group_name(ctx.bot.cogs.values(), query)
        if group_name:
            await self.send_group_category_help(group_name)
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

    async def send_group_category_help(self, group_name: str) -> None:
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
        cogs = view._grouped_cogs().get(group_name, [])
        if len(cogs) == 1:
            cog = cogs[0]
            view.current_cog = cog
            view.current_group = _cog_group_name(cog)
            view.mode = "cog"
            embed = await view.create_cog_embed(cog)
            view._update_components()
            message = await self.get_destination().send(embed=embed, view=view)
            view.message = message
            return

        view.current_group = group_name
        view.mode = "group"
        embed = await view.create_group_embed(group_name)
        view._update_components()
        message = await self.get_destination().send(embed=embed, view=view)
        view.message = message

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
        view.current_cog = cog
        view.current_group = _cog_group_name(cog)
        view.mode = "cog"
        embed = await view.create_cog_embed(cog)
        view._update_components()
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
            group_name = _find_group_name(mapping.keys(), query)
            if group_name:
                cogs = view._grouped_cogs().get(group_name, [])
                if len(cogs) == 1:
                    cog = cogs[0]
                    view.current_cog = cog
                    view.current_group = _cog_group_name(cog)
                    view.mode = "cog"
                    embed = await view.create_cog_embed(cog)
                    view._update_components()
                    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                    view.message = await interaction.original_response()
                    return

                view.current_group = group_name
                view.mode = "group"
                embed = await view.create_group_embed(group_name)
                view._update_components()
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                view.message = await interaction.original_response()
                return

            cog = None
            for candidate in mapping:
                if candidate.qualified_name.lower() == lowered or _cog_display_name(candidate).lower() == lowered:
                    cog = candidate
                    break
            if cog:
                view.current_cog = cog
                view.current_group = _cog_group_name(cog)
                view.mode = "cog"
                embed = await view.create_cog_embed(cog)
                view._update_components()
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
