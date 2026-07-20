import json
import os
import re
import sqlite3
import time as tm
from contextlib import suppress
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from api.buttons import confirm_action
from api.emojis import Emoji
from api.log import log_command_error
from core.amenity import Amenity
from core.cache import cache

MAX_TEMPLATE_NAME = 80
MAX_CONTENT_LENGTH = 2000
MAX_BUTTONS = 5
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class Template(commands.Cog):
    display_name = "Templates"
    group_name = "Templates"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data/templates.db"))
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    embed_json TEXT,
                    buttons_json TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(user_id, name)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS templates_user_id ON templates (user_id)")

    def _cache_key(self, user_id: int) -> str:
        return f"templates:{user_id}"

    def _fetch_user_templates(self, user_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, content, embed_json, buttons_json, created_at, updated_at
                FROM templates
                WHERE user_id = ?
                ORDER BY name COLLATE NOCASE
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_template(row) for row in rows]

    def _get_user_templates(self, user_id: int) -> list[dict[str, Any]]:
        return cache.get_or_set(
            self._cache_key(user_id),
            lambda: self._fetch_user_templates(user_id),
            ttl=60,
        )

    def _invalidate_user_cache(self, user_id: int) -> None:
        cache.delete(self._cache_key(user_id))

    def _row_to_template(self, row: sqlite3.Row) -> dict[str, Any]:
        embed_data = json.loads(row["embed_json"]) if row["embed_json"] else None
        buttons = json.loads(row["buttons_json"] or "[]")
        return {
            "id": row["id"],
            "name": row["name"],
            "content": row["content"] or "",
            "embed": embed_data,
            "buttons": buttons,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_template_by_name_or_id(self, user_id: int, name: str) -> dict[str, Any] | None:
        name = name.strip()
        if name.startswith("id:") and name[3:].strip().isdigit():
            template_id = int(name[3:].strip())
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, name, content, embed_json, buttons_json, created_at, updated_at
                    FROM templates
                    WHERE id = ? AND user_id = ?
                    """,
                    (template_id, user_id),
                ).fetchone()
        else:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT id, name, content, embed_json, buttons_json, created_at, updated_at
                    FROM templates
                    WHERE name = ? AND user_id = ?
                    """,
                    (name, user_id),
                ).fetchone()
        return self._row_to_template(row) if row else None

    def _save_template(self, user_id: int, name: str, data: dict[str, Any]) -> None:
        now = int(tm.time())
        embed_json = json.dumps(data["embed"], separators=(",", ":")) if data.get("embed") else None
        buttons_json = json.dumps(data.get("buttons") or [], separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO templates (user_id, name, content, embed_json, buttons_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET
                    content = excluded.content,
                    embed_json = excluded.embed_json,
                    buttons_json = excluded.buttons_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, name, data.get("content") or "", embed_json, buttons_json, now, now),
            )
        self._invalidate_user_cache(user_id)

    async def _send_embed(
        self,
        ctx: commands.Context,
        description: str,
        title: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        embed = discord.Embed(description=description)
        if title:
            embed.title = title
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=ephemeral)
            return
        await ctx.send(embed=embed)

    def _template_choices(self, user_id: int, current: str) -> list[app_commands.Choice[str]]:
        templates = self._get_user_templates(user_id)
        current_lower = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for template in templates:
            name = template["name"]
            if current_lower and current_lower not in name.lower():
                continue
            if len(name) > 100:
                display_name = f"{name[:97]}..."
                value = f"id:{template['id']}"
            else:
                display_name = name
                value = name
            choices.append(app_commands.Choice(name=display_name, value=value))
            if len(choices) >= 25:
                break
        return choices

    @commands.hybrid_group(name="template", description="Manage message templates", aliases=["tpl"])
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await self._send_embed(
                ctx,
                "Use `/template create`, `/template edit`, `/template delete`, `/template send`, "
                "`/template list`, or `/template nuke`.",
                ephemeral=True,
            )

    @template.command(name="create", description="Create a message template", aliases=["c"])
    @app_commands.describe(name="Template name")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_create(self, ctx: commands.Context, name: str) -> None:
        name = name.strip()
        if not await self._validate_template_name(ctx, name):
            return
        if self._get_template_by_name_or_id(ctx.author.id, name):
            await self._send_embed(
                ctx,
                "A template with that name already exists. Use `/template edit`.",
                ephemeral=True,
            )
            return
        await self._open_builder(ctx, name, {"content": "", "embed": None, "buttons": []}, creating=True)

    @template.command(name="edit", description="Edit a message template", aliases=["e"])
    @app_commands.describe(name="Template name")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_edit(self, ctx: commands.Context, name: str) -> None:
        template = self._get_template_by_name_or_id(ctx.author.id, name)
        if not template:
            await self._send_embed(ctx, "Template not found.", ephemeral=True)
            return
        await self._open_builder(
            ctx,
            template["name"],
            {
                "content": template["content"],
                "embed": template["embed"],
                "buttons": template["buttons"],
            },
            creating=False,
        )

    @template_edit.autocomplete("name")
    async def template_edit_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self._template_choices(interaction.user.id, current)

    @template.command(name="list", description="List and preview your templates", aliases=["ls"])
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_list(self, ctx: commands.Context) -> None:
        templates = self._get_user_templates(ctx.author.id)
        if not templates:
            await self._send_embed(ctx, "You have no templates.", ephemeral=True)
            return
        view = TemplateListView(ctx.author.id, templates)
        embed = view.build_index_embed()
        if ctx.interaction:
            await ctx.interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        await ctx.send(embed=embed, view=view)

    @template.command(name="send", description="Send one of your templates", aliases=["s"])
    @app_commands.describe(name="Template name")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_send(self, ctx: commands.Context, name: str) -> None:
        template = self._get_template_by_name_or_id(ctx.author.id, name)
        if not template:
            await self._send_embed(ctx, "Template not found.", ephemeral=True)
            return
        content, embed, view = build_template_payload(template)
        message_kwargs: dict[str, Any] = {"content": content, "embed": embed}
        if view is not None:
            message_kwargs["view"] = view
        if ctx.interaction:
            await ctx.interaction.response.send_message(**message_kwargs)
            return
        await ctx.send(**message_kwargs)

    @template_send.autocomplete("name")
    async def template_send_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self._template_choices(interaction.user.id, current)

    @template.command(name="delete", description="Delete a template", aliases=["del"])
    @app_commands.describe(name="Template name")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_delete(self, ctx: commands.Context, name: str) -> None:
        template = self._get_template_by_name_or_id(ctx.author.id, name)
        if not template:
            await self._send_embed(ctx, "Template not found.", ephemeral=True)
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM templates WHERE id = ? AND user_id = ?", (template["id"], ctx.author.id))
        self._invalidate_user_cache(ctx.author.id)
        await self._send_embed(ctx, f"Deleted template `{template['name']}`.", ephemeral=True)

    @template_delete.autocomplete("name")
    async def template_delete_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        return self._template_choices(interaction.user.id, current)

    @template.command(name="nuke", description="Delete all your templates")
    @commands.cooldown(1, 5, commands.BucketType.user)
    @commands.max_concurrency(10, commands.BucketType.default, wait=True)
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def template_nuke(self, ctx: commands.Context) -> None:
        confirmed = await confirm_action(
            ctx,
            "This will delete all of your templates. Continue?",
            confirm_label="Delete",
            cancel_label="Cancel",
            confirm_style=discord.ButtonStyle.danger,
            cancel_style=discord.ButtonStyle.secondary,
            timeout=20.0,
            ephemeral=True,
            confirm_message="Deleting templates...",
            cancel_message="Canceled.",
            timeout_message="Timed out.",
        )
        if not confirmed:
            return
        try:
            with self._connect() as conn:
                cursor = conn.execute("DELETE FROM templates WHERE user_id = ?", (ctx.author.id,))
            self._invalidate_user_cache(ctx.author.id)
            await self._send_embed(ctx, f"Deleted {cursor.rowcount} templates.", ephemeral=True)
        except Exception as exc:
            await self._send_embed(ctx, "Error deleting templates.", ephemeral=True)
            await log_command_error(ctx, exc)

    async def _validate_template_name(self, ctx: commands.Context, name: str) -> bool:
        if not name:
            await self._send_embed(ctx, "Template name is required.", ephemeral=True)
            return False
        if len(name) > MAX_TEMPLATE_NAME:
            await self._send_embed(
                ctx,
                f"Template name is too long (max {MAX_TEMPLATE_NAME} characters).",
                ephemeral=True,
            )
            return False
        if name.startswith("id:"):
            await self._send_embed(ctx, "Template names cannot start with `id:`.", ephemeral=True)
            return False
        return True

    async def _open_builder(
        self,
        ctx: commands.Context,
        name: str,
        data: dict[str, Any],
        *,
        creating: bool,
    ) -> None:
        view = TemplateBuilderView(self, ctx.author.id, name, data)
        embed = view.build_preview_embed(creating=creating)
        if ctx.interaction:
            await ctx.interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return
        await ctx.send(embed=embed, view=view)


