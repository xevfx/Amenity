from __future__ import annotations

import ast
import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import discord
import psutil
import pytest

from cogs.fun import Fun
from cogs.games import Games
from cogs.github import Github
from cogs.server_utility import ServerUtility
from cogs.tools import Tools
from cogs.user_utility import UserUtility

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMAND_SOURCES = [*sorted((PROJECT_ROOT / "cogs").glob("*.py")), PROJECT_ROOT / "core" / "help.py"]
ARTIFACT_DIR = PROJECT_ROOT / "artifacts"
REPORT_PATH = ARTIFACT_DIR / "command-workflow-report.json"
MAX_CASE_SECONDS = float(os.getenv("COMMAND_TEST_MAX_SECONDS", "5.0"))
MAX_RSS_DELTA_MB = float(os.getenv("COMMAND_TEST_MAX_RSS_DELTA_MB", "128.0"))


@dataclass(frozen=True)
class CommandRecord:
    qualified_name: str
    module: str
    function: str
    decorator: str
    line: int
    hidden: bool


@dataclass(frozen=True)
class CaseMetric:
    name: str
    wall_ms: float
    cpu_ms: float
    rss_delta_kb: float
    message_count: int
    deferred: bool


class FakeAsset:
    def __init__(self, url: str = "https://cdn.example.test/avatar.png") -> None:
        self.url = url

    def with_format(self, _format: str) -> FakeAsset:
        return self

    def with_size(self, _size: int) -> FakeAsset:
        return self


class FakeMessage:
    def __init__(self, **payload: object) -> None:
        self.payload = payload
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> FakeMessage:
        self.edits.append(kwargs)
        self.payload.update(kwargs)
        return self


class FakeTree:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def add_command(self, command: object) -> None:
        self.commands.append(command)

    def remove_command(self, name: str, *, type: object | None = None) -> None:
        self.commands = [
            command
            for command in self.commands
            if not (getattr(command, "name", None) == name and (type is None or getattr(command, "type", None) == type))
        ]


class FakeBot:
    def __init__(self) -> None:
        self.latency = 0.042
        self.loop = asyncio.get_running_loop()
        self.tree = FakeTree()
        self.user = FakeUser(999, "AmenityBot", bot=True)
        self._users: dict[int, FakeUser] = {self.user.id: self.user}

    def get_user(self, user_id: int) -> FakeUser | None:
        return self._users.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeUser:
        user = self._users.get(user_id) or FakeUser(user_id, f"user-{user_id}")
        self._users[user_id] = user
        return user


class FakeUser:
    def __init__(self, user_id: int = 123, name: str = "tester", *, bot: bool = False) -> None:
        self.id = user_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.mention = f"<@{user_id}>"
        self.bot = bot
        self.avatar = FakeAsset()
        self.default_avatar = FakeAsset("https://cdn.example.test/default.png")
        self.display_avatar = FakeAsset()
        self.banner = None
        self.accent_color = discord.Color.blurple()
        self.created_at = datetime.now(UTC) - timedelta(days=365)
        self.public_flags = SimpleNamespace()
        self.premium_type = None

    def __str__(self) -> str:
        return self.name


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.author = FakeUser()
        self.guild = None
        self.channel = SimpleNamespace(id=321)
        self.interaction = None
        self.command = None
        self.invoked_subcommand = None
        self.prefix = "/"
        self.messages: list[dict[str, object]] = []
        self.deferred = False

    async def send(self, content: str | None = None, **kwargs: object) -> FakeMessage:
        return self._record("send", content=content, **kwargs)

    async def reply(self, content: str | None = None, **kwargs: object) -> FakeMessage:
        return self._record("reply", content=content, **kwargs)

    async def defer(self, **kwargs: object) -> None:
        self.deferred = True
        self.messages.append({"method": "defer", **kwargs})

    async def send_help(self, command: object = None) -> FakeMessage:
        return self._record("send_help", command=command)

    def _record(self, method: str, **payload: object) -> FakeMessage:
        payload["method"] = method
        self.messages.append(payload)
        return FakeMessage(**payload)


