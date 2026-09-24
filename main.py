import asyncio
import logging
import os
import subprocess

from colorama import Fore
from dotenv import load_dotenv

from core import Amenity

Amenity = Amenity()
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format=(Fore.CYAN + "[%(name)s] [%(module)s.%(funcName)s:%(lineno)d] → %(message)s\n"),
)
logger = logging.getLogger(__name__)


async def main() -> None:
    async with Amenity:
        subprocess.run("clear")
        TOKEN = os.getenv("TOKEN")
        if not TOKEN:
            logger.error("TOKEN not found in environment variables.")
            return
        await Amenity.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
