# messages-cli

CLI for reading and searching local message databases (iMessage, Telegram, WhatsApp) on macOS.

## Platforms

**Messages** -- Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly and sends messages via AppleScript. Covers iMessage (blue bubbles) and SMS/RCS (green bubbles). Anywhere a phone number is accepted, you can use a contact name instead. Shows reactions, attachments, and resolves sender phone numbers to contact names. Phone numbers are formatted with proper spacing based on country code (e.g. `+1 206-555-1234`, `+41 79 123 45 67`).

**Telegram** -- Decrypts and reads the local Telegram database (SQLCipher-encrypted postbox). Supports listing chats, reading messages, and searching. Requires `sqlcipher` CLI (`brew install sqlcipher`).

**WhatsApp** -- Coming soon.

All commands work across platforms by default. Use `--platform/-p` to filter by a specific platform (`messages` or `telegram`). Platform tags `[ms]`/`[tg]` appear in merged output.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run messages --help
```

## Usage

### Search contacts

```bash
$ messages contacts search "John"
John Smith
  phone: +1 206 555 1234
  phone: +41 78 555 6789
  email: john.smith@gmail.com
```

### List recent chats

```bash
$ messages chats recent --limit 4
Alice Johnson  [tg]  2026-02-28 14:30:12  @alicej  +41 79 123 45 67
John Smith     [ms]  2026-02-28 14:30:12  +1 206-555-1234
Family Group   [tg]  2026-02-27 09:15:43
Book Club      [ms]  2026-02-26 20:00:01

$ messages chats recent --platform telegram --limit 3
Alice Johnson    2026-02-28 14:30:12  @alicej  +41 79 123 45 67
Family Group     2026-02-27 09:15:43
Bob Williams     2026-02-26 20:00:01  @bobw
```

Phone numbers, group chat participants, and Telegram usernames are resolved automatically. Platform tags only appear in merged (cross-platform) mode.

### Find chats

```bash
$ messages chats find "Alice"
Alice Johnson  [tg]  @alicej  +41 79 123 45 67

$ messages chats find "John"
John Smith  [ms]  +1 206-555-1234
Book Club   [ms]
```

### Read messages

Accepts contact names, group chat names, phone numbers, Telegram usernames, or raw chat/peer IDs. Auto-detects the platform unless `--platform` is specified.

```bash
$ messages read "Sarah" --limit 3
2026-02-27 08:10:00  Sarah Chen  Running 5 min late
2026-02-27 08:12:30  Me          No worries, I'll grab us a table
2026-02-27 09:15:43  Sarah Chen  Thanks for breakfast! [image: IMG_2041.heic]

$ messages read "+41 79 123 45 67" --limit 3   # phone numbers work for both platforms
2026-02-27 08:10:00  Alice Johnson  Did you see the news?
2026-02-27 08:12:30  Me             Yeah, wild
2026-02-27 09:15:43  Alice Johnson  Right??

$ messages read "Book Club" --limit 1 --full
2026-02-26 20:00:01  John Smith  Here's what I was thinking for the next meeting, we should probably try to
coordinate schedules better. Maybe a poll would help?
```

### Search messages

```bash
$ messages search "dinner" --limit 3
2026-02-27 18:30:00  +1 206-555-1234  Me          Dinner at 7?           [ms]
2026-02-26 12:15:00  Family Group     Bob Williams Dinner plans?          [tg]
2026-02-25 09:00:00  +1 415-555-9876  Sarah Chen  Thanks for dinner!     [ms]

$ messages search "dinner" --platform telegram --limit 3
2026-02-26 12:15:00  Family Group  Bob Williams  Dinner plans?
```

### Send a message

Sends via iMessage. Accepts contact names, group chat names, or phone numbers.

```bash
$ messages send "Sarah" "Hey, are we still on for tomorrow?"
Would send to +1 415-555-9876: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "+1 415-555-9876" "On my way!" --confirm
Message sent.
```

### Statistics

```bash
$ messages stats
messages   Messages: 48,291  Chats: 142
telegram   Messages: 28,529  Chats: 207

$ messages stats --platform telegram
telegram   Messages: 28,529  Chats: 207
```

## Requirements

- **Full Disk Access**: Your terminal app must have Full Disk Access granted in System Settings > Privacy & Security > Full Disk Access. Required for reading the iMessage and Telegram databases.
- **sqlcipher** (for Telegram): `brew install sqlcipher`. Required to decrypt the Telegram database.
