from __future__ import annotations

import inspect
import re
import types
import typing
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from api.emojis import Emoji
from core.app_commands import command_mention, load_persisted_command_ids
from core.cache import cache

if TYPE_CHECKING:
    from collections.abc import Sequence

HELP_CACHE_KEY = "help:commands"
HELP_CACHE_TTL = 300
GROUP_ORDER = {
    "utility": 0,
    "server": 1,
    "tools": 2,
    "templates": 3,
    "reminders": 4,
    "github": 5,
    "games": 6,
    "fun": 7,
    "crypto": 8,
    "ai": 9,
    "feedback": 10,
    "other": 11,
}


@dataclass(frozen=True)
class GroupMeta:
    """Everything the help menu needs to describe one command group."""

    label: str
    description: str
    emoji: str


GROUP_METADATA = {
    "utility": GroupMeta("Utility", "Information and everyday utility commands.", Emoji.UTILITIES.value),
    "server": GroupMeta("Server", "Server, role, member, and channel information.", Emoji.HOUSE.value),
    "tools": GroupMeta("Tools", "Helper tools for media, text, web, and security.", Emoji.UTILITIES.value),
    "templates": GroupMeta("Templates", "Save, manage, and send reusable templates.", Emoji.FILE.value),
    "reminders": GroupMeta("Reminders", "Never forget a task with personal reminders.", Emoji.TIME.value),
    "github": GroupMeta("GitHub", "Inspect repositories, users, and pull requests.", Emoji.GITHUB.value),
    "games": GroupMeta("Games", "Play lightweight games directly in chat.", Emoji.GIVEAWAY.value),
    "fun": GroupMeta("Fun", "Lighthearted commands, memes, and jokes.", Emoji.FIRE.value),
    "crypto": GroupMeta("Crypto", "Cryptocurrency prices, balances, and conversions.", Emoji.CRYPTO.value),
    "ai": GroupMeta("AI", "Chat, grammar, and image generation powered by AI.", Emoji.SHINE.value),
    "feedback": GroupMeta("Feedback", "Share bug reports and suggestions with developers.", Emoji.INVITE.value),
    "other": GroupMeta("Other", "Uncategorized and standalone commands.", Emoji.COMMAND.value),
}
COG_GROUPS = {
    "UserUtility": "utility",
    "ServerUtility": "server",
    "Tools": "tools",
    "Template": "templates",
    "Reminder": "reminders",
    "Github": "github",
    "Games": "games",
    "Fun": "fun",
    "Crypto": "crypto",
    "AI": "ai",
    "Feedback": "feedback",
}
COG_EMOJIS = {
    "UserUtility": Emoji.AT.value,
    "ServerUtility": Emoji.HOUSE.value,
    "Tools": Emoji.UTILITIES.value,
    "Template": Emoji.FILE.value,
    "Reminder": Emoji.TIME.value,
    "Github": Emoji.GITHUB.value,
    "Games": Emoji.GIVEAWAY.value,
    "Fun": Emoji.TADA.value,
    "Crypto": Emoji.CRYPTO.value,
    "AI": Emoji.SHINE.value,
    "Feedback": Emoji.INVITE.value,
}


@dataclass(frozen=True)
class HelpCommandInfo:
    name: str
    description: str
    usage: str
    cog_name: str | None
    parent: str | None
    is_group: bool
    enabled: bool
    hidden: bool


def _cog_group_name(cog: commands.Cog | None) -> str:
    if cog is None:
        return "other"
    explicit = getattr(cog, "group_name", None)
    if explicit:
        return str(explicit).strip().lower()
    return COG_GROUPS.get(cog.qualified_name, "other").lower()


def _cog_display_name(cog: commands.Cog | None) -> str:
    if cog is None:
        return "Uncategorized"
    return getattr(cog, "display_name", cog.qualified_name)


def _cog_emoji(cog: commands.Cog | None) -> str:
    if cog is None:
        return Emoji.COMMAND.value
    return COG_EMOJIS.get(cog.qualified_name, Emoji.COMMAND.value)


def _cog_description(cog: commands.Cog | None) -> str:
    if cog is None:
        return "Commands without a category."
    return inspect.cleandoc(cog.description or "").split("\n\n")[0] or "No description provided."


def _group_meta(group_name: str) -> GroupMeta:
    return GROUP_METADATA.get(
        group_name,
        GroupMeta(group_name.replace("_", " ").title(), "Commands in this section.", Emoji.COMMAND.value),
    )


def _group_label(group_name: str) -> str:
    return _group_meta(group_name).label


def _group_description(group_name: str) -> str:
    return _group_meta(group_name).description


def _group_emoji(group_name: str) -> str:
    return _group_meta(group_name).emoji


def _group_order(group_name: str) -> int:
    return GROUP_ORDER.get(group_name, 999)


