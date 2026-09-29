import os
import re
import sys
import time
import json
import uuid
import socket
import shutil
import tempfile
import asyncio
import logging
import subprocess
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable
from urllib.parse import urlparse, parse_qs
import aiohttp
import requests
import yt_dlp

from config import DOWNLOAD_DIR, BASE_DIR
from downloader import format_bytes, remove_file_safely

logger = logging.getLogger(__name__)

# Path to project .env file
ENV_FILE = BASE_DIR / ".env"


def get_terabox_token() -> str:
    """Returns the current TeraBox MiniApp Bearer token directly from .env or os.environ."""
    tok = os.environ.get("TERABOX_BEARER_TOKEN", "").strip()
    if not tok and ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line_clean = line.strip()
                    if line_clean.startswith("TERABOX_BEARER_TOKEN="):
                        val = line_clean.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            tok = val
                            os.environ["TERABOX_BEARER_TOKEN"] = tok
                            break
        except Exception as e:
            logger.error(f"[TeraBox] Error reading token from .env: {e}")

    if tok:
        return tok if tok.startswith("Bearer ") else f"Bearer {tok}"
    return ""


def save_terabox_token(token: str) -> bool:
    """Saves and updates the TeraBox MiniApp Bearer token directly in the .env file."""
    clean_val = token.strip()
    if clean_val.startswith("Bearer "):
        full_token = clean_val
    else:
        full_token = f"Bearer {clean_val}"

    os.environ["TERABOX_BEARER_TOKEN"] = full_token

    try:
        lines = []
        found = False
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        for line in lines:
            if line.strip().startswith("TERABOX_BEARER_TOKEN="):
                new_lines.append(f"TERABOX_BEARER_TOKEN={full_token}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"TERABOX_BEARER_TOKEN={full_token}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        logger.info("[TeraBox] Updated TERABOX_BEARER_TOKEN in .env successfully.")
        return True
    except Exception as e:
        logger.error(f"[TeraBox] Failed to write token to .env: {e}")
        return False


def _clean_filename(name: str) -> str:
    """Sanitizes filename for safe filesystem storage."""
    safe = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return safe or "terabox_video.mp4"


async def _resolve_via_miniapp_api(terabox_url: str) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Directly resolves TeraBox links via the high-speed MiniApp API (apiwala.teradownloader.pro).
    """
    # Ensure token is valid (auto-refreshes via Telethon if missing or older than TOKEN_MAX_AGE_HOURS)
    try:
        from telethon_extractor import ensure_valid_token
        ok, valid_token, _ = await ensure_valid_token("terabox")
        if ok and valid_token:
            token = valid_token
        else:
            token = get_terabox_token()
    except Exception as e:
        logger.debug(f"[TeraBox] ensure_valid_token notice: {e}")
        token = get_terabox_token()

    if not token:
        return False, None, "TeraBox MiniApp token is not set. Use `/eztera` or `/setterabox <token>` in chat to configure it."

    logger.info(f"[TeraBox] Querying MiniApp Direct API for {terabox_url[:80]}...")

    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.6",
        "authorization": token,
        "content-type": "application/json",
        "origin": "https://twa.teradownloader.pro",
        "referer": "https://twa.teradownloader.pro/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "x-bot-id": "terabox_player",
    }

    payload = {
        "link": terabox_url,
        "dir_path": "",
        "page": 1,
    }

    def _post():
        return requests.post(
            "https://apiwala.teradownloader.pro/api/terabox/new",
            json=payload,
            headers=headers,
            timeout=15,
        )

    try:
        resp = await asyncio.to_thread(_post)
        if resp.status_code == 401:
            logger.warning("[TeraBox] MiniApp token expired (HTTP 401). Triggering on-demand Telethon refresh...")
            try:
                from telethon_extractor import ensure_valid_token
                ok_ref, new_tok, _ = await ensure_valid_token("terabox", force_refresh=True)
                if ok_ref and new_tok:
                    headers["authorization"] = new_tok
                    resp = await asyncio.to_thread(_post)
            except Exception as ref_err:
                logger.error(f"[TeraBox] Auto-refresh on 401 failed: {ref_err}")

        if resp.status_code == 401:
            logger.warning("[TeraBox] MiniApp token unauthorized or expired after retry.")
            return False, None, "TeraBox MiniApp token is unauthorized or expired. Auto-refresh failed; please run `/eztera`."

        if resp.status_code != 200:
            logger.warning(f"[TeraBox] MiniApp API returned HTTP {resp.status_code}")
            return False, None, f"MiniApp API returned HTTP {resp.status_code}"

        res_data = resp.json()
        if not res_data.get("success"):
            err_msg = res_data.get("message") or "MiniApp reported unsuccessful response."
            logger.warning(f"[TeraBox] MiniApp resolution error: {err_msg}")
            return False, None, err_msg

        items = res_data.get("data", [])
        if not items:
            return False, None, "No files found in TeraBox link."

        # Pick first file item that has a download link
        selected = None
        for it in items:
            if not it.get("isDir") and it.get("downloadLink"):
                selected = it
                break
        if not selected:
            selected = items[0]

        download_link = selected.get("downloadLink") or selected.get("streamUrl")
        if not download_link:
            return False, None, "No download link found in MiniApp response."

        filename = _clean_filename(selected.get("fileName") or "terabox_video.mp4")
        filesize = int(selected.get("fileSize") or 0)
        thumb = selected.get("thumbnail")

        info = {
            "title": filename,
            "filename": filename,
            "filesize": filesize,
            "download_url": download_link,
            "stream_url": selected.get("streamUrl"),
            "thumbnail": thumb,
            "uploader": "TeraBox",
            "extractor": "terabox_miniapp_api",
        }
        logger.info(f"[TeraBox] MiniApp resolved: {filename} ({format_bytes(filesize)})")
        return True, info, None

    except Exception as e:
        logger.error(f"[TeraBox] MiniApp request exception: {e}")
        return False, None, str(e)


async def _stream_miniapp_download(
    download_url: str,
    dest_path: str,
    filesize_expected: int,
    progress_updater: Optional[Callable[[str], None]] = None,
    stage_label: str = "Telegram Bot Resolver",
) -> bool:
    """Streams file directly to disk in 1MB chunks with real-time speed, ETA, and progress updates."""
    def _do_stream():
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        with session.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total_bytes = int(r.headers.get("content-length", filesize_expected) or 0)

            downloaded_bytes = 0
            start_time = time.time()
            last_update = 0
            chunk_size = 1024 * 1024

            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    now = time.time()
                    if progress_updater and (now - last_update > 2.0 or (total_bytes and downloaded_bytes >= total_bytes)):
                        last_update = now
                        elapsed = max(now - start_time, 0.1)
                        speed = downloaded_bytes / elapsed
                        speed_str = f"{format_bytes(speed)}/s"
                        if total_bytes > 0:
                            pct = int((downloaded_bytes / total_bytes) * 100)
                            rem_bytes = max(total_bytes - downloaded_bytes, 0)
                            eta_sec = int(rem_bytes / max(speed, 1))
                            eta_str = f"{eta_sec}s"
                            msg = f"📥 **Downloading ({stage_label}): {pct}%** `[{format_bytes(downloaded_bytes)} / {format_bytes(total_bytes)}]` @ `{speed_str}` | ETA: `{eta_str}`"
                        else:
                            msg = f"📥 **Downloading ({stage_label}):** `{format_bytes(downloaded_bytes)}` @ `{speed_str}`"
                        try:
                            if asyncio.iscoroutinefunction(progress_updater):
                                asyncio.run_coroutine_threadsafe(progress_updater(msg), loop)
                            else:
                                res = progress_updater(msg)
                                if asyncio.iscoroutine(res):
                                    asyncio.run_coroutine_threadsafe(res, loop)
                        except Exception:
                            pass
            return True

    loop = asyncio.get_running_loop()
    await asyncio.to_thread(_do_stream)
    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 50 * 1024


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


def find_chrome_binary() -> Optional[str]:
    """Locates Chromium/Chrome binary on the local system."""
    candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/snap/bin/chromium",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if os.path.isfile(c) or shutil.which(c):
            return c
    return None


def parse_raw_cookies_for_cdp(raw_cookie: Optional[str]) -> list:
    """Converts Netscape or key=value cookies into CDP Network.setCookies list."""
    if not raw_cookie:
        return []
    cdp_cookies = []
    for line in raw_cookie.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, path, secure_str = parts[0], parts[2], parts[3]
            secure = secure_str.lower() == "true"
            expires_raw = parts[4]
            expires = int(float(expires_raw)) if expires_raw.replace(".", "", 1).isdigit() else None
            name, value = parts[5], parts[6]
            cookie_dict = {"name": name, "value": value, "domain": domain, "path": path, "secure": secure}
            if expires and expires > 0:
                cookie_dict["expires"] = expires
            cdp_cookies.append(cookie_dict)
        elif "=" in line:
            kv = line.split("=", 1)
            cdp_cookies.append({
                "name": kv[0].strip(),
                "value": kv[1].strip().rstrip(";"),
                "domain": ".terabox.app",
                "path": "/",
            })
    return cdp_cookies


def _find_free_port() -> int:
    """Returns a random available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


async def _resolve_via_headless_browser(
    url: str,
    timeout_sec: int = 15,
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    Primary Browser Engine: Uses real headless Chromium with saved cookies.
    Bypasses dynamic token checks and 'need verify' restrictions by mimicking real browser behavior.
    """
    chrome_bin = find_chrome_binary()
    if not chrome_bin:
        return False, {}, "Chromium binary not found on system."

    port = _find_free_port()
    temp_dir = tempfile.mkdtemp(prefix="tb_headless_")

    env = os.environ.copy()
    if "DISPLAY" not in env:
        env["DISPLAY"] = ":99"

    chrome_args = [
        chrome_bin,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f"--user-data-dir={temp_dir}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "about:blank",
    ]

    logger.info(f"[_resolve_via_headless_browser] Launching headless browser on port {port}...")
    proc = subprocess.Popen(chrome_args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        await asyncio.sleep(1.2)
        raw_cookie = get_terabox_cookie()
        cdp_cookies = parse_raw_cookies_for_cdp(raw_cookie)

        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            ws_url = None
            for _ in range(12):
                try:
                    async with session.get(f"http://127.0.0.1:{port}/json/list") as resp:
                        if resp.status == 200:
                            tabs = await resp.json(content_type=None)
                            for t in tabs:
                                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                                    ws_url = t.get("webSocketDebuggerUrl")
                                    break
                            if ws_url:
                                break
                except Exception:
                    await asyncio.sleep(0.3)

            if not ws_url:
                return False, {}, "Could not connect to headless Chrome DevTools."

            async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as ws:
                await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
                await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})
                await ws.send_json({"id": 3, "method": "Runtime.enable", "params": {}})
                await ws.send_json({"id": 4, "method": "Browser.setDownloadBehavior", "params": {
                    "behavior": "allowAndName",
                    "downloadPath": temp_dir,
                    "eventsEnabled": True,
                }})
                if cdp_cookies:
                    await ws.send_json({"id": 5, "method": "Network.setCookies", "params": {"cookies": cdp_cookies}})
                    logger.info(f"[_resolve_via_headless_browser] Injected {len(cdp_cookies)} cookies into headless browser")

                logger.info(f"[_resolve_via_headless_browser] Navigating page to {url}...")
                await ws.send_json({"id": 6, "method": "Page.navigate", "params": {"url": url}})

                play_url = None
                title = "TeraBox_Video"
                thumbnail = None
                start_t = time.time()

                # Trigger button click asynchronously after 3.5s using exact DevTools selectors
                async def _click_download_button():
                    await asyncio.sleep(3.5)
                    click_js = """(() => {
                        const selectors = [
                            '.operate-row button.download-btn',
                            'button.download-btn',
                            'button.download-btn > span',
                            '.download-btn',
                        ];
                        let btn = null;
                        for (const sel of selectors) {
                            btn = document.querySelector(sel);
                            if (btn) break;
                        }
                        if (!btn) {
                            btn = Array.from(document.querySelectorAll('button, a, div, span')).find(el => {
                                const t = (el.innerText || '').trim().toLowerCase();
                                const c = (el.className || '');
                                return t === 'download' || t === 'download video' || c.includes('download-btn');
                            });
                        }
                        if (btn) {
                            if (btn.scrollIntoView) btn.scrollIntoView({ block: 'center', inline: 'center' });
                            btn.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                            btn.click();
                            return { success: true, text: btn.innerText, tag: btn.tagName, class: btn.className };
                        }
                        return { success: false, reason: 'Button not found' };
                    })()"""
                    await ws.send_json({"id": 50, "method": "Runtime.evaluate", "params": {"expression": click_js, "returnByValue": True}})

                asyncio.create_task(_click_download_button())

                while time.time() - start_t < timeout_sec:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=0.8)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            method = data.get("method", "")

                            if method == "Network.requestWillBeSent":
                                req_url = data.get("params", {}).get("request", {}).get("url", "")
                                if "ddata.terabox.app" in req_url or "-ddata." in req_url:
                                    play_url = req_url
                                    logger.info(f"[_resolve_via_headless_browser] Intercepted final CDN kul-ddata URL: {play_url[:80]}...")
                                    break
                                elif "d.terabox" in req_url or "d3.terabox" in req_url or "/file/" in req_url:
                                    play_url = req_url
                                    logger.info(f"[_resolve_via_headless_browser] Intercepted gateway download URL: {play_url[:80]}...")
                                elif "/share/streaming?" in req_url and ("fid=" in req_url or "sign=" in req_url):
                                    if not play_url:
                                        play_url = req_url
                                    logger.info(f"[_resolve_via_headless_browser] Intercepted streaming request: {req_url[:80]}...")

                            elif method == "Browser.downloadWillBegin":
                                dl_url = data.get("params", {}).get("url", "")
                                guid = data.get("params", {}).get("guid")
                                if guid:
                                    try:
                                        await ws.send_json({"id": 99, "method": "Browser.cancelDownload", "params": {"guid": guid}})
                                    except Exception:
                                        pass
                                if dl_url:
                                    play_url = dl_url
                                    logger.info(f"[_resolve_via_headless_browser] Browser download event URL: {play_url[:80]}...")
                                    if "ddata" in dl_url or "-ddata." in dl_url:
                                        break

                            elif method == "Network.responseReceived":
                                resp = data.get("params", {}).get("response", {})
                                resp_url = resp.get("url", "")
                                headers = resp.get("headers", {})
                                location = headers.get("location") or headers.get("Location")
                                if location and ("ddata" in location or "-ddata." in location):
                                    play_url = location
                                    logger.info(f"[_resolve_via_headless_browser] 🚀 Captured 302 Location redirect -> {play_url[:80]}...")
                                    break
                                elif "ddata.terabox.app" in resp_url or "-ddata." in resp_url:
                                    play_url = resp_url
                                    logger.info(f"[_resolve_via_headless_browser] Intercepted kul-ddata response: {play_url[:80]}...")
                                    break
                    except asyncio.TimeoutError:
                        pass

                # Extract page metadata via DOM
                eval_meta = """(() => {
                    return {
                        title: document.title.replace(' - Share Files Online & Send Larges Files with TeraBox', '').trim(),
                        thumb: (document.querySelector('video') || {}).poster || null
                    };
                })()"""
                await ws.send_json({"id": 60, "method": "Runtime.evaluate", "params": {"expression": eval_meta, "returnByValue": True}})
                for _ in range(3):
                    try:
                        m = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if m.type == aiohttp.WSMsgType.TEXT:
                            d = json.loads(m.data)
                            if d.get("id") == 60:
                                val = d.get("result", {}).get("result", {}).get("value", {})
                                if val.get("title"):
                                    title = val["title"]
                                thumbnail = val.get("thumb")
                                break
                    except Exception:
                        break

                if play_url:
                    logger.info(f"[_resolve_via_headless_browser] 🎉 Headless browser resolution SUCCESS: '{title}'")
                    return True, {
                        "id": "tb_" + str(int(time.time())),
                        "title": title,
                        "thumbnail": thumbnail,
                        "uploader": "TeraBox",
                        "play_url": play_url,
                        "original_url": url,
                        "duration": None,
                        "formats": [],
                        "engine": "headless_browser",
                    }, None

                return False, {}, "Headless browser could not intercept video stream URL."

    except Exception as e:
        logger.error(f"[_resolve_via_headless_browser] Error: {e}", exc_info=True)
        return False, {}, f"Headless browser error: {e}"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


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

    client_timeout = aiohttp.ClientTimeout(total=30, connect=10)

    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            headers=headers,
            timeout=client_timeout,
        ) as session:
            for ep in endpoints:
                try:
                    parsed_ep = urllib.parse.urlparse(ep)
                    ep_label = f"{parsed_ep.netloc}{parsed_ep.path}"
                    async with session.get(ep) as resp:
                        resp_text = await resp.text()
                        logger.info(f"[_resolve_via_gateway] {ep_label} -> HTTP {resp.status} | Body: {resp_text[:300]}")

                        if resp.status != 200:
                            diag_logs.append(f"• `{ep_label}`: `HTTP {resp.status}`")
                            continue

                        try:
                            data = json.loads(resp_text)
                        except Exception:
                            diag_logs.append(f"• `{ep_label}`: non-JSON `{resp_text[:80]}`")
                            continue

                        if not data:
                            diag_logs.append(f"• `{ep_label}`: empty JSON")
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
                                diag_logs.append(f"• `{ep_label}`: `HTTP {resp.status}` | `errno={errno}` (`{errmsg or 'need verify'}`)")
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
                                                    diag_logs.append(f"• `share/download`: `errno={dl_data.get('errno')}` (`{dl_data.get('errmsg', '')}`)")
                                    except Exception as dl_err:
                                        logger.warning(f"[_resolve_via_gateway] Step-2 download error: {dl_err}")
                                        diag_logs.append(f"• `share/download`: `{dl_err}`")

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
                                diag_logs.append(f"• `{ep_label}`: no `play_url` in response (`{resp_text[:60]}`)")
                except Exception as e:
                    logger.debug(f"Gateway endpoint {ep} failed: {e}")
                    diag_logs.append(f"• `{ep_label}`: `{e}`")
                    continue
    except Exception as session_err:
        diag_logs.append(f"• Session error: `{session_err}`")

    summary = "\n".join(diag_logs) if diag_logs else "All endpoints returned empty or failed"
    return False, {}, summary


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
    1. Priority Engine: Local Headless Chromium Crawler with user cookies.
    2. Secondary Engine: Cookie & Gateway REST architecture.
    3. Backup Engine: Hostinger resolver API.
    4. Interactive HITL Remote Browser Solver: If CAPTCHA is required.
    """
    if progress_updater:
        try:
            await progress_updater("🌐 Connecting to TeraBox via browser engine...")
        except Exception:
            pass

    # 1. First Priority: High-Speed Telegram MiniApp API (teradownloader.pro)
    if get_terabox_token():
        ma_success, ma_info, ma_err = await _resolve_via_miniapp_api(url)
        if ma_success and ma_info:
            logger.info("TeraBox link resolved successfully via high-speed MiniApp API engine.")
            return True, ma_info, None
        logger.warning(f"TeraBox MiniApp API resolver failed ({ma_err}). Falling back to browser engine...")

    # 2. Local Headless Chromium Engine
    hl_success, hl_info, hl_err = await _resolve_via_headless_browser(url, timeout_sec=15)
    if hl_success and hl_info:
        logger.info("TeraBox link resolved successfully via headless browser engine.")
        return True, hl_info, None

    logger.info(f"Headless browser resolver unavailable ({hl_err}). Trying Gateway REST engine...")

    # 2. Secondary Engine (Cookie & Gateway Architecture)
    gw_success, gw_info, gw_err = await _resolve_via_gateway(url)
    if gw_success and gw_info:
        logger.info("TeraBox link resolved successfully via primary Cookie/Gateway engine.")
        return True, gw_info, None

    logger.info(f"Primary Cookie/Gateway resolver unavailable ({gw_err}). Engaging Hostinger backup resolver...")

    # 3. Backup Engine (Hostinger API)
    err = None
    for attempt in range(1, max_retries + 1):
        success, info, err = await _resolve_via_hostinger(url)
        if success and info:
            logger.info("TeraBox link resolved successfully via backup Hostinger engine.")
            return True, info, None
        if "try again" in str(err).lower() and attempt < max_retries:
            await asyncio.sleep(2.5)
            continue
    raw_cookie = get_terabox_cookie()
    cookie_status = f"✅ Present ({len(raw_cookie)} chars)" if raw_cookie else "⚠️ None (cooky/terabox/cookies.txt missing)"

    full_error_report = (
        f"❌ **TeraBox Resolution Failed**\n\n"
        f"🔐 **Cookie Status:** {cookie_status}\n\n"
        f"🌐 **Headless Browser Engine:**\n• {hl_err}\n\n"
        f"📡 **Gateway Responses:**\n{gw_err}\n\n"
        f"🔄 **Backup Hostinger API:**\n• {err}"
    )

    logger.error(f"[extract_terabox_info] Complete failure report:\n{full_error_report}")
    return False, {}, full_error_report


def _is_process_running(name: str) -> bool:
    try:
        res = subprocess.run(["pgrep", "-f", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return res.returncode == 0
    except Exception:
        return False


def _ensure_xvfb_running() -> str:
    """Ensures Xvfb and VNC display server is actively running on Linux."""
    if sys.platform != "linux":
        return ":0"
    display = ":99"

    # 1. Check if Xvfb is running
    xvfb_running = _is_process_running("Xvfb :99")
    if not xvfb_running:
        # Clean up stale locks
        for lock_file in ["/tmp/.X11-unix/X99", "/tmp/.X99-lock"]:
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

        logger.info(f"Starting Xvfb virtual display on {display} (1920x1080)...")
        try:
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac", "+extension", "GLX", "+render", "-noreset"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.2)
        except Exception as e:
            logger.warning(f"Could not auto-start Xvfb: {e}")

    # 2. Check if x11vnc is running on port 5900
    if not _is_process_running("x11vnc"):
        logger.info("Starting x11vnc on :99...")
        try:
            subprocess.Popen(
                ["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", "5900"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"x11vnc start warning: {e}")

    # 3. Check if websockify (noVNC web interface) is running on port 6080
    if not _is_process_running("websockify"):
        logger.info("Starting noVNC websockify on port 6080...")
        try:
            subprocess.Popen(
                ["websockify", "--web", "/usr/share/novnc", "6080", "localhost:5900"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"websockify start warning: {e}")

    return display


CRAWLER_TIMEOUT_SEC = 180  # Strict 3-minute maximum budget for crawler


async def _kill_crawler_and_chrome(proc: Optional[asyncio.subprocess.Process], download_id: str):
    """Forcefully terminates the crawler process, its Chrome children, and cleans temp files."""
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Terminate any orphaned Chrome/Chromium processes under DISPLAY=:99 on Linux
    if sys.platform == "linux":
        try:
            subprocess.run(
                ["pkill", "-9", "-f", "chrome.*(1920,1080|terabox)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            if proc and proc.pid:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    # Clean any temporary .crdownload files in DOWNLOAD_DIR
    try:
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file() and (f.name.endswith(".crdownload") or download_id in f.name):
                try:
                    f.unlink()
                except Exception:
                    pass
    except Exception:
        pass


async def _download_via_node_crawler(
    url: str,
    output_dir: Path,
    download_id: str,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Priority Downloader Engine: Spawns Node.js Puppeteer Stealth Crawler in Xvfb GUI.
    Downloads the file directly to output_dir and emits real-time progress.
    Strictly enforced with a 3-minute hard timeout and automatic process termination.
    """
    node_bin = shutil.which("node")
    if not node_bin:
        return False, None, None, "Node.js is not installed on the system."

    crawler_script = BASE_DIR / "terabox_crawler.js"
    if not crawler_script.exists():
        return False, None, None, f"Crawler script not found at: {crawler_script}"

    # Ensure Xvfb is active on Linux
    display = _ensure_xvfb_running()

    env = os.environ.copy()
    env["DISPLAY"] = display

    cmd = [
        node_bin,
        str(crawler_script),
        url,
        str(output_dir),
        download_id,
    ]

    logger.info(f"[_download_via_node_crawler] Spawning crawler on {display}: {' '.join(cmd)}")
    if progress_updater:
        try:
            await progress_updater("⏳ **Processing media link via browser...**")
        except Exception:
            pass

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        last_json_line = None
        last_progress_time = 0

        async def _read_and_wait():
            nonlocal last_json_line, last_progress_time
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                # Keep 100% full technical logs in terminal
                logger.info(f"[Crawler Output] {line}")

                now = time.time()
                if line.startswith("[PROGRESS]"):
                    prog_text = line.replace("[PROGRESS]", "").strip()
                    if progress_updater and (now - last_progress_time > 1.8):
                        last_progress_time = now
                        try:
                            await progress_updater(f"📥 **{prog_text}...**")
                        except Exception:
                            pass

                elif line.startswith("{") and line.endswith("}"):
                    last_json_line = line

            stderr_bytes = await proc.stderr.read()
            return_code = await proc.wait()
            return return_code, stderr_bytes

        try:
            return_code, stderr_bytes = await asyncio.wait_for(
                _read_and_wait(), timeout=CRAWLER_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            logger.error(f"[_download_via_node_crawler] Crawler exceeded strict 3-minute ({CRAWLER_TIMEOUT_SEC}s) limit! Aborting.")
            await _kill_crawler_and_chrome(proc, download_id)
            return False, None, None, "TeraBox download timed out (exceeded 3 minutes limit)."

        # Ensure Chrome and worker are 100% cleaned up
        await _kill_crawler_and_chrome(proc, download_id)

        if return_code == 0 and last_json_line:
            try:
                data = json.loads(last_json_line)
                if data.get("success"):
                    filepath = data.get("filepath")
                    if filepath and os.path.exists(filepath):
                        file_sz = os.path.getsize(filepath)
                        # Sanity check: Ensure downloaded file is not an empty/corrupted stub
                        if file_sz > 50 * 1024:
                            info_dict = {
                                "id": download_id,
                                "title": data.get("title") or Path(filepath).name,
                                "thumbnail": data.get("thumbnail"),
                                "uploader": "TeraBox",
                                "filesize": file_sz,
                                "engine": "puppeteer_stealth_xvfb",
                            }
                            logger.info(f"[_download_via_node_crawler] 🎉 Success: {filepath} ({info_dict['title']})")
                            return True, filepath, info_dict, None
                        else:
                            return False, None, None, f"Downloaded file is too small ({file_sz} bytes)."
            except Exception as parse_ex:
                logger.error(f"Error parsing crawler output JSON: {parse_ex}")

        err_msg = stderr_bytes.decode("utf-8", errors="ignore").strip() if stderr_bytes else ""
        if last_json_line:
            try:
                err_data = json.loads(last_json_line)
                if err_data.get("error"):
                    err_msg = err_data["error"]
            except Exception:
                pass
        return False, None, None, err_msg or "Crawler download failed or timed out."

    except Exception as e:
        logger.error(f"[_download_via_node_crawler] Exception: {e}", exc_info=True)
        if proc:
            await _kill_crawler_and_chrome(proc, download_id)
        return False, None, None, str(e)


async def download_terabox_media(
    url: str,
    quality: str = "best",
    format_selector: Optional[str] = None,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    """
    Downloads TeraBox media directly to disk.
    Priority: Node.js Puppeteer Stealth Crawler in Xvfb GUI.
    Returns (success, file_path, info_dict, error_message).
    """
    download_id = uuid.uuid4().hex[:8]

    # 🚀 PRIORITY 1: High-Speed Telegram MiniApp API (teradownloader.pro)
    if get_terabox_token():
        if progress_updater:
            try:
                res = progress_updater("🤖 **[1/2] Resolving via Telegram Bot Resolver (teradownloader.pro)...**")
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

        ma_success, ma_info, ma_err = await _resolve_via_miniapp_api(url)
        if ma_success and ma_info and ma_info.get("download_url"):
            filename = ma_info.get("filename") or f"terabox_{download_id}.mp4"
            dest_file = DOWNLOAD_DIR / f"tera_{download_id}_{filename}"
            dest_path = str(dest_file)
            f_size = ma_info.get("filesize", 0)

            if progress_updater:
                try:
                    res = progress_updater(f"📥 **[1/2] Stream captured! Connecting to CDN ({format_bytes(f_size)})...**")
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

            try:
                dl_ok = await _stream_miniapp_download(
                    download_url=ma_info["download_url"],
                    dest_path=dest_path,
                    filesize_expected=f_size,
                    progress_updater=progress_updater,
                    stage_label="Bot Resolver",
                )
                if dl_ok and os.path.exists(dest_path):
                    actual_size = os.path.getsize(dest_path)
                    ma_info["filesize"] = actual_size
                    ma_info["filepath"] = dest_path
                    ma_info["id"] = download_id
                    logger.info(f"[TeraBox] 🎉 Download completed via MiniApp API: {dest_path} ({format_bytes(actual_size)})")
                    return True, dest_path, ma_info, None
            except Exception as stream_ex:
                logger.warning(f"[TeraBox] MiniApp streaming failed: {stream_ex}. Falling back to browser crawler...")
                remove_file_safely(dest_path)
        else:
            logger.warning(f"[TeraBox] MiniApp API failed ({ma_err}). Falling back to browser crawler...")
            if progress_updater:
                try:
                    res = progress_updater(f"⚠️ **Telegram Bot Resolver:** `{ma_err or 'Resolution failed'}`\n🔄 *Switching to Backup Browser Crawler...*")
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass

    # 🔄 FALLBACK: Original Node.js Puppeteer Stealth Crawler in Xvfb GUI
    if progress_updater:
        try:
            res = progress_updater("🌐 **[2/2] Backup Resolver: Spawning Headless Browser Engine (Puppeteer/Xvfb)...**")
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    crawler_success, crawler_file, crawler_info, crawler_err = await _download_via_node_crawler(
        url=url,
        output_dir=DOWNLOAD_DIR,
        download_id=download_id,
        progress_updater=progress_updater,
    )
    if crawler_success and crawler_file:
        logger.info("TeraBox media downloaded successfully via Node.js Puppeteer Stealth Crawler!")
        return True, crawler_file, crawler_info, None

    logger.error(f"TeraBox Crawler failed: {crawler_err}")
    return False, None, None, crawler_err or "Could not download TeraBox media."

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': referer_url,
        'Origin': 'https://www.terabox.app',
    }
    if cookie_header:
        headers['Cookie'] = cookie_header

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
            'http_headers': headers,
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
