from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from api.log import log_exception
from api.paginator import EmbedPaginator

if TYPE_CHECKING:
    from core.amenity import Amenity

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[1] / ".github" / "ai" / "system.md"
PAGE_CHARS = 3500
PROMPT_PREVIEW_CHARS = 400
SUMMARY_MODEL_KEY = "groq/openai/gpt-oss-120b"
POLLINATIONS_IMAGE_MODEL = "flux"


@dataclass(frozen=True, slots=True)
class AIModel:
    key: str
    provider: str
    label: str
    model_id: str
    env_var: str | None


AI_MODELS: dict[str, AIModel] = {
    "google/gemini-2.5-flash": AIModel(
        key="google/gemini-2.5-flash",
        provider="Google",
        label="Gemini 2.5 Flash",
        model_id="gemini-2.5-flash",
        env_var="GOOGLE",
    ),
    "groq/openai/gpt-oss-120b": AIModel(
        key="groq/openai/gpt-oss-120b",
        provider="Groq",
        label="GPT OSS 120B",
        model_id="openai/gpt-oss-120b",
        env_var="GROQ",
    ),
    "groq/openai/gpt-oss-20b": AIModel(
        key="groq/openai/gpt-oss-20b",
        provider="Groq",
        label="GPT OSS 20B",
        model_id="openai/gpt-oss-20b",
        env_var="GROQ",
    ),
    "pollinations/openai-fast": AIModel(
        key="pollinations/openai-fast",
        provider="Pollinations",
        label="OpenAI Fast",
        model_id="openai-fast",
        env_var=None,
    ),
    "openrouter/openai/gpt-oss-120b:free": AIModel(
        key="openrouter/openai/gpt-oss-120b:free",
        provider="OpenRouter",
        label="GPT OSS 120B Free",
        model_id="openai/gpt-oss-120b:free",
        env_var="OPENROUTER",
    ),
}

MODEL_CHOICES: list[tuple[str, str]] = [
    (model.key, f"{model.provider}: {model.label}") for model in AI_MODELS.values()
]


