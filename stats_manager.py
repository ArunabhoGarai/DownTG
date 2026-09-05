"""
Statistics and Link Analytics Manager for Telegram Downloader Bot.

Tracks:
- Total links resolved (overall and categorized by platform).
- Platform distribution (TeraBox, DiskWala, YouTube, Instagram, Facebook, TikTok, X, Generic).
- Top users leaderboard with bandwidth and platform preferences.
- Per-user link history and audit log (which users resolved which links).
- Thread-safe atomic persistence in data/stats.json.
"""

import os
import re
import json
import time
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from config import BASE_DIR

logger = logging.getLogger(__name__)


def format_bytes(size: Optional[int]) -> str:
    """Format byte size into human readable string."""
    if not size or size <= 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

# Persistence storage
DATA_DIR = BASE_DIR / "data"
STATS_FILE = DATA_DIR / "stats.json"

# In-memory stats cache
_STATS_CACHE: Optional[Dict[str, Any]] = None
_STATS_LOCK = asyncio.Lock()

# Supported platform classification keys and metadata
PLATFORM_META = {
    "terabox": {"badge": "📦 TeraBox", "order": 1},
    "diskwala": {"badge": "💿 DiskWala", "order": 2},
    "youtube": {"badge": "▶️ YouTube", "order": 3},
    "instagram": {"badge": "📸 Instagram", "order": 4},
    "facebook": {"badge": "🔵 Facebook", "order": 5},
    "tiktok": {"badge": "🎵 TikTok", "order": 6},
    "twitter": {"badge": "🐦 X / Twitter", "order": 7},
    "generic": {"badge": "🌐 Generic / Other", "order": 8},
}


def _default_stats_schema() -> Dict[str, Any]:
    """Returns a fresh empty statistics schema."""
    return {
        "total_resolved": 0,
        "total_bytes": 0,
        "platforms": {k: 0 for k in PLATFORM_META},
        "users": {},
        "recent_activity": [],
    }


def _ensure_loaded() -> Dict[str, Any]:
    """Loads statistics from disk if not already cached in memory."""
    global _STATS_CACHE
    if _STATS_CACHE is not None:
        return _STATS_CACHE

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure all schema keys exist
                base = _default_stats_schema()
                for k, v in base.items():
                    if k not in data:
                        data[k] = v
                for pk in PLATFORM_META:
                    if pk not in data["platforms"]:
                        data["platforms"][pk] = 0
                _STATS_CACHE = data
                return _STATS_CACHE
        except Exception as e:
            logger.error(f"[StatsManager] Failed to read {STATS_FILE}: {e}")

    _STATS_CACHE = _default_stats_schema()
    return _STATS_CACHE


def _save_stats_atomic_sync():
    """Synchronously writes the in-memory cache to disk via an atomic temporary file."""
    if _STATS_CACHE is None:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = STATS_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(_STATS_CACHE, f, indent=2, ensure_ascii=False)
        tmp_file.replace(STATS_FILE)
    except Exception as e:
        logger.error(f"[StatsManager] Failed to persist stats to {STATS_FILE}: {e}")
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass


def classify_platform(url: str) -> Tuple[str, str]:
    """
    Classifies a URL into a normalized platform key and user-friendly badge.
    Returns: (platform_key, display_badge)
    """
    u = (url or "").lower().strip()
    if any(k in u for k in ("terabox", "1024tera", "4funbox", "mirrobox", "nephobox", "teradownloader")):
        return "terabox", "📦 TeraBox"
    elif "diskwala" in u:
        return "diskwala", "💿 DiskWala"
    elif "youtube.com" in u or "youtu.be" in u:
        if "/shorts/" in u:
            return "youtube", "🔴 YouTube Shorts"
        return "youtube", "▶️ YouTube"
    elif "instagram.com" in u:
        if "/reel/" in u or "/reels/" in u:
            return "instagram", "📸 Instagram Reel"
        return "instagram", "📸 Instagram"
    elif any(k in u for k in ("facebook.com", "fb.watch", "fb.com")):
        if "/reel/" in u or "/reels/" in u:
            return "facebook", "🔵 Facebook Reel"
        return "facebook", "🔵 Facebook"
    elif "tiktok.com" in u:
        return "tiktok", "🎵 TikTok"
    elif "twitter.com" in u or "x.com" in u:
        return "twitter", "🐦 X / Twitter"
    return "generic", "🌐 Generic / Other"


