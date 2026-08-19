import os
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, Any

from config import (
    BOT_TOKEN,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    IS_MTPROTO_ENABLED,
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
            from pyrogram import Client
            _CLIENT = Client(
                name="tgbot_mtproto",
                api_id=int(TELEGRAM_API_ID),
                api_hash=TELEGRAM_API_HASH,
                bot_token=BOT_TOKEN,
                in_memory=True,
                no_updates=True,
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
) -> bool:
    """
    Uploads media files up to 2,000 MB (2 GB) directly to Telegram via MTProto.
    Returns True on success.
    """
    if not is_mtproto_active():
        # Try to start on-demand if not already started
        started = await start_mtproto()
        if not started:
            return False

    try:
        # Prepare progress throttle wrapper (updates Telegram message at most once per 2.5 seconds)
        last_update_time = 0

        async def _pyro_progress(current: int, total: int):
            nonlocal last_update_time
            now = time.time()
            if progress_callback and (now - last_update_time >= 2.5 or current >= total):
                last_update_time = now
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

        if is_audio:
            await _CLIENT.send_audio(
                chat_id=chat_id,
                audio=file_path,
                title=title,
                caption=caption,
                duration=int(duration_sec) if duration_sec else None,
                thumb=valid_thumb,
                progress=_pyro_progress,
            )
        else:
            await _CLIENT.send_video(
                chat_id=chat_id,
                video=file_path,
                caption=caption,
                duration=int(duration_sec) if duration_sec else None,
                thumb=valid_thumb,
                supports_streaming=True,
                progress=_pyro_progress,
            )

        return True

    except Exception as e:
        logger.error(f"MTProto upload failed for {file_path}: {e}", exc_info=True)
        return False
