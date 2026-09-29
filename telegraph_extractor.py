"""
Telegraph & Multi-Link Extractor Module
=======================================
Handles detecting and extracting media/download links from:
  1. Telegram's Telegraph (telegra.ph & graph.org mirror)
  2. Paste services (pastebin.com, rentry.co, justpaste.it, controlc.com)
  3. Plain-text messages containing multiple URLs
"""

import re
import logging
import urllib.parse
import asyncio
from pathlib import Path
from typing import List, Set, Optional

import requests

logger = logging.getLogger(__name__)

# Primary regex to match web URLs
URL_REGEX = re.compile(
    r'(https?://(?:www\.|(?!www))[a-zA-Z0-9][a-zA-Z0-9-]+[a-zA-Z0-9]\.[^\s]{2,}|'
    r'https?://[a-zA-Z0-9]+\.[^\s]{2,})',
    re.IGNORECASE
)

# Supported article/paste domains that host lists of download links
TELEGRAPH_DOMAINS = {"telegra.ph", "graph.org"}
PASTE_DOMAINS = {
    "pastebin.com",
    "rentry.co",
    "justpaste.it",
    "controlc.com",
    "ghostbin.com",
    "dpaste.org",
    "hastebin.com",
}

# Domains to ignore during extraction (self-referencing or internal assets)
IGNORED_DOMAINS = {
    "telegra.ph",
    "graph.org",
    "telegram.org",
    "t.me",
    "telegram.me",
    "pastebin.com",
    "rentry.co",
    "justpaste.it",
    "controlc.com",
}

# Static file extensions to skip during extraction
IGNORED_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".ico",
    ".svg", ".webp", ".woff", ".woff2", ".ttf", ".eot"
}


