#!/usr/bin/env python3
"""
Manual TeraBox CAPTCHA & Cookie Solver (Xvfb + noVNC Web Desktop)
==================================================================
Runs on your Ubuntu EC2 server in root directory.
Pre-injects existing cookies into Chrome so you can visually verify
your session in the VNC browser, solve any slider challenges or log in,
and save the fresh valid cookies.

Uses 100% Python Standard Library (ZERO pip dependencies required).

Usage on EC2:
    python3 manual_cookie_solver.py
"""

import os
import sys
import time
import json
import socket
import struct
import base64
import shutil
import urllib.parse
import urllib.request
import subprocess
from pathlib import Path

# Root directory of DownTG
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

COOKIE_OUTPUT_PATH = BASE_DIR / "cooky" / "terabox" / "cookies.txt"
PROFILE_DIR = BASE_DIR / ".chrome_manual_profile"


def find_chrome_binary():
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


def parse_raw_cookies_for_cdp(raw_cookie: str) -> list:
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


def load_existing_cookies() -> tuple:
    """Loads existing cookies from cooky/terabox/cookies.txt or .env."""
    raw_content = ""
    source = "none"

    if COOKIE_OUTPUT_PATH.exists() and COOKIE_OUTPUT_PATH.is_file():
        try:
            raw_content = COOKIE_OUTPUT_PATH.read_text(encoding="utf-8", errors="ignore").strip()
            if raw_content:
                source = str(COOKIE_OUTPUT_PATH)
        except Exception:
            pass

    if not raw_content:
        # Check .env file
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("TERABOX_COOKIE="):
                    raw_content = line.split("=", 1)[1].strip().strip("'\"")
                    if raw_content:
                        source = ".env (TERABOX_COOKIE)"
                        break

    cdp_cookies = parse_raw_cookies_for_cdp(raw_content) if raw_content else []
    return cdp_cookies, source


def cookies_to_netscape(cookies: list) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically after manual verification",
        "",
    ]
    for c in cookies:
        domain = c.get("domain", ".terabox.app")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"
        expires = int(c.get("expires", 0)) if c.get("expires") else 2147483647
        name, value = c.get("name", ""), c.get("value", "")
        if name and value:
            lines.append(f"{domain}\t{include_sub}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines)


def get_active_ws_url(debug_port: int = 9222) -> str:
    """Finds active page WebSocket URL in Chrome."""
    for _ in range(15):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{debug_port}/json/list")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    tabs = json.loads(resp.read().decode("utf-8"))
                    for t in tabs:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            return t.get("webSocketDebuggerUrl")
                    if tabs and tabs[0].get("webSocketDebuggerUrl"):
                        return tabs[0].get("webSocketDebuggerUrl")
        except Exception:
            time.sleep(0.5)
    return ""


def send_cdp_command(ws_url: str, method: str, params: dict = None, cmd_id: int = 1) -> dict:
    """Sends a single CDP command and returns result dict via pure standard library socket."""
    if not ws_url:
        return {}
    parsed = urllib.parse.urlparse(ws_url)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((parsed.hostname, parsed.port))

        key = base64.b64encode(os.urandom(16)).decode("utf-8")
        req = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{parsed.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1:{parsed.port}\r\n\r\n"
        )
        s.sendall(req.encode("utf-8"))
        handshake = s.recv(2048).decode("utf-8", errors="ignore")
        if "101" not in handshake:
            s.close()
            return {}

        cmd = json.dumps({"id": cmd_id, "method": method, "params": params or {}}).encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81, 0x80 | len(cmd)]) + mask + bytearray(b ^ mask[i % 4] for i, b in enumerate(cmd))
        s.sendall(frame)

        raw = bytearray()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 4:
                payload_len = raw[1] & 0x7F
                offset = 2
                if payload_len == 126:
                    if len(raw) < 4:
                        continue
                    payload_len = struct.unpack(">H", raw[2:4])[0]
                    offset = 4
                elif payload_len == 127:
                    if len(raw) < 10:
                        continue
                    payload_len = struct.unpack(">Q", raw[2:10])[0]
                    offset = 10
                if len(raw) >= offset + payload_len:
                    break

        s.close()

        payload_len = raw[1] & 0x7F
        offset = 2
        if payload_len == 126:
            payload_len = struct.unpack(">H", raw[2:4])[0]
            offset = 4
        elif payload_len == 127:
            payload_len = struct.unpack(">Q", raw[2:10])[0]
            offset = 10

        body = raw[offset:offset + payload_len].decode("utf-8", errors="ignore")
        return json.loads(body)
    except Exception as e:
        print(f"⚠️  CDP command error ({method}): {e}")
        return {}


