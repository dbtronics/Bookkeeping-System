"""
settings_utils.py — Load and save user-configurable settings.

Stores account types and per-account-type category lists in
NEXTCLOUD_BASE/master/settings.json. Falls back to the hardcoded
defaults in config.py on first run (or if the file is missing).

IMPORTANT: All callers should use the public helpers below rather than
reading config.py directly, so that runtime changes made through the UI
take effect without a server restart.
"""

import json
import logging
from datetime import date
from pathlib import Path

from config import (
    SETTINGS_JSON,
    BUSINESS_CATEGORIES,
    PERSONAL_CATEGORIES,
    EXCLUDE_FROM_PNL_CATEGORIES,
)

log = logging.getLogger("settings_utils")

_DEFAULT_ACCOUNT_TYPES = ["personal", "business"]

_DEFAULTS = {
    "version": "1.0",
    "account_types": _DEFAULT_ACCOUNT_TYPES,
    "categories": {
        "business": list(BUSINESS_CATEGORIES),
        "personal": list(PERSONAL_CATEGORIES),
    },
    "exclude_from_pnl_categories": sorted(EXCLUDE_FROM_PNL_CATEGORIES),
}


# ---------------------------------------------------------------------------
# Core load / save
# ---------------------------------------------------------------------------

def load_settings():
    """Read settings.json and return the full settings dict.

    Creates the file from hardcoded defaults on first run.
    Always returns a valid dict — never raises.
    """
    path = Path(SETTINGS_JSON)
    if not path.exists():
        return _init_settings()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        changed = _backfill_defaults(data)
        if changed:
            _write(data)
        return data
    except Exception as e:
        log.error("Failed to load settings.json: %s — using defaults", e)
        return {k: v for k, v in _DEFAULTS.items()}


def save_settings(data):
    """Write settings dict to settings.json. Returns True on success."""
    data["last_updated"] = date.today().isoformat()
    return _write(data)


# ---------------------------------------------------------------------------
# Public helpers (call these instead of reading load_settings() yourself)
# ---------------------------------------------------------------------------

def get_account_types():
    """Return the ordered list of configured account types."""
    return load_settings().get("account_types", list(_DEFAULT_ACCOUNT_TYPES))


def get_categories(account_type=None):
    """Return category list for account_type, or full categories dict if None."""
    cats = load_settings().get("categories", _DEFAULTS["categories"])
    if account_type is None:
        return cats
    # Fall back to business defaults if requested type has no categories yet
    return cats.get(account_type, cats.get("business", list(BUSINESS_CATEGORIES)))


def get_exclude_from_pnl_categories():
    """Return the set of categories that auto-set exclude_from_pnl=True."""
    return set(load_settings().get(
        "exclude_from_pnl_categories",
        list(EXCLUDE_FROM_PNL_CATEGORIES),
    ))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _backfill_defaults(data):
    """Add missing top-level keys. Returns True if anything was added."""
    changed = False
    for key, default in _DEFAULTS.items():
        if key not in data:
            data[key] = default
            changed = True
    # Ensure every known account_type has a category list
    cats = data.setdefault("categories", {})
    for acct_type in data.get("account_types", []):
        if acct_type not in cats:
            # Try to pull from defaults, else empty list
            cats[acct_type] = list(_DEFAULTS["categories"].get(acct_type, []))
            changed = True
    return changed


def _init_settings():
    """Create settings.json from hardcoded defaults. Returns the new dict."""
    data = {k: v for k, v in _DEFAULTS.items()}
    data["categories"] = {
        k: list(v) for k, v in _DEFAULTS["categories"].items()
    }
    data["exclude_from_pnl_categories"] = list(_DEFAULTS["exclude_from_pnl_categories"])
    _write(data)
    log.info("Initialized settings.json from defaults")
    return data


def _write(data):
    """Atomic write via temp file. Returns True on success."""
    path = Path(SETTINGS_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(path)
        log.debug("Wrote settings.json")
        return True
    except Exception as e:
        log.error("Failed to write settings.json: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False
