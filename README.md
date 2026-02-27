# messages-cli

CLI for reading, searching, and sending iMessages on macOS.

Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly and sends messages via AppleScript.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run messages --help
```

## Usage

```bash
# Search contacts by name
messages contacts search "John"

# List recent chats
messages chats recent --limit 10

# Find chats with a specific person
messages chats find "+15551234567"

# Read messages from a chat
messages read "+15551234567" --limit 30

# Search message content
messages search "dinner" --limit 10

# Send a message (dry-run by default)
messages send "+15551234567" "Hey!"
messages send "+15551234567" "Hey!" --confirm
```

## Requirements

Your terminal app must have **Full Disk Access** granted in System Settings > Privacy & Security > Full Disk Access. Without this, the Messages database cannot be read.
