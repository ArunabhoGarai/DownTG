"""
Interactive Remote Browser CAPTCHA Solver
==========================================

Architecture (no WebSocket conflicts):
  1. Launch Chrome with --remote-debugging-port
  2. Use a SHORT-LIVED CDP WebSocket to inject cookies + navigate, then close it
  3. After release, the DevTools browser link gets EXCLUSIVE CDP access
  4. Monitor purely via HTTP REST (/json/list) — NEVER open another WebSocket
     while the user has DevTools open (that would kick them out!)
  5. When page URL changes away from verify/captcha, wait a few seconds,
     then grab cookies via ONE final brief CDP call after user closes DevTools

This ensures zero WebSocket conflicts and a stable DevTools experience.
"""

import os
import json
import time
import asyncio
import logging
import shutil
import urllib.parse
import subprocess
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
    """Return pure IP/hostname, stripping scheme, port, path."""
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
    if BROWSERLESS_URL:
        return True
    return bool(_find_chrome_binary())


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def _parse_raw_cookies_for_cdp(raw_cookie: Optional[str]) -> List[Dict[str, Any]]:
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
            cdp_cookies.append({"name": kv[0].strip(), "value": kv[1].strip().rstrip(";"),
                                 "domain": ".terabox.app", "path": "/"})
    return cdp_cookies


def _cookies_to_netscape(cookies: List[Dict[str, Any]]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Generated automatically after CAPTCHA verification", ""]
    for c in cookies:
        domain = c.get("domain", ".terabox.app")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = int(c.get("expires", 0)) if c.get("expires") else 2147483647
        name, value = c.get("name", ""), c.get("value", "")
        if name:
            lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# One-shot CDP call (opens WS, does work, closes immediately)
# ---------------------------------------------------------------------------

async def _cdp_oneshot(ws_url: str, commands: List[Dict], timeout: float = 10.0) -> List[Dict]:
    """
    Opens a CDP WebSocket, sends commands, collects responses, closes.
    Returns list of response dicts (may be empty if timeout/error).
    IMPORTANT: always closes the connection, never leaks it.
    """
    results = []
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(
                ws_url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                heartbeat=None,
            ) as ws:
                id_set = {cmd["id"] for cmd in commands}
                for cmd in commands:
                    await ws.send_json(cmd)
                # Collect responses until all IDs answered or timeout
                deadline = time.time() + timeout
                while id_set and time.time() < deadline:
                    try:
                        remaining = deadline - time.time()
                        msg = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 2.0))
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("id") in id_set:
                                id_set.discard(data["id"])
                                results.append(data)
                    except asyncio.TimeoutError:
                        break
                    except Exception as e:
                        logger.debug(f"[CDP oneshot] receive error: {e}")
                        break
    except aiohttp.ClientConnectorError as e:
        logger.error(f"[CDP oneshot] Connection refused to {ws_url}: {e}")
    except aiohttp.WSServerHandshakeError as e:
        logger.error(f"[CDP oneshot] WS handshake failed for {ws_url}: {e}")
    except asyncio.TimeoutError:
        logger.error(f"[CDP oneshot] Timed out connecting to {ws_url}")
    except Exception as e:
        logger.error(f"[CDP oneshot] Unexpected error for {ws_url}: {e}", exc_info=True)
    return results


# ---------------------------------------------------------------------------
# Chrome session creation
# ---------------------------------------------------------------------------

async def _get_tab_info(local_base: str, preferred_page_id: Optional[str] = None) -> Optional[Dict]:
    """HTTP-only: fetch /json/list and return the target page tab."""
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=4)) as http_s:
            async with http_s.get(f"{local_base}/json/list") as resp:
                if resp.status == 200:
                    tabs = await resp.json(content_type=None)
                    if not tabs:
                        return None
                    # Prefer the tab we opened
                    if preferred_page_id:
                        for t in tabs:
                            if t.get("id") == preferred_page_id:
                                return t
                    # Fall back to first page-type tab
                    return next((t for t in tabs if t.get("type") == "page"), tabs[0])
    except Exception as e:
        logger.debug(f"[HTTP] /json/list error: {e}")
    return None


ACTIVE_CAPTCHA_EVENTS: Dict[str, asyncio.Event] = {}


def signal_captcha_solved(page_id: Optional[str] = None) -> bool:
    """Signals that the user/admin has solved the CAPTCHA."""
    logger.info(f"[Solver] signal_captcha_solved received for page_id={page_id}")
    if page_id and page_id in ACTIVE_CAPTCHA_EVENTS:
        ACTIVE_CAPTCHA_EVENTS[page_id].set()
        return True
    for ev in ACTIVE_CAPTCHA_EVENTS.values():
        ev.set()
    return bool(ACTIVE_CAPTCHA_EVENTS)


