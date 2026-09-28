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

# Preferred TeraBox API domain endpoints (with fallback)
TERABOX_API_DOMAINS = [
    "www.1024tera.com",
    "www.terabox.app",
    "www.terabox.com",
    "1024tera.com",
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


def _get_api_headers(cookie_header: str) -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.terabox.app/main?category=all",
        "Origin": "https://www.terabox.app",
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

    headers = _get_api_headers(cookie_header)
    all_videos = []
    seen_fs_ids = set()

    working_domain = None
    # 1. Identify working domain from candidates
    for domain in TERABOX_API_DOMAINS:
        test_url = f"https://{domain}/api/categorylist?category=1&page=1&num=1&app_id=250528&web=1"
        try:
            r = requests.get(test_url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("errno") == 0:
                    working_domain = domain
                    logger.info(f"[TeraBox Account] Connected using domain: {domain}")
                    break
                elif data.get("errno") in (105, -6):
                    return {
                        "success": False,
                        "total_count": 0,
                        "total_size": 0,
                        "total_size_formatted": "0 B",
                        "videos": [],
                        "error": "TeraBox cookie is expired or unauthorized (errno 105). Please provide a fresh cookie.",
                    }
        except Exception as e:
            logger.debug(f"[TeraBox Account] Domain {domain} failed: {e}")

    if not working_domain:
        working_domain = TERABOX_API_DOMAINS[0]

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


async def resolve_and_download_account_video(
    video_item: Dict[str, Any],
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads an account video file using the bot's existing TeraBox resolver
    or direct authenticated streaming.
    Returns: (success, downloaded_filepath, file_info, error_msg)
    """
    filename = _clean_filename(video_item.get("filename") or "terabox_video.mp4")
    dest_path = str(DOWNLOAD_DIR / filename)
    filesize_expected = video_item.get("size", 0)
    fs_id = video_item.get("fs_id")
    remote_path = video_item.get("path", "")

    # Priority 1: Check if video_item has an existing share URL
    share_url = video_item.get("share_url")
    if share_url:
        logger.info(f"[TeraBox Account] Downloading via existing share URL resolver: {share_url}")
        success, downloaded_file, dl_info, error_msg = await download_terabox_media(
            share_url,
            quality="best",
            progress_updater=progress_updater,
        )
        if success and downloaded_file and os.path.exists(downloaded_file):
            return True, downloaded_file, dl_info or video_item, None

    # Priority 2: Direct authenticated PCS download using account cookie
    cookie_header = get_account_cookie_header()
    if cookie_header and (remote_path or fs_id):
        logger.info(f"[TeraBox Account] Downloading via direct authenticated cookie stream: {filename}")
        headers = _get_api_headers(cookie_header)

        # Try PCS file download URL
        for domain in TERABOX_API_DOMAINS:
            encoded_path = requests.utils.quote(remote_path)
            stream_url = (
                f"https://{domain}/rest/2.0/pcs/file"
                f"?method=download&path={encoded_path}&app_id=250528"
            )
            def _stream_cookie():
                session = requests.Session()
                with session.get(stream_url, headers=headers, stream=True, timeout=30) as r:
                    if r.status_code not in (200, 206):
                        return False, f"HTTP {r.status_code}"
                    total_bytes = int(r.headers.get("content-length", filesize_expected) or 0)
                    downloaded = 0
                    chunk_size = 1024 * 1024
                    with open(dest_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                    return True, None

            try:
                ok, err = await asyncio.to_thread(_stream_cookie)
                if ok and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                    logger.info(f"[TeraBox Account] Downloaded {filename} successfully ({format_bytes(os.path.getsize(dest_path))})")
                    return True, dest_path, video_item, None
            except Exception as e:
                logger.debug(f"[TeraBox Account] Direct stream via {domain} failed: {e}")

    # Priority 3: Fallback using existing bot download_terabox_media if a link is available
    fallback_link = f"https://www.terabox.app/s/file?fs_id={fs_id}" if fs_id else None
    if fallback_link:
        success, downloaded_file, dl_info, error_msg = await download_terabox_media(
            fallback_link,
            quality="best",
            progress_updater=progress_updater,
        )
        if success and downloaded_file and os.path.exists(downloaded_file):
            return True, downloaded_file, dl_info or video_item, None

    return False, None, None, f"Failed to download video file '{filename}' from TeraBox account."
