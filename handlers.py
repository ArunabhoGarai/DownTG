import re
import os
import time
import logging
import asyncio
from typing import Dict, Any, List, Optional
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

logger = logging.getLogger(__name__)

# Concurrency semaphore (initialized lazily)
_SEMAPHORE: asyncio.Semaphore = None


def get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _SEMAPHORE

# URL matching regex
URL_REGEX = re.compile(
    r'(https?://(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?://[a-zA-Z0-9]+\.[^\s]{2,})',
    re.IGNORECASE
)

# Global short cache for callback query payloads to keep callback_data under 64 bytes
URL_CACHE: Dict[str, Dict[str, Any]] = {}
MAX_FORMAT_CHOICES = 8


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
    """Edit a callback message whether it is a photo caption or plain text."""
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    user = update.effective_user
    welcome_text = (
        f"👋 **Hello, {user.first_name}!**\n\n"
        "I am your **Universal Media Downloader Bot** 🚀\n\n"
        "**Supported Platforms:**\n"
        "• 🔴 **YouTube**: Standard Videos, Shorts & Audio\n"
        "• 📸 **Instagram**: Reels, Posts & Stories\n"
        "• 🔵 **Facebook**: Videos & Reels\n"
        "• 🎵 **TikTok & 🐦 Twitter/X**\n\n"
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
        "1. Copy a video/reel/shorts link from YouTube, Instagram, or Facebook.\n"
        "2. Paste and send the link here.\n"
        "3. Select your desired quality from the interactive buttons.\n"
        "4. The bot will download and send the media right back to you!\n\n"
        "⚠️ **Note on File Limits:**\n"
        f"• Telegram standard bot limit is **{MAX_FILE_SIZE_MB}MB** per file.\n"
        "• For large YouTube videos, choose 720p, 480p, 360p, or Audio Only (MP3)."
    )
    await update.message.reply_text(
        help_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )


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
):
    """Sends audio or video to chat with metadata caption."""
    platform_badge = get_platform_badge(url)
    if quality == "audio":
        with open(file_path, "rb") as audio_file:
            await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=title,
                performer=uploader,
                duration=duration_sec,
                caption=f"🎵 **{title}**\n{platform_badge}",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
    else:
        width = info.get("width") if info else None
        height = info.get("height") if info else None
        with open(file_path, "rb") as video_file:
            await bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=f"🎬 **{title}**\n{platform_badge}",
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

    status_msg = await update.message.reply_text(
        f"🔍 **Analyzing link...**\n`{url}`",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    # Extract metadata without downloading
    success, info, error_msg = await extract_media_info(url)

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

    raw_title = str(info.get("title") or info.get("description") or "Untitled Media").strip()
    first_line = raw_title.split("\n")[0].strip()
    title = (first_line[:75] + "...") if len(first_line) > 75 else (first_line or "Media")
    duration = format_duration(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown"
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

    try:
        async with get_semaphore():
            success, downloaded_file, dl_info, error_msg = await download_media(url, quality="480")

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

    # Update message status to downloading
    status_text = f"⏳ **Downloading [{quality_label}]...**\n📌 *{title}*\n\nPlease wait..."
    await edit_query_message(query, status_text)

    # Perform download with concurrency semaphore to safeguard CPU & RAM
    downloaded_file = None
    completed = False
    try:
        async with get_semaphore():
            success, downloaded_file, info, error_msg = await download_media(
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
        # Always remove temporary file from disk
        if downloaded_file:
            remove_file_safely(downloaded_file)
        if completed:
            URL_CACHE.pop(cache_key, None)


def register_handlers(application):
    """Register all bot command and message handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
