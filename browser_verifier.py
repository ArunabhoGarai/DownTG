"""
Interactive Remote Browser CAPTCHA Solver
==========================================
Launches a real Chromium process on the server with --remote-debugging-port,
navigates to the TeraBox URL, and sends the admin a DevTools frontend link
where they can interact with the page (slide the captcha, etc).

Once the captcha is solved, cookies are extracted via CDP and saved to disk.

This approach uses Chrome's NATIVE DevTools frontend (served by Chrome itself),
so there are zero external dependencies — no Browserless license, no Docker,
no enterprise features needed. Just Chromium installed on the server.
"""

import os
import json
import time
import asyncio
import logging
import shutil
import signal
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
    # Linux (typical apt installs)
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    # Snap
    "/snap/bin/chromium",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _find_chrome_binary() -> Optional[str]:
    """Locate a usable Chrome / Chromium binary on this system."""
    # Honour explicit override
    explicit = os.getenv("CHROME_BIN", "").strip()
    if explicit and (os.path.isfile(explicit) or shutil.which(explicit)):
        return explicit
    for candidate in _CHROME_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    # Last resort: PATH lookup
    for name in ("chromium-browser", "chromium", "google-chrome-stable", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_browserless_configured() -> bool:
    """Returns True if we can run the remote CAPTCHA solver (either via Browserless Docker or local Chrome)."""
    if BROWSERLESS_URL:
        return True
    return bool(_find_chrome_binary())


# ---------------------------------------------------------------------------
# Cookie helpers (unchanged)
# ---------------------------------------------------------------------------

def _parse_raw_cookies_for_cdp(raw_cookie: Optional[str]) -> List[Dict[str, Any]]:
    """Converts raw Netscape or key=value cookies into CDP Network.setCookies parameter list."""
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
            cookie_dict = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure,
            }
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
    """Formats CDP cookies into standard Netscape cookies.txt format."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically after CAPTCHA verification",
        "",
    ]
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
# Session creation — two modes:
#   Mode A: Browserless Docker (ws://host:3000/chromium?token=...)
#   Mode B: Local Chrome with --remote-debugging-port (no Docker needed)
# ---------------------------------------------------------------------------

async def _connect_via_browserless(target_url: str, initial_cookie_raw: Optional[str]) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Mode A: Connect to a running Browserless v2 Docker container."""
    import urllib.parse

    token_param = f"?token={BROWSERLESS_TOKEN}" if BROWSERLESS_TOKEN else ""
    parsed_bl = urllib.parse.urlparse(BROWSERLESS_URL)
    local_ws_host = parsed_bl.netloc or "127.0.0.1:3000"

    public_base = (BROWSERLESS_PUBLIC_URL or BROWSERLESS_URL).strip().rstrip("/")
    if not public_base.startswith(("http://", "https://")):
        public_base = f"http://{public_base}"

    ws_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    ws = None

    # Try direct WebSocket connection (Browserless v2 style)
    candidate_endpoints = [
        f"ws://{local_ws_host}/chromium{token_param}",
        f"ws://{local_ws_host}{token_param}",
    ]
    for ep in candidate_endpoints:
        try:
            logger.info(f"[Browserless] Attempting WebSocket connection to: {ep}")
            ws = await ws_session.ws_connect(ep, timeout=15)
            logger.info(f"[Browserless] Connected to {ep}")
            break
        except Exception as ws_err:
            logger.debug(f"[Browserless] Endpoint {ep} failed: {ws_err}")

    if not ws:
        await ws_session.close()
        return False, None, "Could not connect to Browserless Docker via WebSocket."

    # Enable CDP domains
    await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
    await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})
    await ws.send_json({"id": 3, "method": "Runtime.enable", "params": {}})

    # Drain responses for the enable commands
    for _ in range(6):
        try:
            await asyncio.wait_for(ws.receive(), timeout=2.0)
        except Exception:
            break

    # Inject cookies
    if initial_cookie_raw:
        cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
        if cdp_cookies:
            await ws.send_json({
                "id": 4,
                "method": "Network.setCookies",
                "params": {"cookies": cdp_cookies},
            })
            logger.info(f"[Browserless] Injected {len(cdp_cookies)} cookies into CDP session")
            try:
                await asyncio.wait_for(ws.receive(), timeout=2.0)
            except Exception:
                pass

    # Navigate to target
    await ws.send_json({
        "id": 5,
        "method": "Page.navigate",
        "params": {"url": target_url},
    })
    logger.info(f"[Browserless] Navigated to {target_url}")

    # For Browserless Docker, the interactive link is the docs page (best available in free tier)
    inspector_url = f"{public_base}/docs{token_param}"

    return True, {
        "mode": "browserless",
        "ws": ws,
        "ws_session": ws_session,
        "inspector_url": inspector_url,
        "public_base": public_base,
        "target_url": target_url,
        "chrome_proc": None,
        "created_at": time.time(),
    }, None


