"""Send iMessages via AppleScript."""

import subprocess


def send_message(phone: str, text: str) -> str:
    """Send an iMessage to a phone number via Messages.app."""
    script = f'''
    tell application "Messages"
        set targetBuddy to "{phone}"
        set targetService to id of 1st account whose service type = iMessage
        send "{text}" to participant targetBuddy of account id targetService
    end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"AppleScript error: {result.stderr.strip()}")
    return "Message sent."