def preinject_cookies_into_chrome(debug_port: int, cdp_cookies: list, navigate_url: str):
    """Pre-injects existing cookies into running Chrome and navigates to the user's dashboard."""
    ws_url = get_active_ws_url(debug_port)
    if not ws_url:
        print("⚠️  Could not find Chrome WebSocket to inject cookies.")
        return

    print(f"💉 Pre-injecting {len(cdp_cookies)} existing cookies into Chrome session...")
    send_cdp_command(ws_url, "Network.enable", cmd_id=1)
    send_cdp_command(ws_url, "Network.setCookies", {"cookies": cdp_cookies}, cmd_id=2)
    print(f"🚀 Navigating Chrome to {navigate_url}...")
    send_cdp_command(ws_url, "Page.navigate", {"url": navigate_url}, cmd_id=3)
    print("✅ Cookies loaded! When you open VNC, you will see your active session.")


def fetch_all_cookies(debug_port: int = 9222) -> list:
    """Extracts live cookies from running Chrome memory."""
    ws_url = get_active_ws_url(debug_port)
    if not ws_url:
        return []

    # 1. Try Storage.getCookies
    res = send_cdp_command(ws_url, "Storage.getCookies", {}, cmd_id=10)
    cookies = res.get("result", {}).get("cookies", [])
    if cookies:
        return cookies

    # 2. Fallback Network.getCookies
    send_cdp_command(ws_url, "Network.enable", {}, cmd_id=11)
    res = send_cdp_command(ws_url, "Network.getCookies", {
        "urls": [
            "https://www.1024terabox.com",
            "https://www.terabox.app",
            "https://terabox.com",
            "https://1024tera.com",
        ]
    }, cmd_id=12)
    return res.get("result", {}).get("cookies", [])