async def _connect_via_local_chrome(target_url: str, initial_cookie_raw: Optional[str]) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """Mode B: Launch a local Chromium with --remote-debugging-port.
    
    Chrome's built-in DevTools frontend is served natively by Chrome at
    http://HOST:DEBUG_PORT — this works perfectly from any browser.
    """
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return False, None, "No Chrome/Chromium binary found on this system."

    debug_port = int(os.getenv("CHROME_DEBUG_PORT", "9222"))
    public_host = (BROWSERLESS_PUBLIC_URL or "").strip().rstrip("/")
    if not public_host:
        public_host = os.getenv("SERVER_PUBLIC_IP", "127.0.0.1").strip()
    # Strip protocol if present for building URLs
    if public_host.startswith(("http://", "https://")):
        import urllib.parse
        parsed = urllib.parse.urlparse(public_host)
        public_host_clean = parsed.netloc or parsed.path
    else:
        public_host_clean = public_host

    user_data_dir = str(BASE_DIR / ".chrome_captcha_profile")

    chrome_args = [
        chrome_bin,
        f"--remote-debugging-port={debug_port}",
        "--remote-debugging-address=0.0.0.0",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--headless=new",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--window-size=1280,900",
        target_url,
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

    # Wait for Chrome to start and the debug port to become available
    ws_url = None
    for attempt in range(15):
        await asyncio.sleep(1)
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=3)) as http_s:
                async with http_s.get(f"http://127.0.0.1:{debug_port}/json/list") as resp:
                    if resp.status == 200:
                        tabs = await resp.json(content_type=None)
                        if tabs:
                            raw_ws = tabs[0].get("webSocketDebuggerUrl", "")
                            page_id = tabs[0].get("id", "")
                            # Rewrite 127.0.0.1 in ws URL to make sure we connect locally
                            if raw_ws:
                                ws_url = raw_ws
                                logger.info(f"[Chrome] Debug port ready. Tab ID: {page_id}")
                                break
        except Exception:
            pass

    if not ws_url:
        # Kill Chrome if we can't connect
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        return False, None, f"Chrome launched but debug port {debug_port} never became available."

    # Connect CDP WebSocket
    ws_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    try:
        ws = await ws_session.ws_connect(ws_url, timeout=15)
    except Exception as e:
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        await ws_session.close()
        return False, None, f"Failed to connect CDP WebSocket: {e}"

    # Enable domains
    await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
    await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})
    await ws.send_json({"id": 3, "method": "Runtime.enable", "params": {}})

    # Drain responses
    for _ in range(6):
        try:
            await asyncio.wait_for(ws.receive(), timeout=2.0)
        except Exception:
            break

    # Inject cookies
    if initial_cookie_raw:
        cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
        if cdp_cookies:
            await ws.send_json({
                "id": 4,
                "method": "Network.setCookies",
                "params": {"cookies": cdp_cookies},
            })
            logger.info(f"[Chrome] Injected {len(cdp_cookies)} cookies")
            try:
                await asyncio.wait_for(ws.receive(), timeout=2.0)
            except Exception:
                pass

    # Build the DevTools frontend URL — Chrome serves this natively!
    # Format: http://PUBLIC_HOST:PORT/devtools/inspector.html?ws=PUBLIC_HOST:PORT/devtools/page/PAGE_ID
    inspector_url = (
        f"http://{public_host_clean}:{debug_port}/devtools/inspector.html"
        f"?ws={public_host_clean}:{debug_port}/devtools/page/{page_id}"
    )
    logger.info(f"[Chrome] DevTools Inspector URL: {inspector_url}")

    return True, {
        "mode": "chrome",
        "ws": ws,
        "ws_session": ws_session,
        "chrome_proc": chrome_proc,
        "debug_port": debug_port,
        "page_id": page_id,
        "inspector_url": inspector_url,
        "target_url": target_url,
        "created_at": time.time(),
    }, None


