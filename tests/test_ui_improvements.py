from __future__ import annotations

import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from discord.ext import commands

from api.buttons import Links
from api.emojis import Emoji
from api.paginator import EmbedPaginator
from cogs.template import TemplateBuilderView, TemplatePayloadTooLong, build_template_payload
from cogs.tools import Tools
from core import app_commands
from core.amenity import Amenity
from core.help import (
    HelpCategorySelect,
    HelpFirstButton,
    HelpHomeButton,
    HelpIndex,
    HelpLastButton,
    HelpNextButton,
    HelpPageIndicator,
    HelpPrevButton,
    HelpSearchButton,
    HelpView,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_app_command_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-ID persistence and GUILD_ID out of the real artifacts directory."""
    monkeypatch.setattr(app_commands, "APP_COMMAND_IDS_PATH", tmp_path / "app_command_ids.json")
    monkeypatch.delenv("GUILD_ID", raising=False)
    monkeypatch.setattr("cogs.tools.create_http_session", lambda *args, **kwargs: MagicMock())


def test_embed_paginator_buttons() -> None:
    embeds = [discord.Embed(title=f"Page {i}") for i in range(3)]
    paginator = EmbedPaginator(embeds)

    button_labels = [child.label for child in paginator.walk_children() if isinstance(child, discord.ui.Button)]
    assert "<<" in button_labels
    assert "<" in button_labels
    assert ">" in button_labels
    assert ">>" in button_labels
    assert "Close" in button_labels
    assert "1/3" in button_labels

    # On page 0: << and < are disabled; > and >> are enabled
    assert paginator.first_page.disabled is True
    assert paginator.previous_page.disabled is True
    assert paginator.next_page.disabled is False
    assert paginator.last_page.disabled is False

    # Simulate move to page 1
    paginator.current_page = 1
    paginator.update_buttons()
    assert paginator.first_page.disabled is False
    assert paginator.previous_page.disabled is False
    assert paginator.next_page.disabled is False
    assert paginator.last_page.disabled is False
    assert paginator.page_counter.label == "2/3"

    # Simulate move to page 2 (last)
    paginator.current_page = 2
    paginator.update_buttons()
    assert paginator.first_page.disabled is False
    assert paginator.previous_page.disabled is False
    assert paginator.next_page.disabled is True
    assert paginator.last_page.disabled is True
    assert paginator.page_counter.label == "3/3"


def test_embed_paginator_v2_limits_text_and_neutralizes_mentions() -> None:
    embed = discord.Embed(
        title="Shop result",
        url="https://example.com/item",
        description="<@123456789> " + "x" * 5000,
    )

    paginator = EmbedPaginator([embed])
    rendered = paginator.content_component.content

    assert paginator.content_length() <= 4000
    assert "[Shop result](https://example.com/item)" in rendered
    assert "<@123456789>" not in rendered
    assert "<@\u200b123456789>" in rendered
    assert "Text truncated" in rendered


def test_links_multi_view_splits_rows_at_five_buttons() -> None:
    view = Links.multi_view([(f"Link {index}", f"https://example.com/{index}") for index in range(7)])

    container = view.to_components()[0]
    rows = container["components"]
    assert [len(row["components"]) for row in rows] == [5, 2]


def test_template_v2_payload_keeps_buttons_and_rejects_oversized_text() -> None:
    template = {
        "content": "Template text",
        "embed": {"title": "Template embed", "url": "https://example.com"},
        "buttons": [{"label": "Open", "url": "https://example.com"}],
    }
    content, embed, view = build_template_payload(template)

    assert content is None
    assert embed is None
    assert view is not None
    assert view.content_length() <= 4000
    assert "Template text" in view.to_components()[0]["components"][0]["content"]
    assert "[Template embed](https://example.com)" in view.to_components()[0]["components"][0]["content"]

    oversized = {
        "content": "x" * 2000,
        "embed": {"description": "y" * 2500},
        "buttons": [{"label": "Open", "url": "https://example.com"}],
    }
    with pytest.raises(TemplatePayloadTooLong):
        build_template_payload(oversized)


@pytest.mark.asyncio
async def test_template_preview_sends_v2_view_for_link_button_templates() -> None:
    builder = TemplateBuilderView(
        SimpleNamespace(),
        author_id=123,
        name="preview",
        data={"content": "Template body", "embed": None, "buttons": [{"label": "Open", "url": "https://example.com"}]},
    )
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    await builder.preview_button(interaction)

    kwargs = interaction.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["view"], discord.ui.LayoutView)
    assert "content" not in kwargs
    assert "embed" not in kwargs


@pytest.mark.asyncio
async def test_help_command_excluded_from_listings() -> None:
    """The bot's own `help` command must never be listed inside the help menu."""
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.owner_id = 999
    index = HelpIndex(bot)
    user = SimpleNamespace(id=999)

    async def cb(ctx: commands.Context[commands.Bot]) -> None:
        pass

    assert await index.can_see_command(commands.Command(cb, name="help"), user) is False
    assert await index.can_see_command(commands.Command(cb, name="avatar"), user) is True


def test_create_adaptive_embeds_signature_regression() -> None:
    """cogs/owner.py passes the title positionally; keep that call shape working."""
    from api.paginator import PaginatorHelper

    lines = [f"line {i}" for i in range(30)]

    # Mirrors cogs/owner.py's /users call: positional title plus max_chars kwarg.
    embeds = PaginatorHelper.create_adaptive_embeds(
        lines,
        "Tracked Command Users",
        items_per_page=8,
        max_chars=3500,
        color=discord.Color.blue(),
    )
    assert embeds
    assert embeds[0].title.startswith("Tracked Command Users")

    # Mirrors cogs/reminder.py's keyword-only call.
    keyword_embeds = PaginatorHelper.create_adaptive_embeds(
        ["<t:1:R> reminder"],
        title="Your reminders",
        items_per_page=10,
        max_chars=1000,
    )
    assert keyword_embeds[0].title == "Your reminders"


def test_help_index_command_mentions() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.app_command_ids = {"qr": 1111111111, "help": 2222222222}

    index = HelpIndex(bot)

    # Root command with known ID
    assert index.get_command_mention("help") == "</help:2222222222>"

    # Subcommand with known root ID
    assert index.get_command_mention("qr text") == "</qr text:1111111111>"
    assert index.get_command_mention("qr crypto") == "</qr crypto:1111111111>"

    # Command without known ID falls back to `/{command}`
    assert index.get_command_mention("unknown") == "`/unknown`"
    assert index.get_command_mention("unknown sub") == "`/unknown sub`"


@pytest.mark.asyncio
async def test_help_view_home_layout_formatting() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    index = HelpIndex(bot)

    class ToolsCog(commands.Cog):
        qualified_name = "Tools"
        display_name = "Tools"
        group_name = "tools"

    cog = ToolsCog()

    async def cb(ctx: commands.Context[commands.Bot]) -> None:
        pass

    cmds = [commands.Command(cb, name=f"tool{i}") for i in range(19)]
    for c in cmds:
        c.cog = cog

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={cog: cmds},
        uncategorized=[],
    )

    content = view.content_component.content
    # Check that category is NOT bold and does NOT have command counts
    assert "**Tools**" not in content
    assert "Tools" in content
    assert "(19)" not in content
    assert "(19 commands)" not in content
    assert view.container.accent_colour is not None