def build_template_payload(template: dict[str, Any]) -> tuple[str | None, discord.Embed | None, discord.ui.View | None]:
    content = template.get("content") or None
    embed = discord.Embed.from_dict(template["embed"]) if template.get("embed") else None
    buttons = template.get("buttons") or []
    view = discord.ui.View(timeout=None) if buttons else None
    if view:
        add_link_buttons(view, buttons)
    return content, embed, view


def add_link_buttons(view: discord.ui.View, buttons: list[dict[str, str]], *, row: int | None = None) -> None:
    for button in buttons[:MAX_BUTTONS]:
        view.add_item(
            discord.ui.Button(
                label=button["label"],
                url=button["url"],
                style=discord.ButtonStyle.link,
                row=row,
            )
        )


class TemplateListView(discord.ui.View):
    def __init__(self, author_id: int, templates: list[dict[str, Any]]) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.templates = templates
        self.add_item(TemplateSelect(templates))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This template list is not for you.", ephemeral=True)
            return False
        return True

    def build_index_embed(self) -> discord.Embed:
        lines = [f"`{template['name']}`" for template in self.templates[:25]]
        embed = discord.Embed(
            title=f"{Emoji.COMMAND.value} Your templates",
            description="\n".join(lines),
        )
        embed.set_footer(text=f"{len(self.templates)} template(s)")
        return embed