async def create_captcha_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Creates an interactive browser session for CAPTCHA solving.
    
    Tries two modes in order:
      1. Browserless Docker (if BROWSERLESS_URL is set)
      2. Local Chrome/Chromium (if a binary is found on the system)
    """
    errors = []

    # Mode A: Browserless Docker
    if BROWSERLESS_URL:
        logger.info("[Session] Trying Browserless Docker mode...")
        try:
            ok, session, err = await _connect_via_browserless(target_url, initial_cookie_raw)
            if ok and session:
                return True, session, None
            if err:
                errors.append(f"Browserless: {err}")
        except Exception as e:
            errors.append(f"Browserless exception: {e}")
            logger.error(f"[Session] Browserless mode failed: {e}", exc_info=True)

    # Mode B: Local Chrome
    chrome_bin = _find_chrome_binary()
    if chrome_bin:
        logger.info(f"[Session] Trying local Chrome mode ({chrome_bin})...")
        try:
            ok, session, err = await _connect_via_local_chrome(target_url, initial_cookie_raw)
            if ok and session:
                return True, session, None
            if err:
                errors.append(f"Chrome: {err}")
        except Exception as e:
            errors.append(f"Chrome exception: {e}")
            logger.error(f"[Session] Local Chrome mode failed: {e}", exc_info=True)

    combined = " | ".join(errors) if errors else "No browser backend available."
    return False, None, combined


# ---------------------------------------------------------------------------
# Session teardown
# ---------------------------------------------------------------------------

async def close_captcha_session(session_obj: Dict[str, Any]) -> None:
    """Safely closes the WebSocket, kills Chrome process if we started one."""
    if not session_obj:
        return

    ws = session_obj.get("ws")
    ws_session = session_obj.get("ws_session")
    chrome_proc = session_obj.get("chrome_proc")

    if ws and not ws.closed:
        try:
            await ws.close()
        except Exception:
            pass

    if ws_session and not ws_session.closed:
        try:
            await ws_session.close()
        except Exception:
            pass

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
# CAPTCHA solve monitor
# ---------------------------------------------------------------------------

async def wait_for_captcha_solved(
    session_obj: Dict[str, Any],
    timeout_sec: int = CAPTCHA_TIMEOUT_SEC,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Monitors the CDP WebSocket for verification completion, extracts the newly
    issued session cookies, and saves them to cooky/terabox/cookies.txt.
    """
    ws = session_obj.get("ws")
    if not ws:
        return False, None, "WebSocket is not connected."

    start_time = time.time()
    last_poll_time = 0.0
    last_progress_time = 0.0
    req_id_counter = 100
    solved = False
    new_cookies_text = None

    try:
        while time.time() - start_time < timeout_sec:
            remaining = int(timeout_sec - (time.time() - start_time))

            # Send progress updates every ~30s
            now = time.time()
            if progress_callback and (now - last_progress_time >= 30):
                last_progress_time = now
                try:
                    await progress_callback(f"⏳ Waiting for CAPTCHA solution ({remaining}s remaining)...")
                except Exception:
                    pass

            # Poll cookies every 3 seconds
            if now - last_poll_time >= 3.0:
                last_poll_time = now
                req_id_counter += 1
                try:
                    await ws.send_json({
                        "id": req_id_counter,
                        "method": "Network.getCookies",
                        "params": {"urls": [
                            "https://www.1024terabox.com",
                            "https://www.terabox.app",
                            "https://terabox.com",
                            "https://1024tera.com",
                        ]},
                    })
                except Exception as e:
                    logger.error(f"[Monitor] Failed to send getCookies: {e}")
                    break

            # Read incoming CDP messages
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"[Monitor] WebSocket closed/error: {msg.type}")
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)

                    # Check Network.getCookies response
                    if "result" in data and "cookies" in data.get("result", {}):
                        cookies_list = data["result"]["cookies"]
                        cookie_names = {c.get("name"): c.get("value") for c in cookies_list}

                        if "ndus" in cookie_names and len(cookie_names.get("ndus", "")) > 10:
                            new_cookies_text = _cookies_to_netscape(cookies_list)

                    # Check for successful API responses (captcha solved → page loads real data)
                    if data.get("method") == "Network.responseReceived":
                        params = data.get("params", {})
                        resp_obj = params.get("response", {})
                        resp_url = resp_obj.get("url", "")

                        if ("share/list" in resp_url or "api/shorturlinfo" in resp_url) and resp_obj.get("status") == 200:
                            logger.info(f"[Monitor] Detected successful API response: {resp_url[:100]}")
                            solved = True

                    # Detect navigation away from verification page
                    if data.get("method") == "Page.frameNavigated":
                        nav_url = data.get("params", {}).get("frame", {}).get("url", "")
                        if nav_url and not any(kw in nav_url.lower() for kw in ["verify", "captcha", "safe/"]):
                            if new_cookies_text:
                                logger.info(f"[Monitor] Page navigated away from captcha to: {nav_url[:100]}")
                                solved = True

            except asyncio.TimeoutError:
                pass

            if solved and new_cookies_text:
                break

        if solved and new_cookies_text:
            # Save cookies
            cookie_target = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
            cookie_target.parent.mkdir(parents=True, exist_ok=True)
            with open(cookie_target, "w", encoding="utf-8") as f:
                f.write(new_cookies_text)
            logger.info(f"[Monitor] CAPTCHA solved! Saved updated cookies to {cookie_target}")
            return True, new_cookies_text, None

        if not solved:
            return False, None, f"CAPTCHA verification timed out after {timeout_sec} seconds."

        return False, None, "Solved but no cookies captured."

    except Exception as e:
        logger.error(f"[Monitor] Error while waiting for CAPTCHA solution: {e}", exc_info=True)
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
    Complete high-level workflow:
    1. Launches browser session (Browserless Docker or local Chrome).
    2. Sends interactive DevTools link to admin.
    3. Waits for admin to solve the slider.
    4. Extracts fresh cookies and saves them.
    """
    if not is_browserless_configured():
        return False, None, "No browser backend configured (set BROWSERLESS_URL or install Chrome/Chromium)."

    logger.info(f"[Solver] Starting interactive CAPTCHA solver for {target_url[:80]}...")

    success, session_obj, err = await create_captcha_session(target_url, initial_cookie)
    if not success or not session_obj:
        logger.error(f"[Solver] Session creation failed: {err}")
        return False, None, err or "Could not create browser session."

    inspector_url = session_obj["inspector_url"]
    mode = session_obj.get("mode", "unknown")
    logger.info(f"[Solver] Session created (mode={mode}). Inspector URL: {inspector_url}")

    # Notify admin via Telegram
    if notify_admin_callback:
        try:
            await notify_admin_callback(inspector_url, target_url)
            logger.info("[Solver] Admin notification sent.")
        except Exception as e:
            logger.warning(f"[Solver] Failed to notify admin: {e}")

    # Update chat with the link
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
