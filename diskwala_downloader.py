import os
import re
import time
import json
import uuid
import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable, List

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import (
    DOWNLOAD_DIR,
    BASE_DIR,
    MAX_FILE_SIZE_BYTES,
)
from downloader import format_bytes, remove_file_safely

logger = logging.getLogger(__name__)

# Path to project .env file
ENV_FILE = BASE_DIR / ".env"

# AES-GCM Key extracted from Diskwala MiniApp bundle (gO = "e7109544dab612bd5b80b8a427ac474ba5541b9efff7a4ca1c8ef85df2489c23")
DISKWALA_AES_KEY = bytes.fromhex("e7109544dab612bd5b80b8a427ac474ba5541b9efff7a4ca1c8ef85df2489c23")

# Diskwala share links embed a 24-character hexadecimal MongoDB ObjectId
_LINK_ID_RE = re.compile(r"[a-fA-F0-9]{24}")

# Regex to detect Diskwala URLs in message text
DISKWALA_URL_RE = re.compile(r"https?://\S*diskwala\.com/\S+", re.IGNORECASE)

# Browser headers for S3 stream downloading
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "video",
    "Sec-Fetch-Mode": "no-cors",
    "Sec-Fetch-Site": "cross-site",
}


def get_diskwala_token() -> str:
    """
    Returns the current Diskwala MiniApp Bearer token directly from .env or os.environ.
    """
    tok = os.environ.get("DISKWALA_BEARER_TOKEN", "").strip()
    if not tok and ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line_clean = line.strip()
                    if line_clean.startswith("DISKWALA_BEARER_TOKEN="):
                        val = line_clean.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            tok = val
                            os.environ["DISKWALA_BEARER_TOKEN"] = tok
                            break
        except Exception as e:
            logger.error(f"[Diskwala] Error reading token from .env: {e}")

    if tok:
        return tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return ""