class TemplateSelect(discord.ui.Select):
    def __init__(self, templates: list[dict[str, Any]]) -> None:
        self.templates_by_id = {str(template["id"]): template for template in templates[:25]}
        self.templates = templates
        options = [
            discord.SelectOption(
                label=template["name"][:100],
                value=str(template["id"]),
                description=template_preview_description(template),
            )
            for template in templates[:25]
        ]
        super().__init__(placeholder="View a template", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        template = self.templates_by_id[self.values[0]]
        content, embed, view = build_template_payload(template)
        if embed is None:
            embed = discord.Embed(title=f"Template: {template['name']}", description=content or "Empty template.")
            content = None
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=True,
        )


class TemplateBuilderView(discord.ui.View):
    def __init__(self, cog: Template, author_id: int, name: str, data: dict[str, Any]) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.author_id = author_id
        self.name = name
        self.data = {
            "content": data.get("content") or "",
            "embed": data.get("embed"),
            "buttons": data.get("buttons") or [],
        }
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This template builder is not for you.", ephemeral=True)
            return False
        return True

    def build_preview_embed(self, *, creating: bool = False) -> discord.Embed:
        embed = discord.Embed(title=f"{'Create' if creating else 'Edit'} template: {self.name}")
        content = self.data.get("content") or ""
        embed.add_field(name="Text", value=content[:1024] if content else "None", inline=False)
        embed.add_field(name="Embed", value="Enabled" if self.data.get("embed") else "None", inline=True)
        embed.add_field(name="Buttons", value=str(len(self.data.get("buttons") or [])), inline=True)
        embed.set_footer(text="Use the buttons below to build, preview, and save.")
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.build_preview_embed(), view=self)

    @discord.ui.button(label="Text", style=discord.ButtonStyle.secondary)
    async def text_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TemplateTextModal(self))

    @discord.ui.button(label="Embed", style=discord.ButtonStyle.secondary)
    async def embed_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TemplateEmbedModal(self))

    @discord.ui.button(label="Media", style=discord.ButtonStyle.secondary)
    async def media_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TemplateMediaModal(self))

    @discord.ui.button(label="Author/Footer", style=discord.ButtonStyle.secondary)
    async def author_footer_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TemplateAuthorFooterModal(self))

    @discord.ui.button(label="Buttons", style=discord.ButtonStyle.secondary)
    async def buttons_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(TemplateButtonsModal(self))

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.primary)
    async def preview_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        content, embed, view = build_template_payload(self.data)
        if content is None and embed is None:
            await interaction.response.send_message("Add text or an embed before previewing.", ephemeral=True)
            return
        message_kwargs: dict[str, Any] = {"content": content, "embed": embed, "ephemeral": True}
        if view is not None:
            message_kwargs["view"] = view
        await interaction.response.send_message(**message_kwargs)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.data.get("content") and not self.data.get("embed"):
            await interaction.response.send_message("Add text or an embed before saving.", ephemeral=True)
            return
        self.cog._save_template(self.author_id, self.name, self.data)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(description=f"Saved template `{self.name}`."),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=discord.Embed(description="Canceled."), view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class TemplateTextModal(discord.ui.Modal):
    def __init__(self, builder: TemplateBuilderView) -> None:
        super().__init__(title="Template text")
        self.builder = builder
        self.content = discord.ui.TextInput(
            label="Message text",
            style=discord.TextStyle.paragraph,
            max_length=MAX_CONTENT_LENGTH,
            required=False,
            default=builder.data.get("content") or "",
        )
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.builder.data["content"] = self.content.value.strip()
        await self.builder.refresh(interaction)


