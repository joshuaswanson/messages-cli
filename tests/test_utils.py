import pytest

from messages_cli.models import AmbiguousChatError, Chat
from messages_cli.utils import choose_chat, format_phone, like_pattern, looks_like_phone, phone_key


@pytest.mark.parametrize("value", ["+41 79 123 45 67", "(206) 555-1234", "12065551234", "079.123.45.67"])
def test_looks_like_phone_accepts_numbers(value):
    assert looks_like_phone(value)


@pytest.mark.parametrize("value", ["Book Club 2", "Team 2024", "2", "12345", "alice@example.com", ""])
def test_looks_like_phone_rejects_names_and_short_codes(value):
    assert not looks_like_phone(value)


def test_phone_key_matches_national_and_international_formats():
    assert phone_key("+1 (206) 555-1234") == phone_key("206-555-1234") == "2065551234"
    assert phone_key("079 123 45 67") in phone_key("+41 79 123 45 67")


def test_like_pattern_escapes_wildcards():
    assert like_pattern("50%_off\\") == "%50\\%\\_off\\\\%"


def test_format_phone_leaves_names_alone():
    assert format_phone("Alice 2") == "Alice 2"
    assert format_phone("12065551234") == "+1 206-555-1234"


def _chat(name, is_group=False, id=None):
    return Chat(platform="test", id=id or name, name=name, is_group=is_group)


def test_choose_chat_single_and_empty():
    assert choose_chat("x", []) is None
    only = _chat("Alice")
    assert choose_chat("al", [only]) is only


def test_choose_chat_prefers_exact_name_then_direct():
    group = _chat("Alice, Bob", is_group=True)
    dm = _chat("Alice Smith")
    assert choose_chat("alice", [group, dm]) is dm
    exact = _chat("Book Club", is_group=True)
    assert choose_chat("book club", [exact, _chat("Book Club Two", is_group=True)]) is exact


def test_choose_chat_same_name_direct_chats_merge_unless_strict():
    newest = _chat("John Smith", id="phone")
    older = _chat("John Smith", id="email")
    assert choose_chat("John", [newest, older]) is newest
    with pytest.raises(AmbiguousChatError):
        choose_chat("John", [newest, older], strict=True)


def test_choose_chat_different_people_is_ambiguous():
    with pytest.raises(AmbiguousChatError):
        choose_chat("John", [_chat("John Smith"), _chat("John Doe")])