class AI(commands.Cog):
    display_name = "AI"
    group_name = "Utilities"

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot
        self.aiohttp = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        self.system_prompt = self._load_system_prompt()
        self.summarize_menu = app_commands.ContextMenu(
            name="Sumarize it by GPT-OSS-120b",
            callback=self.summarize_message,
            type=discord.AppCommandType.message,
        )
        self.professional_menu = app_commands.ContextMenu(
            name="Make it professional by AI",
            callback=self.make_message_professional,
            type=discord.AppCommandType.message,
        )
        self.fix_grammar_menu = app_commands.ContextMenu(
            name="Fix grammer by AI",
            callback=self.fix_message_grammar,
            type=discord.AppCommandType.message,
        )
        self.bot.tree.add_command(self.summarize_menu)
        self.bot.tree.add_command(self.professional_menu)
        self.bot.tree.add_command(self.fix_grammar_menu)

    def cog_unload(self) -> None:
        if not self.aiohttp.closed:
            self.bot.loop.create_task(self.aiohttp.close())
        for menu in (
            self.summarize_menu,
            self.professional_menu,
            self.fix_grammar_menu,
        ):
            self.bot.tree.remove_command(
                menu.name,
                type=menu.type,
            )

    def _get_token(self, env_var: str | None) -> str | None:
        if not env_var:
            return None

        token = os.getenv(env_var)
        if token:
            return token.strip() or None

        aliases = {
            "GOOGLE": ("GEMINI", "GOOGLE_API_KEY"),
            "GROQ": ("GROQ_API_KEY",),
            "OPENROUTER": ("OPENROUTER_API_KEY",),
        }
        for alias in aliases.get(env_var, ()):
            value = os.getenv(alias)
            if value:
                return value.strip() or None
        return None

    def _load_system_prompt(self) -> str:
        try:
            prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log_exception(exc)
            return ""
        return prompt

    def _normalize_provider_error(
        self,
        *,
        provider: str,
        status: int,
        message: str | None = None,
    ) -> str:
        text = (message or "").lower()
        usage_markers = (
            "rate limit",
            "rate-limit",
            "rate limited",
            "quota",
            "usage limit",
            "maximum usage",
            "too many requests",
            "temporarily rate-limited",
            "resource exhausted",
        )
        if status == 429 or any(marker in text for marker in usage_markers):
            return "maximum usage reached"
        return message or f"{provider} returned HTTP {status}."

    async def _generate_with_google(self, model: AIModel, prompt: str) -> str:
        token = self._get_token(model.env_var)
        if not token:
            raise RuntimeError("Missing Google API key.")

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model.model_id}:generateContent?key={token}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 700,
            },
        }
        if self.system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": self.system_prompt}],
            }
        async with self.aiohttp.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                message = data.get("error", {}).get("message") if isinstance(data, dict) else None
                raise RuntimeError(
                    self._normalize_provider_error(
                        provider="Google",
                        status=resp.status,
                        message=message,
                    )
                )

        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not candidates:
            raise RuntimeError("Google returned no candidates.")

        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "").strip() for part in parts if isinstance(part, dict) and part.get("text")]
        text = "\n".join(part for part in text_parts if part)
        if not text:
            raise RuntimeError("Google returned an empty response.")
        return text

    async def _generate_openai_compatible(
        self,
        *,
        url: str,
        model: AIModel,
        prompt: str,
        token: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> str:
        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)

        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model.model_id,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 700,
        }
        async with self.aiohttp.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                message = None
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        message = error.get("message")
                    elif isinstance(error, str):
                        message = error
                raise RuntimeError(
                    self._normalize_provider_error(
                        provider=model.provider,
                        status=resp.status,
                        message=message,
                    )
                )

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise RuntimeError(f"{model.provider} returned no choices.")

        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    text_parts.append(str(item["text"]).strip())
            text = "\n".join(part for part in text_parts if part)
            if text:
                return text

        raise RuntimeError(f"{model.provider} returned an empty response.")

    async def _generate(self, model: AIModel, prompt: str) -> str:
        if model.provider == "Google":
            return await self._generate_with_google(model, prompt)
        if model.provider == "Groq":
            token = self._get_token(model.env_var)
            if not token:
                raise RuntimeError("Missing Groq API key.")
            return await self._generate_openai_compatible(
                url="https://api.groq.com/openai/v1/chat/completions",
                model=model,
                prompt=prompt,
                token=token,
            )
        if model.provider == "Pollinations":
            return await self._generate_openai_compatible(
                url="https://text.pollinations.ai/openai/chat/completions",
                model=model,
                prompt=prompt,
            )
        if model.provider == "OpenRouter":
            token = self._get_token(model.env_var)
            if not token:
                raise RuntimeError("Missing OpenRouter API key.")
            return await self._generate_openai_compatible(
                url="https://openrouter.ai/api/v1/chat/completions",
                model=model,
                prompt=prompt,
                token=token,
                extra_headers={
                    "HTTP-Referer": "https://github.com/azyfx/amenity",
                    "X-Title": "Amenity",
                },
            )
        raise RuntimeError(f"Unsupported provider: {model.provider}")

    def _split_text(self, text: str, *, max_chars: int = PAGE_CHARS) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        pages: list[str] = []
        current = ""
        paragraphs = text.split("\n")

        for paragraph in paragraphs:
            chunk = paragraph if paragraph else "\u200b"
            if len(chunk) > max_chars:
                if current:
                    pages.append(current)
                    current = ""
                start = 0
                while start < len(chunk):
                    pages.append(chunk[start : start + max_chars])
                    start += max_chars
                continue

            candidate = chunk if not current else f"{current}\n{chunk}"
            if len(candidate) > max_chars:
                pages.append(current)
                current = chunk
            else:
                current = candidate

        if current:
            pages.append(current)

        return pages

    def _build_paginated_embeds(
        self,
        *,
        prompt: str,
        model: AIModel,
        response: str,
        author: discord.abc.User,
        title: str = "AI Response",
        prompt_label: str = "Prompt",
    ) -> list[discord.Embed]:
        pages = self._split_text(response)
        total_pages = len(pages)
        embeds: list[discord.Embed] = []

        for index, page in enumerate(pages, start=1):
            embed = discord.Embed(
                title=title,
                description=page,
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Model", value=f"{model.provider}: {model.label}", inline=False)
            embed.add_field(
                name=prompt_label,
                value=discord.utils.escape_markdown(prompt[:PROMPT_PREVIEW_CHARS]) or "\u200b",
                inline=False,
            )
            embed.set_footer(
                text=f"Requested by {author} • Page {index}/{total_pages}",
                icon_url=author.display_avatar.url,
            )
            embeds.append(embed)

        return embeds

    async def _send_paginated_response(
        self,
        *,
        destination: commands.Context | discord.Interaction,
        pages: list[discord.Embed],
        author_id: int,
        ephemeral: bool = False,
    ) -> None:
        if len(pages) == 1:
            if isinstance(destination, commands.Context):
                await destination.send(embed=pages[0])
            elif destination.response.is_done():
                await destination.followup.send(embed=pages[0], ephemeral=ephemeral)
            else:
                await destination.response.send_message(embed=pages[0], ephemeral=ephemeral)
            return

        view = EmbedPaginator(pages, author_id=author_id)
        if isinstance(destination, commands.Context):
            await destination.send(embed=pages[0], view=view)
        elif destination.response.is_done():
            await destination.followup.send(embed=pages[0], view=view, ephemeral=ephemeral)
        else:
            await destination.response.send_message(embed=pages[0], view=view, ephemeral=ephemeral)

    def _build_summary_prompt(self, text: str) -> str:
        return (
            "Summarize the following Discord message clearly and briefly. "
            "Keep the key intent, details, and tone. If the message is already short, just restate it more clearly.\n\n"
            f"Message:\n{text}"
        )

    def _build_professional_prompt(self, text: str) -> str:
        return (
            "Rewrite the following Discord message to sound professional, clear, and polished. "
            "Keep the original meaning and important details. Do not add extra information.\n\n"
            f"Message:\n{text}"
        )

    def _build_grammar_prompt(self, text: str) -> str:
        return (
            "Fix the grammar, spelling, punctuation, and clarity of the following Discord message. "
            "Keep the meaning and tone as close as possible. Return only the corrected version.\n\n"
            f"Message:\n{text}"
        )

    def _extract_message_text(self, message: discord.Message) -> str:
        parts: list[str] = []
        content = message.content.strip()
        if content:
            parts.append(content)

        for embed in message.embeds:
            if embed.title:
                parts.append(embed.title)
            if embed.description:
                parts.append(embed.description)
            for field in embed.fields:
                if field.name:
                    parts.append(field.name)
                if field.value:
                    parts.append(field.value)

        return "\n".join(part.strip() for part in parts if part and part.strip())

    async def _generate_pollinations_image(self, prompt: str) -> bytes:
        encoded_prompt = quote(prompt, safe="")
        url = (
            f"https://image.pollinations.ai/prompt/{encoded_prompt}"
            f"?model={POLLINATIONS_IMAGE_MODEL}&width=1024&height=1024&nologo=true"
        )
        async with self.aiohttp.get(url, headers={"User-Agent": "AmenityBot/1.0"}) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(
                    self._normalize_provider_error(
                        provider="Pollinations",
                        status=resp.status,
                        message=error_text,
                    )
                )
            return await resp.read()

    async def _run_message_action(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        *,
        prompt_builder: callable,
        empty_message: str,
        title: str,
    ) -> None:
        source_text = self._extract_message_text(message)
        if not source_text:
            await interaction.response.send_message(empty_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        model = AI_MODELS[SUMMARY_MODEL_KEY]
        prompt = prompt_builder(source_text)

        try:
            output = await self._generate(model, prompt)
        except Exception as exc:
            log_exception(exc)
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        pages = self._build_paginated_embeds(
            prompt=source_text,
            model=model,
            response=output,
            author=interaction.user,
            title=title,
            prompt_label="Original Message",
        )
        await self._send_paginated_response(
            destination=interaction,
            pages=pages,
            author_id=interaction.user.id,
            ephemeral=True,
        )

    @commands.hybrid_command(
        name="ai",
        description="Ask one AI model from Google, Groq, Pollinations, or OpenRouter.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        model="Pick the model to use.",
        prompt="What you want the model to answer.",
    )
    @commands.cooldown(1, 15, commands.BucketType.user)
    @commands.max_concurrency(5, commands.BucketType.default, wait=True)
    async def ai(
        self,
        ctx: commands.Context,
        model: str,
        *,
        prompt: str,
    ) -> None:
        query = prompt.strip()
        if not query:
            await ctx.send("Please provide a prompt.")
            return

        selected_key = model.strip()
        if selected_key not in AI_MODELS:
            await ctx.send("Unknown model. Use the autocomplete list from `/ai`.")
            return

        await ctx.defer()
        selected_model = AI_MODELS[selected_key]

        try:
            response = await self._generate(selected_model, query)
        except Exception as exc:
            log_exception(exc)
            await ctx.send(str(exc))
            return

        pages = self._build_paginated_embeds(
            prompt=query,
            model=selected_model,
            response=response,
            author=ctx.author,
        )
        await self._send_paginated_response(
            destination=ctx,
            pages=pages,
            author_id=ctx.author.id,
        )

    @commands.hybrid_command(
        name="image-gen",
        description="Generate an image with Pollinations free image generation.",
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        prompt="Describe the image you want to generate.",
    )
    @commands.cooldown(1, 20, commands.BucketType.user)
    @commands.max_concurrency(3, commands.BucketType.default, wait=True)
    async def image_gen(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
    ) -> None:
        query = prompt.strip()
        if not query:
            await ctx.send("Please provide an image prompt.")
            return

        await ctx.defer()

        try:
            image_bytes = await self._generate_pollinations_image(query)
        except Exception as exc:
            log_exception(exc)
            await ctx.send(str(exc))
            return

        file = discord.File(BytesIO(image_bytes), filename="pollinations-image.jpg")
        embed = discord.Embed(
            title="Image Generation",
            description=discord.utils.escape_markdown(query[:PROMPT_PREVIEW_CHARS]),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Provider", value="Pollinations", inline=True)
        embed.add_field(name="Model", value=POLLINATIONS_IMAGE_MODEL, inline=True)
        embed.set_image(url="attachment://pollinations-image.jpg")
        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed, file=file)

    async def summarize_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._run_message_action(
            interaction,
            message,
            prompt_builder=self._build_summary_prompt,
            empty_message="That message has no text to summarize.",
            title="Message Summary",
        )

    async def make_message_professional(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._run_message_action(
            interaction,
            message,
            prompt_builder=self._build_professional_prompt,
            empty_message="That message has no text to rewrite.",
            title="Professional Rewrite",
        )

    async def fix_message_grammar(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        await self._run_message_action(
            interaction,
            message,
            prompt_builder=self._build_grammar_prompt,
            empty_message="That message has no text to fix.",
            title="Grammar Fix",
        )

    @ai.autocomplete("model")
    async def ai_model_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        del interaction
        current_lower = current.strip().lower()
        choices: list[app_commands.Choice[str]] = []
        for value, description in MODEL_CHOICES:
            haystack = f"{value} {description}".lower()
            if current_lower and current_lower not in haystack:
                continue
            choices.append(app_commands.Choice(name=description[:100], value=value))
            if len(choices) >= 25:
                break
        return choices


async def setup(bot: Amenity) -> None:
    await bot.add_cog(AI(bot))