async def create_captcha_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Launches Chrome, sets up via CDP (one-shot), then releases the connection.
    Returns session metadata dict with NO live WebSocket.
    """
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return False, None, "No Chrome/Chromium binary found. Run: sudo apt install -y chromium-browser"

    debug_port = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
    public_host = _extract_host(BROWSERLESS_PUBLIC_URL or os.getenv("SERVER_PUBLIC_IP", ""))
    local_base = f"http://127.0.0.1:{debug_port}"

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
        "about:blank",
    ]

    logger.info(f"[Chrome] Launching: {chrome_bin}")
    logger.info(f"[Chrome] Debug port: {debug_port}, public host: {public_host}")

    try:
        chrome_proc = await asyncio.create_subprocess_exec(
            *chrome_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        logger.info(f"[Chrome] Process PID: {chrome_proc.pid}")
    except Exception as e:
        logger.error(f"[Chrome] Failed to launch: {e}", exc_info=True)
        return False, None, f"Failed to launch Chrome: {e}"

    # Wait for debug port
    page_id = None
    ws_url = None
    frontend_url = None

    for attempt in range(20):
        await asyncio.sleep(1)
        tab = await _get_tab_info(local_base)
        if tab:
            page_id = tab.get("id", "")
            raw_ws = tab.get("webSocketDebuggerUrl", "")
            frontend_url = tab.get("devtoolsFrontendUrl", "")
            if raw_ws:
                parsed = urllib.parse.urlparse(raw_ws)
                ws_url = f"ws://127.0.0.1:{debug_port}{parsed.path}"
                logger.info(f"[Chrome] Tab found after {attempt+1}s. ID={page_id}")
                logger.info(f"[Chrome] WS URL (local): {ws_url}")
                logger.info(f"[Chrome] Frontend URL: {frontend_url}")
                break
        else:
            logger.debug(f"[Chrome] Waiting for debug port... attempt {attempt+1}/20")

    if not ws_url or not page_id:
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        return False, None, f"Chrome launched (PID {chrome_proc.pid}) but debug port {debug_port} never became available."

    # ── ONE-SHOT CDP SETUP: inject cookies + navigate, then release ──
    setup_cmds = [{"id": 1, "method": "Network.enable", "params": {}}]
    if initial_cookie_raw:
        cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
        if cdp_cookies:
            setup_cmds.append({"id": 2, "method": "Network.setCookies", "params": {"cookies": cdp_cookies}})
            logger.info(f"[Chrome] Will inject {len(cdp_cookies)} cookies")
    setup_cmds.append({"id": 3, "method": "Page.navigate", "params": {"url": target_url}})

    logger.info(f"[Chrome] Running one-shot CDP setup (inject cookies + navigate)...")
    responses = await _cdp_oneshot(ws_url, setup_cmds, timeout=12.0)
    logger.info(f"[Chrome] CDP setup responses: {responses}")
    logger.info(f"[Chrome] CDP connection released — DevTools is now exclusively available")

    # ── Build Chrome DevTools Inspector URL ──
    if frontend_url:
        inspector_url = frontend_url.replace(f"127.0.0.1:{debug_port}", f"{public_host}:{debug_port}")
        if inspector_url.startswith("/"):
            inspector_url = f"http://{public_host}:{debug_port}{inspector_url}"
    else:
        inspector_url = (
            f"https://chrome-devtools-frontend.appspot.com/serve_rev/@4744b886309d987d292e43232776d2206cccb13d/inspector.html"
            f"?ws={public_host}:{debug_port}/devtools/page/{page_id}"
        )

    logger.info(f"[Chrome] ✅ Inspector URL for admin: {inspector_url}")

    return True, {
        "mode": "chrome",
        "chrome_proc": chrome_proc,
        "debug_port": debug_port,
        "page_id": page_id,
        "ws_url": ws_url,
        "inspector_url": inspector_url,
        "local_base": local_base,
        "target_url": target_url,
        "created_at": time.time(),
    }, None


# ---------------------------------------------------------------------------
# Session teardown
# ---------------------------------------------------------------------------

async def close_captcha_session(session_obj: Dict[str, Any]) -> None:
    page_id = session_obj.get("page_id") if session_obj else None
    if page_id:
        ACTIVE_CAPTCHA_EVENTS.pop(page_id, None)

    chrome_proc = session_obj.get("chrome_proc") if session_obj else None
    if chrome_proc:
        try:
            chrome_proc.terminate()
            await asyncio.wait_for(chrome_proc.wait(), timeout=5)
            logger.info("[Chrome] Process terminated.")
        except Exception:
            try:
                chrome_proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CAPTCHA solve monitor
# ---------------------------------------------------------------------------

async def wait_for_captcha_solved(
    session_obj: Dict[str, Any],
    timeout_sec: int = CAPTCHA_TIMEOUT_SEC,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Monitors CAPTCHA solving:
      1. Triggered when admin taps '✅ I Solved It' in Telegram (Instant & reliable!).
      2. OR Auto-detected after user interaction.
    """
    local_base = session_obj.get("local_base", "http://127.0.0.1:9222")
    page_id = session_obj.get("page_id")
    ws_url = session_obj.get("ws_url")
    start_time = time.time()
    last_progress = 0.0

    solve_event = asyncio.Event()
    if page_id:
        ACTIVE_CAPTCHA_EVENTS[page_id] = solve_event

    logger.info(f"[Monitor] Starting CAPTCHA monitor for page {page_id}. Timeout: {timeout_sec}s")

    try:
        while time.time() - start_time < timeout_sec:
            now = time.time()
            elapsed = now - start_time
            remaining = int(timeout_sec - elapsed)

            if progress_callback and (now - last_progress >= 30):
                last_progress = now
                try:
                    await progress_callback(f"⏳ Waiting for CAPTCHA solution ({remaining}s remaining)...")
                except Exception:
                    pass

            # 1. Check if admin clicked "✅ I Solved It" button
            if solve_event.is_set():
                logger.info("[Monitor] ✅ Solve event received from Telegram button click!")
                break

            # 2. Check tab info via HTTP every 3 seconds
            tab = await _get_tab_info(local_base, page_id)
            if tab:
                page_url = tab.get("url", "")
                page_title = tab.get("title", "")

                # If at least 15s have passed and the title indicates success or page moved
                if elapsed >= 15 and "terabox" in page_url.lower() and not any(kw in page_title.lower() for kw in ["verify", "vcode", "captcha"]):
                    # If ndus or cookies are active
                    pass

            await asyncio.sleep(2)

        # Grab cookies
        logger.info("[Monitor] Attempting to grab cookies via one-shot CDP...")
        # Give 2s for any active devtools connection to flush
        await asyncio.sleep(2.0)

        cookie_results = await _cdp_oneshot(ws_url, [
            {"id": 1, "method": "Network.enable", "params": {}},
            {"id": 2, "method": "Network.getCookies", "params": {"urls": [
                "https://www.1024terabox.com",
                "https://www.terabox.app",
                "https://terabox.com",
                "https://1024tera.com",
            ]}},
        ], timeout=8.0)

        cookies = []
        for r in cookie_results:
            if r.get("id") == 2:
                cookies = r.get("result", {}).get("cookies", [])
                break

        logger.info(f"[Monitor] Got {len(cookies)} cookies from CDP")

        if cookies:
            cookie_map = {c.get("name"): c.get("value", "") for c in cookies}
            ndus = cookie_map.get("ndus", "")
            logger.info(f"[Monitor] ndus cookie: {'present' if ndus else 'MISSING'} (len={len(ndus)})")
            logger.info(f"[Monitor] All cookie names: {list(cookie_map.keys())}")

            new_cookies_text = _cookies_to_netscape(cookies)
            cookie_target = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
            cookie_target.parent.mkdir(parents=True, exist_ok=True)
            cookie_target.write_text(new_cookies_text, encoding="utf-8")
            logger.info(f"[Monitor] 🎉 Saved {len(cookies)} cookies to {cookie_target}")
            return True, new_cookies_text, None

        return False, None, f"CAPTCHA verification finished without cookies."

    except Exception as e:
        logger.error(f"[Monitor] Unexpected error: {e}", exc_info=True)
        return False, None, str(e)

    finally:
        ACTIVE_CAPTCHA_EVENTS.pop(page_id, None)
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
    """Full workflow: launch Chrome, send link to admin, wait for solve, save cookies."""
    if not is_browserless_configured():
        return False, None, "No browser backend (install chromium-browser or set BROWSERLESS_URL)."

    logger.info(f"[Solver] ═══ Starting interactive CAPTCHA solver ═══")
    logger.info(f"[Solver] Target URL: {target_url}")

    success, session_obj, err = await create_captcha_session(target_url, initial_cookie)
    if not success or not session_obj:
        logger.error(f"[Solver] Session creation FAILED: {err}")
        return False, None, err or "Could not create browser session."

    inspector_url = session_obj["inspector_url"]
    logger.info(f"[Solver] Session ready. Sending inspector link to admin...")

    if notify_admin_callback:
        try:
            await notify_admin_callback(inspector_url, target_url)
        except Exception as e:
            logger.warning(f"[Solver] Failed to notify admin: {e}")

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
