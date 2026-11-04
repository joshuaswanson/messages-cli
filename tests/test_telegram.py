import struct

from messages_cli import telegram_db as t


def _string_field(key: str, value: str) -> bytes:
    data = value.encode()
    return bytes([len(key)]) + key.encode() + bytes([t._ValueType.String.value]) + struct.pack("<i", len(data)) + data


def _peer_blob(**fields: str) -> bytes:
    inner = b"".join(_string_field(k, v) for k, v in fields.items())
    return b"\x01_" + bytes([t._ValueType.Object.value]) + struct.pack("<ii", 0, len(inner)) + inner


def _message_blob(text: str, incoming: bool, author_id: int | None) -> bytes:
    data = struct.pack("<bIIB", 0, 1, 1, 0)
    data += struct.pack("<II", t._MessageFlags.Incoming if incoming else 0, 0)
    data += struct.pack("<b", 0)  # no forward info
    if author_id is None:
        data += struct.pack("<b", 0)
    else:
        data += struct.pack("<bq", 1, author_id)
    encoded = text.encode()
    return data + struct.pack("<i", len(encoded)) + encoded + struct.pack("<ii", 0, 0)


def test_parse_message_key():
    key = struct.pack(">qiii", 123456789, 0, 1700000000, 42)
    assert t._parse_message_key(key) == {
        "peer_id": 123456789, "namespace": 0, "timestamp": 1700000000, "message_id": 42,
    }


def test_message_keys_sort_chronologically_as_blobs():
    older = struct.pack(">qiii", 999, 0, 1700000000, 5)[12:20]
    newer = struct.pack(">qiii", 1, 0, 1700000001, 1)[12:20]
    assert older < newer


def test_parse_message_value():
    msg = t._parse_message_value(_message_blob("hello", incoming=True, author_id=77))
    assert msg is not None
    assert (msg["text"], msg["incoming"], msg["author_id"]) == ("hello", True, 77)
    assert t._parse_message_value(b"\x01") is None


def test_parse_peer_and_display_name():
    peer = t._parse_peer(_peer_blob(fn="Alice", ln="Smith", un="alice", p="41791234567"))
    assert peer is not None
    assert peer["phone"] == "41791234567"
    assert t._peer_display_name(peer) == "Alice Smith"
    assert t._peer_display_name({"username": "bob"}) == "@bob"
