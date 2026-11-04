"""CLI entry point for messages-cli."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime

import click

from . import backends, db, messenger_auth, whatsapp_auth
from .models import BackendError, Chat, Message, SearchHit
from .utils import GO_MODULES, INSTALLED_BIN_DIR, format_phone, go_source_dir

DIM = "bright_black"
SENDER_ME = "green"
SENDER_OTHER = "cyan"
EDITED = "yellow"
REACTION = "magenta"
ATTACHMENT = "yellow"

PLATFORM_TAGS = {
    "messages": "ms",
    "telegram": "tg",
    "whatsapp": "wa",
    "messenger": "fb",
}

_TAG_RE = re.compile(r"\[[^\[\]]*\]")
_ATTACHMENT_PREFIXES = ("[image", "[video", "[audio", "[file", "[sticker", "[document", "[location", "[media")
_REACTION_PREFIXES = tuple(f"[{label}]" for label in db.TAPBACK_TYPES.values())


class _Group(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except BackendError as e:
            raise click.ClickException(str(e)) from e


def platform_option(fn):
    return click.option(
        "--platform", "-p", type=click.Choice(backends.PLATFORMS), default=None,
        help="Only use this platform.",
    )(fn)


def json_option(fn):
    return click.option("--json", "as_json", is_flag=True, help="Print JSON.")(fn)


def _print_json(records) -> None:
    click.echo(json.dumps(
        [r if isinstance(r, dict) else r.to_dict() for r in records],
        indent=2, ensure_ascii=False,
    ))


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
    return click.style(f"[{PLATFORM_TAGS.get(platform, platform[:2])}]", fg=DIM)


def _display_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts


def _style_tag(match: re.Match) -> str:
    tag = match.group(0)
    if tag.startswith(_ATTACHMENT_PREFIXES):
        return click.style(tag, fg=ATTACHMENT)
    if tag.startswith(_REACTION_PREFIXES):
        return click.style(tag, fg=REACTION)
    if tag == "[...]":
        return click.style(tag, fg=DIM)
    return tag


def _sender(m: Message) -> str:
    text = m.sender if m.is_from_me else format_phone(m.sender)
    sender = click.style(text, fg=SENDER_ME if m.is_from_me else SENDER_OTHER)
    if m.edited:
        sender += click.style(" [edited]", fg=EDITED)
    return sender


def _format_message(m: Message, full: bool) -> str:
    ts = click.style(_display_ts(m.timestamp), fg=DIM)
    text = _TAG_RE.sub(_style_tag, _truncate(m.text, full))
    return f"{ts}  {_sender(m)}  {text}"


def _format_hit(h: SearchHit, full: bool, show_tag: bool) -> str:
    ts = click.style(_display_ts(h.timestamp), fg=DIM)
    chat = click.style(h.chat_name, fg=SENDER_OTHER)
    tag = f"  {_platform_tag(h.platform)}" if show_tag else ""
    return f"{ts}  {chat}  {_sender(h)}  {_truncate(h.text, full)}{tag}"


def _print_chats(chats: list[Chat], show_tags: bool, show_time: bool) -> None:
    name_width = max(len(c.name) for c in chats)
    for c in chats:
        columns = [click.style(c.name.ljust(name_width), bold=True)]
        if show_time:
            columns.append(click.style(_display_ts(c.last_message), fg=DIM))
        if show_tags:
            columns.append(_platform_tag(c.platform))
        if c.username:
            columns.append(click.style(f"@{c.username}", fg=DIM))
        if c.phone and c.name != c.phone:
            columns.append(click.style(c.phone, fg=DIM))
        click.echo("  ".join(columns))


@click.group(cls=_Group)
def cli():
    """Unified CLI for all your messaging apps."""


# --- contacts ---


@cli.group()
def contacts():
    """Search contacts."""


@contacts.command("search")
@click.argument("name")
@json_option
def contacts_search(name: str, as_json: bool):
    """Search macOS Contacts by name."""
    results = db.search_contacts(name)
    if as_json:
        _print_json(results)
        return
    if not results:
        click.echo("No contacts found.")
        return
    for c in results:
        click.echo(click.style(c.name, bold=True))
        for phone in c.phones:
            click.echo(f"  {click.style('phone:', fg=DIM)} {format_phone(phone)}")
        for email in c.emails:
            click.echo(f"  {click.style('email:', fg=DIM)} {email}")


# --- chats ---


@cli.group()
def chats():
    """List and find chats."""


@chats.command("recent")
@click.option("--limit", default=20, help="Number of chats to show.")
@platform_option
@json_option
def chats_recent(limit: int, platform: str | None, as_json: bool):
    """List recent chats."""
    rows = backends.recent_chats(limit, platform)
    if as_json:
        _print_json(rows)
    elif rows:
        _print_chats(rows, show_tags=platform is None, show_time=True)
    else:
        click.echo("No chats found.")


@chats.command("find")
@click.argument("query")
@platform_option
@json_option
def chats_find(query: str, platform: str | None, as_json: bool):
    """Find chats by name, phone, or username."""
    rows = backends.find_chats(query, platform)
    if as_json:
        _print_json(rows)
    elif rows:
        _print_chats(rows, show_tags=platform is None, show_time=False)
    else:
        click.echo("No chats found.")


# --- read ---


@cli.command("read")
@click.argument("identifier")
@click.option("--limit", default=20, help="Number of messages to show.")
@click.option("--full", is_flag=True, help="Show full message text without truncation.")
@platform_option
@json_option
def read_cmd(identifier: str, limit: int, full: bool, platform: str | None, as_json: bool):
    """Read messages from a chat. Accepts names, phones, or chat IDs."""
    _chat, messages = backends.read_messages(identifier, limit, platform)
    messages = list(reversed(messages))
    if as_json:
        _print_json(messages)
    elif messages:
        for m in messages:
            click.echo(_format_message(m, full))
    else:
        click.echo("No messages found.")


# --- search ---


@cli.command("search")
@click.argument("query")
@click.option("--limit", default=20, help="Number of results to show.")
@click.option("--full", is_flag=True, help="Show full message text without truncation.")
@click.option("--chat", default=None, help="Scope search to a specific chat (name, phone, or ID).")
@click.option("--context", "-C", default=0, type=click.IntRange(min=0), help="Show N surrounding messages for each hit.")
@platform_option
@json_option
def search_cmd(
    query: str, limit: int, full: bool, chat: str | None, context: int,
    platform: str | None, as_json: bool,
):
    """Search message content across platforms."""
    hits = backends.search_messages(query, limit, platform, chat=chat)
    if as_json:
        if context:
            _print_json([
                {**h.to_dict(), "context": [m.to_dict() for m in backends.read_around(h, context)]}
                for h in hits
            ])
        else:
            _print_json(hits)
        return
    if not hits:
        click.echo("No messages found.")
        return
    show_tags = platform is None

    if not context:
        for h in hits:
            click.echo(_format_hit(h, full, show_tags))
        return

    for i, h in enumerate(hits):
        if i > 0:
            click.echo()
        tag = f" {_platform_tag(h.platform)}" if show_tags else ""
        click.echo(click.style(f"--- {h.chat_name}{tag} ---", fg=DIM))
        surrounding = backends.read_around(h, context) or [h]
        for m in surrounding:
            line = _format_message(m, full)
            if m.id == h.id:
                line += f"  {click.style('<--', fg='yellow')}"
            click.echo(line)


# --- send ---


@cli.command("send")
@click.argument("recipient")
@click.argument("message")
@click.option("--confirm", is_flag=True, help="Actually send (required).")
@platform_option
def send_cmd(recipient: str, message: str, confirm: bool, platform: str | None):
    """Send a message. Requires --confirm flag."""
    backend, target = backends.resolve_send_target(recipient, platform)
    if not confirm:
        display = click.style(format_phone(target.display), fg=SENDER_OTHER)
        if target.detail and target.detail != target.display:
            display += click.style(f" ({target.detail})", fg=DIM)
        click.echo(f"Would send {_platform_tag(backend.name)} to {display}: {message}")
        click.echo(f"Pass {click.style('--confirm', bold=True)} to actually send.")
        return
    click.echo(backend.send_message(target, message))


# --- stats ---


@cli.command("stats")
@platform_option
@json_option
def stats_cmd(platform: str | None, as_json: bool):
    """Show message database statistics."""
    rows = backends.stats(platform)
    if as_json:
        _print_json(rows)
        return
    if not rows:
        click.echo("No platforms available.")
        return
    width = max(len(r["platform"]) for r in rows)
    for r in rows:
        label = click.style(r["platform"].ljust(width), bold=True)
        click.echo(f"{label}  Messages: {r['messages']:,}  Chats: {r['chats']:,}")


# --- auth ---


@cli.group()
def auth():
    """Authenticate with messaging platforms."""


@auth.command("whatsapp")
def auth_whatsapp():
    """Authenticate with WhatsApp (QR code pairing)."""
    raise SystemExit(whatsapp_auth.run_auth())


@auth.command("messenger")
def auth_messenger():
    """Authenticate with Facebook Messenger (browser login)."""
    messenger_auth.run_auth()


# --- setup ---


@cli.command("setup")
def setup_cmd():
    """Build the Go helper tools (WhatsApp send/auth, Messenger pagination)."""
    source = go_source_dir()
    if source is None:
        raise click.ClickException("Go sources not found in this installation.")
    if shutil.which("go") is None:
        raise click.ClickException("Go is not installed. Install it with: brew install go")
    INSTALLED_BIN_DIR.mkdir(parents=True, exist_ok=True)
    for module in GO_MODULES:
        click.echo(f"Building {module} tools into {INSTALLED_BIN_DIR} ...")
        result = subprocess.run(["go", "build", "-o", f"{INSTALLED_BIN_DIR}/", "./cmd/..."], cwd=source / module)
        if result.returncode != 0:
            raise click.ClickException(f"go build failed for the {module} tools.")
    click.echo("Done.")