class TemplateEmbedModal(discord.ui.Modal):
    def __init__(self, builder: TemplateBuilderView) -> None:
        super().__init__(title="Embed content")
        self.builder = builder
        data = builder.data.get("embed") or {}
        self.title_input = discord.ui.TextInput(
            label="Title",
            max_length=256,
            required=False,
            default=data.get("title") or "",
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=4000,
            required=False,
            default=data.get("description") or "",
        )
        self.color_input = discord.ui.TextInput(
            label="Color hex",
            placeholder="#5865F2",
            max_length=7,
            required=False,
            default=dict_color_to_hex(data.get("color")),
        )
        self.url_input = discord.ui.TextInput(
            label="Title URL",
            max_length=300,
            required=False,
            default=data.get("url") or "",
        )
        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.color_input)
        self.add_item(self.url_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = self.builder.data.get("embed") or {}
        set_or_delete(embed, "title", self.title_input.value.strip())
        set_or_delete(embed, "description", self.description_input.value.strip())
        color = self.color_input.value.strip()
        if color:
            try:
                embed["color"] = int(color.removeprefix("#"), 16)
            except ValueError:
                await interaction.response.send_message(
                    "Color must be a valid hex value like `#5865F2`.",
                    ephemeral=True,
                )
                return
        else:
            embed.pop("color", None)
        url = self.url_input.value.strip()
        if url and not URL_RE.match(url):
            await interaction.response.send_message(
                "Title URL must start with `http://` or `https://`.",
                ephemeral=True,
            )
            return
        set_or_delete(embed, "url", url)
        self.builder.data["embed"] = embed if embed_has_content(embed) else None
        await self.builder.refresh(interaction)


class TemplateMediaModal(discord.ui.Modal):
    def __init__(self, builder: TemplateBuilderView) -> None:
        super().__init__(title="Embed media")
        self.builder = builder
        data = builder.data.get("embed") or {}
        self.image = discord.ui.TextInput(
            label="Image URL",
            max_length=300,
            required=False,
            default=(data.get("image") or {}).get("url") or "",
        )
        self.thumbnail = discord.ui.TextInput(
            label="Thumbnail URL",
            max_length=300,
            required=False,
            default=(data.get("thumbnail") or {}).get("url") or "",
        )
        self.add_item(self.image)
        self.add_item(self.thumbnail)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = self.builder.data.get("embed") or {}
        for key, value in (("image", self.image.value.strip()), ("thumbnail", self.thumbnail.value.strip())):
            if value and not URL_RE.match(value):
                await interaction.response.send_message(
                    f"{key.title()} URL must start with `http://` or `https://`.",
                    ephemeral=True,
                )
                return
            if value:
                embed[key] = {"url": value}
            else:
                embed.pop(key, None)
        self.builder.data["embed"] = embed if embed_has_content(embed) else None
        await self.builder.refresh(interaction)


class TemplateAuthorFooterModal(discord.ui.Modal):
    def __init__(self, builder: TemplateBuilderView) -> None:
        super().__init__(title="Embed author and footer")
        self.builder = builder
        data = builder.data.get("embed") or {}
        self.author = discord.ui.TextInput(
            label="Author name",
            max_length=256,
            required=False,
            default=(data.get("author") or {}).get("name") or "",
        )
        self.author_icon = discord.ui.TextInput(
            label="Author icon URL",
            max_length=300,
            required=False,
            default=(data.get("author") or {}).get("icon_url") or "",
        )
        self.footer = discord.ui.TextInput(
            label="Footer text",
            max_length=2048,
            required=False,
            default=(data.get("footer") or {}).get("text") or "",
        )
        self.footer_icon = discord.ui.TextInput(
            label="Footer icon URL",
            max_length=300,
            required=False,
            default=(data.get("footer") or {}).get("icon_url") or "",
        )
        self.add_item(self.author)
        self.add_item(self.author_icon)
        self.add_item(self.footer)
        self.add_item(self.footer_icon)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        embed = self.builder.data.get("embed") or {}
        author = self.author.value.strip()
        author_icon = self.author_icon.value.strip()
        footer = self.footer.value.strip()
        footer_icon = self.footer_icon.value.strip()
        for label, value in (("Author icon", author_icon), ("Footer icon", footer_icon)):
            if value and not URL_RE.match(value):
                await interaction.response.send_message(
                    f"{label} URL must start with `http://` or `https://`.",
                    ephemeral=True,
                )
                return
        if author:
            embed["author"] = {"name": author, **({"icon_url": author_icon} if author_icon else {})}
        else:
            embed.pop("author", None)
        if footer:
            embed["footer"] = {"text": footer, **({"icon_url": footer_icon} if footer_icon else {})}
        else:
            embed.pop("footer", None)
        self.builder.data["embed"] = embed if embed_has_content(embed) else None
        await self.builder.refresh(interaction)


class TemplateButtonsModal(discord.ui.Modal):
    def __init__(self, builder: TemplateBuilderView) -> None:
        super().__init__(title="Link buttons")
        self.builder = builder
        lines = [f"{button['label']} | {button['url']}" for button in builder.data.get("buttons", [])]
        self.buttons = discord.ui.TextInput(
            label="Buttons",
            placeholder="Label | https://example.com",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
            default="\n".join(lines),
        )
        self.add_item(self.buttons)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        buttons: list[dict[str, str]] = []
        for raw_line in self.buttons.value.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if "|" not in line:
                await interaction.response.send_message("Each button must use `Label | https://url`.", ephemeral=True)
                return
            label, url = [part.strip() for part in line.split("|", 1)]
            if not label or len(label) > 80:
                await interaction.response.send_message("Button labels must be 1-80 characters.", ephemeral=True)
                return
            if not URL_RE.match(url):
                await interaction.response.send_message(
                    "Button URLs must start with `http://` or `https://`.",
                    ephemeral=True,
                )
                return
            buttons.append({"label": label, "url": url})
            if len(buttons) > MAX_BUTTONS:
                await interaction.response.send_message(f"You can add up to {MAX_BUTTONS} buttons.", ephemeral=True)
                return
        self.builder.data["buttons"] = buttons
        await self.builder.refresh(interaction)


def template_preview_description(template: dict[str, Any]) -> str:
    parts = []
    if template.get("content"):
        parts.append("text")
    if template.get("embed"):
        parts.append("embed")
    if template.get("buttons"):
        parts.append(f"{len(template['buttons'])} button(s)")
    return ", ".join(parts)[:100] if parts else "empty"


def set_or_delete(data: dict[str, Any], key: str, value: str) -> None:
    if value:
        data[key] = value
    else:
        data.pop(key, None)


def dict_color_to_hex(color: object) -> str:
    if isinstance(color, int):
        return f"#{color:06X}"
    return ""


def embed_has_content(embed: dict[str, Any]) -> bool:
    content_keys = ("title", "description", "url", "color", "image", "thumbnail", "author", "footer")
    return any(key in embed for key in content_keys)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Template(bot))