async def record_resolved_link(
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    url: str = "",
    title: str = "",
    file_size: int = 0,
    chat_id: Optional[int] = None,
    is_group: bool = False,
):
    """
    Records a successful link resolution event into the stats database.
    Thread-safe and async-protected.
    """
    global _STATS_CACHE
    async with _STATS_LOCK:
        stats = _ensure_loaded()
        now = int(time.time())

        plat_key, plat_badge = classify_platform(url)

        # 1. Update Global Counters
        stats["total_resolved"] = stats.get("total_resolved", 0) + 1
        stats["total_bytes"] = stats.get("total_bytes", 0) + max(0, file_size)

        if "platforms" not in stats:
            stats["platforms"] = {k: 0 for k in PLATFORM_META}
        stats["platforms"][plat_key] = stats["platforms"].get(plat_key, 0) + 1

        # 2. Update User Profile & User History
        uid_str = str(user_id)
        users = stats.setdefault("users", {})

        full_name = f"{first_name or ''} {last_name or ''}".strip() or (username or f"User_{user_id}")

        if uid_str not in users:
            users[uid_str] = {
                "user_id": user_id,
                "username": username or "",
                "full_name": full_name,
                "total_resolved": 0,
                "total_bytes": 0,
                "first_seen": now,
                "last_seen": now,
                "platforms": {k: 0 for k in PLATFORM_META},
                "recent_links": [],
            }

        user_entry = users[uid_str]
        user_entry["total_resolved"] += 1
        user_entry["total_bytes"] += max(0, file_size)
        user_entry["last_seen"] = now
        if username:
            user_entry["username"] = username
        if full_name and full_name != f"User_{user_id}":
            user_entry["full_name"] = full_name

        if "platforms" not in user_entry:
            user_entry["platforms"] = {k: 0 for k in PLATFORM_META}
        user_entry["platforms"][plat_key] = user_entry["platforms"].get(plat_key, 0) + 1

        link_record = {
            "url": url,
            "platform": plat_key,
            "title": title or "Untitled Media",
            "file_size": file_size,
            "timestamp": now,
            "is_group": is_group,
            "chat_id": chat_id,
        }

        # Keep last 30 links per user
        user_links = user_entry.setdefault("recent_links", [])
        user_links.insert(0, link_record)
        if len(user_links) > 30:
            user_entry["recent_links"] = user_links[:30]

        # 3. Update Global Activity Stream (keep last 50)
        activity_record = {
            "user_id": user_id,
            "username": username or "",
            "full_name": full_name,
            "url": url,
            "platform": plat_key,
            "title": title or "Untitled Media",
            "file_size": file_size,
            "timestamp": now,
            "is_group": is_group,
        }
        activity = stats.setdefault("recent_activity", [])
        activity.insert(0, activity_record)
        if len(activity) > 50:
            stats["recent_activity"] = activity[:50]

        # Persist to disk asynchronously in a background thread
        await asyncio.to_thread(_save_stats_atomic_sync)
        logger.info(f"[StatsManager] Recorded resolution for user {user_id} ({plat_key}): {url[:60]}")


def get_overview_stats() -> Dict[str, Any]:
    """Returns aggregated global statistics."""
    stats = _ensure_loaded()
    total_resolved = stats.get("total_resolved", 0)
    total_bytes = stats.get("total_bytes", 0)
    platforms = stats.get("platforms", {})
    users = stats.get("users", {})

    top_user = None
    if users:
        sorted_u = sorted(users.values(), key=lambda x: x.get("total_resolved", 0), reverse=True)
        if sorted_u and sorted_u[0].get("total_resolved", 0) > 0:
            top_user = sorted_u[0]

    return {
        "total_resolved": total_resolved,
        "total_bytes": total_bytes,
        "total_users": len(users),
        "platforms": dict(platforms),
        "top_user": top_user,
    }