def _decorator_path(decorator: ast.expr) -> list[str]:
    node = decorator.func if isinstance(decorator, ast.Call) else decorator
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


def _decorator_name(decorator: ast.expr, fallback: str) -> str:
    if isinstance(decorator, ast.Call):
        for keyword in decorator.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
        if decorator.args and isinstance(decorator.args[0], ast.Constant):
            return str(decorator.args[0].value)
    return fallback.replace("_", "-")


def _decorator_bool_kw(decorator: ast.expr, name: str, default: bool = False) -> bool:
    if not isinstance(decorator, ast.Call):
        return default
    for keyword in decorator.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return bool(keyword.value.value)
    return default


def discover_commands() -> list[CommandRecord]:
    records: list[CommandRecord] = []
    for source in COMMAND_SOURCES:
        tree = ast.parse(source.read_text(), filename=str(source))
        public_names: dict[str, str] = {}
        direct_commands: dict[str, str] = {}
        function_nodes = [
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        ]

        for node in function_nodes:
            for decorator in node.decorator_list:
                full_path = ".".join(_decorator_path(decorator))
                if full_path in {
                    "commands.hybrid_command",
                    "commands.command",
                    "app_commands.command",
                    "commands.hybrid_group",
                    "commands.group",
                }:
                    public_names[node.name] = _decorator_name(decorator, node.name)
                    direct_commands[node.name] = full_path

        for node in function_nodes:
            for decorator in node.decorator_list:
                parts = _decorator_path(decorator)
                full_path = ".".join(parts)
                hidden = _decorator_bool_kw(decorator, "hidden")
                if node.name in direct_commands and full_path == direct_commands[node.name]:
                    records.append(
                        CommandRecord(
                            qualified_name=public_names[node.name],
                            module=str(source.relative_to(PROJECT_ROOT)),
                            function=node.name,
                            decorator=full_path,
                            line=node.lineno,
                            hidden=hidden,
                        )
                    )
                elif len(parts) == 2 and parts[1] in {"command", "group"} and parts[0] in public_names:
                    records.append(
                        CommandRecord(
                            qualified_name=f"{public_names[parts[0]]} {_decorator_name(decorator, node.name)}",
                            module=str(source.relative_to(PROJECT_ROOT)),
                            function=node.name,
                            decorator=full_path,
                            line=node.lineno,
                            hidden=hidden,
                        )
                    )
    return sorted(records, key=lambda record: (record.module, record.qualified_name, record.line))


def classify_command(record: CommandRecord) -> str:
    name = record.qualified_name
    if record.hidden or record.module == "cogs/owner.py":
        return "owner-metadata"
    if name in BEHAVIOR_COMMANDS:
        return "offline-behavior"
    if record.module in {"cogs/ai.py", "cogs/crypto.py", "cogs/github.py"}:
        return "external-api-metadata"
    if name.startswith(("image ", "meme ")) or name in {"blackjack", "tic-tac-toe", "enlarge", "deco"}:
        return "interactive-or-asset-metadata"
    if name.startswith(("reminder", "template", "qr", "server", "role", "member", "channel")):
        return "stateful-or-discord-object-metadata"
    return "metadata"


BEHAVIOR_COMMANDS = {
    "ping",
    "avatar",
    "userinfo",
    "say",
    "say in-embed",
    "encrypt",
    "decrypt",
    "2fa",
    "sanitize-url",
    "query-link",
    "checkusers",
    "replace",
    "math",
    "binary",
    "coinflip",
    "mines",
    "server info",
    "gay-rate",
    "faker",
    "github user",
    "github repo",
}