def _is_group(command: commands.Command) -> bool:
    return bool(getattr(command, "commands", None))


def _build_signature(prefix: str, command: commands.Command) -> str:
    signature = command.signature.strip()
    return f"{prefix}{command.qualified_name} {signature}".strip()


def _get_command_description(command: commands.Command) -> str:
    desc = command.description or command.help or command.brief or ""
    return desc.strip() or "No description provided."


def _top_level_commands(commands_list: Sequence[commands.Command]) -> list[commands.Command]:
    return sorted([cmd for cmd in commands_list if cmd.parent is None], key=lambda command: command.qualified_name)


def _format_command_names(command: commands.Command, index: HelpIndex | None = None) -> list[str]:
    children = sorted(getattr(command, "commands", []) or [], key=lambda child: child.qualified_name)
    if not children:
        if index is not None:
            return [index.get_command_mention(command.qualified_name)]
        return [f"`/{command.qualified_name}`"]
    if index is not None:
        return [index.get_command_mention(child.qualified_name) for child in children]
    return [f"`/{child.qualified_name}`" for child in children]


def _format_command_mentions(
    commands_list: Sequence[commands.Command],
    index: HelpIndex | None = None,
    *,
    limit: int | None = 30,
) -> str:
    displayed = []
    for cmd in commands_list:
        if _is_group(cmd) and getattr(cmd, "commands", None):
            continue
        displayed.append(cmd)
    displayed.sort(key=lambda c: c.qualified_name)
    if not displayed:
        return "No commands available."

    if index is not None:
        names = [index.get_command_mention(cmd.qualified_name) for cmd in displayed]
    else:
        names = [f"`/{cmd.qualified_name}`" for cmd in displayed]

    shown = names if limit is None else names[:limit]
    if limit is not None and len(names) > limit:
        shown.append(f"+{len(names) - limit} more")
    return ", ".join(shown)


async def _build_command_embed(
    command: commands.Command,
    index: HelpIndex,
    author: discord.abc.User,
    prefix: str,
) -> discord.Embed:
    mention = index.get_command_mention(command.qualified_name, prefix=prefix)
    embed = discord.Embed(
        title=f"Command: {command.qualified_name}",
        description=f"{mention}\n\n{_get_command_description(command)}",
        color=0x05C1F5,
    )

    if command.parent:
        embed.add_field(
            name="Parent",
            value=index.get_command_mention(command.parent.qualified_name, prefix=prefix),
            inline=True,
        )

    if hasattr(command, "usage") and command.usage:
        usage = f"{prefix}{command.qualified_name} {command.usage}"
    else:
        usage = _build_signature(prefix, command)
    embed.add_field(name="Usage", value=f"`{usage}`", inline=False)

    aliases = getattr(command, "aliases", [])
    if aliases:
        embed.add_field(
            name="Aliases",
            value=", ".join(f"`{alias}`" for alias in sorted(aliases)),
            inline=True,
        )

    cooldown = getattr(command, "_buckets", None)
    if cooldown and cooldown._cooldown:
        embed.add_field(
            name="Cooldown",
            value=f"{cooldown._cooldown.per:.0f}s per {cooldown._cooldown.type.name}",
            inline=True,
        )

    if _is_group(command):
        visible_subcommands = []
        for sub in sorted(command.commands, key=lambda c: c.name):
            if await index.can_see_command(sub, author):
                sub_mention = index.get_command_mention(sub.qualified_name, prefix=prefix)
                visible_subcommands.append(f"{sub_mention}\n-# {_get_command_description(sub)}")
        if visible_subcommands:
            embed.add_field(
                name="Subcommands",
                value="\n\n".join(visible_subcommands[:10]),
                inline=False,
            )

    params = _command_parameters(command)
    if params:
        embed.add_field(name="Parameters", value="\n".join(params), inline=False)

    embed.set_footer(text="<> required, [] optional • Amenity")
    return embed


