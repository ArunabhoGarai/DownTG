import sys
import logging
from telegram.ext import ApplicationBuilder

from config import BOT_TOKEN
from handlers import register_handlers

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


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

    # Build Telegram Bot application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    register_handlers(application)

    logger.info("✅ Bot is online and listening for messages! Press Ctrl+C to stop.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
