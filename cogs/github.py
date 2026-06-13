from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from api.log import log_exception

if TYPE_CHECKING:
    from core.amenity import Amenity


class Github(commands.Cog):
    display_name = "GitHub"
    group_name = "Utilities"

    def __init__(self, bot: Amenity) -> None:
        self.bot = bot
        self.aiohttp = aiohttp.ClientSession()

    def cog_unload(self) -> None:
        if not self.aiohttp.closed:
            self.bot.loop.create_task(self.aiohttp.close())

    async def _fetch_json(self, url: str) -> tuple[dict | None, int | None]:
        try:
            async with self.aiohttp.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                status = resp.status
                if status != 200:
                    return None, status
                return await resp.json(), status
        except (TimeoutError, aiohttp.ClientError):
            return None, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_exception(exc)
            return None, None

    @commands.hybrid_group(
        name="github",
        description="Github related commands.",
        aliases=["gh"],
        invoke_without_command=True,
    )
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 10, commands.BucketType.user)
    @commands.max_concurrency(20, commands.BucketType.default, wait=True)
    async def github(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix or "/"
        cmds = "\n".join(
            [
                f"{prefix}github {cmd.name} - {cmd.description}"
                for cmd in self.github.walk_commands()
                if cmd.description
            ]
        )
        if not cmds:
            cmds = "No commands available."
        embed = discord.Embed(
            description="GitHub available commands:",
            color=discord.Color.light_grey(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="Commands", value=f"```{cmds}```", inline=False)
        await ctx.reply(embed=embed, mention_author=False)

    @github.command(
        name="user",
        description="Gets the profile of a GitHub user.",
        aliases=["profile", "pf"],
    )
    async def github_user(self, ctx: commands.Context, user: str) -> None:
        query = user.strip()
        if not query:
            await ctx.send("Please provide a GitHub username.")
            return

        data, status = await self._fetch_json(f"https://api.github.com/users/{quote_plus(query)}")
        if not data or status != 200:
            await ctx.send("User not found!")
            return

        embed = discord.Embed(
            title=f"GitHub: {data.get('login', query)}",
            description=f"**Bio:** {data.get('bio') or 'N/A'}",
            color=discord.Color.dark_grey(),
        )
        if data.get("avatar_url"):
            embed.set_thumbnail(url=data["avatar_url"])
        embed.add_field(
            name="Username 📛:",
            value=f"[{data.get('name') or data.get('login')}]({data.get('html_url')})",
            inline=True,
        )
        embed.add_field(name="Repos 📁:", value=str(data.get("public_repos", 0)), inline=True)
        embed.add_field(name="Location 📍:", value=data.get("location") or "N/A", inline=True)
        embed.add_field(name="Company 🏢:", value=data.get("company") or "N/A", inline=True)
        embed.add_field(name="Followers 👥:", value=str(data.get("followers", 0)), inline=True)
        embed.add_field(name="Website 🖥️:", value=data.get("blog") or "N/A", inline=True)
        await ctx.reply(embed=embed, mention_author=False)

    @github.command(
        name="repo",
        description="Search GitHub repositories.",
        aliases=["repository", "searchrepo", "reposearch", "search"],
    )
    async def reposearch(self, ctx: commands.Context, *, query: str) -> None:
        try:
            api_url = (
                "https://api.github.com/search/repositories"
                f"?q={quote_plus(query)}&sort=stars&order=desc&per_page=10"
            )
            data, status = await self._fetch_json(api_url)
            if not data or status != 200:
                await ctx.send("Failed to fetch repository info from GitHub.")
                return

            items = data.get("items", [])
            if not items:
                await ctx.send(f"No repositories found for `{query}`.")
                return

            def make_embed(repo: dict) -> discord.Embed:
                embed = discord.Embed(
                    title=repo["full_name"],
                    url=repo["html_url"],
                    description=repo.get("description") or "No description provided.",
                    color=discord.Color.dark_grey(),
                )
                owner = repo.get("owner") or {}
                if owner.get("avatar_url"):
                    embed.set_thumbnail(url=owner["avatar_url"])
                embed.add_field(
                    name="Stars ⭐",
                    value=str(repo.get("stargazers_count", 0)),
                    inline=True,
                )
                embed.add_field(
                    name="Forks 🍴",
                    value=str(repo.get("forks_count", 0)),
                    inline=True,
                )
                embed.add_field(
                    name="Language",
                    value=repo.get("language") or "N/A",
                    inline=True,
                )
                embed.add_field(
                    name="Open Issues",
                    value=str(repo.get("open_issues_count", 0)),
                    inline=True,
                )
                license_info = repo.get("license")
                embed.add_field(
                    name="License",
                    value=license_info.get("name") if license_info else "No license",
                    inline=True,
                )
                embed.add_field(
                    name="Default Branch",
                    value=repo.get("default_branch") or "main",
                    inline=True,
                )
                if owner.get("login"):
                    embed.set_footer(text=f"Owner: {owner['login']}")
                return embed

            class RepoPaginator(discord.ui.View):
                def __init__(self, repos: list[dict]) -> None:
                    super().__init__(timeout=60)
                    self.repos = repos
                    self.index = 0
                    self.message: discord.Message | None = None
                    self.previous_button = discord.ui.Button(
                        label="Previous", style=discord.ButtonStyle.secondary
                    )
                    self.next_button = discord.ui.Button(
                        label="Next", style=discord.ButtonStyle.secondary
                    )
                    self.previous_button.callback = self.previous_callback
                    self.next_button.callback = self.next_callback
                    self.add_item(self.previous_button)
                    self.add_item(self.next_button)

                async def previous_callback(self, interaction: discord.Interaction) -> None:
                    if self.index > 0:
                        self.index -= 1
                        embed = make_embed(self.repos[self.index])
                        await interaction.response.edit_message(embed=embed, view=self)
                    else:
                        await interaction.response.defer()

                async def next_callback(self, interaction: discord.Interaction) -> None:
                    if self.index < len(self.repos) - 1:
                        self.index += 1
                        embed = make_embed(self.repos[self.index])
                        await interaction.response.edit_message(embed=embed, view=self)
                    else:
                        await interaction.response.defer()

                async def on_timeout(self) -> None:
                    self.previous_button.disabled = True
                    self.next_button.disabled = True
                    if self.message:
                        await self.message.edit(view=self)

            view = RepoPaginator(items)
            embed = make_embed(items[0])
            message = await ctx.reply(embed=embed, view=view, mention_author=False)
            view.message = message

        except Exception as exc:
            await ctx.send("An error occurred while searching for repositories")
            log_exception(exc)


    @commands.hybrid_command(
        name="prinfo",
        description="Fetch detailed metrics about a GitHub Pull Request using its URL link."
    )
    @app_commands.describe(pr_url="The full web link to the GitHub Pull Request")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def pr_info_cmd(self, ctx: commands.Context, pr_url: str):


        # 1. Regex URL Parsing match check
        match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url.strip())
        if not match:
            err_embed = discord.Embed(
                title="❌ Invalid PR Link",
                description="Please provide a valid GitHub PR link format.\nExample: `https://github.com/discordjs/discord.js/pull/1234`",
                color=discord.Color.red()
            )
            if ctx.interaction:
                await ctx.interaction.followup.send(embed=err_embed)
            else:
                await ctx.send(embed=err_embed)
            return

        owner, repo, pr_number = match.groups()
        api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {"User-Agent": "Discord-Bot-PR-Fetcher"}

        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as resp:
                if resp.status == 404:
                    embed = discord.Embed(title="❌ PR Not Found", description=f"Could not find PR #{pr_number} under `{owner}/{repo}`.", color=discord.Color.red())
                elif resp.status != 200:
                    embed = discord.Embed(title="❌ API Failure", description=f"GitHub API returned code `{resp.status}`.", color=discord.Color.red())
                else:
                    data = await resp.json()

                    # 2. Extract explicit details
                    title = data.get("title", "No Title Provided")
                    author = data["user"]["login"]
                    avatar = data["user"]["avatar_url"]

                    # Status evaluation rules (Merged vs Closed vs Open)
                    state = data.get("state", "open").upper()
                    if data.get("merged") == True:
                        state = "MERGED"
                        color = discord.Color.purple()
                    else:
                        color = discord.Color.green() if state == "OPEN" else discord.Color.red()

                    # Line statistics changes metrics
                    additions = data.get("additions", 0)
                    deletions = data.get("deletions", 0)
                    changed_files = data.get("changed_files", 0)
                    commits = data.get("commits", 0)

                    # Branches tracking structural pointers
                    head_branch = data["head"]["ref"]
                    base_branch = data["base"]["ref"]

                    # Timestamp conversion parsing loop
                    created_raw = data["created_at"]
                    dt = datetime.strptime(created_raw, "%Y-%m-%dT%H:%M:%SZ")
                    discord_ts = f"<t:{int(dt.timestamp())}:F>"

                    # 3. Design output layout metrics block representation
                    description = (
                        f"📁 **Repository:** [{owner}/{repo}](https://github.com/{owner}/{repo})\n"
                        f"🔀 **Pull Request:** [#{pr_number}]({pr_url}) — **{title}**\n"
                        f"🏁 **Status:** `{state}`\n"
                        f"--- \n"
                        f"👤 **Opened By:** @{author}\n"
                        f"📅 **Created At:** {discord_ts}\n"
                        f"🌿 **Branch Path:** `{head_branch}` ➡️ `{base_branch}`\n\n"
                        f"📊 **Pull Request Stats:**\n"
                        f"🟩 Line Additions: `+{additions}`\n"
                        f"🟥 Line Deletions: `-{deletions}`\n"
                        f"📝 Changed Files: `{changed_files}`\n"
                        f"📦 Internal Commits: `{commits}`"
                    )

                    embed = discord.Embed(title="🔀 GitHub Pull Request Details", description=description, color=color)
                    embed.set_thumbnail(url=avatar)

        await ctx.send(embed=embed)


async def setup(bot: Amenity) -> None:
    await bot.add_cog(Github(bot))
