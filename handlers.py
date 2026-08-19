import re
import os
import time
import logging
import asyncio
from typing import Dict, Any
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


def generate_cache_key(user_id: int) -> str:
    return f"{user_id}_{int(time.time() * 1000) % 1000000}"


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects URLs in user messages and displays download options."""
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
    platform = get_platform_badge(url)

    status_msg = await update.message.reply_text(
        f"🔍 **Analyzing link...**\n`{url}`",
        parse_mode=constants.ParseMode.MARKDOWN
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

    title = info.get("title", "Untitled Video")
    duration = format_duration(info.get("duration"))
    uploader = info.get("uploader", "Unknown Author")
    thumbnail = info.get("thumbnail")

    # Store URL and info in cache
    cache_key = generate_cache_key(user_id)
    URL_CACHE[cache_key] = {
        "url": url,
        "title": title,
        "duration": duration,
        "uploader": uploader,
        "duration_sec": info.get("duration"),
    }

    # Clean old cache entries (keep last 50)
    if len(URL_CACHE) > 50:
        for k in list(URL_CACHE.keys())[:-50]:
            URL_CACHE.pop(k, None)

    # Build Quality Selection Keyboard
    keyboard = [
        [
            InlineKeyboardButton("⚡ Best Quality (<50MB)", callback_data=f"dl:best:{cache_key}"),
        ],
        [
            InlineKeyboardButton("🎬 720p", callback_data=f"dl:720:{cache_key}"),
            InlineKeyboardButton("📺 480p", callback_data=f"dl:480:{cache_key}"),
            InlineKeyboardButton("📱 360p", callback_data=f"dl:360:{cache_key}"),
        ],
        [
            InlineKeyboardButton("🎵 Audio Only (MP3)", callback_data=f"dl:audio:{cache_key}"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{cache_key}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    caption = (
        f"{platform}\n"
        f"📌 **{title}**\n\n"
        f"👤 **Author:** {uploader}\n"
        f"⏱ **Duration:** {duration}\n\n"
        f"👇 *Choose quality to download:*"
    )

    try:
        if thumbnail:
            await status_msg.delete()
            await update.message.reply_photo(
                photo=thumbnail,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN
            )
        else:
            await status_msg.edit_text(
                caption,
                reply_markup=reply_markup,
                parse_mode=constants.ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.warning(f"Could not send thumbnail, falling back to text: {e}")
        await status_msg.edit_text(
            caption,
            reply_markup=reply_markup,
            parse_mode=constants.ParseMode.MARKDOWN
        )


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles quality selection button clicks and downloads media."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "cancel":
        cache_key = parts[1] if len(parts) > 1 else ""
        URL_CACHE.pop(cache_key, None)
        await query.edit_message_caption(caption="❌ Download cancelled.") if query.message.photo else await query.edit_message_text(text="❌ Download cancelled.")
        return

    if action != "dl" or len(parts) < 3:
        return

    quality = parts[1]
    cache_key = parts[2]

    cached_data = URL_CACHE.get(cache_key)
    if not cached_data:
        msg = "⚠️ This download request has expired. Please send the link again."
        if query.message.photo:
            await query.edit_message_caption(caption=msg)
        else:
            await query.edit_message_text(text=msg)
        return

    url = cached_data["url"]
    title = cached_data["title"]
    uploader = cached_data.get("uploader", "")
    duration_sec = cached_data.get("duration_sec")
    quality_label = QUALITIES.get(quality, quality)

    # Update message status to downloading
    status_text = f"⏳ **Downloading [{quality_label}]...**\n📌 *{title}*\n\nPlease wait..."
    if query.message.photo:
        await query.edit_message_caption(caption=status_text, parse_mode=constants.ParseMode.MARKDOWN)
    else:
        await query.edit_message_text(text=status_text, parse_mode=constants.ParseMode.MARKDOWN)

    # Perform download with concurrency semaphore to safeguard CPU & RAM
    downloaded_file = None
    try:
        async with get_semaphore():
            success, downloaded_file, info, error_msg = await download_media(url, quality=quality)

            if not success or not downloaded_file or not os.path.exists(downloaded_file):
                err = error_msg or "Failed to download media."
                error_response = f"❌ **Download failed:**\n`{err[:200]}`"
                if query.message.photo:
                    await query.edit_message_caption(caption=error_response, parse_mode=constants.ParseMode.MARKDOWN)
                else:
                    await query.edit_message_text(text=error_response, parse_mode=constants.ParseMode.MARKDOWN)
                return

            # Check file size
            file_size = os.path.getsize(downloaded_file)
            if file_size > MAX_FILE_SIZE_BYTES:
                size_str = format_bytes(file_size)
                warning_msg = (
                    f"⚠️ **File Too Large!**\n\n"
                    f"Downloaded file size is **{size_str}**, which exceeds Telegram's **{MAX_FILE_SIZE_MB}MB** limit.\n\n"
                    "💡 **Suggestion:** Try downloading in a lower resolution (e.g. 480p or 360p) or Audio Only (MP3)."
                )
                if query.message.photo:
                    await query.edit_message_caption(caption=warning_msg, parse_mode=constants.ParseMode.MARKDOWN)
                else:
                    await query.edit_message_text(text=warning_msg, parse_mode=constants.ParseMode.MARKDOWN)
                return

            # Update status to uploading
            upload_status = f"📤 **Uploading {format_bytes(file_size)} to Telegram...**"
            if query.message.photo:
                await query.edit_message_caption(caption=upload_status, parse_mode=constants.ParseMode.MARKDOWN)
            else:
                await query.edit_message_text(text=upload_status, parse_mode=constants.ParseMode.MARKDOWN)

            # Send media file
            chat_id = update.effective_chat.id
            platform_badge = get_platform_badge(url)

            if quality == "audio":
                with open(downloaded_file, "rb") as audio_file:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=title,
                        performer=uploader,
                        duration=duration_sec,
                        caption=f"🎵 **{title}**\n{platform_badge}",
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
            else:
                width = info.get("width") if info else None
                height = info.get("height") if info else None
                with open(downloaded_file, "rb") as video_file:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        caption=f"🎬 **{title}**\n{platform_badge}",
                        duration=duration_sec,
                        width=width,
                        height=height,
                        supports_streaming=True,
                        parse_mode=constants.ParseMode.MARKDOWN
                    )

            # Delete status message on success
            try:
                await query.message.delete()
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Error during download or upload: {e}", exc_info=True)
        err_msg = f"❌ An error occurred: {str(e)[:150]}"
        try:
            if query.message.photo:
                await query.edit_message_caption(caption=err_msg)
            else:
                await query.edit_message_text(text=err_msg)
        except Exception:
            pass
    finally:
        # Always remove temporary file from disk
        if downloaded_file:
            remove_file_safely(downloaded_file)
        URL_CACHE.pop(cache_key, None)


def register_handlers(application):
    """Register all bot command and message handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
