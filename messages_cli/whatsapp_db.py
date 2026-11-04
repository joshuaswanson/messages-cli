"""Read the WhatsApp Desktop (native macOS app) databases."""

from __future__ import annotations

import functools
import re
import sqlite3
from pathlib import Path

from .models import BackendError, Chat, Message, SearchHit
from .utils import (
    COREDATA_EPOCH,
    choose_chat,
    format_phone,
    format_ts,
    like_pattern,
    looks_like_phone,
)

PLATFORM = "whatsapp"
_WA_CONTAINER = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
CHAT_DB = _WA_CONTAINER / "ChatStorage.sqlite"
CONTACTS_DB = _WA_CONTAINER / "ContactsV2.sqlite"
_MEDIA_BASE = _WA_CONTAINER / "Message"

_MEDIA_LABELS = {1: "image", 2: "video", 3: "audio", 5: "location", 8: "document", 15: "sticker"}
_IMAGE_TYPE = 1
_ACTIVE_SESSION = "ZREMOVED = 0 AND ZHIDDEN = 0"


def is_available() -> bool:
    return CHAT_DB.exists()


def _ts_expr(col: str = "m.ZMESSAGEDATE") -> str:
    """SQL expression converting a CoreData timestamp to unix seconds."""
    return f"CAST({col} + {COREDATA_EPOCH} AS INTEGER)"


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@functools.cache
def _chat_db() -> sqlite3.Connection:
    try:
        conn = _open_readonly(CHAT_DB)
        conn.execute("SELECT 1 FROM ZWACHATSESSION LIMIT 1")
    except sqlite3.Error as e:
        raise BackendError(
            "Cannot open the WhatsApp database. Grant Full Disk Access to your "
            "terminal in System Settings > Privacy & Security > Full Disk Access."
        ) from e
    return conn


@functools.cache
def _contacts() -> list[sqlite3.Row]:
    if not CONTACTS_DB.exists():
        return []
    try:
        conn = _open_readonly(CONTACTS_DB)
        rows = conn.execute(
            "SELECT ZWHATSAPPID, ZLID, ZFULLNAME, ZPHONENUMBER FROM ZWAADDRESSBOOKCONTACT"
        ).fetchall()
        conn.close()
        return rows
    except sqlite3.Error:
        return []


@functools.cache
def _jid_names() -> dict[str, str]:
    """Map phone JIDs and LIDs to contact names."""
    names: dict[str, str] = {}
    for r in _contacts():
        if not r["ZFULLNAME"]:
            continue
        for jid in (r["ZWHATSAPPID"], r["ZLID"]):
            if jid:
                names.setdefault(jid, r["ZFULLNAME"])
    return names


@functools.cache
def _jid_phones() -> dict[str, str]:
    return {r["ZWHATSAPPID"]: r["ZPHONENUMBER"] for r in _contacts() if r["ZWHATSAPPID"] and r["ZPHONENUMBER"]}


def _jid_phone(jid: str) -> str:
    phone = _jid_phones().get(jid, "")
    if not phone and jid.endswith("@s.whatsapp.net"):
        phone = "+" + jid.split("@")[0]
    return format_phone(phone) if phone else ""


def _member_name(member_jid: str | None, *fallback_names: str | None) -> str:
    """Address book name, then WhatsApp's own names for the member, then the phone number."""
    if not member_jid:
        return "Unknown"
    if name := _jid_names().get(member_jid):
        return name
    if name := next((n for n in fallback_names if n), None):
        return name
    if member_jid.endswith("@s.whatsapp.net"):
        return format_phone(member_jid.split("@")[0])
    return member_jid


def _chat_name(partner_name: str | None, jid: str) -> str:
    return partner_name or _jid_names().get(jid) or jid


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

_CHAT_COLUMNS = f"""
    c.Z_PK, c.ZCONTACTJID, c.ZPARTNERNAME,
    {_ts_expr('c.ZLASTMESSAGEDATE')} AS last_ts,
    (SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = c.Z_PK) AS message_count
"""


