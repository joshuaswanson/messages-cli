"""Read Telegram messages from the local macOS App Store database.

The database is SQLCipher-encrypted and uses Telegram's custom PostboxEncoder
binary format. This module handles decryption, binary parsing, and message
extraction.

References:
  - https://gist.github.com/stek29/8a7ac0e673818917525ec4031d77a713
  - https://gist.github.com/Green-m/5f845f52af08cb53b4804ede198fc4f1
"""

from __future__ import annotations

import binascii
import enum
import io
import sqlite3
import struct
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import mmh3
from Crypto.Cipher import AES
from Crypto.Hash import SHA512

# Telegram App Store container
_TG_CONTAINER = Path.home() / "Library/Group Containers/6N38VWS5BX.ru.keepcoder.Telegram"
_DEFAULT_PASSWORD = "no-matter-key"
_MURMUR_SEED = -137723950


def _murmur(data: bytes | str) -> int:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return mmh3.hash(data, seed=_MURMUR_SEED)


# ---------------------------------------------------------------------------
# SQLCipher decryption
# ---------------------------------------------------------------------------

def _find_db_path() -> Path | None:
    """Find the postbox database path (handles appstore/ prefix variation)."""
    for variant in ("appstore", ""):
        base = _TG_CONTAINER / variant if variant else _TG_CONTAINER
        for account_dir in base.glob("account-*"):
            db_path = account_dir / "postbox/db/db_sqlite"
            if db_path.exists():
                return db_path
    return None


def _find_key_path() -> Path | None:
    """Find the .tempkeyEncrypted file."""
    for variant in ("appstore", ""):
        base = _TG_CONTAINER / variant if variant else _TG_CONTAINER
        key_path = base / ".tempkeyEncrypted"
        if key_path.exists():
            return key_path
    return None


def _decrypt_key(key_path: Path, password: str = _DEFAULT_PASSWORD) -> tuple[bytes, bytes]:
    """Decrypt .tempkeyEncrypted to get (db_key, db_salt)."""
    h = SHA512.new()
    h.update(password.encode("utf-8"))
    digest = h.digest()
    aes_key, aes_iv = digest[:32], digest[-16:]

    encrypted = key_path.read_bytes()
    cipher = AES.new(key=aes_key, iv=aes_iv, mode=AES.MODE_CBC)
    decrypted = cipher.decrypt(encrypted)

    db_key = decrypted[:32]
    db_salt = decrypted[32:48]
    db_hash = struct.unpack("<i", decrypted[48:52])[0]

    calc_hash = _murmur(db_key + db_salt)
    if db_hash != calc_hash:
        raise RuntimeError(
            f"Key integrity check failed (hash mismatch: {db_hash} != {calc_hash}). "
            "Is a local passcode set on Telegram?"
        )

    return db_key, db_salt


def _decrypt_database(db_path: Path, db_key: bytes, db_salt: bytes) -> Path:
    """Use sqlcipher CLI to export encrypted DB to a plaintext temp file."""
    hex_key = binascii.hexlify(db_key + db_salt).decode()
    plaintext_path = Path(tempfile.mktemp(suffix=".db"))

    sql_commands = f"""
PRAGMA key="x'{hex_key}'";
PRAGMA cipher_plaintext_header_size=32;
PRAGMA cipher_default_plaintext_header_size=32;
ATTACH DATABASE '{plaintext_path}' AS plaintext KEY '';
SELECT sqlcipher_export('plaintext');
DETACH DATABASE plaintext;
"""

    result = subprocess.run(
        ["sqlcipher", str(db_path)],
        input=sql_commands,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"sqlcipher export failed: {result.stderr}")

    if not plaintext_path.exists() or plaintext_path.stat().st_size == 0:
        raise RuntimeError("sqlcipher produced empty output. Decryption may have failed.")

    return plaintext_path


# ---------------------------------------------------------------------------
# PostboxEncoder binary parser
# ---------------------------------------------------------------------------

