"""SQLite queries for Messages and Contacts databases."""

import re
import sqlite3
import sys
from pathlib import Path

MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
CONTACTS_DIR = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"

# CoreData epoch: seconds between 1970-01-01 and 2001-01-01
COREDATA_EPOCH = 978307200


def _ts_expr(col: str = "m.date") -> str:
    """SQL expression to convert Apple nanosecond timestamp to local datetime."""
    return f'datetime({col}/1000000000 + {COREDATA_EPOCH}, "unixepoch", "localtime")'


def _connect_messages() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(MESSAGES_DB))
        conn.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.OperationalError:
        print(
            "Error: Cannot open Messages database. "
            "Grant Full Disk Access to your terminal in "
            "System Settings > Privacy & Security > Full Disk Access.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn.row_factory = sqlite3.Row
    return conn


def extract_attributed_body(blob: bytes | None) -> str | None:
    """Extract text from an attributedBody NSKeyedArchiver blob."""
    if not blob:
        return None
    try:
        text = blob.split(b"NSString")[1]
        start = text.find(b"+")
        if start < 0:
            return None
        # Skip the length-prefix byte(s) after '+'
        raw = text[start + 2 :]
        # Find end marker — try several NSKeyedArchiver class names
        end = -1
        for marker in (b"NSDictionary", b"NSAttributes", b"NSMutableString", b"NSObject"):
            pos = raw.find(marker)
            if pos >= 0 and (end < 0 or pos < end):
                end = pos
        if end < 0:
            end = min(len(raw), 2000)
        content = raw[:end].decode("utf-8", errors="replace").strip()
        content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ufffc]", "", content)
        # Strip trailing binary garbage (replacement chars + stray bytes)
        content = re.sub(r"[\ufffd].*$", "", content).strip()
        return content or None
    except Exception:
        return None


def _has_digits(s: str) -> bool:
    return bool(re.search(r"\d", s))


def _find_chat_by_display_name(name: str) -> str | None:
    conn = _connect_messages()
    row = conn.execute(
        f"""
        SELECT c.chat_identifier
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON cmj.message_id = m.ROWID
        WHERE c.display_name LIKE ?
        GROUP BY c.ROWID
        ORDER BY MAX(m.date) DESC
        LIMIT 1
        """,
        (f"%{name}%",),
    ).fetchone()
    conn.close()
    return row["chat_identifier"] if row else None


def resolve_identifier(identifier: str) -> str:
    if _has_digits(identifier):
        return identifier
    # Try group chat display name first
    chat_id = _find_chat_by_display_name(identifier)
    if chat_id:
        return chat_id
    # Fall back to contact name resolution
    contacts = search_contacts(identifier)
    if not contacts:
        return identifier
    phones = contacts[0]["phones"]
    if not phones:
        return identifier
    return phones[0]


