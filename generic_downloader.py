import os
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
import yt_dlp

from config import DOWNLOAD_DIR, BASE_DIR

logger = logging.getLogger(__name__)


# Dedicated Generic/Universal cookies directory
GENERIC_COOKIE_DIR = BASE_DIR / "cooky" / "generic"
GENERIC_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
GENERIC_COOKIE_FILE = GENERIC_COOKIE_DIR / "cookies.txt"


def get_generic_ydl_opts() -> Dict[str, Any]:
    """Returns standalone yt-dlp options for generic / universal non-DRM video sources."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'noplaylist': True,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    possible_cookie_paths = [
        GENERIC_COOKIE_FILE,
        BASE_DIR / "cooky" / "generic_cookies.txt",
        Path.home() / "DownTG" / "DownTG" / "cooky" / "generic" / "cookies.txt",
    ]
    for cp in possible_cookie_paths:
        if cp.exists() and cp.is_file() and cp.stat().st_size > 0:
            opts['cookiefile'] = str(cp)
            logger.info(f"Using Generic cookies from: {cp}")
            break

    return opts


import aiohttp
import aiofiles


def is_direct_media_url(url: str) -> bool:
    """Check if URL points directly to a media file."""
    clean_url = url.split("?")[0].lower()
    return any(clean_url.endswith(ext) for ext in [".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".wav"])


async def extract_generic_info(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Extracts metadata from any non-DRM website or direct video link.
    Returns (success, info_dict, error_message).
    """
    if is_direct_media_url(url):
        fname = Path(url.split("?")[0]).name or "Direct_Video.mp4"
        return True, {
            "title": fname,
            "duration": None,
            "uploader": "Direct Link",
            "formats": [],
        }, None

    def _extract():
        opts = get_generic_ydl_opts()
        opts['extract_flat'] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return False, {}, "Could not extract video metadata from this URL."

                # Check for DRM indicator
                if info.get("has_drm") or any(f.get("has_drm") for f in info.get("formats", [])):
                    return False, {}, "This video has DRM protection (e.g., Widevine/FairPlay) and cannot be downloaded."

                return True, info, None
            except Exception as e:
                err_str = str(e)
                if "DRM" in err_str or "encrypted" in err_str.lower():
                    return False, {}, "This stream is DRM-protected and cannot be downloaded."
                return False, {}, err_str

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


async def download_generic_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads media from any non-DRM video source or direct video link.
    Returns (success, file_path, info_dict, error_message).
    """
    download_id = uuid.uuid4().hex[:8]
    output_template = str(DOWNLOAD_DIR / f"gen_{download_id}_%(title).100B.%(ext)s")

    def _download():
        opts = get_generic_ydl_opts()
        opts['outtmpl'] = output_template

        if progress_hook:
            opts['progress_hooks'] = [progress_hook]

        if format_selector:
            opts['format'] = format_selector
            opts['merge_output_format'] = 'mp4'
        elif quality == "audio":
            opts['format'] = 'bestaudio/ba/b/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        elif quality == "720":
            opts['format'] = 'bv*[height<=?720]+ba/b[height<=?720]/bv*[width<=?720]+ba/b[width<=?720]/b/best'
            opts['merge_output_format'] = 'mp4'
        elif quality == "480":
            opts['format'] = 'bv*[height<=?480]+ba/b[height<=?480]/bv*[width<=?480]+ba/b[width<=?480]/b/best'
            opts['merge_output_format'] = 'mp4'
        elif quality == "360":
            opts['format'] = 'bv*[height<=?360]+ba/b[height<=?360]/bv*[width<=?360]+ba/b[width<=?360]/b/best'
            opts['merge_output_format'] = 'mp4'
        else:
            opts['format'] = 'bestvideo+bestaudio/b/best'
            opts['merge_output_format'] = 'mp4'

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                downloaded_file = None

                if 'requested_downloads' in info and info['requested_downloads']:
                    downloaded_file = info['requested_downloads'][0].get('filepath')

                if not downloaded_file or not os.path.exists(downloaded_file):
                    filename = ydl.prepare_filename(info)
                    if quality == "audio":
                        filename = os.path.splitext(filename)[0] + ".mp3"
                    elif opts.get('merge_output_format') == 'mp4':
                        filename = os.path.splitext(filename)[0] + ".mp4"

                    if os.path.exists(filename):
                        downloaded_file = filename
                    else:
                        for f in DOWNLOAD_DIR.glob(f"gen_{download_id}_*"):
                            downloaded_file = str(f)
                            break

                if downloaded_file and os.path.exists(downloaded_file):
                    return True, downloaded_file, info, None
                else:
                    return False, None, info, "Downloaded file could not be located."
            except Exception as e:
                return False, None, None, str(e)

    loop = asyncio.get_running_loop()
    success, downloaded_file, info, error_msg = await loop.run_in_executor(None, _download)

    # Fallback to direct HTTP stream download if yt-dlp failed on a direct media URL
    if not success and is_direct_media_url(url):
        try:
            ext = Path(url.split("?")[0]).suffix or ".mp4"
            target_path = str(DOWNLOAD_DIR / f"direct_{download_id}{ext}")
            connector = aiohttp.TCPConnector(ssl=False)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            }
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.get(url, timeout=60) as resp:
                    if resp.status in (200, 206):
                        async with aiofiles.open(target_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(64 * 1024):
                                await f.write(chunk)
                        if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                            return True, target_path, {"title": Path(url.split("?")[0]).name}, None
        except Exception as dl_err:
            return False, None, None, f"Direct stream download failed: {dl_err}"

    return success, downloaded_file, info, error_msg


def remove_file_safely(file_path: Optional[str]):
    """Safely delete temporary downloaded file."""
    if not file_path:
        return
    try:
        path = Path(file_path)
        if path.exists():
            path.unlink()
            logger.info(f"Cleaned up temporary file: {file_path}")
    except Exception as e:
        logger.error(f"Failed to remove file {file_path}: {e}")