@pytest.mark.asyncio
async def test_help_view_cog_layout_and_pagination() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.app_command_ids = {"test": 9999999999, "cmd0": 1000000000}
    index = HelpIndex(bot)

    class DummyCog(commands.Cog):
        display_name = "TestCategory"
        group_name = "Tools"
        description = "Test category for verification."

    cog = DummyCog()

    # Create 35 dummy commands to test 30 commands per page
    dummy_commands = []
    for i in range(35):

        @commands.command(name=f"cmd{i}", help=f"Description for cmd{i}")
        async def dummy_cmd(ctx: commands.Context[commands.Bot]) -> None:
            pass

        dummy_cmd.cog = cog
        dummy_commands.append(dummy_cmd)

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={cog: dummy_commands},
        uncategorized=[],
    )

    view.mode = "cog"
    view.current_cog = cog
    view._update_components()

    # 35 commands with 30 per page yields 2 total pages
    assert view.total_pages == 2
    assert view.current_page == 0

    # Content should contain the command list without -# descriptions
    content = view.content_component.content
    assert "</cmd0:1000000000>" in content
    assert "-# Description" not in content
    assert "Page 1/2 • 35 commands • Amenity" in content

    # Check interactive components when pagination is active (total_pages > 1)
    labels = [child.label for child in view.walk_children() if isinstance(child, discord.ui.Button)]
    assert "<<" in labels
    assert "<" in labels
    assert "1/2" in labels
    assert ">" in labels
    assert ">>" in labels
    assert "Home" in labels
    assert "Search" in labels
    assert "Close" not in labels

    # Text and all controls are nested in the same Components V2 container.
    assert view.children == [view.container]
    rows = [child for child in view.container.children if isinstance(child, discord.ui.ActionRow)]
    pager_row = next(row for row in rows if any(isinstance(c, HelpFirstButton) for c in row.children))
    nav_row = next(row for row in rows if any(isinstance(c, HelpSearchButton) for c in row.children))
    assert {type(c) for c in pager_row.children} == {
        HelpFirstButton,
        HelpPrevButton,
        HelpPageIndicator,
        HelpNextButton,
        HelpLastButton,
    }
    assert {type(c) for c in pager_row.children} == {
        HelpFirstButton,
        HelpPrevButton,
        HelpPageIndicator,
        HelpNextButton,
        HelpLastButton,
    }
    nav_types = {type(c) for c in nav_row.children}
    assert nav_types == {HelpHomeButton, HelpSearchButton}
    search_button = next(c for c in nav_row.children if isinstance(c, HelpSearchButton))
    assert str(search_button.emoji) == Emoji.SEARCH.value
    assert view.content_component in view.container.children
    assert view.content_length() <= 4000
    view.to_components()


