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
import shutil
import threading
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import secrets

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


_cached_js_token: Optional[str] = None
_token_timestamp: float = 0.0


def _get_js_token(cookie_header: str, force_refresh: bool = False) -> Optional[str]:
    """
    Fetches or returns cached jsToken needed for TeraBox share link generation.
    Scrapes the token from the TeraBox web main HTML.
    """
    global _cached_js_token, _token_timestamp
    now = time.time()
    if not force_refresh and _cached_js_token and (now - _token_timestamp < 3600):
        return _cached_js_token

    domains = ["dm.terabox.app", "www.terabox.app", "terabox.app"]
    for dom in domains:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Cookie": cookie_header,
            "Referer": f"https://{dom}/main?category=all",
            "Origin": f"https://{dom}",
        }
        try:
            r = requests.get(f"https://{dom}/main", headers=headers, timeout=12)
            if r.status_code == 200:
                m = re.search(r'"jsToken":\s*"([^"]+)"', r.text)
                if m:
                    _cached_js_token = m.group(1)
                    _token_timestamp = now
                    logger.info(f"[TeraBox Share] Successfully extracted jsToken from {dom}.")
                    return _cached_js_token
        except Exception as e:
            logger.debug(f"[TeraBox Share] Failed to fetch jsToken from {dom}: {e}")

    return _cached_js_token


