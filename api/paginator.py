import discord
from typing import List, Optional
import math

class EmbedPaginator(discord.ui.View):
    def __init__(self, embeds: List[discord.Embed], *, timeout: float = 180.0, author_id: Optional[int] = None):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.current_page = 0
        self.author_id = author_id
        self.max_pages = len(embeds)
        
        # Update button states
        self.update_buttons()
    
    def update_buttons(self):
        """Update button states based on current page"""
        self.first_page.disabled = self.current_page == 0
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page == self.max_pages - 1
        self.last_page.disabled = self.current_page == self.max_pages - 1
        
        # Update page counter button
        self.page_counter.label = f"{self.current_page + 1}/{self.max_pages}"
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if the user is authorized to use the paginator"""
        if self.author_id and interaction.user.id != self.author_id:
            await interaction.response.send_message("You can't use this paginator!", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label="First", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to first page"""
        self.current_page = 0
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to previous page"""
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page_counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Page counter - disabled button for display"""
        pass
    
    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to next page"""
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(label="Last", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go to last page"""
        self.current_page = self.max_pages - 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.current_page], view=self)
    
    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
    async def delete_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Delete the paginator message"""
        await interaction.response.defer()
        await interaction.delete_original_response()
    
    async def on_timeout(self):
        """Disable all buttons when the view times out"""
        for item in self.children:
            item.disabled = True


class PaginatorHelper:
    """Helper class to create paginated embeds from data"""
    
    @staticmethod
    def create_embeds(data: List[str], title: str, items_per_page: int = 10, 
                     color: discord.Color = discord.Color.blue()) -> List[discord.Embed]:
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
        data: List[str],
        title: str,
        items_per_page: int = 10,
        max_chars: int = 1000,
        color: discord.Color = discord.Color.blue(),
    ) -> List[discord.Embed]:
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
        total_items = len(data)

        for page_index, page_data in enumerate(pages, start=1):
            embed = discord.Embed(title=title, color=color)
            embed.description = "\n".join(page_data)
            embed.set_footer(text=f"Page {page_index}/{total_pages} • Total: {total_items} items")
            embeds.append(embed)

        return embeds
    
    @staticmethod
    def create_field_embeds(data: List[dict], title: str, items_per_page: int = 5,
                           color: discord.Color = discord.Color.blue()) -> List[discord.Embed]:
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
                    name=item.get('name', 'Unknown'),
                    value=item.get('value', 'No value'),
                    inline=item.get('inline', False)
                )
            
            embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total: {len(data)} items")
            embeds.append(embed)
        
        return embeds