@pytest.mark.asyncio
async def test_help_view_group_layout_30_commands_limit() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    index = HelpIndex(bot)

    class ToolsCog(commands.Cog):
        qualified_name = "Tools"
        display_name = "Tools"
        group_name = "tools"
        description = "Practical everyday helper tools."

    cog = ToolsCog()

    async def cb(ctx: commands.Context[commands.Bot]) -> None:
        pass

    # Tools has 17 commands (less than 30)
    cmds = [commands.Command(cb, name=f"tool_cmd_{i}", help="A tool") for i in range(17)]
    for c in cmds:
        c.cog = cog

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={cog: cmds},
        uncategorized=[],
    )

    view.mode = "group"
    view.selected_group = "tools"
    view._update_components()
    content = view.content_component.content

    # All 17 commands must be listed without "+11 more" or any truncation!
    assert "+11 more" not in content
    assert "+ more" not in content
    for i in range(17):
        assert f"`/tool_cmd_{i}`" in content

    # Since 17 <= 30 commands, total_pages == 1, no pagination buttons
    assert view.total_pages == 1
    button_labels = [c.label for c in view.walk_children() if isinstance(c, discord.ui.Button)]
    assert "<<" not in button_labels
    assert "<" not in button_labels
    assert ">" not in button_labels
    assert ">>" not in button_labels


