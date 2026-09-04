"""
Link Protection & Anti-Bot Detection Module
===========================================
Protects scraper accounts, cookies, and server IP from upstream bot detection and rate limits
by enforcing a 5-minute cooldown on duplicate link downloads across all platforms.
"""

import time
import re
import urllib.parse
from typing import Tuple, Dict, Set

# 5 minutes in seconds
LINK_COOLDOWN_SECONDS = 300

# Standardized error message required by bot specification
COOLDOWN_ERROR_MESSAGE = "This file has been downloaded once , please wait 5mins to download again or try another link"

# In-memory storage for recently completed downloads: canonical_key -> timestamp
_DOWNLOADED_LINKS: Dict[str, float] = {}

# In-memory storage for downloads currently in-flight: set of canonical_keys
_IN_PROGRESS_LINKS: Set[str] = set()


def canonicalize_url(raw_url: str) -> str:
    """
    Returns a canonical identifier for a media URL across all supported platforms:
    - YouTube (regular, short, embed, mobile)
    - Instagram (p, reel, reels, share)
    - TeraBox (surl param, short code /s/1..., /s/...)
    - Diskwala (/app/<id>)
    - Facebook (/reel/, /videos/, watch?v=)
    - Generic URLs (cleaned scheme + host + path, tracking params stripped)
    """
    if not raw_url:
        return ""
    url = raw_url.strip()

    # 1. YouTube: watch?v=, shorts/, youtu.be/
    yt_m = re.search(r'(?:youtube\.com\/(?:watch\?.*v=|shorts\/|embed\/)|youtu\.be\/)([a-zA-Z0-9_-]{11})', url)
    if yt_m:
        return f"yt:{yt_m.group(1)}"

    # 2. Instagram: /reel/, /reels/, /p/, /share/reel/
    ig_m = re.search(r'instagram\.com\/(?:p|reel|reels|share\/reel)\/([a-zA-Z0-9_-]+)', url)
    if ig_m:
        return f"ig:{ig_m.group(1)}"

    # 3. TeraBox: ?surl=, /s/1<id>, /s/<id>
    surl_m = re.search(r'[?&]surl=([a-zA-Z0-9_-]+)', url)
    if surl_m:
        return f"tb:{surl_m.group(1)}"
    s_m = re.search(r'\/s\/(?:1)?([a-zA-Z0-9_-]+)', url)
    if s_m:
        return f"tb:{s_m.group(1)}"

    # 4. Diskwala: /app/<id>
    dw_m = re.search(r'diskwala\.com\/app\/([a-zA-Z0-9]+)', url)
    if dw_m:
        return f"dw:{dw_m.group(1)}"

    # 5. Facebook: /reel/<id>, /videos/<id>, /watch/?v=<id>
    fb_reel_m = re.search(r'facebook\.com\/(?:reel|videos)\/([0-9]+)', url)
    if fb_reel_m:
        return f"fb:{fb_reel_m.group(1)}"
    fb_watch_m = re.search(r'facebook\.com\/watch\/?\?.*v=([0-9]+)', url)
    if fb_watch_m:
        return f"fb:{fb_watch_m.group(1)}"

    # 6. Generic normalizer: lowercase scheme + netloc, drop tracking query parameters
    try:
        p = urllib.parse.urlparse(url)
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = p.path.rstrip('/')
        drop_params = {
            'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
            'igsh', 'fbclid', 'si', 'feature', 'ref', 'share_id', '_ga'
        }
        qs = urllib.parse.parse_qs(p.query)
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in drop_params}
        sorted_q = urllib.parse.urlencode(clean_qs, doseq=True)
        return f"{scheme}://{netloc}{path}" + (f"?{sorted_q}" if sorted_q else "")
    except Exception:
        return url.strip().lower()


def is_link_on_cooldown(raw_url: str) -> Tuple[bool, float]:
    """
    Checks if a URL has been downloaded within the last 5 minutes or is currently in-progress.
    Returns (True, remaining_seconds) if blocked, or (False, 0.0) if allowed.
    """
    key = canonicalize_url(raw_url)
    if not key:
        return False, 0.0

    now = time.time()

    # Check if currently being processed/downloaded right now
    if key in _IN_PROGRESS_LINKS:
        return True, float(LINK_COOLDOWN_SECONDS)

    # Check if completed within LINK_COOLDOWN_SECONDS (5 minutes)
    timestamp = _DOWNLOADED_LINKS.get(key)
    if timestamp:
        elapsed = now - timestamp
        if elapsed < LINK_COOLDOWN_SECONDS:
            remaining = LINK_COOLDOWN_SECONDS - elapsed
            return True, remaining
        else:
            # Expired: prune it
            _DOWNLOADED_LINKS.pop(key, None)

    return False, 0.0


def mark_link_in_progress(raw_url: str) -> None:
    """Marks a URL as actively downloading to prevent duplicate concurrent runs."""
    key = canonicalize_url(raw_url)
    if key:
        _IN_PROGRESS_LINKS.add(key)


def mark_link_completed(raw_url: str) -> None:
    """
    Marks a URL download as successfully completed and starts the 5-minute cooldown timer.
    """
    key = canonicalize_url(raw_url)
    if not key:
        return

    _IN_PROGRESS_LINKS.discard(key)
    now = time.time()
    _DOWNLOADED_LINKS[key] = now

    # Housekeeping: prune entries older than 2x cooldown window (10 minutes)
    if len(_DOWNLOADED_LINKS) > 100:
        cutoff = now - (LINK_COOLDOWN_SECONDS * 2)
        expired = [k for k, t in _DOWNLOADED_LINKS.items() if t < cutoff]
        for k in expired:
            _DOWNLOADED_LINKS.pop(k, None)


def mark_link_failed(raw_url: str) -> None:
    """Removes a URL from in-progress if the download failed before completing."""
    key = canonicalize_url(raw_url)
    if key:
        _IN_PROGRESS_LINKS.discard(key)


def clear_link_cooldowns() -> None:
    """Clears all in-progress and completed link cooldown caches (e.g. on /refresh)."""
    _IN_PROGRESS_LINKS.clear()
    _DOWNLOADED_LINKS.clear()
