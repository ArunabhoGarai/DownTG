import re
import os
import json
import time
import logging
import asyncio
from typing import Dict, Any, List, Optional, Tuple, Callable
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
)
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import (
    QUALITIES,
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    MAX_CONCURRENT_DOWNLOADS,
    ADMIN_USER_ID,
    BASE_DIR,
    TOKEN_REFRESH_INTERVAL_HOURS,
)
from downloader import (
    extract_media_info,
    download_media,
    remove_file_safely,
    get_platform_badge,
    format_duration,
    format_bytes,
)
from instagram_downloader import (
    extract_instagram_info,
    download_instagram_media,
)
from facebook_downloader import (
    extract_facebook_info,
    download_facebook_media,
)
from generic_downloader import (
    extract_generic_info,
    download_generic_media,
)
from terabox_downloader import (
    is_terabox_url,
    extract_terabox_info,
    download_terabox_media,
    save_terabox_token,
    get_terabox_token,
)
from diskwala_downloader import (
    is_diskwala_url,
    extract_diskwala_info,
    download_diskwala_media,
    save_diskwala_token,
    get_diskwala_token,
)
from mtproto_uploader import (
    is_mtproto_active,
    upload_media_mtproto,
    download_media_mtproto,
)
from video_streaming_helper import prepare_video_for_telegram
from vnc_manager import (
    start_vnc_capture_session,
    start_autovnc_session,
    stop_vnc_session,
)
from link_protection import (
    is_link_on_cooldown,
    mark_link_in_progress,
    mark_link_completed,
    mark_link_failed,
    clear_link_cooldowns,
    COOLDOWN_ERROR_MESSAGE,
)
from telethon_extractor import fetch_telethon_bearer
from stats_manager import (
    record_resolved_link,
    format_overview_text,
    format_top_users_text,
    format_platform_breakdown_text,
    format_recent_activity_text,
    format_user_detail_text,
    format_personal_stats_text,
    build_stats_keyboard,
    get_user_stats,
)
from terabox_account_manager import (
    fetch_account_videos,
    save_account_cookie,
)
from google_photos_manager import (
    get_authorization_url,
    exchange_code_for_tokens,
    is_photos_authenticated,
    load_transferred_registry,
    clear_transferred_registry,
    mark_as_transferred,
    fetch_app_created_media_items,
    token_has_read_scope,
    upload_video_to_google_photos,
    record_transferred_video,
)
from terabox_to_gphotos_service import (
    execute_transfer_job,
    is_transfer_in_progress,
    cancel_current_transfer,
    skip_current_transfer,
    check_terabox_vs_google_photos,
)
from telegraph_extractor import extract_all_download_links

logger = logging.getLogger(__name__)

DEV_RESTRICTED_MESSAGE = "⛔ This command is restricted to the bot developer."


def is_admin(user_id: int) -> bool:
    """Checks if a user ID is the configured bot developer/administrator."""
    if not ADMIN_USER_ID:
        return False
    admin_ids = [aid.strip() for aid in str(ADMIN_USER_ID).split(",") if aid.strip()]
    return str(user_id) in admin_ids


# Authorization file storage
DATA_DIR = BASE_DIR / "data"
GROUPS_FILE = DATA_DIR / "allowed_groups.json"
USERS_FILE = DATA_DIR / "allowed_users.json"


def _ensure_data_file():
    """Ensures data directory and json storage files exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not GROUPS_FILE.exists():
        try:
            with open(GROUPS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=2)
        except Exception:
            pass
    if not USERS_FILE.exists():
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f, indent=2)
        except Exception:
            pass


def load_allowed_users() -> Dict[str, Dict[str, Any]]:
    """Loads allowed DM users from disk."""
    _ensure_data_file()
    try:
        if USERS_FILE.exists():
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {USERS_FILE}: {e}")
    return {}


def save_allowed_users(users: Dict[str, Dict[str, Any]]) -> bool:
    """Saves allowed DM users to disk."""
    _ensure_data_file()
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to write {USERS_FILE}: {e}")
        return False


def is_user_allowed(user_id: int) -> bool:
    """Checks if a user is authorized to use the bot in private DMs."""
    if is_admin(user_id):
        return True
    users = load_allowed_users()
    return str(user_id) in users


def allow_user(user_id: int, note: str = "") -> bool:
    """Authorizes a user to use the bot in private DMs."""
    users = load_allowed_users()
    users[str(user_id)] = {
        "user_id": user_id,
        "note": note or "Allowed Member",
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return save_allowed_users(users)


def disallow_user(user_id: int) -> bool:
    """Revokes a user's authorization to use the bot in private DMs."""
    users = load_allowed_users()
    key = str(user_id)
    if key in users:
        del users[key]
        return save_allowed_users(users)
    return True


def list_allowed_users() -> List[Dict[str, Any]]:
    """Returns list of allowed DM users."""
    users = load_allowed_users()
    return list(users.values())


def load_allowed_groups() -> Dict[str, Dict[str, Any]]:
    """Loads allowed groups from disk."""
    _ensure_data_file()
    try:
        if GROUPS_FILE.exists():
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {GROUPS_FILE}: {e}")
    return {}


def save_allowed_groups(groups: Dict[str, Dict[str, Any]]) -> bool:
    """Saves allowed groups to disk."""
    _ensure_data_file()
    try:
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(groups, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to write {GROUPS_FILE}: {e}")
        return False


def is_group_allowed(chat_id: int) -> bool:
    """Checks if a group chat ID is allowed."""
    groups = load_allowed_groups()
    return str(chat_id) in groups


def enable_group(chat_id: int, title: str = "") -> bool:
    """Enables bot usage in a specific group chat."""
    groups = load_allowed_groups()
    groups[str(chat_id)] = {
        "chat_id": chat_id,
        "title": title or "Unnamed Group",
    }
    return save_allowed_groups(groups)


def disable_group(chat_id: int) -> bool:
    """Disables bot usage in a specific group chat."""
    groups = load_allowed_groups()
    key = str(chat_id)
    if key in groups:
        del groups[key]
        return save_allowed_groups(groups)
    return True


def list_allowed_groups() -> List[Dict[str, Any]]:
    """Returns list of allowed groups."""
    groups = load_allowed_groups()
    return list(groups.values())


# Concurrency semaphore (initialized lazily)
_SEMAPHORE: asyncio.Semaphore = None
ACTIVE_TASKS: Dict[str, asyncio.Task] = {}
TASK_REQUESTERS: Dict[str, int] = {}
ACTIVE_QUEUES: Dict[str, Dict[str, Any]] = {}


def build_cancel_queue_keyboard(queue_id: str) -> InlineKeyboardMarkup:
    """Builds inline keyboard with a Cancel button for multi-link queues."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Queue", callback_data=f"cancel_queue:{queue_id}")],
    ])


def get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _SEMAPHORE


def get_active_downloads_count() -> int:
    """Returns number of currently running download tasks."""
    finished = [tid for tid, t in list(ACTIVE_TASKS.items()) if t.done()]
    for tid in finished:
        ACTIVE_TASKS.pop(tid, None)
    return len(ACTIVE_TASKS)

# URL matching regex
URL_REGEX = re.compile(
    r'(https?://(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?://[a-zA-Z0-9]+\.[^\s]{2,})',
    re.IGNORECASE
)

# Global short cache for callback query payloads to keep callback_data under 64 bytes
URL_CACHE: Dict[str, Dict[str, Any]] = {}
MAX_FORMAT_CHOICES = 8


def is_youtube_url(url: str) -> bool:
    """Check if URL belongs to YouTube."""
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def is_instagram_url(url: str) -> bool:
    """Check if URL belongs to Instagram."""
    return "instagram.com" in url.lower()


def is_facebook_url(url: str) -> bool:
    """Check if URL belongs to Facebook."""
    u = url.lower()
    return "facebook.com" in u or "fb.watch" in u or "fb.com" in u


async def route_extract_info(
    url: str,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Routes metadata extraction to the dedicated platform downloader."""
    if is_terabox_url(url):
        return await extract_terabox_info(
            url,
            notify_admin_callback=notify_admin_callback,
            progress_updater=progress_updater,
        )
    elif is_diskwala_url(url):
        return await extract_diskwala_info(url, progress_updater=progress_updater)
    elif is_youtube_url(url):
        return await extract_media_info(url)
    elif is_instagram_url(url):
        return await extract_instagram_info(url)
    elif is_facebook_url(url):
        return await extract_facebook_info(url)
    else:
        return await extract_generic_info(url)


async def notify_admin_of_captcha(bot, inspector_url: str, target_url: str):
    """Sends a private DM with the interactive CAPTCHA link to the bot admin."""
    logger.info(f"notify_admin_of_captcha triggered! Inspector URL: {inspector_url}")
    if not ADMIN_USER_ID:
        logger.warning("No ADMIN_USER_ID configured in .env; cannot send private CAPTCHA alert.")
        return
    admin_ids = [aid.strip() for aid in str(ADMIN_USER_ID).split(",") if aid.strip()]
    # Extract page_id from inspector_url if possible
    page_id = ""
    if "/devtools/page/" in inspector_url:
        page_id = inspector_url.split("/devtools/page/")[1].split("?")[0].split("&")[0]

    for aid in admin_ids:
        try:
            try:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🧩 Open Live Captcha Solver", url=inspector_url)],
                    [InlineKeyboardButton("✅ I Have Solved It", callback_data=f"captcha_solved:{page_id}")],
                ])
                await bot.send_message(
                    chat_id=int(aid),
                    text=(
                        "🧩 **TeraBox Human Verification Required!**\n\n"
                        "A download encountered a slider captcha.\n\n"
                        f"🔗 **Target URL:** `{target_url[:80]}`\n\n"
                        f"👉 [1. Open Captcha Solver in Browser]({inspector_url})\n"
                        "👉 **2. Solve the slider, then tap [✅ I Have Solved It] below!**\n\n"
                        "⏱️ *Session will wait for 3 minutes.*"
                    ),
                    reply_markup=keyboard,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
                logger.info(f"Successfully sent CAPTCHA notification with buttons to admin ID {aid}")
            except Exception as btn_err:
                logger.warning(f"Failed to send button captcha notification to admin {aid}: {btn_err}. Retrying with plain text URL...")
                # Fallback to plain text message (guarantees delivery even if Telegram button URL parser rejects complex query strings)
                await bot.send_message(
                    chat_id=int(aid),
                    text=(
                        "🧩 TeraBox Human Verification Required!\n"
                        "A download encountered a slider captcha.\n\n"
                        f"Target: {target_url[:80]}\n\n"
                        f"Solver URL:\n{inspector_url}\n\n"
                        "⏱️ Session will wait for 3 minutes."
                    ),
                )
                logger.info(f"Successfully sent fallback plain-text CAPTCHA notification to admin ID {aid}")
        except Exception as e:
            logger.error(f"Failed to send captcha notification to admin {aid}: {e}")


async def route_download_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Routes media download to the dedicated platform downloader."""
    if is_terabox_url(url):
        return await download_terabox_media(
            url,
            quality=quality,
            format_selector=format_selector,
            notify_admin_callback=notify_admin_callback,
            progress_updater=progress_updater,
        )
    elif is_diskwala_url(url):
        return await download_diskwala_media(
            url,
            quality=quality,
            format_selector=format_selector,
            notify_admin_callback=notify_admin_callback,
            progress_updater=progress_updater,
        )
    elif is_youtube_url(url):
        return await download_media(url, quality=quality, format_selector=format_selector)
    elif is_instagram_url(url):
        return await download_instagram_media(url, quality=quality, format_selector=format_selector)
    elif is_facebook_url(url):
        return await download_facebook_media(url, quality=quality, format_selector=format_selector)
    else:
        return await download_generic_media(url, quality=quality, format_selector=format_selector)


def generate_cache_key(user_id: int) -> str:
    return f"{user_id}_{int(time.time() * 1000) % 1000000}"


def build_format_choices(info: Dict[str, Any]) -> List[Dict[str, str]]:
    """Return a compact set of real, downloadable video format choices."""
    candidates = []
    for fmt in info.get("formats") or []:
        format_id = str(fmt.get("format_id") or "")
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")

        # Do not present storyboards, audio-only streams, DRM formats, or
        # entries with no direct media URL as download choices.
        if (
            not format_id
            or not fmt.get("url")
            or fmt.get("has_drm")
            or not vcodec
            or vcodec == "none"
        ):
            continue

        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        has_audio = bool(acodec and acodec != "none")
        extension = str(fmt.get("ext") or "video").upper()
        size = format_bytes(fmt.get("filesize") or fmt.get("filesize_approx"))
        resolution = f"{height}p" if height else (f"{width}w" if width else "Video")
        audio_note = "" if has_audio else " + audio"
        try:
            bitrate = float(fmt.get("tbr") or 0)
        except (TypeError, ValueError):
            bitrate = 0

        # For a video-only stream, merge the exact selected video with the
        # best available audio. If no audio exists, retain the selected video
        # rather than silently changing to a different video format.
        selector = format_id if has_audio else f"{format_id}+bestaudio/{format_id}"
        label = f"{resolution} | {extension} | {size}{audio_note} | {format_id}"
        candidates.append({
            "selector": selector,
            "label": label[:64],
            "height": height,
            "width": width,
            "has_audio": has_audio,
            "extension": extension,
            "bitrate": bitrate,
        })

    # Prefer a direct (video+audio) file, then MP4, for each resolution. One
    # format per resolution keeps the Telegram keyboard useful and compact.
    candidates.sort(
        key=lambda item: (
            item["height"],
            item["width"],
            item["has_audio"],
            item["extension"] == "MP4",
            item["bitrate"],
        ),
        reverse=True,
    )

    choices = []
    seen_resolutions = set()
    for candidate in candidates:
        resolution_key = (candidate["width"], candidate["height"])
        if resolution_key in seen_resolutions:
            continue
        seen_resolutions.add(resolution_key)
        choices.append({"selector": candidate["selector"], "label": candidate["label"]})

    # Show lower resolutions first; they are more likely to fit Telegram's
    # file-size limit.
    choices.reverse()
    return choices[:MAX_FORMAT_CHOICES]


def build_quality_keyboard(cache_key: str) -> InlineKeyboardMarkup:
    """Build the standard quality menu for a cached URL."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚡ Best Quality (<50MB)", callback_data=f"dl:best:{cache_key}"),
        ],
        [
            InlineKeyboardButton("🎬 720p", callback_data=f"dl:720:{cache_key}"),
            InlineKeyboardButton("📺 480p", callback_data=f"dl:480:{cache_key}"),
            InlineKeyboardButton("📱 360p", callback_data=f"dl:360:{cache_key}"),
        ],
        [
            InlineKeyboardButton("🎛 Available formats", callback_data=f"formats:{cache_key}"),
        ],
        [
            InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"dl:audio:{cache_key}"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{cache_key}"),
        ],
    ])


def build_format_keyboard(cache_key: str, choices: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    """Build a menu whose buttons select a concrete yt-dlp format ID."""
    keyboard = [
        [InlineKeyboardButton(choice["label"], callback_data=f"fmt:{index}:{cache_key}")]
        for index, choice in enumerate(choices)
    ]
    keyboard.append([
        InlineKeyboardButton("◀ Back", callback_data=f"back:{cache_key}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{cache_key}"),
    ])
    return InlineKeyboardMarkup(keyboard)


def build_cancel_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Builds an inline keyboard with a Cancel / Stop button for live operations."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹️ Stop / Cancel", callback_data=f"stop:{task_id}")]
    ])


TERA_ACCOUNT_VIDEOS_CACHE: Dict[int, List[Dict[str, Any]]] = {}
TERA_MISSING_VIDEOS_CACHE: Dict[int, List[Dict[str, Any]]] = {}
TERA_CHECK_STATS_CACHE: Dict[int, Dict[str, Any]] = {}


