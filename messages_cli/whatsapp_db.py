"""SQLite queries for WhatsApp Desktop database (macOS native app)."""

import re
import sqlite3
import sys
from pathlib import Path

from .utils import format_ts

# WhatsApp Desktop stores data in Group Containers
_WA_CONTAINER = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
CHAT_DB = _WA_CONTAINER / "ChatStorage.sqlite"
CONTACTS_DB = _WA_CONTAINER / "ContactsV2.sqlite"
_MEDIA_BASE = _WA_CONTAINER / "Message"

# CoreData epoch: seconds between 1970-01-01 and 2001-01-01
COREDATA_EPOCH = 978307200


def is_available() -> bool:
    """Check if WhatsApp Desktop database exists."""
    return CHAT_DB.exists()


def _connect_chat_db() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(CHAT_DB))
        conn.execute("SELECT 1 FROM ZWACHATSESSION LIMIT 1")
    except sqlite3.OperationalError:
        print(
            "Error: Cannot open WhatsApp database. "
            "Grant Full Disk Access to your terminal in "
            "System Settings > Privacy & Security > Full Disk Access.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_contacts_db() -> sqlite3.Connection | None:
    if not CONTACTS_DB.exists():
        return None
    try:
        conn = sqlite3.connect(str(CONTACTS_DB))
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def _ts_expr(col: str = "m.ZMESSAGEDATE") -> str:
    """SQL expression to convert WhatsApp CoreData timestamp to unix seconds."""
    return f'CAST({col} + {COREDATA_EPOCH} AS INTEGER)'


# ---------------------------------------------------------------------------
# Contact resolution
# ---------------------------------------------------------------------------

def _build_contact_cache() -> dict[str, str]:
    """Build a mapping from WhatsApp JID to display name using ContactsV2."""
    conn = _connect_contacts_db()
    if not conn:
        return {}
    try:
        rows = conn.execute(
            "SELECT ZWHATSAPPID, ZFULLNAME FROM ZWAADDRESSBOOKCONTACT "
            "WHERE ZWHATSAPPID IS NOT NULL AND ZFULLNAME IS NOT NULL"
        ).fetchall()
        cache = {r["ZWHATSAPPID"]: r["ZFULLNAME"] for r in rows}
        conn.close()
        return cache
    except Exception:
        return {}


def _build_jid_to_phone() -> dict[str, str]:
    """Build a mapping from WhatsApp JID to phone number."""
    conn = _connect_contacts_db()
    if not conn:
        return {}
    try:
        rows = conn.execute(
            "SELECT ZWHATSAPPID, ZPHONENUMBER FROM ZWAADDRESSBOOKCONTACT "
            "WHERE ZWHATSAPPID IS NOT NULL AND ZPHONENUMBER IS NOT NULL"
        ).fetchall()
        result = {r["ZWHATSAPPID"]: r["ZPHONENUMBER"] for r in rows}
        conn.close()
        return result
    except Exception:
        return {}


def _resolve_sender(
    from_jid: str | None,
    push_name: str | None,
    is_from_me: bool,
    contact_cache: dict[str, str],
) -> str:
    """Resolve a sender JID to a display name."""
    if is_from_me:
        return "Me"
    if not from_jid:
        return push_name or "Unknown"
    # Try contact name first
    # from_jid in groups uses LID format (NUMBER@lid)
    # Strip the @lid or @s.whatsapp.net suffix for lookup
    jid_base = from_jid.split("@")[0] if "@" in from_jid else from_jid
    # Try direct JID match
    if from_jid in contact_cache:
        return contact_cache[from_jid]
    # Try with @s.whatsapp.net suffix
    phone_jid = f"{jid_base}@s.whatsapp.net"
    if phone_jid in contact_cache:
        return contact_cache[phone_jid]
    # Fall back to push name or JID
    return push_name or from_jid


def _resolve_chat_name(
    row: dict,
    contact_cache: dict[str, str],
) -> str:
    """Resolve a chat session row to a display name."""
    if row["ZPARTNERNAME"]:
        return row["ZPARTNERNAME"]
    jid = row["ZCONTACTJID"] or ""
    if jid in contact_cache:
        return contact_cache[jid]
    return jid


# ---------------------------------------------------------------------------
# LID to phone resolution (for group message senders)
# ---------------------------------------------------------------------------

def _build_lid_to_name(conn: sqlite3.Connection) -> dict[str, str]:
    """Build mapping from LID JIDs to contact names using group member table."""
    rows = conn.execute(
        "SELECT ZMEMBERJID, ZCONTACTNAME FROM ZWAGROUPMEMBER "
        "WHERE ZMEMBERJID IS NOT NULL AND ZCONTACTNAME IS NOT NULL AND ZCONTACTNAME != ''"
    ).fetchall()
    return {r["ZMEMBERJID"]: r["ZCONTACTNAME"] for r in rows}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recent_chats(limit: int = 20) -> list[dict]:
    """List recent chats sorted by last message date."""
    conn = _connect_chat_db()
    contact_cache = _build_contact_cache()
    jid_to_phone = _build_jid_to_phone()
    rows = conn.execute(
        f"""
        SELECT c.ZCONTACTJID, c.ZPARTNERNAME, c.ZLASTMESSAGETEXT,
               {_ts_expr('c.ZLASTMESSAGEDATE')} as last_msg,
               c.ZSESSIONTYPE,
               (SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = c.Z_PK) as message_count
        FROM ZWACHATSESSION c
        WHERE c.ZREMOVED = 0 AND c.ZHIDDEN = 0
        ORDER BY c.ZLASTMESSAGEDATE DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        rd = dict(r)
        jid = rd["ZCONTACTJID"] or ""
        name = _resolve_chat_name(rd, contact_cache)
        phone = jid_to_phone.get(jid, "")
        if not phone and "@s.whatsapp.net" in jid:
            phone = "+" + jid.split("@")[0]
        results.append({
            "name": name,
            "jid": jid,
            "last_message": format_ts(rd["last_msg"]),
            "phone": phone,
            "is_group": jid.endswith("@g.us"),
            "message_count": rd.get("message_count", 0),
        })
    return results


def find_chats(query: str) -> list[dict]:
    """Find chats by name, phone number, or JID."""
    conn = _connect_chat_db()
    contact_cache = _build_contact_cache()
    jid_to_phone = _build_jid_to_phone()
    digits = re.sub(r"\D", "", query)

    # Search by partner name and group subject
    rows = conn.execute(
        f"""
        SELECT ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE,
               {_ts_expr('ZLASTMESSAGEDATE')} as last_msg
        FROM ZWACHATSESSION
        WHERE ZREMOVED = 0 AND ZHIDDEN = 0
          AND (ZPARTNERNAME LIKE ? OR ZCONTACTJID LIKE ?)
        ORDER BY ZLASTMESSAGEDATE DESC
        """,
        (f"%{query}%", f"%{digits}%" if digits else f"%{query}%"),
    ).fetchall()
    conn.close()

    # Also search contacts DB
    extra_jids = set()
    contacts_conn = _connect_contacts_db()
    if contacts_conn:
        contact_rows = contacts_conn.execute(
            "SELECT ZWHATSAPPID FROM ZWAADDRESSBOOKCONTACT "
            "WHERE ZFULLNAME LIKE ? OR ZPHONENUMBER LIKE ?",
            (f"%{query}%", f"%{digits}%" if digits else f"%{query}%"),
        ).fetchall()
        contacts_conn.close()
        extra_jids = {r["ZWHATSAPPID"] for r in contact_rows if r["ZWHATSAPPID"]}

    seen = set()
    results = []
    for r in rows:
        rd = dict(r)
        jid = rd["ZCONTACTJID"] or ""
        if jid in seen:
            continue
        seen.add(jid)
        extra_jids.discard(jid)
        name = _resolve_chat_name(rd, contact_cache)
        phone = jid_to_phone.get(jid, "")
        if not phone and "@s.whatsapp.net" in jid:
            phone = "+" + jid.split("@")[0]
        results.append({
            "name": name,
            "jid": jid,
            "phone": phone,
            "is_group": jid.endswith("@g.us"),
        })

    # Add contacts that matched but weren't in chat results
    if extra_jids:
        conn2 = _connect_chat_db()
        for jid in extra_jids:
            if jid in seen:
                continue
            row = conn2.execute(
                f"""
                SELECT ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE,
                       {_ts_expr('ZLASTMESSAGEDATE')} as last_msg
                FROM ZWACHATSESSION
                WHERE ZCONTACTJID = ? AND ZREMOVED = 0
                LIMIT 1
                """,
                (jid,),
            ).fetchone()
            if row:
                seen.add(jid)
                rd = dict(row)
                name = _resolve_chat_name(rd, contact_cache)
                phone = jid_to_phone.get(jid, "")
                if not phone and "@s.whatsapp.net" in jid:
                    phone = "+" + jid.split("@")[0]
                results.append({
                    "name": name,
                    "jid": jid,
                    "phone": phone,
                    "is_group": jid.endswith("@g.us"),
                })
        conn2.close()

    return results


def resolve_identifier(identifier: str) -> str | None:
    """Resolve a name, phone, or JID to a WhatsApp JID."""
    # Already a JID
    if "@" in identifier:
        return identifier
    # Try by phone digits
    digits = re.sub(r"\D", "", identifier)
    if digits:
        conn = _connect_chat_db()
        row = conn.execute(
            """
            SELECT ZCONTACTJID FROM ZWACHATSESSION
            WHERE ZCONTACTJID LIKE ? AND ZREMOVED = 0
            ORDER BY ZLASTMESSAGEDATE DESC LIMIT 1
            """,
            (f"%{digits}%",),
        ).fetchone()
        conn.close()
        if row:
            return row["ZCONTACTJID"]
    # Try by name
    chats = find_chats(identifier)
    if chats:
        return chats[0]["jid"]
    return None


def read_messages(jid: str, limit: int = 20) -> list[dict]:
    """Read messages from a chat by JID."""
    conn = _connect_chat_db()
    contact_cache = _build_contact_cache()
    lid_cache = _build_lid_to_name(conn)

    # Get chat session Z_PK and partner name
    session = conn.execute(
        "SELECT Z_PK, ZPARTNERNAME, ZCONTACTJID FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
        (jid,),
    ).fetchone()
    if not session:
        conn.close()
        return []

    session_pk = session["Z_PK"]
    partner_name = session["ZPARTNERNAME"]
    is_group = jid.endswith("@g.us")

    rows = conn.execute(
        f"""
        SELECT {_ts_expr()} as timestamp,
               m.ZISFROMME, m.ZFROMJID, m.ZPUSHNAME, m.ZTEXT,
               m.ZMESSAGETYPE, m.ZMEDIAITEM,
               mi.ZMEDIALOCALPATH, mi.ZVCARDNAME, mi.ZTITLE
        FROM ZWAMESSAGE m
        LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
        WHERE m.ZCHATSESSION = ?
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT ?
        """,
        (session_pk, limit),
    ).fetchall()
    conn.close()

    messages = []
    for r in rows:
        text = r["ZTEXT"]
        # Add media info
        if r["ZMEDIALOCALPATH"]:
            media_name = r["ZTITLE"] or r["ZVCARDNAME"] or r["ZMEDIALOCALPATH"]
            media_label = _media_type_label(r["ZMESSAGETYPE"])
            media_tag = f"[{media_label}: {media_name}]"
            text = f"{text} {media_tag}" if text else media_tag
        if not text:
            continue

        # Resolve sender
        if r["ZISFROMME"]:
            sender = "Me"
        elif not is_group:
            # In DMs, the other person is always the partner
            sender = partner_name or contact_cache.get(jid, jid)
        else:
            # In groups, try LID cache, then group member names, then contact cache
            from_jid = r["ZFROMJID"]
            if from_jid and from_jid in lid_cache:
                sender = lid_cache[from_jid]
            else:
                push_name = _decode_push_name(r["ZPUSHNAME"])
                sender = _resolve_sender(from_jid, push_name, False, contact_cache)

        # Resolve image path for image messages (type 1)
        image_paths: list[str] = []
        if r["ZMESSAGETYPE"] == 1 and r["ZMEDIALOCALPATH"]:
            full_path = _MEDIA_BASE / r["ZMEDIALOCALPATH"]
            if full_path.exists():
                image_paths.append(str(full_path))

        messages.append({
            "timestamp": format_ts(r["timestamp"]),
            "sender": sender,
            "text": text,
            "edited": False,
            "is_from_me": bool(r["ZISFROMME"]),
            "image_paths": image_paths,
        })
    return messages


def search_messages(query: str, limit: int = 20) -> list[dict]:
    """Search message content across all WhatsApp chats."""
    conn = _connect_chat_db()
    contact_cache = _build_contact_cache()
    lid_cache = _build_lid_to_name(conn)
    rows = conn.execute(
        f"""
        SELECT {_ts_expr()} as timestamp,
               c.ZCONTACTJID, c.ZPARTNERNAME,
               m.ZISFROMME, m.ZFROMJID, m.ZPUSHNAME, m.ZTEXT
        FROM ZWAMESSAGE m
        JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
        WHERE m.ZTEXT LIKE ?
        ORDER BY m.ZMESSAGEDATE DESC
        LIMIT ?
        """,
        (f"%{query}%", limit),
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        rd = dict(r)
        jid = rd["ZCONTACTJID"] or ""
        chat_name = rd["ZPARTNERNAME"] or contact_cache.get(jid, jid)
        is_group = jid.endswith("@g.us")

        if rd["ZISFROMME"]:
            sender = "Me"
        elif not is_group:
            sender = chat_name
        else:
            from_jid = rd["ZFROMJID"]
            if from_jid and from_jid in lid_cache:
                sender = lid_cache[from_jid]
            else:
                push_name = _decode_push_name(rd["ZPUSHNAME"])
                sender = _resolve_sender(from_jid, push_name, False, contact_cache)

        results.append({
            "timestamp": format_ts(rd["timestamp"]),
            "chat_name": chat_name,
            "sender": sender,
            "text": rd["ZTEXT"],
            "is_from_me": bool(rd["ZISFROMME"]),
        })
    return results


def stats() -> dict:
    """Return message and chat counts."""
    conn = _connect_chat_db()
    msg_count = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
    chat_count = conn.execute(
        "SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZREMOVED = 0 AND ZHIDDEN = 0"
    ).fetchone()[0]
    conn.close()
    return {"messages": msg_count, "chats": chat_count}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _media_type_label(msg_type: int | None) -> str:
    """Convert WhatsApp message type to a media label."""
    labels = {
        1: "image",
        2: "video",
        3: "audio",
        5: "location",
        8: "document",
        15: "sticker",
    }
    return labels.get(msg_type, "media")


def _decode_push_name(raw: str | None) -> str | None:
    """Decode push name, which may be protobuf-encoded binary."""
    if not raw:
        return None
    # If it's printable ASCII/Unicode, return as-is
    if raw.isprintable() and len(raw) < 100:
        return raw
    # Otherwise it's likely protobuf junk, skip it
    return None