def search_contacts(name: str) -> list[dict]:
    """Search all AddressBook sources for contacts matching name.

    Returns deduplicated list: one entry per person with all phones and emails.
    """
    # Collect per-person data keyed by (first, last)
    people: dict[tuple, dict] = {}
    try:
        sources = list(CONTACTS_DIR.iterdir()) if CONTACTS_DIR.exists() else []
    except PermissionError:
        return []
    for source in sources:
        db_path = source / "AddressBook-v22.abcddb"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ZABCDRECORD.Z_PK, ZFIRSTNAME, ZLASTNAME, ZFULLNUMBER, ZADDRESS
                FROM ZABCDRECORD
                LEFT JOIN ZABCDPHONENUMBER ON ZABCDRECORD.Z_PK = ZABCDPHONENUMBER.ZOWNER
                LEFT JOIN ZABCDEMAILADDRESS ON ZABCDRECORD.Z_PK = ZABCDEMAILADDRESS.ZOWNER
                WHERE ZFIRSTNAME LIKE ? OR ZLASTNAME LIKE ?
                """,
                (f"%{name}%", f"%{name}%"),
            ).fetchall()
            for r in rows:
                key = (str(source), r["Z_PK"])
                if key not in people:
                    people[key] = {
                        "first": r["ZFIRSTNAME"],
                        "last": r["ZLASTNAME"],
                        "phones": set(),
                        "emails": set(),
                    }
                if r["ZFULLNUMBER"]:
                    people[key]["phones"].add(r["ZFULLNUMBER"])
                if r["ZADDRESS"]:
                    people[key]["emails"].add(r["ZADDRESS"])
            conn.close()
        except Exception:
            continue
    return [
        {
            "first": p["first"],
            "last": p["last"],
            "phones": sorted(p["phones"]),
            "emails": sorted(p["emails"]),
        }
        for p in people.values()
    ]


def _get_chat_participants(conn: sqlite3.Connection, chat_identifier: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT h.id
        FROM handle h
        JOIN chat_handle_join chj ON h.ROWID = chj.handle_id
        JOIN chat c ON chj.chat_id = c.ROWID
        WHERE c.chat_identifier = ?
        """,
        (chat_identifier,),
    ).fetchall()
    handles = [r["id"] for r in rows]
    cache = _build_contact_cache(handles)
    return [cache.get(h, h) for h in handles]


def recent_chats(limit: int = 20) -> list[dict]:
    """List recent chats with last message time."""
    conn = _connect_messages()
    rows = conn.execute(
        f"""
        SELECT c.chat_identifier, c.display_name,
               {_ts_expr('MAX(m.date)')} as last_msg
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON cmj.message_id = m.ROWID
        GROUP BY c.ROWID
        ORDER BY MAX(m.date) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_chats(identifier: str) -> list[dict]:
    """Find DM and group chats by phone digits, identifier, or contact name."""
    digits = re.sub(r"\D", "", identifier)
    if not digits:
        # No digits — treat as a name, resolve to phone numbers via contacts
        contacts = search_contacts(identifier)
        if contacts:
            all_results = []
            seen = set()
            for c in contacts:
                for phone in c["phones"]:
                    for r in find_chats(phone):
                        if r["ROWID"] not in seen:
                            seen.add(r["ROWID"])
                            all_results.append(r)
            return all_results
        digits = identifier
    conn = _connect_messages()
    # DM chats
    dm_rows = conn.execute(
        """
        SELECT c.chat_identifier, c.display_name, c.ROWID
        FROM chat c
        WHERE c.chat_identifier LIKE ?
        """,
        (f"%{digits}%",),
    ).fetchall()
    # Group chats containing this person
    group_rows = conn.execute(
        """
        SELECT c.chat_identifier, c.display_name, c.ROWID
        FROM chat c
        JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
        JOIN handle h ON chj.handle_id = h.ROWID
        WHERE h.id LIKE ?
        GROUP BY c.ROWID
        """,
        (f"%{digits}%",),
    ).fetchall()
    conn.close()
    seen = set()
    results = []
    for r in list(dm_rows) + list(group_rows):
        rid = r["ROWID"]
        if rid not in seen:
            seen.add(rid)
            results.append(dict(r))
    return results


def _resolve_handle_to_name(handle: str) -> str | None:
    digits = re.sub(r"\D", "", handle)
    if not digits:
        return None
    pattern = f"%{digits[-10:]}%"
    try:
        sources = list(CONTACTS_DIR.iterdir()) if CONTACTS_DIR.exists() else []
    except PermissionError:
        return None
    for source in sources:
        db_path = source / "AddressBook-v22.abcddb"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT ZFIRSTNAME, ZLASTNAME
                FROM ZABCDRECORD
                JOIN ZABCDPHONENUMBER ON ZABCDRECORD.Z_PK = ZABCDPHONENUMBER.ZOWNER
                WHERE REPLACE(REPLACE(REPLACE(REPLACE(ZFULLNUMBER, ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                LIMIT 1
                """,
                (pattern,),
            ).fetchone()
            conn.close()
            if row:
                first = row["ZFIRSTNAME"] or ""
                last = row["ZLASTNAME"] or ""
                name = f"{first} {last}".strip()
                if name:
                    return name
        except Exception:
            continue
    return None


