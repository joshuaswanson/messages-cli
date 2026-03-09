"""Send WhatsApp messages via the wa-send Go binary (whatsmeow)."""

import subprocess
import sys
from pathlib import Path

# Session store (shared with wa-media-tool and whatsapp-cli)
SESSION_DIR = Path.home() / ".whatsapp-cli"

# The wa-send binary lives next to wa-media-tool in the project
_WA_SEND_BIN = Path(__file__).parent.parent / "wa-send-tool" / "wa-send"


def is_available() -> bool:
    """Check if a WhatsApp session exists and the send binary is built."""
    session_db = SESSION_DIR / "whatsapp.db"
    return session_db.exists() and _WA_SEND_BIN.exists()


def send_message(jid: str, text: str) -> str:
    """Send a WhatsApp message to a JID via the wa-send binary."""
    if not _WA_SEND_BIN.exists():
        raise RuntimeError(
            f"wa-send binary not found at {_WA_SEND_BIN}. "
            "Build it with: cd wa-send-tool && go build -o wa-send"
        )
    result = subprocess.run(
        [str(_WA_SEND_BIN), "--session", str(SESSION_DIR), "--to", jid, "--message", text],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"wa-send error: {err}")
    return "Message sent."
