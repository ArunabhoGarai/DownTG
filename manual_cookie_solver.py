#!/usr/bin/env python3
"""
Manual TeraBox CAPTCHA & Cookie Solver (Xvfb + noVNC Web Desktop)
==================================================================
Runs on your Ubuntu EC2 server. Launches a full virtual desktop with Chrome,
serves a web-accessible noVNC viewer in your browser, lets you log in / solve
the CAPTCHA on your server's own IP address, and automatically exports the
valid Netscape session cookies to cooky/terabox/cookies.txt.

Usage on EC2:
    python scripts/manual_cookie_solver.py
"""

import os
import sys
import time
import shutil
import sqlite3
import subprocess
import signal
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
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


def export_sqlite_cookies(profile_dir: Path, output_file: Path):
    """Reads Chromium's SQLite cookie database and exports to standard Netscape cookies.txt."""
    possible_dbs = [
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Default" / "Cookies",
        profile_dir / "Cookies",
    ]
    db_path = None
    for p in possible_dbs:
        if p.exists() and p.is_file():
            db_path = p
            break

    if not db_path:
        print(f"⚠️  Could not locate Chromium Cookies DB in {profile_dir}")
        return False

    temp_copy = profile_dir / "temp_cookies.db"
    try:
        shutil.copy2(db_path, temp_copy)
        conn = sqlite3.connect(temp_copy)
        cursor = conn.cursor()

        cursor.execute("SELECT host_key, path, is_secure, expires_utc, name, value, encrypted_value FROM cookies WHERE host_key LIKE '%terabox%' OR host_key LIKE '%1024tera%'")
        rows = cursor.fetchall()
        conn.close()

        lines = [
            "# Netscape HTTP Cookie File",
            "# Exported from manual Ubuntu browser session",
            "",
        ]

        for host_key, path, is_secure, expires_utc, name, value, enc_value in rows:
            include_sub = "TRUE" if host_key.startswith(".") else "FALSE"
            secure_str = "TRUE" if is_secure else "FALSE"
            # Chromium epoch to unix epoch
            exp_unix = int((expires_utc / 1000000) - 11644473600) if expires_utc > 0 else 2147483647
            val = value
            # On Linux if unencrypted value is present
            if not val and enc_value:
                # Try simple string decode or fallback
                try:
                    val = enc_value.decode('utf-8', errors='ignore')
                except Exception:
                    val = str(enc_value)

            if val:
                lines.append(f"{host_key}\t{include_sub}\t{path}\t{secure_str}\t{exp_unix}\t{name}\t{val}")

        if len(lines) > 3:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text("\n".join(lines), encoding="utf-8")
            print(f"🎉 Successfully exported {len(rows)} cookies to: {output_file}")
            return True
        else:
            print("⚠️  No TeraBox cookies found in SQLite database.")
            return False

    except Exception as e:
        print(f"❌ Error reading cookies SQLite DB: {e}")
        return False
    finally:
        if temp_copy.exists():
            temp_copy.unlink()


def main():
    chrome_bin = find_chrome_binary()
    if not chrome_bin:
        print("❌ Chromium is not installed. Install with: sudo apt update && sudo apt install -y chromium-browser")
        sys.exit(1)

    print("=" * 65)
    print("🧩 TeraBox Manual CAPTCHA & Cookie Solver (Xvfb + noVNC)")
    print("=" * 65)

    novnc_path = shutil.which("novnc") or "/usr/share/novnc" or shutil.which("novnc_proxy")
    has_xvfb = bool(shutil.which("Xvfb"))
    has_x11vnc = bool(shutil.which("x11vnc"))

    if not has_xvfb or not has_x11vnc:
        print("\n⚠️  Required packages missing. Please install them by running:")
        print("    sudo apt update && sudo apt install -y xvfb x11vnc novnc websockify\n")
        sys.exit(1)

    display = ":99"
    vnc_port = 5900
    web_port = 6080
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

        # 4. Start Chromium in the virtual display
        env = os.environ.copy()
        env["DISPLAY"] = display
        chrome_args = [
            chrome_bin,
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
                import urllib.request
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
        print("   3. Log into TeraBox or slide the verification CAPTCHA puzzle.")
        print("   4. Once you are logged in and see your files on the screen...")
        print("   5. Come back to this terminal and press [ENTER] to save cookies!")
        print("=" * 65 + "\n")

        input("👉 Press [ENTER] here once you have solved the captcha / logged in: ")

        print("\n⏳ Extracting session cookies from browser profile...")
        time.sleep(1)

        # Terminate Chrome so SQLite DB is flushed
        p_chrome.terminate()
        try:
            p_chrome.wait(timeout=3)
        except Exception:
            pass

        success = export_sqlite_cookies(PROFILE_DIR, COOKIE_OUTPUT_PATH)
        if success:
            print("✅ All set! Your Telegram bot can now download without CAPTCHA blocks.")
        else:
            print("⚠️  Cookies could not be extracted automatically. You can copy-paste cookie string into .env TERABOX_COOKIE.")

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
