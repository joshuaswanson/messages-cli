# messages-cli

Read, search, and send messages across iMessage, SMS/RCS, Telegram, WhatsApp, and Facebook Messenger from the terminal. macOS only.

## Platforms

- **Messages** -- iMessage and SMS/RCS (blue and green bubbles)
- **Telegram** -- Full support including sending (piggybacks on your Telegram.app session)
- **WhatsApp** -- Full support including sending (requires WhatsApp Desktop and one-time QR auth for sending)
- **Messenger** -- Full support including sending (requires one-time browser login for cookie extraction)

All commands work across platforms by default. Use `--platform/-p` to filter by a specific platform (`messages`, `telegram`, `whatsapp`, or `messenger`). Platform tags `[ms]`/`[tg]`/`[wa]`/`[fb]` appear in merged output.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv run messages --help
```

### WhatsApp sending setup

Reading WhatsApp messages works out of the box (reads the WhatsApp Desktop database directly). To enable sending, you need to authenticate once:

```bash
# Build the auth tool (requires Go)
cd wa-auth-tool && go build -o wa-auth && cd ..

# Build the send tool
cd wa-send-tool && go build -o wa-send && cd ..

# Authenticate by scanning a QR code with your phone
messages auth whatsapp
```

This creates a session at `~/.whatsapp-cli/whatsapp.db`. You only need to do this once.

### Messenger setup

```bash
# Build the pagination tool (requires Go)
cd fb-fetch-tool && go build -o fb-fetch && cd ..

# Log in via browser to extract cookies
messages auth messenger
```

Cookies are saved to `~/.config/messages-cli/messenger_cookies.json`. The fb-fetch binary is only needed for loading older messages (pagination); basic reading and sending work without it.

## Usage

### List recent chats

```bash
$ messages chats recent --limit 4
Alice Johnson  2026-02-28 14:30:12  [tg]  @alicej  +41 79 123 45 67
John Smith     2026-02-28 14:30:12  [ms]  +1 206-555-1234
Family Group   2026-02-27 09:15:43  [wa]
Book Club      2026-02-26 20:00:01  [ms]

$ messages chats recent --platform whatsapp --limit 3
Family Group     2026-02-27 09:15:43
Alice Johnson    2026-02-26 20:00:01  +41 79 123 45 67
Work Chat        2026-02-25 18:00:00
```

### Find chats

```bash
$ messages chats find "Alice"
Alice Johnson  [tg]  @alicej  +41 79 123 45 67
Alice Johnson  [wa]  +41 79 123 45 67

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

$ messages read "Family Group" -p whatsapp --limit 3
2026-02-27 10:00:00  Dad   Anyone free for dinner Sunday?
2026-02-27 10:05:00  Me    I'm in
2026-02-27 10:10:00  Mom   Me too!
```

### Search messages

```bash
$ messages search "dinner" --limit 3
2026-02-27 18:30:00  John Smith    Me          Dinner at 7?           [ms]
2026-02-26 12:15:00  Family Group  Dad         Dinner plans?          [wa]
2026-02-25 09:00:00  Sarah Chen    Sarah Chen  Thanks for dinner!     [ms]
```

### Send a message

Dry-run by default. Pass `--confirm` to actually send.

```bash
$ messages send "Sarah" "Hey, are we still on for tomorrow?"
Would send [ms] to +1 415-555-9876: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "Alice" "See you at 3" -p whatsapp --confirm
Message sent.
```

### Search contacts

```bash
$ messages contacts search "John"
John Smith
  phone: +1 206 555 1234
  phone: +41 78 555 6789
  email: john.smith@example.com
```

### Authenticate

```bash
$ messages auth whatsapp
Scan the QR code below with WhatsApp on your phone:
Open WhatsApp > Settings > Linked Devices > Link a Device
# QR code appears here

$ messages auth messenger
# Opens a browser window to log into messenger.com
# Cookies are saved to ~/.config/messages-cli/messenger_cookies.json
```

### Statistics

```bash
$ messages stats
messages    Messages: 48,291  Chats: 142
telegram    Messages: 28,529  Chats: 207
whatsapp    Messages: 12,847  Chats: 89
messenger   Messages: 1,204   Chats: 31
```

## Requirements

- **Full Disk Access** -- Grant your terminal app Full Disk Access in System Settings > Privacy & Security. Required for reading the iMessage, Telegram, and WhatsApp databases.
- **sqlcipher** -- `brew install sqlcipher`. Required for Telegram support.
- **Go** -- Required to build the WhatsApp send/auth tools and the Messenger pagination tool. `brew install go`.
- **WhatsApp Desktop** -- The native macOS app (not the web version). Required for WhatsApp reading.
- **Messenger cookies** -- Run `messages auth messenger` to log in via browser. Required for Messenger support.

## How it works

**Messages** -- Queries the Messages SQLite database (`~/Library/Messages/chat.db`) directly for reading. Sends via AppleScript. Resolves phone numbers to contact names, shows reactions and attachments, formats phone numbers by country code.

**Telegram** -- Decrypts the local Telegram database (SQLCipher-encrypted postbox) for reading. For sending, it extracts the persistent MTProto auth key from the local database and uses Telethon to make API calls, no separate login needed.

**WhatsApp** -- Reads the WhatsApp Desktop SQLite databases (`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`) directly. Resolves contact names from the contacts database and group member tables. For sending, uses a Go binary (whatsmeow) with a one-time QR code pairing flow.

**Messenger** -- Authenticates with browser cookies extracted via a one-time login flow. Reads messages by fetching thread pages from messenger.com and parsing the embedded Lightspeed payloads. For loading older messages beyond the initial page (~20), uses a Go binary (mautrix-meta) that connects via Facebook's MQTT WebSocket protocol. Sends messages via Facebook's Lightspeed GraphQL API.
