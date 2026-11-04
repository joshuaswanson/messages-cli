"""Read the macOS Messages (iMessage/SMS/RCS) and Contacts databases."""

from __future__ import annotations

import functools
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import AmbiguousChatError, BackendError, Chat, Message, SearchHit
from .utils import (
    COREDATA_EPOCH,
    choose_chat,
    format_phone,
    format_ts,
    like_pattern,
    looks_like_phone,
    phone_key,
)

PLATFORM = "messages"
MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
CONTACTS_DIR = Path.home() / "Library" / "Application Support" / "AddressBook" / "Sources"

GROUP_CHAT_STYLE = 43

TAPBACK_TYPES = {
    2000: "Loved", 2001: "Liked", 2002: "Disliked",
    2003: "Laughed at", 2004: "Emphasized", 2005: "Questioned",
}
_TAPBACK_PREFIXES = tuple(f"{label} " for label in TAPBACK_TYPES.values())
_REACTION_TYPES_SQL = "m.associated_message_type BETWEEN 2000 AND 3999"


def _ts_expr(col: str = "m.date") -> str:
    """SQL expression converting an Apple nanosecond timestamp to unix seconds."""
    return f"CAST({col}/1000000000 + {COREDATA_EPOCH} AS INTEGER)"


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def is_available() -> bool:
    return MESSAGES_DB.exists()


@functools.cache
def _connect() -> sqlite3.Connection:
    try:
        conn = _open_readonly(MESSAGES_DB)
        conn.execute("SELECT 1 FROM message LIMIT 1")
    except sqlite3.Error as e:
        raise BackendError(
            "Cannot open the Messages database. Grant Full Disk Access to your "
            "terminal in System Settings > Privacy & Security > Full Disk Access."
        ) from e
    conn.create_function("message_body", 2, message_body, deterministic=True)
    return conn


# ---------------------------------------------------------------------------
# Message bodies
# ---------------------------------------------------------------------------

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
        end = -1
        for marker in (b"NSDictionary", b"NSAttributes", b"NSMutableString", b"NSObject"):
            pos = raw.find(marker)
            if pos >= 0 and (end < 0 or pos < end):
                end = pos
        if end < 0:
            end = min(len(raw), 2000)
        content = raw[:end].decode("utf-8", errors="replace").strip()
        content = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f￼]", "", content)
        # Trailing binary garbage decodes as replacement characters
        content = re.sub(r"[�].*$", "", content).strip()
        return content or None
    except Exception:
        return None


def message_body(text: str | None, attributed_body: bytes | None) -> str | None:
    """Message text from the plain column, or decoded from attributedBody."""
    return text or extract_attributed_body(attributed_body)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

@dataclass
class Contact:
    first: str
    last: str
    organization: str = ""
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip() or self.organization

    def to_dict(self) -> dict:
        return {"name": self.name, **asdict(self)}


def _load_source(db_path: Path) -> list[Contact]:
    conn = _open_readonly(db_path)
    try:
        records = conn.execute(
            "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"
        ).fetchall()
        phones = conn.execute(
            "SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER WHERE ZFULLNUMBER IS NOT NULL"
        ).fetchall()
        emails = conn.execute(
            "SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS WHERE ZADDRESS IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    by_pk = {
        r["Z_PK"]: Contact(r["ZFIRSTNAME"] or "", r["ZLASTNAME"] or "", r["ZORGANIZATION"] or "")
        for r in records
    }
    for owner, number in phones:
        if owner in by_pk and number not in by_pk[owner].phones:
            by_pk[owner].phones.append(number)
    for owner, address in emails:
        if owner in by_pk and address not in by_pk[owner].emails:
            by_pk[owner].emails.append(address)
    return [c for c in by_pk.values() if c.name]


@functools.cache
def _all_contacts() -> list[Contact]:
    try:
        sources = sorted(CONTACTS_DIR.glob("*/AddressBook-v22.abcddb"))
    except PermissionError:
        return []
    contacts = []
    for db_path in sources:
        try:
            contacts.extend(_load_source(db_path))
        except sqlite3.Error:
            continue
    return contacts


