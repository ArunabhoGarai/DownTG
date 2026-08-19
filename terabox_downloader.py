import os
import uuid
import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
from urllib.parse import urlparse, parse_qs
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

HOSTINGER_API_ENDPOINT = "https://gold-newt-367030.hostingersite.com/tera.php?url="


def is_terabox_url(url: str) -> bool:
    """Check if the URL belongs to TeraBox or its mirror domains."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in TERABOX_DOMAINS)


def extract_surl(url: str) -> Optional[str]:
    """Extracts surl key from any TeraBox URL variation."""
    try:
        parsed = urlparse(url)
        if "surl=" in parsed.query:
            surl = parse_qs(parsed.query).get("surl", [""])[0]
        elif "/s/" in parsed.path:
            surl = parsed.path.split("/s/")[1].split("/")[0].split("?")[0]
        else:
            surl = None

        if surl and surl.startswith("1"):
            surl = surl[1:]
        return surl
    except Exception:
        return None


def get_terabox_cookie() -> Optional[str]:
    """Loads optional TeraBox ndus cookie from cooky/terabox/cookies.txt or .env."""
    cookie_file = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
    if cookie_file.exists() and cookie_file.is_file():
        try:
            with open(cookie_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception:
            pass
    return os.getenv("TERABOX_COOKIE") or os.getenv("COOKIE_JSON")


async def _resolve_via_hostinger(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """Primary Resolver: Hostinger API."""
    encoded_url = urllib.parse.quote(url, safe="")
    target_api = f"{HOSTINGER_API_ENDPOINT}{encoded_url}"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
    }

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=45, connect=10)
        async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=client_timeout) as session:
            async with session.get(target_api) as resp:
                if resp.status != 200:
                    return False, {}, f"Hostinger API returned HTTP {resp.status}"

                data = await resp.json(content_type=None)
                if not data or not isinstance(data, dict):
                    return False, {}, "Invalid response from Hostinger resolver."

                if not data.get("success"):
                    msg = data.get("message") or data.get("error") or "Unknown error"
                    return False, {}, msg

                play_url = data.get("play_url")
                if not play_url:
                    return False, {}, "No play_url in Hostinger response"

                return True, {
                    "id": uuid.uuid4().hex[:8],
                    "title": data.get("title") or "TeraBox_Video",
                    "thumbnail": data.get("thumbnail"),
                    "uploader": data.get("channel") or "TeraBox",
                    "play_url": play_url,
                    "original_url": url,
                    "duration": None,
                    "formats": [],
                    "engine": "hostinger",
                }, None
    except Exception as e:
        return False, {}, str(e)


async def _resolve_via_gateway(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Fallback Resolver: terabox-gateway architecture (saahiyo/terabox-gateway).
    Supports custom gateway instances or public gateway endpoints.
    """
    surl = extract_surl(url)
    custom_gateway = os.getenv("TERABOX_GATEWAY_URL", "").strip()

    # Candidate endpoints to query
    endpoints = []
    if custom_gateway:
        endpoints.append(f"{custom_gateway.rstrip('/')}/api?url={urllib.parse.quote(url, safe='')}")
        if surl:
            endpoints.append(f"{custom_gateway.rstrip('/')}/?mode=resolve&surl={surl}&raw=1")

    # Public gateway fallback endpoints
    encoded_url = urllib.parse.quote(url, safe="")
    endpoints.append(f"https://terabox-dl.qtcloud.workers.dev/api/get-info?url={encoded_url}")
    endpoints.append(f"https://terabox-videodownloader.online/api/info?url={encoded_url}")

    cookie = get_terabox_cookie()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
    }
    if cookie:
        headers['Cookie'] = cookie if "ndus=" in cookie else f"ndus={cookie}"

    connector = aiohttp.TCPConnector(ssl=False)
    client_timeout = aiohttp.ClientTimeout(total=30, connect=10)

    for ep in endpoints:
        try:
            async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=client_timeout) as session:
                async with session.get(ep) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                    if not data:
                        continue

                    # Handle list of files format from terabox-gateway
                    if isinstance(data, list) and len(data) > 0:
                        first = data[0]
                        dlink = first.get("download_link") or first.get("direct_link") or first.get("link")
                        if dlink:
                            return True, {
                                "id": uuid.uuid4().hex[:8],
                                "title": first.get("filename") or "TeraBox_Video",
                                "thumbnail": first.get("thumbnail") or ((first.get("thumbnails") or {}).get("850x580")),
                                "uploader": "TeraBox Gateway",
                                "play_url": dlink,
                                "original_url": url,
                                "duration": None,
                                "formats": [],
                                "engine": "gateway",
                            }, None

                    # Handle dictionary response
                    if isinstance(data, dict):
                        # Format 1: direct link / dlink / stream_url / play_url
                        play_url = (
                            data.get("play_url")
                            or data.get("download_link")
                            or data.get("direct_link")
                            or data.get("dlink")
                            or data.get("stream_url")
                            or data.get("fast_download_link")
                        )
                        # Format 2: nested list or response
                        if not play_url and "list" in data and isinstance(data["list"], list) and len(data["list"]) > 0:
                            item = data["list"][0]
                            play_url = item.get("dlink") or item.get("direct_link") or item.get("download_link")
                            title = item.get("server_filename") or item.get("filename") or "TeraBox_Video"
                            thumb = (item.get("thumbs") or {}).get("url3") or item.get("thumbnail")
                        else:
                            title = data.get("title") or data.get("filename") or "TeraBox_Video"
                            thumb = data.get("thumbnail") or data.get("thumb")

                        if play_url:
                            return True, {
                                "id": uuid.uuid4().hex[:8],
                                "title": title,
                                "thumbnail": thumb,
                                "uploader": "TeraBox Gateway",
                                "play_url": play_url,
                                "original_url": url,
                                "duration": None,
                                "formats": [],
                                "engine": "gateway",
                            }, None
        except Exception as e:
            logger.debug(f"Gateway endpoint {ep} failed: {e}")
            continue

    return False, {}, "Both Hostinger resolver and fallback Gateway could not resolve this link."


async def extract_terabox_info(url: str, max_retries: int = 2) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Fetches video metadata and CDN play URL.
    1. Attempts primary Hostinger resolver.
    2. If Hostinger fails or is busy, automatically falls back to terabox-gateway architecture.
    """
    # 1. Primary Engine (Hostinger)
    for attempt in range(1, max_retries + 1):
        success, info, err = await _resolve_via_hostinger(url)
        if success and info:
            return True, info, None
        if "try again" in str(err).lower() and attempt < max_retries:
            await asyncio.sleep(2)
            continue
        break

    # 2. Fallback Engine (terabox-gateway)
    logger.info(f"Hostinger resolver unavailable ({err}). Engaging terabox-gateway fallback...")
    gw_success, gw_info, gw_err = await _resolve_via_gateway(url)
    if gw_success and gw_info:
        return True, gw_info, None

    return False, {}, f"TeraBox Resolution Failed: {err or gw_err}"


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
            'socket_timeout': 60,
            'retries': 20,
            'fragment_retries': 30,
            'buffersize': 1024 * 1024 * 16,
            'http_chunk_size': 10485760,
            'hls_use_mpegts': True,
            'concurrent_fragment_downloads': 5,
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
