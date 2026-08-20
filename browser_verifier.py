"""
Interactive Remote Browser CAPTCHA Solver
==========================================
Launches a real Chromium process on the server with --remote-debugging-port,
navigates to the TeraBox URL, injects cookies, then CLOSES the Python CDP
WebSocket so the admin has EXCLUSIVE access via the DevTools browser link.

Cookie extraction is done via HTTP polling (GET /json/... + Network.getCookies
through a fresh short-lived WebSocket) rather than holding a persistent
connection — this prevents the "WebSocket disconnected" error in DevTools.

No Browserless enterprise license needed. Just Chromium on the server.
"""

import os
import json
import time
import asyncio
import logging
import shutil
import urllib.parse
from typing import Dict, Any, Optional, Tuple, Callable, List
from pathlib import Path

import aiohttp

from config import (
    BASE_DIR,
    BROWSERLESS_URL,
    BROWSERLESS_PUBLIC_URL,
    BROWSERLESS_TOKEN,
    CAPTCHA_TIMEOUT_SEC,
    IS_BROWSERLESS_ENABLED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chrome / Chromium binary detection
# ---------------------------------------------------------------------------
_CHROME_CANDIDATES = [
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_chrome_binary() -> Optional[str]:
    """Locate a usable Chrome / Chromium binary on this system."""
    explicit = os.getenv("CHROME_BIN", "").strip()
    if explicit and (os.path.isfile(explicit) or shutil.which(explicit)):
        return explicit
    for candidate in _CHROME_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    for name in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _extract_host(raw: Optional[str]) -> str:
    """Return pure IP/hostname from a URL or bare IP (strips scheme, port, path)."""
    if not raw:
        return "127.0.0.1"
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    try:
        return urllib.parse.urlparse(raw).hostname or "127.0.0.1"
    except Exception:
        return "127.0.0.1"


def is_browserless_configured() -> bool:
    """Returns True if we can run the remote CAPTCHA solver."""
    if BROWSERLESS_URL:
        return True
    return bool(_find_chrome_binary())


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _parse_raw_cookies_for_cdp(raw_cookie: Optional[str]) -> List[Dict[str, Any]]:
    """Converts raw Netscape or key=value cookies into CDP Network.setCookies list."""
    if not raw_cookie:
        return []
    cdp_cookies = []
    for line in raw_cookie.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            domain = parts[0]
            path = parts[2]
            secure = parts[3].lower() == "true"
            expires = int(float(parts[4])) if parts[4].replace(".", "", 1).isdigit() else None
            name = parts[5]
            value = parts[6]
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


def _cookies_to_netscape(cookies: List[Dict[str, Any]]) -> str:
    """Formats CDP cookies into Netscape cookies.txt format."""
    lines = ["# Netscape HTTP Cookie File", "# Generated automatically after CAPTCHA verification", ""]
    for c in cookies:
        domain = c.get("domain", ".terabox.app")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = int(c.get("expires", 0)) if c.get("expires") else 2147483647
        name = c.get("name", "")
        value = c.get("value", "")
        if name:
            lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Low-level helper: brief CDP session to set cookies and navigate, then close
# ---------------------------------------------------------------------------

async def _cdp_setup_and_release(ws_url: str, target_url: str, initial_cookie_raw: Optional[str]) -> bool:
    """
    Opens a SHORT-LIVED CDP WebSocket to:
      1. Enable Network/Page domains
      2. Inject existing cookies
      3. Navigate to target_url
      4. Then IMMEDIATELY closes the connection

    After this call, Chrome has no active CDP client, so the DevTools browser
    link can connect exclusively without "WebSocket disconnected".
    """
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=10)) as ws:
                # Enable Network domain
                await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
                await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})

                # Drain enable responses
                for _ in range(4):
                    try:
                        await asyncio.wait_for(ws.receive(), timeout=1.5)
                    except Exception:
                        break

                # Inject cookies
                if initial_cookie_raw:
                    cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
                    if cdp_cookies:
                        await ws.send_json({
                            "id": 3,
                            "method": "Network.setCookies",
                            "params": {"cookies": cdp_cookies},
                        })
                        logger.info(f"[CDP Setup] Injected {len(cdp_cookies)} cookies")
                        try:
                            await asyncio.wait_for(ws.receive(), timeout=1.5)
                        except Exception:
                            pass

                # Navigate
                await ws.send_json({
                    "id": 4,
                    "method": "Page.navigate",
                    "params": {"url": target_url},
                })
                logger.info(f"[CDP Setup] Navigated to {target_url}")

                # Give it a moment to start loading, then close
                await asyncio.sleep(1.5)
                # WebSocket closed on context manager exit
        logger.info("[CDP Setup] CDP connection released — DevTools link is now exclusively available")
        return True
    except Exception as e:
        logger.error(f"[CDP Setup] Failed during setup: {e}", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Low-level helper: brief CDP poll to extract current cookies
# ---------------------------------------------------------------------------

async def _cdp_poll_cookies(ws_url: str) -> Optional[List[Dict[str, Any]]]:
    """
    Opens a SHORT-LIVED CDP WebSocket, requests Network.getCookies, returns
    the cookie list, then immediately closes. This doesn't interfere with an
    open DevTools session if timed carefully (Chrome serialises CDP clients).
    """
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(ws_url, timeout=aiohttp.ClientTimeout(total=8)) as ws:
                await ws.send_json({
                    "id": 1,
                    "method": "Network.getCookies",
                    "params": {"urls": [
                        "https://www.1024terabox.com",
                        "https://www.terabox.app",
                        "https://terabox.com",
                        "https://1024tera.com",
                    ]},
                })
                # Read responses until we find our getCookies result
                for _ in range(10):
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("id") == 1 and "result" in data:
                                return data["result"].get("cookies", [])
                    except Exception:
                        break
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Chrome session creation
# ---------------------------------------------------------------------------

async def create_captcha_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Launches Chrome with --remote-debugging-port, injects cookies, navigates,
    then RELEASES the CDP connection so the user can use DevTools exclusively.

    Returns a session_obj containing metadata (no live WebSocket).
    """
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return False, None, "No Chrome/Chromium binary found. Install: sudo apt install -y chromium-browser"

    debug_port = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
    public_host = _extract_host(BROWSERLESS_PUBLIC_URL or os.getenv("SERVER_PUBLIC_IP", ""))
    local_base = f"http://127.0.0.1:{debug_port}"
    public_base = f"http://{public_host}:{debug_port}"

    user_data_dir = str(BASE_DIR / ".chrome_captcha_profile")

    chrome_args = [
        chrome_bin,
        f"--remote-debugging-port={debug_port}",
        "--remote-debugging-address=0.0.0.0",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--headless=new",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--window-size=1280,900",
        "about:blank",  # Start blank; we navigate via CDP after setup
    ]

    logger.info(f"[Chrome] Launching: {chrome_bin} on port {debug_port}")

    try:
        chrome_proc = await asyncio.create_subprocess_exec(
            *chrome_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return False, None, f"Failed to launch Chrome: {e}"

    # Wait for debug port to become available
    page_id = None
    ws_url = None
    frontend_url = None

    for attempt in range(20):
        await asyncio.sleep(1)
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=3)) as http_s:
                async with http_s.get(f"{local_base}/json/list") as resp:
                    if resp.status == 200:
                        tabs = await resp.json(content_type=None)
                        if tabs:
                            # Pick the first page-type tab
                            target_tab = next((t for t in tabs if t.get("type") == "page"), tabs[0])
                            page_id = target_tab.get("id", "")
                            raw_ws = target_tab.get("webSocketDebuggerUrl", "")
                            frontend_url = target_tab.get("devtoolsFrontendUrl", "")
                            if raw_ws:
                                # Always connect locally (127.0.0.1) regardless of what Chrome reports
                                parsed_ws = urllib.parse.urlparse(raw_ws)
                                ws_url = f"ws://127.0.0.1:{debug_port}{parsed_ws.path}"
                                logger.info(f"[Chrome] Debug port ready. Tab ID: {page_id}")
                                break
        except Exception:
            pass

    if not ws_url or not page_id:
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        return False, None, f"Chrome launched but debug port {debug_port} never became available."

    # Set up cookies + navigation via CDP, then RELEASE the connection
    setup_ok = await _cdp_setup_and_release(ws_url, target_url, initial_cookie_raw)
    if not setup_ok:
        logger.warning("[Chrome] CDP setup had issues but continuing anyway...")

    # Build inspector URL using the devtoolsFrontendUrl from Chrome's JSON (most reliable)
    if frontend_url:
        # Replace 127.0.0.1 with the public IP in the ws= query param
        inspector_url = frontend_url.replace(
            f"127.0.0.1:{debug_port}",
            f"{public_host}:{debug_port}"
        )
        # frontend_url from Chrome is a relative path like /devtools/inspector.html?ws=...
        # or a full https://chrome-devtools-frontend.appspot.com/... URL
        if inspector_url.startswith("/"):
            inspector_url = f"http://{public_host}:{debug_port}{inspector_url}"
        elif not inspector_url.startswith("http"):
            inspector_url = f"http://{public_host}:{debug_port}/{inspector_url}"
    else:
        inspector_url = (
            f"http://{public_host}:{debug_port}/devtools/inspector.html"
            f"?ws={public_host}:{debug_port}/devtools/page/{page_id}"
        )

    logger.info(f"[Chrome] Inspector URL: {inspector_url}")
    logger.info(f"[Chrome] Local WS URL for polling: {ws_url}")

    return True, {
        "mode": "chrome",
        "chrome_proc": chrome_proc,
        "debug_port": debug_port,
        "page_id": page_id,
        "ws_url": ws_url,           # local CDP URL (for polling only, never held open)
        "inspector_url": inspector_url,
        "local_base": local_base,
        "target_url": target_url,
        "created_at": time.time(),
    }, None


# ---------------------------------------------------------------------------
# Session teardown
# ---------------------------------------------------------------------------

async def close_captcha_session(session_obj: Dict[str, Any]) -> None:
    """Kills the Chrome process we started."""
    if not session_obj:
        return
    chrome_proc = session_obj.get("chrome_proc")
    if chrome_proc:
        try:
            chrome_proc.terminate()
            await asyncio.wait_for(chrome_proc.wait(), timeout=5)
            logger.info("[Chrome] Process terminated cleanly.")
        except Exception:
            try:
                chrome_proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CAPTCHA solve monitor — HTTP REST polling, no persistent WebSocket
# ---------------------------------------------------------------------------

async def wait_for_captcha_solved(
    session_obj: Dict[str, Any],
    timeout_sec: int = CAPTCHA_TIMEOUT_SEC,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Polls for CAPTCHA resolution WITHOUT holding a WebSocket connection open.

    Strategy:
      1. Every 5 seconds, call GET /json/list to verify Chrome is still alive.
      2. Every 10 seconds, open a brief CDP WebSocket, call Network.getCookies,
         then immediately close. If ndus cookie is present and verification
         cookies (csrfToken) have changed, we consider it solved.
      3. Watch for the page URL to no longer contain "verify".
    """
    ws_url = session_obj.get("ws_url")
    local_base = session_obj.get("local_base", "http://127.0.0.1:9222")
    page_id = session_obj.get("page_id")
    debug_port = session_obj.get("debug_port", 9222)

    start_time = time.time()
    last_progress_notify = 0.0
    last_cookie_poll = 0.0
    last_url_check = 0.0
    new_cookies_text = None
    solved = False

    logger.info(f"[Monitor] Waiting up to {timeout_sec}s for CAPTCHA to be solved...")

    try:
        while time.time() - start_time < timeout_sec:
            now = time.time()
            remaining = int(timeout_sec - (now - start_time))

            # Progress update every 30s
            if progress_callback and (now - last_progress_notify >= 30):
                last_progress_notify = now
                try:
                    await progress_callback(f"⏳ Waiting for CAPTCHA solution ({remaining}s remaining)...")
                except Exception:
                    pass

            # Check Chrome is still alive via HTTP every 5s
            if now - last_url_check >= 5:
                last_url_check = now
                try:
                    connector = aiohttp.TCPConnector(ssl=False)
                    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=3)) as http_s:
                        async with http_s.get(f"{local_base}/json/list") as resp:
                            if resp.status == 200:
                                tabs = await resp.json(content_type=None)
                                if tabs:
                                    target_tab = next((t for t in tabs if t.get("id") == page_id), tabs[0])
                                    page_url = target_tab.get("url", "")
                                    logger.debug(f"[Monitor] Current page URL: {page_url[:100]}")

                                    # If page navigated away from verify/captcha, likely solved
                                    if page_url and not any(kw in page_url.lower() for kw in ["verify", "captcha", "safe/"]):
                                        if "terabox" in page_url.lower() or "1024tera" in page_url.lower():
                                            logger.info(f"[Monitor] Page navigated to non-verify URL: {page_url[:100]}")
                                            # Trigger a cookie poll immediately
                                            last_cookie_poll = 0
                except Exception as e:
                    logger.debug(f"[Monitor] HTTP check error: {e}")

            # Cookie poll every 10s (brief CDP connection)
            if now - last_cookie_poll >= 10:
                last_cookie_poll = now
                local_ws = ws_url
                if local_ws:
                    try:
                        cookies = await _cdp_poll_cookies(local_ws)
                        if cookies:
                            cookie_map = {c.get("name"): c.get("value", "") for c in cookies}
                            ndus = cookie_map.get("ndus", "")
                            csrf = cookie_map.get("csrfToken", "")
                            logger.info(f"[Monitor] Cookie poll: ndus={'yes' if ndus else 'no'} ({len(ndus)} chars), csrfToken={'yes' if csrf else 'no'}")

                            if ndus and len(ndus) > 10:
                                # We have a valid session cookie
                                new_cookies_text = _cookies_to_netscape(cookies)
                                # Check if this is a fresh verified session
                                if csrf and len(csrf) > 5:
                                    logger.info("[Monitor] Valid session cookies detected (ndus + csrfToken present)!")
                                    solved = True
                    except Exception as e:
                        logger.debug(f"[Monitor] Cookie poll error: {e}")

            if solved and new_cookies_text:
                break

            await asyncio.sleep(3)

        if solved and new_cookies_text:
            cookie_target = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
            cookie_target.parent.mkdir(parents=True, exist_ok=True)
            with open(cookie_target, "w", encoding="utf-8") as f:
                f.write(new_cookies_text)
            logger.info(f"[Monitor] CAPTCHA solved! Saved updated cookies to {cookie_target}")
            return True, new_cookies_text, None

        return False, None, f"CAPTCHA verification timed out after {timeout_sec}s."

    except Exception as e:
        logger.error(f"[Monitor] Unexpected error: {e}", exc_info=True)
        return False, None, str(e)

    finally:
        await close_captcha_session(session_obj)