async def _run_case(name: str, func: Callable[[FakeContext], Awaitable[None]]) -> CaseMetric:
    process = psutil.Process()
    ctx = FakeContext(FakeBot())
    mem_before = process.memory_info().rss
    cpu_before = process.cpu_times()
    start = time.perf_counter()
    await func(ctx)
    wall_ms = (time.perf_counter() - start) * 1000
    cpu_after = process.cpu_times()
    mem_after = process.memory_info().rss
    metric = CaseMetric(
        name=name,
        wall_ms=round(wall_ms, 3),
        cpu_ms=round(((cpu_after.user + cpu_after.system) - (cpu_before.user + cpu_before.system)) * 1000, 3),
        rss_delta_kb=round((mem_after - mem_before) / 1024, 3),
        message_count=len(ctx.messages),
        deferred=ctx.deferred,
    )
    assert ctx.messages, f"{name} did not send, reply, or defer"
    assert wall_ms / 1000 <= MAX_CASE_SECONDS, f"{name} took {wall_ms:.1f}ms"
    assert abs(metric.rss_delta_kb) / 1024 <= MAX_RSS_DELTA_MB, f"{name} changed RSS by {metric.rss_delta_kb:.1f}KB"
    return metric


@pytest.mark.asyncio
async def test_offline_command_behavior_and_metrics() -> None:
    async def with_user_utility(ctx: FakeContext, call: Callable[[UserUtility, FakeContext], Awaitable[None]]) -> None:
        cog = UserUtility(ctx.bot)
        try:
            await call(cog, ctx)
        finally:
            cog.cog_unload()

    async def with_tools(ctx: FakeContext, call: Callable[[Tools, FakeContext], Awaitable[None]]) -> None:
        cog = Tools(ctx.bot)
        try:
            await call(cog, ctx)
        finally:
            cog.cog_unload()

    async def with_fun(ctx: FakeContext, call: Callable[[Fun, FakeContext], Awaitable[None]]) -> None:
        cog = Fun(ctx.bot)
        try:
            await call(cog, ctx)
        finally:
            cog.cog_unload()

    async def with_github(ctx: FakeContext, call: Callable[[Github, FakeContext], Awaitable[None]]) -> None:
        cog = Github(ctx.bot)
        try:
            await call(cog, ctx)
        finally:
            cog.cog_unload()

    async def github_user(cog: Github, ctx: FakeContext) -> None:
        async def fake_fetch(url: str) -> tuple[dict[str, Any], int]:
            return {
                "login": "octocat",
                "name": "Octocat",
                "bio": "Test account",
                "avatar_url": "https://example.test/octocat.png",
                "html_url": "https://github.com/octocat",
                "public_repos": 8,
                "location": "Internet",
                "company": "GitHub",
                "followers": 42,
                "blog": "https://example.test",
            }, 200

        cog._fetch_json = fake_fetch  # type: ignore[method-assign]
        await cog.github_user.callback(cog, ctx, user="octocat")

    async def github_repo(cog: Github, ctx: FakeContext) -> None:
        async def fake_fetch(url: str) -> tuple[dict[str, Any], int]:
            return {
                "items": [
                    {
                        "full_name": "octocat/hello-world",
                        "html_url": "https://github.com/octocat/hello-world",
                        "description": "Fixture repo",
                        "owner": {"avatar_url": "https://example.test/octocat.png"},
                        "stargazers_count": 100,
                        "forks_count": 10,
                        "language": "Python",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                ]
            }, 200

        cog._fetch_json = fake_fetch  # type: ignore[method-assign]
        await cog.reposearch.callback(cog, ctx, query="hello")

    cases: dict[str, Callable[[FakeContext], Awaitable[None]]] = {
        "ping": lambda ctx: with_user_utility(ctx, lambda cog, c: cog.ping.callback(cog, c)),
        "avatar": lambda ctx: with_user_utility(ctx, lambda cog, c: cog.avatar.callback(cog, c)),
        "userinfo": lambda ctx: with_user_utility(ctx, lambda cog, c: cog.userinfo.callback(cog, c)),
        "math": lambda ctx: with_user_utility(ctx, lambda cog, c: cog.math.callback(cog, c, expression="2 + 2 * 3")),
        "binary": lambda ctx: with_user_utility(ctx, lambda cog, c: cog.binary.callback(cog, c, message="hello")),
        "say": lambda ctx: with_tools(ctx, lambda cog, c: cog.say_group.callback(cog, c, message="hello")),
        "say in-embed": lambda ctx: with_tools(ctx, lambda cog, c: cog.say_in_embed.callback(cog, c, message="hello")),
        "encrypt": lambda ctx: with_tools(
            ctx, lambda cog, c: cog.encrypt.callback(cog, c, method="base64", key=None, text="hello")
        ),
        "decrypt": lambda ctx: with_tools(
            ctx, lambda cog, c: cog.decrypt.callback(cog, c, method="base64", key=None, text="aGVsbG8=")
        ),
        "2fa": lambda ctx: with_tools(ctx, lambda cog, c: cog.two_factor_code.callback(cog, c, secret="invalid")),
        "sanitize-url": lambda ctx: with_tools(
            ctx, lambda cog, c: cog.sanitize_url.callback(cog, c, url="https://example.com/?utm_source=x&id=1")
        ),
        "query-link": lambda ctx: with_tools(
            ctx, lambda cog, c: cog.query_link.callback(cog, c, engine="google", query="amenity bot")
        ),
        "checkusers": lambda ctx: with_tools(ctx, lambda cog, c: cog.checkusers.callback(cog, c, users="", days=90)),
        "replace": lambda ctx: with_tools(
            ctx, lambda cog, c: cog.replace_text.callback(cog, c, from_="old", to="new", text="old text")
        ),
        "coinflip": lambda ctx: Games(ctx.bot).coin_flip.callback(Games(ctx.bot), ctx, side="heads"),
        "mines": lambda ctx: Games(ctx.bot).mines.callback(Games(ctx.bot), ctx, mines=1),
        "server info": lambda ctx: ServerUtility(ctx.bot).server_info.callback(ServerUtility(ctx.bot), ctx),
        "gay-rate": lambda ctx: with_fun(ctx, lambda cog, c: cog.gayrate.callback(cog, c)),
        "faker": lambda ctx: with_fun(ctx, lambda cog, c: cog.faker_cmd.callback(cog, c, country="US")),
        "github user": lambda ctx: with_github(ctx, github_user),
        "github repo": lambda ctx: with_github(ctx, github_repo),
    }

    metrics = [await _run_case(name, case) for name, case in sorted(cases.items())]
    ARTIFACT_DIR.mkdir(exist_ok=True)
    catalog = discover_commands()
    REPORT_PATH.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "thresholds": {
                    "max_case_seconds": MAX_CASE_SECONDS,
                    "max_rss_delta_mb": MAX_RSS_DELTA_MB,
                },
                "summary": {
                    "discovered_commands": len(catalog),
                    "behavior_cases": len(metrics),
                    "total_wall_ms": round(sum(metric.wall_ms for metric in metrics), 3),
                    "max_wall_ms": max(metric.wall_ms for metric in metrics),
                    "max_rss_delta_kb": max(abs(metric.rss_delta_kb) for metric in metrics),
                },
                "commands": [asdict(record) | {"test_plan": classify_command(record)} for record in catalog],
                "metrics": [asdict(metric) for metric in metrics],
            },
            indent=2,
            sort_keys=True,
        )
    )


def test_every_command_has_a_test_plan() -> None:
    catalog = discover_commands()
    assert catalog, "No commands were discovered"

    qualified_names = [record.qualified_name for record in catalog]
    duplicates = sorted({name for name in qualified_names if qualified_names.count(name) > 1})
    assert not duplicates, f"Duplicate command names discovered: {duplicates}"

    missing_behavior_markers = sorted(BEHAVIOR_COMMANDS - set(qualified_names))
    assert not missing_behavior_markers, f"Behavior scenarios point at missing commands: {missing_behavior_markers}"

    unplanned = [record.qualified_name for record in catalog if not classify_command(record)]
    assert not unplanned, f"Commands without a test plan: {unplanned}"

    public_behavior_commands = {
        record.qualified_name for record in catalog if classify_command(record) == "offline-behavior"
    }
    assert public_behavior_commands == BEHAVIOR_COMMANDS


def test_command_catalog_quality() -> None:
    for record in discover_commands():
        assert record.qualified_name.strip(), f"{record.module}:{record.line} has an empty command name"
        assert "  " not in record.qualified_name, f"{record.qualified_name} has repeated spaces"
        assert record.function.strip(), f"{record.qualified_name} has no backing function"