def create_account_share_link_sync(
    fs_id: Any,
    cookie_header: str,
    path: Optional[str] = None,
    filename: Optional[str] = None,
    domain: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Creates an active TeraBox share link for an account video using cookie authentication
    via the /share/pset API with jsToken and dp-logid.
    """
    js_token = _get_js_token(cookie_header)
    if not js_token:
        js_token = _get_js_token(cookie_header, force_refresh=True)

    # Prepare path list parameter
    target_path = path or (f"/{filename}" if filename else f"/video_{fs_id}.mp4")
    path_list_json = json.dumps([target_path])

    domains_to_try = []
    if domain:
        domains_to_try.append(domain)
    for d in ["dm.terabox.app", "www.terabox.app", "www.1024terabox.com", "terabox.app"]:
        if d not in domains_to_try:
            domains_to_try.append(d)

    for attempt in range(2):
        dp_logid = secrets.token_hex(16).upper()

        for d in domains_to_try:
            url = (
                f"https://{d}/share/pset"
                f"?app_id=250528&web=1&channel=dubox&clienttype=0"
                f"&jsToken={js_token or ''}&dp-logid={dp_logid}"
            )
            post_data = {
                "app_id": "250528",
                "web": "1",
                "channel": "dubox",
                "clienttype": "0",
                "app": "universe",
                "schannel": "0",
                "channel_list": "[0]",
                "period": "0",
                "path_list": path_list_json,
                "fid_list": f"[{fs_id}]",
                "pwd": "",
                "public": "1",
                "scene": "",
            }
            req_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Cookie": cookie_header,
                "Referer": f"https://{d}/",
                "Origin": f"https://{d}",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            try:
                r = requests.post(url, data=post_data, headers=req_headers, timeout=12)
                if r.status_code == 200:
                    try:
                        res = r.json()
                    except Exception:
                        continue

                    errno = res.get("errno")
                    if errno == 0:
                        link = res.get("link") or res.get("shorturl")
                        if link:
                            if not link.startswith("http"):
                                link = f"https://1024terabox.com/s/{link}"
                            logger.info(f"[TeraBox Share] Successfully created share link: {link} (fs_id: {fs_id})")
                            return True, link, None
                    elif errno in (-6, 105):
                        logger.warning(f"[TeraBox Share] Domain {d} returned auth error errno {errno}")
                        if attempt == 0:
                            js_token = _get_js_token(cookie_header, force_refresh=True)
                            break
                    else:
                        logger.debug(f"[TeraBox Share] Domain {d} returned errno {errno}: {res.get('show_msg')}")
            except Exception as e:
                logger.debug(f"[TeraBox Share] Domain {d} error: {e}")

    return False, None, "Could not generate share link from TeraBox account."


async def create_account_share_link(
    fs_id: Any,
    cookie_header: str,
    path: Optional[str] = None,
    filename: Optional[str] = None,
    domain: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Async wrapper for create_account_share_link_sync."""
    return await asyncio.to_thread(
        create_account_share_link_sync,
        fs_id,
        cookie_header,
        path,
        filename,
        domain,
    )


async def stream_cookie_account_download(
    remote_path: str,
    fs_id: Any,
    dest_path: str,
    filesize_expected: int,
    cookie_header: str,
    progress_updater: Optional[Callable[[str], Any]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Downloads file directly via TeraBox PCS / rest API using unthrottled mobile headers
    and concurrent multi-stream Range chunk downloading (8 parallel streams), achieving 10-25 MB/s.
    """
    mobile_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; SM-G998B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36 TeraBox/3.26.1"
        ),
        "Accept": "*/*",
        "Cookie": cookie_header,
        "Connection": "keep-alive",
    }
    encoded_path = requests.utils.quote(remote_path) if remote_path else ""

    loop = asyncio.get_running_loop()

    def _send_progress(msg: str):
        if not progress_updater:
            return
        try:
            if asyncio.iscoroutinefunction(progress_updater):
                asyncio.run_coroutine_threadsafe(progress_updater(msg), loop)
            else:
                res = progress_updater(msg)
                if asyncio.iscoroutine(res):
                    asyncio.run_coroutine_threadsafe(res, loop)
        except Exception:
            pass

    def _do_download():
        # Prefer regional download manager (dm) first, then mirror domains
        domains = ["dm.terabox.app", "dm.terabox.com", "www.1024tera.com", "www.terabox.app", "terabox.app"]
        session = requests.Session()

        final_stream_url = None
        total_bytes = 0
        accept_ranges = False

        # 1. Probe candidate domains to obtain final redirected CDN storage URL
        for domain in domains:
            if cancel_checker and cancel_checker():
                return False, None, "Download cancelled by user."

            candidate_urls = []
            if encoded_path:
                candidate_urls.append(f"https://{domain}/rest/2.0/pcs/file?method=download&path={encoded_path}&app_id=250528")

            for stream_url in candidate_urls:
                try:
                    with session.get(stream_url, headers=mobile_headers, stream=True, timeout=15, allow_redirects=True) as probe:
                        if probe.status_code in (200, 206):
                            content_type = probe.headers.get("Content-Type", "")
                            # Reject JSON error bodies returned with HTTP 200
                            if "application/json" in content_type:
                                continue

                            final_stream_url = probe.url
                            total_bytes = int(probe.headers.get("content-length") or filesize_expected or 0)
                            ar = probe.headers.get("accept-ranges", "").lower()
                            accept_ranges = (ar == "bytes") or ("ddata" in final_stream_url)
                            logger.info(f"[TeraBox MultiStream] Resolved stream CDN: {final_stream_url[:70]}... (Size: {format_bytes(total_bytes)}, Ranges: {accept_ranges})")
                            break
                except Exception as ex:
                    logger.debug(f"[TeraBox MultiStream] Probe {stream_url} failed: {ex}")

            if final_stream_url:
                break

        if not final_stream_url:
            return False, None, "Could not resolve valid TeraBox storage CDN stream URL."

        # 2. If file supports Range requests and is >= 3 MB, use 8 concurrent Range workers
        if accept_ranges and total_bytes >= 3 * 1024 * 1024:
            num_workers = min(8, max(2, total_bytes // (2 * 1024 * 1024)))
            chunk_size = total_bytes // num_workers
            parts = [f"{dest_path}.part{i}" for i in range(num_workers)]

            downloaded_bytes = [0]
            download_lock = threading.Lock()
            last_update = [0.0]
            start_time = time.time()

            def _dl_worker(worker_idx: int, start_b: int, end_b: int):
                part_path = parts[worker_idx]
                worker_headers = dict(mobile_headers)
                worker_headers["Range"] = f"bytes={start_b}-{end_b}"
                worker_session = requests.Session()

                with worker_session.get(final_stream_url, headers=worker_headers, stream=True, timeout=35) as resp:
                    if resp.status_code not in (200, 206):
                        raise RuntimeError(f"MultiStream worker {worker_idx} received HTTP {resp.status_code}")

                    with open(part_path, "wb") as pf:
                        for chunk in resp.iter_content(chunk_size=131072):
                            if cancel_checker and cancel_checker():
                                raise RuntimeError("Transfer cancelled by user.")
                            if not chunk:
                                continue
                            pf.write(chunk)
                            with download_lock:
                                downloaded_bytes[0] += len(chunk)
                                now = time.time()
                                if progress_updater and (now - last_update[0] >= 1.5 or downloaded_bytes[0] >= total_bytes):
                                    last_update[0] = now
                                    elapsed = max(now - start_time, 0.1)
                                    speed = downloaded_bytes[0] / elapsed
                                    pct = int((downloaded_bytes[0] / total_bytes) * 100) if total_bytes > 0 else 0
                                    rem = max(total_bytes - downloaded_bytes[0], 0)
                                    eta = f"{int(rem / max(speed, 1))}s"
                                    msg = (
                                        f"⚡ **Downloading (Multi-Stream {num_workers}x): {pct}%** "
                                        f"`[{format_bytes(downloaded_bytes[0])} / {format_bytes(total_bytes)}]` "
                                        f"@ `{format_bytes(speed)}/s` | ETA: `{eta}`"
                                    )
                                    _send_progress(msg)
                return part_path

            try:
                with ThreadPoolExecutor(max_workers=num_workers) as pool:
                    futures = []
                    for i in range(num_workers):
                        sb = i * chunk_size
                        eb = sb + chunk_size - 1 if i < num_workers - 1 else total_bytes - 1
                        futures.append(pool.submit(_dl_worker, i, sb, eb))

                    for fut in as_completed(futures):
                        fut.result()

                # Concatenate all parts into dest_path
                with open(dest_path, "wb") as out_f:
                    for p in parts:
                        with open(p, "rb") as in_f:
                            shutil.copyfileobj(in_f, out_f, length=1024 * 1024)
                        remove_file_safely(p)

                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024:
                    return True, dest_path, None

            except Exception as e:
                logger.warning(f"[TeraBox MultiStream] Parallel download failed: {e}. Falling back to single stream...")
                for p in parts:
                    remove_file_safely(p)
                if os.path.exists(dest_path):
                    remove_file_safely(dest_path)

        # 3. Fallback: Single-stream download with mobile headers
        try:
            with session.get(final_stream_url, headers=mobile_headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or total_bytes or filesize_expected or 0)
                downloaded = 0
                start_t = time.time()
                last_up = 0

                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        if cancel_checker and cancel_checker():
                            raise RuntimeError("Transfer cancelled by user.")
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        now = time.time()
                        if progress_updater and (now - last_up >= 1.5 or (total and downloaded >= total)):
                            last_up = now
                            elapsed = max(now - start_t, 0.1)
                            speed = downloaded / elapsed
                            speed_str = f"{format_bytes(speed)}/s"
                            if total > 0:
                                pct = int((downloaded / total) * 100)
                                rem = max(total - downloaded, 0)
                                eta = f"{int(rem / max(speed, 1))}s"
                                msg = f"📥 **Downloading (Mobile Stream): {pct}%** `[{format_bytes(downloaded)} / {format_bytes(total)}]` @ `{speed_str}` | ETA: `{eta}`"
                            else:
                                msg = f"📥 **Downloading (Mobile Stream):** `{format_bytes(downloaded)}` @ `{speed_str}`"
                            _send_progress(msg)

                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024:
                    return True, dest_path, None
        except Exception as fallback_err:
            logger.error(f"[TeraBox SingleStream] Fallback download failed: {fallback_err}")
            if os.path.exists(dest_path):
                remove_file_safely(dest_path)

        return False, None, "Direct account PCS streaming returned no valid data."

    return await asyncio.to_thread(_do_download)


async def resolve_and_download_account_video(
    video_item: Dict[str, Any],
    progress_updater: Optional[Callable[[str], Any]] = None,
    cancel_checker: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads an account video using 2 clean methods:
      1. Main Method: High-Speed MiniApp API via private account share link
         (automatically tries multiple TeraBox domain variations like 1024terabox.com).
      2. Backup Method: Virtual Browser Engine (Node.js Puppeteer Stealth Crawler in Xvfb).
    """
    filename = _clean_filename(video_item.get("filename") or "terabox_video.mp4")
    dest_path = str(DOWNLOAD_DIR / filename)
    filesize_expected = int(video_item.get("size") or 0)
    fs_id = video_item.get("fs_id")
    cookie_header = get_account_cookie_header()

    # Step 1: Ensure an active share link exists for this private account video
    share_url = video_item.get("share_url")
    if not share_url and cookie_header and fs_id:
        await _notify(progress_updater, "🔗 **[Main Method] Generating private share link...**")
        try:
            link_ok, created_link, link_err = await create_account_share_link(
                fs_id=int(fs_id),
                cookie_header=cookie_header,
                path=video_item.get("path"),
                filename=filename,
            )
            if link_ok and created_link:
                video_item["share_url"] = created_link
                share_url = created_link
                logger.info(f"[TeraBox Account] Created share link for fs_id {fs_id}: {share_url}")
            else:
                logger.warning(f"[TeraBox Account] Share link creation failed for fs_id {fs_id}: {link_err}")
        except Exception as ce_err:
            logger.error(f"[TeraBox Account] Error creating share link for {fs_id}: {ce_err}")

    # Build candidate URLs with alternative TeraBox domains for the same surl
    from terabox_downloader import (
        extract_surl,
        _resolve_via_miniapp_api,
        _stream_miniapp_download,
        _download_via_node_crawler,
    )

    candidate_urls = []
    if share_url:
        candidate_urls.append(share_url)
        surl = extract_surl(share_url)
        if surl:
            clean_surl = surl if surl.startswith("1") else f"1{surl}"
            alt_domains = [
                "1024terabox.com",
                "www.terabox.app",
                "terabox.app",
                "teraboxlink.com",
                "1024tera.com",
                "www.terabox.com",
                "freeterabox.com",
            ]
            for ad in alt_domains:
                cand_u = f"https://{ad}/s/{clean_surl}"
                if cand_u not in candidate_urls:
                    candidate_urls.append(cand_u)

    # ---------------------------------------------------------
    # METHOD 1: Main Method (High-Speed MiniApp API)
    # ---------------------------------------------------------
    if candidate_urls:
        await _notify(progress_updater, "🤖 **[Main Method] Resolving video stream...**")
        ma_success = False
        ma_info = None
        ma_err = None

        for cand_u in candidate_urls:
            if cancel_checker and cancel_checker():
                return False, None, None, "Cancelled by user"
            ma_success, ma_info, ma_err = await _resolve_via_miniapp_api(cand_u)
            if ma_success and ma_info and ma_info.get("download_url"):
                logger.info(f"[TeraBox Account] Main Method resolved using candidate: {cand_u}")
                break

        if ma_success and ma_info and ma_info.get("download_url"):
            actual_fsize = ma_info.get("filesize", filesize_expected)
            await _notify(progress_updater, f"📥 **[Main Method] Stream captured! Connecting to CDN ({format_bytes(actual_fsize)})...**")
            try:
                dl_ok = await _stream_miniapp_download(
                    download_url=ma_info["download_url"],
                    dest_path=dest_path,
                    filesize_expected=actual_fsize,
                    progress_updater=progress_updater,
                    stage_label="Main Method",
                    cancel_checker=cancel_checker,
                )
                if dl_ok and os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024:
                    actual_size = os.path.getsize(dest_path)
                    ma_info["filesize"] = actual_size
                    ma_info["filepath"] = dest_path
                    logger.info(f"[TeraBox Account] Main Method success: {filename} ({format_bytes(actual_size)})")
                    return True, dest_path, ma_info or video_item, None
            except Exception as dl_ex:
                logger.warning(f"[TeraBox Account] Main Method stream error: {dl_ex}")
                remove_file_safely(dest_path)
        else:
            logger.warning(f"[TeraBox Account] Main Method resolution failed across candidate domains: {ma_err}")
            await _notify(progress_updater, f"⚠️ **Main Method Failed:** `{ma_err or 'Resolution failed'}`\n🔄 *Switching to Backup Method...*")

    # ---------------------------------------------------------
    # METHOD 2: Backup Method (Virtual Browser Engine / Puppeteer in Xvfb)
    # ---------------------------------------------------------
    if share_url:
        if cancel_checker and cancel_checker():
            return False, None, None, "Cancelled by user"
        await _notify(progress_updater, "🌐 **[Backup Method] Launching virtual browser session...**")
        crawler_ok, crawler_file, crawler_info, crawler_err = await _download_via_node_crawler(
            url=share_url,
            output_dir=DOWNLOAD_DIR,
            download_id=f"acc_{fs_id}",
            progress_updater=progress_updater,
        )
        if crawler_ok and crawler_file and os.path.exists(crawler_file):
            logger.info(f"[TeraBox Account] Backup Method success: {filename} downloaded via browser crawler.")
            return True, crawler_file, crawler_info or video_item, None
        else:
            logger.error(f"[TeraBox Account] Backup Method crawler failed: {crawler_err}")
            await _notify(progress_updater, f"❌ **Backup Method Failed:** `{crawler_err or 'Failed'}`")

    return False, None, None, f"Both Main Method and Backup Method failed for '{filename}'."
