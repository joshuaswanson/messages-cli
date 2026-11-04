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
uv run messages --help          # from a checkout
uv tool install .               # or install the `messages` command globally
```

### Go helper tools

WhatsApp sending and Messenger pagination use small Go programs in two Go modules, `go/whatsapp/` and `go/messenger/`. They are separate because the Messenger library pins an older whatsmeow than WhatsApp currently accepts. Build them once (requires Go):

```bash
messages setup
```

This builds `wa-auth`, `wa-send`, `wa-media`, `fb-fetch`, and `fb-threads` into `~/.local/share/messages-cli/bin`. The CLI also finds binaries in `$MESSAGES_CLI_BIN_DIR`, in `go/bin/` of a checkout (`cd go/whatsapp && go build -o ../bin/ ./cmd/...`, and the same in `go/messenger`), and on `PATH`.

### WhatsApp sending setup

Reading WhatsApp messages works out of the box (reads the WhatsApp Desktop database directly). To enable sending, run `messages setup` and pair once:

```bash
messages auth whatsapp
```

This creates a session at `~/.whatsapp-cli/whatsapp.db`. You only need to do this once.

### Downloading expired WhatsApp media

`wa-media` downloads media whose CDN links have expired by asking your phone to re-upload it. It reads undownloaded items from `~/.whatsapp-cli/messages.db` and saves files to `~/.whatsapp-cli/media/<chat>/`.

```bash
~/.local/share/messages-cli/bin/wa-media --dry-run              # list undownloaded images
~/.local/share/messages-cli/bin/wa-media --type video --limit 20
~/.local/share/messages-cli/bin/wa-media --chat "+41 79 123 45 67"
```

`--type` accepts `image`, `video`, `audio`, or `document`. `--chat` accepts phone numbers or JIDs, comma-separated. Your phone must be online for re-uploads.

### Messenger setup

```bash
messages auth messenger
```

Cookies are saved to `~/.config/messages-cli/messenger_cookies.json`. The Go tools are only needed for pagination: fb-fetch loads older messages within a thread, fb-threads discovers older threads beyond the initial ~15 from the inbox sync. Basic reading and sending work without them.

When a command runs across all platforms, Messenger is only checked against the ~15 threads from the inbox sync, because paging through every thread can take minutes. Use `-p messenger` to search all threads.

On first run, fb-threads automatically registers an E2EE device with Facebook's Signal Protocol infrastructure (ICDC) to access end-to-end encrypted threads. Device keys are persisted in `~/.config/messages-cli/messenger_e2ee.db`.

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

$ messages search "dinner" --chat "Family Group" -p whatsapp
2026-02-26 12:15:00  Family Group  Dad  Dinner plans?

$ messages search "dinner" --context 2
--- Family Group [wa] ---
2026-02-26 12:10:00  Mom   What should we do this weekend?
2026-02-26 12:12:00  Me    Not sure yet
2026-02-26 12:15:00  Dad   Dinner plans?  <--
2026-02-26 12:20:00  Mom   Great idea!
2026-02-26 12:25:00  Me    I'm in
```

### Send a message

Dry-run by default. Pass `--confirm` to actually send. If a name matches several different people, the command stops and asks for a more specific name, phone number, or chat ID.

```bash
$ messages send "Sarah" "Hey, are we still on for tomorrow?"
Would send [ms] to +1 415-555-9876: Hey, are we still on for tomorrow?
Pass --confirm to actually send.

$ messages send "Alice" "See you at 3" -p whatsapp --confirm
Message sent.
```

### JSON output

`chats recent`, `chats find`, `read`, `search`, `stats`, and `contacts search` accept `--json`.

```bash
$ messages read "Sarah" --limit 1 --json
[
  {
    "id": "48213",
    "timestamp": "2026-02-27T09:15:43+01:00",
    "sender": "Sarah Chen",
    "text": "Thanks for breakfast! [image: IMG_2041.heic]",
    "is_from_me": false,
    "edited": false,
    "image_paths": ["/Users/me/Library/Messages/Attachments/.../IMG_2041.heic"]
  }
]
```

Search hits add `platform`, `chat_id`, and `chat_name`. With `--context N`, each hit also has a `context` list of surrounding messages.

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
- **Go** -- Required by `messages setup` to build the WhatsApp send/auth tools and the Messenger pagination tools. `brew install go`.
- **WhatsApp Desktop** -- The native macOS app (not the web version). Required for WhatsApp reading.
- **Messenger cookies** -- Run `messages auth messenger` to log in via browser. Required for Messenger support.

## How it works

**Messages** -- Queries the Messages SQLite database (`~/Library/Messages/chat.db`) read-only. Message text is decoded from `attributedBody` when the plain `text` column is empty, which is the case for most messages on recent macOS versions. Sends via AppleScript to the existing chat (iMessage, SMS, RCS, or group), or over iMessage to a contact with no chat yet. Resolves phone numbers to contact names, shows reactions and attachments, formats phone numbers by country code. Image attachments are returned as local file paths from `~/Library/Messages/Attachments/`.

**Telegram** -- Opens the local Telegram database (SQLCipher-encrypted postbox) read-only with the bundled `sqlcipher3` library. Pages are decrypted in memory and no plaintext copy is written to disk. For sending, it extracts the persistent MTProto auth key from the local database and uses Telethon to make API calls, no separate login needed. Image attachments are resolved from the local media cache by parsing photo resource IDs from the binary message format.

**WhatsApp** -- Reads the WhatsApp Desktop SQLite databases (`~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/`) directly. Resolves contact names from the contacts database and group member tables. For sending, uses the `wa-send` Go binary (whatsmeow) with a one-time QR code pairing flow. The message text is passed on stdin. Image attachments are returned as local file paths from the WhatsApp media cache.

**Messenger** -- Authenticates with browser cookies extracted via a one-time login flow. Reads messages by fetching thread pages from messenger.com and parsing the embedded Lightspeed payloads. Two Go binaries (using mautrix-meta) connect via Facebook's MQTT WebSocket protocol for pagination: fb-fetch pages through older messages within a thread, and fb-threads pages through the thread list itself (the initial Lightspeed sync only returns ~15 recent threads). fb-threads also registers an E2EE device via Facebook's ICDC protocol (Signal Protocol key exchange) to access end-to-end encrypted threads, which Facebook migrated most conversations to in 2023. E2EE 1-on-1 DMs are discovered by intercepting Lightspeed `createOpenToE2EEThreadLink` entries, and contact names are resolved via `GetContactsFullTask` over MQTT. Device keys are persisted locally in a SQLite database. Sends messages via Facebook's Lightspeed GraphQL API. The GraphQL query ID is cached in `~/.cache/messages-cli/messenger_api.json` and rediscovered from the JS bundles when it goes stale. Search covers only the messages included in the inbox sync. Image attachments are downloaded from Facebook's CDN to `~/.cache/messages-cli/messenger/`.

### Errors

Without `--platform`, a platform that fails (missing Full Disk Access, expired cookies, Telegram not installed) prints a warning to stderr and the command continues with the other platforms. With `--platform`, the error is reported and the command exits with status 1.

## Development

```bash
uv run pytest
uv run ruff check .
```

## Support

If you find this useful, [buy me a coffee](https://buymeacoffee.com/swanson).

<img src="assets/bmc_qr.png" alt="Buy Me a Coffee QR" width="200">
