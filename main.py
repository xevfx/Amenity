import asyncio
import logging
import os

from colorama import Fore
from dotenv import load_dotenv

from core import Amenity

load_dotenv()
bot = Amenity()
logging.basicConfig(
    level=logging.INFO,
    format=(Fore.CYAN + "[%(name)s] [%(module)s.%(funcName)s:%(lineno)d] → %(message)s\n"),
)
logger = logging.getLogger(__name__)


async def main() -> None:
    async with bot:
        os.system("clear")
        TOKEN = os.getenv("TOKEN")
        if not TOKEN:
            logger.error("TOKEN not found in environment variables.")
            return
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
