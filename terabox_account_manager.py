"""
TeraBox Account Manager
=======================
Handles fetching video files from an authenticated TeraBox account using cookies,
extracting file lists and counts, and resolving video files for downloading/transfer.
"""

import os
import re
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable

import requests

from config import BASE_DIR, DOWNLOAD_DIR
from downloader import format_bytes, remove_file_safely
from terabox_downloader import (
    get_terabox_cookie,
    format_cookie_header,
    download_terabox_media,
    _clean_filename,
)

logger = logging.getLogger(__name__)

COOKIE_FILE = BASE_DIR / "cooky" / "terabox" / "cookies.txt"

# Preferred TeraBox API domain endpoints (with fallback and regional routing)
TERABOX_API_DOMAINS = [
    "dm.terabox.app",
    "dm.terabox.com",
    "www.terabox.app",
    "terabox.app",
    "www.terabox.com",
    "www.1024tera.com",
    "1024tera.com",
    "www.1024terabox.com",
]

# Supported video extensions for account file matching
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".webm", ".m4v", ".ts", ".3gp", ".rmvb", ".mpg", ".mpeg"
}


def get_account_cookie_header() -> Optional[str]:
    """Returns the formatted HTTP Cookie header string for the TeraBox account."""
    raw = get_terabox_cookie()
    return format_cookie_header(raw)


def save_account_cookie(cookie_content: str) -> bool:
    """Saves raw cookie text into cooky/terabox/cookies.txt."""
    try:
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_content.strip())
        logger.info("[TeraBox Account] Cookie successfully saved to disk.")
        return True
    except Exception as e:
        logger.error(f"[TeraBox Account] Failed to save cookie: {e}")
        return False


def _get_api_headers(cookie_header: str, domain: str = "www.terabox.app") -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://{domain}/main?category=all",
        "Origin": f"https://{domain}",
        "Cookie": cookie_header,
    }


