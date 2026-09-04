"""
Telethon MTProto Extraction for TeraBox MiniApp Signed URL & Bearer Token.

Directly requests the cryptographic signed WebApp URL from Telegram MTProto servers,
bypassing all browser/VNC/coordinate automation.

Usage:
    python test_telethon_miniapp.py
    python test_telethon_miniapp.py --api-id 123456 --api-hash abcdef...
"""

import os
import sys
import re
import json
import asyncio
import argparse
import urllib.parse
import urllib.request
import ssl
from pathlib import Path
from dotenv import load_dotenv

# Base Directory & Env
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

try:
    from telethon import TelegramClient, functions, types
    from telethon.errors import (
        SessionPasswordNeededError,
        BotMethodInvalidError,
        BotResponseTimeoutError,
    )
except ImportError:
    print("[ERROR] Telethon is not installed. Please run: pip install telethon")
    sys.exit(1)


def get_env_val(key: str) -> str:
    """Reads a variable from .env file or os.environ."""
    val = os.environ.get(key, "").strip()
    if not val and ENV_FILE.exists():
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(f"{key}="):
                    val = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return val


def save_to_env(key: str, value: str) -> bool:
    """Updates or adds a key-value pair in .env."""
    try:
        lines = []
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines.append("\n")
            new_lines.append(f"{key}={value}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        os.environ[key] = value
        return True
    except Exception as e:
        print(f"[ERROR] Failed to save {key} to .env: {e}")
        return False


def extract_init_data(signed_url: str) -> str:
    """
    Extracts the exact tgWebAppData string from the signed URL fragment.
    Example URL:
    https://twa.teradownloader.pro/#tgWebAppData=user%3D%7B...%7D&chat_instance=...&auth_date=...&hash=...&tgWebAppVersion=8.0
    """
    if "#" not in signed_url:
        return ""

    fragment = signed_url.split("#", 1)[1]

    # Check for tgWebAppData= in fragment
    if "tgWebAppData=" in fragment:
        raw_part = fragment.split("tgWebAppData=", 1)[1]
        # Keep everything until the next &tgWebApp parameter
        init_chunks = []
        for chunk in raw_part.split("&"):
            if chunk.startswith("tgWebApp"):
                break
            init_chunks.append(chunk)
        init_data = "&".join(init_chunks)
        # If it was double URL encoded, decode once
        if init_data.startswith("user%3D") or init_data.startswith("query_id%3D"):
            # Unquote once so user={"id"...} or query_id=... is formatted as query params
            init_data = urllib.parse.unquote(init_data)
        return init_data

    # Direct query string in fragment
    if "user=" in fragment or "query_id=" in fragment:
        parts = []
        for chunk in fragment.split("&"):
            if chunk.startswith("tgWebApp"):
                break
            parts.append(chunk)
        return "&".join(parts)

    return ""


def test_terabox_api(bearer_token: str, test_link: str = "https://1024terabox.com/s/1FfPx1371J2yv6B3hyqI5PQ") -> dict:
    """
    Validates the bearer token against the live apiwala.teradownloader.pro backend.
    """
    url = "https://apiwala.teradownloader.pro/api/terabox/new"
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.6",
        "authorization": bearer_token if bearer_token.startswith("Bearer ") else f"Bearer {bearer_token}",
        "content-type": "application/json",
        "origin": "https://twa.teradownloader.pro",
        "referer": "https://twa.teradownloader.pro/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "x-bot-id": "terabox_player",
    }
    payload = json.dumps({
        "link": test_link,
        "dir_path": "",
        "page": 1,
    }).encode("utf-8")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as res:
            res_body = res.read().decode("utf-8")
            return json.loads(res_body)
    except urllib.error.HTTPError as he:
        err_msg = he.read().decode("utf-8", errors="ignore")
        return {"success": False, "http_status": he.code, "error": err_msg}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def extract_miniapp_signed_url(client: TelegramClient, bot_username: str) -> str:
    """
    Attempts all MTProto WebApp resolution strategies in sequence against the bot:
    1. RequestMainWebView (Main Web App)
    2. RequestWebView with from_bot_menu=True (Menu Button)
    3. Inline buttons in bot chat (/start)
    4. RequestAppWebView (App ShortName)
    """
    print(f"[*] Resolving bot entity: {bot_username}...")
    try:
        bot_entity = await client.get_input_entity(bot_username)
    except Exception as e:
        print(f"[!] Could not resolve bot username '{bot_username}': {e}")
        # Try numeric peer ID fallback if user provided
        if bot_username.lstrip("@").isdigit():
            bot_entity = await client.get_input_entity(int(bot_username.lstrip("@")))
        else:
            raise

    # ── Strategy 1: Main Web View (Telegram Bot API 7.0+ Main Web App) ──
    print("\n--- [Strategy 1] Testing RequestMainWebViewRequest (Main Web App) ---")
    for platform in ["android", "weba", "ios"]:
        try:
            print(f"[*] Trying RequestMainWebViewRequest (platform='{platform}')...")
            result = await client(functions.messages.RequestMainWebViewRequest(
                peer=bot_entity,
                bot=bot_entity,
                platform=platform,
            ))
            if hasattr(result, "url") and result.url:
                print(f"[+] SUCCESS via RequestMainWebViewRequest (platform={platform})!")
                return result.url
        except Exception as e:
            print(f"[-] RequestMainWebViewRequest ({platform}) notice: {e}")

    # ── Strategy 2: Bot Menu Button ──
    print("\n--- [Strategy 2] Testing RequestWebViewRequest (from_bot_menu=True) ---")
    candidate_urls = [
        "https://twa.teradownloader.pro/",
        "https://twa.teradownloader.pro",
        "https://miniapp.diskwala.net/",
    ]
    for url in candidate_urls:
        for platform in ["weba", "android"]:
            try:
                print(f"[*] Trying RequestWebViewRequest (from_bot_menu=True, url='{url}', platform='{platform}')...")
                result = await client(functions.messages.RequestWebViewRequest(
                    peer=bot_entity,
                    bot=bot_entity,
                    platform=platform,
                    url=url,
                    from_bot_menu=True,
                ))
                if hasattr(result, "url") and result.url:
                    print(f"[+] SUCCESS via RequestWebViewRequest (from_bot_menu=True)!")
                    return result.url
            except Exception as e:
                print(f"[-] RequestWebViewRequest (url={url}, {platform}) notice: {e}")

    # ── Strategy 3: Chat Interaction & Inline Buttons ──
    print("\n--- [Strategy 3] Inspecting Bot Chat Messages & Inline Buttons ---")
    try:
        print("[*] Sending /start to bot...")
        await client.send_message(bot_entity, "/start")
        await asyncio.sleep(2.5)

        messages = await client.get_messages(bot_entity, limit=5)
        for msg in messages:
            if not msg.reply_markup:
                continue

            # Check rows in reply_markup
            rows = getattr(msg.reply_markup, "rows", [])
            for row in rows:
                for btn in getattr(row, "buttons", []):
                    btn_text = getattr(btn, "text", "")
                    print(f"[*] Inspecting button: '{btn_text}' (Type: {type(btn).__name__})")

                    # If button has web_app attribute
                    if hasattr(btn, "url") and ("twa." in btn.url or "miniapp." in btn.url or "terabox" in btn.url):
                        print(f"[*] Found direct WebApp URL in button: {btn.url}")
                        try:
                            res = await client(functions.messages.RequestWebViewRequest(
                                peer=bot_entity,
                                bot=bot_entity,
                                platform="weba",
                                url=btn.url,
                                reply_to=types.InputReplyToMessage(reply_to_msg_id=msg.id),
                            ))
                            if hasattr(res, "url") and res.url:
                                print(f"[+] SUCCESS via Inline Button RequestWebViewRequest!")
                                return res.url
                        except Exception as e:
                            print(f"[-] Button RequestWebViewRequest notice: {e}")

                    # If KeyboardButtonWebView or KeyboardButtonSimpleWebView
                    if isinstance(btn, (types.KeyboardButtonWebView, types.KeyboardButtonSimpleWebView)):
                        target_url = getattr(btn, "url", "https://twa.teradownloader.pro/")
                        try:
                            print(f"[*] Requesting web view for KeyboardButton: {target_url}...")
                            res = await client(functions.messages.RequestWebViewRequest(
                                peer=bot_entity,
                                bot=bot_entity,
                                platform="weba",
                                url=target_url,
                                reply_to=types.InputReplyToMessage(reply_to_msg_id=msg.id),
                            ))
                            if hasattr(res, "url") and res.url:
                                print(f"[+] SUCCESS via KeyboardButtonWebView!")
                                return res.url
                        except Exception as e:
                            print(f"[-] KeyboardButton RequestWebView notice: {e}")
    except Exception as e:
        print(f"[-] Strategy 3 notice: {e}")

    # ── Strategy 4: RequestAppWebView with Bot App ShortName ──
    print("\n--- [Strategy 4] Testing RequestAppWebViewRequest (ShortNames) ---")
    candidate_short_names = ["app", "start", "terabox", "downloader", "bot", "main"]
    for short_name in candidate_short_names:
        for platform in ["android", "weba"]:
            try:
                print(f"[*] Trying RequestAppWebViewRequest (short_name='{short_name}', platform='{platform}')...")
                res = await client(functions.messages.RequestAppWebViewRequest(
                    peer=bot_entity,
                    app=types.InputBotAppShortName(bot_id=bot_entity, short_name=short_name),
                    platform=platform,
                    write_allowed=True,
                ))
                if hasattr(res, "url") and res.url:
                    print(f"[+] SUCCESS via RequestAppWebViewRequest (short_name={short_name})!")
                    return res.url
            except Exception as e:
                print(f"[-] RequestAppWebViewRequest ({short_name}, {platform}) notice: {e}")

    raise RuntimeError("All MTProto MiniApp extraction strategies exhausted without returning a signed URL.")


