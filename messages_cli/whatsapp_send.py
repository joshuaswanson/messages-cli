"""Send WhatsApp messages via the wa-send Go binary (whatsmeow)."""

import subprocess

from .models import BackendError
from .utils import WHATSAPP_SESSION_DIR, find_tool

_SESSION_DB = WHATSAPP_SESSION_DIR / "whatsapp.db"


def unavailable_reason() -> str | None:
    if find_tool("wa-send") is None:
        return "wa-send is not built. Run 'messages setup'."
    if not _SESSION_DB.exists():
        return "No WhatsApp session. Run 'messages auth whatsapp'."
    return None


def send_message(jid: str, text: str) -> str:
    """Send a WhatsApp message to a JID. The text goes through stdin."""
    if reason := unavailable_reason():
        raise BackendError(reason)
    binary = find_tool("wa-send")
    assert binary is not None
    try:
        result = subprocess.run(
            [str(binary), "--session", str(WHATSAPP_SESSION_DIR), "--to", jid],
            input=text,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError("wa-send timed out") from e
    if result.returncode != 0:
        raise BackendError(f"wa-send error: {result.stderr.strip() or result.stdout.strip()}")
    return "Message sent."