def fetch_account_videos_sync(max_pages: int = 50) -> Dict[str, Any]:
    """
    Synchronously queries TeraBox categorylist (category=1 for videos)
    and paginates through all available video files in the account.
    """
    cookie_header = get_account_cookie_header()
    if not cookie_header:
        return {
            "success": False,
            "total_count": 0,
            "total_size": 0,
            "total_size_formatted": "0 B",
            "videos": [],
            "error": "No TeraBox cookie found. Please save cookies in cooky/terabox/cookies.txt or use /setteracookie.",
        }

    all_videos = []
    seen_fs_ids = set()

    working_domain = None
    headers = None
    last_errno = None

    # 1. Identify working domain from candidates (with dynamic regional prefix routing)
    domains_to_try = list(TERABOX_API_DOMAINS)
    tested_domains = set()

    idx = 0
    while idx < len(domains_to_try):
        domain = domains_to_try[idx]
        idx += 1
        if domain in tested_domains:
            continue
        tested_domains.add(domain)

        domain_headers = _get_api_headers(cookie_header, domain=domain)
        test_url = f"https://{domain}/api/categorylist?category=1&page=1&num=1&app_id=250528&web=1&channel=dubox&clienttype=0"
        try:
            r = requests.get(test_url, headers=domain_headers, timeout=10)
            # TeraBox returns regional cluster prefix in response headers when misdirected
            prefix = r.headers.get("Url-Domain-Prefix") or r.headers.get("Region-Domain-Prefix")
            if prefix and prefix.strip():
                p = prefix.strip()
                for suff in ["terabox.app", "terabox.com"]:
                    prefixed_domain = f"{p}.{suff}"
                    if prefixed_domain not in tested_domains and prefixed_domain not in domains_to_try:
                        domains_to_try.insert(idx, prefixed_domain)

            if r.status_code == 200:
                data = r.json()
                errno = data.get("errno")
                if errno == 0:
                    working_domain = domain
                    headers = domain_headers
                    logger.info(f"[TeraBox Account] Connected using domain: {domain}")
                    break
                else:
                    last_errno = errno
                    logger.warning(f"[TeraBox Account] Domain {domain} returned errno {errno}")
        except Exception as e:
            logger.debug(f"[TeraBox Account] Domain {domain} failed: {e}")

    if not working_domain:
        if last_errno in (105, -6):
            return {
                "success": False,
                "total_count": 0,
                "total_size": 0,
                "total_size_formatted": "0 B",
                "videos": [],
                "error": f"TeraBox cookie is unauthorized or expired (errno {last_errno}). Please re-login on www.terabox.app or update cookie with /setteracookie.",
            }
        working_domain = TERABOX_API_DOMAINS[0]
        headers = _get_api_headers(cookie_header, domain=working_domain)

    # 2. Paginate category=1 (Videos)
    page = 1
    num_per_page = 100

    while page <= max_pages:
        url = (
            f"https://{working_domain}/api/categorylist"
            f"?category=1&page={page}&num={num_per_page}&order=time&desc=1"
            f"&clienttype=0&app_id=250528&web=1"
        )
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.warning(f"[TeraBox Account] API page {page} returned HTTP {resp.status_code}")
                break

            data = resp.json()
            errno = data.get("errno")
            if errno != 0:
                logger.warning(f"[TeraBox Account] Page {page} returned errno {errno}")
                break

            info_list = data.get("info", [])
            if not info_list:
                break

            for item in info_list:
                fs_id = item.get("fs_id")
                if not fs_id or fs_id in seen_fs_ids:
                    continue

                filename = item.get("server_filename") or f"video_{fs_id}.mp4"
                size = int(item.get("size") or 0)
                path = item.get("path") or f"/{filename}"

                seen_fs_ids.add(fs_id)
                all_videos.append({
                    "fs_id": fs_id,
                    "filename": filename,
                    "path": path,
                    "size": size,
                    "size_formatted": format_bytes(size),
                    "thumbs": item.get("thumbs") or {},
                    "server_ctime": item.get("server_ctime"),
                    "server_mtime": item.get("server_mtime"),
                })

            if len(info_list) < num_per_page:
                # Reached the last page
                break

            page += 1

        except Exception as e:
            logger.error(f"[TeraBox Account] Error fetching page {page}: {e}")
            break

    # 3. If categorylist returned 0, try crawling root directory as fallback
    if not all_videos:
        try:
            list_url = f"https://{working_domain}/api/list?dir=%2F&order=time&desc=1&clienttype=0&app_id=250528&web=1&page=1&num=100"
            r = requests.get(list_url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                for item in data.get("list", []):
                    fname = item.get("server_filename", "")
                    ext = Path(fname).suffix.lower()
                    if ext in VIDEO_EXTENSIONS and item.get("fs_id") not in seen_fs_ids:
                        size = int(item.get("size") or 0)
                        fs_id = item.get("fs_id")
                        seen_fs_ids.add(fs_id)
                        all_videos.append({
                            "fs_id": fs_id,
                            "filename": fname,
                            "path": item.get("path") or f"/{fname}",
                            "size": size,
                            "size_formatted": format_bytes(size),
                            "thumbs": item.get("thumbs") or {},
                            "server_ctime": item.get("server_ctime"),
                            "server_mtime": item.get("server_mtime"),
                        })
        except Exception as fallback_err:
            logger.debug(f"[TeraBox Account] Root list fallback notice: {fallback_err}")

    total_bytes = sum(v["size"] for v in all_videos)

    return {
        "success": True,
        "total_count": len(all_videos),
        "total_size": total_bytes,
        "total_size_formatted": format_bytes(total_bytes),
        "videos": all_videos,
        "error": None,
    }


async def fetch_account_videos(max_pages: int = 50) -> Dict[str, Any]:
    """Async wrapper to fetch all video files from the TeraBox account."""
    return await asyncio.to_thread(fetch_account_videos_sync, max_pages)


async def _notify(progress_updater: Optional[Callable[[str], Any]], text: str):
    """Safely dispatches progress update whether updater is sync or async."""
    if not progress_updater:
        return
    try:
        if asyncio.iscoroutinefunction(progress_updater):
            await progress_updater(text)
        else:
            res = progress_updater(text)
            if asyncio.iscoroutine(res):
                await res
    except Exception as e:
        logger.debug(f"[Progress Notice] {e}")


def create_account_share_link_sync(
    fs_id: int,
    cookie_header: str,
    domain: str = "www.terabox.app",
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Creates an active TeraBox share link for an account video using cookie authentication.
    """
    headers = _get_api_headers(cookie_header, domain=domain)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    domains_to_try = [domain, "www.1024tera.com", "terabox.app", "www.terabox.com"]
    for d in domains_to_try:
        url = f"https://{d}/api/share/create?app_id=250528&web=1&channel=dubox&clienttype=0"
        data = {
            "fid_list": f"[{fs_id}]",
            "schannel": "4",
            "period": "0",
        }
        try:
            r = requests.post(url, data=data, headers=headers, timeout=12)
            if r.status_code == 200:
                res = r.json()
                if res.get("errno") == 0:
                    link = res.get("link")
                    if not link and res.get("shorturl"):
                        link = f"https://www.terabox.app/s/{res.get('shorturl')}"
                    if link:
                        return True, link, None
                logger.debug(f"[Share Create] Domain {d} returned errno {res.get('errno')}: {res.get('errmsg')}")
        except Exception as e:
            logger.debug(f"[Share Create] Domain {d} error: {e}")

    return False, None, "Could not generate share link from TeraBox account."


async def stream_cookie_account_download(
    remote_path: str,
    fs_id: Any,
    dest_path: str,
    filesize_expected: int,
    cookie_header: str,
    progress_updater: Optional[Callable[[str], Any]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Downloads file directly via TeraBox PCS / rest API with live progress, speed, and ETA.
    """
    headers = _get_api_headers(cookie_header)
    encoded_path = requests.utils.quote(remote_path) if remote_path else ""

    loop = asyncio.get_running_loop()

    def _do_download():
        for domain in ["dm.terabox.app", "www.terabox.app", "www.1024tera.com", "terabox.app"]:
            urls_to_try = []
            if encoded_path:
                urls_to_try.append(f"https://{domain}/rest/2.0/pcs/file?method=download&path={encoded_path}&app_id=250528")
            if fs_id:
                urls_to_try.append(f"https://{domain}/api/download?fid_list=[{fs_id}]&type=dlink&app_id=250528&web=1&channel=dubox&clienttype=0")

            session = requests.Session()
            for stream_url in urls_to_try:
                try:
                    with session.get(stream_url, headers=headers, stream=True, timeout=25, allow_redirects=True) as r:
                        if r.status_code not in (200, 206):
                            continue
                        total_bytes = int(r.headers.get("content-length", filesize_expected) or 0)
                        downloaded = 0
                        start_time = time.time()
                        last_update = 0
                        chunk_size = 1024 * 1024

                        with open(dest_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=chunk_size):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                downloaded += len(chunk)

                                now = time.time()
                                if progress_updater and (now - last_update > 2.0 or (total_bytes and downloaded >= total_bytes)):
                                    last_update = now
                                    elapsed = max(now - start_time, 0.1)
                                    speed = downloaded / elapsed
                                    speed_str = f"{format_bytes(speed)}/s"
                                    if total_bytes > 0:
                                        pct = int((downloaded / total_bytes) * 100)
                                        rem = max(total_bytes - downloaded, 0)
                                        eta = f"{int(rem / max(speed, 1))}s"
                                        msg = f"📥 **Downloading (Direct Stream): {pct}%** `[{format_bytes(downloaded)} / {format_bytes(total_bytes)}]` @ `{speed_str}` | ETA: `{eta}`"
                                    else:
                                        msg = f"📥 **Downloading (Direct Stream):** `{format_bytes(downloaded)}` @ `{speed_str}`"
                                    try:
                                        if asyncio.iscoroutinefunction(progress_updater):
                                            asyncio.run_coroutine_threadsafe(progress_updater(msg), loop)
                                        else:
                                            res = progress_updater(msg)
                                            if asyncio.iscoroutine(res):
                                                asyncio.run_coroutine_threadsafe(res, loop)
                                    except Exception:
                                        pass

                        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024:
                            return True, dest_path, None
                except Exception as ex:
                    logger.debug(f"[Direct Stream] {stream_url} error: {ex}")
                    if os.path.exists(dest_path):
                        remove_file_safely(dest_path)

        return False, None, "Direct account PCS streaming returned no valid data."

    return await asyncio.to_thread(_do_download)


async def resolve_and_download_account_video(
    video_item: Dict[str, Any],
    progress_updater: Optional[Callable[[str], Any]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads an account video file using a robust 3-stage resolver pipeline:
      Stage 1: Telegram Bot / MiniApp Resolver (via share link)
      Stage 2: Direct Authenticated Account Stream (Cookie / PCS)
      Stage 3: Backup Browser Engine (Puppeteer / Xvfb GUI Crawler)

    Every stage displays real-time progress, download speed, and ETA.
    """
    filename = _clean_filename(video_item.get("filename") or "terabox_video.mp4")
    dest_path = str(DOWNLOAD_DIR / filename)
    filesize_expected = int(video_item.get("size") or 0)
    fs_id = video_item.get("fs_id")
    remote_path = video_item.get("path", "")
    cookie_header = get_account_cookie_header()

    # ---------------------------------------------------------
    # STAGE 1: Telegram Bot / MiniApp Resolver (via share link)
    # ---------------------------------------------------------
    share_url = video_item.get("share_url")
    if not share_url and fs_id and cookie_header:
        await _notify(progress_updater, "🤖 **[Stage 1/3] Telegram Bot Resolver:** Generating active share link from account...")
        s_ok, s_link, s_err = await asyncio.to_thread(
            create_account_share_link_sync,
            fs_id=fs_id,
            cookie_header=cookie_header,
        )
        if s_ok and s_link:
            share_url = s_link
            logger.info(f"[TeraBox Account] Created share link for {filename}: {share_url}")
        else:
            logger.warning(f"[TeraBox Account] Share link generation failed for {filename}: {s_err}")

    if share_url:
        await _notify(progress_updater, f"🤖 **[Stage 1/3] Telegram Bot Resolver:** Querying MiniApp API (`{share_url[:42]}...`)...")
        from terabox_downloader import _resolve_via_miniapp_api, _stream_miniapp_download
        ma_success, ma_info, ma_err = await _resolve_via_miniapp_api(share_url)
        if ma_success and ma_info and ma_info.get("download_url"):
            actual_fsize = ma_info.get("filesize", filesize_expected)
            await _notify(progress_updater, f"📥 **[Stage 1/3] Telegram Bot Resolver:** Stream captured! Downloading {format_bytes(actual_fsize)}...")
            try:
                dl_ok = await _stream_miniapp_download(
                    download_url=ma_info["download_url"],
                    dest_path=dest_path,
                    filesize_expected=actual_fsize,
                    progress_updater=progress_updater,
                    stage_label="Bot Resolver",
                )
                if dl_ok and os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024:
                    logger.info(f"[TeraBox Account] Stage 1 success: {filename} downloaded via Telegram Bot Resolver.")
                    return True, dest_path, ma_info or video_item, None
            except Exception as dl_ex:
                logger.warning(f"[TeraBox Account] Stage 1 stream error: {dl_ex}")
                remove_file_safely(dest_path)
        else:
            logger.warning(f"[TeraBox Account] Stage 1 failed ({ma_err}). Moving to Stage 2...")
            await _notify(progress_updater, f"⚠️ **Telegram Bot Resolver:** `{ma_err or 'Resolution failed'}`\n🔄 *Switching to Stage 2 (Direct Account Stream)...*")

    # ---------------------------------------------------------
    # STAGE 2: Direct Authenticated Account Stream (Cookie / PCS)
    # ---------------------------------------------------------
    if cookie_header and (remote_path or fs_id):
        await _notify(progress_updater, "🍪 **[Stage 2/3] Direct Account Stream:** Connecting to TeraBox PCS API...")
        pcs_ok, pcs_file, pcs_err = await stream_cookie_account_download(
            remote_path=remote_path,
            fs_id=fs_id,
            dest_path=dest_path,
            filesize_expected=filesize_expected,
            cookie_header=cookie_header,
            progress_updater=progress_updater,
        )
        if pcs_ok and pcs_file and os.path.exists(pcs_file):
            logger.info(f"[TeraBox Account] Stage 2 success: {filename} downloaded via Direct Account Stream.")
            return True, pcs_file, video_item, None
        else:
            logger.warning(f"[TeraBox Account] Stage 2 failed ({pcs_err}). Moving to Stage 3...")
            await _notify(progress_updater, f"⚠️ **Direct Account Stream:** `{pcs_err or 'Failed'}`\n🔄 *Switching to Stage 3 (Backup Browser Crawler)...*")

    # ---------------------------------------------------------
    # STAGE 3: Backup Browser Engine (Puppeteer / Xvfb GUI Crawler)
    # ---------------------------------------------------------
    if share_url:
        await _notify(progress_updater, "🌐 **[Stage 3/3] Backup Browser Resolver:** Launching Headless Chromium in Xvfb...")
        from terabox_downloader import _download_via_node_crawler
        crawler_ok, crawler_file, crawler_info, crawler_err = await _download_via_node_crawler(
            url=share_url,
            output_dir=DOWNLOAD_DIR,
            download_id=f"acc_{fs_id}",
            progress_updater=progress_updater,
        )
        if crawler_ok and crawler_file and os.path.exists(crawler_file):
            logger.info(f"[TeraBox Account] Stage 3 success: {filename} downloaded via Browser Crawler.")
            return True, crawler_file, crawler_info or video_item, None
        else:
            logger.error(f"[TeraBox Account] Stage 3 crawler failed: {crawler_err}")
            await _notify(progress_updater, f"❌ **Backup Browser Resolver:** `{crawler_err or 'Failed'}`")

    return False, None, None, f"All 3 resolvers failed for '{filename}'."
