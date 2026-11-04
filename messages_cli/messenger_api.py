"""Facebook Messenger API client using cookie-based authentication.

Reads conversations and messages from messenger.com by authenticating with
browser cookies and calling Facebook's internal Lightspeed GraphQL API.
Uses the fb-fetch and fb-threads Go binaries for pagination via MQTT WebSocket.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import random
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import requests

from .models import BackendError, Chat, Message, SearchHit
from .utils import choose_chat, find_tool, format_ts_ms

PLATFORM = "messenger"
COOKIES_PATH = Path.home() / ".config/messages-cli/messenger_cookies.json"
_CACHE_DIR = Path.home() / ".cache/messages-cli"
_IMAGE_CACHE_DIR = _CACHE_DIR / "messenger"
_API_CACHE_PATH = _CACHE_DIR / "messenger_api.json"
_TIMEOUT = 30


def is_available() -> bool:
    """Check if Messenger cookies are configured."""
    return COOKIES_PATH.exists()


class MessengerAPIError(BackendError):
    """Facebook answered, but rejected the request or returned something unexpected."""


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


def _arg(args: list, index: int):
    return args[index] if len(args) > index else None


def _parse_users(calls: dict[str, list]) -> dict[int, str]:
    users: dict[int, str] = {}
    for args in calls.get("verifyContactRowExists", []):
        user_id, name = _arg(args, 0), _arg(args, 3)
        if user_id is not None and name:
            users[user_id] = name
    return users


@dataclass
class Inbox:
    threads: dict[int, dict]
    users: dict[int, str]
    messages: list[dict]

    def thread_name(self, thread_id: int) -> str:
        thread = self.threads.get(thread_id, {})
        return thread.get("name") or self.users.get(thread_id, str(thread_id))


def _parse_inbox(payload: dict, my_user_id: int | None = None) -> Inbox:
    calls = _find_calls(payload)
    users = _parse_users(calls)
    threads: dict[int, dict] = {}

    for args in calls.get("deleteThenInsertThread", []):
        thread_id = _arg(args, 7)
        if thread_id is None:
            continue
        group_name = _arg(args, 3)
        threads[thread_id] = {
            "name": group_name,
            "last_message": args[2],
            "last_sent_ts": args[0],
            "last_read_ts": args[1],
            "last_msg_author_id": _arg(args, 18),
            "is_group": group_name is not None,
            "participants": [],
        }

    for args in calls.get("addParticipantIdToGroupThread", []):
        thread_id, user_id, participant_name = _arg(args, 0), _arg(args, 1), _arg(args, 5)
        if thread_id and thread_id in threads:
            threads[thread_id]["participants"].append(user_id)
        if user_id and participant_name and user_id not in users:
            users[user_id] = participant_name

    for thread_id, thread in threads.items():
        if thread["name"]:
            continue
        others = [uid for uid in thread["participants"] if uid != my_user_id]
        if not others:
            if thread_id in users:
                thread["name"] = users[thread_id]
            continue
        if len(others) == 1:
            thread["name"] = users.get(others[0], str(others[0]))
        else:
            names = [users.get(uid, str(uid)) for uid in others[:3]]
            thread["name"] = ", ".join(names)
            if len(others) > 3:
                thread["name"] += f" +{len(others) - 3}"
            thread["is_group"] = True

    messages = []
    for args in calls.get("upsertMessage", []):
        text, thread_id = _arg(args, 0), _arg(args, 3)
        if text and thread_id:
            messages.append({
                "text": text,
                "thread_id": thread_id,
                "timestamp": _arg(args, 5),
                "message_id": _arg(args, 8),
                "author_id": _arg(args, 10),
            })

    return Inbox(threads, users, messages)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _load_api_cache() -> dict:
    try:
        return json.loads(_API_CACHE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_api_cache(data: dict) -> None:
    _API_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _API_CACHE_PATH.write_text(json.dumps(data))


class MessengerSession:
    """Authenticated session with messenger.com."""

    def __init__(self):
        self._session = requests.Session()
        self._dtsg: str | None = None
        self._device_id: str | None = None
        self._schema_version: str | None = None
        self._query_id: str | None = None
        self._ids_from_cache = False
        self._inbox: Inbox | None = None
        self.my_user_id: int | None = None

    def connect(self) -> None:
        """Load cookies and extract API tokens from messenger.com."""
        if not COOKIES_PATH.exists():
            raise BackendError("Messenger cookies not found. Run 'messages auth messenger'.")
        cookies = json.loads(COOKIES_PATH.read_text())
        self._session.cookies.update(cookies)
        if c_user := cookies.get("c_user"):
            self.my_user_id = int(c_user)

        try:
            page = self._session.get("https://www.messenger.com", allow_redirects=True, timeout=_TIMEOUT)
        except requests.RequestException as e:
            raise BackendError(f"Could not reach messenger.com: {e}") from e
        if "login" in page.url:
            raise BackendError("Messenger cookies expired. Run 'messages auth messenger'.")

        dtsg_match = re.search(r'DTSG.{,20}"token":"([^"]+)"', page.text)
        device_match = re.search(r'"(?:deviceId|clientID)"\s*:\s*"([^"]+)"', page.text)
        if not dtsg_match or not device_match:
            raise BackendError("Failed to extract Messenger auth tokens.")
        self._dtsg = dtsg_match.group(1)
        self._device_id = device_match.group(1)

        cached = _load_api_cache()
        if cached.get("query_id") and cached.get("schema_version"):
            self._query_id = cached["query_id"]
            self._schema_version = cached["schema_version"]
            self._ids_from_cache = True
        else:
            self._discover_api_ids(page.text)

    def _discover_api_ids(self, page_text: str) -> None:
        """Find the GraphQL query ID and schema version in the JS bundles (slow)."""
        sv_match = re.search(r'\\"version\\":([0-9]{2,})', page_text)
        self._schema_version = sv_match.group(1) if sv_match else None
        self._query_id = None

        script_urls = sorted(set(re.findall(r'"(https://static[^"]+)"', page_text)))
        script_urls = [u for u in script_urls if ".js" in u or "/rsrc.php/" in u]

        def fetch_script(url):
            try:
                return requests.get(url, timeout=10).text
            except requests.RequestException:
                return ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            scripts = list(ex.map(fetch_script, script_urls))

        for text in scripts:
            if "LSPlatformGraphQLLightspeedRequestQuery" not in text:
                continue
            qid_match = re.search(
                r'id:\s*"([0-9]+)".{,50}name:\s*"LSPlatformGraphQLLightspeedRequestQuery"', text,
            )
            if qid_match:
                self._query_id = qid_match.group(1)
            sv2_match = re.search(r'__d\s*\(\s*"LSVersion".{,50}exports\s*=\s*"([0-9]+)"', text)
            if sv2_match and not self._schema_version:
                self._schema_version = sv2_match.group(1)
            break

        if not self._query_id or not self._schema_version:
            raise BackendError("Failed to find the Messenger API query ID.")
        self._ids_from_cache = False
        _save_api_cache({"query_id": self._query_id, "schema_version": self._schema_version})

    def _post_graphql(self, request_type: int, request_payload: dict) -> dict:
        try:
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
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            result = resp.json()
        except requests.RequestException as e:
            raise BackendError(f"Messenger request failed: {e}") from e
        except ValueError as e:
            raise MessengerAPIError("Messenger returned a non-JSON response.") from e
        if result.get("errors"):
            raise MessengerAPIError(f"Messenger API error: {result['errors'][0].get('message', result['errors'])}")
        try:
            return json.loads(result["data"]["viewer"]["lightspeed_web_request"]["payload"])
        except (KeyError, TypeError, ValueError) as e:
            raise MessengerAPIError("Unexpected Messenger API response.") from e

    def _read_graphql(self, request_payload: dict) -> dict:
        """A read-only sync request (type 1).

        If Facebook rejects it while the API IDs come from the disk cache, the
        IDs are rediscovered and the request is repeated once. Network errors
        are never retried. Writes must not use this, because a repeated send
        could deliver a message twice.
        """
        try:
            return self._post_graphql(1, request_payload)
        except MessengerAPIError:
            if not self._ids_from_cache:
                raise
        page = self._session.get("https://www.messenger.com", timeout=_TIMEOUT)
        self._discover_api_ids(page.text)
        return self._post_graphql(1, {**request_payload, "version": self._schema_version})

    def fetch_thread_page(self, thread_id: str) -> list[dict]:
        """Fetch a thread page and extract all embedded lightspeed payloads."""
        try:
            resp = self._session.get(
                f"https://www.messenger.com/t/{thread_id}/", allow_redirects=True, timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise BackendError(f"Could not load Messenger thread: {e}") from e
        raw_payloads = re.findall(
            r'"lightspeed_web_request":\{"payload":"((?:[^"\\]|\\.)*)"', resp.text,
        )
        parsed = []
        for raw in raw_payloads:
            try:
                parsed.append(json.loads(json.loads('"' + raw + '"')))
            except ValueError:
                pass
        return parsed

    def inbox(self) -> Inbox:
        """The initial inbox sync, fetched once per process."""
        if self._inbox is None:
            payload = self._read_graphql({
                "database": 1,
                "version": self._schema_version,
                "sync_params": json.dumps({}),
            })
            self._inbox = _parse_inbox(payload, self.my_user_id)
        return self._inbox

    def send_text(self, thread_id: str, text: str) -> None:
        """Send once, with no retry. Facebook may have delivered a message even when the response is lost."""
        # The inbox read confirms (or refreshes) cached API IDs before the send.
        self.inbox()
        timestamp = int(datetime.datetime.now().timestamp() * 1000)
        epoch = timestamp << 22
        otid = epoch + random.randrange(2**22)
        self._post_graphql(3, {
            "version_id": self._schema_version,
            "epoch_id": epoch,
            "tasks": [{
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
            }],
        })


_session: MessengerSession | None = None


def _get_session() -> MessengerSession:
    global _session
    if _session is None:
        session = MessengerSession()
        session.connect()
        _session = session
    return _session


# ---------------------------------------------------------------------------
# MQTT pagination (Go binaries)
# ---------------------------------------------------------------------------

def _fetch_older_messages(thread_id: int, ref_timestamp_ms: int, ref_message_id: str) -> list[dict]:
    """Older messages via fb-fetch: [{"text", "timestamp_ms", "sender_id", "message_id"}]."""
    binary = find_tool("fb-fetch")
    if binary is None:
        return []
    try:
        result = subprocess.run(
            [str(binary), str(COOKIES_PATH), str(thread_id), str(ref_timestamp_ms), ref_message_id],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


def _fetch_all_threads(max_pages: int = 100, e2ee: bool = True) -> tuple[list[dict], bool]:
    """All threads via fb-threads: ([{"thread_id", "name", "last_activity_ms", "thread_type"}], has_more).

    With e2ee=True, also discovers end-to-end encrypted threads.
    """
    binary = find_tool("fb-threads")
    if binary is None:
        return [], False
    cmd = [str(binary), str(COOKIES_PATH), str(max_pages)]
    if e2ee:
        cmd.append("--e2ee")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            return [], False
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            return data.get("threads", []), data.get("has_more", False)
        return data, False
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return [], False


def _mqtt_chat(t: dict, inbox: Inbox) -> Chat:
    tid = str(t["thread_id"])
    return Chat(
        platform=PLATFORM,
        id=tid,
        name=t["name"] or inbox.users.get(t["thread_id"], tid),
        last_message=format_ts_ms(t["last_activity_ms"]),
        is_group=t["thread_type"] != 1,
    )


def _download_images(urls: list[str], session: requests.Session) -> dict[str, str]:
    """Download images to the local cache. Returns {url: local_path} for successes."""
    _IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def download(url: str) -> tuple[str, str | None]:
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        ext = ".png" if ".png" in url else ".webp" if ".webp" in url else ".jpg"
        local_path = _IMAGE_CACHE_DIR / f"{url_hash}{ext}"
        if local_path.exists():
            return url, str(local_path)
        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            return url, str(local_path)
        except requests.RequestException:
            return url, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return {url: path for url, path in ex.map(download, urls) if path}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _inbox_chats(inbox: Inbox) -> list[Chat]:
    chats = [
        Chat(
            platform=PLATFORM,
            id=str(thread_id),
            name=inbox.thread_name(thread_id),
            last_message=format_ts_ms(thread["last_sent_ts"]),
            is_group=thread["is_group"],
        )
        for thread_id, thread in inbox.threads.items()
    ]
    chats.sort(key=lambda c: c.last_message, reverse=True)
    return chats


def recent_chats(limit: int = 20) -> list[Chat]:
    """Recent conversations. Uses fb-threads when the limit exceeds the inbox sync (~15 threads)."""
    inbox = _get_session().inbox()
    results = _inbox_chats(inbox)[:limit]
    if len(results) < limit:
        seen = {c.id for c in results}
        mqtt_threads, _ = _fetch_all_threads()
        for t in sorted(mqtt_threads, key=lambda x: x["last_activity_ms"], reverse=True):
            if len(results) >= limit:
                break
            if str(t["thread_id"]) not in seen:
                seen.add(str(t["thread_id"]))
                results.append(_mqtt_chat(t, inbox))
    return results


def all_threads() -> dict:
    """All discoverable threads: {"threads": [Chat, ...], "has_more": bool}."""
    inbox = _get_session().inbox()
    results = _inbox_chats(inbox)
    seen = {c.id for c in results}
    mqtt_threads, has_more = _fetch_all_threads(max_pages=0)
    for t in mqtt_threads:
        if str(t["thread_id"]) not in seen:
            seen.add(str(t["thread_id"]))
            results.append(_mqtt_chat(t, inbox))
    results.sort(key=lambda c: c.last_message, reverse=True)
    return {"threads": results, "has_more": has_more}


def find_chats(query: str, deep: bool = True) -> list[Chat]:
    """Find chats by name in the inbox sync.

    With deep=True and no inbox match, pages through every thread with
    fb-threads, which can take minutes.
    """
    wanted = query.strip().casefold()
    inbox = _get_session().inbox()
    results = [c for c in _inbox_chats(inbox) if wanted in c.name.casefold()]
    if results or not deep:
        return results
    mqtt_threads, _ = _fetch_all_threads()
    return [c for t in mqtt_threads if wanted in (c := _mqtt_chat(t, inbox)).name.casefold()]


def resolve_chat(identifier: str, deep: bool = True, strict: bool = False) -> Chat | None:
    stripped = identifier.strip()
    if stripped.isdigit():
        inbox = _get_session().inbox()
        return Chat(platform=PLATFORM, id=stripped, name=inbox.thread_name(int(stripped)))
    return choose_chat(stripped, find_chats(stripped, deep=deep), strict)


def _sender(author_id: int | None, users: dict[int, str], my_user_id: int | None) -> str:
    if author_id == my_user_id:
        return "Me"
    return users.get(author_id, str(author_id)) if author_id else "Unknown"


def read_messages(thread_id: str, limit: int = 20) -> list[Message]:
    """Most recent messages in a thread, newest first.

    The thread page embeds about 20 messages. fb-fetch loads older ones when
    more are requested.
    """
    sess = _get_session()
    tid = int(thread_id)

    all_calls: dict[str, list] = {}
    for payload in sess.fetch_thread_page(thread_id):
        for fn, arglist in _find_calls(payload).items():
            all_calls.setdefault(fn, []).extend(arglist)
    users = _parse_users(all_calls)

    # message_id -> [(label, image_url_or_none)]
    attach_map: dict[str, list[tuple[str, str | None]]] = {}
    for args in all_calls.get("insertBlobAttachment", []):
        if len(args) <= 32 or args[27] != tid:
            continue
        atype, fname = args[29], args[0] or ""  # atype 2=image, 4=video
        if atype == 4 or fname.startswith("video"):
            attach_map.setdefault(args[32], []).append(("video", None))
        else:
            url = args[3] if isinstance(args[3], str) else None
            attach_map.setdefault(args[32], []).append(("image", url))
    for args in all_calls.get("insertStickerAttachment", []):
        if len(args) <= 18 or args[14] != tid:
            continue
        attach_map.setdefault(args[18], []).append((f"sticker: {args[13] or 'sticker'}", None))

    raw: list[dict] = []
    seen = set()
    for args in all_calls.get("upsertMessage", []):
        if len(args) <= 10 or args[3] != tid:
            continue
        text, timestamp, msg_id, author_id = args[0], args[5], args[8], args[10]
        attachments = attach_map.get(msg_id, []) if msg_id else []
        key = (timestamp, author_id, text)
        if key in seen or (not text and not attachments):
            continue
        seen.add(key)
        raw.append({
            "id": msg_id, "text": text, "timestamp": timestamp,
            "author_id": author_id, "attachments": attachments,
        })

    if len(raw) < limit:
        ref = min(
            (
                (args[1], args[3]) for args in all_calls.get("insertNewMessageRange", [])
                if args[0] == tid and len(args) > 7 and args[7] and args[1] and args[3]
            ),
            default=None,
        )
        if ref:
            for m in _fetch_older_messages(tid, *ref):
                key = (m["timestamp_ms"], m["sender_id"], m["text"])
                if key not in seen:
                    seen.add(key)
                    raw.append({
                        "id": m.get("message_id"), "text": m["text"], "timestamp": m["timestamp_ms"],
                        "author_id": m["sender_id"], "attachments": [],
                    })

    raw.sort(key=lambda m: m["timestamp"] or 0, reverse=True)
    raw = raw[:limit]
    urls = [url for m in raw for _, url in m["attachments"] if url]
    downloaded = _download_images(urls, sess._session) if urls else {}

    messages = []
    for m in raw:
        tags = [f"[{label}]" for label, _ in m["attachments"]]
        messages.append(Message(
            id=str(m["id"] or ""),
            timestamp=format_ts_ms(m["timestamp"]),
            sender=_sender(m["author_id"], users, sess.my_user_id),
            text=" ".join(filter(None, [m["text"], *tags])),
            is_from_me=m["author_id"] == sess.my_user_id,
            image_paths=[downloaded[u] for _, u in m["attachments"] if u in downloaded],
        ))
    return messages


def search_messages(query: str, limit: int = 20, thread_id: str | None = None) -> list[SearchHit]:
    """Search messages in the initial inbox sync only (the latest messages of recent threads)."""
    wanted = query.casefold()
    sess = _get_session()
    inbox = sess.inbox()
    hits = []
    for m in inbox.messages:
        if wanted not in m["text"].casefold():
            continue
        if thread_id is not None and str(m["thread_id"]) != str(thread_id):
            continue
        hits.append(SearchHit(
            id=str(m["message_id"] or ""),
            timestamp=format_ts_ms(m["timestamp"]),
            sender=_sender(m["author_id"], inbox.users, sess.my_user_id),
            text=m["text"],
            is_from_me=m["author_id"] == sess.my_user_id,
            platform=PLATFORM,
            chat_id=str(m["thread_id"]),
            chat_name=inbox.thread_name(m["thread_id"]),
        ))
    hits.sort(key=lambda h: h.timestamp, reverse=True)
    return hits[:limit]


def send_message(thread_id: str, text: str) -> str:
    _get_session().send_text(thread_id, text)
    return "Message sent."


def stats() -> dict:
    """Counts from the initial inbox sync."""
    inbox = _get_session().inbox()
    return {"messages": len(inbox.messages), "chats": len(inbox.threads)}
