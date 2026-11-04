import subprocess

from messages_cli import db, send


def _attributed_body(text: str) -> bytes:
    encoded = text.encode()
    return (
        b"streamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject"
        b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + bytes([len(encoded)]) + encoded
        + b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00"
    )


def test_extract_attributed_body():
    assert db.extract_attributed_body(_attributed_body("dinner at 7?")) == "dinner at 7?"
    assert db.extract_attributed_body(None) is None
    assert db.extract_attributed_body(b"garbage") is None


def test_message_body_prefers_plain_text():
    assert db.message_body("plain", _attributed_body("archived")) == "plain"
    assert db.message_body(None, _attributed_body("archived")) == "archived"


def test_send_passes_text_as_argv(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(send.subprocess, "run", fake_run)
    hostile = 'hi" & (do shell script "touch /tmp/pwned") & "'
    send.send_message("chat", "any;-;+12065551234", hostile)
    args = calls[0]
    assert args[:2] == ["osascript", "-e"]
    assert hostile not in args[2]
    assert args[3:] == ["chat", "any;-;+12065551234", hostile]
