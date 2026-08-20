"""
Interactive Remote Browser CAPTCHA Solver
==========================================
Supports:
  1. Browserbase (PRIMARY, Cloud-hosted):
     - Official cloud-hosted live interactive screencast with full touch & mouse support.
     - No ports, no mixed content, works on any mobile & desktop browser.
     - Requires BROWSERBASE_API_KEY in .env.

  2. Local Chromium with --remote-debugging-port (SECONDARY / Fallback).
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
    BROWSERBASE_API_KEY,
    BROWSERBASE_PROJECT_ID,
    BROWSERLESS_URL,
    BROWSERLESS_PUBLIC_URL,
    BROWSERLESS_TOKEN,
    CAPTCHA_TIMEOUT_SEC,
    IS_BROWSERLESS_ENABLED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global Session Event Registry (for instant Telegram button signals)
# ---------------------------------------------------------------------------
ACTIVE_CAPTCHA_EVENTS: Dict[str, asyncio.Event] = {}


def signal_captcha_solved(page_id: Optional[str] = None) -> bool:
    """Signals that the user/admin has solved the CAPTCHA."""
    logger.info(f"[Solver] signal_captcha_solved received for session/page: {page_id}")
    if page_id and page_id in ACTIVE_CAPTCHA_EVENTS:
        ACTIVE_CAPTCHA_EVENTS[page_id].set()
        return True
    for ev in ACTIVE_CAPTCHA_EVENTS.values():
        ev.set()
    return bool(ACTIVE_CAPTCHA_EVENTS)


# ---------------------------------------------------------------------------
# Binary / Backend detection
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
    """Returns True if either Browserbase or local Chrome is available."""
    if BROWSERBASE_API_KEY:
        return True
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
# One-shot CDP command execution over WebSocket
# ---------------------------------------------------------------------------

async def _cdp_oneshot(ws_url: str, commands: List[Dict], timeout: float = 12.0) -> List[Dict]:
    """Connects to CDP WebSocket, executes commands, collects responses, and closes cleanly."""
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
    except Exception as e:
        logger.error(f"[CDP oneshot] Error connecting to {ws_url[:60]}...: {e}")
    return results


async def _navigate_and_inject_browserbase(
    connect_url: str,
    target_url: str,
    initial_cookie_raw: Optional[str],
) -> bool:
    """Connects to Browserbase CDP WebSocket, injects cookies, navigates, and verifies page rendering."""
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(
                connect_url,
                timeout=aiohttp.ClientTimeout(total=45),
                heartbeat=15.0,
            ) as ws:
                logger.info("[Browserbase CDP] WebSocket connected. Enabling domains...")
                # 1. Enable domains
                await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
                await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})
                await ws.send_json({"id": 3, "method": "Runtime.enable", "params": {}})
                await asyncio.sleep(1.0)

                # 2. Inject initial cookies
                if initial_cookie_raw:
                    cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
                    if cdp_cookies:
                        await ws.send_json({
                            "id": 4,
                            "method": "Network.setCookies",
                            "params": {"cookies": cdp_cookies},
                        })
                        logger.info(f"[Browserbase CDP] Injected {len(cdp_cookies)} existing cookies")
                        await asyncio.sleep(0.5)

                # 3. Navigate to target URL
                logger.info(f"[Browserbase CDP] Triggering Page.navigate to {target_url}...")
                await ws.send_json({
                    "id": 5,
                    "method": "Page.navigate",
                    "params": {"url": target_url},
                })

                # 4. Wait for page load and events
                loaded = False
                for _ in range(16):
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            method = data.get("method", "")
                            if method in ("Page.loadEventFired", "Page.domContentEventFired", "Page.frameNavigated"):
                                logger.info(f"[Browserbase CDP] Navigation event: {method}")
                                loaded = True
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        break

                # 5. Check location.href via Runtime.evaluate
                await ws.send_json({
                    "id": 6,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "window.location.href"},
                })
                for _ in range(6):
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("id") == 6:
                                cur_url = data.get("result", {}).get("result", {}).get("value", "")
                                logger.info(f"[Browserbase CDP] Current page location: {cur_url}")
                                break
                    except Exception:
                        break

                # Keep connection alive for 3 more seconds to ensure full rendering in Browserbase
                await asyncio.sleep(3.0)
                logger.info("[Browserbase CDP] Navigation & initial render phase complete.")
                return True
    except Exception as e:
        logger.error(f"[Browserbase CDP] Navigation error: {e}", exc_info=True)
        return False


async def _create_browserbase_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Creates an interactive cloud browser session on Browserbase."""
    logger.info("[Browserbase] Initializing cloud browser session...")
    api_headers = {
        "X-BB-API-Key": BROWSERBASE_API_KEY,
        "Content-Type": "application/json",
    }
    create_body: Dict[str, Any] = {
        "keepAlive": True,
        "browserSettings": {
            "solveCaptchas": True,
        },
    }
    if BROWSERBASE_PROJECT_ID:
        create_body["projectId"] = BROWSERBASE_PROJECT_ID

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=25)) as session:
            # 1. Create Session
            async with session.post(
                "https://api.browserbase.com/v1/sessions",
                headers=api_headers,
                json=create_body,
            ) as resp:
                if resp.status not in (200, 201):
                    err_txt = await resp.text()
                    logger.error(f"[Browserbase] Session creation failed (HTTP {resp.status}): {err_txt}")
                    return False, None, f"Browserbase session create error: {err_txt}"
                session_data = await resp.json(content_type=None)
                session_id = session_data.get("id")
                connect_url = session_data.get("connectUrl")

            if not session_id or not connect_url:
                return False, None, "Browserbase returned empty session ID or connectUrl."

            logger.info(f"[Browserbase] Created session {session_id}. Fetching live debug URL...")

            # 2. Get Live Debugger Fullscreen URL
            inspector_url = f"https://www.browserbase.com/sessions/{session_id}"
            try:
                async with session.get(
                    f"https://api.browserbase.com/v1/sessions/{session_id}/debug",
                    headers=api_headers,
                ) as dbg_resp:
                    if dbg_resp.status == 200:
                        dbg_data = await dbg_resp.json(content_type=None)
                        inspector_url = (
                            dbg_data.get("debuggerFullscreenUrl")
                            or dbg_data.get("debuggerUrl")
                            or inspector_url
                        )
            except Exception as dbg_err:
                logger.warning(f"[Browserbase] Debug URL fetch warning: {dbg_err}")

            # 3. Setup Session via CDP (Inject Cookies + Navigate and wait for render)
            logger.info(f"[Browserbase] Setting up cookies and navigating to {target_url}...")
            await _navigate_and_inject_browserbase(connect_url, target_url, initial_cookie_raw)

            logger.info(f"[Browserbase] ✅ Interactive Live URL Ready: {inspector_url}")

            return True, {
                "mode": "browserbase",
                "session_id": session_id,
                "connect_url": connect_url,
                "inspector_url": inspector_url,
                "page_id": session_id,
                "target_url": target_url,
                "created_at": time.time(),
            }, None

    except Exception as e:
        logger.error(f"[Browserbase] Session creation failed: {e}", exc_info=True)
        return False, None, f"Browserbase error: {e}"


