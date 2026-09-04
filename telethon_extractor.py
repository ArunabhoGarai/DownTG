"""
Telethon MTProto MiniApp Bearer Extractor for DiskWala and TeraBox.

Uses native Telegram MTProto (RequestWebView / RequestMainWebView) to request
the cryptographic signed WebApp launch URL directly from Telegram Data Centers,
extracting fresh Bearer tokens in ~0.3s without Chromium, VNC, or UI automation.
"""

import os
import re
import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

from config import (
    BASE_DIR,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    TOKEN_REFRESH_INTERVAL_HOURS,
)

logger = logging.getLogger(__name__)

# Fallback official Telegram Desktop credentials if not set in .env
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"

# Default session file name in project directory
DEFAULT_SESSION_NAME = "terabox_user_session"

# Target Bot Configurations
BOT_CONFIGS = {
    "diskwala": {
        "peer_id": 7802633228,
        "username": "sky577bot",
        "title": "DiskWala",
        "menu_url": "https://miniapp.diskwala.net/",
        "short_names": ["app", "main", "start"],
    },
    "terabox": {
        "peer_id": 7802009139,
        "username": "terabox_downloader_new_bot",
        "title": "TeraBox",
        "menu_url": "https://twa.teradownloader.pro/",
        "short_names": ["app", "main", "start"],
    },
}


def get_api_credentials() -> Tuple[int, str]:
    """Resolves Telegram API ID and API Hash with fallback to official desktop client."""
    api_id = TELEGRAM_API_ID
    api_hash = TELEGRAM_API_HASH

    if api_id and str(api_id).strip().isdigit() and api_hash and str(api_hash).strip():
        return int(str(api_id).strip()), str(api_hash).strip()

    return DEFAULT_API_ID, DEFAULT_API_HASH


def get_session_path(session_name: str = DEFAULT_SESSION_NAME) -> Path:
    """Returns the absolute path to the Telethon session file."""
    # Check if session exists with or without .session extension
    candidate = BASE_DIR / session_name
    if candidate.with_suffix(".session").exists():
        return candidate
    return candidate


ENV_FILE = BASE_DIR / ".env"


def save_token_to_env(key: str, token: str) -> bool:
    """Safely updates or inserts a token into the .env file and os.environ."""
    clean_val = token.strip()
    full_token = clean_val if clean_val.startswith("Bearer ") else f"Bearer {clean_val}"
    os.environ[key] = full_token

    try:
        lines = []
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={full_token}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{key}={full_token}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        logger.error(f"[TelethonExtractor] Failed to write {key} to .env: {e}")
        return False


def extract_init_data(signed_url: str) -> str:
    """
    Extracts the exact tgWebAppData string from the signed URL fragment.
    Example:
    https://miniapp.diskwala.net/#tgWebAppData=user%3D%7B...%7D&chat_instance=...&auth_date=...&tgWebAppVersion=9.6
    """
    if not signed_url or "#" not in signed_url:
        return ""

    fragment = signed_url.split("#", 1)[1]

    if "tgWebAppData=" in fragment:
        raw_part = fragment.split("tgWebAppData=", 1)[1]
        chunks = []
        for chunk in raw_part.split("&"):
            if chunk.startswith("tgWebApp"):
                break
            chunks.append(chunk)
        init_data = "&".join(chunks)
        # If double URL encoded (starts with user%3D or query_id%3D), unquote once
        if init_data.startswith("user%3D") or init_data.startswith("query_id%3D"):
            init_data = urllib.parse.unquote(init_data)
        return init_data

    if "user=" in fragment or "query_id=" in fragment:
        chunks = []
        for chunk in fragment.split("&"):
            if chunk.startswith("tgWebApp"):
                break
            chunks.append(chunk)
        return "&".join(chunks)

    return ""