async def _build_command_text(
    command: commands.Command,
    index: HelpIndex,
    author: discord.abc.User,
    prefix: str,
) -> str:
    """Markdown representation of a command detail screen for Components V2.

    Mirrors :func:`_build_command_embed` field-for-field; used only inside the
    interactive (Components V2) help view, where embeds are not allowed.
    """
    mention = index.get_command_mention(command.qualified_name, prefix=prefix)
    sections = [
        f"## Command: {command.qualified_name}",
        f"{mention}\n\n{_get_command_description(command)}",
    ]

    if command.parent:
        sections.append(f"**Parent**\n{index.get_command_mention(command.parent.qualified_name, prefix=prefix)}")

    if hasattr(command, "usage") and command.usage:
        usage = f"{prefix}{command.qualified_name} {command.usage}"
    else:
        usage = _build_signature(prefix, command)
    sections.append(f"**Usage**\n`{usage}`")

    aliases = getattr(command, "aliases", [])
    if aliases:
        sections.append(f"**Aliases**\n{', '.join(f'`{alias}`' for alias in sorted(aliases))}")

    cooldown = getattr(command, "_buckets", None)
    if cooldown and cooldown._cooldown:
        sections.append(f"**Cooldown**\n{cooldown._cooldown.per:.0f}s per {cooldown._cooldown.type.name}")

    if _is_group(command):
        visible_subcommands = []
        for sub in sorted(command.commands, key=lambda c: c.name):
            if await index.can_see_command(sub, author):
                sub_mention = index.get_command_mention(sub.qualified_name, prefix=prefix)
                visible_subcommands.append(f"{sub_mention}\n-# {_get_command_description(sub)}")
        if visible_subcommands:
            sections.append("**Subcommands**\n" + "\n\n".join(visible_subcommands[:10]))

    params = _command_parameters(command)
    if params:
        sections.append("**Parameters**\n" + "\n".join(params))

    sections.append("-# <> required, [] optional • Amenity")
    return "\n\n".join(sections)


IGNORED_COGS: frozenset[str] = frozenset({"owner", "jishaku", "helpcog"})


def _is_owner_cog(command: commands.Command) -> bool:
    cog = command.cog
    if not cog:
        return False
    name = cog.qualified_name.lower()
    return name in IGNORED_COGS or getattr(cog, "owner_only", False) or getattr(cog, "hidden", False)


def _is_help_command(command: commands.Command) -> bool:
    """The bot's own help command never appears inside the help menu."""
    return command.qualified_name == "help" or command.name == "help"


def _is_command_visible(command: commands.Command, *, is_owner: bool) -> bool:
    """Single source of truth for whether a user may see a command in help."""
    if _is_help_command(command) or _is_owner_cog(command):
        return False
    parent = getattr(command, "parent", None)
    while parent is not None:
        if parent.hidden and not is_owner:
            return False
        parent = getattr(parent, "parent", None)
    if command.hidden and not is_owner:
        return False
    return command.enabled


class HelpIndex:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_command_names: tuple[str, ...] = ()

    async def fetch_app_command_ids_if_needed(self) -> dict[str, int]:
        bot_ids = getattr(self.bot, "app_command_ids", None)
        if isinstance(bot_ids, dict) and bot_ids:
            return bot_ids

        fetch = getattr(self.bot, "fetch_app_commands", None)
        if callable(fetch):
            return await fetch()

        persisted = load_persisted_command_ids()
        if persisted:
            self.bot.app_command_ids = persisted
        return persisted

    def get_command_mention(self, command_name: str, *, prefix: str = "/") -> str:
        return command_mention(command_name, getattr(self.bot, "app_command_ids", None) or {}, prefix=prefix)

    async def is_owner(self, user: discord.abc.User) -> bool:
        return await self.bot.is_owner(user)

    async def can_see_command(self, command: commands.Command, user: discord.abc.User) -> bool:
        return _is_command_visible(command, is_owner=await self.is_owner(user))

    def _command_names(self) -> tuple[str, ...]:
        return tuple(sorted(command.qualified_name for command in self.bot.commands))

    def invalidate_if_stale(self) -> None:
        current = self._command_names()
        if current != self._last_command_names:
            cache.delete(HELP_CACHE_KEY)
            self._last_command_names = current

    async def build_cache(self) -> dict[str, HelpCommandInfo]:
        self.invalidate_if_stale()
        cached = cache.get(HELP_CACHE_KEY)
        if isinstance(cached, dict):
            return cached

        data: dict[str, HelpCommandInfo] = {}
        for command in self.bot.walk_commands():
            data[command.qualified_name] = HelpCommandInfo(
                name=command.qualified_name,
                description=_get_command_description(command),
                usage=_build_signature("/", command),
                cog_name=command.cog_name,
                parent=command.parent.qualified_name if command.parent else None,
                is_group=_is_group(command),
                enabled=command.enabled,
                hidden=command.hidden,
            )

        cache.set(HELP_CACHE_KEY, data, ttl=HELP_CACHE_TTL)
        self._last_command_names = self._command_names()
        return data

    async def visible_commands(self, user: discord.abc.User) -> list[commands.Command]:
        is_owner = await self.is_owner(user)
        visible = [cmd for cmd in self.bot.commands if _is_command_visible(cmd, is_owner=is_owner)]
        return sorted(visible, key=lambda cmd: cmd.qualified_name)

    async def visible_cogs(self, user: discord.abc.User) -> dict[commands.Cog, list[commands.Command]]:
        mapping: dict[commands.Cog, list[commands.Command]] = {}
        is_owner = await self.is_owner(user)

        for cog in self.bot.cogs.values():
            name = cog.qualified_name.lower()
            if name in IGNORED_COGS or getattr(cog, "owner_only", False) or getattr(cog, "hidden", False):
                continue

            visible_in_cog = [
                command for command in cog.walk_commands() if _is_command_visible(command, is_owner=is_owner)
            ]
            if visible_in_cog:
                mapping[cog] = sorted(visible_in_cog, key=lambda cmd: cmd.qualified_name)

        return mapping

    async def visible_uncategorized(self, user: discord.abc.User) -> list[commands.Command]:
        is_owner = await self.is_owner(user)
        visible = [
            command
            for command in self.bot.commands
            if command.cog is None and _is_command_visible(command, is_owner=is_owner)
        ]
        return sorted(visible, key=lambda cmd: cmd.qualified_name)

    async def search(self, query: str, user: discord.abc.User) -> list[commands.Command]:
        terms = query.lower().split()
        if not terms:
            return []

        results: list[commands.Command] = []
        for command in self.bot.walk_commands():
            if not await self.can_see_command(command, user):
                continue

            target = f"{command.qualified_name} {_get_command_description(command)}".lower()
            if all(term in target for term in terms):
                results.append(command)

        return sorted(results, key=lambda cmd: cmd.qualified_name)