class _ByteReader:
    """Little-endian binary reader."""

    def __init__(self, data: bytes, endian: str = "<"):
        self.buf = io.BytesIO(data)
        self.endian = endian

    def _read(self, fmt: str) -> int | float:
        fmt = self.endian + fmt
        size = struct.calcsize(fmt)
        data = self.buf.read(size)
        if len(data) < size:
            raise EOFError("Unexpected end of data")
        return struct.unpack(fmt, data)[0]

    def read_int8(self) -> int:
        return self._read("b")

    def read_uint8(self) -> int:
        return self._read("B")

    def read_int32(self) -> int:
        return self._read("i")

    def read_uint32(self) -> int:
        return self._read("I")

    def read_int64(self) -> int:
        return self._read("q")

    def read_double(self) -> float:
        return self._read("d")

    def read_bytes(self) -> bytes:
        length = self.read_int32()
        return self.buf.read(length)

    def read_str(self) -> str:
        return self.read_bytes().decode("utf-8", errors="replace")

    def read_short_str(self) -> str:
        length = self.read_uint8()
        return self.buf.read(length).decode("utf-8", errors="replace")

    @property
    def remaining(self) -> int:
        pos = self.buf.tell()
        self.buf.seek(0, io.SEEK_END)
        end = self.buf.tell()
        self.buf.seek(pos)
        return end - pos


class _ValueType(enum.Enum):
    Int32 = 0
    Int64 = 1
    Bool = 2
    Double = 3
    String = 4
    Object = 5
    Int32Array = 6
    Int64Array = 7
    ObjectArray = 8
    ObjectDictionary = 9
    Bytes = 10
    Nil = 11
    StringArray = 12
    BytesArray = 13


