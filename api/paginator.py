import math
from contextlib import suppress

import discord

MAX_COMPONENT_TEXT_LENGTH = 4000


class EmbedPaginator(discord.ui.LayoutView):
    def __init__(
        self,
        embeds: list[discord.Embed],
        *,
        timeout: float = 180.0,
        author_id: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.author_id = author_id
        self.max_pages = len(embeds)
        self.message: discord.Message | None = None

        self.first_page = discord.ui.Button(label="<<", style=discord.ButtonStyle.secondary)
        self.previous_page = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary)
        self.page_counter = discord.ui.Button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
        self.next_page = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary)
        self.last_page = discord.ui.Button(label=">>", style=discord.ButtonStyle.secondary)
        self.delete_message = discord.ui.Button(label="Close", style=discord.ButtonStyle.danger)
        self.first_page.callback = self._first_page
        self.previous_page.callback = self._previous_page
        self.next_page.callback = self._next_page
        self.last_page.callback = self._last_page
        self.delete_message.callback = self._delete_message
        buttons = [
            self.first_page,
            self.previous_page,
            self.page_counter,
            self.next_page,
            self.last_page,
            self.delete_message,
        ]
        self.content_component = discord.ui.TextDisplay("")
        self.container = discord.ui.Container(self.content_component)
        self.add_item(self.container)
        self.add_item(discord.ui.ActionRow(*buttons[:5]))
        self.add_item(discord.ui.ActionRow(buttons[5]))
        self._render_embed()

        # Update button states
        self.update_buttons()

    def update_buttons(self) -> None:
        """Update button states based on current page"""
        if self.max_pages == 0:
            self.first_page.disabled = True
            self.previous_page.disabled = True
            self.next_page.disabled = True
            self.last_page.disabled = True
            self.page_counter.label = "0/0"
            return
        self.first_page.disabled = self.current_page == 0
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == self.max_pages - 1
        self.last_page.disabled = self.current_page == self.max_pages - 1

        # Update page counter button
        self.page_counter.label = f"{self.current_page + 1}/{self.max_pages}"

    def _render_embed(self) -> None:
        if not self.embeds:
            self.content_component.content = "No pages to display."
            self.container.accent_colour = None
            for button in (self.first_page, self.previous_page, self.next_page, self.last_page):
                button.disabled = True
            self.page_counter.label = "0/0"
            return

        embed = self.embeds[self.current_page]
        parts: list[str] = []
        if embed.author.name:
            author = embed.author.name
            if embed.author.url:
                author = f"[{author}]({embed.author.url})"
            parts.append(f"-# {author}")
        if embed.title:
            title = embed.title
            if embed.url:
                title = f"[{title}]({embed.url})"
            parts.append(f"## {title}")
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"**{field.name}**\n{field.value}")
        if embed.footer.text:
            footer = embed.footer.text
            if embed.timestamp:
                footer = f"{footer} • <t:{int(embed.timestamp.timestamp())}:f>"
            parts.append(f"-# {footer}")
        rendered = "\n\n".join(parts)
        rendered = rendered.replace("<@", "<@\u200b").replace("<#", "<#\u200b")
        rendered = discord.utils.escape_mentions(rendered) or "\u200b"
        if len(rendered) > MAX_COMPONENT_TEXT_LENGTH:
            marker = "\n\n-# Text truncated to fit Discord's Components V2 limit."
            rendered = rendered[: MAX_COMPONENT_TEXT_LENGTH - len(marker)].rstrip() + marker
        self.content_component.content = rendered
        self.container.accent_colour = embed.colour
        self.container.clear_items()
        self.container.add_item(self.content_component)
        media_urls = list(dict.fromkeys(url for url in (embed.image.url, embed.thumbnail.url) if url))
        if media_urls:
            media_items = [discord.MediaGalleryItem(media=url) for url in media_urls]
            self.container.add_item(discord.ui.MediaGallery(*media_items))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if the user is authorized to use the paginator"""
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message("You can't use this paginator!", ephemeral=True)
            return False
        return True

    async def _first_page(self, interaction: discord.Interaction) -> None:
        self.current_page = 0
        self.update_buttons()
        self._render_embed()
        await interaction.response.edit_message(view=self)

    async def _previous_page(self, interaction: discord.Interaction) -> None:
        self.current_page -= 1
        self.update_buttons()
        self._render_embed()
        await interaction.response.edit_message(view=self)

    async def _next_page(self, interaction: discord.Interaction) -> None:
        self.current_page += 1
        self.update_buttons()
        self._render_embed()
        await interaction.response.edit_message(view=self)

    async def _last_page(self, interaction: discord.Interaction) -> None:
        self.current_page = self.max_pages - 1
        self.update_buttons()
        self._render_embed()
        await interaction.response.edit_message(view=self)

    async def _delete_message(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await interaction.delete_original_response()

    async def on_timeout(self) -> None:
        """Disable all buttons when the view times out"""
        for item in self.children:
            if isinstance(item, discord.ui.ActionRow):
                for button in item.children:
                    if isinstance(button, discord.ui.Button):
                        button.disabled = True
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)


class PaginatorHelper:
    """Helper class to create paginated embeds from data"""

    @staticmethod
    def create_embeds(
        data: list[str],
        title: str,
        items_per_page: int = 10,
        color: discord.Color = discord.Color.blue(),
    ) -> list[discord.Embed]:
        """
        Create paginated embeds from a list of strings
        Args:
            data: List of strings to paginate
            title: Title for the embeds
            items_per_page: Number of items per page
            color: Color for the embeds
        Returns:
            List of discord.Embed objects
        """
        if not data:
            embed = discord.Embed(title=title, description="No data found.", color=color)
            return [embed]

        embeds = []
        total_pages = math.ceil(len(data) / items_per_page)

        for page in range(total_pages):
            start_index = page * items_per_page
            end_index = min(start_index + items_per_page, len(data))
            page_data = data[start_index:end_index]

            embed = discord.Embed(title=title, color=color)
            embed.description = "\n".join(page_data)
            embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total: {len(data)} items")
            embeds.append(embed)

        return embeds

    @staticmethod
    def create_adaptive_embeds(
        data: list[str],
        title: str = "Output",
        items_per_page: int = 10,
        max_chars: int = 1000,
        color: discord.Color = discord.Color.blue(),
    ) -> list[discord.Embed]:
        """
        Create paginated embeds from a list of strings using
        both a max items per page and a max character budget.
        Args:
            data: List of strings to paginate
            title: Title for the embeds
            items_per_page: Maximum number of items per page
            max_chars: Maximum characters per page description
            color: Color for the embeds
        Returns:
            List of discord.Embed objects
        """
        if not data:
            embed = discord.Embed(title=title, description="No data found.", color=color)
            return [embed]

        pages: list[list[str]] = []
        current: list[str] = []
        current_len = 0

        for item in data:
            item_text = item
            if len(item_text) > max_chars:
                item_text = item_text[: max_chars - 3] + "..."

            extra_len = len(item_text) + (1 if current else 0)
            if current and (len(current) >= items_per_page or current_len + extra_len > max_chars):
                pages.append(current)
                current = []
                current_len = 0

            current.append(item_text)
            current_len += len(item_text) + (1 if len(current) > 1 else 0)

        if current:
            pages.append(current)

        embeds: list[discord.Embed] = []
        total_pages = len(pages)

        for page_index, page_data in enumerate(pages, start=1):
            embed = discord.Embed(
                title=f"{title} (Page {page_index}/{total_pages})" if total_pages > 1 else title,
                description="\n".join(page_data),
                color=color,
            )
            if total_pages > 1:
                embed.set_footer(text=f"Page {page_index}/{total_pages} • Total items: {len(data)}")
            else:
                embed.set_footer(text=f"Total items: {len(data)}")
            embeds.append(embed)

        return embeds

    @staticmethod
    def create_field_embeds(
        data: list[dict],
        title: str,
        items_per_page: int = 5,
        color: discord.Color = discord.Color.blue(),
    ) -> list[discord.Embed]:
        """
        Create paginated embeds with fields from a list of dictionaries
        Args:
            data: List of dictionaries with 'name' and 'value' keys
            title: Title for the embeds
            items_per_page: Number of fields per page
            color: Color for the embeds
        Returns:
            List of discord.Embed objects
        """
        if not data:
            embed = discord.Embed(title=title, description="No data found.", color=color)
            return [embed]

        embeds = []
        total_pages = math.ceil(len(data) / items_per_page)

        for page in range(total_pages):
            start_index = page * items_per_page
            end_index = min(start_index + items_per_page, len(data))
            page_data = data[start_index:end_index]

            embed = discord.Embed(title=title, color=color)

            for item in page_data:
                embed.add_field(
                    name=item.get("name", "Unknown"),
                    value=item.get("value", "No value"),
                    inline=item.get("inline", False),
                )

            embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total: {len(data)} items")
            embeds.append(embed)

        return embeds
