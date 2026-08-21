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


def format_cookie_header(raw_cookie: Optional[str]) -> Optional[str]:
    """Parses raw Netscape or key=value cookies into a clean single-line HTTP Cookie header string."""
    if not raw_cookie:
        return None
    cookies = []
    for line in raw_cookie.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies.append(f"{parts[5]}={parts[6]}")
        elif "=" in line:
            cookies.append(line.rstrip(";"))
    if not cookies:
        clean = raw_cookie.strip().replace("\n", "").replace("\r", "")
        return clean if "=" in clean else f"ndus={clean}"
    return "; ".join(cookies)


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
    """Backup Resolver: Hostinger API."""
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
                resp_text = await resp.text()
                logger.info(f"[_resolve_via_hostinger] HTTP {resp.status} | Body: {resp_text[:300]}")

                if resp.status != 200:
                    return False, {}, f"HTTP {resp.status}: {resp_text[:150]}"

                try:
                    data = json.loads(resp_text)
                except Exception:
                    return False, {}, f"Non-JSON response: {resp_text[:150]}"

                if not data or not isinstance(data, dict):
                    return False, {}, "Invalid response format from Hostinger resolver."

                if not data.get("success"):
                    msg = data.get("message") or data.get("error") or resp_text[:150]
                    return False, {}, msg

                play_url = data.get("play_url")
                if not play_url:
                    return False, {}, f"No play_url in response: {resp_text[:150]}"

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
        logger.error(f"[_resolve_via_hostinger] Exception: {e}", exc_info=True)
        return False, {}, str(e)


