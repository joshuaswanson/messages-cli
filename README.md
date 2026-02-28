# messages-cli

CLI for reading, searching, and sending iMessages on macOS.

Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly and sends messages via AppleScript. Anywhere a phone number is accepted, you can use a contact name instead. Shows reactions, attachments, and resolves sender phone numbers to contact names.

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
$ messages chats recent --limit 3
+12065551234    2026-02-28 14:30:12
+14155559876    2026-02-27 09:15:43
chat927461038  Book Club  2026-02-26 20:00:01
```

### Find chats with a person

```bash
$ messages chats find "John"
+12065551234    (ROWID=42)
chat927461038  Book Club  (ROWID=87)
```

### Read messages

Works with contact names, group chat names, phone numbers, or chat IDs.

```bash
$ messages read "Book Club" --limit 5
2026-02-26 19:55:00 | John Smith | Has everyone finished the book?
2026-02-26 19:56:12 | Sarah Chen | Almost done!
2026-02-26 19:57:30 | Me | Just started chapter 10 [image: IMG_4521.png]
2026-02-26 19:58:01 | Sarah Chen | [Loved] "Just started chapter 10"
2026-02-26 20:00:01 | John Smith | Let's discuss Wednesday
```

### Search messages

```bash
$ messages search "dinner" --limit 3
2026-02-27 18:30:00 | +12065551234 |  | Me | Dinner at 7?
2026-02-26 12:15:00 | chat927461038 | Book Club | John Smith | Dinner after the meetup?
2026-02-25 09:00:00 | +14155559876 |  | Sarah Chen | Thanks for dinner last night!
```

### Send a message

```bash
$ messages send "John" "Hey, are we still on for tomorrow?"
Would send to +12065551234: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "John" "Hey, are we still on for tomorrow?" --confirm
Message sent.
```

## Requirements

Your terminal app must have **Full Disk Access** granted in System Settings > Privacy & Security > Full Disk Access. Without this, the Messages database cannot be read.
