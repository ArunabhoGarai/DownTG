import os
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, Any, Tuple

from config import (
    BOT_TOKEN,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    IS_MTPROTO_ENABLED,
    BASE_DIR,
)

logger = logging.getLogger(__name__)

_CLIENT = None
_IS_RUNNING = False


def get_mtproto_client():
    """Initializes the Pyrogram MTProto Client instance lazily."""
    global _CLIENT
    if not IS_MTPROTO_ENABLED:
        return None

    if _CLIENT is None:
        try:
            # Check for native C crypto acceleration
            try:
                import tgcrypto
                logger.info("⚡ Native C crypto (TgCrypto) detected: High-speed uploads active!")
            except ImportError:
                logger.warning(
                    "⚠️ TgCrypto C-extension not found. Using pure Python fallback. "
                    "For 5x-10x faster MTProto uploads on EC2, run: pip install tgcrypto"
                )

            from pyrogram import Client
            _CLIENT = Client(
                name="tgbot_mtproto",
                api_id=int(TELEGRAM_API_ID),
                api_hash=TELEGRAM_API_HASH,
                bot_token=BOT_TOKEN,
                workdir=str(BASE_DIR),
                no_updates=True,
                workers=8,
            )
            logger.info("Pyrogram MTProto Client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Pyrogram MTProto Client: {e}", exc_info=True)
            _CLIENT = None

    return _CLIENT


async def start_mtproto():
    """Starts the MTProto Client session if enabled."""
    global _IS_RUNNING
    if not IS_MTPROTO_ENABLED:
        logger.info("MTProto credentials not set. Running in standard 50MB HTTP Bot API mode.")
        return False

    client = get_mtproto_client()
    if client and not _IS_RUNNING:
        try:
            await client.start()
            _IS_RUNNING = True
            logger.info("🚀 MTProto Client started! 2GB file uploads are now ACTIVE.")
            return True
        except Exception as e:
            logger.error(f"Failed to start MTProto Client: {e}", exc_info=True)
            _IS_RUNNING = False
            return False
    return _IS_RUNNING


async def stop_mtproto():
    """Stops the MTProto Client session cleanly on shutdown."""
    global _IS_RUNNING, _CLIENT
    if _CLIENT and _IS_RUNNING:
        try:
            await _CLIENT.stop()
            _IS_RUNNING = False
            logger.info("MTProto Client stopped cleanly.")
        except Exception as e:
            logger.warning(f"Error stopping MTProto Client: {e}")


def is_mtproto_active() -> bool:
    """Returns True if MTProto 2GB uploads are active and ready."""
    return _IS_RUNNING and _CLIENT is not None


async def upload_media_mtproto(
    chat_id: int,
    file_path: str,
    title: str,
    caption: str,
    duration_sec: Optional[int] = None,
    thumbnail_path: Optional[str] = None,
    is_audio: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Uploads media files up to 2,000 MB (2 GB) directly to Telegram via MTProto.
    Returns (success, error_message).
    """
    if not IS_MTPROTO_ENABLED:
        return False, "MTProto API credentials (TELEGRAM_API_ID & TELEGRAM_API_HASH) are missing in .env."

    if not is_mtproto_active():
        started = await start_mtproto()
        if not started:
            return False, "Failed to start MTProto client session."

    try:
        # Prepare progress throttle wrapper (updates Telegram message at most once per 4.0 seconds)
        last_update_time = 0
        last_percent = -1

        async def _pyro_progress(current: int, total: int, *args):
            nonlocal last_update_time, last_percent
            now = time.time()
            current_pct = int((current / total) * 100) if total > 0 else 0
            if progress_callback and (
                (now - last_update_time >= 4.0 and current_pct >= last_percent + 4)
                or current >= total
            ):
                last_update_time = now
                last_percent = current_pct
                try:
                    res = progress_callback(current, total)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

        # Validate thumbnail
        valid_thumb = None
        if thumbnail_path and os.path.exists(thumbnail_path):
            valid_thumb = thumbnail_path

        # Truncate caption to Telegram limit (1024 chars)
        safe_caption = caption[:1020] if caption else ""

        try:
            from pyrogram.enums import ParseMode
            pyro_parse_mode = ParseMode.MARKDOWN
        except Exception:
            pyro_parse_mode = None

        if is_audio:
            try:
                await _CLIENT.send_audio(
                    chat_id=chat_id,
                    audio=file_path,
                    title=title,
                    caption=safe_caption,
                    duration=int(duration_sec) if duration_sec else None,
                    thumb=valid_thumb,
                    parse_mode=pyro_parse_mode,
                    progress=_pyro_progress,
                )
            except Exception as e_inner:
                logger.warning(f"Retrying audio upload without markdown parse_mode: {e_inner}")
                await _CLIENT.send_audio(
                    chat_id=chat_id,
                    audio=file_path,
                    title=title,
                    caption=safe_caption,
                    duration=int(duration_sec) if duration_sec else None,
                    thumb=valid_thumb,
                    parse_mode=None,
                    progress=_pyro_progress,
                )
        else:
            try:
                await _CLIENT.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=safe_caption,
                    duration=int(duration_sec) if duration_sec else None,
                    thumb=valid_thumb,
                    supports_streaming=True,
                    parse_mode=pyro_parse_mode,
                    progress=_pyro_progress,
                )
            except Exception as e_inner:
                logger.warning(f"Video upload failed ({e_inner}). Retrying as generic document...")
                try:
                    await _CLIENT.send_document(
                        chat_id=chat_id,
                        document=file_path,
                        caption=safe_caption,
                        thumb=valid_thumb,
                        parse_mode=None,
                        progress=_pyro_progress,
                    )
                except Exception as e_doc:
                    logger.error(f"Document upload fallback failed: {e_doc}")
                    return False, str(e_doc)

        return True, None

    except Exception as e:
        logger.error(f"MTProto upload failed for {file_path}: {e}", exc_info=True)
        return False, str(e)
