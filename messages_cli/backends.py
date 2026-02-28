"""Platform-agnostic adapter layer for message databases.

Queries iMessage and Telegram (and future platforms) through a unified interface.
Each function accepts an optional platform filter; None means query all available.
"""

from __future__ import annotations

import asyncio
import atexit
import re
from pathlib import Path

from . import db, send, telegram_send

# Telegram container path (same as telegram_db.py)
_TG_CONTAINER = Path.home() / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"

# Lazy TelegramDB singleton
_telegram_db = None


def _get_telegram_db():
    global _telegram_db
    if _telegram_db is None:
        from .telegram_db import TelegramDB
        _telegram_db = TelegramDB()
        atexit.register(_telegram_db.close)
    return _telegram_db


def available_platforms() -> list[str]:
    """Return list of platforms whose databases exist locally."""
    platforms = []
    if db.MESSAGES_DB.exists():
        platforms.append("messages")
    # Check for Telegram database without decrypting
    for variant in ("appstore", ""):
        base = _TG_CONTAINER / variant if variant else _TG_CONTAINER
        for account_dir in base.glob("account-*"):
            if (account_dir / "postbox/db/db_sqlite").exists():
                platforms.append("telegram")
                break
        if "telegram" in platforms:
            break
    return platforms


def _want(platform: str | None, name: str) -> bool:
    """Check if a platform should be queried given the filter."""
    if platform is None:
        return name in available_platforms()
    return platform == name


# ---------------------------------------------------------------------------
# Name resolution helpers (moved from cli.py)
# ---------------------------------------------------------------------------

def _format_phone(value: str) -> str:
    import phonenumbers
    if not value:
        return value
    # Telegram stores phones without '+' prefix; add it for parsing
    to_parse = value if value.startswith("+") else f"+{value}"
    try:
        parsed = phonenumbers.parse(to_parse)
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        )
    except phonenumbers.NumberParseException:
        return value


def _resolve_imessage_display_name(row: dict) -> str:
    """Resolve an iMessage chat row to a human-readable name."""
    cid = row["chat_identifier"]
    if row["display_name"]:
        return row["display_name"]
    if cid.startswith("chat"):
        conn = db._connect_messages()
        participants = db._get_chat_participants(conn, cid)
        conn.close()
        if participants:
            name = ", ".join(_format_phone(p) for p in participants[:3])
            if len(participants) > 3:
                name += f" +{len(participants) - 3}"
            return name
        return _format_phone(cid)
    # Single handle -- resolve to contact name
    handles = [cid]
    cache = db._build_contact_cache(handles)
    return cache.get(cid, "") or _format_phone(cid)


# ---------------------------------------------------------------------------
# Unified functions
# ---------------------------------------------------------------------------

def recent_chats(limit: int, platform: str | None = None) -> list[dict]:
    """Recent chats across platforms, sorted by last_message descending."""
    results = []

    if _want(platform, "messages"):
        # Fetch more than limit so we can merge properly
        rows = db.recent_chats(limit)
        for r in rows:
            results.append({
                "name": _resolve_imessage_display_name(r),
                "id": r["chat_identifier"],
                "platform": "messages",
                "last_message": r["last_msg"],
                "phone": _format_phone(r["chat_identifier"]) if not r["chat_identifier"].startswith("chat") else "",
                "username": "",
            })

    if _want(platform, "telegram"):
        tdb = _get_telegram_db()
        chats = tdb.recent_chats(limit)
        for c in chats:
            results.append({
                "name": c["name"],
                "id": str(c["peer_id"]),
                "platform": "telegram",
                "last_message": c["last_message"],
                "phone": _format_phone(c["phone"]) if c.get("phone") else "",
                "username": c.get("username", ""),
            })

    # Sort by last_message descending and take top limit
    results.sort(key=lambda x: x["last_message"], reverse=True)
    return results[:limit]


def find_chats(query: str, platform: str | None = None) -> list[dict]:
    """Find chats by name, phone, or username across platforms."""
    results = []

    if _want(platform, "messages"):
        rows = db.find_chats(query)
        for r in rows:
            results.append({
                "name": _resolve_imessage_display_name(r),
                "id": r["chat_identifier"],
                "platform": "messages",
                "phone": _format_phone(r["chat_identifier"]) if not r["chat_identifier"].startswith("chat") else "",
                "username": "",
            })

    if _want(platform, "telegram"):
        tdb = _get_telegram_db()
        matches = tdb.find_chats(query)
        for c in matches:
            results.append({
                "name": c["name"],
                "id": str(c["peer_id"]),
                "platform": "telegram",
                "phone": _format_phone(c["phone"]) if c.get("phone") else "",
                "username": c.get("username", ""),
            })

    return results


def read_messages(
    identifier: str, limit: int, platform: str | None = None
) -> list[dict]:
    """Read messages from a chat, auto-detecting platform if not specified."""
    if platform == "messages":
        # find_chats handles name->phone->chat_identifier resolution properly
        chats = db.find_chats(identifier)
        if chats:
            return db.read_messages(chats[0]["chat_identifier"], limit)
        # Fall back to resolve_identifier for direct chat IDs
        chat_id = db.resolve_identifier(identifier)
        return db.read_messages(chat_id, limit)

    if platform == "telegram":
        tdb = _get_telegram_db()
        peer_id = tdb.resolve_identifier(identifier)
        if peer_id is None:
            return []
        return tdb.read_messages(peer_id, limit)

    # Auto-detect: try both platforms
    im_chat_id = None
    tg_peer_id = None

    if "messages" in available_platforms():
        resolved = db.resolve_identifier(identifier)
        # find_chats uses LIKE with digits, which handles format differences
        chats = db.find_chats(identifier)
        if chats:
            im_chat_id = chats[0]["chat_identifier"]
        elif resolved != identifier:
            # resolve_identifier found a contact phone -- try that too
            chats = db.find_chats(resolved)
            if chats:
                im_chat_id = chats[0]["chat_identifier"]

    if "telegram" in available_platforms():
        tdb = _get_telegram_db()
        tg_peer_id = tdb.resolve_identifier(identifier)

    if im_chat_id and tg_peer_id:
        raise SystemExit(
            f'Found "{identifier}" on both Messages and Telegram. '
            "Use --platform/-p to specify which one."
        )

    if im_chat_id:
        return db.read_messages(im_chat_id, limit)
    if tg_peer_id:
        tdb = _get_telegram_db()
        return tdb.read_messages(tg_peer_id, limit)

    return []


