"""
Google Photos OAuth 2.0 & Media Upload Manager
===============================================
Handles OAuth 2.0 authentication, automatic token refresh,
and uploading videos directly into Google Photos library.
"""

import os
import json
import time
import mimetypes
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable, List, Set
import urllib.parse
import aiohttp
import requests

from config import BASE_DIR

try:
    from config import (
        GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET,
        GOOGLE_REDIRECT_URI,
    )
except ImportError:
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8080/oauth2callback").strip()
from downloader import format_bytes

logger = logging.getLogger(__name__)

DATA_DIR = BASE_DIR / "data"
TOKEN_FILE = DATA_DIR / "google_photos_token.json"

OAUTH_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
PHOTOS_UPLOAD_ENDPOINT = "https://photoslibrary.googleapis.com/v1/uploads"
PHOTOS_BATCH_CREATE_ENDPOINT = "https://photoslibrary.googleapis.com/v1/mediaItems:batchCreate"

SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary",
]

TRANSFERRED_REGISTRY_FILE = DATA_DIR / "transferred_videos.json"


def load_transferred_registry() -> Dict[str, Any]:
    """Loads persistent record of videos transferred to Google Photos."""
    if not TRANSFERRED_REGISTRY_FILE.exists():
        return {"items": {}, "filenames": {}, "manual_marked": []}
    try:
        with open(TRANSFERRED_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"items": {}, "filenames": {}, "manual_marked": []}
            data.setdefault("items", {})
            data.setdefault("filenames", {})
            data.setdefault("manual_marked", [])
            return data
    except Exception as e:
        logger.error(f"[Google Photos] Error reading transferred registry: {e}")
        return {"items": {}, "filenames": {}, "manual_marked": []}


def save_transferred_registry(data: Dict[str, Any]) -> bool:
    """Saves persistent record of videos transferred to Google Photos."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(TRANSFERRED_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"[Google Photos] Error writing transferred registry: {e}")
        return False


def record_transferred_video(
    fs_id: Any,
    filename: str,
    size: int = 0,
    google_id: Optional[str] = None,
    product_url: Optional[str] = None,
) -> bool:
    """Records a successfully transferred video into the persistent registry."""
    data = load_transferred_registry()
    str_id = str(fs_id).strip() if fs_id is not None else ""
    clean_name = filename.strip().lower()

    record = {
        "fs_id": str_id,
        "filename": filename.strip(),
        "clean_name": clean_name,
        "size": size,
        "google_id": google_id or "",
        "product_url": product_url or "",
        "transferred_at": int(time.time()),
    }

    if str_id:
        data["items"][str_id] = record
    if clean_name:
        data["filenames"][clean_name] = str_id

    return save_transferred_registry(data)


def mark_as_transferred(identifiers: List[str]) -> int:
    """Manually marks a list of filenames or fs_ids as already present in Google Photos."""
    data = load_transferred_registry()
    count = 0
    for ident in identifiers:
        s = ident.strip()
        if not s:
            continue
        clean = s.lower()
        if clean not in data.get("manual_marked", []):
            data["manual_marked"].append(clean)
            data["filenames"][clean] = clean
            count += 1
    if count > 0:
        save_transferred_registry(data)
    return count


async def fetch_app_created_media_items(max_pages: int = 20) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    """
    Queries Google Photos Library API for media items created by this application.
    Returns: (success, media_items_list, error_msg)
    """
    ok, access_token, err = await get_valid_access_token()
    if not ok or not access_token:
        return False, [], err or "Not authenticated."

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    items_list = []
    page_token = None
    page = 1

    try:
        while page <= max_pages:
            url = f"https://photoslibrary.googleapis.com/v1/mediaItems?pageSize=100"
            if page_token:
                url += f"&pageToken={urllib.parse.quote(page_token)}"

            def _get():
                return requests.get(url, headers=headers, timeout=20)

            resp = await asyncio.to_thread(_get)
            if resp.status_code == 403:
                # Scope may lack readonly.appcreateddata on older tokens
                logger.warning(f"[Google Photos] mediaItems.list returned 403 (insufficient scopes): {resp.text[:120]}")
                return False, items_list, "Insufficient scopes for mediaItems.list (requires re-auth with appcreateddata)."

            if resp.status_code != 200:
                logger.warning(f"[Google Photos] mediaItems.list returned HTTP {resp.status_code}")
                return False, items_list, f"HTTP {resp.status_code}: {resp.text[:120]}"

            data = resp.json()
            batch = data.get("mediaItems", [])
            for it in batch:
                fname = it.get("filename", "")
                i_id = it.get("id", "")
                if fname:
                    items_list.append({
                        "id": i_id,
                        "filename": fname,
                        "product_url": it.get("productUrl", ""),
                        "mime_type": it.get("mimeType", ""),
                    })
                    # Sync into local catalog
                    record_transferred_video(fs_id=i_id, filename=fname, google_id=i_id, product_url=it.get("productUrl", ""))

            page_token = data.get("nextPageToken")
            if not page_token or len(batch) < 100:
                break
            page += 1

        logger.info(f"[Google Photos] Fetched {len(items_list)} app-created items from Google Photos API.")
        return True, items_list, None

    except Exception as e:
        logger.error(f"[Google Photos] Error fetching media items: {e}")
        return False, items_list, str(e)


async def get_known_google_photos_files() -> Tuple[Set[str], Set[str]]:
    """
    Returns two sets: (known_fs_ids, known_filenames) representing all videos
    already present in Google Photos (from persistent registry + API scan).
    All filenames are lowercased and stripped for fuzzy/exact matching.
    """
    reg = load_transferred_registry()
    known_fs_ids = set()
    known_filenames = set()

    for fs_id, item in reg.get("items", {}).items():
        if fs_id:
            known_fs_ids.add(str(fs_id).strip())
        fname = item.get("clean_name") or item.get("filename", "")
        if fname:
            known_filenames.add(fname.strip().lower())

    for fname, fs_id in reg.get("filenames", {}).items():
        if fname:
            known_filenames.add(fname.strip().lower())
        if fs_id:
            known_fs_ids.add(str(fs_id).strip())

    for m in reg.get("manual_marked", []):
        if m:
            known_filenames.add(m.strip().lower())

    # Opportunistically attempt API fetch for newly synced items if authenticated
    try:
        auth_ok, _ = is_photos_authenticated()
        if auth_ok:
            api_ok, api_items, _ = await fetch_app_created_media_items(max_pages=5)
            if api_ok and api_items:
                for it in api_items:
                    fn = (it.get("filename") or "").strip().lower()
                    if fn:
                        known_filenames.add(fn)
    except Exception as e:
        logger.debug(f"[Google Photos] Optional API fetch notice: {e}")

    return known_fs_ids, known_filenames


def load_token_data() -> Optional[Dict[str, Any]]:
    """Loads saved Google Photos OAuth token from disk."""
    if not TOKEN_FILE.exists():
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[Google Photos] Failed to read token file: {e}")
        return None


def save_token_data(data: Dict[str, Any]) -> bool:
    """Safely saves Google Photos OAuth token to disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"[Google Photos] Failed to write token file: {e}")
        return False


