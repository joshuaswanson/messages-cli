"""WhatsApp authentication via QR code pairing."""

import subprocess

from .models import BackendError
from .utils import WHATSAPP_SESSION_DIR, find_tool


def run_auth() -> int:
    """Run wa-auth interactively so the QR code prints to this terminal."""
    binary = find_tool("wa-auth")
    if binary is None:
        raise BackendError("wa-auth is not built. Run 'messages setup'.")
    return subprocess.run([str(binary), "--session", str(WHATSAPP_SESSION_DIR)]).returncode
