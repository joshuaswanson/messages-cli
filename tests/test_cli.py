import json

import click
import pytest
from click.testing import CliRunner

from messages_cli import backends, cli
from messages_cli.models import AmbiguousChatError, BackendError, Chat, Message


class FakeBackend(backends.Backend):
    def __init__(self, name, chats=(), error=None):
        self.name = self.display_name = name
        self._chats = list(chats)
        self._error = error

    def is_available(self):
        return True

    def recent_chats(self, limit):
        if self._error:
            raise self._error
        return self._chats[:limit]

    def find_chats(self, query):
        return [c for c in self._chats if query.lower() in c.name.lower()]

    def resolve_chat(self, identifier, deep=True, strict=False):
        if self._error:
            raise self._error
        matches = self.find_chats(identifier)
        if len(matches) > 1:
            raise AmbiguousChatError("ambiguous")
        return matches[0] if matches else None

    def read_messages(self, chat_id, limit):
        return [Message(id="1", timestamp="2026-01-01T10:00:00+00:00", sender="Me", text="hi", is_from_me=True)]

    def search_messages(self, query, limit, chat_id=None):
        return []

    def stats(self):
        return {"messages": 1, "chats": 1}


@pytest.fixture
def fake_backends(monkeypatch):
    def install(*items):
        monkeypatch.setattr(backends, "ALL_BACKENDS", list(items))
        monkeypatch.setattr(backends, "_BY_NAME", {b.name: b for b in items})
        monkeypatch.setattr(backends.messenger_api, "is_available", lambda: False)
    return install


def _chat(platform, name, ts="2026-01-01T00:00:00+00:00"):
    return Chat(platform=platform, id=name, name=name, last_message=ts)


def test_broken_backend_is_skipped_with_warning(fake_backends):
    fake_backends(FakeBackend("a", [_chat("a", "Alice")]), FakeBackend("b", error=BackendError("cookies expired")))
    result = CliRunner().invoke(cli.cli, ["chats", "recent", "--json"])
    assert result.exit_code == 0
    assert [c["name"] for c in json.loads(result.stdout)] == ["Alice"]
    assert "skipping b: cookies expired" in result.stderr


def test_explicit_platform_error_is_reported(fake_backends):
    fake_backends(FakeBackend("messages", error=BackendError("cookies expired")))
    result = CliRunner().invoke(cli.cli, ["chats", "recent", "-p", "messages"])
    assert result.exit_code == 1
    assert "cookies expired" in result.stderr


def test_read_on_two_platforms_asks_for_platform(fake_backends):
    fake_backends(FakeBackend("a", [_chat("a", "Alice")]), FakeBackend("b", [_chat("b", "Alice")]))
    with pytest.raises(BackendError, match="Use --platform"):
        backends.read_messages("Alice", 5)


def test_ambiguity_on_one_platform_is_reported(fake_backends):
    fake_backends(FakeBackend("a", [_chat("a", "Alice A"), _chat("a", "Alice B")]), FakeBackend("b"))
    with pytest.raises(AmbiguousChatError):
        backends.read_messages("Alice", 5)


def test_read_json(fake_backends):
    fake_backends(FakeBackend("a", [_chat("a", "Alice")]))
    result = CliRunner().invoke(cli.cli, ["read", "Alice", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["text"] == "hi"


def test_format_message_styles_tags():
    m = Message(id="1", timestamp="2026-01-01T10:00:00+00:00", sender="Me", text="[Loved] nice [image: a.jpg]", is_from_me=True)
    line = cli._format_message(m, full=True)
    assert click.style("[Loved]", fg=cli.REACTION) in line
    assert click.style("[image: a.jpg]", fg=cli.ATTACHMENT) in line