class _PostboxDecoder:
    """Decoder for Telegram's PostboxEncoder binary format."""

    def __init__(self, data: bytes):
        self.reader = _ByteReader(data)
        self.size = len(data)

    def decode_all_fields(self) -> dict:
        """Decode all key-value pairs into a dict."""
        self.reader.buf.seek(0)
        fields = {}
        while self.reader.buf.tell() < self.size:
            try:
                key = self.reader.read_short_str()
                _, value = self._read_value()
                fields[key] = value
            except (EOFError, struct.error, ValueError):
                break
        return fields

    def get_string(self, key: str) -> str | None:
        """Find a string field by key."""
        self.reader.buf.seek(0)
        while self.reader.buf.tell() < self.size:
            try:
                k = self.reader.read_short_str()
                vtype, value = self._read_value()
                if k == key and vtype == _ValueType.String:
                    return value
            except (EOFError, struct.error, ValueError):
                break
        return None

    def get_int64(self, key: str) -> int | None:
        self.reader.buf.seek(0)
        while self.reader.buf.tell() < self.size:
            try:
                k = self.reader.read_short_str()
                vtype, value = self._read_value()
                if k == key and vtype == _ValueType.Int64:
                    return value
            except (EOFError, struct.error, ValueError):
                break
        return None

    def _skip_value(self, vtype: _ValueType):
        """Skip over a value without decoding it."""
        if vtype in (_ValueType.Int32, _ValueType.Int32Array):
            if vtype == _ValueType.Int32:
                self.reader.buf.read(4)
            else:
                count = self.reader.read_int32()
                self.reader.buf.read(count * 4)
        elif vtype in (_ValueType.Int64, _ValueType.Double):
            self.reader.buf.read(8)
        elif vtype == _ValueType.Bool:
            self.reader.buf.read(1)
        elif vtype in (_ValueType.String, _ValueType.Bytes):
            length = self.reader.read_int32()
            self.reader.buf.read(length)
        elif vtype == _ValueType.Object:
            self.reader.buf.read(4)  # type hash
            data_len = self.reader.read_int32()
            self.reader.buf.read(data_len)
        elif vtype == _ValueType.Nil:
            pass
        elif vtype == _ValueType.ObjectArray:
            count = self.reader.read_int32()
            for _ in range(count):
                self.reader.buf.read(4)  # type hash
                data_len = self.reader.read_int32()
                self.reader.buf.read(data_len)
        elif vtype == _ValueType.Int64Array:
            count = self.reader.read_int32()
            self.reader.buf.read(count * 8)
        elif vtype == _ValueType.StringArray:
            count = self.reader.read_int32()
            for _ in range(count):
                length = self.reader.read_int32()
                self.reader.buf.read(length)
        elif vtype == _ValueType.BytesArray:
            count = self.reader.read_int32()
            for _ in range(count):
                length = self.reader.read_int32()
                self.reader.buf.read(length)
        elif vtype == _ValueType.ObjectDictionary:
            count = self.reader.read_int32()
            for _ in range(count):
                self.reader.buf.read(4)
                klen = self.reader.read_int32()
                self.reader.buf.read(klen)
                self.reader.buf.read(4)
                vlen = self.reader.read_int32()
                self.reader.buf.read(vlen)

    def _read_value(self) -> tuple[_ValueType, object]:
        vtype = _ValueType(self.reader.read_uint8())

        if vtype == _ValueType.Int32:
            return vtype, self.reader.read_int32()
        elif vtype == _ValueType.Int64:
            return vtype, self.reader.read_int64()
        elif vtype == _ValueType.Bool:
            return vtype, self.reader.read_uint8() != 0
        elif vtype == _ValueType.Double:
            return vtype, self.reader.read_double()
        elif vtype == _ValueType.String:
            return vtype, self.reader.read_str()
        elif vtype == _ValueType.Object:
            _type_hash = self.reader.read_int32()
            data_len = self.reader.read_int32()
            data = self.reader.buf.read(data_len)
            return vtype, data
        elif vtype == _ValueType.Int32Array:
            count = self.reader.read_int32()
            return vtype, [self.reader.read_int32() for _ in range(count)]
        elif vtype == _ValueType.Int64Array:
            count = self.reader.read_int32()
            return vtype, [self.reader.read_int64() for _ in range(count)]
        elif vtype == _ValueType.ObjectArray:
            count = self.reader.read_int32()
            items = []
            for _ in range(count):
                _type_hash = self.reader.read_int32()
                data_len = self.reader.read_int32()
                data = self.reader.buf.read(data_len)
                items.append(data)
            return vtype, items
        elif vtype == _ValueType.ObjectDictionary:
            count = self.reader.read_int32()
            items = []
            for _ in range(count):
                # key object
                self.reader.read_int32()
                klen = self.reader.read_int32()
                self.reader.buf.read(klen)
                # value object
                self.reader.read_int32()
                vlen = self.reader.read_int32()
                self.reader.buf.read(vlen)
            return vtype, items
        elif vtype == _ValueType.Bytes:
            return vtype, self.reader.read_bytes()
        elif vtype == _ValueType.Nil:
            return vtype, None
        elif vtype == _ValueType.StringArray:
            count = self.reader.read_int32()
            return vtype, [self.reader.read_str() for _ in range(count)]
        elif vtype == _ValueType.BytesArray:
            count = self.reader.read_int32()
            return vtype, [self.reader.read_bytes() for _ in range(count)]
        else:
            raise ValueError(f"Unknown value type: {vtype}")


# ---------------------------------------------------------------------------
# Message flags
# ---------------------------------------------------------------------------

class _MessageFlags(enum.IntFlag):
    Unsent = 1
    Failed = 2
    Incoming = 4
    TopIndexable = 16
    Sending = 32
    WasScheduled = 128
    CountedAsIncoming = 256


class _MessageDataFlags(enum.IntFlag):
    GloballyUniqueId = 1 << 0
    GlobalTags = 1 << 1
    GroupingKey = 1 << 2
    GroupInfo = 1 << 3
    LocalTags = 1 << 4
    ThreadId = 1 << 5


class _FwdInfoFlags(enum.IntFlag):
    SourceId = 1 << 1
    SourceMessage = 1 << 2
    Signature = 1 << 3
    PsaType = 1 << 4
    Flags = 1 << 5


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _parse_message_key(key: bytes) -> dict:
    """Parse t7 key: peerId(i64) + namespace(i32) + timestamp(i32) + messageId(i32)."""
    reader = _ByteReader(key, endian=">")
    return {
        "peer_id": reader.read_int64(),
        "namespace": reader.read_int32(),
        "timestamp": reader.read_int32(),
        "message_id": reader.read_int32(),
    }


