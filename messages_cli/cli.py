"""CLI entry point for messages-cli."""

import click

from . import db, send


@click.group()
def cli():
    """CLI for macOS Messages — read, search, and send iMessages."""


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
        name_str = f"{first} {last}".strip()
        click.echo(name_str)
        for phone in c["phones"]:
            click.echo(f"  phone: {phone}")
        for email in c["emails"]:
            click.echo(f"  email: {email}")


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
    for r in rows:
        name = r["display_name"] or ""
        click.echo(f"{r['chat_identifier']}  {name}  {r['last_msg']}")


@chats.command("find")
@click.argument("identifier")
def chats_find(identifier: str):
    """Find DM/group chats for a phone number or name."""
    rows = db.find_chats(identifier)
    if not rows:
        click.echo("No chats found.")
        return
    for r in rows:
        name = r["display_name"] or ""
        click.echo(f"{r['chat_identifier']}  {name}  (ROWID={r['ROWID']})")


# --- read ---


@cli.command("read")
@click.argument("chat_id")
@click.option("--limit", default=20, help="Number of messages to show.")
def read_cmd(chat_id: str, limit: int):
    """Read messages from a chat."""
    messages = db.read_messages(chat_id, limit)
    if not messages:
        click.echo("No messages found.")
        return
    for m in reversed(messages):
        edited = " [edited]" if m["edited"] else ""
        click.echo(f"{m['timestamp']} | {m['sender']}{edited} | {m['text']}")


# --- search ---


@cli.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Number of results to show.")
def search_cmd(query: str, limit: int):
    """Search message content."""
    results = db.search_messages(query, limit)
    if not results:
        click.echo("No messages found.")
        return
    for r in results:
        name = r["display_name"] or r["chat_identifier"]
        click.echo(f"{r['timestamp']} | {name} | {r['sender']} | {r['text']}")


# --- send ---


@cli.command("send")
@click.argument("phone")
@click.argument("message")
@click.option("--confirm", is_flag=True, help="Actually send (required).")
def send_cmd(phone: str, message: str, confirm: bool):
    """Send an iMessage. Requires --confirm flag."""
    if not confirm:
        click.echo(f"Would send to {phone}: {message}")
        click.echo("Pass --confirm to actually send.")
        return
    result = send.send_message(phone, message)
    click.echo(result)