async def main():
    parser = argparse.ArgumentParser(description="Telethon MTProto Signed MiniApp URL & Bearer Extractor")
    parser.add_argument("--api-id", default="", help="Telegram API ID (integer)")
    parser.add_argument("--api-hash", default="", help="Telegram API Hash (string)")
    parser.add_argument("--phone", default="", help="Telegram Phone Number (e.g. +1234567890)")
    parser.add_argument("--bot", default="@terabox_downloader_new_bot", help="Target Telegram bot username")
    parser.add_argument("--session", default="terabox_user_session", help="Session file name")
    parser.add_argument("--test-link", default="https://1024terabox.com/s/1FfPx1371J2yv6B3hyqI5PQ", help="TeraBox link to test API")
    parser.add_argument("--no-save", action="store_true", help="Do not save the token to .env")
    args = parser.parse_args()

    print("==================================================================")
    print("      Telethon MTProto MiniApp Bearer Token Extractor             ")
    print("==================================================================")

    # 1. Resolve API ID & API Hash
    api_id_val = args.api_id.strip() or get_env_val("TELEGRAM_API_ID")
    api_hash_val = args.api_hash.strip() or get_env_val("TELEGRAM_API_HASH")

    if not api_id_val or not api_hash_val:
        print("\n[!] Telegram API ID or API Hash is not set in .env.")
        print("    You can get credentials from: https://my.telegram.org -> 'API development tools'.\n")
        if not api_id_val:
            api_id_val = input("Enter your TELEGRAM_API_ID: ").strip()
        if not api_hash_val:
            api_hash_val = input("Enter your TELEGRAM_API_HASH: ").strip()

        if api_id_val and api_hash_val:
            save_to_env("TELEGRAM_API_ID", api_id_val)
            save_to_env("TELEGRAM_API_HASH", api_hash_val)
            print("[+] Saved TELEGRAM_API_ID and TELEGRAM_API_HASH to .env.")

    if not api_id_val or not api_hash_val:
        print("[ERROR] TELEGRAM_API_ID and TELEGRAM_API_HASH are strictly required.")
        sys.exit(1)

    try:
        api_id_int = int(api_id_val)
    except ValueError:
        print(f"[ERROR] TELEGRAM_API_ID must be a numeric integer. Got: '{api_id_val}'")
        sys.exit(1)

    session_path = str(BASE_DIR / args.session)
    phone_val = args.phone.strip() or get_env_val("TELEGRAM_PHONE")

    print(f"[*] Initializing Telethon client (session: {args.session}.session)...")
    client = TelegramClient(session_path, api_id_int, api_hash_val)

    await client.start(phone=phone_val if phone_val else None)

    me = await client.get_me()
    print(f"[+] Authenticated successfully as: {me.first_name} {me.last_name or ''} (@{me.username or 'no_username'}) [ID: {me.id}]")

    # 2. Extract Signed MiniApp URL
    try:
        signed_url = await extract_miniapp_signed_url(client, args.bot)
    except Exception as e:
        print(f"\n[ERROR] Failed to extract signed MiniApp URL: {e}")
        await client.disconnect()
        sys.exit(1)

    print("\n==================================================================")
    print("🎯 SIGNED MINIAPP URL EXTRACTED:")
    print(f"{signed_url}")
    print("==================================================================")

    # 3. Parse tgWebAppData / Bearer Token
    init_data = extract_init_data(signed_url)
    if not init_data:
        print("[ERROR] Could not isolate tgWebAppData from the signed URL.")
        await client.disconnect()
        sys.exit(1)

    bearer_token = f"Bearer {init_data}"
    print(f"\n🔑 EXTRACTED BEARER TOKEN (Length: {len(bearer_token)} chars):")
    print(f"{bearer_token[:120]}...{bearer_token[-40:]}")

    # 4. Validate Token with Live API Call
    print(f"\n[*] Testing token validity against apiwala.teradownloader.pro...")
    print(f"[*] Test URL: {args.test_link}")

    api_result = test_terabox_api(bearer_token, args.test_link)
    print("\n[+] API Response:")
    print(json.dumps(api_result, indent=2))

    success = api_result.get("success", False)
    items = api_result.get("data", [])

    if success or (isinstance(items, list) and len(items) > 0):
        print(f"\n🎉 VERIFICATION PASSED! API successfully resolved {len(items)} file(s).")
        for idx, it in enumerate(items, 1):
            print(f"    Item {idx}: {it.get('fileName')} ({it.get('fileSizeMB')} MB)")
            dl = it.get('downloadLink', '')
            if dl:
                print(f"            Download Link: {dl[:70]}...")

        # 5. Persist to .env
        if not args.no_save:
            save_to_env("TERABOX_BEARER_TOKEN", bearer_token)
            print(f"\n[+] Saved TERABOX_BEARER_TOKEN to .env successfully!")
    else:
        print(f"\n[!] Notice: API returned success={success}. Error: {api_result.get('error') or api_result.get('message')}")
        if not args.no_save:
            # Still save if user wants or for debugging
            save_to_env("TERABOX_BEARER_TOKEN", bearer_token)
            print(f"[+] Saved extracted token to .env for inspection.")

    await client.disconnect()
    print("\n[*] Telethon extraction complete. Session saved in workspace for future automated runs.")


if __name__ == "__main__":
    asyncio.run(main())
