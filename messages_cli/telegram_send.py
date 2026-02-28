"""Send Telegram messages via Telethon.

Uses the official macOS Telegram api_id/api_hash (public in source code).
Extracts the persistent auth key from the local Telegram database, so no
separate login is needed -- if you're logged into Telegram.app, sending works.
"""

from __future__ import annotations

import plistlib

from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession

# Official macOS Telegram values (public in source)
_API_ID = 2834
_API_HASH = "68875f756c9b437a8b916ca3de215815"

# Standard Telegram DC addresses
_DC_ADDRESSES = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


def _extract_auth_keys() -> dict[int, bytes]:
    """Extract persistent auth keys from the Telegram postbox t1 table.

    Returns {dc_id: auth_key_bytes} for standard DCs (1-5) with persistent keys.
    """
    from .telegram_db import TelegramDB

    tg = TelegramDB()
    tg._ensure_connection()
    conn = tg._conn

    row = conn.execute(
        "SELECT value FROM t1 WHERE key = ?", (b"persistent:datacenterAuthInfoById",)
    ).fetchone()
    tg.close()

    if not row:
        return {}

    plist = plistlib.loads(row[0])
    objects = plist["$objects"]

    def resolve(uid):
        return objects[uid] if isinstance(uid, plistlib.UID) else uid

    root = objects[1]
    results = {}
    for key_uid, val_uid in zip(root["NS.keys"], root["NS.objects"]):
        dc_id = resolve(key_uid)
        if not isinstance(dc_id, int) or dc_id not in _DC_ADDRESSES:
            continue

        info = resolve(val_uid)
        if not isinstance(info, dict) or "authKey" not in info:
            continue

        # Only use persistent keys (validUntilTimestamp == 0)
        if info.get("validUntilTimestamp", 0) != 0:
            continue

        auth_key_obj = resolve(info["authKey"])
        auth_key_bytes = auth_key_obj.get("NS.data", b"") if isinstance(auth_key_obj, dict) else b""
        if len(auth_key_bytes) == 256:
            results[dc_id] = auth_key_bytes

    return results


async def _get_client() -> TelegramClient:
    """Create a TelegramClient using the auth key from the local Telegram database."""
    keys = _extract_auth_keys()
    if not keys:
        raise RuntimeError(
            "No Telegram auth keys found. Is Telegram installed and logged in?"
        )

    # Try each DC's persistent key until one is authorized
    for dc_id in (2, 1, 4, 5, 3):
        if dc_id not in keys:
            continue

        session = MemorySession()
        ip = _DC_ADDRESSES[dc_id]
        session.set_dc(dc_id, ip, 443)
        session.auth_key = AuthKey(keys[dc_id])

        client = TelegramClient(session, _API_ID, _API_HASH)
        client.session.set_dc(dc_id, ip, 443)
        await client.connect()

        if await client.is_user_authorized():
            return client

        await client.disconnect()

    raise RuntimeError(
        "Telegram auth keys found but none are authorized. "
        "Try logging out and back into Telegram.app."
    )


def is_available() -> bool:
    """Check if we can extract auth keys from the local Telegram database."""
    return bool(_extract_auth_keys())


def _resolve_entity_hint(peer_id: int) -> str | None:
    """Get a phone number or username for a peer from the local DB.

    Telethon can't resolve raw peer_ids without an access_hash cache,
    so we look up a phone or username to use as the entity identifier.
    """
    from .telegram_db import TelegramDB

    tdb = TelegramDB()
    peer = tdb._get_peer(peer_id)
    tdb.close()

    if peer.get("phone"):
        phone = peer["phone"]
        return phone if phone.startswith("+") else f"+{phone}"
    if peer.get("username"):
        return f"@{peer['username']}"
    return None


async def send_message(peer_id: int, text: str) -> str:
    """Send a message to a Telegram peer. Returns confirmation string."""
    hint = _resolve_entity_hint(peer_id)
    if not hint:
        raise RuntimeError(
            f"Cannot send to peer {peer_id}: no phone number or username found."
        )

    client = await _get_client()
    try:
        entity = await client.get_entity(hint)
        await client.send_message(entity, text)
        return "Message sent."
    finally:
        await client.disconnect()