def _parse_fwd_info(reader: _ByteReader) -> dict | None:
    info_flags = _FwdInfoFlags(reader.read_int8())
    if info_flags == 0:
        return None

    author_id = reader.read_int64()
    date = reader.read_int32()

    if _FwdInfoFlags.SourceId in info_flags:
        reader.read_int64()
    if _FwdInfoFlags.SourceMessage in info_flags:
        reader.read_int64()  # peer_id
        reader.read_int32()  # namespace
        reader.read_int32()  # id
    if _FwdInfoFlags.Signature in info_flags:
        reader.read_str()
    if _FwdInfoFlags.PsaType in info_flags:
        reader.read_str()
    if _FwdInfoFlags.Flags in info_flags:
        reader.read_int32()

    return {"author_id": author_id, "date": date}


def _parse_message_value(data: bytes) -> dict | None:
    """Parse a t7 value blob into a message dict."""
    reader = _ByteReader(data)

    try:
        msg_type = reader.read_int8()
        if msg_type != 0:
            return None

        _stable_id = reader.read_uint32()
        _stable_ver = reader.read_uint32()
        data_flags = _MessageDataFlags(reader.read_uint8())

        if _MessageDataFlags.GloballyUniqueId in data_flags:
            reader.read_int64()
        if _MessageDataFlags.GlobalTags in data_flags:
            reader.read_uint32()
        if _MessageDataFlags.GroupingKey in data_flags:
            reader.read_int64()
        if _MessageDataFlags.GroupInfo in data_flags:
            reader.read_uint32()
        if _MessageDataFlags.LocalTags in data_flags:
            reader.read_uint32()
        if _MessageDataFlags.ThreadId in data_flags:
            reader.read_int64()

        flags = _MessageFlags(reader.read_uint32())
        _tags = reader.read_uint32()

        _fwd_info = _parse_fwd_info(reader)

        author_id = None
        has_author = reader.read_int8()
        if has_author == 1:
            author_id = reader.read_int64()

        text = reader.read_str()

        return {
            "text": text,
            "author_id": author_id,
            "incoming": bool(_MessageFlags.Incoming & flags),
        }
    except (EOFError, struct.error):
        return None


# ---------------------------------------------------------------------------
# Peer parsing
# ---------------------------------------------------------------------------

def _parse_peer(data: bytes) -> dict | None:
    """Parse a t2 value blob into a peer info dict.

    Peer data is PostboxEncoder-encoded with a root object at key "_".
    The root object contains the actual peer fields (fn, ln, un, t, p, etc.).
    """
    if len(data) < 8:
        return None

    # The outer blob is PostboxEncoder with key "_" -> Object
    # We need to find the Object value for key "_" and parse its inner data
    outer = _PostboxDecoder(data)
    outer.reader.buf.seek(0)

    try:
        while outer.reader.buf.tell() < outer.size:
            key = outer.reader.read_short_str()
            vtype_raw = outer.reader.read_uint8()
            if key == "_" and vtype_raw == _ValueType.Object.value:
                _type_hash = outer.reader.read_int32()
                data_len = outer.reader.read_int32()
                inner_data = outer.reader.buf.read(data_len)
                inner = _PostboxDecoder(inner_data)
                fields = inner.decode_all_fields()
                return {
                    "first_name": fields.get("fn", ""),
                    "last_name": fields.get("ln", ""),
                    "username": fields.get("un", ""),
                    "title": fields.get("t", ""),
                    "phone": fields.get("p", ""),
                }
            else:
                # Skip this value to continue searching
                vtype = _ValueType(vtype_raw)
                outer._skip_value(vtype)
    except (EOFError, struct.error, ValueError):
        pass

    return None