def _build_contact_cache(handles: list[str]) -> dict[str, str]:
    cache = {}
    for handle in handles:
        name = _resolve_handle_to_name(handle)
        if name:
            cache[handle] = name
    return cache


def read_messages(chat_id: str, limit: int = 20) -> list[dict]:
    conn = _connect_messages()
    rows = conn.execute(
        f"""
        SELECT {_ts_expr()} as timestamp,
               m.is_from_me,
               m.text,
               m.attributedBody,
               h.id as handle,
               m.date_edited,
               m.associated_message_type,
               m.ROWID as message_id
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE c.chat_identifier = ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (chat_id, limit),
    ).fetchall()

    # Get attachments for these messages
    message_ids = [r["message_id"] for r in rows]
    attachment_map: dict[int, list[str]] = {}
    if message_ids:
        placeholders = ",".join("?" * len(message_ids))
        att_rows = conn.execute(
            f"""
            SELECT maj.message_id, a.filename, a.mime_type, a.transfer_name
            FROM message_attachment_join maj
            JOIN attachment a ON maj.attachment_id = a.ROWID
            WHERE maj.message_id IN ({placeholders})
            """,
            message_ids,
        ).fetchall()
        for a in att_rows:
            mid = a["message_id"]
            name = a["transfer_name"] or a["filename"] or "attachment"
            if "pluginPayloadAttachment" in name:
                continue
            mime = a["mime_type"] or ""
            label = mime.split("/")[0] if "/" in mime else "file"
            attachment_map.setdefault(mid, []).append(f"[{label}: {name}]")

    conn.close()

    TAPBACK_TYPES = {
        2000: "Loved", 2001: "Liked", 2002: "Disliked",
        2003: "Laughed at", 2004: "Emphasized", 2005: "Questioned",
    }

    # Resolve phone numbers to contact names
    handles = {r["handle"] for r in rows if r["handle"] and not r["is_from_me"]}
    name_cache = _build_contact_cache(list(handles))

    messages = []
    for r in rows:
        assoc_type = r["associated_message_type"] or 0

        # Tapback reactions
        if assoc_type in TAPBACK_TYPES:
            reaction = TAPBACK_TYPES[assoc_type]
            body = r["text"] or extract_attributed_body(r["attributedBody"]) or ""
            # Strip redundant reaction prefix from body (e.g. 'Loved "msg"' -> '"msg"')
            for prefix in ("Loved ", "Liked ", "Disliked ", "Laughed at ", "Emphasized ", "Questioned "):
                if body.startswith(prefix):
                    body = body[len(prefix):]
                    break
            content = f'[{reaction}] {body}'
        elif assoc_type >= 3000:
            continue  # reaction removal, skip
        else:
            content = r["text"] or extract_attributed_body(r["attributedBody"])
            attachments = attachment_map.get(r["message_id"], [])
            if attachments:
                att_str = " ".join(attachments)
                content = f"{content} {att_str}" if content else att_str
            if not content:
                continue

        if r["is_from_me"]:
            sender = "Me"
        else:
            handle = r["handle"] or "Unknown"
            sender = name_cache.get(handle, handle)
        edited = r["date_edited"] and r["date_edited"] > 0
        messages.append(
            {
                "timestamp": r["timestamp"],
                "sender": sender,
                "text": content,
                "edited": edited,
            }
        )
    return messages


def search_messages(query: str, limit: int = 20) -> list[dict]:
    """Search message content."""
    conn = _connect_messages()
    rows = conn.execute(
        f"""
        SELECT {_ts_expr()} as timestamp,
               c.chat_identifier,
               c.display_name,
               CASE WHEN m.is_from_me = 1 THEN 'Me' ELSE h.id END as sender,
               m.text
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        JOIN chat c ON cmj.chat_id = c.ROWID
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE m.text LIKE ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (f"%{query}%", limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
