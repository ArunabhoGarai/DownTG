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

# Browserbase Cloud Solver Configuration (Cloud-hosted interactive live browser)
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY", "").strip()
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "").strip()

# Local / Browserless Remote CAPTCHA Solver Configuration
BROWSERLESS_URL = os.getenv("BROWSERLESS_URL", "").strip().rstrip("/")
BROWSERLESS_PUBLIC_URL = os.getenv("BROWSERLESS_PUBLIC_URL", "").strip().rstrip("/")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()
_raw_captcha_timeout = os.getenv("CAPTCHA_TIMEOUT_SEC", "180").strip()
CAPTCHA_TIMEOUT_SEC = int(_raw_captcha_timeout) if _raw_captcha_timeout.isdigit() else 180
IS_BROWSERLESS_ENABLED = bool(BROWSERBASE_API_KEY or os.getenv("BROWSERLESS_URL") or os.getenv("SERVER_PUBLIC_IP"))

# Diskwala MiniApp Direct API Configuration
DISKWALA_BEARER_TOKEN = os.getenv("DISKWALA_BEARER_TOKEN", "").strip()

# TeraBox MiniApp Direct API Configuration (teradownloader.pro)
TERABOX_BEARER_TOKEN = os.getenv("TERABOX_BEARER_TOKEN", "").strip()

# Telethon MiniApp Bearer token max age in hours before on-demand refresh (default: 2 hours)
_raw_max_age = (
    os.getenv("TOKEN_MAX_AGE_HOURS", "")
    or os.getenv("TOKEN_REFRESH_INTERVAL_HOURS", "")
    or os.getenv("TOKEN_REFRESH_HOURS", "2")
).strip()
try:
    TOKEN_MAX_AGE_HOURS = float(_raw_max_age) if _raw_max_age else 2.0
except ValueError:
    TOKEN_MAX_AGE_HOURS = 2.0
TOKEN_REFRESH_INTERVAL_HOURS = TOKEN_MAX_AGE_HOURS

# Google Photos OAuth 2.0 Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth2callback").strip()

# Delay in seconds between sequential transfers (user requirement: 10s timeout)
TRANSFER_DELAY_SEC = int(os.getenv("TRANSFER_DELAY_SEC", "10"))