ENV_FILE = BASE_DIR / ".env"


def save_google_credentials_to_env(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> bool:
    """
    Saves or updates Google OAuth credentials in the project's .env file
    and keeps os.environ and config module in sync immediately.
    """
    updates = {}
    if client_id is not None:
        cid = client_id.strip()
        updates["GOOGLE_CLIENT_ID"] = cid
        os.environ["GOOGLE_CLIENT_ID"] = cid
    if client_secret is not None:
        csec = client_secret.strip()
        updates["GOOGLE_CLIENT_SECRET"] = csec
        os.environ["GOOGLE_CLIENT_SECRET"] = csec
    if redirect_uri is not None:
        ruri = redirect_uri.strip()
        updates["GOOGLE_REDIRECT_URI"] = ruri
        os.environ["GOOGLE_REDIRECT_URI"] = ruri

    if not updates:
        return True

    try:
        import config
        for k, v in updates.items():
            setattr(config, k, v)
    except Exception:
        pass

    try:
        lines = []
        if ENV_FILE.exists():
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

        new_lines = []
        handled_keys = set()

        for line in lines:
            line_clean = line.strip()
            matched = False
            for k, v in updates.items():
                if line_clean.startswith(f"{k}="):
                    new_lines.append(f"{k}={v}\n")
                    handled_keys.add(k)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)

        for k, v in updates.items():
            if k not in handled_keys:
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines.append("\n")
                new_lines.append(f"{k}={v}\n")

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.writelines(new_lines)

        return True
    except Exception as e:
        logger.error(f"[Google Photos] Error saving credentials to .env: {e}")
        return False