def save_diskwala_token(token: str) -> bool:
    """
    Saves and updates the Diskwala MiniApp Bearer token directly in the .env file.
    """
    clean_val = token.strip()
    if clean_val.startswith("Bearer "):
        full_token = clean_val
    else:
        full_token = f"Bearer {clean_val}"

    # Update in-memory environment variable immediately
    os.environ["DISKWALA_BEARER_TOKEN"] = full_token

    # Persist directly into .env
    try:
        lines = []
        found = False
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.strip().startswith("DISKWALA_BEARER_TOKEN="):
                new_lines.append(f"DISKWALA_BEARER_TOKEN={full_token}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"DISKWALA_BEARER_TOKEN={full_token}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        logger.info("[Diskwala] Updated DISKWALA_BEARER_TOKEN in .env successfully.")
        return True
    except Exception as e:
        logger.error(f"[Diskwala] Failed to write token to .env: {e}")
        return False


def is_diskwala_url(url: str) -> bool:
    """Checks whether the given URL is a Diskwala link."""
    if not url:
        return False
    u = url.lower().strip()
    return "diskwala.com" in u


def extract_diskwala_id(text: str) -> Optional[str]:
    """Extracts the 24-hex Diskwala link id found in `text`, or None."""
    m = _LINK_ID_RE.search(text or "")
    return m.group(0) if m else None


def extract_all_diskwala_urls(text: str) -> List[str]:
    """Extracts all unique Diskwala URLs (with link id) from `text`."""
    seen = set()
    urls = []
    for m in DISKWALA_URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(").,]}\"'")
        if url not in seen and extract_diskwala_id(url):
            seen.add(url)
            urls.append(url)
    return urls


def _clean_filename(name: str) -> str:
    """Sanitizes filename for safe filesystem storage."""
    safe = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return safe or "diskwala_video.mp4"


def _build_session() -> requests.Session:
    """Creates a robust requests session with retries and browser headers."""
    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=4,
        max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504]),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _decrypt_diskwala_file(file_obj: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypts AES-GCM encrypted file metadata returned by api2.diskwala.net."""
    if not file_obj.get("_x"):
        return file_obj

    iv = bytes.fromhex(file_obj["s"])
    ciphertext = bytes.fromhex(file_obj["p"])
    tag = bytes.fromhex(file_obj["h"])

    aesgcm = AESGCM(DISKWALA_AES_KEY)
    plaintext = aesgcm.decrypt(iv, ciphertext + tag, None)
    return json.loads(plaintext.decode("utf-8"))


async def _resolve_via_miniapp_api(diskwala_url: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Directly resolves Diskwala links via Telegram MiniApp backend (api2.diskwala.net)
    with instant AES-GCM decryption.
    """
    # Ensure token is valid (auto-refreshes via Telethon if missing or older than TOKEN_MAX_AGE_HOURS)
    try:
        from telethon_extractor import ensure_valid_token
        ok, valid_token, _ = await ensure_valid_token("diskwala")
        if ok and valid_token:
            token = valid_token
        else:
            token = get_diskwala_token()
    except Exception as e:
        logger.debug(f"[Diskwala] ensure_valid_token notice: {e}")
        token = get_diskwala_token()

    if not token:
        return False, None, "Diskwala Bearer Token is not set. Use `/ezdisk` or `/setdiskwala <token>` in chat to configure it."

    logger.info(f"[Diskwala] Querying MiniApp Direct API for {diskwala_url[:80]}...")

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.6",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://miniapp.diskwala.net",
        "referer": "https://miniapp.diskwala.net/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "x-bot-id": "diskwala",
    }

    # Step 1: Trigger link processing
    def _trigger():
        trigger_url = "https://api2.diskwala.net/api/diskwala/download/d"
        return requests.post(trigger_url, json={"link": diskwala_url}, headers=headers, timeout=15)

    try:
        t_res = await asyncio.to_thread(_trigger)
        if t_res.status_code == 401:
            logger.warning("[Diskwala] MiniApp token expired (HTTP 401). Triggering on-demand Telethon refresh...")
            try:
                from telethon_extractor import ensure_valid_token
                ok_ref, new_tok, _ = await ensure_valid_token("diskwala", force_refresh=True)
                if ok_ref and new_tok:
                    headers["authorization"] = new_tok
                    t_res = await asyncio.to_thread(_trigger)
            except Exception as ref_err:
                logger.error(f"[Diskwala] Auto-refresh on 401 failed: {ref_err}")

        if t_res.status_code == 401:
            logger.warning("[Diskwala] MiniApp token unauthorized/expired after retry.")
            return False, None, "Diskwala Bearer Token is unauthorized or expired. Auto-refresh failed; please run `/ezdisk`."

        t_data = t_res.json()
        if not t_data.get("ok"):
            err_msg = t_data.get("error") or "API rejected link."
            logger.warning(f"[Diskwala] MiniApp trigger returned error: {err_msg}")
            return False, None, f"Diskwala error: {err_msg}"
    except Exception as e:
        logger.error(f"[Diskwala] MiniApp trigger request error: {e}")
        return False, None, f"Failed to reach Diskwala API: {e}"

    # Step 2: Poll status endpoint for completed file metadata
    encoded_link = urllib.parse.quote(diskwala_url, safe="")
    status_url = f"https://api2.diskwala.net/api/diskwala/status?link={encoded_link}"
    status_headers = dict(headers)
    status_headers.pop("content-type", None)

    def _poll():
        return requests.get(status_url, headers=status_headers, timeout=15)

    for attempt in range(8):
        try:
            res = await asyncio.to_thread(_poll)
            if res.status_code == 401:
                return False, None, "Diskwala Bearer Token is unauthorized or expired. Please update it with `/setdiskwala <token>`."

            if res.status_code == 200:
                body = res.json()
                if body.get("ok") and body.get("status") == "done" and body.get("file"):
                    file_info = body["file"]
                    try:
                        decrypted = _decrypt_diskwala_file(file_info)
                    except Exception as dec_ex:
                        logger.error(f"[Diskwala] Decryption error: {dec_ex}")
                        return False, None, f"Failed to decrypt file payload: {dec_ex}"

                    download_url = decrypted.get("downloadUrl") or decrypted.get("download_url") or decrypted.get("url")
                    if download_url:
                        filename = _clean_filename(decrypted.get("name") or "diskwala_video.mp4")
                        filesize = int(decrypted.get("size") or 0)
                        thumb = decrypted.get("thumb") or decrypted.get("thumbnail")

                        info = {
                            "title": filename,
                            "filename": filename,
                            "filesize": filesize,
                            "download_url": download_url,
                            "thumbnail": thumb,
                            "uploader": "Diskwala",
                            "extractor": "diskwala_miniapp_api",
                        }
                        logger.info(f"[Diskwala] MiniApp resolved: {filename} ({format_bytes(filesize)})")
                        return True, info, None

                elif body.get("ok") and body.get("status") == "error":
                    return False, None, "Diskwala reported error resolving this link. The link may be deleted or private."
        except Exception as poll_ex:
            logger.debug(f"[Diskwala] Poll attempt {attempt + 1} error: {poll_ex}")

        await asyncio.sleep(1.5)

    return False, None, "Diskwala status poll timed out. Please try again in a moment."