_PARAM_TYPE_NAMES = {
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

_NONE_NAMES = {"None", "NoneType", "type(None)"}


def _describe_single_type(tp: object) -> str:
    if isinstance(tp, commands.Greedy) or tp is commands.Greedy:
        return "multiple values"
    if typing.get_origin(tp) is list or (isinstance(tp, type) and issubclass(tp, list)):
        return "list of items"
    module = getattr(tp, "__module__", "") or ""
    qualname = getattr(tp, "__qualname__", None) or getattr(tp, "__name__", None)
    if qualname:
        key = f"{module}.{qualname}" if module and module not in ("builtins", "typing") else qualname
        if key in _PARAM_TYPE_NAMES:
            return _PARAM_TYPE_NAMES[key]
    return "text"


def _describe_str_annotation(text: str) -> str:
    text = text.strip()
    optional_match = re.fullmatch(r"(?:typing\.)?Optional\[(.+)\]", text)
    if optional_match:
        return _describe_str_annotation(optional_match.group(1))
    if text.startswith(("Union[", "typing.Union[")):
        parts = [part.strip() for part in text[text.index("[") + 1 : -1].split(",")]
    elif "|" in text:
        parts = [part.strip() for part in text.split("|")]
    else:
        parts = [text]
    meaningful = [part for part in parts if part not in _NONE_NAMES]
    if len(meaningful) > 1:
        return "multiple types"
    single = meaningful[0] if meaningful else "text"
    if single.startswith(("Greedy[", "commands.Greedy")):
        return "multiple values"
    if single in _PARAM_TYPE_NAMES:
        return _PARAM_TYPE_NAMES[single]
    if re.fullmatch(r"(?:typing\.)?[Ll]ist\[.+\]", single):
        return "list of items"
    return "text"


def _describe_param(param: inspect.Parameter) -> str:
    annotation = param.annotation
    if annotation == param.empty:
        return "text"
    if annotation is None or annotation is type(None):
        return "text"
    if isinstance(annotation, str):
        # Modules using `from __future__ import annotations` leave these unevaluated.
        return _describe_str_annotation(annotation)
    if isinstance(annotation, commands.Greedy) or annotation is commands.Greedy:
        return "multiple values"
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        meaningful = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
        if len(meaningful) == 1:
            return _describe_single_type(meaningful[0])
        if not meaningful:
            return "text"
        return "multiple types"
    return _describe_single_type(annotation)


def _command_parameters(command: commands.Command) -> list[str]:
    lines = []
    for name, param in command.clean_params.items():
        desc = getattr(param, "description", None)
        annotation = _describe_param(param)
        required = param.default is inspect.Parameter.empty
        req_str = "required" if required else "optional"
        suffix = f" - {desc}" if desc else ""
        lines.append(f"`{name}` ({annotation}, {req_str}){suffix}")
    return lines


HELP_ACCENT_COLOR = 0x0EEDEB
HELP_COMMAND_ACCENT_COLOR = 0x05C1F5
MAX_HELP_TEXT_LENGTH = 4000


class HelpView(discord.ui.LayoutView):
    """Components V2 help menu.

    Message text and its controls live together inside one Components V2
    container; send and edit paths pass only ``view=``.
    """

    def __init__(
        self,
        bot: commands.Bot,
        index: HelpIndex,
        author: discord.abc.User,
        prefix: str,
        mapping: dict[commands.Cog, list[commands.Command]],
        uncategorized: list[commands.Command],
    ) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.index = index
        self.author = author
        self.prefix = prefix
        self.mapping = mapping
        self.uncategorized = uncategorized
        self.message: discord.Message | None = None
        self.selected_group: str | None = None
        self.current_cog: commands.Cog | None = None
        self.current_page: int = 0
        self.total_pages: int = 1
        self.mode: str = "home"
        self.content_component = discord.ui.TextDisplay("\u200b")
        self.container = discord.ui.Container(self.content_component, accent_colour=HELP_ACCENT_COLOR)
        self.add_item(self.container)
        self._update_components()

    def _set_content(self, content: str) -> None:
        """Keep each V2 help message within Discord's TextDisplay budget."""
        content = content.replace("<@", "<@\u200b").replace("<#", "<#\u200b")
        content = discord.utils.escape_mentions(content)
        marker = "\n\n-# Help text shortened to fit Discord's Components V2 limit."
        if len(content) > MAX_HELP_TEXT_LENGTH:
            content = content[: MAX_HELP_TEXT_LENGTH - len(marker)].rstrip() + marker
        self.content_component.content = content or "\u200b"

    def _groups(self) -> list[str]:
        groups = set()
        for cog in self.mapping:
            if self._get_display_commands(self.mapping.get(cog, [])):
                groups.add(_cog_group_name(cog))
        # The "other" bucket is intentionally never advertised: any command that
        # lands here (e.g. the bot's own help command) is not browsable.
        groups.discard("other")
        groups.discard("owner")
        return sorted(groups, key=_group_order)

    def _grouped_cogs(self) -> dict[str, list[commands.Cog]]:
        grouped: dict[str, list[commands.Cog]] = {}
        for cog in self.mapping:
            group = _cog_group_name(cog)
            grouped.setdefault(group, []).append(cog)
        for group in grouped:
            grouped[group].sort(key=lambda item: _cog_display_name(item).lower())
        return grouped

    def _group_command_count(self, group_name: str) -> int:
        if group_name == "other":
            return len(self.uncategorized)
        total = 0
        for cog in self._grouped_cogs().get(group_name, []):
            total += len(self.mapping.get(cog, []))
        return total

    def _group_options(self) -> list[discord.SelectOption]:
        options = []
        for group in self._groups():
            options.append(
                discord.SelectOption(
                    label=_group_label(group),
                    value=f"group:{group}",
                    description=_group_description(group)[:100],
                    emoji=_group_emoji(group),
                )
            )
        return options

    def _cog_options(self, group_name: str) -> list[discord.SelectOption]:
        if group_name == "other":
            return []

        cogs = self._grouped_cogs().get(group_name, [])
        if len(cogs) <= 1:
            return []

        options = []
        for cog in cogs:
            preview = _format_command_mentions(self.mapping.get(cog, []), self.index, limit=3)
            clean_preview = re.sub(r"</([^:]+):\d+>", r"/\1", preview).replace("`", "")[:100]
            options.append(
                discord.SelectOption(
                    label=_cog_display_name(cog),
                    value=f"cog:{cog.qualified_name}",
                    description=clean_preview,
                    emoji=_cog_emoji(cog),
                )
            )
        return options

    def _clear_rows(self) -> None:
        """Keep the help text and rebuild all controls inside its container."""
        self.container.clear_items()
        self.container.add_item(self.content_component)

    def _update_components(self) -> None:
        if self.mode == "home":
            self._render_home_content()
        elif self.mode == "group" and self.selected_group:
            self._render_group_content(self.selected_group)
        elif self.mode == "cog":
            self._render_cog_content(self.current_cog)
        # "command" and "search" content is rendered by their callers.

        self._clear_rows()

        placeholder = "Choose a command category..."
        options = self._group_options()
        if self.selected_group:
            cog_options = self._cog_options(self.selected_group)
            if cog_options:
                options = cog_options
                placeholder = f"{_group_label(self.selected_group)}: Choose a category..."

        if options:
            self.container.add_item(
                discord.ui.ActionRow(HelpCategorySelect(self, options[:25], placeholder=placeholder))
            )

        if self.total_pages > 1:
            self.container.add_item(
                discord.ui.ActionRow(
                    HelpFirstButton(self),
                    HelpPrevButton(self),
                    HelpPageIndicator(self),
                    HelpNextButton(self),
                    HelpLastButton(self),
                )
            )

        nav_items = []
        if self.mode != "home":
            nav_items.append(HelpHomeButton(self))
        nav_items.append(HelpSearchButton(self))
        self.container.add_item(discord.ui.ActionRow(*nav_items))

    def _render_home_content(self) -> None:
        self.container.accent_colour = HELP_ACCENT_COLOR
        group_lines = [f"{_group_emoji(group_name)} {_group_label(group_name)}" for group_name in self._groups()]
        help_mention = self.index.get_command_mention("help", prefix=self.prefix)
        self._set_content(
            "\n".join(
                [
                    f"## {Emoji.CROWN.value} Amenity Commands",
                    "Select a command group from the dropdown below to explore commands.\n",
                    *group_lines,
                    f"\nUse {help_mention} `<command>` for details.",
                ]
            )
        )

    def _render_group_content(self, group_name: str) -> None:
        self.container.accent_colour = HELP_ACCENT_COLOR
        label = _group_label(group_name)
        emoji = _group_emoji(group_name)
        description = _group_description(group_name)
        command_count = self._group_command_count(group_name)
        heading = f"## {emoji} {label} — {command_count} commands"

        cogs = self._grouped_cogs().get(group_name, [])
        if len(cogs) <= 1:
            if group_name == "other":
                commands_list = self.uncategorized
            elif cogs:
                commands_list = self.mapping.get(cogs[0], [])
            else:
                commands_list = []

            display_commands = self._get_display_commands(commands_list)
            if not display_commands:
                self.total_pages = 0
                self.current_page = 0
                note = "*No commands are currently available in this category.*"
                self._set_content("\n\n".join(part for part in (heading, description, note) if part))
                return

            per_page = 30
            self.total_pages = max(1, (len(display_commands) + per_page - 1) // per_page)
            self.current_page = max(0, min(self.current_page, self.total_pages - 1))
            start = self.current_page * per_page
            end = start + per_page
            page_commands = display_commands[start:end]

            mentions = _format_command_mentions(page_commands, self.index, limit=None)
            body = "\n\n".join(part for part in (description, mentions) if part)
            if self.total_pages > 1:
                footer = (
                    f"-# Page {self.current_page + 1}/{self.total_pages} • {len(display_commands)} commands • Amenity"
                )
            else:
                footer = f"-# {len(display_commands)} commands • Amenity"
            self._set_content("\n\n".join([heading, body, footer]))
            return

        self.total_pages = 1
        self.current_page = 0
        sections = [heading]
        rendered = False
        for cog in cogs:
            commands_list = self.mapping.get(cog, [])
            display_commands = self._get_display_commands(commands_list)
            if not display_commands:
                continue
            rendered = True
            mentions = _format_command_mentions(display_commands, self.index, limit=30)
            sections.append(f"**{_cog_emoji(cog)} {_cog_display_name(cog)}**\n{mentions}")
        if not rendered:
            note = "*No commands are currently available in this category.*"
            self._set_content("\n\n".join(part for part in (heading, description, note) if part))
            return
        sections.append(f"-# {command_count} commands • Amenity")
        self._set_content("\n\n".join(sections))

    def _render_cog_content(self, cog: commands.Cog | None) -> None:
        self.container.accent_colour = HELP_ACCENT_COLOR
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
            description = _cog_description(cog)

        heading = f"## {icon} {title} Commands"

        display_commands = self._get_display_commands(commands_list)
        if not display_commands:
            self.total_pages = 0
            self.current_page = 0
            note = f"{description}\n\n*No commands are currently available in this category.*"
            self._set_content("\n\n".join([heading, note]))
            return

        per_page = 30
        self.total_pages = max(1, (len(display_commands) + per_page - 1) // per_page)
        self.current_page = max(0, min(self.current_page, self.total_pages - 1))
        start = self.current_page * per_page
        end = start + per_page
        page_commands = display_commands[start:end]

        command_entries = [
            self.index.get_command_mention(cmd.qualified_name, prefix=self.prefix) for cmd in page_commands
        ]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for entry in command_entries:
            extra = len(entry) + (2 if current else 0)
            if current_len + extra > 1000:
                chunks.append(", ".join(current))
                current = [entry]
                current_len = len(entry)
            else:
                current.append(entry)
                current_len += extra
        if current:
            chunks.append(", ".join(current))

        sections = [heading, description]
        for i, chunk in enumerate(chunks):
            field_name = "Commands" if i == 0 else "Commands (cont.)"
            sections.append(f"**{field_name}**\n{chunk}")

        footer_parts = (
            [f"Page {self.current_page + 1}/{self.total_pages}", f"{len(display_commands)} commands", "Amenity"]
            if self.total_pages > 1
            else [f"{len(display_commands)} commands", "Amenity"]
        )
        sections.append(f"-# {' • '.join(footer_parts)}")
        self._set_content("\n\n".join(sections))

    def _get_display_commands(self, commands_list: Sequence[commands.Command]) -> list[commands.Command]:
        displayed: list[commands.Command] = []
        seen = set()
        for cmd in commands_list:
            if _is_group(cmd) and getattr(cmd, "commands", None):
                continue
            if cmd.qualified_name not in seen:
                seen.add(cmd.qualified_name)
                displayed.append(cmd)
        return sorted(displayed, key=lambda c: c.qualified_name)

    async def show_home(self, interaction: discord.Interaction) -> None:
        self.mode = "home"
        self.selected_group = None
        self.current_cog = None
        self.current_page = 0
        self.total_pages = 1
        self._update_components()
        await interaction.response.edit_message(view=self)

    async def show_group(self, interaction: discord.Interaction, group_name: str) -> None:
        if group_name == "other":
            commands_list = self.uncategorized
        else:
            commands_list = []
            for cog in self._grouped_cogs().get(group_name, []):
                commands_list.extend(self.mapping.get(cog, []))
        if not self._get_display_commands(commands_list):
            await self.show_home(interaction)
            return
        self.mode = "group"
        self.selected_group = group_name
        self.current_cog = None
        self.current_page = 0
        self.total_pages = 1
        self._update_components()
        await interaction.response.edit_message(view=self)

    async def build_cog_view(self, cog: commands.Cog | None) -> None:
        """Switch the view into cog mode and render its opening page."""
        self.mode = "cog"
        self.current_cog = cog
        self.selected_group = _cog_group_name(cog) if cog is not None else None
        self.current_page = 0
        self._update_components()

    async def show_cog(self, interaction: discord.Interaction, cog: commands.Cog | None) -> None:
        await self.build_cog_view(cog)
        await interaction.response.edit_message(view=self)

    async def show_command(self, interaction: discord.Interaction, command: commands.Command) -> None:
        self.mode = "command"
        self.total_pages = 1
        self._set_content(
            await _build_command_text(
                command=command,
                index=self.index,
                author=self.author,
                prefix=self.prefix,
            )
        )
        self.container.accent_colour = HELP_COMMAND_ACCENT_COLOR
        self._update_components()
        await interaction.response.edit_message(view=self)

    async def go_to_page(self, interaction: discord.Interaction, page: int) -> None:
        self.current_page = max(0, min(page, self.total_pages - 1))
        self._update_components()
        await interaction.response.edit_message(view=self)

    async def change_page(self, interaction: discord.Interaction, delta: int) -> None:
        await self.go_to_page(interaction, self.current_page + delta)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "You cannot use this menu. Use `/help` to open your own.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message:
            with suppress(discord.HTTPException):
                for child in self.walk_children():
                    if hasattr(child, "disabled"):
                        child.disabled = True
                await self.message.edit(view=self)


class HelpCategorySelect(discord.ui.Select):
    def __init__(
        self,
        help_view: HelpView,
        options: list[discord.SelectOption],
        placeholder: str = "Choose a command category...",
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]

        if value.startswith("group:"):
            group_name = value.split(":", 1)[1]
            await self.help_view.show_group(interaction, group_name)
            return

        if value.startswith("cog:"):
            cog_name = value.split(":", 1)[1]
            if cog_name == "none":
                await self.help_view.show_cog(interaction, None)
                return

            cog = self.help_view.bot.get_cog(cog_name)
            if cog:
                await self.help_view.show_cog(interaction, cog)
            else:
                await self.help_view.show_home(interaction)
            return

        if value.startswith("cmd:"):
            cmd_name = value.split(":", 1)[1]
            command = self.help_view.bot.get_command(cmd_name)
            if command:
                await self.help_view.show_command(interaction, command)
            else:
                await self.help_view.show_home(interaction)
            return

        await self.help_view.show_home(interaction)


class HelpSearchModal(discord.ui.Modal, title="Search Commands"):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__()
        self.help_view = help_view
        self.query = discord.ui.TextInput(
            label="Search Query",
            placeholder="Type a command name or keyword...",
            min_length=1,
            max_length=50,
            required=True,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        results = await self.help_view.index.search(self.query.value, interaction.user)
        visible = [cmd for cmd in results if not _is_group(cmd) or not getattr(cmd, "commands", None)]
        help_view = self.help_view

        if visible:
            lines = [
                f"{help_view.index.get_command_mention(cmd.qualified_name, prefix=help_view.prefix)}"
                f"\n-# {_get_command_description(cmd)}"
                for cmd in visible
            ]
            content = f"**Search Results: `{self.query.value}`**\n\n" + "\n\n".join(lines)
            content += f"\n\n-# {len(visible)} result(s) found • Amenity"
            help_view.container.accent_colour = HELP_ACCENT_COLOR
        else:
            content = f"No commands found matching `{self.query.value}`."
            help_view.container.accent_colour = discord.Color.red()

        help_view._set_content(content)
        help_view.current_cog = None
        help_view.current_page = 0
        help_view.total_pages = 0
        help_view.mode = "search"
        help_view._update_components()
        await interaction.response.edit_message(view=help_view)


class HelpFirstButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="<<",
            disabled=help_view.current_page <= 0,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.go_to_page(interaction, 0)


class HelpPrevButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="<",
            disabled=help_view.current_page <= 0,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.change_page(interaction, -1)


class HelpPageIndicator(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=f"{help_view.current_page + 1}/{max(help_view.total_pages, 1)}",
            disabled=True,
        )


class HelpNextButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=">",
            disabled=help_view.current_page >= help_view.total_pages - 1,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.change_page(interaction, 1)


class HelpLastButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label=">>",
            disabled=help_view.current_page >= help_view.total_pages - 1,
        )
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.go_to_page(interaction, self.help_view.total_pages - 1)


class HelpHomeButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(style=discord.ButtonStyle.primary, label="Home")
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.help_view.show_home(interaction)


class HelpSearchButton(discord.ui.Button):
    def __init__(self, help_view: HelpView) -> None:
        super().__init__(style=discord.ButtonStyle.secondary, label="Search", emoji=Emoji.SEARCH.value)
        self.help_view = help_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(HelpSearchModal(self.help_view))


class AmenityHelpCommand(commands.HelpCommand):
    def __init__(self) -> None:
        super().__init__(
            command_attrs={
                "help": "Show help about the bot, a category, or a specific command.",
                "cooldown": commands.CooldownMapping.from_cooldown(2, 5, commands.BucketType.user),
            }
        )

    def _get_index(self) -> HelpIndex:
        return HelpIndex(self.context.bot)

    async def send_bot_help(self, mapping: dict[commands.Cog | None, list[commands.Command]]) -> None:
        ctx = self.context
        index = self._get_index()

        cog_mapping = await index.visible_cogs(ctx.author)
        uncategorized = await index.visible_uncategorized(ctx.author)

        view = HelpView(
            bot=ctx.bot,
            index=index,
            author=ctx.author,
            prefix=ctx.clean_prefix,
            mapping=cog_mapping,
            uncategorized=uncategorized,
        )
        # Components V2: the message content lives inside the view itself.
        message = await ctx.send(view=view)
        view.message = message

    async def send_cog_help(self, cog: commands.Cog) -> None:
        ctx = self.context
        index = self._get_index()

        cog_mapping = await index.visible_cogs(ctx.author)
        uncategorized = await index.visible_uncategorized(ctx.author)

        if cog not in cog_mapping or cog.qualified_name.lower() in IGNORED_COGS:
            await self.send_error_message(f'No category called "{cog.qualified_name}" found.')
            return

        view = HelpView(
            bot=ctx.bot,
            index=index,
            author=ctx.author,
            prefix=ctx.clean_prefix,
            mapping=cog_mapping,
            uncategorized=uncategorized,
        )
        await view.build_cog_view(cog)
        message = await ctx.send(view=view)
        view.message = message

    async def send_group_help(self, group: commands.Group) -> None:
        await self.send_command_help(group)

    async def send_command_help(self, command: commands.Command) -> None:
        ctx = self.context
        index = self._get_index()

        if not await index.can_see_command(command, ctx.author):
            await ctx.send("Command not found.")
            return

        embed = await _build_command_embed(
            command=command,
            index=index,
            author=ctx.author,
            prefix=ctx.clean_prefix,
        )
        await ctx.send(embed=embed)

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
        if hasattr(self.bot, "fetch_app_commands"):
            await self.bot.fetch_app_commands()
        else:
            await self.index.fetch_app_command_ids_if_needed()

    @app_commands.command(name="help", description="Show the help menu")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(query="Command or category")
    async def help(self, interaction: discord.Interaction, query: str | None = None) -> None:
        index = self.index
        mapping = await index.visible_cogs(interaction.user)
        uncategorized = await index.visible_uncategorized(interaction.user)

        def make_view() -> HelpView:
            return HelpView(
                bot=self.bot,
                index=index,
                author=interaction.user,
                prefix="/",
                mapping=mapping,
                uncategorized=uncategorized,
            )

        if query:
            query = query.strip()
            if query.lower() in IGNORED_COGS:
                await interaction.response.send_message(
                    f"No command or category found matching `{query}`.",
                    ephemeral=True,
                )
                return

            for cog in mapping:
                if cog.qualified_name.lower() == query.lower() or _cog_display_name(cog).lower() == query.lower():
                    view = make_view()
                    await view.build_cog_view(cog)
                    await interaction.response.send_message(view=view, ephemeral=True)
                    view.message = await interaction.original_response()
                    return

            command = self.bot.get_command(query)
            if command and await index.can_see_command(command, interaction.user):
                embed = await _build_command_embed(
                    command=command,
                    index=index,
                    author=interaction.user,
                    prefix="/",
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            results = await index.search(query, interaction.user)
            visible = [cmd for cmd in results if not _is_group(cmd) or not getattr(cmd, "commands", None)]

            if visible:
                lines = [
                    f"{index.get_command_mention(cmd.qualified_name)}\n-# {_get_command_description(cmd)}"
                    for cmd in visible
                ]
                embed = discord.Embed(
                    title=f"Search Results: `{query}`",
                    description="\n\n".join(lines[:15]),
                    color=0x0EEDEB,
                )
                embed.set_footer(text=f"{len(visible)} result(s) found • Amenity")
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            await interaction.response.send_message(
                f"No command or category found matching `{query}`.",
                ephemeral=True,
            )
            return

        view = make_view()
        await interaction.response.send_message(view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