def _row_to_chat(r: sqlite3.Row) -> Chat:
    jid = r["ZCONTACTJID"] or ""
    return Chat(
        platform=PLATFORM,
        id=jid,
        name=_chat_name(r["ZPARTNERNAME"], jid),
        last_message=format_ts(r["last_ts"]),
        phone=_jid_phone(jid),
        is_group=jid.endswith("@g.us"),
        message_count=r["message_count"],
    )


def recent_chats(limit: int = 20) -> list[Chat]:
    rows = _chat_db().execute(
        f"""
        SELECT {_CHAT_COLUMNS} FROM ZWACHATSESSION c
        WHERE {_ACTIVE_SESSION}
        ORDER BY c.ZLASTMESSAGEDATE DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_chat(r) for r in rows]


def find_chats(query: str) -> list[Chat]:
    """Find chats by JID, phone number, chat/group name, or contact name."""
    query = query.strip()
    conditions = ["c.ZCONTACTJID = ?"]
    params: list = [query]
    if looks_like_phone(query):
        conditions.append("c.ZCONTACTJID LIKE ? ESCAPE '\\'")
        params.append(like_pattern(re.sub(r"\D", "", query)))
    else:
        conditions.append("c.ZPARTNERNAME LIKE ? ESCAPE '\\'")
        params.append(like_pattern(query))
        wanted = query.casefold()
        contact_jids = [
            r["ZWHATSAPPID"] for r in _contacts()
            if r["ZWHATSAPPID"] and r["ZFULLNAME"] and wanted in r["ZFULLNAME"].casefold()
        ]
        if contact_jids:
            conditions.append(f"c.ZCONTACTJID IN ({','.join('?' * len(contact_jids))})")
            params.extend(contact_jids)
    rows = _chat_db().execute(
        f"""
        SELECT {_CHAT_COLUMNS} FROM ZWACHATSESSION c
        WHERE {_ACTIVE_SESSION} AND ({' OR '.join(conditions)})
        ORDER BY c.ZLASTMESSAGEDATE DESC
        """,
        params,
    ).fetchall()
    return [_row_to_chat(r) for r in rows]


def resolve_chat(identifier: str, strict: bool = False) -> Chat | None:
    return choose_chat(identifier, find_chats(identifier), strict)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

_MESSAGE_SELECT = f"""
    SELECT {_ts_expr()} AS timestamp, m.Z_PK, m.ZMESSAGEDATE,
           m.ZISFROMME, m.ZTEXT, m.ZMESSAGETYPE,
           mi.ZMEDIALOCALPATH, mi.ZVCARDNAME, mi.ZTITLE,
           gm.ZMEMBERJID, gm.ZCONTACTNAME, gm.ZFIRSTNAME, p.ZPUSHNAME,
           c.ZCONTACTJID, c.ZPARTNERNAME
    FROM ZWAMESSAGE m
    JOIN ZWACHATSESSION c ON m.ZCHATSESSION = c.Z_PK
    LEFT JOIN ZWAMEDIAITEM mi ON mi.ZMESSAGE = m.Z_PK
    LEFT JOIN ZWAGROUPMEMBER gm ON m.ZGROUPMEMBER = gm.Z_PK
    LEFT JOIN ZWAPROFILEPUSHNAME p ON p.ZJID = gm.ZMEMBERJID
"""


def _row_to_message(r: sqlite3.Row) -> Message | None:
    text = r["ZTEXT"]
    if r["ZMEDIALOCALPATH"]:
        media_name = r["ZTITLE"] or r["ZVCARDNAME"] or r["ZMEDIALOCALPATH"]
        media_tag = f"[{_MEDIA_LABELS.get(r['ZMESSAGETYPE'], 'media')}: {media_name}]"
        text = f"{text} {media_tag}" if text else media_tag
    if not text:
        return None

    jid = r["ZCONTACTJID"] or ""
    if r["ZISFROMME"]:
        sender = "Me"
    elif jid.endswith("@g.us"):
        sender = _member_name(r["ZMEMBERJID"], r["ZCONTACTNAME"], r["ZPUSHNAME"], r["ZFIRSTNAME"])
    else:
        sender = _chat_name(r["ZPARTNERNAME"], jid)

    image_paths: list[str] = []
    if r["ZMESSAGETYPE"] == _IMAGE_TYPE and r["ZMEDIALOCALPATH"]:
        full_path = _MEDIA_BASE / r["ZMEDIALOCALPATH"]
        if full_path.exists():
            image_paths.append(str(full_path))

    return Message(
        id=str(r["Z_PK"]),
        timestamp=format_ts(r["timestamp"]),
        sender=sender,
        text=text,
        is_from_me=bool(r["ZISFROMME"]),
        image_paths=image_paths,
    )


def _to_messages(rows: list[sqlite3.Row]) -> list[Message]:
    return [m for r in rows if (m := _row_to_message(r)) is not None]


def read_messages(jid: str, limit: int = 20) -> list[Message]:
    """Most recent messages in a chat, newest first."""
    rows = _chat_db().execute(
        f"{_MESSAGE_SELECT} WHERE c.ZCONTACTJID = ? ORDER BY m.ZMESSAGEDATE DESC LIMIT ?",
        (jid, limit * 2 + 10),
    ).fetchall()
    return _to_messages(rows)[:limit]


def read_around(jid: str, message_id: str, count: int) -> list[Message]:
    """Up to `count` messages before and after a message, in chronological order."""
    conn = _chat_db()
    hit = conn.execute("SELECT ZMESSAGEDATE FROM ZWAMESSAGE WHERE Z_PK = ?", (int(message_id),)).fetchone()
    if hit is None:
        return []
    date, pk, window = hit[0], int(message_id), count * 2 + 10
    before = conn.execute(
        f"""{_MESSAGE_SELECT}
        WHERE c.ZCONTACTJID = ? AND (m.ZMESSAGEDATE < ? OR (m.ZMESSAGEDATE = ? AND m.Z_PK <= ?))
        ORDER BY m.ZMESSAGEDATE DESC, m.Z_PK DESC LIMIT ?""",
        (jid, date, date, pk, window),
    ).fetchall()
    after = conn.execute(
        f"""{_MESSAGE_SELECT}
        WHERE c.ZCONTACTJID = ? AND (m.ZMESSAGEDATE > ? OR (m.ZMESSAGEDATE = ? AND m.Z_PK > ?))
        ORDER BY m.ZMESSAGEDATE ASC, m.Z_PK ASC LIMIT ?""",
        (jid, date, date, pk, window),
    ).fetchall()
    older = _to_messages(before)[: count + 1]
    newer = _to_messages(after)[:count]
    return list(reversed(older)) + newer


def search_messages(query: str, limit: int = 20, jid: str | None = None) -> list[SearchHit]:
    """Search message text, newest first."""
    where = "m.ZTEXT LIKE ? ESCAPE '\\'"
    params: list = [like_pattern(query)]
    if jid:
        where += " AND c.ZCONTACTJID = ?"
        params.append(jid)
    rows = _chat_db().execute(
        f"{_MESSAGE_SELECT} WHERE {where} ORDER BY m.ZMESSAGEDATE DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    hits = []
    for r in rows:
        m = _row_to_message(r)
        if m is None:
            continue
        chat_jid = r["ZCONTACTJID"] or ""
        hits.append(SearchHit(
            **vars(m),
            platform=PLATFORM,
            chat_id=chat_jid,
            chat_name=_chat_name(r["ZPARTNERNAME"], chat_jid),
        ))
    return hits


def stats() -> dict:
    conn = _chat_db()
    messages = conn.execute("SELECT COUNT(*) FROM ZWAMESSAGE").fetchone()[0]
    chats = conn.execute(f"SELECT COUNT(*) FROM ZWACHATSESSION WHERE {_ACTIVE_SESSION}").fetchone()[0]
    return {"messages": messages, "chats": chats}
