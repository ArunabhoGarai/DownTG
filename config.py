import os
from pathlib import Path
from dotenv import load_dotenv

# Base Directory
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

# Telegram MTProto Credentials for 2GB file upload support (get from my.telegram.org)
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
IS_MTPROTO_ENABLED = bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)

# Max allowed file size (2000MB / 2GB for MTProto, 50MB for standard HTTP Bot API)
DEFAULT_MAX_MB = 2000 if IS_MTPROTO_ENABLED else 50
_raw_max_mb = os.getenv("MAX_FILE_SIZE_MB", "").strip()
MAX_FILE_SIZE_MB = int(_raw_max_mb) if _raw_max_mb.isdigit() else DEFAULT_MAX_MB
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024

# Download directories
DOWNLOAD_DIR = BASE_DIR / os.getenv("DOWNLOAD_DIR", "downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Concurrency limits for server/EC2 resource protection (settable in .env)
_raw_concurrent = os.getenv("MAX_CONCURRENT_DOWNLOADS", "2").strip()
MAX_CONCURRENT_DOWNLOADS = int(_raw_concurrent) if _raw_concurrent.isdigit() and int(_raw_concurrent) > 0 else 2

# Quality presets
best_label = "⚡ Best Quality (<2GB)" if IS_MTPROTO_ENABLED else "⚡ Best Quality (<50MB)"
QUALITIES = {
    "best": best_label,
    "720": "720p HD",
    "480": "480p SD",
    "360": "360p Low",
    "audio": "🎵 Audio Only (MP3)",
}

# Browserless Remote CAPTCHA Solver Configuration (Docker container)
BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "http://127.0.0.1:3000").strip().rstrip("/")
BROWSERLESS_PUBLIC_URL = os.getenv("BROWSERLESS_PUBLIC_URL", "").strip().rstrip("/")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()
_raw_captcha_timeout = os.getenv("CAPTCHA_TIMEOUT_SEC", "180").strip()
CAPTCHA_TIMEOUT_SEC = int(_raw_captcha_timeout) if _raw_captcha_timeout.isdigit() else 180
IS_BROWSERLESS_ENABLED = bool(os.getenv("BROWSERLESS_URL") or os.getenv("BROWSERLESS_PUBLIC_URL"))