async def fetch_telethon_bearer(
    platform: str,
    session_name: str = DEFAULT_SESSION_NAME,
) -> Tuple[bool, str, str]:
    """
    Connects to Telegram via Telethon using the local user session, queries the target
    bot's MiniApp signed URL, formats the Bearer token, and saves it to .env.

    Returns:
        (success: bool, bearer_token: str, error_message: str)
    """
    try:
        from telethon import TelegramClient, functions, types
    except ImportError:
        err = "Telethon is not installed. Please run: pip install telethon"
        logger.error(f"[TelethonExtractor] {err}")
        return False, "", err

    plat_key = platform.lower().strip()
    if plat_key in ("dw", "diskwala"):
        cfg = BOT_CONFIGS["diskwala"]
        save_fn_name = "save_diskwala_token"
    elif plat_key in ("tera", "terabox", "tb"):
        cfg = BOT_CONFIGS["terabox"]
        save_fn_name = "save_terabox_token"
    else:
        return False, "", f"Unknown platform: '{platform}'. Must be 'diskwala' or 'terabox'."

    session_file = BASE_DIR / f"{session_name}.session"
    if not session_file.exists():
        err = (
            f"Telethon session file not found: `{session_file.name}`.\n"
            "Please initialize the user session on the server once using `test_telethon_miniapp.py`."
        )
        logger.error(f"[TelethonExtractor] {err}")
        return False, "", err

    api_id, api_hash = get_api_credentials()
    session_path = str(BASE_DIR / session_name)

    client = TelegramClient(session_path, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            err = (
                "Telethon session is not authorized or has expired.\n"
                "Please run `python test_telethon_miniapp.py` in the terminal to re-authenticate."
            )
            logger.error(f"[TelethonExtractor] {err}")
            return False, "", err

        # Resolve bot entity
        bot_entity = None
        target_peer = cfg["peer_id"]
        target_username = cfg["username"]

        try:
            bot_entity = await client.get_input_entity(target_peer)
        except Exception as e:
            logger.warning(f"[TelethonExtractor] Could not resolve peer ID {target_peer}: {e}. Trying username...")
            try:
                bot_entity = await client.get_input_entity(target_username)
            except Exception as e2:
                err = f"Could not resolve bot entity for {cfg['title']}: {e2}"
                logger.error(f"[TelethonExtractor] {err}")
                return False, "", err

        signed_url = None

        # ── Strategy 1: Bot Menu Button (from_bot_menu=True) ──
        menu_url = cfg["menu_url"]
        for plat in ["weba", "android"]:
            try:
                logger.info(f"[TelethonExtractor] Trying RequestWebViewRequest (from_bot_menu=True, {plat})...")
                res = await client(functions.messages.RequestWebViewRequest(
                    peer=bot_entity,
                    bot=bot_entity,
                    platform=plat,
                    url=menu_url,
                    from_bot_menu=True,
                ))
                if getattr(res, "url", None):
                    signed_url = res.url
                    logger.info(f"[TelethonExtractor] Success via Menu Button ({plat})!")
                    break
            except Exception as e:
                logger.debug(f"[TelethonExtractor] Menu button notice ({plat}): {e}")

        # ── Strategy 2: Main Web App (RequestMainWebViewRequest) ──
        if not signed_url:
            for plat in ["android", "weba"]:
                try:
                    logger.info(f"[TelethonExtractor] Trying RequestMainWebViewRequest ({plat})...")
                    res = await client(functions.messages.RequestMainWebViewRequest(
                        peer=bot_entity,
                        bot=bot_entity,
                        platform=plat,
                    ))
                    if getattr(res, "url", None):
                        signed_url = res.url
                        logger.info(f"[TelethonExtractor] Success via Main Web App ({plat})!")
                        break
                except Exception as e:
                    logger.debug(f"[TelethonExtractor] Main web view notice ({plat}): {e}")

        # ── Strategy 3: Chat Inline Buttons Inspection ──
        if not signed_url:
            try:
                logger.info(f"[TelethonExtractor] Interrogating bot messages for WebApp buttons...")
                messages = await client.get_messages(bot_entity, limit=4)
                for msg in messages:
                    if not msg.reply_markup:
                        continue
                    rows = getattr(msg.reply_markup, "rows", [])
                    for row in rows:
                        for btn in getattr(row, "buttons", []):
                            if isinstance(btn, (types.KeyboardButtonWebView, types.KeyboardButtonSimpleWebView)) or hasattr(btn, "url"):
                                btn_url = getattr(btn, "url", menu_url)
                                if "http" in str(btn_url):
                                    try:
                                        res = await client(functions.messages.RequestWebViewRequest(
                                            peer=bot_entity,
                                            bot=bot_entity,
                                            platform="weba",
                                            url=btn_url,
                                            reply_to=types.InputReplyToMessage(reply_to_msg_id=msg.id),
                                        ))
                                        if getattr(res, "url", None):
                                            signed_url = res.url
                                            logger.info("[TelethonExtractor] Success via Inline Keyboard Button!")
                                            break
                                    except Exception:
                                        pass
                        if signed_url:
                            break
                    if signed_url:
                        break
            except Exception as e:
                logger.debug(f"[TelethonExtractor] Inline button notice: {e}")

        # ── Strategy 4: App ShortNames (RequestAppWebViewRequest) ──
        if not signed_url:
            for sn in cfg["short_names"]:
                for plat in ["android", "weba"]:
                    try:
                        logger.info(f"[TelethonExtractor] Trying RequestAppWebViewRequest (short_name='{sn}', {plat})...")
                        res = await client(functions.messages.RequestAppWebViewRequest(
                            peer=bot_entity,
                            app=types.InputBotAppShortName(bot_id=bot_entity, short_name=sn),
                            platform=plat,
                            write_allowed=True,
                        ))
                        if getattr(res, "url", None):
                            signed_url = res.url
                            logger.info(f"[TelethonExtractor] Success via RequestAppWebViewRequest ({sn})!")
                            break
                    except Exception as e:
                        logger.debug(f"[TelethonExtractor] Short name notice ({sn}): {e}")
                if signed_url:
                    break

        if not signed_url:
            return False, "", f"All MTProto WebApp queries exhausted without a signed URL for {cfg['title']}."

        # Parse tgWebAppData / Bearer token
        init_data = extract_init_data(signed_url)
        if not init_data:
            return False, "", f"Found signed URL but could not parse tgWebAppData: {signed_url[:90]}..."

        bearer_token = f"Bearer {init_data}"

        # Persist token to disk & memory
        env_key = "DISKWALA_BEARER_TOKEN" if plat_key in ("dw", "diskwala") else "TERABOX_BEARER_TOKEN"
        save_token_to_env(env_key, bearer_token)

        # Also trigger downloader module sync if available
        try:
            if plat_key in ("dw", "diskwala"):
                from diskwala_downloader import save_diskwala_token
                save_diskwala_token(bearer_token)
            else:
                from terabox_downloader import save_terabox_token
                save_terabox_token(bearer_token)
        except Exception:
            pass

        logger.info(f"[TelethonExtractor] Successfully saved {env_key} to .env and memory.")
        return True, bearer_token, ""

    except Exception as e:
        logger.exception(f"[TelethonExtractor] Unexpected error: {e}")
        return False, "", str(e)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ── On-Demand Reactive Token Verifier & Refresh Locks ──
_refresh_locks = {
    "diskwala": asyncio.Lock(),
    "terabox": asyncio.Lock(),
}


def is_token_expired(token: str, max_age_hours: Optional[float] = None) -> bool:
    """
    Checks if a token is empty or older than max_age_hours by inspecting the
    cryptographic 'auth_date=...' Unix timestamp inside the Telegram signed data.
    """
    if not token or not str(token).strip():
        return True

    from config import TOKEN_MAX_AGE_HOURS
    age_hours = TOKEN_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    max_age_seconds = max(300, int(age_hours * 3600))

    match = re.search(r"auth_date=(\d+)", str(token))
    if not match:
        return False  # No explicit timestamp found, assume valid until HTTP 401

    import time
    auth_ts = int(match.group(1))
    current_ts = int(time.time())
    age = current_ts - auth_ts
    return age > max_age_seconds


async def ensure_valid_token(
    platform: str,
    force_refresh: bool = False,
    max_age_hours: Optional[float] = None,
) -> Tuple[bool, str, str]:
    """
    On-demand token verification and refresh:
    - If token is missing, expired (older than max_age_hours), or force_refresh is True:
      Calls Telethon MTProto to fetch a fresh token immediately.
    - Uses asyncio.Lock per platform so concurrent downloads do not trigger duplicate queries.
    """
    plat_key = "diskwala" if platform.lower() in ("dw", "diskwala") else "terabox"
    lock = _refresh_locks.get(plat_key, _refresh_locks["terabox"])

    async with lock:
        env_key = "DISKWALA_BEARER_TOKEN" if plat_key == "diskwala" else "TERABOX_BEARER_TOKEN"
        current_token = os.environ.get(env_key, "").strip()

        if not force_refresh and current_token and not is_token_expired(current_token, max_age_hours=max_age_hours):
            return True, current_token, ""

        logger.info(f"[TelethonExtractor] On-demand token refresh for {plat_key} (forced={force_refresh})...")
        ok, new_token, err = await fetch_telethon_bearer(plat_key)
        if ok and new_token:
            return True, new_token, ""
        elif current_token:
            logger.warning(f"[TelethonExtractor] Telethon refresh failed ({err}); falling back to existing token.")
            return True, current_token, f"Refresh failed: {err}"
        return False, "", err


# ── Scheduled Background Auto-Refresh Loop ──
_refresh_task: Optional[asyncio.Task] = None
_last_refresh_time: Optional[float] = None
_last_refresh_status: Dict[str, Any] = {}


async def refresh_all_tokens() -> Dict[str, Tuple[bool, str]]:
    """Refreshes both DiskWala and TeraBox tokens via Telethon."""
    global _last_refresh_time, _last_refresh_status
    import time
    results = {}

    logger.info("[AutoRefresh] Running DiskWala token refresh...")
    ok_dw, tok_dw, err_dw = await fetch_telethon_bearer("diskwala")
    results["diskwala"] = (ok_dw, "Refreshed" if ok_dw else err_dw)

    # Brief pause between MTProto calls
    await asyncio.sleep(2)

    logger.info("[AutoRefresh] Running TeraBox token refresh...")
    ok_tb, tok_tb, err_tb = await fetch_telethon_bearer("terabox")
    results["terabox"] = (ok_tb, "Refreshed" if ok_tb else err_tb)

    _last_refresh_time = time.time()
    _last_refresh_status = results
    return results


async def _auto_refresh_loop(interval_hours: float):
    """Infinite background loop that refreshes tokens every interval_hours."""
    interval_seconds = max(60, int(interval_hours * 3600))
    logger.info(
        f"[AutoRefresh] Telethon token scheduler started (Interval: {interval_hours} hours / {interval_seconds}s)."
    )

    # Initial delay on bot boot before running first refresh
    await asyncio.sleep(10)

    while True:
        try:
            logger.info("[AutoRefresh] Initiating scheduled Telethon token refresh...")
            res = await refresh_all_tokens()
            logger.info(f"[AutoRefresh] Scheduled token refresh cycle complete: {res}")
        except asyncio.CancelledError:
            logger.info("[AutoRefresh] Background token refresh task cancelled.")
            break
        except Exception as e:
            logger.error(f"[AutoRefresh] Unexpected error during scheduled token refresh: {e}", exc_info=True)
            # Sleep 5 minutes before retrying on unexpected failure
            await asyncio.sleep(300)
            continue

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("[AutoRefresh] Background token refresh task cancelled during sleep.")
            break


def start_auto_refresh_scheduler(interval_hours: Optional[float] = None) -> Optional[asyncio.Task]:
    """Starts the background auto-refresh task in the active asyncio loop."""
    global _refresh_task
    if interval_hours is None:
        interval_hours = TOKEN_REFRESH_INTERVAL_HOURS

    if interval_hours <= 0:
        logger.info(f"[AutoRefresh] Token auto-refresh is disabled (interval={interval_hours}).")
        return None

    if _refresh_task and not _refresh_task.done():
        logger.info("[AutoRefresh] Background scheduler task is already running.")
        return _refresh_task

    _refresh_task = asyncio.create_task(_auto_refresh_loop(interval_hours))
    return _refresh_task


async def stop_auto_refresh_scheduler():
    """Stops and cleans up the background auto-refresh task."""
    global _refresh_task
    if _refresh_task and not _refresh_task.done():
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
        _refresh_task = None
        logger.info("[AutoRefresh] Stopped Telethon token auto-refresh scheduler.")


def get_scheduler_status() -> Dict[str, Any]:
    """Returns the current status of the background auto-refresh scheduler."""
    is_running = bool(_refresh_task and not _refresh_task.done())
    return {
        "running": is_running,
        "interval_hours": TOKEN_REFRESH_INTERVAL_HOURS,
        "last_refresh_time": _last_refresh_time,
        "last_status": _last_refresh_status,
    }