@functools.cache
def _handle_names() -> dict[str, str]:
    """Map phone keys and lowercase emails to contact names."""
    names: dict[str, str] = {}
    for c in _all_contacts():
        for phone in c.phones:
            key = phone_key(phone)
            if key:
                names.setdefault(key, c.name)
        for email in c.emails:
            names.setdefault(email.lower(), c.name)
    return names


def contact_name(handle: str | None) -> str | None:
    if not handle:
        return None
    names = _handle_names()
    if "@" in handle:
        return names.get(handle.lower())
    key = phone_key(handle)
    return names.get(key) if key else None


def _handle_display(handle: str | None) -> str:
    if not handle:
        return "Unknown"
    return contact_name(handle) or format_phone(handle)


def search_contacts(query: str) -> list[Contact]:
    """Find contacts whose name contains the query (case-insensitive)."""
    wanted = query.strip().casefold()
    return [
        c for c in _all_contacts()
        if wanted in c.name.casefold() or wanted in c.organization.casefold()
    ]


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

_CHAT_QUERY = f"""
    SELECT c.chat_identifier, c.guid, c.display_name, c.style,
           {_ts_expr('MAX(m.date)')} AS last_ts,
           COUNT(m.ROWID) AS message_count
    FROM chat c
    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
    JOIN message m ON cmj.message_id = m.ROWID
    WHERE {{where}}
    GROUP BY c.chat_identifier
    ORDER BY MAX(m.date) DESC
    {{limit}}
"""