@pytest.mark.asyncio
async def test_help_view_group_layout_no_subgroups() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.app_command_ids = {"ai": 111, "fix-grammar": 222, "image-gen": 333}
    index = HelpIndex(bot)

    class AICog(commands.Cog):
        qualified_name = "AI"
        display_name = "AI"
        group_name = "ai"
        description = "Chat, grammar, and image generation powered by AI."

    ai_cog = AICog()

    async def cb(ctx: commands.Context[commands.Bot]) -> None:
        pass

    cmd1 = commands.Command(cb, name="ai", help="Chat with AI")
    cmd2 = commands.Command(cb, name="fix-grammar", help="Fix grammar")
    cmd3 = commands.Command(cb, name="image-gen", help="Generate image")
    for c in (cmd1, cmd2, cmd3):
        c.cog = ai_cog

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={ai_cog: [cmd1, cmd2, cmd3]},
        uncategorized=[],
    )

    view.mode = "group"
    view.selected_group = "ai"
    view._update_components()
    content = view.content_component.content

    # In a group without sub-groups:
    # 1. No repeated bold group heading line like ":shine: AI"
    assert content.count("**AI**") == 0

    # 2. Commands are displayed directly under the description
    assert "Chat, grammar, and image generation powered by AI." in content
    assert "</ai:111>" in content
    assert "</fix-grammar:222>" in content
    assert "</image-gen:333>" in content

    # 3. Dropdown shows main groups instead of 1-item sub-group
    select_items = [c for c in view.walk_children() if isinstance(c, HelpCategorySelect)]
    assert len(select_items) == 1
    select = select_items[0]
    # Options are main groups (values starting with group:), not sub-cogs (cog:...)
    assert any(opt.value == "group:ai" for opt in select.options)
    assert not any(opt.value.startswith("cog:") for opt in select.options)
    assert select.placeholder == "Choose a command category..."


@pytest.mark.asyncio
async def test_help_view_group_layout_with_subgroups() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.app_command_ids = {"server": 101, "avatar": 102}
    index = HelpIndex(bot)

    class ServerUtilityCog(commands.Cog):
        qualified_name = "ServerUtility"
        display_name = "Server Utility"
        group_name = "utilities"
        description = "Server utilities."

    class UserUtilityCog(commands.Cog):
        qualified_name = "UserUtility"
        display_name = "User Utility"
        group_name = "utilities"
        description = "User utilities."

    c1 = ServerUtilityCog()
    c2 = UserUtilityCog()

    async def cb1(ctx: commands.Context[commands.Bot]) -> None:
        pass

    async def cb2(ctx: commands.Context[commands.Bot]) -> None:
        pass

    cmd1 = commands.Command(cb1, name="server", help="Server info")
    cmd1.cog = c1
    cmd2 = commands.Command(cb2, name="avatar", help="User avatar")
    cmd2.cog = c2

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={c1: [cmd1], c2: [cmd2]},
        uncategorized=[],
    )

    view.mode = "group"
    view.selected_group = "utilities"
    view._update_components()
    content = view.content_component.content

    # In a group WITH sub-groups:
    # Separate bold headings are shown for each sub-group
    assert "Server Utility**" in content
    assert "User Utility**" in content
    assert "</server:101>" in content
    assert "</avatar:102>" in content

    # Dropdown shows sub-group options for utilities
    select_items = [c for c in view.walk_children() if isinstance(c, HelpCategorySelect)]
    assert len(select_items) == 1
    select = select_items[0]
    cog_values = [opt.value for opt in select.options]
    assert "cog:ServerUtility" in cog_values
    assert "cog:UserUtility" in cog_values
    assert "Utilities" in select.placeholder