# ---------------------------------------------------------------------------
# High-level orchestrator
# ---------------------------------------------------------------------------

async def solve_terabox_captcha_interactive(
    target_url: str,
    initial_cookie: Optional[str] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Complete workflow:
    1. Launch Chrome, inject cookies, navigate, RELEASE CDP connection.
    2. Send interactive DevTools link to admin (they get exclusive access).
    3. Poll periodically for cookie changes.
    4. Save fresh cookies and return.
    """
    if not is_browserless_configured():
        return False, None, "No browser backend (install chromium-browser or set BROWSERLESS_URL)."

    logger.info(f"[Solver] Starting interactive CAPTCHA solver for {target_url[:80]}...")

    success, session_obj, err = await create_captcha_session(target_url, initial_cookie)
    if not success or not session_obj:
        logger.error(f"[Solver] Session creation failed: {err}")
        return False, None, err or "Could not create browser session."

    inspector_url = session_obj["inspector_url"]
    logger.info(f"[Solver] Session ready. Inspector URL: {inspector_url}")

    # Notify admin via Telegram
    if notify_admin_callback:
        try:
            await notify_admin_callback(inspector_url, target_url)
            logger.info("[Solver] Admin notification sent.")
        except Exception as e:
            logger.warning(f"[Solver] Failed to notify admin: {e}")

    # Update chat message
    if progress_updater:
        try:
            await progress_updater(
                "🧩 **Human Verification Required!**\n\n"
                f"👉 [Click Here to Open Captcha Solver]({inspector_url})\n\n"
                f"⏱️ Waiting for verification ({CAPTCHA_TIMEOUT_SEC}s)..."
            )
        except Exception:
            pass

    return await wait_for_captcha_solved(
        session_obj,
        timeout_sec=CAPTCHA_TIMEOUT_SEC,
        progress_callback=progress_updater,
    )
