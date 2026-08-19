import sys
import logging
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

from config import BOT_TOKEN, IS_MTPROTO_ENABLED
from handlers import register_handlers
from mtproto_uploader import start_mtproto, stop_mtproto

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


async def on_startup(app):
    """Startup hook to initialize MTProto client if enabled."""
    if IS_MTPROTO_ENABLED:
        logger.info("Initializing Telegram MTProto Client...")
        await start_mtproto()
    else:
        logger.info("MTProto is not configured. Running with standard 50MB HTTP Bot API.")


async def on_shutdown(app):
    """Shutdown hook to cleanly close MTProto client."""
    await stop_mtproto()


def main():
    """Main entry point to start the bot."""
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error(
            "❌ ERROR: BOT_TOKEN is missing or not configured!\n"
            "Please open .env and replace YOUR_TELEGRAM_BOT_TOKEN_HERE with your bot token from @BotFather."
        )
        print("\n" + "=" * 60)
        print(" [!] Please set your Telegram Bot Token in the .env file")
        print(" 1. Talk to @BotFather on Telegram (https://t.me/botfather)")
        print(" 2. Create a new bot with /newbot to obtain your token")
        print(" 3. Paste it inside .env: BOT_TOKEN=123456789:ABCdef...")
        print("=" * 60 + "\n")
        sys.exit(1)

    logger.info("Starting Telegram Video Downloader Bot...")

    # Generous HTTP request timeouts to prevent httpx.ReadTimeout during large operations
    request_client = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=60.0,
    )

    # Build Telegram Bot application with MTProto lifecycle hooks and extended timeouts
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request_client)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    # Register handlers
    register_handlers(application)

    logger.info("✅ Bot is online and listening for messages! Press Ctrl+C to stop.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
