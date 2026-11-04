"""Send iMessage/SMS/RCS messages via AppleScript."""

import subprocess

from .models import BackendError

# Values arrive through argv so message text is never parsed as AppleScript.
_SCRIPT = """
on run argv
    set targetKind to item 1 of argv
    set targetId to item 2 of argv
    set messageText to item 3 of argv
    tell application "Messages"
        if targetKind is "chat" then
            send messageText to chat id targetId
        else
            set targetService to 1st account whose service type = iMessage
            send messageText to participant targetId of targetService
        end if
    end tell
end run
"""


def send_message(kind: str, target: str, text: str) -> str:
    """Send to an existing chat by GUID (kind="chat") or to a handle over iMessage (kind="handle")."""
    if kind not in ("chat", "handle"):
        raise ValueError(f"unknown target kind: {kind}")
    result = subprocess.run(
        ["osascript", "-e", _SCRIPT, kind, target, text],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise BackendError(f"AppleScript error: {result.stderr.strip()}")
    return "Message sent."
