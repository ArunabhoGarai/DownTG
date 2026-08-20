import os
import json
import time
import asyncio
import logging
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


def is_browserless_configured() -> bool:
    """Checks if Browserless endpoint is configured."""
    return bool(BROWSERLESS_URL)


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


async def create_captcha_session(
    target_url: str,
    initial_cookie_raw: Optional[str] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Creates a new browser tab in Browserless via CDP, sets existing cookies,
    navigates to target_url, and builds the Live Inspector link for the user.
    """
    if not is_browserless_configured():
        return False, None, "Browserless is not configured in .env."

    token_param = f"?token={BROWSERLESS_TOKEN}" if BROWSERLESS_TOKEN else ""
    new_page_endpoint = f"{BROWSERLESS_URL}/json/new{token_param}"

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        client_timeout = aiohttp.ClientTimeout(total=20, connect=5)
        async with aiohttp.ClientSession(connector=connector, timeout=client_timeout) as http_session:
            async with http_session.put(new_page_endpoint) as resp:
                if resp.status != 200:
                    # Fallback to GET /json/new
                    async with http_session.get(new_page_endpoint) as resp_get:
                        if resp_get.status != 200:
                            return False, None, f"Failed to create tab on Browserless (HTTP {resp_get.status})"
                        tab_data = await resp_get.json(content_type=None)
                else:
                    tab_data = await resp.json(content_type=None)

        page_id = tab_data.get("id")
        ws_url = tab_data.get("webSocketDebuggerUrl")
        if not page_id or not ws_url:
            return False, None, "Invalid response from Browserless /json/new."

        # Ensure internal ws_url has the token parameter if required by Browserless
        if BROWSERLESS_TOKEN and "token=" not in ws_url:
            delimiter = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{delimiter}token={BROWSERLESS_TOKEN}"

        # Build public Live Inspector link for the user
        public_base = (BROWSERLESS_PUBLIC_URL or BROWSERLESS_URL).strip().rstrip("/")
        if not public_base.startswith(("http://", "https://")):
            public_base = f"http://{public_base}"
        parsed_public = urllib.parse.urlparse(public_base)
        public_host_port = parsed_public.netloc or parsed_public.path or "127.0.0.1:3000"
        
        # Build WebSocket target path for DevTools frontend
        ws_path = f"{public_host_port}/devtools/page/{page_id}"
        token_query = f"&token={BROWSERLESS_TOKEN}" if BROWSERLESS_TOKEN else ""
        inspector_url = f"{public_base}/devtools/inspector.html?ws={ws_path}{token_query}"

        # Connect to CDP WebSocket to prepare the session
        ws_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        ws = await ws_session.ws_connect(ws_url, timeout=30)

        # 1. Enable domains
        await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
        await ws.send_json({"id": 2, "method": "Page.enable", "params": {}})
        await ws.send_json({"id": 3, "method": "Runtime.enable", "params": {}})

        # 2. Inject existing cookies if present
        if initial_cookie_raw:
            cdp_cookies = _parse_raw_cookies_for_cdp(initial_cookie_raw)
            if cdp_cookies:
                await ws.send_json({
                    "id": 4,
                    "method": "Network.setCookies",
                    "params": {"cookies": cdp_cookies},
                })

        # 3. Navigate to target URL
        await ws.send_json({
            "id": 5,
            "method": "Page.navigate",
            "params": {"url": target_url},
        })

        session_obj = {
            "page_id": page_id,
            "ws": ws,
            "ws_session": ws_session,
            "inspector_url": inspector_url,
            "target_url": target_url,
            "created_at": time.time(),
        }

        logger.info(f"Created Browserless CAPTCHA session {page_id}. Inspector: {inspector_url}")
        return True, session_obj, None

    except Exception as e:
        logger.error(f"Error creating Browserless session: {e}", exc_info=True)
        return False, None, str(e)


async def close_captcha_session(session_obj: Dict[str, Any]) -> None:
    """Safely closes the WebSocket and kills the browser tab on Browserless."""
    if not session_obj:
        return

    ws = session_obj.get("ws")
    ws_session = session_obj.get("ws_session")
    page_id = session_obj.get("page_id")

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

    if page_id:
        token_param = f"?token={BROWSERLESS_TOKEN}" if BROWSERLESS_TOKEN else ""
        close_endpoint = f"{BROWSERLESS_URL}/json/close/{page_id}{token_param}"
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=5)) as http_s:
                async with http_s.get(close_endpoint) as resp:
                    logger.debug(f"Closed Browserless tab {page_id}: status {resp.status}")
        except Exception as e:
            logger.debug(f"Failed to close tab {page_id}: {e}")


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
    page_id = session_obj.get("page_id")
    if not ws:
        return False, None, "WebSocket is not connected."

    start_time = time.time()
    last_poll_time = 0.0
    req_id_counter = 100
    solved = False
    new_cookies_text = None

    try:
        while time.time() - start_time < timeout_sec:
            remaining = int(timeout_sec - (time.time() - start_time))
            if remaining % 30 == 0 and progress_callback:
                try:
                    await progress_callback(f"⏳ Waiting for CAPTCHA solution ({remaining}s remaining)...")
                except Exception:
                    pass

            # Periodically poll for cookies and successful verification requests
            now = time.time()
            if now - last_poll_time >= 2.5:
                last_poll_time = now
                req_id_counter += 1
                # Poll Network.getCookies
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

            # Read incoming CDP events with small timeout
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.5)
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)

                    # 1. Inspect responses to Network.getCookies
                    if "result" in data and "cookies" in data["result"]:
                        cookies_list = data["result"]["cookies"]
                        cookie_names = {c.get("name"): c.get("value") for c in cookies_list}
                        
                        # Check if active session tokens are present
                        if "ndus" in cookie_names and len(cookie_names["ndus"]) > 10:
                            # If we see browserid or csrfToken alongside ndus, session is authenticated
                            new_cookies_text = _cookies_to_netscape(cookies_list)

                    # 2. Inspect Network.responseReceived events
                    if data.get("method") == "Network.responseReceived":
                        params = data.get("params", {})
                        resp_obj = params.get("response", {})
                        resp_url = resp_obj.get("url", "")
                        
                        # Detect successful filelist or verification API calls
                        if ("share/list" in resp_url or "api/shorturlinfo" in resp_url) and resp_obj.get("status") == 200:
                            solved = True

                    # 3. Detect URL changes away from verification pages
                    if data.get("method") == "Page.frameNavigated":
                        nav_url = data.get("params", {}).get("frame", {}).get("url", "")
                        if nav_url and not any(kw in nav_url.lower() for kw in ["verify", "captcha", "safe/"]):
                            # User completed navigation
                            if new_cookies_text:
                                solved = True

            except asyncio.TimeoutError:
                pass

            if solved and new_cookies_text:
                break

        if solved and new_cookies_text:
            # Save newly obtained cookies to disk
            cookie_target = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
            cookie_target.parent.mkdir(parents=True, exist_ok=True)
            with open(cookie_target, "w", encoding="utf-8") as f:
                f.write(new_cookies_text)
            logger.info(f"CAPTCHA solved successfully! Saved updated cookies to {cookie_target}")
            return True, new_cookies_text, None

        if not solved:
            return False, None, f"CAPTCHA verification timed out after {timeout_sec} seconds."

    except Exception as e:
        logger.error(f"Error while waiting for CAPTCHA solution: {e}", exc_info=True)
        return False, None, str(e)

    finally:
        await close_captcha_session(session_obj)


async def solve_terabox_captcha_interactive(
    target_url: str,
    initial_cookie: Optional[str] = None,
    notify_admin_callback: Optional[Callable[[str, str], None]] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Complete high-level workflow:
    1. Launches browser tab on Browserless.
    2. Sends Live Inspector link to admin.
    3. Waits for admin to solve the slider.
    4. Extracts fresh cookies and saves them.
    """
    if not is_browserless_configured():
        return False, None, "Browserless is not configured."

    success, session_obj, err = await create_captcha_session(target_url, initial_cookie)
    if not success or not session_obj:
        return False, None, err or "Could not create browser session."

    inspector_url = session_obj["inspector_url"]
    
    # Notify admin via Telegram with one-click solve link
    if notify_admin_callback:
        try:
            await notify_admin_callback(inspector_url, target_url)
        except Exception as e:
            logger.warning(f"Failed to notify admin of CAPTCHA URL: {e}")

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
