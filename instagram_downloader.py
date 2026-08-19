import os
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
import yt_dlp

from config import DOWNLOAD_DIR, BASE_DIR

logger = logging.getLogger(__name__)

# Dedicated Instagram cookies directory
INSTAGRAM_COOKIE_DIR = BASE_DIR / "cooky" / "instagram"
INSTAGRAM_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
INSTAGRAM_COOKIE_FILE = INSTAGRAM_COOKIE_DIR / "cookies.txt"


def get_instagram_ydl_opts() -> Dict[str, Any]:
    """Returns standalone yt-dlp options specifically tuned for Instagram."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'noplaylist': True,
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 '
                'Instagram 314.0.0.19.109'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.instagram.com/',
        },
    }

    # Use isolated Instagram cookies if present
    possible_cookie_paths = [
        INSTAGRAM_COOKIE_FILE,
        BASE_DIR / "cooky" / "instagram_cookies.txt",
        Path.home() / "DownTG" / "DownTG" / "cooky" / "instagram" / "cookies.txt",
    ]
    for cp in possible_cookie_paths:
        if cp.exists() and cp.is_file() and cp.stat().st_size > 0:
            opts['cookiefile'] = str(cp)
            logger.info(f"Using Instagram cookies from: {cp}")
            break

    return opts


async def extract_instagram_info(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Extracts Instagram metadata (Reels, Posts, Videos) without downloading.
    Returns (success, info_dict, error_message).
    """
    def _extract():
        opts = get_instagram_ydl_opts()
        opts['extract_flat'] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return True, info, None
            except Exception as e:
                return False, {}, str(e)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


async def download_instagram_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads Instagram media with isolated settings and cookies.
    Returns (success, file_path, info_dict, error_message).
    """
    download_id = uuid.uuid4().hex[:8]
    output_template = str(DOWNLOAD_DIR / f"ig_{download_id}_%(title).100B.%(ext)s")

    def _download():
        opts = get_instagram_ydl_opts()
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
        else:
            # Instagram serves direct mp4 streams
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
                        for f in DOWNLOAD_DIR.glob(f"ig_{download_id}_*"):
                            downloaded_file = str(f)
                            break

                if downloaded_file and os.path.exists(downloaded_file):
                    return True, downloaded_file, info, None
                else:
                    return False, None, info, "Downloaded Instagram file could not be located."
            except Exception as e:
                return False, None, None, str(e)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download)