@pytest.mark.asyncio
async def test_amenity_fetch_app_commands_in_memory() -> None:
    bot = Amenity()
    try:
        assert bot.command_prefix is commands.when_mentioned
        assert bot.intents.message_content is False

        mock_global_cmds = [
            SimpleNamespace(name="ping", id=1534606769270034533),
            SimpleNamespace(name="qr", id=1534606770511806525),
        ]

        with patch.object(bot.tree, "fetch_commands", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_global_cmds
            ids = await bot.fetch_app_commands()

        assert bot.app_command_ids["ping"] == 1534606769270034533
        assert bot.app_command_ids["qr"] == 1534606770511806525
        assert ids["ping"] == 1534606769270034533

        # Check bot mention helper
        assert bot.get_app_command_mention("ping") == "</ping:1534606769270034533>"
        assert bot.get_app_command_mention("qr text") == "</qr text:1534606770511806525>"

        # Check HelpIndex mention helper
        index = HelpIndex(bot)
        assert index.get_command_mention("ping") == "</ping:1534606769270034533>"
        assert index.get_command_mention("qr text") == "</qr text:1534606770511806525>"
    finally:
        bot._blocking_executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_checkusers_ui_formatting_flagged() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    tools_cog = Tools(bot)

    now = discord.utils.utcnow()
    user1 = MagicMock(spec=discord.User)
    user1.id = 111111111111111111
    user1.name = "testuser_one"
    user1.mention = "<@111111111111111111>"
    user1.created_at = now - datetime.timedelta(days=10)
    user1.avatar = None
    user1.avatar_decoration = None
    user1.avatar_decoration_sku_id = None
    user1.avatar_decoration_data = None

    user2 = MagicMock(spec=discord.User)
    user2.id = 222222222222222222
    user2.name = "testuser_two"
    user2.mention = "<@222222222222222222>"
    user2.created_at = now - datetime.timedelta(days=200)
    user2.avatar = "some_avatar"
    user2.avatar_decoration = "some_deco"

    ctx = MagicMock()
    ctx.bot = bot
    ctx.author.id = 999
    ctx.interaction = None
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()

    bot.get_user = MagicMock(side_effect=lambda uid: {111111111111111111: user1, 222222222222222222: user2}.get(uid))

    await tools_cog.checkusers.callback(
        tools_cog, ctx, users="111111111111111111 222222222222222222", days=90, no_avatar=True
    )

    ctx.send.assert_awaited_once()
    _, kwargs = ctx.send.await_args
    embed = kwargs.get("embed")
    assert embed is not None
    # No bulky inline fields
    assert len(embed.fields) == 0

    # Header in description
    assert "> **Filters:**" in embed.description
    assert "<90d age" in embed.description
    assert "No avatar" in embed.description
    assert "> **Results:** Checked **2** total • **1** flagged" in embed.description

    # User 1 is flagged (10 days old, no avatar)
    assert "**testuser\\_one**" in embed.description
    assert "`111111111111111111`" in embed.description
    assert "-# ⏰ Created" in embed.description
    assert "<90d age (10d)" in embed.description
    assert "no avatar" in embed.description

    # User 2 is not flagged (200 days old, has avatar, deco was not requested)
    assert "testuser_two" not in embed.description


@pytest.mark.asyncio
async def test_checkusers_ui_formatting_no_matches() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    tools_cog = Tools(bot)

    now = discord.utils.utcnow()
    user = MagicMock(spec=discord.User)
    user.id = 333333333333333333
    user.name = "old_user"
    user.mention = "<@333333333333333333>"
    user.created_at = now - datetime.timedelta(days=500)
    user.avatar = "has_avatar"
    user.avatar_decoration = None
    user.avatar_decoration_sku_id = None
    user.avatar_decoration_data = None

    ctx = MagicMock()
    ctx.bot = bot
    ctx.author.id = 999
    ctx.interaction = None
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()

    bot.get_user = MagicMock(return_value=user)

    await tools_cog.checkusers.callback(tools_cog, ctx, users="333333333333333333", days=90)

    ctx.send.assert_awaited_once()
    _, kwargs = ctx.send.await_args
    embed = kwargs.get("embed")
    assert embed is not None
    assert "✅ **No flagged accounts found.**" in embed.description
    assert embed.footer.text == "Checked 1 user • Amenity"


@pytest.mark.asyncio
async def test_checkusers_ui_pagination() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    tools_cog = Tools(bot)

    now = discord.utils.utcnow()
    users = {}
    for i in range(9):
        user_id = 900000000000000000 + i
        user = MagicMock(spec=discord.User)
        user.id = user_id
        user.name = f"young_{i}"
        user.mention = f"<@{user_id}>"
        user.created_at = now - datetime.timedelta(days=10)
        user.avatar = "avatar"
        user.avatar_decoration = None
        user.avatar_decoration_sku_id = None
        user.avatar_decoration_data = None
        users[user_id] = user

    ctx = MagicMock()
    ctx.bot = bot
    ctx.author.id = 999
    ctx.interaction = None
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()

    bot.get_user = MagicMock(side_effect=lambda uid: users.get(uid))

    await tools_cog.checkusers.callback(tools_cog, ctx, users=" ".join(str(uid) for uid in users), days=90)

    _, kwargs = ctx.send.await_args
    view = kwargs.get("view")
    assert isinstance(view, EmbedPaginator)
    # 9 flagged users at 8 per page -> 2 pages
    assert len(view.embeds) == 2
    assert view.embeds[0].footer.text == "Page 1/2 • 9 flagged • Amenity"
    assert view.embeds[1].footer.text == "Page 2/2 • 9 flagged • Amenity"
    assert "young\\_8" in view.embeds[1].description


@pytest.mark.asyncio
async def test_checkusers_ui_unreachable_users() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    tools_cog = Tools(bot)

    ctx = MagicMock()
    ctx.bot = bot
    ctx.author.id = 999
    ctx.interaction = None
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()

    bot.get_user = MagicMock(return_value=None)
    with patch("cogs.tools.fetch_user_cached", new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = None
        await tools_cog.checkusers.callback(tools_cog, ctx, users="444444444444444444", days=90)

    ctx.send.assert_awaited_once()
    _, kwargs = ctx.send.await_args
    embed = kwargs.get("embed")
    assert embed is not None
    assert "⚠️ **Could not fetch 1 user(s):** `444444444444444444`" in embed.description


@pytest.mark.asyncio
async def test_owner_and_jishaku_excluded_from_help() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    bot.owner_id = 999
    index = HelpIndex(bot)
    owner_user = SimpleNamespace(id=999)

    class NormalCog(commands.Cog):
        qualified_name = "Tools"
        display_name = "Tools"
        group_name = "tools"

        @commands.command(name="toolcmd")
        async def toolcmd(self, ctx: commands.Context[commands.Bot]) -> None:
            pass

    class OwnerCog(commands.Cog):
        qualified_name = "Owner"
        display_name = "Owner"
        group_name = "owner"

        @commands.command(name="eval")
        async def eval(self, ctx: commands.Context[commands.Bot]) -> None:
            pass

    class JishakuCog(commands.Cog):
        qualified_name = "Jishaku"
        display_name = "Jishaku"

        @commands.command(name="jsk")
        async def jsk(self, ctx: commands.Context[commands.Bot]) -> None:
            pass

    class HelpCogMock(commands.Cog):
        qualified_name = "HelpCog"
        display_name = "Help"

    normal_cog = NormalCog()
    owner_cog = OwnerCog()
    jishaku_cog = JishakuCog()
    helpcog = HelpCogMock()

    await bot.add_cog(normal_cog)
    await bot.add_cog(owner_cog)
    await bot.add_cog(jishaku_cog)
    await bot.add_cog(helpcog)

    normal_cmd = normal_cog.toolcmd
    owner_cmd = owner_cog.eval
    jishaku_cmd = jishaku_cog.jsk

    # 1. Owner and Jishaku commands are never visible, even to bot owner
    assert await index.can_see_command(normal_cmd, owner_user) is True
    assert await index.can_see_command(owner_cmd, owner_user) is False
    assert await index.can_see_command(jishaku_cmd, owner_user) is False

    # 2. visible_cogs excludes Owner, Jishaku, and HelpCog
    cogs_mapping = await index.visible_cogs(owner_user)
    cog_names = [cog.qualified_name for cog in cogs_mapping]
    assert "Tools" in cog_names
    assert "Owner" not in cog_names
    assert "Jishaku" not in cog_names
    assert "HelpCog" not in cog_names


@pytest.mark.asyncio
async def test_help_view_other_and_owner_groups_discarded() -> None:
    bot = commands.Bot(command_prefix=",", intents=discord.Intents.default())
    index = HelpIndex(bot)

    class ToolsCog(commands.Cog):
        qualified_name = "Tools"
        display_name = "Tools"
        group_name = "tools"

    class OtherCog(commands.Cog):
        qualified_name = "RandomCog"
        display_name = "Random"
        group_name = "other"

    class ExplicitOwnerCog(commands.Cog):
        qualified_name = "AdminCog"
        display_name = "Admin"
        group_name = "owner"

    c1 = ToolsCog()
    c2 = OtherCog()
    c3 = ExplicitOwnerCog()

    async def cb(ctx: commands.Context[commands.Bot]) -> None:
        pass

    cmd1 = commands.Command(cb, name="tool")
    cmd1.cog = c1
    cmd2 = commands.Command(cb, name="rand")
    cmd2.cog = c2
    cmd3 = commands.Command(cb, name="adm")
    cmd3.cog = c3

    user = SimpleNamespace(id=123)
    view = HelpView(
        bot=bot,
        index=index,
        author=user,  # type: ignore[arg-type]
        prefix="/",
        mapping={c1: [cmd1], c2: [cmd2], c3: [cmd3]},
        uncategorized=[],
    )

    # 1. _groups() must not contain "other" or "owner"
    groups = view._groups()
    assert "other" not in groups
    assert "owner" not in groups
    assert groups == ["tools"]

    # 2. Home content must not advertise "Other" or "Owner"
    content = view.content_component.content
    assert "Other" not in content
    assert "Owner" not in content
    assert "Tools" in content

    # 3. Dropdown options must not contain "other" or "owner"
    options = view._group_options()
    assert not any(opt.value in ("group:other", "group:owner") for opt in options)
    assert not any(opt.label in ("Other", "Owner") for opt in options)


@pytest.mark.asyncio
async def test_view_timeout_with_deleted_message_suppresses_not_found() -> None:
    from api.buttons import ConfirmView
    from cogs.games import MinesView, TicTacToeView
    from cogs.template import TemplateBuilderView, TemplateListView
    from cogs.tools import TOTPRefreshView

    # Create dummy response for discord.NotFound
    response = MagicMock()
    response.status = 404
    response.reason = "Not Found"
    not_found_err = discord.NotFound(response, {"message": "Unknown Message", "code": 10008})

    # 1. EmbedPaginator
    paginator = EmbedPaginator([discord.Embed(title="Page 1")])
    mock_msg = MagicMock()
    mock_msg.edit = AsyncMock(side_effect=not_found_err)
    paginator.message = mock_msg
    await paginator.on_timeout()
    assert all(child.disabled for child in paginator.walk_children() if hasattr(child, "disabled"))

    # 2. ConfirmView
    confirm = ConfirmView(author_id=123, prompt="Are you sure?")
    confirm_msg = MagicMock()
    confirm_msg.edit = AsyncMock(side_effect=not_found_err)
    confirm.message = confirm_msg
    await confirm.on_timeout()
    assert all(child.disabled for child in confirm.walk_children() if hasattr(child, "disabled"))

    # 3. TicTacToeView
    tictactoe = TicTacToeView(
        player_x=SimpleNamespace(id=1, mention="<@1>"), player_o=SimpleNamespace(id=2, mention="<@2>")
    )
    ttt_msg = MagicMock()
    ttt_msg.edit = AsyncMock(side_effect=not_found_err)
    tictactoe.message = ttt_msg
    await tictactoe.on_timeout()
    assert all(child.disabled for child in tictactoe.walk_children() if hasattr(child, "disabled"))

    # 4. MinesView
    mines = MinesView(player_id=1, num_mines=3)
    mines_msg = MagicMock()
    mines_msg.edit = AsyncMock(side_effect=not_found_err)
    mines.message = mines_msg
    await mines.on_timeout()
    assert all(child.disabled for child in mines.walk_children() if hasattr(child, "disabled"))

    # 5. TemplateListView
    tlist = TemplateListView(author_id=123, templates=[{"id": 1, "name": "test"}])
    tlist_msg = MagicMock()
    tlist_msg.edit = AsyncMock(side_effect=not_found_err)
    tlist.message = tlist_msg
    await tlist.on_timeout()
    assert all(child.disabled for child in tlist.walk_children() if hasattr(child, "disabled"))

    # 6. TemplateBuilderView
    tbuilder = TemplateBuilderView(MagicMock(), author_id=123, name="tpl", data={})
    tb_msg = MagicMock()
    tb_msg.edit = AsyncMock(side_effect=not_found_err)
    tbuilder.message = tb_msg
    await tbuilder.on_timeout()
    assert all(child.disabled for child in tbuilder.walk_children() if hasattr(child, "disabled"))

    # 7. TOTPRefreshView
    totp = TOTPRefreshView("JBSWY3DPEHPK3PXP", author_id=123)
    totp_msg = MagicMock()
    totp_msg.edit = AsyncMock(side_effect=not_found_err)
    totp.message = totp_msg
    await totp.on_timeout()
    assert all(child.disabled for child in totp.walk_children() if hasattr(child, "disabled"))


@pytest.mark.asyncio
async def test_view_on_error_suppresses_not_found() -> None:
    from api.buttons import ConfirmView
    from cogs.games import MinesView, TicTacToeView
    from cogs.template import TemplateBuilderView, TemplateListView
    from cogs.tools import TOTPRefreshView

    response = MagicMock()
    response.status = 404
    response.reason = "Not Found"
    not_found_err = discord.NotFound(response, {"message": "Unknown Message", "code": 10008})
    interaction = MagicMock()
    item = MagicMock()

    views = [
        EmbedPaginator([discord.Embed(title="test")]),
        ConfirmView(author_id=123, prompt="Are you sure?"),
        TicTacToeView(player_x=SimpleNamespace(id=1, mention="<@1>"), player_o=SimpleNamespace(id=2, mention="<@2>")),
        MinesView(player_id=1, num_mines=3),
        TemplateListView(author_id=123, templates=[]),
        TemplateBuilderView(MagicMock(), author_id=123, name="tpl", data={}),
        TOTPRefreshView("JBSWY3DPEHPK3PXP", author_id=123),
    ]

    for view in views:
        # Should cleanly return None and not raise
        await view.on_error(interaction, not_found_err, item)


@pytest.mark.asyncio
async def test_bot_error_handlers_suppress_unknown_interaction_and_message() -> None:
    bot = Amenity()

    ctx = MagicMock()
    ctx.command = MagicMock()
    ctx.reply = AsyncMock()
    ctx.send_help = AsyncMock()

    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    resp_10062 = MagicMock(status=404, reason="Not Found")
    err_10062 = discord.NotFound(resp_10062, {"message": "Unknown interaction", "code": 10062})

    resp_10008 = MagicMock(status=404, reason="Not Found")
    err_10008 = discord.NotFound(resp_10008, {"message": "Unknown Message", "code": 10008})

    # on_command_error should silently return on 10062 and 10008
    await bot.on_command_error(ctx, err_10062)
    await bot.on_command_error(ctx, err_10008)
    ctx.reply.assert_not_called()
    ctx.send_help.assert_not_called()

    # on_app_command_error should silently return on 10062 and 10008
    await bot.on_app_command_error(interaction, err_10062)
    await bot.on_app_command_error(interaction, err_10008)
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_not_called()


@pytest.mark.asyncio
async def test_on_app_command_error_send_error_handles_view_properly() -> None:
    bot = Amenity()
    interaction = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    class TestUnexpectedError(discord.app_commands.AppCommandError):
        pass

    with patch("core.amenity.log_app_command_error", AsyncMock()), pytest.raises(TestUnexpectedError):
        await bot.on_app_command_error(interaction, TestUnexpectedError("unexpected failure"))

    interaction.response.send_message.assert_called_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "view" in kwargs
    assert isinstance(kwargs["view"], discord.ui.LayoutView)


def test_tictactoe_early_tie_detection() -> None:
    from cogs.games import TicTacToeView

    view = TicTacToeView(
        player_x=SimpleNamespace(id=1, mention="<@1>"),
        player_o=SimpleNamespace(id=2, mention="<@2>"),
    )

    # Empty board: still fully winnable for both players.
    assert view.check_winner() is None

    # Every line contains both symbols, but a square is still empty —
    # game should be declared a tie without filling the board.
    view.board = [
        [view.X, view.X, view.O],
        [view.O, view.O, view.X],
        [view.X, view.O, 0],
    ]
    assert any(cell == 0 for row in view.board for cell in row)
    assert view.check_winner() == TicTacToeView.TIE

    # A live board with empty squares keeps the game running.
    view.board = [
        [view.X, view.O, 0],
        [0, view.X, 0],
        [0, 0, view.O],
    ]
    assert view.check_winner() is None

    # A line that is still winnable by one side is not a tie.
    view.board = [
        [view.X, view.O, view.O],
        [view.O, view.X, view.X],
        [view.X, view.O, 0],
    ]
    # Diagonal [X, X, -] is live for X, so the game continues.
    assert view.check_winner() is None