def _participants(identifiers: list[str]) -> dict[str, list[str]]:
    if not identifiers:
        return {}
    placeholders = ",".join("?" * len(identifiers))
    rows = _connect().execute(
        f"""
        SELECT DISTINCT c.chat_identifier, h.id
        FROM chat c
        JOIN chat_handle_join chj ON c.ROWID = chj.chat_id
        JOIN handle h ON chj.handle_id = h.ROWID
        WHERE c.chat_identifier IN ({placeholders})
        """,
        identifiers,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for chat_identifier, handle in rows:
        result.setdefault(chat_identifier, []).append(handle)
    return result


def _group_name(handles: list[str]) -> str:
    names = [_handle_display(h) for h in handles[:3]]
    name = ", ".join(names)
    if len(handles) > 3:
        name += f" +{len(handles) - 3}"
    return name


def _rows_to_chats(rows: list[sqlite3.Row]) -> list[Chat]:
    groups = [r["chat_identifier"] for r in rows if r["style"] == GROUP_CHAT_STYLE and not r["display_name"]]
    participants = _participants(groups)
    chats = []
    for r in rows:
        cid = r["chat_identifier"]
        is_group = r["style"] == GROUP_CHAT_STYLE
        if r["display_name"]:
            name = r["display_name"]
        elif is_group:
            name = _group_name(participants.get(cid, [])) or cid
        else:
            name = _handle_display(cid)
        chats.append(Chat(
            platform=PLATFORM,
            id=cid,
            name=name,
            last_message=format_ts(r["last_ts"]),
            phone=format_phone(cid) if not is_group and looks_like_phone(cid) else "",
            is_group=is_group,
            message_count=r["message_count"],
        ))
    return chats


def _query_chats(where: str, params: list, limit: int | None = None) -> list[sqlite3.Row]:
    sql = _CHAT_QUERY.format(where=where, limit="LIMIT ?" if limit else "")
    return _connect().execute(sql, params + ([limit] if limit else [])).fetchall()


def recent_chats(limit: int = 20) -> list[Chat]:
    return _rows_to_chats(_query_chats("1", [], limit))


_HAS_HANDLE = (
    "c.ROWID IN (SELECT chj.chat_id FROM chat_handle_join chj "
    "JOIN handle h ON chj.handle_id = h.ROWID WHERE h.id LIKE ? ESCAPE '\\')"
)


def find_chats(identifier: str) -> list[Chat]:
    """Find DM and group chats by chat ID, phone, email, group name, or contact name."""
    identifier = identifier.strip()
    conditions = ["c.chat_identifier = ?"]
    params: list = [identifier]

    def add_handle(pattern: str) -> None:
        conditions.append("c.chat_identifier LIKE ? ESCAPE '\\'")
        conditions.append(_HAS_HANDLE)
        params.extend([pattern, pattern])

    if looks_like_phone(identifier):
        add_handle(like_pattern(phone_key(identifier)))
    elif "@" in identifier and " " not in identifier:
        add_handle(like_pattern(identifier))
    else:
        conditions.append("c.display_name LIKE ? ESCAPE '\\'")
        params.append(like_pattern(identifier))
        for contact in search_contacts(identifier):
            for phone in contact.phones:
                if key := phone_key(phone):
                    add_handle(like_pattern(key))
            for email in contact.emails:
                add_handle(like_pattern(email))

    return _rows_to_chats(_query_chats(" OR ".join(conditions), params))


def resolve_chat(identifier: str, strict: bool = False) -> Chat | None:
    return choose_chat(identifier, find_chats(identifier), strict)


def chat_guid(chat_identifier: str) -> str | None:
    """GUID of the most recently used chat row for this identifier (used to send)."""
    row = _connect().execute(
        """
        SELECT c.guid
        FROM chat c
        LEFT JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        LEFT JOIN message m ON cmj.message_id = m.ROWID
        WHERE c.chat_identifier = ?
        GROUP BY c.ROWID
        ORDER BY MAX(m.date) DESC
        LIMIT 1
        """,
        (chat_identifier,),
    ).fetchone()
    return row["guid"] if row else None


def contact_handle(identifier: str) -> tuple[str, str] | None:
    """(handle, display name) for a contact that has no existing chat."""
    if looks_like_phone(identifier) or ("@" in identifier and " " not in identifier):
        return identifier.strip(), _handle_display(identifier.strip())
    matches = [c for c in search_contacts(identifier) if c.phones or c.emails]
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(sorted({c.name for c in matches})[:5])
        raise AmbiguousChatError(f'Multiple contacts match "{identifier}" ({names}).')
    contact = matches[0]
    return (contact.phones or contact.emails)[0], contact.name


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

_MESSAGE_COLUMNS = f"""
    {_ts_expr()} AS timestamp, m.ROWID AS message_id, m.date,
    m.is_from_me, m.text, m.attributedBody, h.id AS handle,
    m.date_edited, m.associated_message_type
"""

_MESSAGE_FROM = """
    FROM message m
    JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
    JOIN chat c ON cmj.chat_id = c.ROWID
    LEFT JOIN handle h ON m.handle_id = h.ROWID
"""


def _attachments(message_ids: list[int]) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    tags: dict[int, list[str]] = {}
    images: dict[int, list[str]] = {}
    if not message_ids:
        return tags, images
    placeholders = ",".join("?" * len(message_ids))
    rows = _connect().execute(
        f"""
        SELECT maj.message_id, a.filename, a.mime_type, a.transfer_name
        FROM message_attachment_join maj
        JOIN attachment a ON maj.attachment_id = a.ROWID
        WHERE maj.message_id IN ({placeholders})
        """,
        message_ids,
    ).fetchall()
    for a in rows:
        mid = a["message_id"]
        name = a["transfer_name"] or a["filename"] or "attachment"
        if "pluginPayloadAttachment" in name:
            continue
        mime = a["mime_type"] or ""
        label = mime.split("/")[0] if "/" in mime else "file"
        tags.setdefault(mid, []).append(f"[{label}: {name}]")
        if mime.startswith("image/") and a["filename"]:
            resolved = Path(a["filename"]).expanduser()
            if resolved.exists():
                images.setdefault(mid, []).append(str(resolved))
    return tags, images


def _rows_to_messages(rows: list[sqlite3.Row]) -> list[Message]:
    tags, images = _attachments([r["message_id"] for r in rows])
    messages = []
    for r in rows:
        assoc_type = r["associated_message_type"] or 0
        body = message_body(r["text"], r["attributedBody"])
        if assoc_type in TAPBACK_TYPES:
            quoted = body or ""
            for prefix in _TAPBACK_PREFIXES:
                if quoted.startswith(prefix):
                    quoted = quoted[len(prefix):]
                    break
            content = f"[{TAPBACK_TYPES[assoc_type]}] {quoted}"
        elif assoc_type >= 3000:
            continue  # reaction removal
        else:
            content = " ".join(filter(None, [body, *tags.get(r["message_id"], [])]))
            if not content:
                continue
        is_from_me = bool(r["is_from_me"])
        messages.append(Message(
            id=str(r["message_id"]),
            timestamp=format_ts(r["timestamp"]),
            sender="Me" if is_from_me else _handle_display(r["handle"]),
            text=content,
            is_from_me=is_from_me,
            edited=bool(r["date_edited"]),
            image_paths=images.get(r["message_id"], []),
        ))
    return messages


def read_messages(chat_identifier: str, limit: int = 20) -> list[Message]:
    """Most recent messages in a chat, newest first."""
    rows = _connect().execute(
        f"""
        SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM}
        WHERE c.chat_identifier = ?
        ORDER BY m.date DESC
        LIMIT ?
        """,
        (chat_identifier, limit * 2 + 10),
    ).fetchall()
    return _rows_to_messages(rows)[:limit]


def read_around(chat_identifier: str, message_id: str, count: int) -> list[Message]:
    """Up to `count` messages before and after a message, in chronological order."""
    conn = _connect()
    hit = conn.execute("SELECT date FROM message WHERE ROWID = ?", (int(message_id),)).fetchone()
    if hit is None:
        return []
    # Fetch extra rows because reaction removals and empty rows are dropped.
    window = count * 2 + 10
    before = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM}
        WHERE c.chat_identifier = ? AND (m.date < ? OR (m.date = ? AND m.ROWID <= ?))
        ORDER BY m.date DESC, m.ROWID DESC
        LIMIT ?
        """,
        (chat_identifier, hit["date"], hit["date"], int(message_id), window),
    ).fetchall()
    after = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS} {_MESSAGE_FROM}
        WHERE c.chat_identifier = ? AND (m.date > ? OR (m.date = ? AND m.ROWID > ?))
        ORDER BY m.date ASC, m.ROWID ASC
        LIMIT ?
        """,
        (chat_identifier, hit["date"], hit["date"], int(message_id), window),
    ).fetchall()
    older = _rows_to_messages(before)[: count + 1]
    newer = _rows_to_messages(after)[:count]
    return list(reversed(older)) + newer