def search_messages(
    query: str, limit: int, platform: str | None = None
) -> list[dict]:
    """Search messages across platforms, merged by timestamp descending."""
    results = []

    if _want(platform, "messages"):
        rows = db.search_messages(query, limit)
        for r in rows:
            chat_name = r["display_name"] or _format_phone(r["chat_identifier"])
            sender = r["sender"] if r["sender"] == "Me" else _format_phone(r["sender"])
            results.append({
                "timestamp": r["timestamp"],
                "chat_name": chat_name,
                "sender": sender,
                "text": r["text"],
                "platform": "messages",
            })

    if _want(platform, "telegram"):
        tdb = _get_telegram_db()
        rows = tdb.search_messages(query, limit)
        for r in rows:
            results.append({
                "timestamp": r["timestamp"],
                "chat_name": r["chat_name"],
                "sender": r["sender"],
                "text": r["text"],
                "platform": "telegram",
            })

    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results[:limit]


def stats(platform: str | None = None) -> list[dict]:
    """Get message/chat counts per platform."""
    results = []

    if _want(platform, "messages"):
        conn = db._connect_messages()
        msg_count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        chat_count = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
        conn.close()
        results.append({
            "platform": "messages",
            "messages": msg_count,
            "chats": chat_count,
        })

    if _want(platform, "telegram"):
        tdb = _get_telegram_db()
        s = tdb.stats()
        results.append({
            "platform": "telegram",
            "messages": s["messages"],
            "chats": s["peers"],
        })

    return results


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def send_message(
    identifier: str, text: str, platform: str | None = None
) -> tuple[str, str]:
    """Resolve identifier and send a message.

    Returns (platform_used, result_string).
    If platform is None, auto-detects (errors if ambiguous).
    """
    if platform == "messages":
        phone = db.resolve_identifier(identifier)
        result = send.send_message(phone, text)
        return "messages", result

    if platform == "telegram":
        if not telegram_send.is_available():
            raise SystemExit(
                "No Telegram auth keys found. Is Telegram installed and logged in?"
            )
        tdb = _get_telegram_db()
        peer_id = tdb.resolve_identifier(identifier)
        if peer_id is None:
            raise SystemExit(f'Could not find Telegram chat for "{identifier}".')
        result = asyncio.run(telegram_send.send_message(peer_id, text))
        return "telegram", result

    # Auto-detect
    im_phone = None
    tg_peer_id = None

    if "messages" in available_platforms():
        resolved = db.resolve_identifier(identifier)
        if resolved != identifier:
            im_phone = resolved
        else:
            chats = db.find_chats(identifier)
            if chats:
                im_phone = resolved

    if "telegram" in available_platforms() and telegram_send.is_available():
        tdb = _get_telegram_db()
        tg_peer_id = tdb.resolve_identifier(identifier)

    if im_phone and tg_peer_id:
        raise SystemExit(
            f'Found "{identifier}" on both Messages and Telegram. '
            "Use --platform/-p to specify which one."
        )

    if im_phone:
        result = send.send_message(im_phone, text)
        return "messages", result

    if tg_peer_id:
        result = asyncio.run(telegram_send.send_message(tg_peer_id, text))
        return "telegram", result

    raise SystemExit(f'Could not find "{identifier}" on any platform.')


def resolve_send_target(
    identifier: str, platform: str | None = None
) -> tuple[str, str]:
    """Resolve identifier for send preview (no actual send).

    Returns (platform_name, display_name) for dry-run output.
    """
    if platform == "messages":
        phone = db.resolve_identifier(identifier)
        return "messages", phone

    if platform == "telegram":
        if not telegram_send.is_available():
            raise SystemExit(
                "No Telegram auth keys found. Is Telegram installed and logged in?"
            )
        tdb = _get_telegram_db()
        peer_id = tdb.resolve_identifier(identifier)
        if peer_id is None:
            raise SystemExit(f'Could not find Telegram chat for "{identifier}".')
        peer = tdb._get_peer(peer_id)
        from .telegram_db import _peer_display_name
        return "telegram", _peer_display_name(peer)

    # Auto-detect
    im_phone = None
    tg_peer_id = None

    if "messages" in available_platforms():
        resolved = db.resolve_identifier(identifier)
        if resolved != identifier:
            im_phone = resolved
        else:
            chats = db.find_chats(identifier)
            if chats:
                im_phone = resolved

    if "telegram" in available_platforms() and telegram_send.is_available():
        tdb = _get_telegram_db()
        tg_peer_id = tdb.resolve_identifier(identifier)

    if im_phone and tg_peer_id:
        raise SystemExit(
            f'Found "{identifier}" on both Messages and Telegram. '
            "Use --platform/-p to specify which one."
        )

    if im_phone:
        return "messages", im_phone

    if tg_peer_id:
        tdb = _get_telegram_db()
        peer = tdb._get_peer(tg_peer_id)
        from .telegram_db import _peer_display_name
        return "telegram", _peer_display_name(peer)

    raise SystemExit(f'Could not find "{identifier}" on any platform.')
