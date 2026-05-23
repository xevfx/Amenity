import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

from discord import app_commands
from discord.ext import commands


def _annotation_to_str(annotation: object) -> str | None:
    if annotation is None or annotation is inspect._empty:
        return None
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return str(annotation)


def _serialize_cooldown(command: commands.Command) -> dict | None:
    bucket = getattr(command, "_buckets", None)
    cooldown = getattr(bucket, "_cooldown", None)
    if not cooldown:
        return None
    return {
        "rate": cooldown.rate,
        "per": cooldown.per,
        "type": str(getattr(cooldown, "type", None) or "") or None,
    }


def _serialize_prefix_params(command: commands.Command) -> list[dict]:
    params: list[dict] = []
    for name, param in command.clean_params.items():
        params.append(
            {
                "name": name,
                "kind": str(param.kind),
                "required": param.default is inspect._empty,
                "default": None if param.default is inspect._empty else param.default,
                "annotation": _annotation_to_str(param.annotation),
            }
        )
    return params


def _serialize_prefix_command(command: commands.Command) -> dict:
    cog = command.cog
    group_name = getattr(cog, "group_name", None) if cog else None
    display_name = getattr(cog, "display_name", None) if cog else None
    command_type = "prefix"
    if isinstance(command, commands.HybridCommand):
        command_type = "hybrid"
    return {
        "name": command.name,
        "qualified_name": command.qualified_name,
        "description": command.help or command.description or "",
        "usage": command.usage,
        "type": command_type,
        "aliases": list(command.aliases),
        "hidden": bool(command.hidden),
        "parent": command.parent.qualified_name if command.parent else None,
        "category": group_name or (cog.qualified_name if cog else None),
        "cog": display_name or (cog.qualified_name if cog else None),
        "cooldown": _serialize_cooldown(command),
        "params": _serialize_prefix_params(command),
    }


def _serialize_app_params(command: app_commands.Command) -> list[dict]:
    params: list[dict] = []
    for param in getattr(command, "parameters", []) or []:
        param_type = getattr(param, "type", None)
        params.append(
            {
                "name": param.name,
                "description": param.description,
                "required": getattr(param, "required", False),
                "type": getattr(param_type, "name", None) or str(param_type),
                "choices": [choice.name for choice in (param.choices or [])],
            }
        )
    return params


def _serialize_app_command(command: app_commands.Command) -> dict:
    cog = getattr(command, "binding", None)
    group_name = getattr(cog, "group_name", None) if cog else None
    display_name = getattr(cog, "display_name", None) if cog else None
    return {
        "name": command.name,
        "qualified_name": getattr(command, "qualified_name", command.name),
        "description": command.description or "",
        "type": "group" if isinstance(command, app_commands.Group) else "command",
        "parent": command.parent.qualified_name if command.parent else None,
        "category": group_name or (cog.qualified_name if cog else None),
        "cog": display_name or (cog.qualified_name if cog else None),
        "params": _serialize_app_params(command),
    }


def build_commands_payload(bot: commands.Bot) -> dict:
    prefix_commands = [_serialize_prefix_command(cmd) for cmd in bot.walk_commands()]
    app_commands_list = [
        _serialize_app_command(cmd) for cmd in bot.tree.walk_commands()
    ]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "prefix": len([c for c in prefix_commands if c["type"] == "prefix"]),
            "hybrid": len([c for c in prefix_commands if c["type"] == "hybrid"]),
            "app": len(app_commands_list),
            "total": len(prefix_commands) + len(app_commands_list),
        },
        "commands": {
            "prefix": prefix_commands,
            "app": app_commands_list,
        },
    }


def export_commands(bot: commands.Bot, output_path: str = "docs/commands.json") -> dict:
    payload = build_commands_payload(bot)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return payload