def get_authorization_url(redirect_uri: Optional[str] = None, state: Optional[str] = None) -> Tuple[bool, str]:
    """
    Generates the Google OAuth 2.0 consent URL for Google Photos.
    Allows passing a custom redirect_uri if specified.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip() or GOOGLE_CLIENT_ID
    if not client_id:
        return False, "GOOGLE_CLIENT_ID is not configured in .env."

    chosen_redirect_uri = (
        redirect_uri
        or os.getenv("GOOGLE_REDIRECT_URI", "").strip()
        or GOOGLE_REDIRECT_URI
        or "http://localhost:8080/oauth2callback"
    ).strip()

    params = {
        "client_id": client_id,
        "redirect_uri": chosen_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state

    query_str = urllib.parse.urlencode(params)
    url = f"{OAUTH_AUTH_ENDPOINT}?{query_str}"
    return True, url


async def exchange_code_for_tokens(code: str, redirect_uri: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """
    Exchanges authorization code for access_token and refresh_token.
    Requires redirect_uri to match the one sent during the initial auth URL generation.
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip() or GOOGLE_CLIENT_ID
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip() or GOOGLE_CLIENT_SECRET
    if not client_id or not client_secret:
        return False, "Google OAuth Client ID or Client Secret is missing in .env."

    # Clean code in case user pasted full URL
    clean_code = code.strip()
    if "code=" in clean_code:
        try:
            parsed = urllib.parse.urlparse(clean_code)
            qs = urllib.parse.parse_qs(parsed.query)
            clean_code = qs.get("code", [clean_code])[0]
        except Exception:
            pass

    chosen_redirect_uri = (
        redirect_uri
        or os.getenv("GOOGLE_REDIRECT_URI", "").strip()
        or GOOGLE_REDIRECT_URI
        or "http://localhost:8080/oauth2callback"
    ).strip()

    payload = {
        "code": clean_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": chosen_redirect_uri,
        "grant_type": "authorization_code",
    }

    def _request():
        return requests.post(OAUTH_TOKEN_ENDPOINT, data=payload, timeout=20)

    try:
        resp = await asyncio.to_thread(_request)
        if resp.status_code != 200:
            err = resp.text
            try:
                err_json = resp.json()
                err = err_json.get("error_description") or err_json.get("error") or err
            except Exception:
                pass
            logger.error(f"[Google Photos] Token exchange error: {err}")
            return False, f"Google token exchange failed: {err}"

        tokens = resp.json()
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        expires_in = tokens.get("expires_in", 3600)
        expires_at = time.time() + float(expires_in)

        # Merge with existing token if refresh_token wasn't returned on re-auth
        existing = load_token_data() or {}
        final_data = {
            "access_token": access_token,
            "refresh_token": refresh_token or existing.get("refresh_token"),
            "token_type": tokens.get("token_type", "Bearer"),
            "scope": tokens.get("scope", " ".join(SCOPES)),
            "expires_at": expires_at,
            "updated_at": time.time(),
        }

        save_token_data(final_data)
        logger.info("[Google Photos] OAuth tokens successfully acquired and saved.")
        return True, None

    except Exception as e:
        logger.error(f"[Google Photos] Exception during token exchange: {e}")
        return False, str(e)


