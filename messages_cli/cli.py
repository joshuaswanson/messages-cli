"""CLI entry point for messages-cli."""

import click
import phonenumbers

from . import db, send, backends

# Colors
DIM = "bright_black"
SENDER_ME = "green"
SENDER_OTHER = "cyan"
EDITED = "yellow"
REACTION = "magenta"
ATTACHMENT = "yellow"

PLATFORM_TAGS = {
    "messages": "ms",
    "telegram": "tg",
}

VALID_PLATFORMS = ("messages", "telegram")


def _format_phone(value: str) -> str:
    """Format a phone number nicely based on country code."""
    try:
        parsed = phonenumbers.parse(value)
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.INTERNATIONAL
        )
    except phonenumbers.NumberParseException:
        return value


def _truncate(text: str, full: bool) -> str:
    if full:
        return text
    first_line = text.split("\n")[0]
    if len(first_line) > 120:
        return first_line[:117] + "..."
    if "\n" in text:
        return first_line + " [...]"
    return first_line


def _platform_tag(platform: str) -> str:
    """Return a dim [im] or [tg] tag."""
    tag = PLATFORM_TAGS.get(platform, platform[:2])
    return click.style(f"[{tag}]", fg=DIM)


def _validate_platform(ctx, param, value):
    if value is not None and value not in VALID_PLATFORMS:
        raise click.BadParameter(f"must be one of: {', '.join(VALID_PLATFORMS)}")
    return value


def platform_option(fn):
    """Decorator adding --platform/-p to a command."""
    return click.option(
        "--platform", "-p", default=None, callback=_validate_platform,
        expose_value=True, is_eager=False,
        help="Filter by platform (imessage, telegram).",
    )(fn)


def _format_message(m: dict, full: bool) -> str:
    ts = click.style(m["timestamp"], fg=DIM)
    is_me = m["sender"] == "Me"
    sender_text = m["sender"] if is_me else _format_phone(m["sender"])
    sender = click.style(sender_text, fg=SENDER_ME if is_me else SENDER_OTHER)
    if m["edited"]:
        sender += click.style(" [edited]", fg=EDITED)

    text = _truncate(m["text"], full)
    # Colorize attachment and reaction tags
    parts = []
    i = 0
    while i < len(text):
        bracket_start = text.find("[", i)
        if bracket_start < 0:
            parts.append(text[i:])
            break
        parts.append(text[i:bracket_start])
        bracket_end = text.find("]", bracket_start)
        if bracket_end < 0:
            parts.append(text[bracket_start:])
            break
        tag = text[bracket_start:bracket_end + 1]
        if tag.startswith("[image:") or tag.startswith("[video:") or tag.startswith("[audio:") or tag.startswith("[file:"):
            parts.append(click.style(tag, fg=ATTACHMENT))
        elif tag.startswith("[Loved") or tag.startswith("[Liked") or tag.startswith("[Disliked") or tag.startswith("[Laughed") or tag.startswith("[Emphasized") or tag.startswith("[Questioned"):
            parts.append(click.style(tag, fg=REACTION))
        elif tag == "[edited]":
            parts.append(click.style(tag, fg=EDITED))
        elif tag == "[...]":
            parts.append(click.style(tag, fg=DIM))
        else:
            parts.append(tag)
        i = bracket_end + 1
    text = "".join(parts)

    return f"{ts}  {sender}  {text}"


@click.group()
def cli():
    """Unified CLI for all your messaging apps."""


# --- contacts ---


@cli.group()
def contacts():
    """Search contacts."""


@contacts.command("search")
@click.argument("name")
def contacts_search(name: str):
    """Search contacts by name."""
    results = db.search_contacts(name)
    if not results:
        click.echo("No contacts found.")
        return
    for c in results:
        first = c["first"] or ""
        last = c["last"] or ""
        name_str = click.style(f"{first} {last}".strip(), bold=True)
        click.echo(name_str)
        for phone in c["phones"]:
            click.echo(f"  {click.style('phone:', fg=DIM)} {_format_phone(phone)}")
        for email in c["emails"]:
            click.echo(f"  {click.style('email:', fg=DIM)} {email}")


# --- chats ---


@cli.group()
def chats():
    """List and find chats."""


