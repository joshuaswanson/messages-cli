"""CLI entry point for messages-cli."""

import click

from . import db, send

# Colors
DIM = "bright_black"
SENDER_ME = "green"
SENDER_OTHER = "cyan"
EDITED = "yellow"
REACTION = "magenta"
ATTACHMENT = "yellow"


def _truncate(text: str, full: bool) -> str:
    if full:
        return text
    first_line = text.split("\n")[0]
    if len(first_line) > 120:
        return first_line[:117] + "..."
    if "\n" in text:
        return first_line + " [...]"
    return first_line


def _format_message(m: dict, full: bool) -> str:
    ts = click.style(m["timestamp"], fg=DIM)
    is_me = m["sender"] == "Me"
    sender = click.style(m["sender"], fg=SENDER_ME if is_me else SENDER_OTHER)
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
    """CLI for macOS Messages."""


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
            click.echo(f"  {click.style('phone:', fg=DIM)} {phone}")
        for email in c["emails"]:
            click.echo(f"  {click.style('email:', fg=DIM)} {email}")


# --- chats ---


@cli.group()
def chats():
    """List and find chats."""


@chats.command("recent")
@click.option("--limit", default=20, help="Number of chats to show.")
def chats_recent(limit: int):
    """List recent chats."""
    rows = db.recent_chats(limit)
    if not rows:
        click.echo("No chats found.")
        return
    # Calculate column widths
    ids = [r["chat_identifier"] for r in rows]
    names = [r["display_name"] or "" for r in rows]
    id_width = max(len(i) for i in ids)
    name_width = max((len(n) for n in names if n), default=0)
    for r, cid, name in zip(rows, ids, names):
        id_col = click.style(cid.ljust(id_width), fg=SENDER_OTHER)
        name_col = click.style(name.ljust(name_width), bold=True) + "  " if name else " " * (name_width + 2) if name_width else ""
        ts_col = click.style(r["last_msg"], fg=DIM)
        click.echo(f"{id_col}  {name_col}{ts_col}")


@chats.command("find")
@click.argument("identifier")
def chats_find(identifier: str):
    """Find DM/group chats for a phone number or name."""
    rows = db.find_chats(identifier)
    if not rows:
        click.echo("No chats found.")
        return
    ids = [r["chat_identifier"] for r in rows]
    id_width = max(len(i) for i in ids)
    for r, cid in zip(rows, ids):
        id_col = click.style(cid.ljust(id_width), fg=SENDER_OTHER)
        name = r["display_name"] or ""
        name_col = click.style(name, bold=True) if name else ""
        click.echo(f"{id_col}  {name_col}")


# --- read ---


@cli.command("read")
@click.argument("chat_id")
@click.option("--limit", default=20, help="Number of messages to show.")
@click.option("--full", is_flag=True, help="Show full message text without truncation.")
def read_cmd(chat_id: str, limit: int, full: bool):
    """Read messages from a chat."""
    chat_id = db.resolve_identifier(chat_id)
    messages = db.read_messages(chat_id, limit)
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
def search_cmd(query: str, limit: int, full: bool):
    """Search message content."""
    results = db.search_messages(query, limit)
    if not results:
        click.echo("No messages found.")
        return
    for r in results:
        ts = click.style(r["timestamp"], fg=DIM)
        chat = click.style(r["display_name"] or r["chat_identifier"], fg=SENDER_OTHER)
        sender = click.style(r["sender"], fg=SENDER_ME if r["sender"] == "Me" else SENDER_OTHER)
        text = _truncate(r["text"], full)
        click.echo(f"{ts}  {chat}  {sender}  {text}")


# --- send ---


@cli.command("send")
@click.argument("phone")
@click.argument("message")
@click.option("--confirm", is_flag=True, help="Actually send (required).")
def send_cmd(phone: str, message: str, confirm: bool):
    """Send an iMessage. Requires --confirm flag."""
    phone = db.resolve_identifier(phone)
    if not confirm:
        click.echo(f"Would send to {click.style(phone, fg=SENDER_OTHER)}: {message}")
        click.echo(f"Pass {click.style('--confirm', bold=True)} to actually send.")
        return
    result = send.send_message(phone, message)
    click.echo(result)
