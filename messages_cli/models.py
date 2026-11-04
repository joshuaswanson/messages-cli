"""Platform-independent records returned by every backend."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


class BackendError(Exception):
    """A backend could not complete a request (missing access, expired auth, etc.)."""


class AmbiguousChatError(BackendError):
    """An identifier matched more than one chat."""


@dataclass
class Chat:
    platform: str
    id: str
    name: str
    last_message: str = ""
    phone: str = ""
    username: str = ""
    is_group: bool = False
    message_count: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Message:
    id: str
    timestamp: str
    sender: str
    text: str
    is_from_me: bool
    edited: bool = False
    image_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SearchHit(Message):
    platform: str = ""
    chat_id: str = ""
    chat_name: str = ""
