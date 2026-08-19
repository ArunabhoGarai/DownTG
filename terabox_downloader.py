import os
import uuid
import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
import aiohttp
import yt_dlp

from config import DOWNLOAD_DIR, BASE_DIR

logger = logging.getLogger(__name__)

# TeraBox domain matching keywords
TERABOX_DOMAINS = [
    "terabox.com",
    "teraboxapp.com",
    "teraboxlink.com",
    "teraboxshare.com",
    "1024tera.com",
    "1024terabox.com",
    "freeterabox.com",
    "terabox.app",
    "tibibox.com",
    "mirrobox.com",
    "nephobox.com",
    "4funbox.com",
]

API_ENDPOINT = "https://gold-newt-367030.hostingersite.com/tera.php?url="


def is_terabox_url(url: str) -> bool:
    """Check if the URL belongs to TeraBox or its mirror domains."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in TERABOX_DOMAINS)


async def extract_terabox_info(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Fetches video metadata and CDN play URL from the custom TeraBox resolver API.
    Returns (success, info_dict, error_message).
    """
    encoded_url = urllib.parse.quote(url, safe="")
    target_api = f"{API_ENDPOINT}{encoded_url}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json',
    }

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=75, connect=15)
        async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=client_timeout) as session:
            async with session.get(target_api) as resp:
                if resp.status != 200:
                    return False, {}, f"TeraBox resolver API returned HTTP {resp.status}"

                data = await resp.json(content_type=None)
                if not data or not isinstance(data, dict):
                    return False, {}, "Invalid response from TeraBox resolver."

                if not data.get("success"):
                    err_msg = data.get("message") or data.get("error") or "Failed to resolve TeraBox video link."
                    return False, {}, err_msg

                play_url = data.get("play_url")
                if not play_url:
                    return False, {}, "No playable video stream found for this TeraBox link."

                title = data.get("title") or "TeraBox_Video"
                thumbnail = data.get("thumbnail")
                channel = data.get("channel") or "TeraBox"

                info_dict = {
                    "id": uuid.uuid4().hex[:8],
                    "title": title,
                    "thumbnail": thumbnail,
                    "uploader": channel,
                    "play_url": play_url,
                    "original_url": url,
                    "duration": None,
                    "formats": [],
                }
                return True, info_dict, None

    except asyncio.TimeoutError:
        return False, {}, "TeraBox resolver API timed out."
    except Exception as e:
        logger.error(f"Error resolving TeraBox URL: {e}", exc_info=True)
        return False, {}, f"TeraBox resolution error: {str(e)}"


async def download_terabox_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads TeraBox stream (m3u8 or mp4) obtained from the resolver API.
    Returns (success, file_path, info_dict, error_message).
    """
    # 1. Resolve CDN play URL
    success, info, err = await extract_terabox_info(url)
    if not success or not info or "play_url" not in info:
        return False, None, None, err or "Could not retrieve TeraBox stream URL."

    play_url = info["play_url"]
    download_id = uuid.uuid4().hex[:8]
    output_template = str(DOWNLOAD_DIR / f"tera_{download_id}_%(title).100B.%(ext)s")

    def _download():
        opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'noplaylist': True,
            'outtmpl': output_template,
            'merge_output_format': 'mp4',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                'Referer': 'https://www.terabox.com/',
            },
        }

        if progress_hook:
            opts['progress_hooks'] = [progress_hook]

        if quality == "audio":
            opts['format'] = 'bestaudio/ba/b/best'
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            opts['format'] = 'bestvideo+bestaudio/b/best'

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                dl_info = ydl.extract_info(play_url, download=True)
                downloaded_file = None

                if 'requested_downloads' in dl_info and dl_info['requested_downloads']:
                    downloaded_file = dl_info['requested_downloads'][0].get('filepath')

                if not downloaded_file or not os.path.exists(downloaded_file):
                    filename = ydl.prepare_filename(dl_info)
                    if quality == "audio":
                        filename = os.path.splitext(filename)[0] + ".mp3"
                    elif opts.get('merge_output_format') == 'mp4':
                        filename = os.path.splitext(filename)[0] + ".mp4"

                    if os.path.exists(filename):
                        downloaded_file = filename
                    else:
                        for f in DOWNLOAD_DIR.glob(f"tera_{download_id}_*"):
                            downloaded_file = str(f)
                            break

                if downloaded_file and os.path.exists(downloaded_file):
                    # Attach original TeraBox metadata
                    dl_info['title'] = info.get('title', dl_info.get('title'))
                    dl_info['thumbnail'] = info.get('thumbnail', dl_info.get('thumbnail'))
                    dl_info['uploader'] = info.get('uploader', dl_info.get('uploader'))
                    return True, downloaded_file, dl_info, None
                else:
                    return False, None, info, "Downloaded TeraBox file could not be located."
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
