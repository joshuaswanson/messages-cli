# messages-cli

CLI for reading and searching local message databases (iMessage, Telegram, WhatsApp) on macOS.

## Platforms

**iMessage** -- Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly and sends messages via AppleScript. Anywhere a phone number is accepted, you can use a contact name instead. Shows reactions, attachments, and resolves sender phone numbers to contact names. Phone numbers are formatted with proper spacing based on country code (e.g. `+1 206-555-1234`, `+41 79 123 45 67`).

**Telegram** -- Decrypts and reads the local Telegram database (SQLCipher-encrypted postbox). Supports listing chats, reading messages, and searching. Requires `sqlcipher` CLI (`brew install sqlcipher`).

**WhatsApp** -- Coming soon.

Output is colorized with truncation for long messages.

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
John Smith                    2026-02-28 14:30:12  +1 206-555-1234
Sarah Chen                    2026-02-27 09:15:43  +1 415-555-9876
Book Club                     2026-02-26 20:00:01
John Smith, Sarah Chen, Alex  2026-02-25 18:00:00
```

Phone numbers and group chat participants are resolved to contact names automatically. Unnamed group chats show participant names.

### Find chats with a person

```bash
$ messages chats find "John"
John Smith                     +1 206-555-1234
Book Club
John Smith, Sarah Chen, Alex
```

### Read messages

Accepts contact names, group chat names, phone numbers, or chat IDs. Long messages are truncated to one line by default.

```bash
$ messages read "Sarah" --limit 3
2026-02-27 08:10:00  Sarah Chen  Running 5 min late
2026-02-27 08:12:30  Me          No worries, I'll grab us a table
2026-02-27 09:15:43  Sarah Chen  Thanks for breakfast! [image: IMG_2041.heic]

$ messages read "+1 415-555-9876" --limit 3   # phone numbers work too
2026-02-27 08:10:00  Sarah Chen  Running 5 min late
2026-02-27 08:12:30  Me          No worries, I'll grab us a table
2026-02-27 09:15:43  Sarah Chen  Thanks for breakfast! [image: IMG_2041.heic]

$ messages read "Book Club" --limit 1 --full  # show full message text
2026-02-26 20:00:01  John Smith  Here's what I was thinking for the next meeting, we should probably try to
coordinate schedules better. Maybe a poll would help?
```

### Search messages

```bash
$ messages search "dinner" --limit 3
2026-02-27 18:30:00  +1 206-555-1234  Me          Dinner at 7?
2026-02-26 12:15:00  Book Club        John Smith  Dinner after the meetup?
2026-02-25 09:00:00  +1 415-555-9876  Sarah Chen  Thanks for dinner last night!
```

### Send a message

Accepts contact names, group chat names, or phone numbers.

```bash
$ messages send "Sarah" "Hey, are we still on for tomorrow?"
Would send to +1 415-555-9876: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "+1 415-555-9876" "On my way!" --confirm
Message sent.
```

### Telegram

#### List recent Telegram chats

```bash
$ messages telegram chats --limit 3
Alice Johnson    2026-02-28 14:30:12  @alicej    12345678
Family Group     2026-02-27 09:15:43             87654321
Bob Williams     2026-02-26 20:00:01  @bobw      11223344
```

#### Find Telegram chats

```bash
$ messages telegram find "Alice"
Alice Johnson  @alicej  12345678
```

#### Read Telegram messages

```bash
$ messages telegram read 12345678 --limit 3
2026-02-27 08:10:00  Alice Johnson  Did you see the news?
2026-02-27 08:12:30  Me             Yeah, wild
2026-02-27 09:15:43  Alice Johnson  Right??
```

#### Search Telegram messages

```bash
$ messages telegram search "dinner" --limit 3
2026-02-27 18:30:00  Alice Johnson   Me             Dinner at 7?
2026-02-26 12:15:00  Family Group    Bob Williams   Dinner plans?
```

#### Telegram stats

```bash
$ messages telegram stats
Messages: 28529
Peers: 207
```

## Requirements

- **Full Disk Access**: Your terminal app must have Full Disk Access granted in System Settings > Privacy & Security > Full Disk Access. Required for reading the iMessage and Telegram databases.
- **sqlcipher** (for Telegram): `brew install sqlcipher`. Required to decrypt the Telegram database.