def _peer_display_name(peer: dict) -> str:
    """Build a display name from peer fields."""
    if peer.get("title"):
        return peer["title"]
    first = peer.get("first_name", "")
    last = peer.get("last_name", "")
    name = f"{first} {last}".strip()
    if name:
        return name
    if peer.get("username"):
        return f"@{peer['username']}"
    return "Unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class TelegramDB:
    """Interface to the local Telegram macOS database."""

    def __init__(self):
        self._db_path = _find_db_path()
        self._key_path = _find_key_path()
        self._conn: sqlite3.Connection | None = None
        self._plaintext_path: Path | None = None
        self._peer_cache: dict[int, dict] = {}

    @property
    def available(self) -> bool:
        return self._db_path is not None and self._key_path is not None

    def _ensure_connection(self):
        if self._conn is not None:
            return

        if not self.available:
            print(
                "Error: Telegram database not found. "
                "Is Telegram (App Store) installed and logged in?",
                file=sys.stderr,
            )
            sys.exit(1)

        db_key, db_salt = _decrypt_key(self._key_path)
        self._plaintext_path = _decrypt_database(self._db_path, db_key, db_salt)
        self._conn = sqlite3.connect(str(self._plaintext_path))

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
        if self._plaintext_path and self._plaintext_path.exists():
            self._plaintext_path.unlink()
            self._plaintext_path = None

    def _get_peer(self, peer_id: int) -> dict:
        if peer_id in self._peer_cache:
            return self._peer_cache[peer_id]

        self._ensure_connection()
        row = self._conn.execute(
            "SELECT value FROM t2 WHERE key = ? LIMIT 1", (peer_id,)
        ).fetchone()

        if row is None:
            result = {"first_name": "", "last_name": "", "username": "", "title": "", "phone": ""}
        else:
            result = _parse_peer(row[0]) or {"first_name": "", "last_name": "", "username": "", "title": "", "phone": ""}

        self._peer_cache[peer_id] = result
        return result

    def recent_chats(self, limit: int = 20) -> list[dict]:
        """List recent chats with peer names and last message time."""
        self._ensure_connection()

        rows = self._conn.execute("SELECT key FROM t7").fetchall()

        # Find the most recent timestamp per peer
        peer_latest: dict[int, int] = {}
        for (key,) in rows:
            parsed = _parse_message_key(key)
            pid = parsed["peer_id"]
            ts = parsed["timestamp"]
            if pid not in peer_latest or ts > peer_latest[pid]:
                peer_latest[pid] = ts

        # Sort by most recent timestamp
        sorted_peers = sorted(peer_latest.items(), key=lambda x: x[1], reverse=True)[:limit]

        chats = []
        for peer_id, last_ts in sorted_peers:
            peer = self._get_peer(peer_id)
            name = _peer_display_name(peer)
            ts = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d %H:%M:%S")
            chats.append({
                "peer_id": peer_id,
                "name": name,
                "username": peer.get("username", ""),
                "phone": peer.get("phone", ""),
                "last_message": ts,
            })

        return chats

    def find_chats(self, query: str) -> list[dict]:
        """Find chats by name, username, or phone substring."""
        import re as _re
        self._ensure_connection()
        query_lower = query.lower()
        query_digits = _re.sub(r"\D", "", query)

        # Get all known peers from t2
        rows = self._conn.execute("SELECT key, value FROM t2").fetchall()

        results = []
        for peer_id, value in rows:
            peer = _parse_peer(value)
            if peer is None:
                continue
            self._peer_cache[peer_id] = peer
            name = _peer_display_name(peer)
            searchable = f"{name} {peer.get('username', '')} {peer.get('phone', '')}".lower()
            # Match by text substring or by phone digits
            phone_digits = _re.sub(r"\D", "", peer.get("phone", ""))
            if query_lower in searchable or (query_digits and query_digits in phone_digits):
                results.append({
                    "peer_id": peer_id,
                    "name": name,
                    "username": peer.get("username", ""),
                    "phone": peer.get("phone", ""),
                })

        return results

    def find_peer_by_phone(self, phone_digits: str) -> int | None:
        """Find a peer_id by phone number digits."""
        self._ensure_connection()
        import re
        digits = re.sub(r"\D", "", phone_digits)
        if not digits:
            return None
        rows = self._conn.execute("SELECT key, value FROM t2").fetchall()
        for peer_id, value in rows:
            peer = _parse_peer(value)
            if peer is None:
                continue
            self._peer_cache[peer_id] = peer
            peer_phone = re.sub(r"\D", "", peer.get("phone", ""))
            if peer_phone and digits in peer_phone:
                return peer_id
        return None

    def resolve_identifier(self, identifier: str) -> int | None:
        """Resolve a name, phone, or peer_id string to a peer_id int."""
        import re
        stripped = identifier.strip()
        # Pure digits and large -> treat as peer_id directly
        if stripped.isdigit() and int(stripped) > 100000:
            return int(stripped)
        # Contains digits -> try phone lookup
        if re.search(r"\d", stripped):
            result = self.find_peer_by_phone(stripped)
            if result is not None:
                return result
        # Name lookup
        matches = self.find_chats(stripped)
        if matches:
            return matches[0]["peer_id"]
        return None

    def read_messages(self, peer_id: int, limit: int = 20) -> list[dict]:
        """Read messages from a specific chat."""
        self._ensure_connection()

        # Match messages by peer_id prefix (first 8 bytes of the 20-byte key)
        prefix = struct.pack(">q", peer_id)

        rows = self._conn.execute(
            "SELECT key, value FROM t7 WHERE substr(key, 1, 8) = ? ORDER BY key DESC LIMIT ?",
            (prefix, limit),
        ).fetchall()

        messages = []
        for key, value in rows:
            idx = _parse_message_key(key)
            msg = _parse_message_value(value)
            if msg is None or not msg["text"]:
                continue

            # Resolve sender
            if msg["incoming"]:
                author_id = msg["author_id"] or idx["peer_id"]
                author_peer = self._get_peer(author_id)
                sender = _peer_display_name(author_peer)
            else:
                sender = "Me"

            ts = datetime.fromtimestamp(idx["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")

            messages.append({
                "timestamp": ts,
                "sender": sender,
                "text": msg["text"],
                "edited": False,
                "peer_id": idx["peer_id"],
                "message_id": idx["message_id"],
            })

        return messages

    def search_messages(self, query: str, limit: int = 50) -> list[dict]:
        """Search all messages for a text substring."""
        self._ensure_connection()
        query_lower = query.lower()

        rows = self._conn.execute(
            "SELECT key, value FROM t7 ORDER BY key DESC"
        ).fetchall()

        results = []
        for key, value in rows:
            if len(results) >= limit:
                break

            msg = _parse_message_value(value)
            if msg is None or not msg["text"]:
                continue

            if query_lower not in msg["text"].lower():
                continue

            idx = _parse_message_key(key)
            peer = self._get_peer(idx["peer_id"])

            if msg["incoming"]:
                author_id = msg["author_id"] or idx["peer_id"]
                author_peer = self._get_peer(author_id)
                sender = _peer_display_name(author_peer)
            else:
                sender = "Me"

            ts = datetime.fromtimestamp(idx["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")

            results.append({
                "timestamp": ts,
                "chat_name": _peer_display_name(peer),
                "sender": sender,
                "text": msg["text"],
                "peer_id": idx["peer_id"],
            })

        return results

    def get_all_messages(self, since_timestamp: int = 0) -> list[dict]:
        """Get all messages, optionally filtered by timestamp. For bulk export."""
        self._ensure_connection()

        rows = self._conn.execute(
            "SELECT key, value FROM t7 ORDER BY key ASC"
        ).fetchall()

        messages = []
        for key, value in rows:
            idx = _parse_message_key(key)
            if idx["timestamp"] < since_timestamp:
                continue

            msg = _parse_message_value(value)
            if msg is None:
                continue

            # Resolve sender
            if msg["incoming"]:
                author_id = msg["author_id"] or idx["peer_id"]
                author_peer = self._get_peer(author_id)
                sender_name = _peer_display_name(author_peer)
            else:
                sender_name = None

            peer = self._get_peer(idx["peer_id"])

            messages.append({
                "peer_id": idx["peer_id"],
                "peer_name": _peer_display_name(peer),
                "message_id": idx["message_id"],
                "timestamp": idx["timestamp"],
                "text": msg["text"],
                "is_from_me": not msg["incoming"],
                "sender_name": sender_name,
            })

        return messages

    def stats(self) -> dict:
        """Get database statistics."""
        self._ensure_connection()
        msg_count = self._conn.execute("SELECT COUNT(*) FROM t7").fetchone()[0]
        peer_count = self._conn.execute("SELECT COUNT(*) FROM t2").fetchone()[0]
        return {"messages": msg_count, "peers": peer_count}