def get_top_users(limit: int = 10) -> List[Dict[str, Any]]:
    """Returns top users ranked by total resolved links."""
    stats = _ensure_loaded()
    users = list(stats.get("users", {}).values())
    users.sort(key=lambda u: (u.get("total_resolved", 0), u.get("total_bytes", 0)), reverse=True)
    return users[:limit]


def get_user_stats(user_id_or_username: Any) -> Optional[Dict[str, Any]]:
    """Fetches stats for a single user by numeric user_id or username string."""
    stats = _ensure_loaded()
    users = stats.get("users", {})

    target_str = str(user_id_or_username).strip().lstrip("@").lower()

    # Try exact user_id match
    if target_str in users:
        return users[target_str]

    # Try numeric ID lookup
    for uid, udata in users.items():
        if str(udata.get("user_id")) == target_str:
            return udata

    # Try username lookup
    for udata in users.values():
        if udata.get("username", "").lower() == target_str:
            return udata

    return None


def get_recent_activity(limit: int = 10, platform: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns recent link resolution activity log."""
    stats = _ensure_loaded()
    activity = stats.get("recent_activity", [])
    if platform:
        activity = [a for a in activity if a.get("platform") == platform]
    return activity[:limit]


def _format_time_ago(ts: int) -> str:
    """Formats a Unix timestamp into a relative human time (e.g. 2m ago, 1h ago)."""
    if not ts:
        return "Never"
    diff = max(0, int(time.time() - ts))
    if diff < 60:
        return f"{diff}s ago"
    elif diff < 3600:
        return f"{diff // 60}m ago"
    elif diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _clean_markdown(text: str) -> str:
    """Escapes problematic Markdown characters for safe Telegram display."""
    if not text:
        return ""
    return re.sub(r"([_*`\[\]])", r"\\\1", str(text))


# ── Presentation / Formatting Functions ──

def format_overview_text() -> str:
    """Renders the main Analytics Overview dashboard text."""
    data = get_overview_stats()
    tot = data["total_resolved"]
    bytes_str = format_bytes(data["total_bytes"])
    u_count = data["total_users"]
    plats = data["platforms"]

    # Top platform
    sorted_plats = sorted(plats.items(), key=lambda p: p[1], reverse=True)
    top_plat_name = PLATFORM_META.get(sorted_plats[0][0], {}).get("badge", "None") if (sorted_plats and sorted_plats[0][1] > 0) else "None"

    # Top user
    top_u = data.get("top_user")
    if top_u:
        u_name = f"@{top_u['username']}" if top_u.get("username") else top_u.get("full_name", f"ID: {top_u['user_id']}")
        top_user_line = f"• **Most Active User:** `{_clean_markdown(u_name)}` (**{top_u['total_resolved']}** links)\n"
    else:
        top_user_line = ""

    # Platform distribution rows
    plat_lines = []
    for pk, pmeta in sorted(PLATFORM_META.items(), key=lambda x: x[1]["order"]):
        cnt = plats.get(pk, 0)
        pct = (cnt / tot * 100) if tot > 0 else 0
        plat_lines.append(f"• {pmeta['badge']}: **{cnt}** `({pct:.1f}%)`")

    text = (
        "📊 **Bot Analytics & Resolution Dashboard**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔢 **Total Links Resolved:** `{tot}`\n"
        f"💾 **Total Bandwidth:** `{bytes_str}`\n"
        f"👥 **Total Unique Users:** `{u_count}`\n"
        f"👑 **Top Platform:** `{top_plat_name}`\n"
        f"{top_user_line}\n"
        "📈 **Platform Breakdown:**\n"
        + "\n".join(plat_lines)
        + "\n\n💡 *Use the buttons below to browse the leaderboard, platform stats, or recent links.*"
    )
    return text


def format_top_users_text(limit: int = 10) -> str:
    """Renders the Top Users Leaderboard."""
    top_users = get_top_users(limit=limit)

    if not top_users or top_users[0].get("total_resolved", 0) == 0:
        return (
            "🏆 **Top Users Leaderboard**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "ℹ️ No download activity recorded yet. Start resolving links to populate the leaderboard!"
        )

    lines = [
        "🏆 **Top Users Leaderboard (Most Active)**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for idx, u in enumerate(top_users):
        if u.get("total_resolved", 0) <= 0:
            continue
        rank_icon = medals[idx] if idx < len(medals) else f"**{idx + 1}.**"
        uname = f"@{u['username']}" if u.get("username") else u.get("full_name", f"User_{u['user_id']}")
        u_bytes = format_bytes(u.get("total_bytes", 0))

        # Find user's top platform
        u_plats = u.get("platforms", {})
        top_p = max(u_plats.items(), key=lambda x: x[1]) if u_plats else ("generic", 0)
        top_badge = PLATFORM_META.get(top_p[0], {}).get("badge", "Generic") if top_p[1] > 0 else "N/A"

        lines.append(
            f"{rank_icon} **{_clean_markdown(uname)}** (ID: `{u['user_id']}`)\n"
            f"   • Links: **{u['total_resolved']}** | Bandwidth: `{u_bytes}`\n"
            f"   • Favorite: {top_badge} ({top_p[1]} links) | Last: *{_format_time_ago(u.get('last_seen', 0))}*\n"
        )

    lines.append("💡 *Tap any user button below to view their complete link history!*")
    return "\n".join(lines)


def format_platform_breakdown_text() -> str:
    """Renders in-depth Platform Breakdown statistics."""
    stats = _ensure_loaded()
    tot = stats.get("total_resolved", 0)
    plats = stats.get("platforms", {})

    sorted_plats = sorted(PLATFORM_META.items(), key=lambda p: plats.get(p[0], 0), reverse=True)

    lines = [
        "📈 **Detailed Platform Breakdown**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for pk, pmeta in sorted_plats:
        cnt = plats.get(pk, 0)
        pct = (cnt / tot * 100) if tot > 0 else 0
        bar_len = int(pct / 10)
        bar = "█" * bar_len + "░" * (10 - bar_len)

        lines.append(
            f"{pmeta['badge']}\n"
            f"  `[{bar}]` **{cnt}** ({pct:.1f}%)\n"
        )

    lines.append(f"📊 **Grand Total:** `{tot}` resolved links across all platforms.")
    return "\n".join(lines)


def format_recent_activity_text(limit: int = 10) -> str:
    """Renders the Latest Link Resolution events."""
    activity = get_recent_activity(limit=limit)

    if not activity:
        return (
            "📜 **Recent Link Resolution Activity**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "ℹ️ No recent link resolution activity found."
        )

    lines = [
        "📜 **Recent Resolved Links Audit**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]

    for idx, act in enumerate(activity, 1):
        plat_meta = PLATFORM_META.get(act.get("platform", "generic"), {})
        badge = plat_meta.get("badge", "🌐 Video")
        uname = f"@{act['username']}" if act.get("username") else act.get("full_name", f"ID: {act['user_id']}")
        title = act.get("title", "Untitled Media")
        if len(title) > 40:
            title = title[:37] + "..."
        size_str = format_bytes(act.get("file_size", 0))
        time_str = _format_time_ago(act.get("timestamp", 0))

        raw_url = act.get("url", "")
        # Short display URL
        short_url = raw_url[:45] + "..." if len(raw_url) > 45 else raw_url

        lines.append(
            f"**{idx}.** {badge} • `{size_str}` • *{time_str}*\n"
            f"   📌 **Title:** `{_clean_markdown(title)}`\n"
            f"   👤 **User:** `{_clean_markdown(uname)}` (`{act['user_id']}`)\n"
            f"   🔗 [Open Original Link]({raw_url})\n"
        )

    return "\n".join(lines)


def format_user_detail_text(user_id_or_username: Any) -> str:
    """Renders detailed resolution history and platform usage for a specific user."""
    udata = get_user_stats(user_id_or_username)
    if not udata:
        return f"❌ User `{_clean_markdown(str(user_id_or_username))}` was not found in the resolution database."

    uname = f"@{udata['username']}" if udata.get("username") else udata.get("full_name", f"User_{udata['user_id']}")
    tot = udata.get("total_resolved", 0)
    bytes_str = format_bytes(udata.get("total_bytes", 0))
    first_seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(udata.get("first_seen", 0)))
    last_seen = _format_time_ago(udata.get("last_seen", 0))

    u_plats = udata.get("platforms", {})
    plat_lines = []
    for pk, pmeta in sorted(PLATFORM_META.items(), key=lambda x: u_plats.get(x[0], 0), reverse=True):
        cnt = u_plats.get(pk, 0)
        if cnt > 0:
            pct = (cnt / tot * 100) if tot > 0 else 0
            plat_lines.append(f"• {pmeta['badge']}: **{cnt}** `({pct:.1f}%)`")

    links = udata.get("recent_links", [])
    link_lines = []
    for idx, l in enumerate(links[:8], 1):
        plat_meta = PLATFORM_META.get(l.get("platform", "generic"), {})
        badge = plat_meta.get("badge", "🌐")
        title = l.get("title", "Untitled")
        if len(title) > 35:
            title = title[:32] + "..."
        size_str = format_bytes(l.get("file_size", 0))
        time_str = _format_time_ago(l.get("timestamp", 0))
        link_url = l.get("url", "")
        link_lines.append(f"  **{idx}.** {badge} [{_clean_markdown(title)}]({link_url}) (`{size_str}`) • *{time_str}*")

    text = (
        f"👤 **User Activity Profile: {_clean_markdown(uname)}**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"• **User ID:** `{udata['user_id']}`\n"
        f"• **Full Name:** {_clean_markdown(udata.get('full_name', 'N/A'))}\n"
        f"• **Total Links Resolved:** **{tot}**\n"
        f"• **Total Bandwidth:** `{bytes_str}`\n"
        f"• **First Seen:** `{first_seen}`\n"
        f"• **Last Active:** *{last_seen}*\n\n"
        "📊 **Platform Usage:**\n"
        + ("\n".join(plat_lines) if plat_lines else "• No platform data yet")
        + "\n\n"
        "📜 **Recent Links Resolved by User (Last 8):**\n"
        + ("\n".join(link_lines) if link_lines else "• No recent links")
    )
    return text


def format_personal_stats_text(
    user_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
) -> str:
    """Renders personal usage statistics for a regular non-admin user."""
    udata = get_user_stats(user_id)
    global_stats = get_overview_stats()
    tot_global = global_stats["total_resolved"]

    if not udata or udata.get("total_resolved", 0) == 0:
        display_name = f"@{username}" if username else (first_name or f"User_{user_id}")
        return (
            "📊 **Your Download Statistics**\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Hello `{_clean_markdown(display_name)}`, you haven't resolved any links yet!\n\n"
            f"🌍 **Total Community Downloads:** `{tot_global}` links resolved till now.\n\n"
            "💡 *Send any supported link (TeraBox, DiskWala, YouTube, Instagram, etc.) to get started!*"
        )

    uname = f"@{udata['username']}" if udata.get("username") else (f"@{username}" if username else (first_name or f"User_{user_id}"))
    tot = udata.get("total_resolved", 0)
    bytes_str = format_bytes(udata.get("total_bytes", 0))
    first_seen = time.strftime("%Y-%m-%d", time.localtime(udata.get("first_seen", 0)))

    u_plats = udata.get("platforms", {})
    plat_lines = []
    for pk, pmeta in sorted(PLATFORM_META.items(), key=lambda x: u_plats.get(x[0], 0), reverse=True):
        cnt = u_plats.get(pk, 0)
        if cnt > 0:
            pct = (cnt / tot * 100) if tot > 0 else 0
            plat_lines.append(f"• {pmeta['badge']}: **{cnt}** `({pct:.1f}%)`")

    return (
        f"📊 **Your Personal Download Statistics**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 **User:** `{_clean_markdown(uname)}`\n"
        f"🔢 **Your Total Links Resolved:** **{tot}**\n"
        f"💾 **Your Total Bandwidth:** `{bytes_str}`\n"
        f"📅 **Member Since:** `{first_seen}`\n\n"
        "📈 **Your Favorite Platforms:**\n"
        + ("\n".join(plat_lines) if plat_lines else "• None")
        + f"\n\n🌍 **Total Community Downloads:** `{tot_global}` links"
    )


def build_stats_keyboard(current_view: str = "overview", target_user_id: Optional[Any] = None):
    """
    Constructs the navigation InlineKeyboardMarkup for the analytics dashboard.
    """
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None

    buttons = []

    if current_view == "overview":
        buttons.append([
            InlineKeyboardButton("🏆 Top Users", callback_data="stats_view:top_users"),
            InlineKeyboardButton("📈 Platforms", callback_data="stats_view:platforms"),
        ])
        buttons.append([
            InlineKeyboardButton("📜 Recent Activity", callback_data="stats_view:recent"),
            InlineKeyboardButton("🔄 Refresh", callback_data="stats_view:overview"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Close Dashboard", callback_data="stats_close"),
        ])

    elif current_view == "top_users":
        # Add clickable drill-down buttons for top users (up to 5)
        top_u = get_top_users(limit=5)
        user_row = []
        for u in top_u:
            if u.get("total_resolved", 0) > 0:
                name_btn = f"👤 @{u['username']}" if u.get("username") else f"👤 {u.get('full_name', str(u['user_id']))[:10]}"
                user_row.append(InlineKeyboardButton(f"{name_btn} ({u['total_resolved']})", callback_data=f"stats_user:{u['user_id']}"))

        # Chunk user buttons into pairs
        for i in range(0, len(user_row), 2):
            buttons.append(user_row[i:i+2])

        buttons.append([
            InlineKeyboardButton("📊 Overview", callback_data="stats_view:overview"),
            InlineKeyboardButton("📈 Platforms", callback_data="stats_view:platforms"),
        ])
        buttons.append([
            InlineKeyboardButton("📜 Recent Activity", callback_data="stats_view:recent"),
            InlineKeyboardButton("🔄 Refresh", callback_data="stats_view:top_users"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Close", callback_data="stats_close"),
        ])

    elif current_view == "platforms":
        buttons.append([
            InlineKeyboardButton("📊 Overview", callback_data="stats_view:overview"),
            InlineKeyboardButton("🏆 Top Users", callback_data="stats_view:top_users"),
        ])
        buttons.append([
            InlineKeyboardButton("📜 Recent Activity", callback_data="stats_view:recent"),
            InlineKeyboardButton("🔄 Refresh", callback_data="stats_view:platforms"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Close", callback_data="stats_close"),
        ])

    elif current_view == "recent":
        buttons.append([
            InlineKeyboardButton("📊 Overview", callback_data="stats_view:overview"),
            InlineKeyboardButton("🏆 Top Users", callback_data="stats_view:top_users"),
        ])
        buttons.append([
            InlineKeyboardButton("📈 Platforms", callback_data="stats_view:platforms"),
            InlineKeyboardButton("🔄 Refresh", callback_data="stats_view:recent"),
        ])
        buttons.append([
            InlineKeyboardButton("❌ Close", callback_data="stats_close"),
        ])

    elif current_view == "user_detail":
        cb_refresh = f"stats_user:{target_user_id}" if target_user_id else "stats_view:top_users"
        buttons.append([
            InlineKeyboardButton("⬅️ Back to Top Users", callback_data="stats_view:top_users"),
            InlineKeyboardButton("📊 Overview", callback_data="stats_view:overview"),
        ])
        buttons.append([
            InlineKeyboardButton("🔄 Refresh", callback_data=cb_refresh),
            InlineKeyboardButton("❌ Close", callback_data="stats_close"),
        ])

    return InlineKeyboardMarkup(buttons)
