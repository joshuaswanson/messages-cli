"""Facebook Messenger API client using cookie-based authentication.

Reads conversations and messages from messenger.com by authenticating with
browser cookies and calling Facebook's internal Lightspeed GraphQL API.
Uses fb-fetch-tool (Go binary) for message pagination via MQTT WebSocket.
"""

import concurrent.futures
import datetime
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import requests

COOKIES_PATH = Path.home() / ".config/messages-cli/messenger_cookies.json"


def is_available() -> bool:
    """Check if Messenger cookies are configured."""
    return COOKIES_PATH.exists()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class MessengerSession:
    """Authenticated session with messenger.com."""

    def __init__(self):
        self._session = requests.Session()
        self._dtsg: str | None = None
        self._device_id: str | None = None
        self._schema_version: str | None = None
        self._query_id: str | None = None
        self._users: dict[int, str] = {}
        self._my_user_id: int | None = None

    def connect(self) -> None:
        """Load cookies and extract API tokens from messenger.com."""
        if not COOKIES_PATH.exists():
            print(
                "Messenger cookies not found. "
                "Run 'messages auth messenger' to set up.",
                file=sys.stderr,
            )
            sys.exit(1)

        with open(COOKIES_PATH) as f:
            cookies = json.load(f)
        self._session.cookies.update(cookies)

        # Fetch messenger.com to get auth tokens
        page = self._session.get("https://www.messenger.com", allow_redirects=True)
        if "login" in page.url:
            print(
                "Messenger cookies expired. "
                "Update cookies in ~/.config/messages-cli/messenger_cookies.json",
                file=sys.stderr,
            )
            sys.exit(1)

        dtsg_match = re.search(r'DTSG.{,20}"token":"([^"]+)"', page.text)
        device_match = re.search(r'"(?:deviceId|clientID)"\s*:\s*"([^"]+)"', page.text)
        if not dtsg_match or not device_match:
            print("Failed to extract Messenger auth tokens.", file=sys.stderr)
            sys.exit(1)

        self._dtsg = dtsg_match.group(1)
        self._device_id = device_match.group(1)

        # Schema version from page or JS bundles
        sv_match = re.search(r'\\"version\\":([0-9]{2,})', page.text)
        self._schema_version = sv_match.group(1) if sv_match else None

        # Find query_id from JS bundles
        script_urls = sorted(set(re.findall(r'"(https://static[^"]+)"', page.text)))
        script_urls = [u for u in script_urls if ".js" in u or "/rsrc.php/" in u]

        def fetch_script(url):
            try:
                return url, requests.get(url, timeout=10).text
            except Exception:
                return url, ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(fetch_script, script_urls))

        for _url, text in results:
            if "LSPlatformGraphQLLightspeedRequestQuery" not in text:
                continue
            qid_match = re.search(
                r'id:\s*"([0-9]+)".{,50}name:\s*"LSPlatformGraphQLLightspeedRequestQuery"',
                text,
            )
            if qid_match:
                self._query_id = qid_match.group(1)
            sv2_match = re.search(
                r'__d\s*\(\s*"LSVersion".{,50}exports\s*=\s*"([0-9]+)"', text
            )
            if sv2_match and not self._schema_version:
                self._schema_version = sv2_match.group(1)
            break

        if not self._query_id or not self._schema_version:
            print("Failed to find Messenger API query ID.", file=sys.stderr)
            sys.exit(1)

        # Store own user ID from cookies
        c_user = cookies.get("c_user")
        if c_user:
            self._my_user_id = int(c_user)

    def _graphql_request(self, request_type: int, request_payload: dict) -> dict:
        """Make a Lightspeed GraphQL request."""
        resp = self._session.post(
            "https://www.messenger.com/api/graphql/",
            data={
                "doc_id": self._query_id,
                "fb_dtsg": self._dtsg,
                "variables": json.dumps({
                    "deviceId": self._device_id,
                    "requestId": 0,
                    "requestPayload": json.dumps(request_payload),
                    "requestType": request_type,
                }),
            },
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_thread_page(self, thread_id: str) -> list[dict]:
        """Fetch a thread page and extract all lightspeed payloads."""
        resp = self._session.get(
            f"https://www.messenger.com/t/{thread_id}/",
            allow_redirects=True,
        )
        resp.raise_for_status()

        # Extract all lightspeed payloads embedded in the page HTML
        raw_payloads = re.findall(
            r'"lightspeed_web_request":\{"payload":"((?:[^"\\]|\\.)*)"',
            resp.text,
        )
        parsed = []
        for raw in raw_payloads:
            try:
                unescaped = json.loads('"' + raw + '"')
                parsed.append(json.loads(unescaped))
            except (json.JSONDecodeError, ValueError):
                pass
        return parsed

    def _fetch_inbox(self) -> dict:
        """Fetch inbox data and return parsed payload."""
        result = self._graphql_request(1, {
            "database": 1,
            "version": self._schema_version,
            "sync_params": json.dumps({}),
        })
        payload_str = result["data"]["viewer"]["lightspeed_web_request"]["payload"]
        return json.loads(payload_str)

    def _send_tasks(self, tasks: list[dict]) -> None:
        """Send task-based requests (send message, mark read, etc.)."""
        timestamp = int(datetime.datetime.now().timestamp() * 1000)
        epoch = timestamp << 22
        self._graphql_request(3, {
            "version_id": self._schema_version,
            "epoch_id": epoch,
            "tasks": tasks,
        })


# ---------------------------------------------------------------------------
# MQTT-based message fetching (via fb-fetch-tool Go binary)
# ---------------------------------------------------------------------------

_FB_FETCH_BINARY = Path(__file__).parent.parent / "fb-fetch-tool" / "fb-fetch"


def _fetch_older_messages(thread_id: int, ref_timestamp_ms: int,
                           ref_message_id: str) -> list[dict]:
    """Fetch older messages via the fb-fetch-tool Go binary (MQTT WebSocket).

    Returns a list of {"text", "timestamp_ms", "sender_id", "message_id"} dicts.
    """
    if not _FB_FETCH_BINARY.exists():
        return []

    try:
        result = subprocess.run(
            [
                str(_FB_FETCH_BINARY),
                str(COOKIES_PATH),
                str(thread_id),
                str(ref_timestamp_ms),
                ref_message_id,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


# ---------------------------------------------------------------------------
# Lightspeed payload parsing
# ---------------------------------------------------------------------------

def _val(a):
    """Convert a Lightspeed value to a Python value."""
    if isinstance(a, list) and len(a) == 2 and a[0] == 19:
        return int(a[1])
    if isinstance(a, list) and len(a) == 1 and a[0] == 9:
        return None
    return a


def _find_calls(obj: object, calls: dict | None = None) -> dict[str, list]:
    """Walk the JSON tree and find all [5, "functionName", ...args] calls."""
    if calls is None:
        calls = {}
    if isinstance(obj, list):
        if len(obj) >= 2 and obj[0] == 5 and isinstance(obj[1], str):
            args = [_val(a) for a in obj[2:]]
            calls.setdefault(obj[1], []).append(args)
        else:
            for item in obj:
                _find_calls(item, calls)
    elif isinstance(obj, dict):
        for v in obj.values():
            _find_calls(v, calls)
    return calls


def _parse_inbox(payload: dict, my_user_id: int | None = None) -> tuple[dict, dict, list]:
    """Parse inbox payload into (threads, users, messages)."""
    calls = _find_calls(payload)

    users: dict[int, str] = {}
    threads: dict[int, dict] = {}

    for args in calls.get("verifyContactRowExists", []):
        user_id = args[0]
        name = args[3] if len(args) > 3 else None
        if user_id is not None and name:
            users[user_id] = name

    for args in calls.get("deleteThenInsertThread", []):
        last_sent_ts = args[0]  # timestamp ms
        last_read_ts = args[1]
        last_msg_snippet = args[2]
        group_name = args[3] if len(args) > 3 else None
        thread_id = args[7] if len(args) > 7 else None
        last_msg_author = args[18] if len(args) > 18 else None

        if thread_id is None:
            continue

        is_group = group_name is not None
        threads[thread_id] = {
            "name": group_name,
            "last_message": last_msg_snippet,
            "last_sent_ts": last_sent_ts,
            "last_read_ts": last_read_ts,
            "last_msg_author_id": last_msg_author,
            "is_group": is_group,
            "participants": [],
        }

    for args in calls.get("addParticipantIdToGroupThread", []):
        thread_id = args[0] if len(args) > 0 else None
        user_id = args[1] if len(args) > 1 else None
        participant_name = args[5] if len(args) > 5 else None
        if thread_id and thread_id in threads:
            threads[thread_id]["participants"].append(user_id)
        # Also pick up names from group participants
        if user_id and participant_name and user_id not in users:
            users[user_id] = participant_name

    # Resolve thread names from participants for unnamed threads
    for thread_id, thread in threads.items():
        if thread["name"]:
            continue
        others = [uid for uid in thread["participants"] if uid != my_user_id]
        if not others:
            # Try thread_id as user_id (DMs)
            if thread_id in users:
                thread["name"] = users[thread_id]
            continue
        if len(others) == 1:
            # DM: use the other person's name
            thread["name"] = users.get(others[0], str(others[0]))
        else:
            # Unnamed group: join first few participant names
            names = [users.get(uid, str(uid)) for uid in others[:3]]
            thread["name"] = ", ".join(names)
            if len(others) > 3:
                thread["name"] += f" +{len(others) - 3}"

    # Parse messages from upsertMessage
    messages: list[dict] = []
    for args in calls.get("upsertMessage", []):
        text = args[0] if len(args) > 0 else None
        msg_thread_id = args[3] if len(args) > 3 else None
        timestamp = args[5] if len(args) > 5 else None
        author_id = args[10] if len(args) > 10 else None
        if text and msg_thread_id:
            messages.append({
                "text": text,
                "thread_id": msg_thread_id,
                "timestamp": timestamp,
                "author_id": author_id,
            })

    return threads, users, messages


def _ts_to_datetime(ts: int | None) -> str:
    """Convert Facebook timestamp (ms) to datetime string."""
    if ts is None:
        return ""
    try:
        dt = datetime.datetime.fromtimestamp(ts / 1000)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ""


# ---------------------------------------------------------------------------
# Singleton session
# ---------------------------------------------------------------------------

_session: MessengerSession | None = None


def _get_session() -> MessengerSession:
    global _session
    if _session is None:
        _session = MessengerSession()
        _session.connect()
    return _session


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def recent_chats(limit: int = 20) -> list[dict]:
    """List recent Messenger conversations."""
    sess = _get_session()
    payload = sess._fetch_inbox()
    threads, users, _messages = _parse_inbox(payload, sess._my_user_id)

    results = []
    sorted_threads = sorted(
        threads.items(),
        key=lambda x: x[1]["last_sent_ts"] or 0,
        reverse=True,
    )

    for thread_id, thread in sorted_threads[:limit]:
        name = thread["name"] or users.get(thread_id, str(thread_id))
        results.append({
            "name": name,
            "thread_id": str(thread_id),
            "last_message": _ts_to_datetime(thread["last_sent_ts"]),
            "is_group": thread["is_group"],
        })

    return results


def find_chats(query: str) -> list[dict]:
    """Find Messenger chats by name."""
    query_lower = query.lower()
    sess = _get_session()
    payload = sess._fetch_inbox()
    threads, users, _messages = _parse_inbox(payload, sess._my_user_id)

    results = []
    for thread_id, thread in threads.items():
        name = thread["name"] or users.get(thread_id, str(thread_id))
        if name and query_lower in name.lower():
            results.append({
                "name": name,
                "thread_id": str(thread_id),
                "is_group": thread["is_group"],
            })

    return results


def resolve_identifier(identifier: str) -> str | None:
    """Resolve a name or thread ID to a Messenger thread ID."""
    # If it's already numeric, treat as thread ID
    if identifier.isdigit():
        return identifier
    chats = find_chats(identifier)
    if chats:
        return chats[0]["thread_id"]
    return None


def read_messages(thread_id: str, limit: int = 20) -> list[dict]:
    """Read messages from a Messenger thread.

    Fetches the thread page (~20 recent messages), then uses the fb-fetch-tool
    Go binary to load older messages via MQTT if more are needed.
    """
    sess = _get_session()
    my_user_id = sess._my_user_id
    tid = int(thread_id)

    # Fetch the thread page which embeds message history
    page_payloads = sess._fetch_thread_page(thread_id)

    # Merge all calls from all payloads
    all_calls: dict[str, list] = {}
    for payload in page_payloads:
        calls = _find_calls(payload)
        for fn, arglist in calls.items():
            all_calls.setdefault(fn, []).extend(arglist)

    # Build user lookup
    users: dict[int, str] = {}
    for args in all_calls.get("verifyContactRowExists", []):
        uid = args[0]
        name = args[3] if len(args) > 3 else None
        if uid is not None and name:
            users[uid] = name

    # Build attachment map: message_id -> list of attachment descriptions
    attach_map: dict[str, list[str]] = {}
    for args in all_calls.get("insertBlobAttachment", []):
        if len(args) <= 32 or args[27] != tid:
            continue
        msg_id = args[32]
        atype = args[29]  # 2=image, 4=video
        fname = args[0] or ""
        if atype == 4 or fname.startswith("video"):
            attach_map.setdefault(msg_id, []).append("video")
        else:
            attach_map.setdefault(msg_id, []).append("image")

    for args in all_calls.get("insertStickerAttachment", []):
        if len(args) <= 18 or args[14] != tid:
            continue
        msg_id = args[18]
        label = args[13] if len(args) > 13 and args[13] else "sticker"
        attach_map.setdefault(msg_id, []).append(f"sticker: {label}")

    # Get messages for this thread
    messages = []
    seen_ids = set()
    for args in all_calls.get("upsertMessage", []):
        if len(args) <= 10:
            continue
        msg_thread_id = args[3]
        if msg_thread_id != tid:
            continue
        text = args[0]
        timestamp = args[5]
        author_id = args[10]
        msg_id = args[8] if len(args) > 8 else None
        attachments = attach_map.get(msg_id, []) if msg_id else []
        key = (timestamp, author_id, text)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        if not text and not attachments:
            continue
        messages.append({
            "text": text,
            "timestamp": timestamp,
            "author_id": author_id,
            "attachments": attachments,
        })

    # If we need more messages, use MQTT pagination via fb-fetch-tool
    if len(messages) < limit and _FB_FETCH_BINARY.exists():
        # Find pagination cursor from insertNewMessageRange
        # Pick the entry with the smallest min_timestamp (oldest message boundary)
        ref_ts = None
        ref_msg_id = None
        for args in all_calls.get("insertNewMessageRange", []):
            if args[0] == tid and len(args) > 7 and args[7]:  # has_more_before
                ts = args[1] if len(args) > 1 else None
                mid = args[3] if len(args) > 3 else None
                if ts and mid and (ref_ts is None or ts < ref_ts):
                    ref_ts = ts
                    ref_msg_id = mid

        if ref_ts and ref_msg_id:
            older = _fetch_older_messages(tid, ref_ts, ref_msg_id)
            for m in older:
                key = (m["timestamp_ms"], m["sender_id"], m["text"])
                if key not in seen_ids:
                    seen_ids.add(key)
                    messages.append({
                        "text": m["text"],
                        "timestamp": m["timestamp_ms"],
                        "author_id": m["sender_id"],
                    })

    messages.sort(key=lambda m: m["timestamp"] or 0, reverse=True)

    results = []
    for m in messages[:limit]:
        author_id = m["author_id"]
        if author_id == my_user_id:
            sender = "Me"
        else:
            sender = users.get(author_id, str(author_id) if author_id else "Unknown")
        text = m["text"] or ""
        for att in m.get("attachments", []):
            text = f"{text} [{att}]" if text else f"[{att}]"
        results.append({
            "timestamp": _ts_to_datetime(m["timestamp"]),
            "sender": sender,
            "text": text,
        })

    return results


def search_messages(query: str, limit: int = 20) -> list[dict]:
    """Search messages across Messenger threads.

    Note: Only searches messages in the initial inbox sync (recent messages).
    """
    query_lower = query.lower()
    sess = _get_session()
    payload = sess._fetch_inbox()
    threads, users, messages = _parse_inbox(payload, sess._my_user_id)

    my_user_id = sess._my_user_id
    results = []

    for m in messages:
        if not m["text"] or query_lower not in m["text"].lower():
            continue

        tid = m["thread_id"]
        thread = threads.get(tid, {})
        chat_name = thread.get("name") or users.get(tid, str(tid))

        author_id = m["author_id"]
        if author_id == my_user_id:
            sender = "Me"
        else:
            sender = users.get(author_id, str(author_id) if author_id else "Unknown")

        results.append({
            "timestamp": _ts_to_datetime(m["timestamp"]),
            "chat_name": chat_name,
            "sender": sender,
            "text": m["text"],
        })

    results.sort(key=lambda x: x["timestamp"], reverse=True)
    return results[:limit]


def send_message(thread_id: str, text: str) -> str:
    """Send a message to a Messenger thread."""
    sess = _get_session()
    timestamp = int(datetime.datetime.now().timestamp() * 1000)
    epoch = timestamp << 22
    otid = epoch + random.randrange(2**22)

    sess._send_tasks([
        {
            "label": "46",
            "payload": json.dumps({
                "thread_id": int(thread_id),
                "otid": str(otid),
                "source": 0,
                "send_type": 1,
                "text": text,
                "initiating_source": 1,
            }),
            "queue_name": thread_id,
            "task_id": 0,
        },
    ])
    return "Message sent."


def stats() -> dict:
    """Return basic Messenger stats from the inbox."""
    sess = _get_session()
    payload = sess._fetch_inbox()
    threads, users, messages = _parse_inbox(payload, sess._my_user_id)
    return {
        "messages": len(messages),
        "chats": len(threads),
    }