def is_telegraph_or_paste_url(url: str) -> bool:
    """Checks if a URL points to a Telegraph article or recognized paste site."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc in TELEGRAPH_DOMAINS or netloc in PASTE_DOMAINS
    except Exception:
        return False


def extract_links_from_html(html: str) -> List[str]:
    """
    Extracts all candidate download URLs from an HTML document or raw text.
    Extracts from both <a href="..."> tags and raw URL strings in body text.
    Filters out internal links and non-media static assets.
    Preserves original order and deduplicates.
    """
    if not html:
        return []

    raw_candidates = []

    # 1. Extract href values from <a> tags
    hrefs = re.findall(r'<a\s+(?:[^>]*?\s+)?href=[\'"](https?://[^\'"]+)[\'"]', html, re.IGNORECASE)
    raw_candidates.extend(hrefs)

    # 2. Extract plain text URLs (strip HTML tags first to avoid matching inside attributes)
    clean_text = re.sub(r'<[^>]+>', ' ', html)
    text_urls = [m.group(0).rstrip('.,;!?:)\'"[]{}<>') for m in URL_REGEX.finditer(clean_text)]
    raw_candidates.extend(text_urls)

    seen: Set[str] = set()
    filtered: List[str] = []

    for u in raw_candidates:
        u = u.strip().rstrip('.,;!?:)\'"[]{}<>')
        if not u or u in seen:
            continue

        try:
            p = urllib.parse.urlparse(u)
            dom = p.netloc.lower()
            if dom.startswith("www."):
                dom = dom[4:]

            # Skip self-referencing article links
            if dom in IGNORED_DOMAINS:
                continue

            # Skip common non-media static asset extensions
            path_lower = p.path.lower()
            if any(path_lower.endswith(ext) for ext in IGNORED_EXTENSIONS):
                continue

            seen.add(u)
            filtered.append(u)
        except Exception:
            pass

    return filtered


def _fetch_page_content_sync(url: str, timeout: int = 15) -> Optional[str]:
    """
    Synchronously fetches page content with browser headers and fallback mirrors.
    If telegra.ph is blocked (common in some regions), falls back to graph.org.
    """
    try:
        p = urllib.parse.urlparse(url.strip())
        dom = p.netloc.lower()
        if dom.startswith("www."):
            dom = dom[4:]
    except Exception:
        return None

    urls_to_try = [url]

    # For Telegraph: graph.org is the official Telegram mirror
    if dom == "telegra.ph":
        urls_to_try.append(urllib.parse.urlunparse(p._replace(netloc="graph.org")))
    elif dom == "graph.org":
        urls_to_try.append(urllib.parse.urlunparse(p._replace(netloc="telegra.ph")))

    # For Pastebin: try raw endpoint first
    if dom == "pastebin.com" and not p.path.startswith("/raw/"):
        raw_url = urllib.parse.urlunparse(p._replace(path="/raw" + p.path))
        urls_to_try.insert(0, raw_url)

    # For Rentry: try raw endpoint first
    if dom == "rentry.co" and not p.path.endswith("/raw"):
        raw_url = urllib.parse.urlunparse(p._replace(path=p.path.rstrip("/") + "/raw"))
        urls_to_try.insert(0, raw_url)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for target in urls_to_try:
        try:
            logger.info(f"[Telegraph Extractor] Fetching {target}...")
            resp = requests.get(target, headers=headers, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                return resp.text
        except Exception as e:
            logger.debug(f"[Telegraph Extractor] Target {target} failed: {e}")

    # Fallback for Telegraph: Try Telegraph JSON API on graph.org / telegra.ph
    if dom in TELEGRAPH_DOMAINS:
        path_slug = p.path.strip("/")
        for api_host in ("api.graph.org", "api.telegra.ph"):
            try:
                api_url = f"https://{api_host}/getPage/{path_slug}?return_content=true"
                r = requests.get(api_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("ok"):
                        content_nodes = data.get("result", {}).get("content", [])
                        return _telegraph_nodes_to_text(content_nodes)
            except Exception as ae:
                logger.debug(f"[Telegraph Extractor] API {api_host} fallback failed: {ae}")

    return None


def _telegraph_nodes_to_text(nodes) -> str:
    """Recursively converts Telegraph JSON DOM nodes into an HTML-like string."""
    parts = []
    if isinstance(nodes, list):
        for n in nodes:
            parts.append(_telegraph_nodes_to_text(n))
    elif isinstance(nodes, dict):
        tag = nodes.get("tag", "")
        attrs = nodes.get("attrs", {})
        href = attrs.get("href", "")
        children = nodes.get("children", [])
        child_text = _telegraph_nodes_to_text(children)
        if tag == "a" and href:
            parts.append(f'<a href="{href}">{child_text}</a>')
        else:
            parts.append(f"<{tag}>{child_text}</{tag}>")
    elif isinstance(nodes, str):
        parts.append(nodes)
    return " ".join(parts)


async def fetch_telegraph_or_paste_links(url: str) -> List[str]:
    """
    Asynchronously fetches a Telegraph or paste URL and extracts all download links.
    Returns: List of URLs in order of appearance.
    """
    content = await asyncio.to_thread(_fetch_page_content_sync, url)
    if not content:
        logger.warning(f"[Telegraph Extractor] Could not retrieve content from {url}")
        return []
    links = extract_links_from_html(content)
    logger.info(f"[Telegraph Extractor] Extracted {len(links)} links from {url}")
    return links


async def extract_all_download_links(text: str) -> List[str]:
    """
    Parses a user's text message or command argument:
      1. Finds all URLs present in the text.
      2. If any URL is a Telegraph article or recognized paste site,
         fetches the page and expands all links inside it.
      3. Preserves link sequence order.
      4. Deduplicates links so identical URLs aren't queued twice.
    Returns: Ordered list of final download URLs.
    """
    if not text or not isinstance(text, str):
        return []

    # Find all direct URLs in the input text
    matched_urls = [
        m.group(0).rstrip('.,;!?:)\'"[]{}<>')
        for m in URL_REGEX.finditer(text)
    ]

    all_links: List[str] = []
    seen: Set[str] = set()

    for u in matched_urls:
        u = u.strip().rstrip('.,;!?:)\'"[]{}<>')
        if not u:
            continue

        if is_telegraph_or_paste_url(u):
            # Expand Telegraph/paste site
            extracted = await fetch_telegraph_or_paste_links(u)
            for sub_url in extracted:
                if sub_url not in seen:
                    seen.add(sub_url)
                    all_links.append(sub_url)
        else:
            if u not in seen:
                seen.add(u)
                all_links.append(u)

    return all_links
