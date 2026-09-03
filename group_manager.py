import json
import logging
from pathlib import Path
from typing import Dict, List, Any

from config import BASE_DIR

logger = logging.getLogger(__name__)

DATA_DIR = BASE_DIR / "data"
GROUPS_FILE = DATA_DIR / "allowed_groups.json"


def _ensure_data_file():
    """Ensures data directory and allowed_groups.json exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not GROUPS_FILE.exists():
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)


def load_allowed_groups() -> Dict[str, Dict[str, Any]]:
    """Loads allowed groups from disk."""
    _ensure_data_file()
    try:
        with open(GROUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read {GROUPS_FILE}: {e}")
        return {}


def save_allowed_groups(groups: Dict[str, Dict[str, Any]]) -> bool:
    """Saves allowed groups to disk."""
    _ensure_data_file()
    try:
        with open(GROUPS_FILE, "w", encoding="utf-8") as f:
            json.dump(groups, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to write {GROUPS_FILE}: {e}")
        return False


def is_group_allowed(chat_id: int) -> bool:
    """Checks if a group chat ID is allowed."""
    groups = load_allowed_groups()
    return str(chat_id) in groups


def enable_group(chat_id: int, title: str = "") -> bool:
    """Enables bot usage in a specific group chat."""
    groups = load_allowed_groups()
    groups[str(chat_id)] = {
        "chat_id": chat_id,
        "title": title or "Unnamed Group",
    }
    return save_allowed_groups(groups)


def disable_group(chat_id: int) -> bool:
    """Disables bot usage in a specific group chat."""
    groups = load_allowed_groups()
    key = str(chat_id)
    if key in groups:
        del groups[key]
        return save_allowed_groups(groups)
    return True


def list_allowed_groups() -> List[Dict[str, Any]]:
    """Returns list of allowed groups."""
    groups = load_allowed_groups()
    return list(groups.values())
