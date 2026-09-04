import os
import sys
import time
import shutil
import asyncio
import logging
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Callable, Awaitable, Tuple

from config import BASE_DIR
from diskwala_downloader import save_diskwala_token
from terabox_downloader import save_terabox_token

logger = logging.getLogger(__name__)

# Directory where Chrome profile for Telegram Web is saved permanently
PROFILE_DIR = BASE_DIR / "data" / "tg_browser_profile"

# Global reference to running VNC capture process and task
_ACTIVE_VNC_PROCESS: Optional[asyncio.subprocess.Process] = None
_ACTIVE_READER_TASK: Optional[asyncio.Task] = None


def is_process_running(name: str) -> bool:
    """Checks if a process matching name is running."""
    try:
        res = subprocess.run(["pgrep", "-f", name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return res.returncode == 0
    except Exception:
        return False


def get_public_ip() -> str:
    """Detects server public IP address."""
    env_ip = os.getenv("SERVER_PUBLIC_IP", "").strip()
    if env_ip:
        return env_ip
    for service in [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]:
        try:
            req = urllib.request.Request(service, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=3) as res:
                ip = res.read().decode("utf-8").strip()
                if ip:
                    return ip
        except Exception:
            continue
    return "YOUR_SERVER_IP"


def ensure_vnc_running() -> Tuple[bool, str]:
    """
    Ensures Xvfb (:99), x11vnc (:5900), and noVNC websockify (:6080) are active.
    Returns (success, novnc_web_url).
    """
    if sys.platform != "linux":
        # On Windows development environments, Chrome opens in normal desktop window
        return True, "http://localhost:6080/vnc.html?autoconnect=true&resize=scale"

    display = ":99"
    xvfb_running = is_process_running("Xvfb :99")
    if not xvfb_running:
        for lock_file in ["/tmp/.X11-unix/X99", "/tmp/.X99-lock"]:
            if os.path.exists(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass

        logger.info("Starting Xvfb virtual display on :99 (1920x1080)...")
        try:
            subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac", "+extension", "GLX", "+render", "-noreset"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.2)
        except Exception as e:
            logger.error(f"Failed to start Xvfb: {e}")
            return False, f"Failed to start Xvfb: {e}"

    if not is_process_running("x11vnc"):
        logger.info("Starting x11vnc on port 5900...")
        try:
            subprocess.Popen(
                ["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", "5900"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"x11vnc start warning: {e}")

    if not is_process_running("websockify"):
        logger.info("Starting noVNC websockify on port 6080...")
        try:
            subprocess.Popen(
                ["websockify", "--web", "/usr/share/novnc", "6080", "localhost:5900"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"websockify start warning: {e}")

    public_ip = get_public_ip()
    vnc_url = f"http://{public_ip}:6080/vnc.html?autoconnect=true&resize=scale"
    return True, vnc_url


DEFAULT_BOT_URLS = {
    "diskwala": "https://web.telegram.org/a/#7802633228",
    "tera": "https://web.telegram.org/a/#7802009139",
    "terabox": "https://web.telegram.org/a/#7802009139",
}


def format_telegram_web_url(target_input: Optional[str], platform: str = "diskwala") -> str:
    """
    Formats a target link, peer ID, or bot username into a direct Telegram Web URL.
    Defaults:
      - Diskwala: https://web.telegram.org/a/#7802633228
      - TeraBox: https://web.telegram.org/a/#7802009139
    """
    default_url = DEFAULT_BOT_URLS.get(platform.lower(), "https://web.telegram.org/a/#7802633228")
    if not target_input:
        return default_url

    t = target_input.strip()
    if t.startswith("http://") or t.startswith("https://"):
        if "t.me/" in t:
            username = t.split("t.me/")[1].split("/")[0].split("?")[0].lstrip("@")
            return f"https://web.telegram.org/a/#?tgaddr=tg%3A%2F%2Fresolve%3Fdomain%3D{username}"
        return t

    # If user provided a raw peer ID like "7802633228" or "#7802633228"
    if t.startswith("#"):
        return f"https://web.telegram.org/a/{t}"
    if t.isdigit():
        return f"https://web.telegram.org/a/#{t}"

    clean_username = t.lstrip("@").strip()
    if clean_username:
        return f"https://web.telegram.org/a/#?tgaddr=tg%3A%2F%2Fresolve%3Fdomain%3D{clean_username}"

    return default_url


async def start_vnc_capture_session(
    target_bot: Optional[str] = None,
    on_token_captured: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Tuple[bool, str, str]:
    """
    Starts VNC stack and launches Chrome GUI with persistent profile.
    Returns (success, vnc_web_url, error_message).
    """
    global _ACTIVE_VNC_PROCESS, _ACTIVE_READER_TASK

    # Terminate any existing capture process first
    await stop_vnc_session()

    ok, vnc_url = ensure_vnc_running()
    if not ok:
        return False, "", vnc_url

    script_path = BASE_DIR / "vnc_diskwala_capture.js"
    if not script_path.exists():
        return False, "", "vnc_diskwala_capture.js not found."

    target_url = format_telegram_web_url(target_bot)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if sys.platform == "linux":
        env["DISPLAY"] = ":99"

    node_bin = shutil.which("node") or "node"
    cmd = [
        node_bin,
        str(script_path),
        target_url,
        str(PROFILE_DIR),
    ]

    logger.info(f"[VNC Manager] Launching browser worker: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _ACTIVE_VNC_PROCESS = proc

        async def _reader():
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                logger.info(f"[VNC Worker] {line}")
                if line.startswith("[TERABOX_TOKEN_CAPTURED]"):
                    captured_token = line.replace("[TERABOX_TOKEN_CAPTURED]", "").strip()
                    logger.info("[VNC Manager] 🎉 Captured TeraBox Token from MiniApp!")
                    save_terabox_token(captured_token)
                    if on_token_captured:
                        try:
                            await on_token_captured("terabox", captured_token)
                        except Exception as cb_err:
                            logger.error(f"[VNC Manager] Callback error: {cb_err}")

                elif line.startswith("[DISKWALA_TOKEN_CAPTURED]") or line.startswith("[TOKEN_CAPTURED]"):
                    captured_token = line.replace("[DISKWALA_TOKEN_CAPTURED]", "").replace("[TOKEN_CAPTURED]", "").strip()
                    logger.info("[VNC Manager] 🎉 Captured Diskwala Token from MiniApp!")
                    save_diskwala_token(captured_token)
                    if on_token_captured:
                        try:
                            await on_token_captured("diskwala", captured_token)
                        except Exception as cb_err:
                            logger.error(f"[VNC Manager] Callback error: {cb_err}")

        _ACTIVE_READER_TASK = asyncio.create_task(_reader())
        return True, vnc_url, ""

    except Exception as e:
        logger.error(f"[VNC Manager] Failed to launch VNC capture session: {e}")
        return False, "", str(e)


async def start_autovnc_session(
    platform: str,
    target_bot: Optional[str] = None,
    on_token_captured: Optional[Callable[[str, str], Awaitable[None]]] = None,
    progress_updater: Optional[Callable[[str], Awaitable[None]]] = None,
) -> Tuple[bool, str, str]:
    """
    Starts VNC stack and launches automated Puppeteer Extra Stealth worker to navigate Telegram Web,
    open MiniApp, enter dummy link, click download, and auto-capture bearer token.
    Returns (success, vnc_web_url, error_message).
    """
    global _ACTIVE_VNC_PROCESS, _ACTIVE_READER_TASK

    await stop_vnc_session()

    ok, vnc_url = ensure_vnc_running()
    if not ok:
        return False, "", vnc_url

    script_path = BASE_DIR / "vnc_auto_capture.js"
    if not script_path.exists():
        return False, "", "vnc_auto_capture.js not found."

    target_url = format_telegram_web_url(target_bot, platform=platform)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if sys.platform == "linux":
        env["DISPLAY"] = ":99"

    node_bin = shutil.which("node") or "node"
    cmd = [
        node_bin,
        str(script_path),
        platform.lower(),
        target_url,
        str(PROFILE_DIR),
    ]

    logger.info(f"[AutoVNC Manager] Launching auto-capture worker: {' '.join(cmd)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _ACTIVE_VNC_PROCESS = proc

        async def _reader():
            last_update_time = 0
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                logger.info(f"[AutoVNC Worker] {line}")

                if line.startswith("[AUTOVNC_STATUS]") and progress_updater:
                    now = time.time()
                    if now - last_update_time > 1.8:
                        last_update_time = now
                        status_text = line.replace("[AUTOVNC_STATUS]", "").strip()
                        try:
                            await progress_updater(status_text)
                        except Exception:
                            pass

                elif line.startswith("[AUTOVNC_LOGIN_REQUIRED]") and progress_updater:
                    try:
                        await progress_updater("⚠️ **Telegram Web is logged out!**\nPlease run `/vnc` first to scan QR code / log in.")
                    except Exception:
                        pass

                elif line.startswith("[TERABOX_TOKEN_CAPTURED]"):
                    captured_token = line.replace("[TERABOX_TOKEN_CAPTURED]", "").strip()
                    logger.info("[AutoVNC Manager] 🎉 Auto-Captured TeraBox Token!")
                    save_terabox_token(captured_token)
                    if on_token_captured:
                        try:
                            await on_token_captured("terabox", captured_token)
                        except Exception as cb_err:
                            logger.error(f"[AutoVNC Callback Error]: {cb_err}")

                elif line.startswith("[DISKWALA_TOKEN_CAPTURED]") or line.startswith("[TOKEN_CAPTURED]"):
                    captured_token = line.replace("[DISKWALA_TOKEN_CAPTURED]", "").replace("[TOKEN_CAPTURED]", "").strip()
                    logger.info("[AutoVNC Manager] 🎉 Auto-Captured Diskwala Token!")
                    save_diskwala_token(captured_token)
                    if on_token_captured:
                        try:
                            await on_token_captured("diskwala", captured_token)
                        except Exception as cb_err:
                            logger.error(f"[AutoVNC Callback Error]: {cb_err}")

        _ACTIVE_READER_TASK = asyncio.create_task(_reader())
        return True, vnc_url, ""

    except Exception as e:
        logger.error(f"[AutoVNC Manager] Failed to launch Auto-VNC worker: {e}")
        return False, "", str(e)


async def stop_vnc_session() -> bool:
    """Stops the active VNC browser session and cleans up Chrome processes."""
    global _ACTIVE_VNC_PROCESS, _ACTIVE_READER_TASK

    if _ACTIVE_READER_TASK and not _ACTIVE_READER_TASK.done():
        _ACTIVE_READER_TASK.cancel()
    _ACTIVE_READER_TASK = None

    if _ACTIVE_VNC_PROCESS:
        try:
            _ACTIVE_VNC_PROCESS.terminate()
            await asyncio.sleep(0.5)
        except Exception:
            pass
        _ACTIVE_VNC_PROCESS = None

    if sys.platform == "linux":
        try:
            subprocess.run(["pkill", "-f", "vnc_diskwala_capture.js"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-f", "vnc_auto_capture.js"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["pkill", "-9", "-f", "chrome.*(1920,1080|tg_browser_profile)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    return True
