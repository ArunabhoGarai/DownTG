#!/usr/bin/env python3
"""
Live VNC Puppeteer Crawler Viewer for Ubuntu EC2
=================================================
Runs on your EC2 server in root directory.
Starts Xvfb virtual desktop, x11vnc, noVNC web viewer, and executes
terabox_crawler.js in full GUI mode so you can watch Chrome live in your
browser at http://<EC2-IP>:6080/vnc.html!

Usage on EC2:
    python3 live_vnc_crawler.py "https://www.terabox.app/sharing/link?surl=LVTGV9gLseLGpNb-FJUlkg"
"""

import os
import sys
import time
import shutil
import subprocess
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

def main():
    target_url = sys.argv[1] if len(sys.argv) > 1 else "https://www.terabox.app/sharing/link?surl=LVTGV9gLseLGpNb-FJUlkg"
    
    print("=" * 65)
    print("📺 TeraBox Live VNC Puppeteer Crawler Viewer")
    print("=" * 65)

    has_xvfb = bool(shutil.which("Xvfb"))
    has_x11vnc = bool(shutil.which("x11vnc"))
    has_node = bool(shutil.which("node"))

    if not has_xvfb or not has_x11vnc:
        print("\n⚠️ Installing required Xvfb / VNC packages...")
        subprocess.run(["sudo", "apt", "update", "-y"])
        subprocess.run(["sudo", "apt", "install", "-y", "xvfb", "x11vnc", "novnc", "websockify"])

    display = ":99"
    vnc_port = 5900
    web_port = 6080
    processes = []

    try:
        # Kill any old lingering Xvfb / vnc instances on :99
        subprocess.run(["killall", "Xvfb", "x11vnc", "websockify"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        # 1. Start Xvfb
        print(f"\n🖥️ Starting Xvfb virtual display on {display} (1366x850)...")
        p_xvfb = subprocess.Popen(["Xvfb", display, "-screen", "0", "1366x850x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_xvfb)
        time.sleep(1)

        # 2. Start x11vnc
        print(f"📡 Starting x11vnc server on port {vnc_port}...")
        p_vnc = subprocess.Popen(["x11vnc", "-display", display, "-nopw", "-forever", "-shared", "-rfbport", str(vnc_port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_vnc)
        time.sleep(1)

        # 3. Start noVNC
        print(f"🌐 Starting web noVNC proxy on port {web_port}...")
        p_novnc = subprocess.Popen(["websockify", "--web", "/usr/share/novnc", str(web_port), f"localhost:{vnc_port}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(p_novnc)
        time.sleep(1)

        # Detect public IP
        public_ip = os.getenv("SERVER_PUBLIC_IP", "")
        if not public_ip:
            try:
                public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode('utf-8').strip()
            except Exception:
                public_ip = "YOUR_EC2_PUBLIC_IP"

        vnc_web_url = f"http://{public_ip}:{web_port}/vnc.html?autoconnect=true"

        print("\n" + "=" * 65)
        print("🎉 LIVE VNC DESKTOP IS READY!")
        print(f"👉 OPEN THIS IN YOUR BROWSER: {vnc_web_url}")
        print("=" * 65 + "\n")
        print("⏳ Starting terabox_crawler.js on the virtual desktop in 5 seconds...")
        print("   (Open the VNC link above right now to watch the browser live!)")
        time.sleep(5)

        # 4. Run terabox_crawler.js on DISPLAY=:99
        env = os.environ.copy()
        env["DISPLAY"] = display
        
        output_dir = str(BASE_DIR / "downloads")
        crawler_script = str(BASE_DIR / "terabox_crawler.js")
        
        print(f"\n🚀 Running: node terabox_crawler.js on {display}...")
        p_crawler = subprocess.Popen(["node", crawler_script, target_url, output_dir, "vnc_test"], env=env)
        processes.append(p_crawler)

        # Wait for crawler to finish or user to inspect
        p_crawler.wait()

        print("\n✅ Crawler process exited. Keeping VNC open for 60 seconds so you can inspect...")
        print("Press [Ctrl+C] to close VNC and exit.")
        time.sleep(60)

    except KeyboardInterrupt:
        print("\n🛑 Stopping VNC and cleaning up processes...")
    finally:
        for p in processes:
            try:
                p.terminate()
                p.kill()
            except Exception:
                pass
        subprocess.run(["killall", "Xvfb", "x11vnc", "websockify"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✨ Cleanup complete.")

if __name__ == "__main__":
    main()