# ---------------------------------------------------------------------------
# 2. Local Chromium Fallback Engine
# ---------------------------------------------------------------------------

async def _get_tab_info(local_base: str, preferred_page_id: Optional[str] = None) -> Optional[Dict]:
    """HTTP-only: fetch /json/list from local Chrome."""
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=4)) as http_s:
            async with http_s.get(f"{local_base}/json/list") as resp:
                if resp.status == 200:
                    tabs = await resp.json(content_type=None)
                    if not tabs:
                        return None
                    if preferred_page_id:
                        for t in tabs:
                            if t.get("id") == preferred_page_id:
                                return t
                    return next((t for t in tabs if t.get("type") == "page"), tabs[0])
    except Exception:
        pass
    return None


async def _create_local_chrome_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Launches local Chrome as a fallback."""
    chrome_bin = _find_chrome_binary()
    if not chrome_bin:
        return False, None, "No Chrome/Chromium binary found. Configure BROWSERBASE_API_KEY in .env!"

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

    try:
        chrome_proc = await asyncio.create_subprocess_exec(
            *chrome_args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return False, None, f"Failed to launch Chrome: {e}"

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
                break

    if not ws_url or not page_id:
        try:
            chrome_proc.terminate()
        except Exception:
            pass
        return False, None, f"Local Chrome failed to bind to port {debug_port}."

    setup_cmds = [{"id": 1, "method": "Network.enable", "params": {}}]
    if initial_cookie_raw:
        cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
        if cdp_cookies:
            setup_cmds.append({"id": 2, "method": "Network.setCookies", "params": {"cookies": cdp_cookies}})
    setup_cmds.append({"id": 3, "method": "Page.navigate", "params": {"url": target_url}})

    await _cdp_oneshot(ws_url, setup_cmds, timeout=12.0)

    if frontend_url:
        inspector_url = frontend_url.replace(f"127.0.0.1:{debug_port}", f"{public_host}:{debug_port}")
        if inspector_url.startswith("/"):
            inspector_url = f"http://{public_host}:{debug_port}{inspector_url}"
    else:
        inspector_url = f"http://{public_host}:{debug_port}/devtools/inspector.html?ws={public_host}:{debug_port}/devtools/page/{page_id}"

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
# High-Level Session Creator
# ---------------------------------------------------------------------------

async def create_captcha_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """Creates a CAPTCHA session. Prioritizes Browserbase cloud, falls back to local Chrome."""
    if BROWSERBASE_API_KEY:
        ok, session, err = await _create_browserbase_session(target_url, initial_cookie_raw)
        if ok and session:
            return True, session, None
        logger.warning(f"[Session] Browserbase session failed ({err}), trying local Chrome...")

    return await _create_local_chrome_session(target_url, initial_cookie_raw)


async def close_captcha_session(session_obj: Dict[str, Any]) -> None:
    """Closes the active CAPTCHA session."""
    if not session_obj:
        return
    mode = session_obj.get("mode")
    page_id = session_obj.get("page_id")
    if page_id:
        ACTIVE_CAPTCHA_EVENTS.pop(page_id, None)

    if mode == "browserbase":
        session_id = session_obj.get("session_id")
        if session_id and BROWSERBASE_API_KEY:
            try:
                connector = aiohttp.TCPConnector(ssl=False)
                async with aiohttp.ClientSession(connector=connector) as http_s:
                    await http_s.post(
                        f"https://api.browserbase.com/v1/sessions/{session_id}",
                        headers={"X-BB-API-Key": BROWSERBASE_API_KEY, "Content-Type": "application/json"},
                        json={"status": "REQUEST_RELEASE"},
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
                logger.info(f"[Browserbase] Released session {session_id}")
            except Exception:
                pass
    elif mode == "chrome":
        chrome_proc = session_obj.get("chrome_proc")
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
    """Monitors CAPTCHA solving and extracts cookies upon resolution."""
    mode = session_obj.get("mode", "browserbase")
    page_id = session_obj.get("page_id")
    ws_url = session_obj.get("connect_url") if mode == "browserbase" else session_obj.get("ws_url")
    start_time = time.time()
    last_progress = 0.0

    solve_event = asyncio.Event()
    if page_id:
        ACTIVE_CAPTCHA_EVENTS[page_id] = solve_event

    logger.info(f"[Monitor] Starting CAPTCHA monitor (mode={mode}, session_id={page_id}). Timeout: {timeout_sec}s")

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

            # Check if user clicked "✅ I Solved It" button in Telegram
            if solve_event.is_set():
                logger.info("[Monitor] ✅ Telegram 'I Solved It' signal received!")
                break

            await asyncio.sleep(2)

        # Grab cookies
        logger.info(f"[Monitor] Extracting authenticated session cookies via CDP ({mode})...")
        await asyncio.sleep(1.5)

        cookie_results = await _cdp_oneshot(ws_url, [
            {"id": 1, "method": "Network.enable", "params": {}},
            {"id": 2, "method": "Network.getCookies", "params": {"urls": [
                "https://www.1024terabox.com",
                "https://www.terabox.app",
                "https://terabox.com",
                "https://1024tera.com",
            ]}},
        ], timeout=10.0)

        cookies = []
        for r in cookie_results:
            if r.get("id") == 2:
                cookies = r.get("result", {}).get("cookies", [])
                break

        logger.info(f"[Monitor] Extracted {len(cookies)} cookies from browser session")

        if cookies:
            cookie_map = {c.get("name"): c.get("value", "") for c in cookies}
            ndus = cookie_map.get("ndus", "")
            logger.info(f"[Monitor] ndus: {'present' if ndus else 'missing'} (len={len(ndus)}), keys: {list(cookie_map.keys())}")

            new_cookies_text = _cookies_to_netscape(cookies)
            cookie_target = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
            cookie_target.parent.mkdir(parents=True, exist_ok=True)
            cookie_target.write_text(new_cookies_text, encoding="utf-8")
            logger.info(f"[Monitor] 🎉 Successfully saved {len(cookies)} cookies to {cookie_target}")
            return True, new_cookies_text, None

        return False, None, "Verification finished without capturing session cookies."

    except Exception as e:
        logger.error(f"[Monitor] Verification error: {e}", exc_info=True)
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
    """Orchestrates interactive CAPTCHA solving with Browserbase / local Chrome."""
    if not is_browserless_configured():
        return False, None, "No solver backend configured (set BROWSERBASE_API_KEY in .env)."

    logger.info(f"[Solver] ═══ Launching Interactive CAPTCHA Solver ═══")
    logger.info(f"[Solver] Target URL: {target_url}")

    success, session_obj, err = await create_captcha_session(target_url, initial_cookie)
    if not success or not session_obj:
        logger.error(f"[Solver] Session creation FAILED: {err}")
        return False, None, err or "Could not create browser session."

    inspector_url = session_obj["inspector_url"]
    mode = session_obj.get("mode", "browserbase")
    logger.info(f"[Solver] Session active (engine: {mode}). Inspector link: {inspector_url}")

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
