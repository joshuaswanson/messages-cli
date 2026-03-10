"""Shared utilities for messages-cli."""

from datetime import datetime
from pathlib import Path

# WhatsApp session store (shared between Python and Go tools)
WHATSAPP_SESSION_DIR = Path.home() / ".whatsapp-cli"


def format_phone(value: str) -> str:
    """Format a phone number by country code."""
    import phonenumbers
    if not value:
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
