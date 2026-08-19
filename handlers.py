import re
import os
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

from config import QUALITIES, MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB, MAX_CONCURRENT_DOWNLOADS
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
)
from mtproto_uploader import (
    is_mtproto_active,
    upload_media_mtproto,
)

logger = logging.getLogger(__name__)

# Concurrency semaphore (initialized lazily)
_SEMAPHORE: asyncio.Semaphore = None
ACTIVE_TASKS: Dict[str, asyncio.Task] = {}


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


async def route_extract_info(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Routes metadata extraction to the dedicated platform downloader."""
    if is_terabox_url(url):
        return await extract_terabox_info(url)
    elif is_youtube_url(url):
        return await extract_media_info(url)
    elif is_instagram_url(url):
        return await extract_instagram_info(url)
    elif is_facebook_url(url):
        return await extract_facebook_info(url)
    else:
        return await extract_generic_info(url)


async def route_download_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """Routes media download to the dedicated platform downloader."""
    if is_terabox_url(url):
        return await download_terabox_media(url, quality=quality, format_selector=format_selector)
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    welcome_text = (
        f"👋 **Hello, {user.first_name}!**\n\n"
        "I am your **Universal Media Downloader Bot** 🚀\n\n"
        "**Supported Sources (Any Non-DRM Video):**\n"
        "• 🔴 **YouTube**: Videos, Shorts & MP3\n"
        "• 📸 **Instagram**: Reels, Posts & Stories\n"
        "• 🔵 **Facebook**: Videos & Reels\n"
        "• 📦 **TeraBox**: Cloud videos & shares\n"
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
    """Handles the /help command."""
    help_text = (
        "📖 **How to Download Media:**\n\n"
        "1. Copy a video link from **YouTube**, **Instagram**, **Facebook**, **TikTok**, **Twitter/X**, **Reddit**, or any non-DRM site.\n"
        "2. Paste and send the link here.\n"
        "3. The bot will automatically download it in 480p (or show quality options if needed) and send the video directly to you!\n\n"
        "⚠️ **Note on File Limits:**\n"
        f"• Telegram standard bot limit is **{MAX_FILE_SIZE_MB}MB** per file.\n"
        "• DRM-protected services (like Netflix, Prime, Disney+) cannot be downloaded."
    )
    await update.message.reply_text(
        help_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )


async def edit_status_msg_safe(msg, text: str):
    """Safely edit status text without crashing on transient HTTP transport errors."""
    if not msg:
        return
    try:
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

    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

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

        mtproto_success = await upload_media_mtproto(
            chat_id=chat_id,
            file_path=file_path,
            title=title,
            caption=caption,
            duration_sec=duration_sec,
            thumbnail_path=None,
            is_audio=is_audio,
            progress_callback=_mtproto_progress,
        )
        if mtproto_success:
            return
        elif file_size > 50 * 1024 * 1024:
            logger.error(f"MTProto upload failed for large file ({format_bytes(file_size)}). Cannot fallback to 50MB HTTP Bot API.")
            raise RuntimeError(f"MTProto upload failed for {format_bytes(file_size)} file.")

    # 2. Standard HTTP Bot API upload (for files <= 50MB)
    if is_audio:
        with open(file_path, "rb") as audio_file:
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
        width = info.get("width") if info else None
        height = info.get("height") if info else None
        with open(file_path, "rb") as video_file:
            await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                duration=duration_sec,
                width=width,
                height=height,
                supports_streaming=True,
                parse_mode=constants.ParseMode.MARKDOWN,
            )


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects URLs, attempts 480p default download, and falls back to quality panel on failure."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    match = URL_REGEX.search(text)
    if not match:
        await update.message.reply_text(
            "❌ No valid link detected. Please send a valid YouTube, Facebook, or Instagram video link."
        )
        return

    url = match.group(0)
    user_id = update.effective_user.id

    # Check concurrent download capacity before analyzing link
    if get_active_downloads_count() >= MAX_CONCURRENT_DOWNLOADS:
        await update.message.reply_text(
            f"⚠️ **Server Busy:** `{MAX_CONCURRENT_DOWNLOADS}` downloads are already in progress. Please try again after some time.",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_to_message_id=update.message.message_id,
        )
        return

    status_msg = await update.message.reply_text(
        f"🔍 **Analyzing link...**\n`{url}`",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )

    # Extract metadata without downloading (routed to dedicated engine)
    success, info, error_msg = await route_extract_info(url)

    if not success or not info:
        err = error_msg or "Unable to retrieve video information."
        if "Private video" in err or "login" in err.lower():
            err_text = "🔒 This video is private or requires login."
        elif "not found" in err.lower():
            err_text = "❌ Video not found or has been deleted."
        else:
            err_text = f"❌ Failed to fetch video:\n`{err[:150]}`"

        await status_msg.edit_text(err_text, parse_mode=constants.ParseMode.MARKDOWN)
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
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    except Exception:
        pass

    downloaded_file = None
    default_succeeded = False
    fail_reason = None
    task_id = f"{user_id}_{time.time()}"

    try:
        ACTIVE_TASKS[task_id] = asyncio.current_task()
        async with get_semaphore():
            success, downloaded_file, dl_info, error_msg = await route_download_media(url, quality="480")

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
                    URL_CACHE.pop(cache_key, None)
                else:
                    fail_reason = f"480p file is {format_bytes(file_size)}, exceeding Telegram's {MAX_FILE_SIZE_MB}MB limit"
            else:
                fail_reason = error_msg or "480p stream could not be downloaded"
    except Exception as e:
        logger.error(f"Error during default 480p download: {e}", exc_info=True)
        fail_reason = str(e)[:100]
    finally:
        ACTIVE_TASKS.pop(task_id, None)
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
    """Handles quality selection button clicks and deletes the panel when finished."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

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


from config import (
    QUALITIES,
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    MAX_CONCURRENT_DOWNLOADS,
    ADMIN_USER_ID,
    BASE_DIR,
)


def is_admin(user_id: int) -> bool:
    """Check if the user is authorized to manage cookies."""
    if not ADMIN_USER_ID:
        return True  # If not configured, allow bot owner
    return str(user_id) == str(ADMIN_USER_ID)


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
        await update.message.reply_text("⛔ You are not authorized to use this command.")
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
        await update.message.reply_text("⛔ You are not authorized to use this command.")
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
        await update.message.reply_text("⛔ You are not authorized to use this command.")
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
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return

    killed_count = 0
    for tid, task in list(ACTIVE_TASKS.items()):
        if not task.done():
            task.cancel()
            killed_count += 1
    ACTIVE_TASKS.clear()
    URL_CACHE.clear()

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
        f"🔄 **Server Reset & Refreshed!**\n\n"
        f"• Terminated active tasks: `{killed_count}`\n"
        f"• Cleaned temporary files: `{cleaned_files}`\n"
        f"• Concurrency slots available: `{MAX_CONCURRENT_DOWNLOADS}/{MAX_CONCURRENT_DOWNLOADS}`\n"
        f"• State: 🟢 **Ready for new downloads**",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_to_message_id=update.message.message_id,
    )


def register_handlers(application):
    """Register all bot command and message handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("killtasks", refresh_command))
    application.add_handler(CommandHandler("reset", refresh_command))
    application.add_handler(CommandHandler("setcookie", setcookie_command))
    application.add_handler(CommandHandler("clearcookie", clearcookie_command))
    application.add_handler(CommandHandler("cookiestatus", cookiestatus_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