async def extract_diskwala_info(
    url: str,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Extracts Diskwala video metadata and direct downloadable stream URL
    using the high-speed MiniApp Direct API.
    """
    download_id = uuid.uuid4().hex[:8]

    if progress_updater:
        try:
            await progress_updater("⏳ **Resolving Diskwala link...**")
        except Exception:
            pass

    success, info, err = await _resolve_via_miniapp_api(url)
    if success and info:
        info["id"] = download_id
        return True, info, None

    return False, None, err or "Failed to resolve Diskwala media."


async def download_diskwala_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads media from Diskwala URL via direct S3 streaming.
    """
    logger.info(f"[Diskwala] Starting download for: {url}")
    success, info, err = await extract_diskwala_info(url, progress_updater=progress_updater)
    if not success or not info:
        return False, None, None, err or "Unable to extract Diskwala video URL."

    download_url = info.get("download_url")
    if not download_url:
        return False, None, None, "No downloadable stream URL found."

    filename = info.get("filename") or f"diskwala_{info.get('id', 'video')}.mp4"
    if not filename.lower().endswith((".mp4", ".mkv", ".webm", ".ts")):
        filename += ".mp4"

    dest_file = DOWNLOAD_DIR / f"diskwala_{info.get('id', 'dl')}_{filename}"
    dest_path = str(dest_file)

    logger.info(f"[Diskwala] Streaming from {download_url[:60]}... to {dest_path}")
    if progress_updater:
        try:
            await progress_updater("📥 **Starting download...**")
        except Exception:
            pass

    def _stream_download():
        session = _build_session()
        with session.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get("content-length", info.get("filesize", 0) or 0))

            if total_bytes > MAX_FILE_SIZE_BYTES:
                raise ValueError(
                    f"File exceeds maximum allowed size ({format_bytes(total_bytes)} > {format_bytes(MAX_FILE_SIZE_BYTES)})."
                )

            downloaded_bytes = 0
            last_update = 0
            chunk_size = 1024 * 1024  # 1 MB chunks

            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    now = time.time()
                    if progress_updater and (now - last_update > 1.8):
                        last_update = now
                        if total_bytes > 0:
                            pct = int((downloaded_bytes / total_bytes) * 100)
                            msg = f"📥 **Downloading: {pct}%** ({format_bytes(downloaded_bytes)} / {format_bytes(total_bytes)})"
                        else:
                            msg = f"📥 **Downloading: {format_bytes(downloaded_bytes)}...**"
                        try:
                            asyncio.run_coroutine_threadsafe(
                                progress_updater(msg),
                                loop
                            )
                        except Exception:
                            pass

            return True

    loop = asyncio.get_running_loop()
    try:
        await asyncio.to_thread(_stream_download)
    except Exception as e:
        logger.error(f"[Diskwala] Streaming download failed: {e}")
        remove_file_safely(dest_path)
        return False, None, None, f"Download failed: {e}"

    if not os.path.exists(dest_path):
        return False, None, None, "Downloaded file not found on disk."

    file_size = os.path.getsize(dest_path)
    if file_size < 50 * 1024:
        remove_file_safely(dest_path)
        return False, None, None, f"Downloaded file is corrupt or too small ({file_size} bytes)."

    info["filesize"] = file_size
    info["filepath"] = dest_path
    logger.info(f"[Diskwala] 🎉 Download completed successfully: {dest_path} ({format_bytes(file_size)})")

    return True, dest_path, info, None
