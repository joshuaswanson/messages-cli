"""Platform-agnostic adapter layer for message databases.

Each platform implements BackendAdapter. Dispatch functions query all available
backends (or a specific one) through the registry.
"""

from __future__ import annotations

import asyncio
import atexit
from abc import ABC, abstractmethod
from pathlib import Path

from . import db, send, telegram_send, whatsapp_db, whatsapp_send, messenger_api
from .utils import format_phone


# ---------------------------------------------------------------------------
# Backend adapter base
# ---------------------------------------------------------------------------

class BackendAdapter(ABC):
    name: str
    display_name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def recent_chats(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def find_chats(self, query: str) -> list[dict]: ...

    @abstractmethod
    def has_chat(self, identifier: str) -> bool: ...

    @abstractmethod
    def read_messages(self, identifier: str, limit: int) -> list[dict]: ...

    @abstractmethod
    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[dict]: ...

    @abstractmethod
    def stats(self) -> dict: ...

    def can_send(self) -> bool:
        return False

    def _require_send(self) -> None:
        if not self.can_send():
            raise SystemExit(f"{self.display_name} send is not available.")

    def send_message(self, identifier: str, text: str) -> str:
        raise NotImplementedError

    def resolve_display_name(self, identifier: str) -> str:
        return identifier


# ---------------------------------------------------------------------------
# iMessage
# ---------------------------------------------------------------------------

class IMessageAdapter(BackendAdapter):
    name = "messages"
    display_name = "Messages"

    def is_available(self) -> bool:
        return db.MESSAGES_DB.exists()

    def _display_name_for_chat(self, row: dict) -> str:
        cid = row["chat_identifier"]
        if row["display_name"]:
            return row["display_name"]
        if cid.startswith("chat"):
            conn = db._connect_messages()
            participants = db._get_chat_participants(conn, cid)
            conn.close()
            if participants:
                name = ", ".join(format_phone(p) for p in participants[:3])
                if len(participants) > 3:
                    name += f" +{len(participants) - 3}"
                return name
            return format_phone(cid)
        cache = db._build_contact_cache([cid])
        return cache.get(cid, "") or format_phone(cid)

    def recent_chats(self, limit: int) -> list[dict]:
        return [{
            "name": self._display_name_for_chat(r),
            "id": r["chat_identifier"],
            "platform": self.name,
            "last_message": r["last_message"],
            "phone": format_phone(r["chat_identifier"]) if not r["chat_identifier"].startswith("chat") else "",
            "username": "",
            "message_count": r.get("message_count"),
        } for r in db.recent_chats(limit)]

    def find_chats(self, query: str) -> list[dict]:
        return [{
            "name": self._display_name_for_chat(r),
            "id": r["chat_identifier"],
            "platform": self.name,
            "phone": format_phone(r["chat_identifier"]) if not r["chat_identifier"].startswith("chat") else "",
            "username": "",
        } for r in db.find_chats(query)]

    def has_chat(self, identifier: str) -> bool:
        if db.find_chats(identifier):
            return True
        return db.resolve_identifier(identifier) != identifier

    def read_messages(self, identifier: str, limit: int) -> list[dict]:
        chats = db.find_chats(identifier)
        if chats:
            return db.read_messages(chats[0]["chat_identifier"], limit)
        return db.read_messages(db.resolve_identifier(identifier), limit)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[dict]:
        resolved = None
        if chat_id:
            chats = db.find_chats(chat_id)
            resolved = chats[0]["chat_identifier"] if chats else db.resolve_identifier(chat_id)
        return [{
            "timestamp": r["timestamp"],
            "chat_name": r["display_name"] or format_phone(r["chat_identifier"]),
            "sender": r["sender"] if r["sender"] == "Me" else format_phone(r["sender"]),
            "text": r["text"],
            "is_from_me": r.get("is_from_me", r["sender"] == "Me"),
            "platform": self.name,
        } for r in db.search_messages(query, limit, chat_id=resolved)]

    def stats(self) -> dict:
        conn = db._connect_messages()
        msg_count = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        chat_count = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
        conn.close()
        return {"platform": self.name, "messages": msg_count, "chats": chat_count}

    def can_send(self) -> bool:
        return True

    def send_message(self, identifier: str, text: str) -> str:
        return send.send_message(db.resolve_identifier(identifier), text)

    def resolve_display_name(self, identifier: str) -> str:
        return format_phone(db.resolve_identifier(identifier))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

_telegram_db_instance = None


def _get_telegram_db():
    global _telegram_db_instance
    if _telegram_db_instance is None:
        from .telegram_db import TelegramDB
        _telegram_db_instance = TelegramDB()
        atexit.register(_telegram_db_instance.close)
    return _telegram_db_instance


class TelegramAdapter(BackendAdapter):
    name = "telegram"
    display_name = "Telegram"

    _TG_CONTAINER = Path.home() / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"

    def is_available(self) -> bool:
        for variant in ("appstore", ""):
            base = self._TG_CONTAINER / variant if variant else self._TG_CONTAINER
            for account_dir in base.glob("account-*"):
                if (account_dir / "postbox/db/db_sqlite").exists():
                    return True
        return False

    def _tdb(self):
        return _get_telegram_db()

    def recent_chats(self, limit: int) -> list[dict]:
        return [{
            "name": c["name"],
            "id": str(c["peer_id"]),
            "platform": self.name,
            "last_message": c["last_message"],
            "phone": format_phone(c["phone"]) if c.get("phone") else "",
            "username": c.get("username", ""),
            "message_count": c.get("message_count"),
        } for c in self._tdb().recent_chats(limit)]

    def find_chats(self, query: str) -> list[dict]:
        return [{
            "name": c["name"],
            "id": str(c["peer_id"]),
            "platform": self.name,
            "phone": format_phone(c["phone"]) if c.get("phone") else "",
            "username": c.get("username", ""),
        } for c in self._tdb().find_chats(query)]

    def has_chat(self, identifier: str) -> bool:
        return self._tdb().resolve_identifier(identifier) is not None

    def read_messages(self, identifier: str, limit: int) -> list[dict]:
        peer_id = self._tdb().resolve_identifier(identifier)
        if peer_id is None:
            return []
        return self._tdb().read_messages(peer_id, limit)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[dict]:
        peer = self._tdb().resolve_identifier(chat_id) if chat_id else None
        return [{
            "timestamp": r["timestamp"],
            "chat_name": r["chat_name"],
            "sender": r["sender"],
            "text": r["text"],
            "is_from_me": r.get("is_from_me", r["sender"] == "Me"),
            "platform": self.name,
        } for r in self._tdb().search_messages(query, limit, peer_id=peer)]

    def stats(self) -> dict:
        s = self._tdb().stats()
        return {"platform": self.name, "messages": s["messages"], "chats": s["peers"]}

    def can_send(self) -> bool:
        return telegram_send.is_available()

    def _require_send(self) -> None:
        if not self.can_send():
            raise SystemExit(
                "No Telegram auth keys found. Is Telegram installed and logged in?"
            )

    def send_message(self, identifier: str, text: str) -> str:
        self._require_send()
        peer_id = self._tdb().resolve_identifier(identifier)
        if peer_id is None:
            raise SystemExit(f'Could not find Telegram chat for "{identifier}".')
        return asyncio.run(telegram_send.send_message(peer_id, text))

    def resolve_display_name(self, identifier: str) -> str:
        peer_id = self._tdb().resolve_identifier(identifier)
        if peer_id is None:
            return identifier
        peer = self._tdb()._get_peer(peer_id)
        from .telegram_db import _peer_display_name
        return _peer_display_name(peer)


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------

class WhatsAppAdapter(BackendAdapter):
    name = "whatsapp"
    display_name = "WhatsApp"

    def is_available(self) -> bool:
        return whatsapp_db.is_available()

    def recent_chats(self, limit: int) -> list[dict]:
        return [{
            "name": c["name"],
            "id": c["jid"],
            "platform": self.name,
            "last_message": c["last_message"],
            "phone": format_phone(c["phone"]) if c.get("phone") else "",
            "username": "",
            "message_count": c.get("message_count"),
        } for c in whatsapp_db.recent_chats(limit)]

    def find_chats(self, query: str) -> list[dict]:
        return [{
            "name": c["name"],
            "id": c["jid"],
            "platform": self.name,
            "phone": format_phone(c["phone"]) if c.get("phone") else "",
            "username": "",
        } for c in whatsapp_db.find_chats(query)]

    def has_chat(self, identifier: str) -> bool:
        return whatsapp_db.resolve_identifier(identifier) is not None

    def read_messages(self, identifier: str, limit: int) -> list[dict]:
        jid = whatsapp_db.resolve_identifier(identifier)
        if jid is None:
            return []
        return whatsapp_db.read_messages(jid, limit)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[dict]:
        resolved = whatsapp_db.resolve_identifier(chat_id) if chat_id else None
        return [{
            "timestamp": r["timestamp"],
            "chat_name": r["chat_name"],
            "sender": r["sender"],
            "text": r["text"],
            "is_from_me": r.get("is_from_me", r["sender"] == "Me"),
            "platform": self.name,
        } for r in whatsapp_db.search_messages(query, limit, jid=resolved)]

    def stats(self) -> dict:
        s = whatsapp_db.stats()
        return {"platform": self.name, "messages": s["messages"], "chats": s["chats"]}

    def can_send(self) -> bool:
        return whatsapp_send.is_available()

    def _require_send(self) -> None:
        if not self.can_send():
            raise SystemExit(
                "WhatsApp send not available. Ensure wa-send is built and session exists."
            )

    def send_message(self, identifier: str, text: str) -> str:
        self._require_send()
        jid = whatsapp_db.resolve_identifier(identifier)
        if jid is None:
            raise SystemExit(f'Could not find WhatsApp chat for "{identifier}".')
        return whatsapp_send.send_message(jid, text)

    def resolve_display_name(self, identifier: str) -> str:
        chats = whatsapp_db.find_chats(identifier)
        return chats[0]["name"] if chats else (whatsapp_db.resolve_identifier(identifier) or identifier)


# ---------------------------------------------------------------------------
# Messenger
# ---------------------------------------------------------------------------

class MessengerAdapter(BackendAdapter):
    name = "messenger"
    display_name = "Messenger"

    def is_available(self) -> bool:
        return messenger_api.is_available()

    def recent_chats(self, limit: int) -> list[dict]:
        return [{
            "name": c["name"],
            "id": c["thread_id"],
            "platform": self.name,
            "last_message": c["last_message"],
            "phone": "",
            "username": "",
            "message_count": c.get("message_count"),
        } for c in messenger_api.recent_chats(limit)]

    def find_chats(self, query: str) -> list[dict]:
        return [{
            "name": c["name"],
            "id": c["thread_id"],
            "platform": self.name,
            "phone": "",
            "username": "",
        } for c in messenger_api.find_chats(query)]

    def has_chat(self, identifier: str) -> bool:
        return messenger_api.resolve_identifier(identifier) is not None

    def read_messages(self, identifier: str, limit: int) -> list[dict]:
        thread_id = messenger_api.resolve_identifier(identifier)
        if thread_id is None:
            return []
        return messenger_api.read_messages(thread_id, limit)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[dict]:
        resolved = messenger_api.resolve_identifier(chat_id) if chat_id else None
        return [{
            "timestamp": r["timestamp"],
            "chat_name": r["chat_name"],
            "sender": r["sender"],
            "text": r["text"],
            "is_from_me": r.get("is_from_me", r["sender"] == "Me"),
            "platform": self.name,
        } for r in messenger_api.search_messages(query, limit, thread_id=resolved)]

    def stats(self) -> dict:
        s = messenger_api.stats()
        return {"platform": self.name, "messages": s["messages"], "chats": s["chats"]}

    def can_send(self) -> bool:
        return messenger_api.is_available()

    def _require_send(self) -> None:
        if not self.can_send():
            raise SystemExit(
                "Messenger not available. Run 'messages auth messenger' to set up."
            )

    def send_message(self, identifier: str, text: str) -> str:
        self._require_send()
        thread_id = messenger_api.resolve_identifier(identifier)
        if thread_id is None:
            raise SystemExit(f'Could not find Messenger chat for "{identifier}".')
        return messenger_api.send_message(thread_id, text)

    def resolve_display_name(self, identifier: str) -> str:
        chats = messenger_api.find_chats(identifier)
        return chats[0]["name"] if chats else (messenger_api.resolve_identifier(identifier) or identifier)


# ---------------------------------------------------------------------------
# Registry and dispatch
# ---------------------------------------------------------------------------

_ALL_BACKENDS = [
    IMessageAdapter(), TelegramAdapter(), WhatsAppAdapter(), MessengerAdapter(),
]
_BACKEND_MAP = {b.name: b for b in _ALL_BACKENDS}


def _get_backends(platform: str | None = None) -> list[BackendAdapter]:
    if platform:
        b = _BACKEND_MAP.get(platform)
        return [b] if b and b.is_available() else []
    return [b for b in _ALL_BACKENDS if b.is_available()]


def _get_backend(platform: str) -> BackendAdapter:
    b = _BACKEND_MAP.get(platform)
    if b is None or not b.is_available():
        raise SystemExit(f'Platform "{platform}" is not available.')
    return b


def _find_platform(
    identifier: str, backends: list[BackendAdapter], require_send: bool = False,
) -> BackendAdapter | None:
    found = []
    for b in backends:
        if require_send and not b.can_send():
            continue
        if b.has_chat(identifier):
            found.append(b)
    if len(found) > 1:
        names = " and ".join(b.display_name for b in found)
        raise SystemExit(
            f'Found "{identifier}" on {names}. Use --platform/-p to specify which one.'
        )
    return found[0] if found else None


def available_platforms() -> list[str]:
    return [b.name for b in _ALL_BACKENDS if b.is_available()]


def recent_chats(limit: int, platform: str | None = None) -> list[dict]:
    results = []
    for b in _get_backends(platform):
        results.extend(b.recent_chats(limit))
    results.sort(key=lambda x: x["last_message"], reverse=True)
    return results[:limit]


def find_chats(query: str, platform: str | None = None) -> list[dict]:
    results = []
    for b in _get_backends(platform):
        results.extend(b.find_chats(query))
    return results


def read_messages(
    identifier: str, limit: int, platform: str | None = None,
) -> list[dict]:
    if platform:
        return _get_backend(platform).read_messages(identifier, limit)
    b = _find_platform(identifier, _get_backends())
    return b.read_messages(identifier, limit) if b else []


def search_messages(
    query: str, limit: int, platform: str | None = None, chat: str | None = None,
) -> list[dict]:
    results = []
    for b in _get_backends(platform):
        results.extend(b.search_messages(query, limit, chat_id=chat))
    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results[:limit]


def stats(platform: str | None = None) -> list[dict]:
    return [b.stats() for b in _get_backends(platform)]


def send_message(
    identifier: str, text: str, platform: str | None = None,
) -> tuple[str, str]:
    if platform:
        b = _get_backend(platform)
        return b.name, b.send_message(identifier, text)
    b = _find_platform(identifier, _get_backends(), require_send=True)
    if b is None:
        raise SystemExit(f'Could not find "{identifier}" on any platform.')
    return b.name, b.send_message(identifier, text)


def resolve_send_target(
    identifier: str, platform: str | None = None,
) -> tuple[str, str]:
    if platform:
        b = _get_backend(platform)
        b._require_send()
        return b.name, b.resolve_display_name(identifier)
    b = _find_platform(identifier, _get_backends(), require_send=True)
    if b is None:
        raise SystemExit(f'Could not find "{identifier}" on any platform.')
    return b.name, b.resolve_display_name(identifier)
