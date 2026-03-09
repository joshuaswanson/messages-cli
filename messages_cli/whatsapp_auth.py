"""WhatsApp authentication via QR code pairing."""

import subprocess
import sys
from pathlib import Path

_WA_AUTH_BIN = Path(__file__).parent.parent / "wa-auth-tool" / "wa-auth"
SESSION_DIR = Path.home() / ".whatsapp-cli"


def run_auth() -> None:
    """Run the wa-auth binary for QR code pairing.

    This runs interactively, displaying a QR code in the terminal
    for the user to scan with their phone.
    """
    if not _WA_AUTH_BIN.exists():
        print(
            f"wa-auth binary not found at {_WA_AUTH_BIN}. "
            "Build it with: cd wa-auth-tool && go build -o wa-auth",
            file=sys.stderr,
        )
        sys.exit(1)

    # Run interactively (stdin/stdout/stderr pass through)
    result = subprocess.run(
        [str(_WA_AUTH_BIN), "--session", str(SESSION_DIR)],
    )
    sys.exit(result.returncode)