async def _resolve_via_gateway(url: str) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Primary Resolver: Cookie & terabox-gateway architecture (saahiyo/terabox-gateway).
    Supports direct cookie-based authenticated endpoints, custom gateways, and public gateway endpoints.
    """
    surl = extract_surl(url)
    custom_gateway = os.getenv("TERABOX_GATEWAY_URL", "").strip()
    raw_cookie = get_terabox_cookie()
    cookie_header = format_cookie_header(raw_cookie)

    logger.info(f"[_resolve_via_gateway] Starting resolution for surl={surl}")
    logger.info(f"[_resolve_via_gateway] Loaded cookie len={len(raw_cookie) if raw_cookie else 0}, header preview: {cookie_header[:80] if cookie_header else 'NONE'}")

    diag_logs = []

    # Candidate endpoints to query
    endpoints = []

    # 1. Direct authenticated official endpoints (if cookie is active)
    if surl and cookie_header:
        endpoints.append(f"https://www.1024terabox.com/share/list?app_id=250528&shorturl={surl}&root=1")
        endpoints.append(f"https://www.terabox.app/share/list?app_id=250528&shorturl={surl}&root=1")
        endpoints.append(f"https://www.1024terabox.com/share/list?app_id=250528&shorturl=1{surl}&root=1")
        endpoints.append(f"https://www.terabox.app/share/list?app_id=250528&shorturl=1{surl}&root=1")
        endpoints.append(f"https://www.1024terabox.com/api/shorturlinfo?shorturl={surl}&root=1")
        endpoints.append(f"https://teraboxapp.com/api/shorturlinfo?shorturl={surl}&root=1")
    elif not cookie_header:
        diag_logs.append("No active cookies found in cooky/terabox/cookies.txt")

    # 2. Custom gateway instance from .env
    if custom_gateway:
        endpoints.append(f"{custom_gateway.rstrip('/')}/api?url={urllib.parse.quote(url, safe='')}")
        if surl:
            endpoints.append(f"{custom_gateway.rstrip('/')}/?mode=resolve&surl={surl}&raw=1")

    # 3. Public gateway fallback endpoints
    encoded_url = urllib.parse.quote(url, safe="")
    endpoints.append(f"https://terabox-dl.qtcloud.workers.dev/api/get-info?url={encoded_url}")
    endpoints.append(f"https://terabox-videodownloader.online/api/info?url={encoded_url}")

    referer_url = f"https://www.terabox.app/sharing/link?surl={surl}" if surl else url
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': referer_url,
        'Origin': 'https://www.terabox.app',
    }
    if cookie_header:
        headers['Cookie'] = cookie_header

    connector = aiohttp.TCPConnector(ssl=False)
    client_timeout = aiohttp.ClientTimeout(total=30, connect=10)

    for ep in endpoints:
        try:
            parsed_ep = urllib.parse.urlparse(ep)
            ep_label = f"{parsed_ep.netloc}{parsed_ep.path}"
            async with aiohttp.ClientSession(connector=connector, headers=headers, timeout=client_timeout) as session:
                async with session.get(ep) as resp:
                    resp_text = await resp.text()
                    logger.info(f"[_resolve_via_gateway] {ep_label} -> HTTP {resp.status} | Body: {resp_text[:300]}")

                    if resp.status != 200:
                        diag_logs.append(f"{ep_label}: HTTP {resp.status}")
                        continue

                    try:
                        data = json.loads(resp_text)
                    except Exception:
                        diag_logs.append(f"{ep_label}: invalid JSON ({resp_text[:80]})")
                        continue

                    if not data:
                        diag_logs.append(f"{ep_label}: empty JSON")
                        continue

                    # Handle list of files format from terabox-gateway
                    if isinstance(data, list) and len(data) > 0:
                        first = data[0]
                        dlink = first.get("download_link") or first.get("direct_link") or first.get("link")
                        if dlink:
                            logger.info(f"[_resolve_via_gateway] Success via gateway list: {dlink[:60]}")
                            return True, {
                                "id": uuid.uuid4().hex[:8],
                                "title": first.get("filename") or "TeraBox_Video",
                                "thumbnail": first.get("thumbnail") or ((first.get("thumbnails") or {}).get("850x580")),
                                "uploader": "TeraBox Gateway",
                                "play_url": dlink,
                                "original_url": url,
                                "duration": None,
                                "formats": [],
                                "engine": "cookie_gateway",
                            }, None

                    # Handle dictionary response
                    if isinstance(data, dict):
                        errno = data.get("errno")
                        errmsg = data.get("errmsg") or data.get("msg") or data.get("message") or ""
                        if errno not in (None, 0):
                            logger.warning(f"[_resolve_via_gateway] {ep_label} returned errno: {errno} ({errmsg})")
                            diag_logs.append(f"{ep_label}: errno={errno} ({errmsg or 'error'})")
                            continue

                        # Format 1: direct link / dlink / stream_url / play_url
                        play_url = (
                            data.get("play_url")
                            or data.get("download_link")
                            or data.get("direct_link")
                            or data.get("dlink")
                            or data.get("stream_url")
                            or data.get("fast_download_link")
                        )
                        title = data.get("title") or data.get("filename") or "TeraBox_Video"
                        thumb = data.get("thumbnail") or data.get("thumb")

                        # Format 2: nested list from official /share/list
                        if "list" in data and isinstance(data["list"], list) and len(data["list"]) > 0:
                            item = data["list"][0]
                            title = item.get("server_filename") or item.get("filename") or title
                            thumb = (item.get("thumbs") or {}).get("url3") or item.get("thumbnail") or thumb
                            play_url = play_url or item.get("dlink") or item.get("direct_link") or item.get("download_link")

                            # If no dlink in share/list, attempt step-2 /share/download API call
                            if not play_url and item.get("fs_id") and data.get("shareid") and data.get("uk"):
                                try:
                                    fs_id = item["fs_id"]
                                    shareid = data["shareid"]
                                    uk = data["uk"]
                                    sign = data.get("sign", "")
                                    timestamp = data.get("timestamp", int(time.time()))
                                    jsToken = data.get("jsToken", "")
                                    dl_ep = (
                                        f"https://www.terabox.app/share/download?"
                                        f"app_id=250528&web=1&channel=dubox&clienttype=0"
                                        f"&jsToken={jsToken}&shareid={shareid}&uk={uk}&sign={sign}&timestamp={timestamp}"
                                        f"&primaryid={shareid}&fid_list=[{fs_id}]"
                                    )
                                    logger.info(f"[_resolve_via_gateway] Attempting 2-step /share/download for fs_id={fs_id}")
                                    async with session.get(dl_ep) as dl_resp:
                                        dl_text = await dl_resp.text()
                                        logger.info(f"[_resolve_via_gateway] Step-2 dl_ep -> HTTP {dl_resp.status} | Body: {dl_text[:300]}")
                                        if dl_resp.status == 200:
                                            dl_data = json.loads(dl_text)
                                            if dl_data and dl_data.get("errno") == 0:
                                                play_url = dl_data.get("dlink")
                                                logger.info(f"[_resolve_via_gateway] Step-2 download succeeded: {bool(play_url)}")
                                            else:
                                                diag_logs.append(f"share/download: errno={dl_data.get('errno')} ({dl_data.get('errmsg', '')})")
                                except Exception as dl_err:
                                    logger.warning(f"[_resolve_via_gateway] Step-2 download error: {dl_err}")
                                    diag_logs.append(f"share/download exception: {dl_err}")

                        if play_url:
                            logger.info(f"[_resolve_via_gateway] Successfully extracted play_url: {play_url[:60]}...")
                            return True, {
                                "id": uuid.uuid4().hex[:8],
                                "title": title,
                                "thumbnail": thumb,
                                "uploader": "TeraBox Gateway",
                                "play_url": play_url,
                                "original_url": url,
                                "duration": None,
                                "formats": [],
                                "engine": "cookie_gateway",
                            }, None
                        else:
                            diag_logs.append(f"{ep_label}: no play_url in response ({resp_text[:100]})")
        except Exception as e:
            logger.debug(f"Gateway endpoint {ep} failed: {e}")
            diag_logs.append(f"{ep_label}: {e}")
            continue

    summary = "\n• ".join(diag_logs[:4]) if diag_logs else "All endpoints returned empty or failed"
    return False, {}, f"• {summary}"


from browser_verifier import (
    is_browserless_configured,
    solve_terabox_captcha_interactive,
)


async def extract_terabox_info(
    url: str,
    max_retries: int = 3,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Fetches video metadata and CDN play URL.
    1. Primary Engine: Cookie-based Gateway architecture.
    2. Fallback / Backup Engine: Hostinger resolver API.
    3. Interactive HITL Remote Browser Solver: If CAPTCHA is required and Browserless is configured.
    """
    # 1. Primary Engine (Cookie & Gateway Architecture)
    gw_success, gw_info, gw_err = await _resolve_via_gateway(url)
    if gw_success and gw_info:
        logger.info("TeraBox link resolved successfully via primary Cookie/Gateway engine.")
        return True, gw_info, None

    logger.info(f"Primary Cookie/Gateway resolver unavailable ({gw_err}). Engaging Hostinger backup resolver...")

    # 2. Backup Engine (Hostinger API)
    err = None
    for attempt in range(1, max_retries + 1):
        success, info, err = await _resolve_via_hostinger(url)
        if success and info:
            logger.info("TeraBox link resolved successfully via backup Hostinger engine.")
            return True, info, None
        if "try again" in str(err).lower() and attempt < max_retries:
            await asyncio.sleep(2.5)
            continue
        break

    # 3. Interactive HITL Remote Browser Solver (if Browserless Docker is running)
    if is_browserless_configured() and notify_admin_callback:
        logger.info("Engaging Interactive Remote Browser CAPTCHA solver...")
        raw_cookie = get_terabox_cookie()
        solved, new_cookie, captcha_err = await solve_terabox_captcha_interactive(
            target_url=url,
            initial_cookie=raw_cookie,
            notify_admin_callback=notify_admin_callback,
            progress_updater=progress_updater,
        )
        if solved:
            logger.info("CAPTCHA resolved! Re-attempting primary resolution with new session...")
            # Retry 1: Cookie Gateway
            retry_success, retry_info, retry_err = await _resolve_via_gateway(url)
            if retry_success and retry_info:
                return True, retry_info, None
            logger.warning(f"Re-resolution via Gateway failed ({retry_err}). Re-attempting Hostinger backup...")

            # Retry 2: Hostinger backup
            h_success, h_info, h_err = await _resolve_via_hostinger(url)
            if h_success and h_info:
                return True, h_info, None
            logger.error(f"Re-resolution via Hostinger after CAPTCHA solve also failed: {h_err}")
        else:
            logger.error(f"Interactive CAPTCHA solver failed: {captcha_err}")

    raw_cookie = get_terabox_cookie()
    cookie_status = f"✅ Present ({len(raw_cookie)} chars)" if raw_cookie else "⚠️ None (cooky/terabox/cookies.txt missing)"

    full_error_report = (
        f"❌ **TeraBox Resolution Failed**\n\n"
        f"🔐 **Cookie Status:** {cookie_status}\n\n"
        f"🌐 **Primary Gateway Responses:**\n{gw_err}\n\n"
        f"🔄 **Backup Hostinger API:**\n• {err}"
    )

    logger.error(f"[extract_terabox_info] Complete failure report:\n{full_error_report}")
    return False, {}, full_error_report


async def download_terabox_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads TeraBox stream (m3u8 or mp4) obtained from the resolver API.
    Returns (success, file_path, info_dict, error_message).
    """
    # 1. Resolve CDN play URL
    success, info, err = await extract_terabox_info(
        url,
        notify_admin_callback=notify_admin_callback,
        progress_updater=progress_updater,
    )
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
            'skip_unavailable_fragments': True,
            'keep_fragments': False,
            'buffersize': 1024 * 1024 * 8,
            'postprocessor_args': ['-movflags', '+faststart'],
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