@chats.command("recent")
@click.option("--limit", default=20, help="Number of chats to show.")
@platform_option
def chats_recent(limit: int, platform: str | None):
    """List recent chats."""
    rows = backends.recent_chats(limit, platform)
    if not rows:
        click.echo("No chats found.")
        return
    show_tags = platform is None
    name_width = max(len(r["name"]) for r in rows)
    for r in rows:
        name_col = click.style(r["name"].ljust(name_width), bold=True)
        ts_col = click.style(r["last_message"], fg=DIM)
        extras = []
        if show_tags:
            extras.append(_platform_tag(r["platform"]))
        if r["username"]:
            extras.append(click.style(f"@{r['username']}", fg=DIM))
        if r["phone"] and r["name"] != r["phone"]:
            extras.append(click.style(r["phone"], fg=DIM))
        extra = "  ".join(extras)
        if extra:
            extra = "  " + extra
        click.echo(f"{name_col}  {ts_col}{extra}")


@chats.command("find")
@click.argument("query")
@platform_option
def chats_find(query: str, platform: str | None):
    """Find chats by name, phone, or username."""
    rows = backends.find_chats(query, platform)
    if not rows:
        click.echo("No chats found.")
        return
    show_tags = platform is None
    name_width = max(len(r["name"]) for r in rows)
    for r in rows:
        name_col = click.style(r["name"].ljust(name_width), bold=True)
        extras = []
        if show_tags:
            extras.append(_platform_tag(r["platform"]))
        if r["username"]:
            extras.append(click.style(f"@{r['username']}", fg=DIM))
        if r["phone"] and r["name"] != r["phone"]:
            extras.append(click.style(r["phone"], fg=DIM))
        extra = "  ".join(extras)
        if extra:
            extra = "  " + extra
        click.echo(f"{name_col}{extra}")


# --- read ---


@cli.command("read")
@click.argument("identifier")
@click.option("--limit", default=20, help="Number of messages to show.")
@click.option("--full", is_flag=True, help="Show full message text without truncation.")
@platform_option
def read_cmd(identifier: str, limit: int, full: bool, platform: str | None):
    """Read messages from a chat. Accepts names, phones, or chat IDs."""
    messages = backends.read_messages(identifier, limit, platform)
    if not messages:
        click.echo("No messages found.")
        return
    for m in reversed(messages):
        click.echo(_format_message(m, full))


# --- search ---


@cli.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Number of results to show.")
@click.option("--full", is_flag=True, help="Show full message text without truncation.")
@platform_option
def search_cmd(query: str, limit: int, full: bool, platform: str | None):
    """Search message content across platforms."""
    results = backends.search_messages(query, limit, platform)
    if not results:
        click.echo("No messages found.")
        return
    show_tags = platform is None
    for r in results:
        ts = click.style(r["timestamp"], fg=DIM)
        chat = click.style(r["chat_name"], fg=SENDER_OTHER)
        sender_text = r["sender"] if r["sender"] == "Me" else _format_phone(r["sender"])
        sender = click.style(sender_text, fg=SENDER_ME if r["sender"] == "Me" else SENDER_OTHER)
        text = _truncate(r["text"], full)
        tag = f"  {_platform_tag(r['platform'])}" if show_tags else ""
        click.echo(f"{ts}  {chat}  {sender}  {text}{tag}")


# --- send ---


@cli.command("send")
@click.argument("recipient")
@click.argument("message")
@click.option("--confirm", is_flag=True, help="Actually send (required).")
@platform_option
def send_cmd(recipient: str, message: str, confirm: bool, platform: str | None):
    """Send a message. Requires --confirm flag."""
    if not confirm:
        plat, display = backends.resolve_send_target(recipient, platform)
        tag = _platform_tag(plat)
        if plat == "messages":
            display = _format_phone(display)
        click.echo(f"Would send {tag} to {click.style(display, fg=SENDER_OTHER)}: {message}")
        click.echo(f"Pass {click.style('--confirm', bold=True)} to actually send.")
        return
    plat, result = backends.send_message(recipient, message, platform)
    click.echo(result)


# --- stats ---


@cli.command("stats")
@platform_option
def stats_cmd(platform: str | None):
    """Show message database statistics."""
    rows = backends.stats(platform)
    if not rows:
        click.echo("No platforms available.")
        return
    for r in rows:
        label = click.style(r["platform"], bold=True)
        click.echo(f"{label}  Messages: {r['messages']:,}  Chats: {r['chats']:,}")