def build_terafetch_keyboard(page: int = 0, total_videos: int = 0, page_size: int = 5) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for TeraBox fetched videos."""
    buttons = []
    # Row 1: Quick actions if videos exist
    if total_videos > 0:
        buttons.append([
            InlineKeyboardButton("🔍 Check Missing in Photos", callback_data="teracheck_refresh"),
            InlineKeyboardButton("🚀 Transfer All", callback_data="teragphotos_transfer"),
        ])

    # Row 2: Pagination buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"terafetch_page:{page - 1}"))
    if (page + 1) * page_size < total_videos:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"terafetch_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    # Row 3: Refresh and Cancel/Close
    buttons.append([
        InlineKeyboardButton("🔄 Refresh List", callback_data="terafetch_refresh"),
        InlineKeyboardButton("❌ Close", callback_data="stats_close"),
    ])
    return InlineKeyboardMarkup(buttons)


def format_terafetch_page_text(videos: List[Dict[str, Any]], page: int = 0, page_size: int = 5) -> str:
    """Renders paginated video listing text."""
    total_count = len(videos)
    total_size = sum(v.get("size", 0) for v in videos)
    start_idx = page * page_size
    page_items = videos[start_idx : start_idx + page_size]

    text = (
        f"📦 **TeraBox Account Video Library**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎥 **Total Video Files Fetched:** `{total_count}`\n"
        f"💾 **Total Video Size:** `{format_bytes(total_size)}`\n"
    )

    if not videos:
        text += (
            "\n⚠️ *No video files found in this TeraBox account.*\n"
            "💡 Upload videos to your TeraBox account or update cookies with `/setteracookie`."
        )
        return text

    max_pages = max(1, (total_count + page_size - 1) // page_size)
    text += f"\n📑 **Video Files (Page {page + 1} of {max_pages}):**\n"
    for i, it in enumerate(page_items, start_idx + 1):
        fn = it.get("filename", "unknown.mp4")
        sz = it.get("size_formatted", format_bytes(it.get("size", 0)))
        path = it.get("path", "")
        text += f"**{i}.** 🎬 `{fn}`\n   ↳ 💾 `{sz}` | 📁 `{path[:35]}`\n"

    text += (
        f"\n💡 *Tap [🔍 Check Missing in Photos] to see what's not uploaded, or [🚀 Transfer All] to transfer all {total_count} videos.*"
    )
    return text


def build_teracheck_keyboard(page: int = 0, total_missing: int = 0, page_size: int = 5) -> InlineKeyboardMarkup:
    """Builds interactive inline keyboard for TeraBox vs Google Photos checker."""
    buttons = []
    # Row 1: Transfer missing button if missing items exist
    if total_missing > 0:
        buttons.append([
            InlineKeyboardButton(f"🚀 Transfer Missing ({total_missing}) to Photos", callback_data="teragphotos_transfer_missing")
        ])

    # Row 2: Navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"teracheck_page:{page - 1}"))
    if (page + 1) * page_size < total_missing:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"teracheck_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    # Row 3: Re-Check, View All, and Close
    buttons.append([
        InlineKeyboardButton("🔄 Re-Check", callback_data="teracheck_refresh"),
        InlineKeyboardButton("📦 View All Videos", callback_data="terafetch_refresh"),
        InlineKeyboardButton("❌ Close", callback_data="stats_close"),
    ])
    return InlineKeyboardMarkup(buttons)


def build_teratransfer_keyboard() -> InlineKeyboardMarkup:
    """Builds inline keyboard with Skip and Cancel buttons for active transfer."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭️ Skip Current Video", callback_data="teratransfer_skip"),
            InlineKeyboardButton("🛑 Cancel Transfer", callback_data="teratransfer_cancel"),
        ]
    ])