async def get_valid_access_token() -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Returns a valid access token. Automatically refreshes using refresh_token if expired.
    Returns: (success, access_token, error_msg)
    """
    tokens = load_token_data()
    if not tokens or not tokens.get("access_token"):
        return False, None, "Google Photos is not authenticated. Run `/gphotos_auth` to log in."

    access_token = tokens.get("access_token")
    expires_at = float(tokens.get("expires_at", 0))
    refresh_token = tokens.get("refresh_token")

    # If token has more than 60 seconds of validity remaining, return it
    if time.time() < (expires_at - 60):
        return True, access_token, None

    if not refresh_token:
        return False, None, "Access token expired and no refresh_token found. Please run `/gphotos_auth` again."

    logger.info("[Google Photos] Access token expired. Refreshing token with Google OAuth...")
    cid = os.getenv("GOOGLE_CLIENT_ID", "").strip() or GOOGLE_CLIENT_ID
    csec = os.getenv("GOOGLE_CLIENT_SECRET", "").strip() or GOOGLE_CLIENT_SECRET
    payload = {
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    def _refresh():
        return requests.post(OAUTH_TOKEN_ENDPOINT, data=payload, timeout=20)

    try:
        resp = await asyncio.to_thread(_refresh)
        if resp.status_code != 200:
            err = resp.text
            try:
                err = resp.json().get("error_description") or err
            except Exception:
                pass
            logger.error(f"[Google Photos] Token refresh failed: {err}")
            return False, None, f"Token refresh failed: {err}"

        new_data = resp.json()
        new_access = new_data.get("access_token")
        new_expires_in = new_data.get("expires_in", 3600)

        tokens["access_token"] = new_access
        tokens["expires_at"] = time.time() + float(new_expires_in)
        tokens["updated_at"] = time.time()
        save_token_data(tokens)

        logger.info("[Google Photos] Token refreshed successfully.")
        return True, new_access, None

    except Exception as e:
        logger.error(f"[Google Photos] Refresh request exception: {e}")
        return False, None, str(e)


def is_photos_authenticated() -> Tuple[bool, Optional[str]]:
    """Checks if Google Photos OAuth token is configured and available."""
    tokens = load_token_data()
    if not tokens or not tokens.get("access_token"):
        return False, "Not authenticated."
    if tokens.get("refresh_token"):
        return True, "Authenticated (Refresh Token active)"
    if time.time() < float(tokens.get("expires_at", 0)):
        return True, "Authenticated (Active Access Token)"
    return False, "Token expired (No refresh token)."


async def upload_video_to_google_photos(
    file_path: str,
    custom_filename: Optional[str] = None,
    progress_updater: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    Uploads a local video file to Google Photos using the Google Photos Library API.
    Flow:
      1. POST raw bytes to /v1/uploads -> returns upload_token
      2. POST /v1/mediaItems:batchCreate -> adds to library
    Returns: (success, result_info, error_msg)
    """
    path_obj = Path(file_path)
    if not path_obj.exists() or not path_obj.is_file():
        return False, None, f"Local file not found: {file_path}"

    filesize = path_obj.stat().st_size
    filename = custom_filename or path_obj.name

    # Guess MIME type
    mime_type, _ = mimetypes.guess_type(filename)
    if not mime_type or not mime_type.startswith("video"):
        mime_type = "video/mp4"

    ok, access_token, err = await get_valid_access_token()
    if not ok or not access_token:
        return False, None, err or "Authentication required."

    logger.info(f"[Google Photos] Starting upload for {filename} ({format_bytes(filesize)})...")

    # Step 1: Upload Raw Video Stream
    upload_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/octet-stream",
        "X-Goog-Upload-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "raw",
    }

    loop = asyncio.get_running_loop()

    def _do_upload_stream():
        session = requests.Session()
        last_update = 0
        uploaded_bytes = 0

        class ProgressReader:
            def __init__(self, fp):
                self._fp = fp

            def read(self, size=-1):
                nonlocal uploaded_bytes, last_update
                chunk = self._fp.read(size)
                if chunk:
                    uploaded_bytes += len(chunk)
                    now = time.time()
                    if progress_updater and (now - last_update > 2.0):
                        last_update = now
                        pct = int((uploaded_bytes / filesize) * 100) if filesize > 0 else 0
                        msg = f"☁️ **Uploading to Google Photos: {pct}%** ({format_bytes(uploaded_bytes)} / {format_bytes(filesize)})"
                        try:
                            asyncio.run_coroutine_threadsafe(progress_updater(msg), loop)
                        except Exception:
                            pass
                return chunk

        with open(file_path, "rb") as f:
            reader = ProgressReader(f)
            resp = session.post(
                PHOTOS_UPLOAD_ENDPOINT,
                data=reader,
                headers=upload_headers,
                timeout=300,
            )
        return resp

    try:
        upload_resp = await asyncio.to_thread(_do_upload_stream)
        if upload_resp.status_code != 200:
            logger.error(f"[Google Photos] Upload step 1 failed: HTTP {upload_resp.status_code} - {upload_resp.text}")
            return False, None, f"Google Photos upload endpoint returned HTTP {upload_resp.status_code}: {upload_resp.text[:200]}"

        upload_token = upload_resp.text.strip()
        if not upload_token:
            return False, None, "Google Photos upload did not return an upload token."

        logger.info(f"[Google Photos] Video uploaded successfully. Creating media item for {filename}...")

        # Step 2: Batch Create Media Item in Google Photos Library
        batch_payload = {
            "newMediaItems": [
                {
                    "description": f"Transferred from TeraBox: {filename}",
                    "simpleMediaItem": {
                        "fileName": filename,
                        "uploadToken": upload_token,
                    },
                }
            ]
        }

        batch_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        def _do_batch_create():
            return requests.post(
                PHOTOS_BATCH_CREATE_ENDPOINT,
                json=batch_payload,
                headers=batch_headers,
                timeout=30,
            )

        batch_resp = await asyncio.to_thread(_do_batch_create)
        if batch_resp.status_code != 200:
            logger.error(f"[Google Photos] Media item creation failed: HTTP {batch_resp.status_code} - {batch_resp.text}")
            return False, None, f"Google Photos batchCreate returned HTTP {batch_resp.status_code}: {batch_resp.text[:200]}"

        batch_data = batch_resp.json()
        results = batch_data.get("newMediaItemResults", [])
        if not results:
            return False, None, "No creation result returned by Google Photos."

        item_result = results[0]
        status = item_result.get("status", {})
        status_msg = status.get("message", "")

        if status_msg.lower() not in ("success", "ok", ""):
            return False, None, f"Google Photos item creation error: {status_msg}"

        media_item = item_result.get("mediaItem", {})
        product_url = media_item.get("productUrl")
        item_id = media_item.get("id")

        logger.info(f"[Google Photos] Successfully added '{filename}' to Google Photos! ID: {item_id}")

        return True, {
            "filename": filename,
            "id": item_id,
            "product_url": product_url,
            "filesize": filesize,
        }, None

    except Exception as e:
        logger.error(f"[Google Photos] Upload exception for {filename}: {e}", exc_info=True)
        return False, None, str(e)
