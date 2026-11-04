"""Platform registry and cross-platform dispatch.

Each platform implements Backend. Without --platform, commands query every
available backend; a backend that fails prints a warning and is skipped.
"""

from __future__ import annotations

import asyncio
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from . import db, messenger_api, send, telegram_db, telegram_send, whatsapp_db, whatsapp_send
from .models import AmbiguousChatError, BackendError, Chat, Message, SearchHit
from .utils import format_phone, looks_like_phone

T = TypeVar("T")


@dataclass
class SendTarget:
    id: str
    display: str
    detail: str = ""
    kind: str = "chat"


class Backend(ABC):
    name: str
    display_name: str

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def recent_chats(self, limit: int) -> list[Chat]: ...

    @abstractmethod
    def find_chats(self, query: str) -> list[Chat]: ...

    @abstractmethod
    def resolve_chat(self, identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
        """The chat an identifier refers to. Raises AmbiguousChatError.

        deep=False is used when probing every platform and must stay fast.
        strict=True (used for sending) never merges same-name chats.
        """

    @abstractmethod
    def read_messages(self, chat_id: str, limit: int) -> list[Message]:
        """Most recent messages, newest first."""

    def read_around(self, chat_id: str, message_id: str, count: int) -> list[Message]:
        """Messages surrounding message_id, oldest first."""
        messages = list(reversed(self.read_messages(chat_id, 200)))
        for i, m in enumerate(messages):
            if m.id == message_id:
                return messages[max(0, i - count): i + count + 1]
        return []

    @abstractmethod
    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[SearchHit]: ...

    @abstractmethod
    def stats(self) -> dict: ...

    def send_unavailable_reason(self) -> str | None:
        return f"{self.display_name} does not support sending."

    def send_target(self, identifier: str, deep: bool = True) -> SendTarget | None:
        chat = self.resolve_chat(identifier, deep=deep, strict=True)
        return SendTarget(chat.id, chat.name, chat.phone or chat.username or chat.id) if chat else None

    def send_message(self, target: SendTarget, text: str) -> str:
        raise BackendError(f"{self.display_name} does not support sending.")


class IMessageBackend(Backend):
    name = "messages"
    display_name = "Messages"

    def is_available(self) -> bool:
        return db.is_available()

    def recent_chats(self, limit: int) -> list[Chat]:
        return db.recent_chats(limit)

    def find_chats(self, query: str) -> list[Chat]:
        return db.find_chats(query)

    def resolve_chat(self, identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
        return db.resolve_chat(identifier, strict)

    def read_messages(self, chat_id: str, limit: int) -> list[Message]:
        return db.read_messages(chat_id, limit)

    def read_around(self, chat_id: str, message_id: str, count: int) -> list[Message]:
        return db.read_around(chat_id, message_id, count)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[SearchHit]:
        return db.search_messages(query, limit, chat_id)

    def stats(self) -> dict:
        return db.stats()

    def send_unavailable_reason(self) -> str | None:
        return None

    def send_target(self, identifier: str, deep: bool = True) -> SendTarget | None:
        chat = db.resolve_chat(identifier, strict=True)
        if chat and (guid := db.chat_guid(chat.id)):
            return SendTarget(guid, chat.name, chat.phone or chat.id)
        # When probing all platforms, a bare phone or email only counts if a chat exists.
        if not deep and (looks_like_phone(identifier) or "@" in identifier):
            return None
        if contact := db.contact_handle(identifier):
            handle, display = contact
            return SendTarget(handle, display, format_phone(handle), kind="handle")
        return None

    def send_message(self, target: SendTarget, text: str) -> str:
        return send.send_message(target.kind, target.id, text)


class TelegramBackend(Backend):
    name = "telegram"
    display_name = "Telegram"

    def is_available(self) -> bool:
        return telegram_db.is_available()

    @property
    def _db(self) -> telegram_db.TelegramDB:
        return telegram_db.get_db()

    def recent_chats(self, limit: int) -> list[Chat]:
        return self._db.recent_chats(limit)

    def find_chats(self, query: str) -> list[Chat]:
        return self._db.find_chats(query)

    def resolve_chat(self, identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
        return self._db.resolve_chat(identifier, strict)

    def read_messages(self, chat_id: str, limit: int) -> list[Message]:
        return self._db.read_messages(int(chat_id), limit)

    def read_around(self, chat_id: str, message_id: str, count: int) -> list[Message]:
        return self._db.read_around(message_id, count)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[SearchHit]:
        return self._db.search_messages(query, limit, int(chat_id) if chat_id else None)

    def stats(self) -> dict:
        return self._db.stats()

    def send_unavailable_reason(self) -> str | None:
        return telegram_send.unavailable_reason()

    def send_message(self, target: SendTarget, text: str) -> str:
        return asyncio.run(telegram_send.send_message(int(target.id), text))


class WhatsAppBackend(Backend):
    name = "whatsapp"
    display_name = "WhatsApp"

    def is_available(self) -> bool:
        return whatsapp_db.is_available()

    def recent_chats(self, limit: int) -> list[Chat]:
        return whatsapp_db.recent_chats(limit)

    def find_chats(self, query: str) -> list[Chat]:
        return whatsapp_db.find_chats(query)

    def resolve_chat(self, identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
        return whatsapp_db.resolve_chat(identifier, strict)

    def read_messages(self, chat_id: str, limit: int) -> list[Message]:
        return whatsapp_db.read_messages(chat_id, limit)

    def read_around(self, chat_id: str, message_id: str, count: int) -> list[Message]:
        return whatsapp_db.read_around(chat_id, message_id, count)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[SearchHit]:
        return whatsapp_db.search_messages(query, limit, chat_id)

    def stats(self) -> dict:
        return whatsapp_db.stats()

    def send_unavailable_reason(self) -> str | None:
        return whatsapp_send.unavailable_reason()

    def send_target(self, identifier: str, deep: bool = True) -> SendTarget | None:
        stripped = identifier.strip()
        if stripped.endswith(("@s.whatsapp.net", "@g.us")):
            return SendTarget(stripped, stripped, stripped)
        return super().send_target(identifier, deep)

    def send_message(self, target: SendTarget, text: str) -> str:
        return whatsapp_send.send_message(target.id, text)


class MessengerBackend(Backend):
    name = "messenger"
    display_name = "Messenger"

    def is_available(self) -> bool:
        return messenger_api.is_available()

    def recent_chats(self, limit: int) -> list[Chat]:
        return messenger_api.recent_chats(limit)

    def find_chats(self, query: str) -> list[Chat]:
        return messenger_api.find_chats(query)

    def resolve_chat(self, identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
        return messenger_api.resolve_chat(identifier, deep=deep, strict=strict)

    def read_messages(self, chat_id: str, limit: int) -> list[Message]:
        return messenger_api.read_messages(chat_id, limit)

    def search_messages(self, query: str, limit: int, chat_id: str | None = None) -> list[SearchHit]:
        return messenger_api.search_messages(query, limit, chat_id)

    def stats(self) -> dict:
        return messenger_api.stats()

    def send_unavailable_reason(self) -> str | None:
        return None

    def send_message(self, target: SendTarget, text: str) -> str:
        return messenger_api.send_message(target.id, text)


ALL_BACKENDS: list[Backend] = [IMessageBackend(), TelegramBackend(), WhatsAppBackend(), MessengerBackend()]
PLATFORMS = tuple(b.name for b in ALL_BACKENDS)
_BY_NAME = {b.name: b for b in ALL_BACKENDS}


def get_backend(platform: str) -> Backend:
    b = _BY_NAME.get(platform)
    if b is None or not b.is_available():
        raise BackendError(f'Platform "{platform}" is not available.')
    return b


def _warn(backend: Backend, error: Exception) -> None:
    print(f"warning: skipping {backend.display_name}: {error}", file=sys.stderr)


def _collect(platform: str | None, fn: Callable[[Backend], list[T]]) -> list[T]:
    """Run fn on one backend (errors propagate) or on all (errors become warnings)."""
    if platform:
        return fn(get_backend(platform))
    results: list[T] = []
    for b in ALL_BACKENDS:
        if not b.is_available():
            continue
        try:
            results.extend(fn(b))
        except BackendError as e:
            _warn(b, e)
    return results


def _locate(identifier: str, lookup: Callable[[Backend], T | None]) -> tuple[Backend, T]:
    """Find the one backend where lookup succeeds, probing every available backend."""
    found: list[tuple[Backend, T]] = []
    ambiguous: list[tuple[Backend, AmbiguousChatError]] = []
    for b in ALL_BACKENDS:
        if not b.is_available():
            continue
        try:
            result = lookup(b)
        except AmbiguousChatError as e:
            ambiguous.append((b, e))
            continue
        except BackendError as e:
            _warn(b, e)
            continue
        if result is not None:
            found.append((b, result))
    matched = [b for b, _ in found] + [b for b, _ in ambiguous]
    if len(matched) > 1:
        names = " and ".join(b.display_name for b in matched)
        raise BackendError(f'Found "{identifier}" on {names}. Use --platform/-p to pick one.')
    if ambiguous:
        raise ambiguous[0][1]
    if not found:
        hint = ""
        if messenger_api.is_available():
            hint = " Messenger was only checked against recent threads; use -p messenger to search all of them."
        raise BackendError(f'Could not find a chat matching "{identifier}".{hint}')
    return found[0]


def resolve_chat(identifier: str, platform: str | None = None) -> tuple[Backend, Chat]:
    if platform:
        b = get_backend(platform)
        chat = b.resolve_chat(identifier, deep=True)
        if chat is None:
            raise BackendError(f'Could not find a {b.display_name} chat matching "{identifier}".')
        return b, chat
    return _locate(identifier, lambda b: b.resolve_chat(identifier, deep=False))


def recent_chats(limit: int, platform: str | None = None) -> list[Chat]:
    chats = _collect(platform, lambda b: b.recent_chats(limit))
    chats.sort(key=lambda c: c.last_message, reverse=True)
    return chats[:limit]


def find_chats(query: str, platform: str | None = None) -> list[Chat]:
    return _collect(platform, lambda b: b.find_chats(query))


def read_messages(identifier: str, limit: int, platform: str | None = None) -> tuple[Chat, list[Message]]:
    b, chat = resolve_chat(identifier, platform)
    return chat, b.read_messages(chat.id, limit)


def read_around(hit: SearchHit, count: int) -> list[Message]:
    return get_backend(hit.platform).read_around(hit.chat_id, hit.id, count)


def search_messages(
    query: str, limit: int, platform: str | None = None, chat: str | None = None,
) -> list[SearchHit]:
    if chat:
        b, target = resolve_chat(chat, platform)
        return b.search_messages(query, limit, chat_id=target.id)
    hits = _collect(platform, lambda b: b.search_messages(query, limit))
    hits.sort(key=lambda h: h.timestamp, reverse=True)
    return hits[:limit]


def stats(platform: str | None = None) -> list[dict]:
    return _collect(platform, lambda b: [{"platform": b.name, **b.stats()}])


def _sendable(b: Backend) -> Backend:
    if reason := b.send_unavailable_reason():
        raise BackendError(reason)
    return b


def resolve_send_target(identifier: str, platform: str | None = None) -> tuple[Backend, SendTarget]:
    if platform:
        b = _sendable(get_backend(platform))
        target = b.send_target(identifier, deep=True)
        if target is None:
            raise BackendError(f'Could not find a {b.display_name} chat matching "{identifier}".')
        return b, target
    return _locate(
        identifier,
        lambda b: None if b.send_unavailable_reason() else b.send_target(identifier, deep=False),
    )
