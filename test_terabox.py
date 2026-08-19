import asyncio
import os
import sys
from pathlib import Path

# Force UTF-8 output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from terabox_downloader import (
    extract_surl,
    get_terabox_cookie,
    extract_terabox_info,
    is_terabox_url,
)

async def main():
    test_url = sys.argv[1] if len(sys.argv) > 1 else "https://1024terabox.com/s/1UxZYfUcXhzkXIrFyonRAkw"
    print("=" * 60)
    print("TERABOX RESOLVER & COOKIE VERIFICATION TEST")
    print("=" * 60)
    print(f"Target URL: {test_url}")
    print(f"Is TeraBox URL: {is_terabox_url(test_url)}")
    
    surl = extract_surl(test_url)
    print(f"Extracted SURL: {surl}")
    
    cookie = get_terabox_cookie()
    if cookie:
        cookie_preview = cookie[:40] + "..." if len(cookie) > 40 else cookie
        print(f"Active Cookie: [FOUND] ({cookie_preview})")
    else:
        print("Active Cookie: [NOT FOUND] (No cookie in cooky/terabox/cookies.txt)")
    
    print("-" * 60)
    print("Attempting to resolve video information...")
    success, info, err = await extract_terabox_info(test_url)
    
    if success and info:
        print("[SUCCESS] Video resolved successfully!")
        print(f"• Title:    {info.get('title')}")
        print(f"• Engine:   {info.get('engine', 'default')}")
        print(f"• Author:   {info.get('uploader')}")
        print(f"• Play URL: {info.get('play_url', '')[:80]}...")
    else:
        print("[FAILED] Resolution error:")
        print(f"• Error: {err}")
    
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
