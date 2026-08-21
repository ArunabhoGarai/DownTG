#!/usr/bin/env python3
"""
Manual TeraBox CAPTCHA & Cookie Solver (Xvfb + noVNC Web Desktop)
==================================================================
Runs on your Ubuntu EC2 server in root directory.
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


def cookies_to_netscape(cookies: list) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated automatically after manual login",
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


def fetch_cdp_cookies_stdlib(debug_port: int = 9222) -> list:
    """Extracts 100% decrypted plaintext cookies from Chromium memory using ONLY Python standard library."""
    # 1. Fetch tab WebSocket URL via HTTP
    ws_url = None
    for _ in range(12):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{debug_port}/json/list")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    tabs = json.loads(resp.read().decode("utf-8"))
                    for t in tabs:
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                            ws_url = t.get("webSocketDebuggerUrl")
                            break
                    if not ws_url and tabs:
                        ws_url = tabs[0].get("webSocketDebuggerUrl")
                    if ws_url:
                        break
        except Exception:
            time.sleep(0.5)

    if not ws_url:
        print("⚠️  No active page WebSocket found on debug port.")
        return []

    print(f"🔌 Connecting to Chrome DevTools WebSocket (standard library)...")
    try:
        parsed = urllib.parse.urlparse(ws_url)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)
        s.connect((parsed.hostname, parsed.port))

        key = base64.b64encode(os.urandom(16)).decode("utf-8")
        req = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{debug_port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://127.0.0.1:{debug_port}\r\n\r\n"
        )
        s.sendall(req.encode("utf-8"))
        handshake = s.recv(2048).decode("utf-8", errors="ignore")
        if "101" not in handshake:
            print("⚠️  WebSocket handshake failed.")
            s.close()
            return []

        # Send Storage.getCookies command
        cmd = json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}).encode("utf-8")
        mask = os.urandom(4)
        frame = bytearray([0x81, 0x80 | len(cmd)]) + mask + bytearray(b ^ mask[i % 4] for i, b in enumerate(cmd))
        s.sendall(frame)

        # Read response
        raw = bytearray()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 4:
                # Check payload size
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
        res = json.loads(body)
        return res.get("result", {}).get("cookies", [])

    except Exception as e:
        print(f"⚠️  Error fetching cookies via CDP socket: {e}")
        return []


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

    display = ":99"
    vnc_port = 5900
    web_port = 6080
    debug_port = 9222
    processes = []

    try:
        # 1. Start Xvfb (Virtual Framebuffer)
        print(f"🖥️  Starting Xvfb virtual display on {display}...")
        xvfb_cmd = ["Xvfb", display, "-screen", "0", "1280x900x24"]
        p_xvfb = subprocess.Popen(xvfb_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_xvfb)
        time.sleep(1)

        # 2. Start x11vnc
        print(f"📡 Starting x11vnc server on port {vnc_port}...")
        x11vnc_cmd = ["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", str(vnc_port)]
        p_vnc = subprocess.Popen(x11vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_vnc)
        time.sleep(1)

        # 3. Start websockify / noVNC
        print(f"🌐 Starting web noVNC proxy on port {web_port}...")
        novnc_cmd = ["websockify", "--web", "/usr/share/novnc", str(web_port), f"localhost:{vnc_port}"]
        p_novnc = subprocess.Popen(novnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_novnc)
        time.sleep(1)

        # 4. Start Chromium in the virtual display with debugging port
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
            "https://www.terabox.app/",
        ]
        print(f"🚀 Launching Chromium on virtual display...")
        p_chrome = subprocess.Popen(chrome_args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_chrome)

        # Detect public IP
        public_ip = os.getenv("SERVER_PUBLIC_IP", "")
        if not public_ip:
            try:
                public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode('utf-8').strip()
            except Exception:
                public_ip = "YOUR_EC2_PUBLIC_IP"

        vnc_url = f"http://{public_ip}:{web_port}/vnc.html?autoconnect=true&resize=scale"

        print("\n" + "=" * 65)
        print("✅ Virtual Desktop is LIVE!")
        print("=" * 65)
        print(f"👉 Open this link in your browser (phone or PC):\n   {vnc_url}\n")
        print("👉 Instructions:")
        print("   1. Open the link above in your browser.")
        print("   2. You will see the full Chrome desktop running on your EC2.")
        print("   3. Log into TeraBox or solve the verification CAPTCHA slider.")
        print("   4. Once you are logged in / solved...")
        print("   5. Come back to this terminal and press [ENTER] to save cookies!")
        print("=" * 65 + "\n")

        input("👉 Press [ENTER] here once you have solved the captcha / logged in: ")

        print("\n⏳ Extracting live plaintext cookies directly from Chrome memory...")
        cookies = fetch_cdp_cookies_stdlib(debug_port)

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
            print("❌ Could not capture cookies from Chrome memory. Please make sure Chrome is open and logged in.")

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
