# messages-cli

Read, search, and send messages across iMessage, SMS/RCS, and Telegram from the terminal. macOS only.

## Platforms

- **Messages** -- iMessage and SMS/RCS (blue and green bubbles)
- **Telegram** -- Full support including sending (piggybacks on your Telegram.app session)
- **WhatsApp** -- Coming soon

All commands work across platforms by default. Use `--platform/-p` to filter by a specific platform (`messages` or `telegram`). Platform tags `[ms]`/`[tg]` appear in merged output.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run messages --help
```

## Usage

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

### Find chats

```bash
$ messages chats find "Alice"
Alice Johnson  [tg]  @alicej  +41 79 123 45 67

$ messages chats find "John"
John Smith  [ms]  +1 206-555-1234
Book Club   [ms]
```

### Read messages

Accepts contact names, group chat names, phone numbers, or Telegram usernames. Auto-detects the platform unless `--platform` is specified.

```bash
$ messages read "Sarah" --limit 3
2026-02-27 08:10:00  Sarah Chen  Running 5 min late
2026-02-27 08:12:30  Me          No worries, I'll grab us a table
2026-02-27 09:15:43  Sarah Chen  Thanks for breakfast! [image: IMG_2041.heic]

$ messages read "+41 79 123 45 67" --limit 3
2026-02-27 08:10:00  Alice Johnson  Did you see the news?
2026-02-27 08:12:30  Me             Yeah, wild
2026-02-27 09:15:43  Alice Johnson  Right??
```

### Search messages

```bash
$ messages search "dinner" --limit 3
2026-02-27 18:30:00  +1 206-555-1234  Me          Dinner at 7?           [ms]
2026-02-26 12:15:00  Family Group     Bob Williams Dinner plans?          [tg]
2026-02-25 09:00:00  +1 415-555-9876  Sarah Chen  Thanks for dinner!     [ms]
```

### Send a message

Dry-run by default. Pass `--confirm` to actually send.

```bash
$ messages send "Sarah" "Hey, are we still on for tomorrow?"
Would send [ms] to +1 415-555-9876: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "+1 415-555-9876" "On my way!" --confirm
Message sent.

$ messages send "Alice" "See you at 3" -p telegram
Would send [tg] to Alice Johnson: See you at 3
Pass --confirm to actually send.

$ messages send "Alice" "See you at 3" -p telegram --confirm
Message sent.
```

### Search contacts

```bash
$ messages contacts search "John"
John Smith
  phone: +1 206 555 1234
  phone: +41 78 555 6789
  email: john.smith@gmail.com
```

### Statistics

```bash
$ messages stats
messages   Messages: 48,291  Chats: 142
telegram   Messages: 28,529  Chats: 207
```

## Requirements

- **Full Disk Access** -- Grant your terminal app Full Disk Access in System Settings > Privacy & Security. Required for reading the iMessage and Telegram databases.
- **sqlcipher** -- `brew install sqlcipher`. Required for Telegram support.

## How it works

**Messages** -- Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly for reading. Sends via AppleScript. Resolves phone numbers to contact names, shows reactions and attachments, formats phone numbers by country code.

**Telegram** -- Decrypts the local Telegram database (SQLCipher-encrypted postbox) for reading. For sending, it extracts the persistent MTProto auth key from the local database and uses Telethon to make API calls -- no separate login needed, it piggybacks on your existing Telegram.app session.
