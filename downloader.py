import os
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
import yt_dlp

from config import DOWNLOAD_DIR, MAX_FILE_SIZE_BYTES, BASE_DIR

logger = logging.getLogger(__name__)

# Initialize static-ffmpeg if available so ffmpeg is in PATH
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    logger.info("static-ffmpeg initialized successfully.")
except Exception as e:
    logger.warning(f"Could not initialize static-ffmpeg: {e}")


def get_platform_badge(url: str) -> str:
    """Returns an emoji and platform name based on URL."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        if "/shorts/" in url_lower:
            return "🔴 YouTube Shorts"
        return "▶️ YouTube"
    elif "instagram.com" in url_lower:
        if "/reel/" in url_lower or "/reels/" in url_lower:
            return "📸 Instagram Reel"
        return "📸 Instagram"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower:
        if "/reel/" in url_lower or "/reels/" in url_lower:
            return "🔵 Facebook Reel"
        return "🔵 Facebook"
    elif "tiktok.com" in url_lower:
        return "🎵 TikTok"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "🐦 X / Twitter"
    return "🌐 Video"


def format_duration(seconds: Optional[int]) -> str:
    """Format duration in seconds to HH:MM:SS or MM:SS."""
    if not seconds:
        return "N/A"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_bytes(size: Optional[int]) -> str:
    """Format byte size into human readable string."""
    if not size or size <= 0:
        return "Unknown size"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_base_ydl_opts() -> Dict[str, Any]:
    """Returns standard base options for yt-dlp."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'noplaylist': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'tv_embedded', 'mweb', 'web'],
            }
        },
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Fetch-Mode': 'navigate',
        },
    }
    
    # Check for cookies file in project root
    cookie_file = BASE_DIR / "cookies.txt"
    if cookie_file.exists():
        opts['cookiefile'] = str(cookie_file)

    return opts


async def extract_media_info(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Extracts metadata from the given URL without downloading.
    Returns (success, info_dict, error_message).
    """
    def _extract():
        opts = get_base_ydl_opts()
        opts['extract_flat'] = False
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                return True, info, None
            except Exception as e:
                return False, {}, str(e)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


async def download_media(
    url: str,
    quality: str = "best",
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads media from URL with the chosen quality preset.
    Returns (success, file_path, info_dict, error_message).
    """
    download_id = uuid.uuid4().hex[:8]
    output_template = str(DOWNLOAD_DIR / f"{download_id}_%(title).100B.%(ext)s")

    def _download():
        opts = get_base_ydl_opts()
        opts['outtmpl'] = output_template

        if progress_hook:
            opts['progress_hooks'] = [progress_hook]

        if quality == "audio":
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
        else:  # "best"
            opts['format'] = 'bv*+ba/b/best'
            opts['merge_output_format'] = 'mp4'

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=True)
                downloaded_file = None

                # Find downloaded file
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
                        # Scan directory for matching prefix
                        for f in DOWNLOAD_DIR.glob(f"{download_id}_*"):
                            downloaded_file = str(f)
                            break

                if downloaded_file and os.path.exists(downloaded_file):
                    return True, downloaded_file, info, None
                else:
                    return False, None, info, "Downloaded file could not be located on disk."
            except Exception as e:
                return False, None, None, str(e)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download)


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