def format_teracheck_page_text(check_result: Dict[str, Any], page: int = 0, page_size: int = 5) -> str:
    """Renders formatted checker status and paginated missing videos."""
    total_tb = check_result.get("total_terabox", 0)
    total_tb_size = check_result.get("total_terabox_size_formatted", "0 B")
    present_cnt = check_result.get("present_count", 0)
    present_sz = check_result.get("present_size_formatted", "0 B")
    missing_cnt = check_result.get("missing_count", 0)
    missing_sz = check_result.get("missing_size_formatted", "0 B")
    missing_videos = check_result.get("missing_videos", [])

    text = (
        "🔍 **TeraBox ➔ Google Photos Sync Checker**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 **TeraBox Account Total:** `{total_tb}` videos (`{total_tb_size}`)\n"
        f"✅ **Already in Google Photos:** `{present_cnt}` videos (`{present_sz}`)\n"
        f"⚠️ **Missing from Google Photos:** `{missing_cnt}` videos (`{missing_sz}`)\n"
    )

    if check_result.get("warning"):
        text += f"\n⚠️ **Notice:** {check_result['warning']}\n"

    if total_tb == 0:
        text += (
            "\n⚠️ *No videos found in your TeraBox account.*\n"
            "💡 Upload videos to TeraBox or refresh cookies with `/setteracookie`."
        )
        return text

    if missing_cnt == 0:
        text += (
            "\n🎉 **Everything is 100% in Sync!**\n"
            f"All `{total_tb}` video files from your TeraBox account are already present in your Google Photos library."
        )
        return text

    start_idx = page * page_size
    page_items = missing_videos[start_idx : start_idx + page_size]
    max_pages = max(1, (missing_cnt + page_size - 1) // page_size)

    text += f"\n📋 **Missing Videos Queue (Page {page + 1} of {max_pages}):**\n"
    for i, it in enumerate(page_items, start_idx + 1):
        fn = it.get("filename", "unknown.mp4")
        sz = it.get("size_formatted", format_bytes(it.get("size", 0)))
        path = it.get("path", "")
        text += f"**{i}.** 🎬 `{fn}`\n   ↳ 💾 `{sz}` | 📁 `{path[:35]}`\n"

    text += (
        f"\n💡 *Tap [🚀 Transfer Missing ({missing_cnt})] below or use `/teratransfer missing` to transfer only these unsynced videos (with 10s cooldown)!*"
    )
    return text


async def edit_query_message(
    query,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
):
    """Edit a callback message whether it is a photo caption or plain text with exception safety."""
    try:
        if query.message.photo:
            await query.edit_message_caption(
                caption=text,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        else:
            await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.debug(f"Non-critical query message edit suppressed: {e}")


UNAUTHORIZED_DM_MESSAGE = "Only Authorized People and Groups Can Use This Bot , Contact @dorachangg to get authorized"


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    chat = update.effective_chat
    user = update.effective_user
    user_id = user.id

    # Restrict private DM access to authorized users and admin
    if chat.type == "private" and not is_user_allowed(user_id):
        await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        return

    welcome_text = (
        f"👋 **Hello, {user.first_name}!**\n\n"
        "I am your **Universal Media Downloader Bot** 🚀\n\n"
        "**Supported Sources (Any Non-DRM Video):**\n"
        "• 🔴 **YouTube**: Videos, Shorts & MP3\n"
        "• 📸 **Instagram**: Reels, Posts & Stories\n"
        "• 🔵 **Facebook**: Videos & Reels\n"
        "• 📦 **TeraBox & 💿 Diskwala**: Cloud videos & share links\n"
        "• 🎵 **TikTok & 🐦 X / Twitter**\n"
        "• 🌐 **Reddit, Pinterest, Vimeo, Twitch, Threads, Dailymotion**\n"
        "• 🔗 **Direct MP4 / WebM / HLS video URLs**\n\n"
        "👉 **How to use:**\n"
        "Simply send or forward me any video link!"
    )
    await update.message.reply_text(
        welcome_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only comprehensive control panel or authorized user guide."""
    chat = update.effective_chat
    user_id = update.effective_user.id
    is_group = chat.type in ("group", "supergroup")

    if not is_admin(user_id):
        if not is_group and is_user_allowed(user_id):
            await gchelp_command(update, context)
        elif not is_group:
            await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        else:
            await update.message.reply_text("ℹ️ Use `/gchelp` to view group commands.", parse_mode=constants.ParseMode.MARKDOWN)
        return

    admin_help_text = (
        "👑 **Developer & Admin Control Panel**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👤 **DM User Access Controls:**\n"
        "• `/allow <user_id> [note]` — Authorize a user to download in private DMs.\n"
        "• `/disallow <user_id>` (or `/revoke`) — Revoke a user's private DM access.\n"
        "• `/allowedusers` (or `/listusers`) — View all authorized private DM users.\n\n"
        "🍪 **Cookie Management Commands:**\n"
        "• `/cookiestatus` — Check status, lines, and sizes for YouTube, Instagram, Facebook, and TeraBox cookies.\n"
        "• `/setcookie <platform> <cookie_text>` — Save raw cookie text directly in chat (e.g. `/setcookie terabox ndus=...`).\n"
        "• `/clearcookie <platform>` — Delete cookies for `youtube`, `instagram`, `facebook`, `terabox`, or `generic`.\n"
        "• *Tip:* Drag & drop any `cookies.txt` document with caption `youtube`, `instagram`, `facebook`, or `terabox` to update automatically!\n\n"
        "⚡ **Instant Telethon Token Fetchers (Headless MTProto):**\n"
        "• `/ezdisk` — Instantly fetch & save DiskWala Bearer token via native Telethon (~0.3s).\n"
        "• `/eztera` — Instantly fetch & save TeraBox Bearer token via native Telethon (~0.3s).\n\n"
        "💿 **Diskwala MiniApp Controls:**\n"
        "• `/setdiskwala <bearer_token>` — Update or refresh Diskwala MiniApp Authorization token.\n"
        "• `/diskwalastatus` — Check Diskwala API & resolver status.\n\n"
        "📦 **TeraBox MiniApp Controls:**\n"
        "• `/setterabox <bearer_token>` — Update or refresh TeraBox MiniApp Authorization token.\n"
        "• `/teraboxstatus` — Check TeraBox API resolver & fallback status.\n\n"
        "🖥️ **Live VNC Remote Desktop (Token Capture):**\n"
        "• `/autovnc <diskwala|tera> [@botusername]` — Fully automated token capture (opens app, types link, clicks download, saves token to `.env`).\n"
        "• `/vnc [bot_name_or_link]` — Manual VNC desktop session with persistent Telegram login.\n"
        "• `/stopvnc` — Close the VNC browser session.\n\n"
        "👥 **Group Chat Management:**\n"
        "• `/startgc` — Activate and authorize the bot inside the current group chat.\n"
        "• `/stopgc` — Deactivate and revoke bot access in the current group chat.\n"
        "• `/gchelp` — Show member command guide in group chat.\n\n"
        "⚙️ **Maintenance & Server Controls:**\n"
        "• `/refresh` (or `/killtasks` / `/reset`) — Kill all running download tasks, purge temporary files from `downloads/`, and reset concurrency slots.\n\n"
        "📊 **Statistics & Link Analytics:**\n"
        "• `/stats` (or `/analytics`, `/stat`) — Interactive statistics dashboard (top users leaderboard, platform classification breakdown, recent link activity audit).\n"
        "• `/userstats <user_id>` (or `/stats <user_id>`) — Drill down into a specific user's resolved links and bandwidth history.\n\n"
        "☁️ **TeraBox Account & Google Photos Transfer:**\n"
        "• `/terafetch` (or `/teravideos`) — Fetch & count all video files from your TeraBox account using cookies.\n"
        "• `/teratransfer` (or `/gtransfer`) — Transfer TeraBox account videos to Google Photos (10s delay between transfers + failed files report at the end).\n"
        "• `/skiptransfer` (or `/skip`) — Skip currently transferring video and advance immediately to next file.\n"
        "• `/canceltransfer` — Stop the currently active transfer job.\n"
        "• `/gphotos_auth` (or `/gauth`) — Connect to Google Photos via OAuth 2.0.\n"
        "• `/gphotos_code <code>` — Submit authorization code manually after OAuth login.\n"
        "• `/gphotos_status` — Check Google Photos authentication status.\n"
        "• `/gphotos_upload` (or `/gupload`, `/gpush`) — Reply to any video to upload directly to Google Photos.\n"
        "• `/setteracookie <cookie>` — Update TeraBox account cookie text directly in chat.\n\n"
        "📥 **Manual Download Commands:**\n"
        "• `/dl <video_url>` (or `/download <url>`) — Manually trigger video download.\n\n"
        "📊 **Current Configuration:**\n"
        f"• Max Concurrent Downloads: `{MAX_CONCURRENT_DOWNLOADS}`\n"
        f"• Max Upload Size: `{MAX_FILE_SIZE_MB} MB` (MTProto 2GB Active)\n"
    )
    await update.message.reply_text(
        admin_help_text,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def gchelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Member help guide usable in authorized groups or private chats."""
    chat = update.effective_chat
    user_id = update.effective_user.id
    is_group = chat.type in ("group", "supergroup")

    if is_group and not is_group_allowed(chat.id):
        await update.message.reply_text(
            "⛔ This bot is not activated in this group. Ask the admin to activate it with `/startgc`.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    if not is_group and not is_user_allowed(user_id):
        await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        return

    member_help = (
        "🚀 **DownTG by Dorachan**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ **Capacity:** Max `{MAX_CONCURRENT_DOWNLOADS}` Downloads can be processed at once.\n"
        "💡 *If bot is stuck kindly contact @dorachangg to refresh.*\n\n"
        "📥 **How to Download Videos:**\n"
        "1. Simply paste or send any video link directly in this chat.\n"
        "2. Or use the command: `/dl <video_url>`\n\n"
        "🌐 **Supported Platforms:**\n"
        "• 🔴 **YouTube**: Videos, Shorts & Audio (MP3)\n"
        "• 📸 **Instagram**: Reels, Posts & Stories\n"
        "• 🔵 **Facebook**: Videos & Reels\n"
        "• 📦 **TeraBox & 💿 Diskwala**: Cloud videos & share links\n"
        "• 🎵 **TikTok & 🐦 X (Twitter)**\n"
        "• 🌐 **Reddit, Pinterest, Vimeo, Twitch & Direct MP4 links**\n\n"
        "⏹️ **Process Control:**\n"
        "• Tap the **[ ⏹️ Stop / Cancel ]** button on your download message at any time to abort.\n\n"
        "⚡ *Powered by High-Speed 2GB MTProto Direct Uploads.*"
    )
    await update.message.reply_text(
        member_help,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


_UNSET_MARKUP = object()


async def edit_status_msg_safe(msg, text: str, reply_markup: Any = _UNSET_MARKUP):
    """Safely edit status text without crashing on transient HTTP transport errors."""
    if not msg:
        return
    try:
        if reply_markup is not _UNSET_MARKUP:
            await msg.edit_text(text, reply_markup=reply_markup, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await msg.edit_text(text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        logger.debug(f"Status message edit suppressed: {e}")


async def send_media_to_chat(
    bot,
    chat_id: int,
    file_path: str,
    title: str,
    uploader: str,
    duration_sec: Optional[int],
    url: str,
    quality: str,
    info: Optional[Dict[str, Any]],
    progress_status_updater: Optional[Callable[[str], None]] = None,
):
    """Sends audio or video to chat with metadata caption via MTProto (up to 2GB) or standard Bot API."""
    platform_badge = get_platform_badge(url)
    is_audio = (quality == "audio")
    caption = f"🎵 **{title}**\n{platform_badge}" if is_audio else f"🎬 **{title}**\n{platform_badge}"

    upload_file_path = file_path
    thumbnail_path = None
    width = info.get("width") if info else None
    height = info.get("height") if info else None
    temp_files_to_clean = []

    # Prepare video for progressive streaming in Telegram (FastStart + MP4 container + thumbnail + metadata)
    if not is_audio:
        try:
            prep = prepare_video_for_telegram(file_path)
            upload_file_path = prep["file_path"]
            thumbnail_path = prep["thumbnail_path"]
            if prep.get("duration"):
                duration_sec = prep["duration"]
            if prep.get("width"):
                width = prep["width"]
            if prep.get("height"):
                height = prep["height"]
            if prep.get("is_remuxed") and upload_file_path != file_path:
                temp_files_to_clean.append(upload_file_path)
            if thumbnail_path:
                temp_files_to_clean.append(thumbnail_path)
        except Exception as prep_err:
            logger.debug(f"[Video Prep] Streaming preparation notice: {prep_err}")

    try:
        file_size = os.path.getsize(upload_file_path) if os.path.exists(upload_file_path) else 0

        # 1. Attempt MTProto upload (supports up to 2GB with live upload percentage)
        if is_mtproto_active() or file_size > 50 * 1024 * 1024:
            async def _mtproto_progress(current: int, total: int):
                if progress_status_updater and total > 0:
                    pct = int((current / total) * 100)
                    cur_str = format_bytes(current)
                    tot_str = format_bytes(total)
                    text = f"📤 **Uploading to Telegram: {pct}%**\n`[{cur_str} / {tot_str}]`"
                    try:
                        res = progress_status_updater(text)
                        if asyncio.iscoroutine(res):
                            await res
                    except Exception:
                        pass

            mtproto_success, mtproto_err = await upload_media_mtproto(
                chat_id=chat_id,
                file_path=upload_file_path,
                title=title,
                caption=caption,
                duration_sec=duration_sec,
                thumbnail_path=thumbnail_path,
                width=width,
                height=height,
                is_audio=is_audio,
                progress_callback=_mtproto_progress,
            )
            if mtproto_success:
                return
            elif file_size > 50 * 1024 * 1024:
                err_msg = mtproto_err or "MTProto client error"
                logger.error(f"MTProto upload failed for {format_bytes(file_size)} file: {err_msg}")
                raise RuntimeError(f"MTProto upload failed: {err_msg}")

        # 2. Standard HTTP Bot API upload (for files <= 50MB)
        if is_audio:
            with open(upload_file_path, "rb") as audio_file:
                await bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=title,
                    performer=uploader,
                    duration=duration_sec,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
        else:
            with open(upload_file_path, "rb") as video_file:
                thumb_f = None
                if thumbnail_path and os.path.exists(thumbnail_path):
                    try:
                        thumb_f = open(thumbnail_path, "rb")
                    except Exception:
                        thumb_f = None
                try:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        caption=caption,
                        duration=duration_sec,
                        width=width,
                        height=height,
                        thumbnail=thumb_f,
                        supports_streaming=True,
                        parse_mode=constants.ParseMode.MARKDOWN,
                    )
                finally:
                    if thumb_f:
                        thumb_f.close()
    finally:
        for tf in temp_files_to_clean:
            if tf and tf != file_path and os.path.exists(tf):
                remove_file_safely(tf)


async def show_quality_panel(
    update: Update,
    status_msg,
    cache_key: str,
    url: str,
    title: str,
    uploader: str,
    duration: str,
    thumbnail: Optional[str],
    reason: Optional[str] = None,
):
    """Displays the interactive quality options panel when default download cannot complete."""
    platform = get_platform_badge(url)
    reply_markup = build_quality_keyboard(cache_key)

    reason_text = f"\n⚠️ *{reason}*\n" if reason else ""
    caption = (
        f"{platform}\n"
        f"📌 **{title}**\n\n"
        f"👤 **Author:** {uploader}\n"
        f"⏱ **Duration:** {duration}\n"
        f"{reason_text}\n"
        f"👇 *Choose quality to download:*"
    )

    try:
        if thumbnail:
            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        else:
            if status_msg:
                await status_msg.edit_text(
                    caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            else:
                await update.message.reply_text(
                    caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
    except Exception as e:
        logger.warning(f"Could not render quality panel with photo: {e}")
        if status_msg:
            try:
                await status_msg.edit_text(
                    caption,
                    reply_markup=reply_markup,
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            except Exception:
                pass


async def process_multi_link_queue(
    links: List[str],
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    is_group: bool,
):
    """
    Sequentially processes a queue of multiple download links (e.g. from Telegraph or multi-link text).
    Downloads and uploads each media file one by one, immediately cleans up local disk space,
    allows user cancellation, and posts a final comprehensive summary report.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    total_links = len(links)
    queue_id = f"q_{user_id}_{int(time.time() * 1000)}"

    ACTIVE_QUEUES[queue_id] = {
        "cancelled": False,
        "task": asyncio.current_task(),
        "user_id": user_id,
    }

    status_msg = await update.message.reply_text(
        f"📋 **Multi-Link Queue Started**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• **Total Links:** `{total_links}`\n"
        f"• **Mode:** Sequential Processing (1 by 1)\n\n"
        f"⏳ Initializing link 1 of {total_links}...",
        reply_markup=build_cancel_queue_keyboard(queue_id),
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    successful: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total_downloaded_bytes = 0

    for index, url in enumerate(links, 1):
        # Check cancellation before starting next link
        if ACTIVE_QUEUES.get(queue_id, {}).get("cancelled"):
            logger.info(f"[Queue {queue_id}] Cancelled by user before item {index}/{total_links}.")
            break

        # Anti-Bot Cooldown Check
        on_cooldown, rem_sec = is_link_on_cooldown(url)
        if on_cooldown:
            logger.info(f"[Queue {queue_id}] Link {url} skipped due to 5-min anti-bot cooldown.")
            skipped.append({
                "url": url,
                "reason": f"5-min cooldown active ({int(rem_sec)}s remaining)",
            })
            await edit_status_msg_safe(
                status_msg,
                f"📋 **Queue [{index}/{total_links}]** ⚠️ *Link skipped (Cooldown active)*\n🔗 `{url}`",
                reply_markup=build_cancel_queue_keyboard(queue_id),
            )
            continue

        task_id = f"{user_id}_{int(time.time() * 1000)}"
        TASK_REQUESTERS[task_id] = user_id
        downloaded_file = None
        item_success = False
        item_title = None
        item_size = 0
        item_err = None

        await edit_status_msg_safe(
            status_msg,
            f"📋 **Queue [{index}/{total_links}]** ⏳ *Analyzing & Downloading...*\n🔗 `{url}`",
            reply_markup=build_cancel_queue_keyboard(queue_id),
        )

        try:
            ACTIVE_TASKS[task_id] = asyncio.current_task()
            mark_link_in_progress(url)

            # ── 1. TeraBox Link ──
            if is_terabox_url(url):
                async with get_semaphore():
                    dl_ok, downloaded_file, dl_info, dl_err = await route_download_media(
                        url,
                        quality="best",
                        progress_updater=lambda txt: edit_status_msg_safe(
                            status_msg,
                            f"📋 **Queue [{index}/{total_links}]** 📥 {txt}\n🔗 `{url}`",
                            reply_markup=build_cancel_queue_keyboard(queue_id),
                        ),
                    )
                    if dl_ok and downloaded_file and os.path.exists(downloaded_file):
                        item_size = os.path.getsize(downloaded_file)
                        item_title = dl_info.get("title") if dl_info else Path(downloaded_file).name
                        await edit_status_msg_safe(
                            status_msg,
                            f"📋 **Queue [{index}/{total_links}]** 📤 *Uploading {format_bytes(item_size)} to Telegram...*\n📌 `{item_title}`",
                            reply_markup=build_cancel_queue_keyboard(queue_id),
                        )
                        await send_media_to_chat(
                            bot=context.bot,
                            chat_id=chat_id,
                            file_path=downloaded_file,
                            title=item_title,
                            uploader=dl_info.get("uploader", "TeraBox") if dl_info else "TeraBox",
                            duration_sec=None,
                            url=url,
                            quality="best",
                            info=dl_info,
                            progress_status_updater=lambda txt: edit_status_msg_safe(
                                status_msg,
                                f"📋 **Queue [{index}/{total_links}]** 📤 {txt}\n📌 `{item_title}`",
                                reply_markup=build_cancel_queue_keyboard(queue_id),
                            ),
                        )
                        item_success = True
                        mark_link_completed(url)
                        try:
                            await record_resolved_link(
                                user_id=user_id,
                                username=update.effective_user.username,
                                first_name=update.effective_user.first_name,
                                last_name=update.effective_user.last_name,
                                url=url,
                                title=item_title,
                                file_size=item_size,
                                chat_id=chat_id,
                                is_group=is_group,
                            )
                        except Exception as st_err:
                            logger.error(f"Failed to record TeraBox stats: {st_err}")
                    else:
                        item_err = dl_err or "TeraBox download failed."

            # ── 2. Diskwala Link ──
            elif is_diskwala_url(url):
                async with get_semaphore():
                    dl_ok, downloaded_file, dl_info, dl_err = await route_download_media(
                        url,
                        quality="best",
                        progress_updater=lambda txt: edit_status_msg_safe(
                            status_msg,
                            f"📋 **Queue [{index}/{total_links}]** 📥 {txt}\n🔗 `{url}`",
                            reply_markup=build_cancel_queue_keyboard(queue_id),
                        ),
                    )
                    if dl_ok and downloaded_file and os.path.exists(downloaded_file):
                        item_size = os.path.getsize(downloaded_file)
                        item_title = dl_info.get("title") if dl_info else Path(downloaded_file).name
                        await edit_status_msg_safe(
                            status_msg,
                            f"📋 **Queue [{index}/{total_links}]** 📤 *Uploading {format_bytes(item_size)} to Telegram...*\n📌 `{item_title}`",
                            reply_markup=build_cancel_queue_keyboard(queue_id),
                        )
                        await send_media_to_chat(
                            bot=context.bot,
                            chat_id=chat_id,
                            file_path=downloaded_file,
                            title=item_title,
                            uploader=dl_info.get("uploader", "Diskwala") if dl_info else "Diskwala",
                            duration_sec=None,
                            url=url,
                            quality="best",
                            info=dl_info,
                            progress_status_updater=lambda txt: edit_status_msg_safe(
                                status_msg,
                                f"📋 **Queue [{index}/{total_links}]** 📤 {txt}\n📌 `{item_title}`",
                                reply_markup=build_cancel_queue_keyboard(queue_id),
                            ),
                        )
                        item_success = True
                        mark_link_completed(url)
                        try:
                            await record_resolved_link(
                                user_id=user_id,
                                username=update.effective_user.username,
                                first_name=update.effective_user.first_name,
                                last_name=update.effective_user.last_name,
                                url=url,
                                title=item_title,
                                file_size=item_size,
                                chat_id=chat_id,
                                is_group=is_group,
                            )
                        except Exception as st_err:
                            logger.error(f"Failed to record DiskWala stats: {st_err}")
                    else:
                        item_err = dl_err or "Diskwala download failed."

            # ── 3. YouTube / Instagram / Facebook / Generic Engine ──
            else:
                info_ok, info, info_err = await route_extract_info(
                    url,
                    notify_admin_callback=lambda insp, tgt: notify_admin_of_captcha(context.bot, insp, tgt),
                    progress_updater=lambda txt: edit_status_msg_safe(
                        status_msg,
                        f"📋 **Queue [{index}/{total_links}]** 🔍 {txt}",
                        reply_markup=build_cancel_queue_keyboard(queue_id),
                    ),
                )
                if info_ok and info:
                    item_title = info.get("title", "Untitled Video")
                    uploader = info.get("uploader", "Unknown Author")
                    duration_sec = info.get("duration")

                    async with get_semaphore():
                        # Try 480p default
                        dl_ok, downloaded_file, dl_info, dl_err = await route_download_media(
                            url,
                            quality="480",
                            notify_admin_callback=lambda insp, tgt: notify_admin_of_captcha(context.bot, insp, tgt),
                            progress_updater=lambda txt: edit_status_msg_safe(
                                status_msg,
                                f"📋 **Queue [{index}/{total_links}]** 📥 {txt}",
                                reply_markup=build_cancel_queue_keyboard(queue_id),
                            ),
                        )
                        # Fallback to best if 480 was unavailable
                        if not dl_ok or not downloaded_file or not os.path.exists(downloaded_file):
                            dl_ok, downloaded_file, dl_info, dl_err = await route_download_media(
                                url,
                                quality="best",
                                progress_updater=lambda txt: edit_status_msg_safe(
                                    status_msg,
                                    f"📋 **Queue [{index}/{total_links}]** 📥 {txt}",
                                    reply_markup=build_cancel_queue_keyboard(queue_id),
                                ),
                            )

                        if dl_ok and downloaded_file and os.path.exists(downloaded_file):
                            item_size = os.path.getsize(downloaded_file)
                            if item_size <= MAX_FILE_SIZE_BYTES:
                                await edit_status_msg_safe(
                                    status_msg,
                                    f"📋 **Queue [{index}/{total_links}]** 📤 *Uploading {format_bytes(item_size)} to Telegram...*\n📌 `{item_title}`",
                                    reply_markup=build_cancel_queue_keyboard(queue_id),
                                )
                                await send_media_to_chat(
                                    bot=context.bot,
                                    chat_id=chat_id,
                                    file_path=downloaded_file,
                                    title=item_title,
                                    uploader=uploader,
                                    duration_sec=duration_sec,
                                    url=url,
                                    quality="480",
                                    info=dl_info or info,
                                    progress_status_updater=lambda txt: edit_status_msg_safe(
                                        status_msg,
                                        f"📋 **Queue [{index}/{total_links}]** 📤 {txt}\n📌 `{item_title}`",
                                        reply_markup=build_cancel_queue_keyboard(queue_id),
                                    ),
                                )
                                item_success = True
                                mark_link_completed(url)
                                try:
                                    await record_resolved_link(
                                        user_id=user_id,
                                        username=update.effective_user.username,
                                        first_name=update.effective_user.first_name,
                                        last_name=update.effective_user.last_name,
                                        url=url,
                                        title=item_title,
                                        file_size=item_size,
                                        chat_id=chat_id,
                                        is_group=is_group,
                                    )
                                except Exception as st_err:
                                    logger.error(f"Failed to record stats: {st_err}")
                            else:
                                item_err = f"File is {format_bytes(item_size)}, exceeding {MAX_FILE_SIZE_MB}MB limit."
                        else:
                            item_err = dl_err or "Stream could not be downloaded."
                else:
                    item_err = info_err or "Could not extract video metadata."

        except asyncio.CancelledError:
            logger.info(f"[Queue {queue_id}] Task cancelled at item {index}.")
            item_err = "Operation cancelled."
            break
        except Exception as ex:
            logger.error(f"[Queue {queue_id}] Unexpected error on item {index}: {ex}", exc_info=True)
            item_err = str(ex)[:120]
        finally:
            ACTIVE_TASKS.pop(task_id, None)
            TASK_REQUESTERS.pop(task_id, None)
            if downloaded_file:
                remove_file_safely(downloaded_file)
                downloaded_file = None

        if item_success:
            total_downloaded_bytes += item_size
            successful.append({
                "url": url,
                "title": item_title or f"Video {index}",
                "size": item_size,
            })
        else:
            mark_link_failed(url)
            failed.append({
                "url": url,
                "reason": item_err or "Download failed",
            })

        # Sequential cooldown between items
        if index < total_links and not ACTIVE_QUEUES.get(queue_id, {}).get("cancelled"):
            await asyncio.sleep(2)

    was_cancelled = ACTIVE_QUEUES.get(queue_id, {}).get("cancelled", False)
    ACTIVE_QUEUES.pop(queue_id, None)

    title_banner = "🛑 **Multi-Link Queue Cancelled by User**" if was_cancelled else "🏁 **Multi-Link Queue Completed!**"

    succ_lines = []
    for i, s in enumerate(successful[:12], 1):
        succ_lines.append(f"  {i}. ✅ `{s['title']}` ({format_bytes(s['size'])})")
    if len(successful) > 12:
        succ_lines.append(f"  ... and {len(successful) - 12} more files sent to chat.")

    fail_lines = []
    for i, f in enumerate(failed[:10], 1):
        fail_lines.append(f"  {i}. ❌ `{f['url']}`\n     ↳ *Reason:* {f['reason']}")
    if len(failed) > 10:
        fail_lines.append(f"  ... and {len(failed) - 10} more failed.")

    report = (
        f"{title_banner}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 **Batch Statistics:**\n"
        f"• **Total Queued:** `{total_links}` links\n"
        f"• **✅ Successfully Sent:** `{len(successful)}` ({format_bytes(total_downloaded_bytes)})\n"
        f"• **❌ Failed:** `{len(failed)}`\n"
    )
    if skipped:
        report += f"• **⏭️ Skipped (Cooldown):** `{len(skipped)}`\n"
    report += "\n"

    if succ_lines:
        report += "✅ **Completed Files:**\n" + "\n".join(succ_lines) + "\n\n"

    if fail_lines:
        report += "❌ **Failed Links:**\n" + "\n".join(fail_lines) + "\n\n"

    if not was_cancelled and len(failed) == 0 and len(successful) > 0:
        report += "🎉 **All links were processed with 100% success!**"

    try:
        await status_msg.edit_text(report, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception:
        try:
            await update.message.reply_text(report, parse_mode=constants.ParseMode.MARKDOWN)
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects URLs, expands Telegraph/paste sites, checks authorization, and processes single or multi-link downloads."""
    if not update.message:
        return

    raw_text = update.message.text or update.message.caption or ""
    text = raw_text.strip()
    if not text:
        return

    chat = update.effective_chat
    user_id = update.effective_user.id
    is_group = chat.type in ("group", "supergroup")

    # If message is in private chat (DM), restrict to authorized users and admin
    if not is_group and not is_user_allowed(user_id):
        await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        return

    # If message is in a group chat, ensure the group has been authorized via /startgc
    if is_group and not is_group_allowed(chat.id):
        return

    # Extract all download links (including expanding Telegraph & paste sites)
    links = await extract_all_download_links(text)
    if not links:
        # Silently ignore non-link messages in groups to prevent conversation spam
        if not is_group:
            await update.message.reply_text(
                "❌ No valid link detected. Please send a valid YouTube, Facebook, Instagram, TeraBox, or Telegraph link."
            )
        return

    # ── Multi-Link Queue Mode ──
    # If the message contains more than 1 link, process each link sequentially
    if len(links) > 1:
        await process_multi_link_queue(links, update, context, is_group)
        return

    url = links[0]

    # Anti-Bot Protection: Enforce 5-minute cooldown on duplicate link downloads
    on_cooldown, remaining_sec = is_link_on_cooldown(url)
    if on_cooldown:
        logger.info(f"[handle_message] Link {url} blocked by 5-min anti-bot cooldown ({int(remaining_sec)}s remaining)")
        await update.message.reply_text(
            f"⚠️ {COOLDOWN_ERROR_MESSAGE}",
            reply_to_message_id=update.message.message_id,
        )
        return

    # Check concurrent download capacity before analyzing link
    if get_active_downloads_count() >= MAX_CONCURRENT_DOWNLOADS:
        await update.message.reply_text(
            f"⚠️ **Server Busy:** `{MAX_CONCURRENT_DOWNLOADS}` downloads are already in progress. Please try again after some time.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    task_id = f"{user_id}_{int(time.time() * 1000)}"
    TASK_REQUESTERS[task_id] = user_id

    status_msg = await update.message.reply_text(
        f"🔍 **Analyzing link...**\n`{url}`",
        reply_markup=build_cancel_keyboard(task_id),
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    downloaded_file = None
    default_succeeded = False
    fail_reason = None

    try:
        ACTIVE_TASKS[task_id] = asyncio.current_task()
        mark_link_in_progress(url)

        # For TeraBox links: Run unified Crawler download directly
        if is_terabox_url(url):
            async with get_semaphore():
                success, downloaded_file, dl_info, error_msg = await route_download_media(
                    url,
                    quality="best",
                    progress_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
                )
                if success and downloaded_file and os.path.exists(downloaded_file):
                    file_size = os.path.getsize(downloaded_file)
                    try:
                        await status_msg.edit_text(
                            f"📤 **Uploading {format_bytes(file_size)} to Telegram...**",
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                    tb_title = dl_info.get("title") if dl_info else Path(downloaded_file).name
                    await send_media_to_chat(
                        bot=context.bot,
                        chat_id=update.effective_chat.id,
                        file_path=downloaded_file,
                        title=tb_title,
                        uploader=dl_info.get("uploader", "TeraBox") if dl_info else "TeraBox",
                        duration_sec=None,
                        url=url,
                        quality="best",
                        info=dl_info,
                        progress_status_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
                    )
                    mark_link_completed(url)
                    try:
                        await record_resolved_link(
                            user_id=update.effective_user.id,
                            username=update.effective_user.username,
                            first_name=update.effective_user.first_name,
                            last_name=update.effective_user.last_name,
                            url=url,
                            title=tb_title,
                            file_size=file_size,
                            chat_id=update.effective_chat.id,
                            is_group=is_group,
                        )
                    except Exception as st_err:
                        logger.error(f"Failed to record TeraBox stats: {st_err}")
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    return
                else:
                    mark_link_failed(url)
                    err = error_msg or "TeraBox download failed."
                    # Log 100% full raw technical error to terminal
                    logger.error(f"[handle_message] TeraBox failed: {err}")
                    if "timed out" in err.lower() or "timeout" in err.lower():
                        await edit_status_msg_safe(
                            status_msg,
                            "❌ **Download Failed**\n\nTeraBox download timed out (exceeded 3 minutes limit). Please try again or verify the link."
                        )
                    else:
                        await edit_status_msg_safe(
                            status_msg,
                            "❌ **Download Failed**\n\nUnable to retrieve this video. Please try again in a moment or verify the link."
                        )
                    return

        # For Diskwala links: Run Diskwala download directly
        if is_diskwala_url(url):
            async with get_semaphore():
                success, downloaded_file, dl_info, error_msg = await route_download_media(
                    url,
                    quality="best",
                    progress_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
                )
                if success and downloaded_file and os.path.exists(downloaded_file):
                    file_size = os.path.getsize(downloaded_file)
                    try:
                        await status_msg.edit_text(
                            f"📤 **Uploading {format_bytes(file_size)} to Telegram...**",
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                    dw_title = dl_info.get("title") if dl_info else Path(downloaded_file).name
                    await send_media_to_chat(
                        bot=context.bot,
                        chat_id=update.effective_chat.id,
                        file_path=downloaded_file,
                        title=dw_title,
                        uploader=dl_info.get("uploader", "Diskwala") if dl_info else "Diskwala",
                        duration_sec=None,
                        url=url,
                        quality="best",
                        info=dl_info,
                        progress_status_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
                    )
                    mark_link_completed(url)
                    try:
                        await record_resolved_link(
                            user_id=update.effective_user.id,
                            username=update.effective_user.username,
                            first_name=update.effective_user.first_name,
                            last_name=update.effective_user.last_name,
                            url=url,
                            title=dw_title,
                            file_size=file_size,
                            chat_id=update.effective_chat.id,
                            is_group=is_group,
                        )
                    except Exception as st_err:
                        logger.error(f"Failed to record DiskWala stats: {st_err}")
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass
                    return
                else:
                    mark_link_failed(url)
                    err = error_msg or "Diskwala download failed."
                    # Log 100% full raw technical error to terminal
                    logger.error(f"[handle_message] Diskwala failed: {err}")
                    if is_admin(user_id):
                        if "token" in err.lower() or "unauthorized" in err.lower() or "expired" in err.lower():
                            await edit_status_msg_safe(
                                status_msg,
                                f"⚠️ **Diskwala Token Error**\n\n{err}\n\n👉 Run `/vnc` to auto-capture or `/setdiskwala <token>` in this chat."
                            )
                        else:
                            await edit_status_msg_safe(status_msg, f"❌ **Download Failed**\n\n{err}")
                    else:
                        await edit_status_msg_safe(
                            status_msg,
                            "❌ **Download Failed**\n\nUnable to retrieve this video. Please try again in a moment or verify the link."
                        )
                    return

        # Extract metadata without downloading (routed to dedicated engine)
        success, info, error_msg = await route_extract_info(
            url,
            notify_admin_callback=lambda insp, tgt: notify_admin_of_captcha(context.bot, insp, tgt),
            progress_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
        )

        if not success or not info:
            mark_link_failed(url)
            err = error_msg or "Unable to retrieve video information."
            await edit_status_msg_safe(status_msg, err)
            return

        title = info.get("title", "Untitled Video")
        duration = format_duration(info.get("duration"))
        uploader = info.get("uploader", "Unknown Author")
        thumbnail = info.get("thumbnail")
        duration_sec = info.get("duration")
        format_choices = build_format_choices(info)

        # Store URL and info in cache for fallback panel
        cache_key = generate_cache_key(user_id)
        URL_CACHE[cache_key] = {
            "url": url,
            "title": title,
            "duration": duration,
            "uploader": uploader,
            "duration_sec": duration_sec,
            "thumbnail": thumbnail,
            "format_choices": format_choices,
        }

        # Clean old cache entries (keep last 50)
        if len(URL_CACHE) > 50:
            for k in list(URL_CACHE.keys())[:-50]:
                URL_CACHE.pop(k, None)

        # 1. Attempt 480p auto-download by default
        try:
            await status_msg.edit_text(
                f"⏳ **Downloading (480p default)...**\n📌 *{title}*",
                reply_markup=build_cancel_keyboard(task_id),
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        async with get_semaphore():
            success, downloaded_file, dl_info, error_msg = await route_download_media(
                url,
                quality="480",
                notify_admin_callback=lambda insp, tgt: notify_admin_of_captcha(context.bot, insp, tgt),
                progress_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
            )

            if success and downloaded_file and os.path.exists(downloaded_file):
                file_size = os.path.getsize(downloaded_file)
                if file_size <= MAX_FILE_SIZE_BYTES:
                    # Update status to uploading
                    try:
                        await status_msg.edit_text(
                            f"📤 **Uploading {format_bytes(file_size)} to Telegram...**",
                            parse_mode=constants.ParseMode.MARKDOWN,
                        )
                    except Exception:
                        pass

                    # Send media file
                    await send_media_to_chat(
                        bot=context.bot,
                        chat_id=update.effective_chat.id,
                        file_path=downloaded_file,
                        title=title,
                        uploader=uploader,
                        duration_sec=duration_sec,
                        url=url,
                        quality="480",
                        info=dl_info or info,
                        progress_status_updater=lambda txt: edit_status_msg_safe(status_msg, txt),
                    )

                    # Delete the status message on completion
                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

                    default_succeeded = True
                    mark_link_completed(url)
                    try:
                        await record_resolved_link(
                            user_id=update.effective_user.id,
                            username=update.effective_user.username,
                            first_name=update.effective_user.first_name,
                            last_name=update.effective_user.last_name,
                            url=url,
                            title=title,
                            file_size=file_size,
                            chat_id=update.effective_chat.id,
                            is_group=is_group,
                        )
                    except Exception as st_err:
                        logger.error(f"Failed to record 480p stats: {st_err}")
                    URL_CACHE.pop(cache_key, None)
                else:
                    fail_reason = f"480p file is {format_bytes(file_size)}, exceeding Telegram's {MAX_FILE_SIZE_MB}MB limit"
            else:
                fail_reason = error_msg or "480p stream could not be downloaded"

    except asyncio.CancelledError:
        logger.info(f"Task {task_id} was cancelled by user.")
        try:
            await status_msg.delete()
        except Exception:
            pass
        return
    except Exception as e:
        logger.error(f"Error during default 480p download: {e}", exc_info=True)
        fail_reason = str(e)[:100]
    finally:
        ACTIVE_TASKS.pop(task_id, None)
        TASK_REQUESTERS.pop(task_id, None)
        if not default_succeeded:
            mark_link_failed(url)
        if downloaded_file:
            remove_file_safely(downloaded_file)

    # 2. If 480p failed or exceeded size limit, show the interactive quality options panel
    if not default_succeeded:
        await show_quality_panel(
            update=update,
            status_msg=status_msg,
            cache_key=cache_key,
            url=url,
            title=title,
            uploader=uploader,
            duration=duration,
            thumbnail=thumbnail,
            reason=fail_reason,
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles quality selection button clicks, cancellation, and captcha solver responses."""
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    # ── 1. CAPTCHA Solved Confirmation (Always Allowed for the recipient) ──
    if action == "captcha_solved":
        page_id = parts[1] if len(parts) > 1 else ""
        logger.info(f"[Callback] captcha_solved received from user {query.from_user.id} for session {page_id}")
        from browser_verifier import signal_captcha_solved
        signal_captcha_solved(page_id)
        try:
            await query.answer("✅ Verification recorded! Extracting session & resuming download...", show_alert=True)
        except Exception:
            pass
        try:
            await query.edit_message_text(
                "🧩 **TeraBox Human Verification**\n\n"
                "✅ **Verification Recorded!**\n"
                "⏳ The bot is now capturing cookies and resuming your download...",
                parse_mode=constants.ParseMode.MARKDOWN
            )
        except Exception:
            pass
        return

    chat = update.effective_chat
    user_id = query.from_user.id
    is_group = chat.type in ("group", "supergroup") if chat else False

    if not is_group and not is_user_allowed(user_id):
        await query.answer(UNAUTHORIZED_DM_MESSAGE, show_alert=True)
        return

    await query.answer()

    # ── Stats Dashboard Interactions ──
    if action == "stats_close":
        try:
            await query.message.delete()
        except Exception:
            await edit_query_message(query, "📊 *Stats dashboard closed.*")
        return

    if action == "stats_view":
        if not is_admin(user_id):
            await query.answer("⚠️ Only the bot administrator can view the full analytics dashboard.", show_alert=True)
            return

        view_name = parts[1] if len(parts) > 1 else "overview"
        if view_name == "top_users":
            text = format_top_users_text(limit=10)
        elif view_name == "platforms":
            text = format_platform_breakdown_text()
        elif view_name == "recent":
            text = format_recent_activity_text(limit=10)
        else:
            text = format_overview_text()
            view_name = "overview"

        keyboard = build_stats_keyboard(view_name)
        await edit_query_message(query, text, reply_markup=keyboard)
        return

    if action == "stats_user":
        if not is_admin(user_id):
            await query.answer("⚠️ Only the bot administrator can view user analytics.", show_alert=True)
            return

        target_id_str = parts[1] if len(parts) > 1 else ""
        try:
            target_id = int(target_id_str)
            text = format_user_detail_text(target_id)
            keyboard = build_stats_keyboard("user_detail", target_user_id=target_id)
            await edit_query_message(query, text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"[Stats Callback] Failed to fetch user detail for {target_id_str}: {e}")
            await query.answer("❌ User not found or invalid ID.", show_alert=True)
            return
        return

    # ── TeraBox Account & Google Photos Transfer Callbacks (STRICT DEV ONLY) ──
    if action in (
        "terafetch_page", "terafetch_refresh", "teragphotos_transfer", "teragphotos_cancel",
        "teracheck_page", "teracheck_refresh", "teragphotos_transfer_missing",
        "teratransfer_skip", "teratransfer_cancel", "teragphotos_skip",
    ):
        if not is_admin(user_id):
            await query.answer("⛔ This action is restricted to the bot developer.", show_alert=True)
            return

    if action == "terafetch_page":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        videos = TERA_ACCOUNT_VIDEOS_CACHE.get(user_id, [])
        if not videos:
            res = await fetch_account_videos()
            videos = res.get("videos", [])
            TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos
        text = format_terafetch_page_text(videos, page=page)
        kb = build_terafetch_keyboard(page=page, total_videos=len(videos))
        await edit_query_message(query, text, reply_markup=kb)
        return

    if action == "terafetch_refresh":
        await edit_query_message(query, "⏳ **Refreshing TeraBox video list via cookie...**")
        res = await fetch_account_videos()
        if not res.get("success"):
            await edit_query_message(query, f"❌ **Failed to fetch videos:** {res.get('error')}")
            return
        videos = res.get("videos", [])
        TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos
        text = format_terafetch_page_text(videos, page=0)
        kb = build_terafetch_keyboard(page=0, total_videos=len(videos))
        await edit_query_message(query, text, reply_markup=kb)
        return

    if action == "teracheck_page":
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        check_res = TERA_CHECK_STATS_CACHE.get(user_id)
        if not check_res:
            check_res = await check_terabox_vs_google_photos()
            TERA_CHECK_STATS_CACHE[user_id] = check_res
            TERA_MISSING_VIDEOS_CACHE[user_id] = check_res.get("missing_videos", [])
        text = format_teracheck_page_text(check_res, page=page)
        kb = build_teracheck_keyboard(page=page, total_missing=check_res.get("missing_count", 0))
        await edit_query_message(query, text, reply_markup=kb)
        return

    if action == "teracheck_refresh":
        await edit_query_message(query, "⏳ **Checking TeraBox vs Google Photos Library...**\nCross-referencing video library and checking upload history...")
        check_res = await check_terabox_vs_google_photos()
        if not check_res.get("success"):
            await edit_query_message(query, f"❌ **Sync Check Failed:**\n`{check_res.get('error')}`")
            return
        TERA_CHECK_STATS_CACHE[user_id] = check_res
        TERA_MISSING_VIDEOS_CACHE[user_id] = check_res.get("missing_videos", [])
        TERA_ACCOUNT_VIDEOS_CACHE[user_id] = check_res.get("all_videos", [])
        text = format_teracheck_page_text(check_res, page=0)
        kb = build_teracheck_keyboard(page=0, total_missing=check_res.get("missing_count", 0))
        await edit_query_message(query, text, reply_markup=kb)
        return

    if action == "teragphotos_transfer_missing":
        if is_transfer_in_progress():
            await query.answer("⚠️ A transfer is already in progress. Use /canceltransfer to stop it.", show_alert=True)
            return
        auth_ok, auth_msg = is_photos_authenticated()
        if not auth_ok:
            await query.answer("⚠️ Google Photos is not connected. Use /gphotos_auth first.", show_alert=True)
            return
        missing_videos = TERA_MISSING_VIDEOS_CACHE.get(user_id, [])
        if not missing_videos:
            check_res = await check_terabox_vs_google_photos()
            TERA_CHECK_STATS_CACHE[user_id] = check_res
            missing_videos = check_res.get("missing_videos", [])
            TERA_MISSING_VIDEOS_CACHE[user_id] = missing_videos

        if not missing_videos:
            await query.answer("🎉 All TeraBox videos are already in Google Photos! Nothing to transfer.", show_alert=True)
            return

        await query.answer(f"🚀 Starting transfer of {len(missing_videos)} missing videos...")
        status_msg = await query.message.reply_text(
            f"🚀 **Starting Transfer of Missing Videos to Google Photos**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 **Queue:** `{len(missing_videos)}` missing videos\n"
            f"⏱️ **Cooldown:** `10s` between each transfer\n"
            "Initializing pipeline...",
            reply_markup=build_teratransfer_keyboard(),
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        async def _update_status(txt: str, is_finished: bool = False):
            kb = None if is_finished else build_teratransfer_keyboard()
            await edit_status_msg_safe(status_msg, txt, reply_markup=kb)
        asyncio.create_task(execute_transfer_job(missing_videos, status_updater=_update_status))
        return

    if action == "teragphotos_transfer":
        if is_transfer_in_progress():
            await query.answer("⚠️ A transfer is already in progress. Use /canceltransfer to stop it.", show_alert=True)
            return
        auth_ok, auth_msg = is_photos_authenticated()
        if not auth_ok:
            await query.answer("⚠️ Google Photos is not connected. Use /gphotos_auth first.", show_alert=True)
            return
        videos = TERA_ACCOUNT_VIDEOS_CACHE.get(user_id, [])
        if not videos:
            res = await fetch_account_videos()
            videos = res.get("videos", [])
            TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos
        if not videos:
            await query.answer("❌ No video files found in TeraBox account to transfer.", show_alert=True)
            return
        await query.answer("🚀 Starting transfer to Google Photos...")
        status_msg = await query.message.reply_text(
            f"🚀 **Transfer Pipeline Initialized**\n"
            f"Queue: `{len(videos)}` videos\n"
            "Cooldown: `10s` between transfers\n"
            "Starting...",
            reply_markup=build_teratransfer_keyboard(),
        )
        async def _update_status(txt: str, is_finished: bool = False):
            kb = None if is_finished else build_teratransfer_keyboard()
            await edit_status_msg_safe(status_msg, txt, reply_markup=kb)
        asyncio.create_task(execute_transfer_job(videos, status_updater=_update_status))
        return

    if action in ("teratransfer_skip", "teragphotos_skip"):
        if not is_transfer_in_progress():
            await query.answer("ℹ️ No active transfer job is currently running.", show_alert=True)
            return
        skipped = skip_current_transfer()
        if skipped:
            await query.answer("⏭️ Skipping current video! Purging cache and memory...", show_alert=False)
        else:
            await query.answer("⚠️ Could not skip; no file is currently transferring or transfer is ending.", show_alert=True)
        return

    if action in ("teratransfer_cancel", "teragphotos_cancel"):
        if not is_transfer_in_progress():
            await query.answer("ℹ️ No active transfer job is currently running.", show_alert=True)
            return
        cancel_current_transfer()
        await query.answer("🛑 Transfer cancellation requested.", show_alert=True)
        return

    if action == "stop":
        task_id = parts[1] if len(parts) > 1 else ""
        requester_id = TASK_REQUESTERS.get(task_id)
        user_id = query.from_user.id
        if requester_id and user_id != requester_id and not is_admin(user_id):
            await query.answer("⚠️ Only the user who sent this link (or admin) can stop it.", show_alert=True)
            return

        task = ACTIVE_TASKS.pop(task_id, None)
        if task and not task.done():
            task.cancel()

        TASK_REQUESTERS.pop(task_id, None)
        try:
            await query.message.delete()
        except Exception:
            await edit_query_message(query, "🛑 **Process stopped by user.**")
        await query.answer("Stopped.")
        return

    if action == "stop_vnc_session":
        user_id = query.from_user.id
        if not is_admin(user_id):
            await query.answer("⚠️ Only the administrator can stop VNC.", show_alert=True)
            return
        await stop_vnc_session()
        await edit_query_message(query, "🛑 **VNC Browser Session Closed.**")
        await query.answer("VNC Closed.")
        return

    if action == "cancel_queue":
        queue_id = parts[1] if len(parts) > 1 else ""
        if queue_id in ACTIVE_QUEUES:
            ACTIVE_QUEUES[queue_id]["cancelled"] = True
            try:
                await query.answer("🛑 Queue cancellation requested! Stopping after current item completes.", show_alert=True)
            except Exception:
                pass
        else:
            try:
                await query.answer("⚠️ Queue is no longer active.", show_alert=False)
            except Exception:
                pass
        return

    if action == "cancel":
        cache_key = parts[1] if len(parts) > 1 else ""
        URL_CACHE.pop(cache_key, None)
        try:
            await query.message.delete()
        except Exception:
            await edit_query_message(query, "❌ Cancelled.")
        return

    if action in {"formats", "back"}:
        cache_key = parts[1] if len(parts) > 1 else ""
        cached_data = URL_CACHE.get(cache_key)
        if not cached_data:
            await edit_query_message(query, "⚠️ This download request has expired. Please send the link again.")
            return

        if action == "back":
            await edit_query_message(
                query,
                f"📌 **{cached_data['title']}**\n\n👇 *Choose quality to download:*",
                build_quality_keyboard(cache_key),
            )
            return

        choices = cached_data.get("format_choices") or []
        if not choices:
            await edit_query_message(
                query,
                "⚠️ yt-dlp did not report any directly downloadable video formats for this link. "
                "Update yt-dlp and try the link again.",
                build_quality_keyboard(cache_key),
            )
            return

        await edit_query_message(
            query,
            "🎛 **Available formats**\n\n"
            "These are the actual video formats yt-dlp found for this link. "
            "A `+ audio` option merges the selected video stream with audio.\n\n"
            "Choose one to download:",
            build_format_keyboard(cache_key, choices),
        )
        return

    format_selector = None
    selected_choices = None
    if action == "fmt" and len(parts) >= 3:
        try:
            format_index = int(parts[1])
        except ValueError:
            return
        cache_key = parts[2]
        cached_data = URL_CACHE.get(cache_key)
        if not cached_data:
            await edit_query_message(query, "⚠️ This download request has expired. Please send the link again.")
            return
        selected_choices = cached_data.get("format_choices") or []
        if not 0 <= format_index < len(selected_choices):
            await edit_query_message(
                query,
                "⚠️ That format is no longer available. Please choose another one.",
                build_format_keyboard(cache_key, selected_choices),
            )
            return
        selected_format = selected_choices[format_index]
        quality = "format"
        quality_label = selected_format["label"]
        format_selector = selected_format["selector"]
    elif action == "dl" and len(parts) >= 3:
        quality = parts[1]
        cache_key = parts[2]
        cached_data = URL_CACHE.get(cache_key)
        if not cached_data:
            await edit_query_message(query, "⚠️ This download request has expired. Please send the link again.")
            return
        quality_label = QUALITIES.get(quality, quality)
    else:
        return

    url = cached_data["url"]
    title = cached_data["title"]
    uploader = cached_data.get("uploader", "")
    duration_sec = cached_data.get("duration_sec")
    failure_markup = (
        build_format_keyboard(cache_key, selected_choices)
        if selected_choices is not None
        else build_quality_keyboard(cache_key)
    )

    # Check if user is authorized to interact with this panel in group chats
    requester_id = cache_key.split("_")[0] if cache_key else None
    if requester_id and str(query.from_user.id) != requester_id and not is_admin(query.from_user.id):
        await query.answer("⚠️ Only the user who sent this link can select the quality.", show_alert=True)
        return

    # Check concurrency limit before starting download
    if get_active_downloads_count() >= MAX_CONCURRENT_DOWNLOADS:
        await query.answer(
            f"⚠️ Server Busy: {MAX_CONCURRENT_DOWNLOADS} downloads already in progress. Please try again in a moment.",
            show_alert=True,
        )
        return

    # Update message status to downloading
    status_text = f"⏳ **Downloading [{quality_label}]...**\n📌 *{title}*\n\nPlease wait..."
    await edit_query_message(query, status_text)

    # Perform download with concurrency semaphore to safeguard CPU & RAM
    downloaded_file = None
    completed = False
    task_id = f"{query.from_user.id}_{time.time()}"

    try:
        ACTIVE_TASKS[task_id] = asyncio.current_task()
        async with get_semaphore():
            success, downloaded_file, info, error_msg = await route_download_media(
                url,
                quality=quality,
                format_selector=format_selector,
                notify_admin_callback=lambda insp, tgt: notify_admin_of_captcha(context.bot, insp, tgt),
                progress_updater=lambda txt: edit_query_message(query, txt),
            )

            if not success or not downloaded_file or not os.path.exists(downloaded_file):
                err = error_msg or "Failed to download media."
                error_response = (
                    f"❌ **Download failed:**\n`{err[:200]}`\n\n"
                    "Choose another available format or try again."
                )
                await edit_query_message(query, error_response, failure_markup)
                return

            # Check file size
            file_size = os.path.getsize(downloaded_file)
            if file_size > MAX_FILE_SIZE_BYTES:
                size_str = format_bytes(file_size)
                warning_msg = (
                    f"⚠️ **File Too Large!**\n\n"
                    f"Downloaded file size is **{size_str}**, which exceeds Telegram's **{MAX_FILE_SIZE_MB}MB** limit.\n\n"
                    "💡 **Suggestion:** Try downloading in a lower resolution (e.g. 360p) or Audio Only (MP3)."
                )
                await edit_query_message(query, warning_msg, failure_markup)
                return

            # Update status to uploading
            upload_status = f"📤 **Uploading {format_bytes(file_size)} to Telegram...**"
            await edit_query_message(query, upload_status)

            # Send media file
            await send_media_to_chat(
                bot=context.bot,
                chat_id=update.effective_chat.id,
                file_path=downloaded_file,
                title=title,
                uploader=uploader,
                duration_sec=duration_sec,
                url=url,
                quality=quality,
                info=info,
                progress_status_updater=lambda txt: edit_query_message(query, txt),
            )

            # Clear out / delete the quality selector panel message on successful upload
            try:
                await query.message.delete()
            except Exception:
                pass
            completed = True
            mark_link_completed(url)
            try:
                await record_resolved_link(
                    user_id=query.from_user.id,
                    username=query.from_user.username,
                    first_name=query.from_user.first_name,
                    last_name=query.from_user.last_name,
                    url=url,
                    title=title,
                    file_size=file_size,
                    chat_id=update.effective_chat.id,
                    is_group=is_group,
                )
            except Exception as st_err:
                logger.error(f"Failed to record callback download stats: {st_err}")

    except Exception as e:
        logger.error(f"Error during download or upload: {e}", exc_info=True)
        err_msg = f"❌ An error occurred: {str(e)[:150]}"
        try:
            await edit_query_message(query, err_msg, failure_markup)
        except Exception:
            pass
    finally:
        ACTIVE_TASKS.pop(task_id, None)
        # Always remove temporary file from disk
        if downloaded_file:
            remove_file_safely(downloaded_file)
        if completed:
            URL_CACHE.pop(cache_key, None)


def get_cookie_target_path(platform: str) -> Optional[Tuple[str, Path]]:
    """Resolves platform name to its isolated cookie file path."""
    p = platform.lower().strip()
    if p in ("yt", "youtube"):
        return "YouTube", BASE_DIR / "cookies.txt"
    elif p in ("ig", "instagram", "insta"):
        return "Instagram", BASE_DIR / "cooky" / "instagram" / "cookies.txt"
    elif p in ("fb", "facebook"):
        return "Facebook", BASE_DIR / "cooky" / "facebook" / "cookies.txt"
    elif p in ("tb", "terabox", "tera"):
        return "TeraBox", BASE_DIR / "cooky" / "terabox" / "cookies.txt"
    elif p in ("gen", "generic", "other", "all"):
        return "Generic", BASE_DIR / "cooky" / "generic" / "cookies.txt"
    return None


async def setcookie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets raw cookie text for a specific platform. Usage: /setcookie <platform> <cookie_text>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📝 **Usage:** `/setcookie <platform> <cookie_text>`\n\n"
            "**Supported Platforms:**\n"
            "• `youtube` (or `yt`)\n"
            "• `instagram` (or `ig`)\n"
            "• `facebook` (or `fb`)\n"
            "• `terabox` (or `tb`)\n"
            "• `generic` (or `gen`)\n\n"
            "💡 *Alternatively, you can just send the `cookies.txt` file as a document directly to this chat!*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    platform_key = context.args[0]
    cookie_content = update.message.text.split(None, 2)[2].strip()

    target = get_cookie_target_path(platform_key)
    if not target:
        await update.message.reply_text("❌ Unknown platform. Choose: `youtube`, `instagram`, `facebook`, `terabox`, or `generic`.")
        return

    plat_name, file_path = target
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(cookie_content)

    line_count = len(cookie_content.strip().splitlines())
    await update.message.reply_text(
        f"✅ **Saved {plat_name} Cookies!**\n"
        f"📁 Path: `{file_path.name}`\n"
        f"📊 Lines: `{line_count}`",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def clearcookie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clears/deletes cookies for a platform. Usage: /clearcookie <platform>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        await update.message.reply_text("📝 **Usage:** `/clearcookie <youtube|instagram|facebook|terabox|generic>`")
        return

    target = get_cookie_target_path(context.args[0])
    if not target:
        await update.message.reply_text("❌ Unknown platform. Choose: `youtube`, `instagram`, `facebook`, `terabox`, or `generic`.")
        return

    plat_name, file_path = target
    if file_path.exists():
        file_path.unlink()
        await update.message.reply_text(f"🗑️ **Cleared {plat_name} Cookies.**", parse_mode=constants.ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"ℹ️ No existing cookie file found for {plat_name}.")


async def cookiestatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows cookie status for all isolated platforms."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    platforms = [
        ("🔴 YouTube", BASE_DIR / "cookies.txt"),
        ("📸 Instagram", BASE_DIR / "cooky" / "instagram" / "cookies.txt"),
        ("🔵 Facebook", BASE_DIR / "cooky" / "facebook" / "cookies.txt"),
        ("📦 TeraBox", BASE_DIR / "cooky" / "terabox" / "cookies.txt"),
        ("🌐 Generic", BASE_DIR / "cooky" / "generic" / "cookies.txt"),
    ]

    status_lines = ["🍪 **Cookie Storage Status:**\n"]
    for name, path in platforms:
        if path.exists() and path.is_file() and path.stat().st_size > 0:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                lines = len(f.readlines())
            size_kb = path.stat().st_size / 1024
            status_lines.append(f"• {name}: ✅ **Active** (`{lines}` lines, `{size_kb:.1f} KB`)")
        else:
            status_lines.append(f"• {name}: ⚪ *Not set*")

    status_lines.append("\n💡 *To update, send a `cookies.txt` file as document or use `/setcookie`.*")
    await update.message.reply_text("\n".join(status_lines), parse_mode=constants.ParseMode.MARKDOWN)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles uploaded cookie files (.txt) sent as documents."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    doc = update.message.document
    if not doc:
        return

    filename = (doc.file_name or "").lower()
    caption = (update.message.caption or "").lower()

    # Determine platform from caption or filename
    target_platform = None
    if "instagram" in filename or "instagram" in caption or "insta" in caption or "ig" in caption:
        target_platform = "instagram"
    elif "facebook" in filename or "facebook" in caption or "fb" in caption:
        target_platform = "facebook"
    elif "terabox" in filename or "terabox" in caption or "tera" in caption or "tb" in caption:
        target_platform = "terabox"
    elif "generic" in filename or "generic" in caption:
        target_platform = "generic"
    elif "youtube" in filename or "youtube" in caption or "yt" in caption or "cookies.txt" in filename:
        target_platform = "youtube"

    if not target_platform:
        await update.message.reply_text(
            "📁 **Received document:**\n"
            "Please send the file with a caption specifying the platform, for example:\n"
            "`instagram`, `facebook`, `terabox`, `youtube`, or `generic`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    target = get_cookie_target_path(target_platform)
    if not target:
        return

    plat_name, target_path = target
    target_path.parent.mkdir(parents=True, exist_ok=True)

    status_msg = await update.message.reply_text(f"⏳ Saving {plat_name} cookies...")
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(custom_path=str(target_path))

        with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = len(f.readlines())

        await status_msg.edit_text(
            f"✅ **{plat_name} Cookies Updated!**\n"
            f"📁 Saved to: `{target_path.name}`\n"
            f"📊 Total lines: `{lines}`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Error saving cookie document: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Failed to save cookie file: {e}")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to terminate all active download processes and reset temporary storage."""
    global _SEMAPHORE
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    killed_count = 0
    for tid, task in list(ACTIVE_TASKS.items()):
        if not task.done():
            task.cancel()
            killed_count += 1
    ACTIVE_TASKS.clear()
    TASK_REQUESTERS.clear()
    URL_CACHE.clear()

    for qid, qdata in list(ACTIVE_QUEUES.items()):
        qdata["cancelled"] = True
        qtask = qdata.get("task")
        if qtask and not qtask.done():
            qtask.cancel()
    ACTIVE_QUEUES.clear()

    # Instantly recreate the concurrency semaphore so all slots are 100% free
    _SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

    # Terminate all orphaned chrome, chromium, node crawlers, and ffmpeg processes
    killed_processes = []
    if os.name != "nt":
        try:
            subprocess.run(["pkill", "-9", "-f", "chrome|chromium|terabox_crawler|node|ffmpeg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed_processes.append("Chrome/Chromium & Node Crawlers")
        except Exception:
            pass
        # Clean up stale X11 locks
        for lock in ["/tmp/.X11-unix/X99", "/tmp/.X99-lock"]:
            if os.path.exists(lock):
                try:
                    os.remove(lock)
                except Exception:
                    pass
        killed_processes.append("X11 Server Locks")
    else:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["taskkill", "/F", "/IM", "node.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["taskkill", "/F", "/IM", "ffmpeg.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killed_processes.append("Windows Chrome, Node & FFmpeg")
        except Exception:
            pass

    # Stop any running VNC browser session
    try:
        await stop_vnc_session()
        killed_processes.append("VNC Browser Session")
    except Exception:
        pass

    # Clear anti-bot link cooldown cache
    clear_link_cooldowns()
    killed_processes.append("Anti-Bot Cooldown Cache")

    # Clean orphaned files in downloads folder
    from config import DOWNLOAD_DIR
    cleaned_files = 0
    try:
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file() and not f.name.startswith("."):
                try:
                    f.unlink()
                    cleaned_files += 1
                except Exception:
                    pass
    except Exception:
        pass

    await update.message.reply_text(
        f"🔄 **Server Hard Reset & Refreshed!**\n\n"
        f"• **Cancelled Tasks:** `{killed_count}`\n"
        f"• **Killed Processes:** `{', '.join(killed_processes)}`\n"
        f"• **Cleaned Temp Files:** `{cleaned_files}`\n"
        f"• **Concurrency Slots:** `{MAX_CONCURRENT_DOWNLOADS}/{MAX_CONCURRENT_DOWNLOADS}`\n"
        f"• **State:** 🟢 **100% Ready for new downloads**",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def startgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activates bot in the current group chat. Restricted to bot developer/admin."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE, reply_to_message_id=update.message.message_id)
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "ℹ️ Please run `/startgc` inside the group chat you want to activate.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    enable_group(chat.id, chat.title)
    await update.message.reply_text(
        f"✅ **Bot Activated for this Group!**\n\n"
        f"📌 **Group:** `{chat.title}`\n"
        f"🆔 **Chat ID:** `{chat.id}`\n\n"
        f"🚀 All members can now send video links directly here to download (YouTube, Instagram, Facebook, TeraBox, TikTok, etc.)!",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def stopgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivates bot in the current group chat. Restricted to bot developer/admin."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE, reply_to_message_id=update.message.message_id)
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "ℹ️ Please run `/stopgc` inside the group chat you want to deactivate.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    disable_group(chat.id)
    await update.message.reply_text(
        f"🛑 **Bot Deactivated for this Group.**\n"
        f"No download requests will be processed in `{chat.title}` until re-enabled by the admin.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Authorizes a user by Telegram ID to use the bot in private DMs. Usage: /allow <user_id> [note]"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        await update.message.reply_text(
            "📝 **Usage:** `/allow <telegram_user_id> [optional_name_or_note]`\n\n"
            "💡 *Example:* `/allow 123456789 John`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    raw_target_id = context.args[0].strip()
    if not raw_target_id.isdigit():
        await update.message.reply_text("❌ Invalid Telegram User ID. It must be numbers only (e.g. `123456789`).")
        return

    target_id = int(raw_target_id)
    note = " ".join(context.args[1:]).strip() if len(context.args) > 1 else ""

    allow_user(target_id, note)
    note_str = f" ({note})" if note else ""
    await update.message.reply_text(
        f"✅ **User Authorized for DMs!**\n\n"
        f"• **Telegram ID:** `{target_id}`{note_str}\n"
        f"• **Access:** Can now download videos directly in private messages.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def disallow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Revokes a user's DM access by Telegram ID. Usage: /disallow <user_id>"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        await update.message.reply_text(
            "📝 **Usage:** `/disallow <telegram_user_id>`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    raw_target_id = context.args[0].strip()
    if not raw_target_id.isdigit():
        await update.message.reply_text("❌ Invalid Telegram User ID.")
        return

    target_id = int(raw_target_id)
    disallow_user(target_id)
    await update.message.reply_text(
        f"🛑 **User Revoked from DMs!**\n\n"
        f"• **Telegram ID:** `{target_id}`\n"
        f"• **Access:** Private DM access has been deactivated.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def listusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all authorized DM users."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    users = list_allowed_users()
    if not users:
        await update.message.reply_text(
            "📋 **Authorized DM Users:** None\n\n"
            "💡 *Use `/allow <user_id>` to authorize someone.*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    lines = [f"📋 **Authorized DM Users ({len(users)}):**\n"]
    for u in users:
        uid = u.get("user_id")
        note = u.get("note", "")
        added = u.get("added_at", "")
        note_text = f" — *{note}*" if note else ""
        date_text = f" `[{added}]`" if added else ""
        lines.append(f"• `{uid}`{note_text}{date_text}")

    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN, reply_to_message_id=update.message.message_id)


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles explicit /dl and /download commands."""
    chat = update.effective_chat
    user_id = update.effective_user.id
    is_group = chat.type in ("group", "supergroup")

    if not is_group and not is_user_allowed(user_id):
        await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        return

    if is_group and not is_group_allowed(chat.id):
        await update.message.reply_text(
            "⛔ This bot is not activated in this group. Contact the bot administrator to activate it with `/startgc`.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "📝 **Usage:** `/dl <video_url>`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    await handle_message(update, context)


async def setdiskwala_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to update Diskwala MiniApp bearer token in .env."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "📝 **Usage:** `/setdiskwala <bearer_token_or_initData>`\n\n"
            "💡 *Paste the full Authorization string or initData extracted from the miniapp.*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    new_token = parts[1].strip()
    if save_diskwala_token(new_token):
        await update.message.reply_text(
            "✅ **Diskwala Token Saved to `.env`!**\n\n"
            "Direct API resolution with AES-GCM decryption is now active.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
    else:
        await update.message.reply_text("❌ Failed to save Diskwala token to `.env`.")


async def diskwalastatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to check Diskwala resolver status."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    tok = get_diskwala_token()
    token_status = "🟢 Configured (.env Active)" if tok else "🔴 Not Set (Use /setdiskwala or /vnc)"

    msg = (
        "💿 **Diskwala Resolver Status**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ **MiniApp Direct API**: {token_status}\n"
        "🔐 **Decryption**: AES-GCM (Hardware-Accelerated)\n"
        "📁 **Storage**: Saved directly in `.env` (`DISKWALA_BEARER_TOKEN`)\n"
        f"🔄 **Telethon Auto-Refresh**: ⚡ On-Demand (Triggers if expired >{TOKEN_MAX_AGE_HOURS}h or on HTTP 401)\n\n"
        "💡 *Use `/ezdisk` for instant Telethon refresh or `/setdiskwala <token>`.*"
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN, reply_to_message_id=update.message.message_id)


async def setterabox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to update TeraBox MiniApp bearer token in .env."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "📝 **Usage:** `/setterabox <bearer_token_or_initData>`\n\n"
            "💡 *Paste the full Authorization string or initData extracted from the miniapp.*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    new_token = parts[1].strip()
    if save_terabox_token(new_token):
        await update.message.reply_text(
            "✅ **TeraBox Token Saved to `.env`!**\n\n"
            "High-speed MiniApp API resolution is now active as Priority 1.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
    else:
        await update.message.reply_text("❌ Failed to save TeraBox token to `.env`.")


async def teraboxstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to check TeraBox resolver status."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    tok = get_terabox_token()
    token_status = "🟢 Configured (.env Active)" if tok else "🔴 Not Set (Use /setterabox or /vnc)"

    msg = (
        "📦 **TeraBox Resolver Status**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ **Priority 1 (MiniApp API)**: {token_status}\n"
        "🕷️ **Priority 2 (Headless Crawler)**: 🟢 Available (Fallback)\n"
        "🍪 **Priority 3 (Gateway / Cookies)**: 🟢 Available (Fallback)\n"
        "📁 **Storage**: Saved directly in `.env` (`TERABOX_BEARER_TOKEN`)\n"
        f"🔄 **Telethon Auto-Refresh**: ⚡ On-Demand (Triggers if expired >{TOKEN_MAX_AGE_HOURS}h or on HTTP 401)\n\n"
        "💡 *Use `/eztera` for instant Telethon refresh or `/setterabox <token>`.*"
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN, reply_to_message_id=update.message.message_id)


async def vnc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to start VNC desktop with persistent Telegram profile for token capture."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    target_bot = context.args[0] if context.args else None
    status_msg = await update.message.reply_text(
        "⏳ **Firing up VNC Desktop & Telegram Web...**\n"
        "Starting Xvfb virtual display, x11vnc, noVNC proxy, and persistent browser session...",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    async def on_token_captured(platform: str, captured_tok: str):
        if platform == "google_oauth":
            if captured_tok == "success":
                msg = (
                    "🎉 **Google Photos Connected Automatically via VNC!**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    "• **Tokens:** Saved securely to `data/google_photos_token.json`\n"
                    "• **Offline Access:** Refresh token configured (auto-renews)\n"
                    "• **Action:** You can now run `/teratransfer` to start uploading!"
                )
            else:
                msg = f"❌ **Google Photos Auto-Connection Failed:**\n`{captured_tok}`"
        elif platform == "terabox_cookie":
            msg = (
                "🎉 **TeraBox Account Cookie Captured Automatically!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "• **Saved to:** `cooky/terabox/cookies.txt`\n"
                "• **Status:** Active & Ready for `/terafetch` and `/teratransfer`!\n"
                "• **Action:** You can now close your VNC viewer tab or click Stop below."
            )
        else:
            plat_title = "TeraBox" if platform == "terabox" else "Diskwala"
            env_key = "TERABOX_BEARER_TOKEN" if platform == "terabox" else "DISKWALA_BEARER_TOKEN"
            msg = (
                f"🎉 **{plat_title} Bearer Token Captured Automatically!**\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"• **Saved to:** `.env` (`{env_key}`)\n"
                f"• **Engine:** {plat_title} MiniApp API is now active!\n"
                "• **Action:** You can now close your VNC viewer tab or click Stop below."
            )
        try:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛑 Stop VNC Browser", callback_data="stop_vnc_session")],
            ])
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Error sending token notification: {e}")

    success, vnc_url, err = await start_vnc_capture_session(
        target_bot=target_bot,
        on_token_captured=on_token_captured,
    )

    if not success:
        await status_msg.edit_text(
            f"❌ **Failed to start VNC:**\n`{err}`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥️ Open Live VNC Desktop", url=vnc_url)],
        [InlineKeyboardButton("🛑 Stop VNC Browser", callback_data="stop_vnc_session")],
    ])

    instructions = (
        "🖥️ **Live VNC Desktop is Ready!**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔗 **Web Viewer:** [Click Here to Open VNC]({vnc_url})\n\n"
        "📋 **Steps to capture token:**\n"
        "1. Click the button below to view the browser live.\n"
        "2. *(First time only)* Log in to your Telegram account. Your session is saved permanently in your server profile!\n"
        "3. Open the bot & click the Diskwala or TeraBox MiniApp button.\n"
        "4. As soon as the MiniApp opens, the bot **automatically captures the Bearer Token** and saves it to `.env`!\n\n"
        "💡 *Click 'Stop VNC Browser' or use `/stopvnc` when finished.*"
    )

    await status_msg.edit_text(
        instructions,
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def stopvnc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to stop VNC browser session."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    await stop_vnc_session()
    await update.message.reply_text(
        "🛑 **VNC Browser Session Closed.**\nChrome and capture processes have been terminated.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def autovnc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to automatically navigate Telegram Web, trigger MiniApp, enter link, and capture Bearer token."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "📝 **Usage:** `/autovnc <diskwala|tera> [link_or_bot]`\n\n"
            "💡 *Defaults (no link needed):*\n"
            "• `/autovnc diskwala` → opens `https://web.telegram.org/a/#7802633228`\n"
            "• `/autovnc tera` → opens `https://web.telegram.org/a/#7802009139`\n\n"
            "🔗 *You can also pass a custom link or peer ID:*\n"
            "• `/autovnc diskwala https://web.telegram.org/a/#7802633228`\n\n"
            "🤖 *The bot will launch Chrome on VNC, focus the active chat tab directly, click Open MiniApp, type a dummy link, click Download, and auto-capture the token into `.env`!*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    platform_raw = context.args[0].lower().strip()
    if platform_raw in ("dw", "diskwala"):
        platform = "diskwala"
        plat_title = "Diskwala"
    elif platform_raw in ("tera", "terabox", "tb"):
        platform = "tera"
        plat_title = "TeraBox"
    else:
        await update.message.reply_text(
            "❌ Unknown platform. Please specify either `diskwala` or `tera`.\n"
            "Usage: `/autovnc <diskwala|tera> [link_or_bot]`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    target_bot = context.args[1] if len(context.args) > 1 else None
    display_target = target_bot or ("https://web.telegram.org/a/#7802633228" if platform == "diskwala" else "https://web.telegram.org/a/#7802009139")

    status_msg = await update.message.reply_text(
        f"🤖 **Starting Auto-VNC for {plat_title}...**\n"
        f"🎯 Target: `{display_target}`\n"
        "Initializing virtual display, stealth browser, and focusing Telegram chat...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    captured = False

    async def on_token_captured(plat: str, captured_tok: str):
        nonlocal captured
        captured = True
        title = "TeraBox" if plat == "terabox" else "Diskwala"
        env_key = "TERABOX_BEARER_TOKEN" if plat == "terabox" else "DISKWALA_BEARER_TOKEN"
        msg = (
            f"🎉 **{title} Bearer Token Auto-Captured!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• **Saved to:** `.env` (`{env_key}`)\n"
            f"• **Status:** {title} Direct API is now active & ready for downloads!\n"
            "• **Browser:** Closed cleanly."
        )
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Error sending token notification: {e}")

    async def on_progress_update(status_txt: str):
        if captured:
            return
        try:
            await status_msg.edit_text(
                f"🤖 **Auto-VNC ({plat_title}) In Progress**\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚡ {status_txt}\n\n"
                "💡 *You can watch live using the button below or let it run automatically.*",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🖥️ Watch Live on VNC", url=vnc_url)],
                    [InlineKeyboardButton("🛑 Stop Auto VNC", callback_data="stop_vnc_session")],
                ]),
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    success, vnc_url, err = await start_autovnc_session(
        platform=platform,
        target_bot=target_bot,
        on_token_captured=on_token_captured,
        progress_updater=on_progress_update,
    )

    if not success:
        await status_msg.edit_text(
            f"❌ **Failed to start Auto-VNC:**\n`{err}`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖥️ Watch Live on VNC", url=vnc_url)],
        [InlineKeyboardButton("🛑 Stop Auto VNC", callback_data="stop_vnc_session")],
    ])

    await status_msg.edit_text(
        f"🤖 **Auto-VNC ({plat_title}) Started!**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 **Target Bot:** `{target_bot or 'Default Telegram Web'}`\n"
        f"🔗 **Live Stream:** [Click here to watch VNC live]({vnc_url})\n\n"
        "⏳ Navigating to chat and triggering MiniApp...",
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def ezdisk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to instantly fetch & save DiskWala Bearer token via native Telethon MTProto."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    status_msg = await update.message.reply_text(
        "⚡ **Fetching DiskWala Bearer Token via Telethon MTProto...**\n"
        "Connecting to Telegram Data Center and requesting MiniApp...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    success, token, err = await fetch_telethon_bearer("diskwala")
    if success and token:
        excerpt = f"{token[:40]}...{token[-25:]}"
        await status_msg.edit_text(
            "🎉 **DiskWala Bearer Token Auto-Captured!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "• **Method:** Native MTProto Query (~0.3s)\n"
            "• **Target Bot:** `@sky577bot` (`#7802633228`)\n"
            "• **Saved to:** `.env` (`DISKWALA_BEARER_TOKEN`)\n"
            f"• **Token:** `{excerpt}`\n"
            "• **Status:** Diskwala Direct API is now active & ready for downloads!",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    else:
        await status_msg.edit_text(
            f"❌ **Failed to fetch DiskWala token via Telethon:**\n`{err}`\n\n"
            "💡 *Tip: Ensure the user session file exists on the server or use `/autovnc diskwala` as fallback.*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )


async def eztera_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to instantly fetch & save TeraBox Bearer token via native Telethon MTProto."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    status_msg = await update.message.reply_text(
        "⚡ **Fetching TeraBox Bearer Token via Telethon MTProto...**\n"
        "Connecting to Telegram Data Center and requesting MiniApp...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    success, token, err = await fetch_telethon_bearer("terabox")
    if success and token:
        excerpt = f"{token[:40]}...{token[-25:]}"
        await status_msg.edit_text(
            "🎉 **TeraBox Bearer Token Auto-Captured!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "• **Method:** Native MTProto Query (~0.3s)\n"
            "• **Target Bot:** `@terabox_downloader_new_bot` (`#7802009139`)\n"
            "• **Saved to:** `.env` (`TERABOX_BEARER_TOKEN`)\n"
            f"• **Token:** `{excerpt}`\n"
            "• **Status:** TeraBox Direct API is now active & ready for downloads!",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    else:
        await status_msg.edit_text(
            f"❌ **Failed to fetch TeraBox token via Telethon:**\n`{err}`\n\n"
            "💡 *Tip: Ensure the user session file exists on the server or use `/autovnc tera` as fallback.*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows analytics dashboard for Admins or personal statistics card for regular users.
    Usage: /stats or /stats <user_id>
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    user_id = user.id
    is_group = chat.type in ("group", "supergroup")

    if not is_group and not is_user_allowed(user_id):
        await update.message.reply_text(UNAUTHORIZED_DM_MESSAGE)
        return

    # If regular user: show personal download statistics
    if not is_admin(user_id):
        personal_text = format_personal_stats_text(
            user_id=user_id,
            username=user.username,
            first_name=user.first_name,
        )
        await update.message.reply_text(
            personal_text,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    # If Admin passed a specific user ID: /stats <user_id>
    if context.args and len(context.args) >= 1:
        target_arg = context.args[0].replace("@", "").strip()
        try:
            target_id = int(target_arg)
            user_detail_text = format_user_detail_text(target_id)
            keyboard = build_stats_keyboard("user_detail", target_user_id=target_id)
            await update.message.reply_text(
                user_detail_text,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id,
            )
            return
        except ValueError:
            pass

    # Admin full overview dashboard
    overview_text = format_overview_text()
    keyboard = build_stats_keyboard("overview")
    await update.message.reply_text(
        overview_text,
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def userstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin command to inspect a specific user's link history and stats.
    Usage: /userstats <user_id>
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        await update.message.reply_text(
            "📝 **Usage:** `/userstats <user_id>`\n\n"
            "💡 *Tip: You can find user IDs from `/stats` -> 🏆 Top Users.*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    try:
        target_id = int(context.args[0].replace("@", "").strip())
        user_detail_text = format_user_detail_text(target_id)
        keyboard = build_stats_keyboard("user_detail", target_user_id=target_id)
        await update.message.reply_text(
            user_detail_text,
            reply_markup=keyboard,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Please provide a numeric Telegram user ID.")


async def terafetch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fetches and displays the count and list of all video files in the TeraBox account using cookies.
    Usage: /terafetch or /teravideos
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    status_msg = await update.message.reply_text(
        "🔍 **Connecting to TeraBox account using cookies...**\n"
        "Fetching all video files and calculating sizes...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    result = await fetch_account_videos()
    if not result.get("success"):
        err = result.get("error") or "Unknown error while fetching videos."
        await status_msg.edit_text(
            f"❌ **Failed to fetch TeraBox videos:**\n`{err}`\n\n"
            "💡 *Tip: Ensure valid cookies are in `cooky/terabox/cookies.txt` or set via `/setteracookie <cookie>`.*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    videos = result.get("videos", [])
    TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos

    text = format_terafetch_page_text(videos, page=0)
    keyboard = build_terafetch_keyboard(page=0, total_videos=len(videos))

    await status_msg.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def teratransfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Starts sequential transfer of TeraBox account videos to Google Photos with a 10s cooldown.
    Usage:
      • /teratransfer -> transfers all TeraBox videos
      • /teratransfer missing -> transfers ONLY videos not yet in Google Photos
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if is_transfer_in_progress():
        await update.message.reply_text(
            "⚠️ **A transfer job is already currently running!**\n"
            "Use `/canceltransfer` if you wish to abort the current job.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    # Check Google Photos authorization
    auth_ok, auth_msg = is_photos_authenticated()
    if not auth_ok:
        await update.message.reply_text(
            f"⚠️ **Google Photos is not authenticated!**\n\n"
            f"Status: `{auth_msg}`\n\n"
            "👉 Please run `/gphotos_auth` to link your Google Photos account first.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    transfer_only_missing = False
    if context.args and context.args[0].lower() in ("missing", "diff", "unsynced", "new"):
        transfer_only_missing = True

    if transfer_only_missing:
        videos = TERA_MISSING_VIDEOS_CACHE.get(user_id, [])
        if not videos:
            fetch_msg = await update.message.reply_text(
                "⏳ **Checking missing videos against Google Photos first...**",
                parse_mode=constants.ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id,
            )
            check_res = await check_terabox_vs_google_photos()
            if not check_res.get("success"):
                err = check_res.get("error") or "Failed to cross-reference videos."
                await fetch_msg.edit_text(f"❌ **Transfer Aborted:** {err}")
                return
            TERA_CHECK_STATS_CACHE[user_id] = check_res
            videos = check_res.get("missing_videos", [])
            TERA_MISSING_VIDEOS_CACHE[user_id] = videos
            TERA_ACCOUNT_VIDEOS_CACHE[user_id] = check_res.get("all_videos", [])
            try:
                await fetch_msg.delete()
            except Exception:
                pass

        if not videos:
            await update.message.reply_text(
                "🎉 **All TeraBox videos are already in Google Photos!**\nThere are no missing videos to transfer.",
                parse_mode=constants.ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id,
            )
            return

        label_type = "Missing Videos"
    else:
        # Get cached videos or fetch fresh
        videos = TERA_ACCOUNT_VIDEOS_CACHE.get(user_id, [])
        if not videos:
            fetch_msg = await update.message.reply_text(
                "⏳ **Fetching video library from TeraBox account first...**",
                parse_mode=constants.ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id,
            )
            res = await fetch_account_videos()
            if not res.get("success") or not res.get("videos"):
                err = res.get("error") or "No video files found in account."
                await fetch_msg.edit_text(f"❌ **Transfer Aborted:** {err}")
                return
            videos = res.get("videos", [])
            TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos
            try:
                await fetch_msg.delete()
            except Exception:
                pass
        label_type = "All Videos"

    status_msg = await update.message.reply_text(
        f"🚀 **Starting Transfer ({label_type}) to Google Photos**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 **Queue:** `{len(videos)}` videos\n"
        f"⏱️ **Cooldown:** `10s` between each transfer\n"
        "Initializing pipeline...",
        reply_markup=build_teratransfer_keyboard(),
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    async def _update_status(txt: str, is_finished: bool = False):
        kb = None if is_finished else build_teratransfer_keyboard()
        await edit_status_msg_safe(status_msg, txt, reply_markup=kb)

    asyncio.create_task(execute_transfer_job(videos, status_updater=_update_status))


async def teracheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Checks which videos from the TeraBox account are not yet present in Google Photos,
    and provides an option to transfer only the missing ones.
    Usage: /teracheck or /checkphotos or /terasync
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    status_msg = await update.message.reply_text(
        "🔍 **Cross-Referencing TeraBox vs Google Photos Library...**\n"
        "Connecting to TeraBox account and checking against Google Photos upload records...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    check_res = await check_terabox_vs_google_photos()
    if not check_res.get("success"):
        err = check_res.get("error") or "Unknown error checking videos."
        await status_msg.edit_text(
            f"❌ **Sync Check Failed:**\n`{err}`\n\n"
            "💡 *Tip: Ensure valid cookies in `cooky/terabox/cookies.txt` and Google Photos is linked via `/gphotos_auth`.*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    TERA_CHECK_STATS_CACHE[user_id] = check_res
    TERA_MISSING_VIDEOS_CACHE[user_id] = check_res.get("missing_videos", [])
    TERA_ACCOUNT_VIDEOS_CACHE[user_id] = check_res.get("all_videos", [])

    text = format_teracheck_page_text(check_res, page=0)
    keyboard = build_teracheck_keyboard(page=0, total_missing=check_res.get("missing_count", 0))

    await status_msg.edit_text(
        text,
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def canceltransfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Aborts any running TeraBox -> Google Photos transfer job.
    Usage: /canceltransfer
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not is_transfer_in_progress():
        await update.message.reply_text("ℹ️ No active transfer job is currently running.")
        return

    cancel_current_transfer()
    await update.message.reply_text(
        "🛑 **Transfer cancellation requested!**\n"
        "The active transfer will abort cleanly after completing any in-flight step.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def skiptransfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Skips the currently transferring video in an active TeraBox -> Google Photos job.
    Usage: /skiptransfer or /skip
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not is_transfer_in_progress():
        await update.message.reply_text("ℹ️ No active transfer job is currently running.")
        return

    skipped = skip_current_transfer()
    if skipped:
        await update.message.reply_text(
            "⏭️ **Skip signal sent!**\n"
            "The stuck transfer has been aborted, residual files purged from disk and memory, and the next file will start immediately.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
    else:
        await update.message.reply_text(
            "⚠️ **Could not skip right now.**\nEither no file is currently downloading/uploading, or the transfer is already completing.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )


async def gphotos_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Directly uploads any video sent by the Telegram bot (or in chat) to Google Photos.
    Usage:
        Reply to any video or video document with: /gphotos_upload (or /gupload, /gpush)
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    # 1. Check Google Photos authorization
    auth_ok, auth_msg = is_photos_authenticated()
    if not auth_ok:
        await update.message.reply_text(
            f"⚠️ **Google Photos is not authenticated!**\n\n"
            f"Status: `{auth_msg}`\n\n"
            "👉 Please run `/gphotos_auth` to link your Google Photos account first.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    # 2. Check for replied message or attached video
    target_msg = update.message.reply_to_message
    if not target_msg:
        if update.message.video or update.message.document:
            target_msg = update.message
        else:
            await update.message.reply_text(
                "ℹ️ **Google Photos Direct Video Upload (Dev)**\n\n"
                "👉 **How to use:**\n"
                "1. Find any video or video document sent by this bot.\n"
                "2. **Reply** to that video with `/gphotos_upload` (or `/gupload`, `/gpush`).\n\n"
                "⚡ The bot will fetch the video directly from Telegram via MTProto and upload it straight into your Google Photos library!",
                parse_mode=constants.ParseMode.MARKDOWN,
                reply_to_message_id=update.message.message_id,
            )
            return

    media_obj = target_msg.video or target_msg.document or target_msg.animation
    if not media_obj:
        await update.message.reply_text(
            "⚠️ The message you replied to does not contain a video or downloadable media file.",
            reply_to_message_id=update.message.message_id,
        )
        return

    file_name = getattr(media_obj, "file_name", None)
    file_unique_id = getattr(media_obj, "file_unique_id", str(int(time.time())))
    file_size = getattr(media_obj, "file_size", 0) or 0

    if not file_name:
        file_name = f"telegram_video_{file_unique_id}.mp4"

    # Ensure mp4 extension for Google Photos indexing if no extension
    if not Path(file_name).suffix:
        file_name = f"{file_name}.mp4"

    status_msg = await update.message.reply_text(
        f"⏳ **Processing Video for Google Photos...**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 **File:** `{file_name}`\n"
        f"📦 **Size:** `{format_bytes(file_size)}`\n"
        f"📥 **Step 1/2:** Downloading from Telegram server...",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    temp_dest = DOWNLOAD_DIR / f"gphotos_up_{file_unique_id}_{file_name}"
    downloaded_file = None

    try:
        # Step 1: Download from Telegram via MTProto (up to 2GB) or standard Bot API
        async def _dl_progress(cur: int, tot: int):
            if tot > 0:
                pct = int((cur / tot) * 100)
                await edit_status_msg_safe(
                    status_msg,
                    f"⏳ **Downloading from Telegram: {pct}%**\n"
                    f"🎬 `{file_name}`\n"
                    f"`[{format_bytes(cur)} / {format_bytes(tot)}]`",
                )

        if is_mtproto_active() or file_size > 20 * 1024 * 1024:
            dl_ok, dl_res = await download_media_mtproto(
                chat_id=target_msg.chat_id,
                message_id=target_msg.message_id,
                dest_path=str(temp_dest),
                progress_callback=_dl_progress,
            )
            if dl_ok and dl_res and os.path.exists(dl_res):
                downloaded_file = dl_res
            else:
                raise RuntimeError(f"Telegram download failed: {dl_res}")
        else:
            tg_file = await context.bot.get_file(media_obj.file_id)
            await tg_file.download_to_drive(custom_path=temp_dest)
            downloaded_file = str(temp_dest)

        if not downloaded_file or not os.path.exists(downloaded_file):
            raise RuntimeError("Downloaded file not found on disk.")

        actual_size = os.path.getsize(downloaded_file)

        # Step 2: Upload to Google Photos
        await edit_status_msg_safe(
            status_msg,
            f"☁️ **Step 2/2:** Uploading to Google Photos ({format_bytes(actual_size)})...",
        )

        async def _up_progress(p_txt: str):
            await edit_status_msg_safe(
                status_msg,
                f"☁️ **Uploading to Google Photos:**\n"
                f"🎬 `{file_name}`\n{p_txt}",
            )

        up_ok, up_res, up_err = await upload_video_to_google_photos(
            file_path=downloaded_file,
            custom_filename=file_name,
            progress_updater=lambda txt: asyncio.create_task(_up_progress(txt)),
        )

        if not up_ok or not up_res:
            err_msg = up_err or "Unknown upload error."
            await edit_status_msg_safe(
                status_msg,
                f"❌ **Google Photos Upload Failed:**\n`{err_msg}`",
            )
            return

        photo_id = up_res.get("id", "Unknown")
        prod_url = up_res.get("product_url", "")

        # Record in registry
        try:
            record_transferred_video(
                fs_id=f"tg_{file_unique_id}",
                filename=file_name,
                size=actual_size,
                google_id=photo_id,
                product_url=prod_url,
            )
        except Exception as reg_err:
            logger.debug(f"[Google Photos Upload] Registry error: {reg_err}")

        link_line = f"\n🔗 [View in Google Photos]({prod_url})" if prod_url else ""
        await edit_status_msg_safe(
            status_msg,
            f"✅ **Uploaded to Google Photos Successfully!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 **File:** `{file_name}`\n"
            f"📦 **Size:** `{format_bytes(actual_size)}`\n"
            f"🆔 **ID:** `{photo_id[:16]}...`"
            f"{link_line}",
        )

    except Exception as e:
        logger.error(f"[Google Photos Upload] Failed for message {target_msg.message_id}: {e}", exc_info=True)
        await edit_status_msg_safe(
            status_msg,
            f"❌ **Upload Failed:**\n`{str(e)}`",
        )
    finally:
        # Immediate cleanup of residual files from disk and memory
        if downloaded_file and os.path.exists(downloaded_file):
            remove_file_safely(downloaded_file)
        if temp_dest.exists():
            remove_file_safely(str(temp_dest))
        import gc
        gc.collect()


_OAUTH_HTTP_SERVER = None


async def _start_temp_oauth_listener(port: int, expected_path: str, bot, chat_id: int):
    """
    Lightweight temporary background asyncio server to catch Google OAuth redirect.
    Automatically grabs the code, saves tokens, and informs user in Telegram.
    """
    global _OAUTH_HTTP_SERVER
    if _OAUTH_HTTP_SERVER:
        try:
            _OAUTH_HTTP_SERVER.close()
        except Exception:
            pass

    expected_path_clean = expected_path.rstrip("/")

    async def handle_client(reader, writer):
        try:
            data = await reader.read(4096)
            req_text = data.decode("utf-8", errors="ignore")
            first_line = req_text.split("\r\n")[0]
            parts = first_line.split(" ")
            if len(parts) >= 2:
                req_url = parts[1]
                parsed = urllib.parse.urlparse(req_url)
                req_p = parsed.path.rstrip("/")
                if req_p == expected_path_clean or req_p == "":
                    qs = urllib.parse.parse_qs(parsed.query)
                    if "code" in qs:
                        code = qs["code"][0]
                        html = (
                            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Linked</title></head>"
                            "<body style='font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;"
                            "text-align:center;padding:50px;background:#f0fdf4;'>"
                            "<div style='background:#ffffff;padding:40px 50px;border-radius:16px;box-shadow:0 10px 25px rgba(0,0,0,0.08);text-align:center;max-width:480px;margin:0 auto;'>"
                            "<div style='font-size:55px;margin-bottom:15px;'>🎉</div>"
                            "<h2 style='color:#166534;margin:0 0 10px 0;'>Google Photos Linked!</h2>"
                            "<p style='color:#374151;font-size:16px;line-height:1.5;margin:0 0 20px 0;'>Your authorization was automatically captured. You can now close this tab and return to Telegram.</p>"
                            "<span style='background:#dcfce7;color:#15803d;padding:6px 14px;border-radius:20px;font-size:13px;font-weight:600;'>Return to Telegram</span>"
                            "</div></body></html>"
                        )
                        resp_bytes = html.encode("utf-8")
                        header = (
                            f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                            f"Content-Length: {len(resp_bytes)}\r\nConnection: close\r\n\r\n"
                        )
                        writer.write(header.encode("utf-8") + resp_bytes)
                        await writer.drain()
                        writer.close()

                        ok, err = await exchange_code_for_tokens(code)
                        if ok:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    "🎉 **Google Photos Connected Automatically via Web Redirect!**\n"
                                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                    "• **Status:** Active & Ready\n"
                                    "• **Tokens:** Saved securely to `data/google_photos_token.json`\n"
                                    "• **Offline Access:** Auto-refresh active\n\n"
                                    "👉 You can now run `/teratransfer` to start uploading!"
                                ),
                                parse_mode=constants.ParseMode.MARKDOWN,
                            )
                        else:
                            await bot.send_message(
                                chat_id=chat_id,
                                text=f"❌ **Auto-Exchange Failed:** `{err}`",
                            )
                        return
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    try:
        server = await asyncio.start_server(handle_client, "0.0.0.0", port)
        _OAUTH_HTTP_SERVER = server
        await asyncio.sleep(300)
        server.close()
        await server.wait_closed()
    except Exception as e:
        logger.debug(f"[OAuth Server] Background listener note: {e}")


async def gphotos_auth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initiates Google Photos OAuth 2.0 authorization.
    Usage: /gphotos_auth or /gauth
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    ok, auth_url = get_authorization_url()
    if not ok:
        await update.message.reply_text(
            f"❌ **Google OAuth Configuration Error:**\n`{auth_url}`\n\n"
            "Please ensure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set in `.env`.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    auth_status_ok, status_desc = is_photos_authenticated()
    current_status = "🟢 Already Linked" if auth_status_ok else "🔴 Not Connected"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Authorize with Google", url=auth_url)],
    ])

    try:
        from config import GOOGLE_REDIRECT_URI
    except ImportError:
        GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth2callback").strip()

    ruri = GOOGLE_REDIRECT_URI or "http://localhost:8080/oauth2callback"
    instructions = (
        "🔐 **Google Photos OAuth 2.0 Authorization**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• **Current Status:** {current_status} (`{status_desc}`)\n"
        f"• **Redirect URI:** `{ruri}`\n\n"
        "📋 **How to Connect:**\n"
        "1. Tap the **[ 🔗 Authorize with Google ]** button below.\n"
        "2. Log in and allow Google Photos library access.\n"
        "3. **Automatic:** If using VNC (`/vnc gauth`) or if your browser can reach the callback port, authorization completes automatically!\n"
        "4. **Manual Fallback:** Or copy the `code=...` parameter and send it here:\n"
        "   👉 `/gphotos_code <paste_your_code_here>`\n\n"
        "⚠️ *Note: If you get Error 400 redirect_uri_mismatch, ensure the Redirect URI above is added under Authorized redirect URIs in Google Cloud Console.*"
    )

    # Launch background auto-capture listener on the configured port
    try:
        parsed_ruri = urllib.parse.urlparse(ruri)
        port = parsed_ruri.port or 8080
        path = parsed_ruri.path or "/oauth2callback"
        asyncio.create_task(_start_temp_oauth_listener(port, path, context.bot, update.effective_chat.id))
    except Exception as le:
        logger.debug(f"[OAuth] Could not start background listener: {le}")

    await update.message.reply_text(
        instructions,
        reply_markup=keyboard,
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def gphotos_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Submits Google OAuth authorization code to acquire tokens.
    Usage: /gphotos_code <code>
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        await update.message.reply_text(
            "📝 **Usage:** `/gphotos_code <authorization_code_or_url>`\n\n"
            "Paste the authorization code received from the Google OAuth consent page.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    code_input = context.args[0].strip()
    status_msg = await update.message.reply_text(
        "⏳ **Exchanging authorization code with Google OAuth...**",
        reply_to_message_id=update.message.message_id,
    )

    ok, err = await exchange_code_for_tokens(code_input)
    if ok:
        await status_msg.edit_text(
            "🎉 **Google Photos Connected Successfully!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "• **Status:** Active & Ready\n"
            "• **Tokens:** Saved securely to `data/google_photos_token.json`\n"
            "• **Offline Access:** Refresh token configured (auto-renews)\n\n"
            "👉 You can now run `/teratransfer` to start uploading TeraBox videos!",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    else:
        await status_msg.edit_text(
            f"❌ **Failed to connect Google Photos:**\n`{err}`\n\n"
            "💡 *Tip: Authorization codes expire within a few minutes. Try generating a new link via `/gphotos_auth`.*",
            parse_mode=constants.ParseMode.MARKDOWN,
        )


async def gphotos_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Checks the connection status of Google Photos.
    Usage: /gphotos_status
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    is_ok, desc = is_photos_authenticated()
    icon = "🟢" if is_ok else "🔴"
    status_str = "Connected & Active" if is_ok else "Disconnected"

    msg = (
        "📸 **Google Photos Integration Status**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• **Connection:** {icon} {status_str}\n"
        f"• **Details:** `{desc}`\n"
        f"• **Storage:** `data/google_photos_token.json`\n\n"
        "💡 *Use `/gphotos_auth` to re-authorize or connect a different account.*"
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN, reply_to_message_id=update.message.message_id)


async def teramark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manually marks video(s) as already transferred to Google Photos, or triggers an API sync.
    Usage:
      /teramark 1-50          (marks videos #1 through #50 from TeraBox account list)
      /teramark 5             (marks video #5 from TeraBox account list)
      /teramark <filename>    (marks specific filename)
      /teramark sync          (fetches app-created items from Google Photos API)
      /teramark clear         (resets local transfer registry)
      /teramark status        (displays count of registered items)
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    if not context.args:
        reg = load_transferred_registry()
        reg_count = len(reg.get("items", {})) + len(reg.get("manual_marked", []))
        await update.message.reply_text(
            "📝 **TeraBox ➔ Google Photos Sync Marker**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• **Registered Transferred Files:** `{reg_count}`\n\n"
            "**Available Commands:**\n"
            "• `/teramark 1-20` — Mark items 1 to 20 as already transferred\n"
            "• `/teramark 5` — Mark item #5 as already transferred\n"
            "• `/teramark video.mp4` — Mark a specific filename\n"
            "• `/teramark sync` — Query Google Photos API to auto-import uploaded items\n"
            "• `/teramark status` — Show registry counts\n"
            "• `/teramark clear` — Reset registry to clean slate",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    arg = " ".join(context.args).strip()

    if arg.lower() == "clear":
        clear_transferred_registry()
        await update.message.reply_text(
            "🗑️ **Transferred Videos Registry has been reset.**",
            reply_to_message_id=update.message.message_id,
        )
        return

    if arg.lower() in ("status", "info"):
        reg = load_transferred_registry()
        cnt_items = len(reg.get("items", {}))
        cnt_manual = len(reg.get("manual_marked", []))
        await update.message.reply_text(
            f"📊 **Transfer Registry Status:**\n"
            f"• API / Auto Recorded Items: `{cnt_items}`\n"
            f"• Manually Marked Items: `{cnt_manual}`\n"
            f"• Total Known Files: `{cnt_items + cnt_manual}`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    if arg.lower() == "sync":
        status_msg = await update.message.reply_text(
            "⏳ **Scanning all library dates from Google Photos API...**\n"
            "Traversing all timeline pages to discover previously transferred videos...",
            reply_to_message_id=update.message.message_id,
        )
        ok, items, err = await fetch_app_created_media_items(max_pages=200)
        if ok:
            await status_msg.edit_text(
                f"✅ **Google Photos Synced!**\n"
                f"Traversed entire library timeline and recorded `{len(items)}` app-created video items into the local registry.\n\n"
                f"👉 Run `/teracheck` to view updated missing queue.",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        else:
            await status_msg.edit_text(
                f"❌ **Sync Failed:**\n`{err}`\n\n"
                f"💡 *Tip: If you see 'Insufficient scopes', run `/gphotos_auth` to re-authorize with read permissions.*",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        return

    # Check for range: e.g. "1-20" or single index "5"
    videos = TERA_ACCOUNT_VIDEOS_CACHE.get(user_id) or []
    if not videos:
        fetch_res = await fetch_account_videos()
        if fetch_res.get("success"):
            videos = fetch_res.get("videos", [])
            TERA_ACCOUNT_VIDEOS_CACHE[user_id] = videos

    import re
    range_match = re.match(r"^(\d+)\s*(?:-|to)\s*(\d+)$", arg, re.IGNORECASE)
    single_num_match = re.match(r"^(\d+)$", arg)

    if (range_match or single_num_match) and videos:
        if range_match:
            start_num = max(1, int(range_match.group(1)))
            end_num = min(len(videos), int(range_match.group(2)))
        else:
            start_num = int(single_num_match.group(1))
            end_num = start_num

        if start_num > len(videos) or start_num > end_num:
            await update.message.reply_text(
                f"❌ Invalid index range `{arg}`. TeraBox account has `{len(videos)}` videos.",
                reply_to_message_id=update.message.message_id,
            )
            return

        to_mark = []
        for i in range(start_num - 1, end_num):
            v = videos[i]
            fn = v.get("filename")
            fs_id = v.get("fs_id")
            if fn:
                to_mark.append(fn)
            if fs_id:
                to_mark.append(str(fs_id))

        mark_as_transferred(to_mark)
        total_items_in_range = end_num - start_num + 1
        await update.message.reply_text(
            f"✅ **Marked {total_items_in_range} videos (#{start_num} to #{end_num}) as transferred!**\n"
            f"Run `/teracheck` to view updated diff.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    # Otherwise mark by string / filename
    marked = mark_as_transferred([arg])
    await update.message.reply_text(
        f"✅ Marked `{arg}` as transferred (`{marked}` new entries added).\n"
        f"Run `/teracheck` to view updated diff.",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


async def setteracookie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Saves raw TeraBox cookie or ndus value into cooky/terabox/cookies.txt.
    Usage: /setteracookie <cookie_content>
    """
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text(DEV_RESTRICTED_MESSAGE)
        return

    raw_text = update.message.text or ""
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "📝 **Usage:** `/setteracookie <cookie_content_or_ndus>`\n\n"
            "💡 *Paste your Netscape cookie format or `ndus=...` string directly.*",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    content = parts[1].strip()
    if save_account_cookie(content):
        # Invalidate cached videos to force re-fetch with new cookie
        TERA_ACCOUNT_VIDEOS_CACHE.pop(user_id, None)
        await update.message.reply_text(
            "✅ **TeraBox Account Cookie Saved!**\n\n"
            "📁 Saved to: `cooky/terabox/cookies.txt`\n"
            "👉 Run `/terafetch` to view your video library.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
    else:
        await update.message.reply_text("❌ Failed to save TeraBox cookie.")


def register_handlers(application):
    """Register all bot command and message handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("gchelp", gchelp_command))
    application.add_handler(CommandHandler("startgc", startgc_command))
    application.add_handler(CommandHandler("stopgc", stopgc_command))
    application.add_handler(CommandHandler("allow", allow_command))
    application.add_handler(CommandHandler("disallow", disallow_command))
    application.add_handler(CommandHandler("revoke", disallow_command))
    application.add_handler(CommandHandler("allowedusers", listusers_command))
    application.add_handler(CommandHandler("listusers", listusers_command))
    application.add_handler(CommandHandler("allowed", listusers_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stat", stats_command))
    application.add_handler(CommandHandler("analytics", stats_command))
    application.add_handler(CommandHandler("mystats", stats_command))
    application.add_handler(CommandHandler("userstats", userstats_command))
    application.add_handler(CommandHandler("terafetch", terafetch_command))
    application.add_handler(CommandHandler("teravideos", terafetch_command))
    application.add_handler(CommandHandler("teracheck", teracheck_command))
    application.add_handler(CommandHandler("checkphotos", teracheck_command))
    application.add_handler(CommandHandler("terasync", teracheck_command))
    application.add_handler(CommandHandler("photoscheck", teracheck_command))
    application.add_handler(CommandHandler("teratransfer", teratransfer_command))
    application.add_handler(CommandHandler("gtransfer", teratransfer_command))
    application.add_handler(CommandHandler("canceltransfer", canceltransfer_command))
    application.add_handler(CommandHandler("skiptransfer", skiptransfer_command))
    application.add_handler(CommandHandler("skip", skiptransfer_command))
    application.add_handler(CommandHandler("gphotos_auth", gphotos_auth_command))
    application.add_handler(CommandHandler("gauth", gphotos_auth_command))
    application.add_handler(CommandHandler("gphotos_code", gphotos_code_command))
    application.add_handler(CommandHandler("gphotos_status", gphotos_status_command))
    application.add_handler(CommandHandler("gphotos_upload", gphotos_upload_command))
    application.add_handler(CommandHandler("gupload", gphotos_upload_command))
    application.add_handler(CommandHandler("gpush", gphotos_upload_command))
    application.add_handler(CommandHandler("photoupload", gphotos_upload_command))
    application.add_handler(CommandHandler("teramark", teramark_command))
    application.add_handler(CommandHandler("photosmark", teramark_command))
    application.add_handler(CommandHandler("setteracookie", setteracookie_command))
    application.add_handler(CommandHandler("dl", download_command))
    application.add_handler(CommandHandler("download", download_command))
    application.add_handler(CommandHandler("d", download_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("killtasks", refresh_command))
    application.add_handler(CommandHandler("reset", refresh_command))
    application.add_handler(CommandHandler("setcookie", setcookie_command))
    application.add_handler(CommandHandler("clearcookie", clearcookie_command))
    application.add_handler(CommandHandler("cookiestatus", cookiestatus_command))
    application.add_handler(CommandHandler("setdiskwala", setdiskwala_command))
    application.add_handler(CommandHandler("diskwalastatus", diskwalastatus_command))
    application.add_handler(CommandHandler("setterabox", setterabox_command))
    application.add_handler(CommandHandler("teraboxstatus", teraboxstatus_command))
    application.add_handler(CommandHandler("ezdisk", ezdisk_command))
    application.add_handler(CommandHandler("eztera", eztera_command))
    application.add_handler(CommandHandler("vnc", vnc_command))
    application.add_handler(CommandHandler("autovnc", autovnc_command))
    application.add_handler(CommandHandler("stopvnc", stopvnc_command))
    application.add_handler(CommandHandler("killvnc", stopvnc_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, handle_document))
    application.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))