def search_messages(query: str, limit: int = 20, chat_identifier: str | None = None) -> list[SearchHit]:
    """Search message text (including attributedBody), newest first."""
    conn = _connect()
    where = f"message_body(m.text, m.attributedBody) LIKE ? ESCAPE '\\' AND NOT ({_REACTION_TYPES_SQL})"
    params: list = [like_pattern(query)]
    if chat_identifier:
        where += " AND c.chat_identifier = ?"
        params.append(chat_identifier)
    rows = conn.execute(
        f"""
        SELECT {_MESSAGE_COLUMNS}, c.chat_identifier {_MESSAGE_FROM}
        WHERE {where}
        ORDER BY m.date DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    chat_ids: list[str] = list({r["chat_identifier"] for r in rows})
    chat_names: dict[str, str] = {}
    if chat_ids:
        placeholders = ",".join("?" * len(chat_ids))
        for chat in _rows_to_chats(_query_chats(f"c.chat_identifier IN ({placeholders})", chat_ids)):
            chat_names[chat.id] = chat.name
    hits = []
    for r in rows:
        is_from_me = bool(r["is_from_me"])
        hits.append(SearchHit(
            id=str(r["message_id"]),
            timestamp=format_ts(r["timestamp"]),
            sender="Me" if is_from_me else _handle_display(r["handle"]),
            text=message_body(r["text"], r["attributedBody"]) or "",
            is_from_me=is_from_me,
            edited=bool(r["date_edited"]),
            platform=PLATFORM,
            chat_id=r["chat_identifier"],
            chat_name=chat_names.get(r["chat_identifier"]) or r["chat_identifier"],
        ))
    return hits


def stats() -> dict:
    conn = _connect()
    messages = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    chats = conn.execute("SELECT COUNT(DISTINCT chat_identifier) FROM chat").fetchone()[0]
    return {"messages": messages, "chats": chats}
