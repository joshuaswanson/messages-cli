"""Shared utilities for messages-cli."""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import phonenumbers

from .models import AmbiguousChatError

if TYPE_CHECKING:
    from .models import Chat

# WhatsApp session store (shared between Python and Go tools)
WHATSAPP_SESSION_DIR = Path.home() / ".whatsapp-cli"

# Seconds between 1970-01-01 and 2001-01-01 (Apple CoreData reference date)
COREDATA_EPOCH = 978307200

_PACKAGE_DIR = Path(__file__).resolve().parent
INSTALLED_BIN_DIR = Path.home() / ".local/share/messages-cli/bin"

_PHONE_CHARS = re.compile(r"^\+?[\d\s\-().]+$")
MIN_PHONE_DIGITS = 7


def go_source_dir() -> Path | None:
    """Location of the Go tool sources (wheel installs and source checkouts)."""
    for candidate in (_PACKAGE_DIR / "go", _PACKAGE_DIR.parent / "go"):
        if (candidate / "go.mod").exists():
            return candidate
    return None


def find_tool(name: str) -> Path | None:
    """Locate a helper binary built from the Go sources."""
    candidates = []
    if env_dir := os.environ.get("MESSAGES_CLI_BIN_DIR"):
        candidates.append(Path(env_dir) / name)
    candidates.append(INSTALLED_BIN_DIR / name)
    if src := go_source_dir():
        candidates.append(src / "bin" / name)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    on_path = shutil.which(name)
    return Path(on_path) if on_path else None


def looks_like_phone(value: str) -> bool:
    """True if the value is written like a phone number."""
    stripped = value.strip()
    if not _PHONE_CHARS.match(stripped):
        return False
    return len(re.sub(r"\D", "", stripped)) >= MIN_PHONE_DIGITS


def phone_key(value: str) -> str:
    """Digits that identify a phone number across national and international formats."""
    return re.sub(r"\D", "", value).lstrip("0")[-10:]


def like_pattern(value: str) -> str:
    """Wrap a value for a SQL `LIKE ? ESCAPE '\\'` substring match."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def choose_chat(identifier: str, chats: list[Chat], strict: bool = False) -> Chat | None:
    """Pick the chat an identifier refers to, or raise if it is ambiguous.

    Preference order: the only match, the only exact name match, the only
    direct (non-group) chat. Unless strict, several direct chats with the
    same name (usually one person reachable by phone and email) resolve to
    the first, so callers should pass chats sorted by recency. Sending uses
    strict because two different people can share a name.
    """
    if not chats:
        return None
    if len(chats) == 1:
        return chats[0]
    wanted = identifier.strip().casefold()
    exact = [c for c in chats if c.name.casefold() == wanted]
    if len(exact) == 1:
        return exact[0]
    direct = [c for c in (exact or chats) if not c.is_group]
    if len(direct) == 1 or (not strict and direct and len({c.name for c in direct}) == 1):
        return direct[0]
    names = ", ".join(sorted({c.name for c in chats})[:5])
    raise AmbiguousChatError(
        f'Multiple chats match "{identifier}" ({names}). '
        "Use a more specific name, a phone number, or a chat ID."
    )


def format_phone(value: str) -> str:
    """Format a phone number by country code."""
    if not value or not looks_like_phone(value):
        return value
    to_parse = value if value.startswith("+") else f"+{value}"
    try:
        parsed = phonenumbers.parse(to_parse)
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        )
    except phonenumbers.NumberParseException:
        return value


def format_ts(unix_ts: int | float | None) -> str:
    """Format unix timestamp as ISO 8601 with local timezone."""
    if unix_ts is None:
        return ""
    return datetime.fromtimestamp(unix_ts).astimezone().isoformat()


def format_ts_ms(ts_ms: int | None) -> str:
    """Format millisecond timestamp as ISO 8601 with local timezone."""
    if ts_ms is None:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000).astimezone().isoformat()
    except (ValueError, OSError):
        return ""
