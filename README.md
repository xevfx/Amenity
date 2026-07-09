# Amenity

Amenity is a Discord bot with utility, AI, crypto, reminder, template, game, GitHub, and moderation-style helper commands. It is built with `discord.py` and loads command modules from the `cogs/` directory.

![Amenity](https://github.com/xevfx/amenity/blob/main/assets/bot/banner.png)

## Requirements

- Python 3.12 or newer
- A Discord application and bot token
- The Message Content intent enabled for the bot in the Discord Developer Portal
- Optional API keys/Secrets for AI, crypto, GitHub, and webhook logging features

## Setup

1. Clone the repository and enter the project directory.

   ```bash
   git clone https://github.com/xevfx/amenity.git
   cd amenity
   ```

2. Create and activate a virtual environment.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   On Windows PowerShell:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies.

   ```bash
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. Create your environment file.

   ```bash
   cp .env.example .env
   ```

5. Edit `.env` and add at least your Discord bot token.

   ```env
   TOKEN=your_discord_bot_token
   ```

6. Start the bot.

   ```bash
   python main.py
   ```

## Configuration

The bot reads settings from `.env` with `python-dotenv`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `TOKEN` | Yes | Discord bot token used to log in. |
| `GUILD_ID` | No | Optional development guild ID. The current code syncs commands globally by default. |
| `USAGE_HOOK` | No | Discord webhook URL for command usage logs. |
| `ERROR_HOOK` | No | Discord webhook URL for command error logs. |
| `GH_TOKEN` or `GITHUB_TOKEN` | No | GitHub token for GitHub-related commands. |
| `GROQ_API_KEY` / `GROQ` | No | Groq API key or alias used by AI commands. |
| `GOOGLE_API_KEY` / `GOOGLE` | No | Google API key or alias used by AI commands. |
| `OPENROUTER_API_KEY` / `OPENROUTER` | No | OpenRouter API key or alias used by AI commands. |
| `POLLINATIOAI` | No | Optional AI provider setting used by AI commands. |
| `BLOCKCYPHER_TOKEN` | No | Optional BlockCypher token for crypto commands. |
| `ETHERSCAN_API_KEY` | No | Optional Etherscan API key for crypto commands. |
| `BSCSCAN_API_KEY` | No | Optional BscScan API key for crypto commands. |

Keep `.env` private. Do not commit real tokens or API keys.

## Discord Bot Setup

1. Open the Discord Developer Portal.
2. Create an application, then create a bot for it.
3. Copy the bot token into `TOKEN` in `.env`.
4. Enable the required privileged intents:
   - Message Content Intent
   - Server Members Intent, if you use member-related commands
5. Invite the bot with the permissions and scopes your commands need.

The default text command prefix is `,`. Slash and hybrid commands are synced when the bot starts.

## Development

Run tests:

```bash
pytest
```

Run the command workflow test only:

```bash
pytest tests/test_command_workflow.py
```

Run linting:

```bash
ruff check .
```

Format code:

```bash
ruff format .
```

The command workflow test writes a report to:

```text
artifacts/command-workflow-report.json
```

Optional test limits can be adjusted with:

```bash
COMMAND_TEST_MAX_SECONDS=5.0 COMMAND_TEST_MAX_RSS_DELTA_MB=128.0 pytest
```

## Project Structure

```text
api/        Shared helpers for HTTP, logging, pagination, parsing, and UI components.
cogs/       Discord command modules loaded automatically at startup.
core/       Bot class, checks, help command, cache, and installed-user tracking.
data/       Local SQLite databases used by bot features.
docs/       Generated or maintained documentation artifacts.
tests/      Pytest suite.
main.py     Application entry point.
```

## Troubleshooting

- `TOKEN not found in environment variables.`: create `.env` and set `TOKEN`.
- Commands do not appear immediately: global Discord application command sync can take time to propagate.
- Message commands do not respond: enable Message Content Intent in the Discord Developer Portal.
- Import errors: activate the virtual environment and rerun `pip install -r requirements.txt`.
- Webhook logs are missing: check that `USAGE_HOOK` and `ERROR_HOOK` are valid Discord webhook URLs.

## License

This project is licensed under the terms in [LICENSE](LICENSE).