def main():
    chrome_bin = find_chrome_binary()
    if not chrome_bin:
        print("❌ Chromium is not installed. Install with: sudo apt update && sudo apt install -y chromium-browser")
        sys.exit(1)

    print("=" * 65)
    print("🧩 TeraBox Manual CAPTCHA & Cookie Solver (Xvfb + noVNC)")
    print("=" * 65)

    has_xvfb = bool(shutil.which("Xvfb"))
    has_x11vnc = bool(shutil.which("x11vnc"))

    if not has_xvfb or not has_x11vnc:
        print("\n⚠️  Required packages missing. Please install them by running:")
        print("    sudo apt update && sudo apt install -y xvfb x11vnc novnc websockify\n")
        sys.exit(1)

    # 1. Load existing cookies to inject
    cdp_cookies, cookie_source = load_existing_cookies()
    if cdp_cookies:
        cookie_names = [c.get("name") for c in cdp_cookies]
        has_ndus = any(c.get("name") == "ndus" for c in cdp_cookies)
        print(f"📂 Found {len(cdp_cookies)} existing cookies from: {cookie_source}")
        print(f"🍪 Cookie names: {cookie_names}")
        print(f"🔑 'ndus' cookie: {'✅ PRESENT' if has_ndus else '⚠️ MISSING'}")
    else:
        print("ℹ️  No existing cookies found. Starting fresh browser session.")

    display = ":99"
    vnc_port = 5900
    web_port = 6080
    debug_port = 9222
    processes = []

    try:
        # 2. Start Xvfb (Virtual Framebuffer)
        print(f"\n🖥️  Starting Xvfb virtual display on {display}...")
        xvfb_cmd = ["Xvfb", display, "-screen", "0", "1280x900x24"]
        p_xvfb = subprocess.Popen(xvfb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_xvfb)
        time.sleep(1)

        # 3. Start x11vnc
        print(f"📡 Starting x11vnc server on port {vnc_port}...")
        x11vnc_cmd = ["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", str(vnc_port)]
        p_vnc = subprocess.Popen(x11vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_vnc)
        time.sleep(1)

        # 4. Start websockify / noVNC
        print(f"🌐 Starting web noVNC proxy on port {web_port}...")
        novnc_cmd = ["websockify", "--web", "/usr/share/novnc", str(web_port), f"localhost:{vnc_port}"]
        p_novnc = subprocess.Popen(novnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_novnc)
        time.sleep(1)

        # 5. Start Chromium in virtual display with blank page first so we can inject cookies
        env = os.environ.copy()
        env["DISPLAY"] = display
        chrome_args = [
            chrome_bin,
            f"--remote-debugging-port={debug_port}",
            "--remote-debugging-address=0.0.0.0",
            "--remote-allow-origins=*",
            f"--user-data-dir={PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--window-size=1280,900",
            "about:blank",
        ]
        print(f"🚀 Launching Chromium on virtual display...")
        p_chrome = subprocess.Popen(chrome_args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_chrome)
        time.sleep(2)

        # 6. Pre-inject cookies into Chrome if available
        target_dashboard = sys.argv[1] if len(sys.argv) > 1 else "https://www.terabox.app/main?category=all"
        if cdp_cookies:
            preinject_cookies_into_chrome(debug_port, cdp_cookies, target_dashboard)
        else:
            ws_url = get_active_ws_url(debug_port)
            if ws_url:
                send_cdp_command(ws_url, "Page.navigate", {"url": target_dashboard})

        # Detect public IP
        public_ip = os.getenv("SERVER_PUBLIC_IP", "")
        if not public_ip:
            try:
                public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode('utf-8').strip()
            except Exception:
                public_ip = "YOUR_EC2_PUBLIC_IP"

        vnc_url = f"http://{public_ip}:{web_port}/vnc.html?autoconnect=true&resize=scale"

        print("\n" + "=" * 65)
        print("✅ Virtual Desktop is LIVE with your cookies loaded!")
        print("=" * 65)
        print(f"👉 Open this link in your browser (phone or PC):\n   {vnc_url}\n")
        print("👉 Instructions:")
        print("   1. Open the link above in your browser.")
        print("   2. You will see Chrome running on your EC2 with your cookies.")
        print("   3. Check if you are logged in, or solve any CAPTCHA puzzle.")
        print("   4. Once you see your files / logged in dashboard...")
        print("   5. Come back to this terminal and press [ENTER] to save updated cookies!")
        print("=" * 65 + "\n")

        input("👉 Press [ENTER] here once you have checked/solved the session: ")

        print("\n⏳ Extracting live plaintext cookies directly from Chrome memory...")
        cookies = fetch_all_cookies(debug_port)

        if cookies:
            cookie_map = {c.get("name"): c.get("value", "") for c in cookies}
            ndus = cookie_map.get("ndus", "")
            print(f"📋 Captured {len(cookies)} cookies total!")
            print(f"🍪 Cookie names: {list(cookie_map.keys())}")
            print(f"🔑 'ndus' cookie: {'✅ PRESENT (len=' + str(len(ndus)) + ')' if ndus else '⚠️ MISSING'}")

            netscape_text = cookies_to_netscape(cookies)
            COOKIE_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_OUTPUT_PATH.write_text(netscape_text, encoding="utf-8")
            print(f"🎉 Successfully saved cookies to: {COOKIE_OUTPUT_PATH}")
            print("✅ All set! You can restart tgbot.service now.")
        else:
            print("❌ Could not capture cookies from Chrome memory.")

    except KeyboardInterrupt:
        print("\n🛑 Aborted by user.")
    finally:
        print("🧹 Cleaning up virtual desktop processes...")
        for p in processes:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        print("👋 Done!")


if __name__ == "__main__":
    main()
