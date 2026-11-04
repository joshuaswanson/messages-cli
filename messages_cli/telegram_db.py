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
import functools
import io
import os
import struct
from pathlib import Path

import mmh3
from Crypto.Cipher import AES
from Crypto.Hash import SHA512

from .models import BackendError, Chat, Message, SearchHit
from .utils import choose_chat, format_phone, format_ts, looks_like_phone, phone_key

PLATFORM = "telegram"

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
        raise BackendError(
            f"Key integrity check failed (hash mismatch: {db_hash} != {calc_hash}). "
            "Is a local passcode set on Telegram?"
        )

    return db_key, db_salt


def _open_encrypted(db_path: Path, db_key: bytes, db_salt: bytes):
    """Open the SQLCipher database read-only, decrypting pages in memory."""
    import sqlcipher3

    conn = sqlcipher3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    hex_key = binascii.hexlify(db_key + db_salt).decode()
    conn.execute(f"PRAGMA key=\"x'{hex_key}'\"")
    conn.execute("PRAGMA cipher_plaintext_header_size=32")
    try:
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlcipher3.DatabaseError as e:
        conn.close()
        raise BackendError(f"Could not decrypt the Telegram database: {e}") from e
    return conn


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
        tags = reader.read_uint32()

        _fwd_info = _parse_fwd_info(reader)

        author_id = None
        has_author = reader.read_int8()
        if has_author == 1:
            author_id = reader.read_int64()

        text = reader.read_str()

        # Capture position after text for media parsing
        post_text_pos = reader.buf.tell()
        post_text_data = reader.buf.read()
        reader.buf.seek(post_text_pos)

        return {
            "text": text,
            "author_id": author_id,
            "incoming": bool(_MessageFlags.Incoming & flags),
            "tags": tags,
            "_post_text": post_text_data,
        }
    except (EOFError, struct.error):
        return None


# ---------------------------------------------------------------------------
# Photo media extraction
# ---------------------------------------------------------------------------

_TG_MEDIA_IMAGE_HASH = 0x8bae2094


def _extract_photo_resource_ids(post_text_data: bytes) -> list[int]:
    """Extract photo resource IDs from the post-text portion of a message.

    After text, the message binary contains:
      - attributes: count(i32) + for_each(len(i32) + data)
      - media: count(i32) + for_each(len(i32) + data)

    For TelegramMediaImage objects, we parse representations to find
    resource IDs that correspond to cached photo files on disk.
    """
    if len(post_text_data) < 8:
        return []

    reader = _ByteReader(post_text_data)
    try:
        # Skip attributes
        attr_count = reader.read_int32()
        for _ in range(attr_count):
            alen = reader.read_int32()
            reader.buf.read(alen)

        # Read media
        media_count = reader.read_int32()
        if media_count == 0:
            return []

        resource_ids = []
        for _ in range(media_count):
            mlen = reader.read_int32()
            media_data = reader.buf.read(mlen)
            rids = _parse_media_image(media_data)
            resource_ids.extend(rids)

        return resource_ids
    except (EOFError, struct.error, ValueError):
        return []


def _parse_media_image(media_data: bytes) -> list[int]:
    """Parse a media blob and extract resource IDs if it's a TelegramMediaImage."""
    try:
        mr = _ByteReader(media_data)
        klen = mr.read_uint8()
        mr.buf.read(klen)  # key (usually "_")
        vtype = mr.read_uint8()
        if vtype != _ValueType.Object.value:
            return []
        type_hash = mr.read_int32() & 0xffffffff
        if type_hash != _TG_MEDIA_IMAGE_HASH:
            return []
        inner_len = mr.read_int32()
        inner_data = mr.buf.read(inner_len)

        inner = _PostboxDecoder(inner_data)
        fields = inner.decode_all_fields()
        reps = fields.get('r', [])
        if not isinstance(reps, list):
            return []

        resource_ids = []
        for rep_bytes in reps:
            if not isinstance(rep_bytes, bytes):
                continue
            rid = _extract_resource_id_from_rep(rep_bytes)
            if rid is not None:
                resource_ids.append(rid)
        return resource_ids
    except (EOFError, struct.error, ValueError):
        return []


def _extract_resource_id_from_rep(rep_bytes: bytes) -> int | None:
    """Extract the resource ID from a photo representation blob."""
    try:
        dec = _PostboxDecoder(rep_bytes)
        fields = dec.decode_all_fields()
        r_data = fields.get('r')
        if not isinstance(r_data, bytes):
            return None
        r_dec = _PostboxDecoder(r_data)
        r_fields = r_dec.decode_all_fields()
        i_val = r_fields.get('i')
        if isinstance(i_val, int):
            return i_val
        if isinstance(i_val, bytes) and len(i_val) == 8:
            return struct.unpack('<q', i_val)[0]
        return None
    except (EOFError, struct.error, ValueError):
        return None


def _build_photo_cache(media_dir: Path) -> dict[int, str]:
    """Build a lookup of resource_id -> best local file path from the media dir.

    Prefers larger size variants (y > x > m > s > c > a > b).
    """
    variant_priority = {'y': 6, 'x': 5, 'm': 4, 's': 3, 'c': 2, 'a': 1, 'b': 0}
    cache: dict[int, tuple[str, int]] = {}

    if not media_dir.exists():
        return {}

    for f in os.listdir(media_dir):
        if not f.startswith("telegram-cloud-photo-size-"):
            continue
        if f.endswith("_partial") or f.endswith(".meta"):
            continue
        parts = f.split("-")
        if len(parts) < 7:
            continue
        try:
            rid = int(parts[5])
        except ValueError:
            continue
        variant = parts[6]
        prio = variant_priority.get(variant, -1)
        if rid not in cache or prio > cache[rid][1]:
            cache[rid] = (str(media_dir / f), prio)

    return {rid: path for rid, (path, _) in cache.items()}


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

# t7 keys are peerId(i64) + namespace(i32) + timestamp(i32) + messageId(i32),
# big-endian, so these byte ranges sort chronologically as blobs.
_PEER_PREFIX = "substr(key, 1, 8)"
_TIME_ORDER = "substr(key, 13, 8)"

_EMPTY_PEER = {"first_name": "", "last_name": "", "username": "", "title": "", "phone": ""}


def is_available() -> bool:
    return _find_db_path() is not None and _find_key_path() is not None


class TelegramDB:
    """Interface to the local Telegram macOS database."""

    def __init__(self):
        self._db_path = _find_db_path()
        self._key_path = _find_key_path()
        self._conn = None
        self._peer_cache: dict[int, dict] = {}
        self._all_peers: dict[int, dict] | None = None
        self._photo_cache: dict[int, str] | None = None

    @property
    def conn(self):
        if self._conn is None:
            if self._db_path is None or self._key_path is None:
                raise BackendError(
                    "Telegram database not found. Is Telegram (App Store) installed and logged in?"
                )
            db_key, db_salt = _decrypt_key(self._key_path)
            self._conn = _open_encrypted(self._db_path, db_key, db_salt)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- peers --------------------------------------------------------------

    def get_peer(self, peer_id: int) -> dict:
        if peer_id not in self._peer_cache:
            row = self.conn.execute("SELECT value FROM t2 WHERE key = ? LIMIT 1", (peer_id,)).fetchone()
            self._peer_cache[peer_id] = (_parse_peer(row[0]) if row else None) or dict(_EMPTY_PEER)
        return self._peer_cache[peer_id]

    def _peers(self) -> dict[int, dict]:
        if self._all_peers is None:
            self._all_peers = {}
            for peer_id, value in self.conn.execute("SELECT key, value FROM t2"):
                peer = _parse_peer(value)
                if peer is not None:
                    self._all_peers[peer_id] = peer
                    self._peer_cache[peer_id] = peer
        return self._all_peers

    def _peer_chat(self, peer_id: int, peer: dict, last_ts: int | None = None, count: int | None = None) -> Chat:
        return Chat(
            platform=PLATFORM,
            id=str(peer_id),
            name=_peer_display_name(peer),
            last_message=format_ts(last_ts) if last_ts else "",
            phone=format_phone(peer["phone"]) if peer.get("phone") else "",
            username=peer.get("username", ""),
            is_group=bool(peer.get("title")) and not peer.get("first_name"),
            message_count=count,
        )

    def _sender(self, msg: dict, peer_id: int) -> str:
        if not msg["incoming"]:
            return "Me"
        return _peer_display_name(self.get_peer(msg["author_id"] or peer_id))

    # -- photos -------------------------------------------------------------

    def _get_photo_cache(self) -> dict[int, str]:
        if self._photo_cache is None:
            # _db_path is .../postbox/db/db_sqlite, media is at .../postbox/media/
            media_dir = self._db_path.parent.parent / "media" if self._db_path else None
            self._photo_cache = _build_photo_cache(media_dir) if media_dir else {}
        return self._photo_cache

    def _resolve_image_paths(self, msg: dict) -> list[str]:
        if not (msg.get("tags", 0) & 1):  # bit 0 = PhotoOrVideo
            return []
        rids = _extract_photo_resource_ids(msg.get("_post_text", b""))
        if not rids:
            return []
        cache = self._get_photo_cache()
        # Representations of one photo share a resource ID
        return [cache[rid] for rid in dict.fromkeys(rids) if rid in cache]

    # -- chats --------------------------------------------------------------

    def recent_chats(self, limit: int = 20) -> list[Chat]:
        rows = self.conn.execute(
            f"""
            SELECT {_PEER_PREFIX} AS peer, MAX(substr(key, 13, 4)) AS last_ts, COUNT(*) AS n
            FROM t7 GROUP BY peer ORDER BY last_ts DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        chats = []
        for peer_blob, ts_blob, count in rows:
            peer_id = struct.unpack(">q", peer_blob)[0]
            last_ts = struct.unpack(">i", ts_blob)[0]
            chats.append(self._peer_chat(peer_id, self.get_peer(peer_id), last_ts, count))
        return chats

    def find_chats(self, query: str) -> list[Chat]:
        """Find chats by name, username, or phone number."""
        wanted = query.strip().lstrip("@").casefold()
        key = phone_key(query) if looks_like_phone(query) else ""
        results = []
        for peer_id, peer in self._peers().items():
            searchable = f"{_peer_display_name(peer)} {peer.get('username', '')}".casefold()
            if wanted in searchable or (key and key in phone_key(peer.get("phone", ""))):
                results.append(self._peer_chat(peer_id, peer))
        return results

    def resolve_chat(self, identifier: str, strict: bool = False) -> Chat | None:
        stripped = identifier.strip()
        if stripped.lstrip("-").isdigit() and int(stripped) in self._peers():
            peer_id = int(stripped)
            return self._peer_chat(peer_id, self._peers()[peer_id])
        return choose_chat(stripped, self.find_chats(stripped), strict)

    # -- messages -----------------------------------------------------------

    def _to_message(self, key: bytes, value: bytes) -> Message | None:
        idx = _parse_message_key(key)
        msg = _parse_message_value(value)
        if msg is None:
            return None
        image_paths = self._resolve_image_paths(msg)
        text = msg["text"] or " ".join("[image]" for _ in image_paths)
        if not text:
            return None
        return Message(
            id=key.hex(),
            timestamp=format_ts(idx["timestamp"]),
            sender=self._sender(msg, idx["peer_id"]),
            text=text,
            is_from_me=not msg["incoming"],
            image_paths=image_paths,
        )

    def _messages(self, sql: str, params: tuple) -> list[Message]:
        return [m for key, value in self.conn.execute(sql, params) if (m := self._to_message(key, value))]

    def read_messages(self, peer_id: int, limit: int = 20) -> list[Message]:
        """Most recent messages in a chat, newest first."""
        return self._messages(
            f"SELECT key, value FROM t7 WHERE {_PEER_PREFIX} = ? ORDER BY {_TIME_ORDER} DESC LIMIT ?",
            (struct.pack(">q", peer_id), limit * 2 + 10),
        )[:limit]

    def read_around(self, message_id: str, count: int) -> list[Message]:
        """Up to `count` messages before and after a message, in chronological order."""
        key = bytes.fromhex(message_id)
        prefix, position, window = key[:8], key[12:20], count * 2 + 10
        older = self._messages(
            f"""SELECT key, value FROM t7 WHERE {_PEER_PREFIX} = ? AND {_TIME_ORDER} <= ?
            ORDER BY {_TIME_ORDER} DESC LIMIT ?""",
            (prefix, position, window),
        )[: count + 1]
        newer = self._messages(
            f"""SELECT key, value FROM t7 WHERE {_PEER_PREFIX} = ? AND {_TIME_ORDER} > ?
            ORDER BY {_TIME_ORDER} ASC LIMIT ?""",
            (prefix, position, window),
        )[:count]
        return list(reversed(older)) + newer

    def search_messages(self, query: str, limit: int = 20, peer_id: int | None = None) -> list[SearchHit]:
        """Search message text, newest first."""
        wanted = query.casefold()
        sql = "SELECT key, value FROM t7"
        params: tuple = ()
        if peer_id is not None:
            sql += f" WHERE {_PEER_PREFIX} = ?"
            params = (struct.pack(">q", peer_id),)
        sql += f" ORDER BY {_TIME_ORDER} DESC"

        hits = []
        for key, value in self.conn.execute(sql, params):
            msg = _parse_message_value(value)
            if msg is None or not msg["text"] or wanted not in msg["text"].casefold():
                continue
            idx = _parse_message_key(key)
            hits.append(SearchHit(
                id=key.hex(),
                timestamp=format_ts(idx["timestamp"]),
                sender=self._sender(msg, idx["peer_id"]),
                text=msg["text"],
                is_from_me=not msg["incoming"],
                platform=PLATFORM,
                chat_id=str(idx["peer_id"]),
                chat_name=_peer_display_name(self.get_peer(idx["peer_id"])),
            ))
            if len(hits) >= limit:
                break
        return hits

    def stats(self) -> dict:
        messages = self.conn.execute("SELECT COUNT(*) FROM t7").fetchone()[0]
        chats = self.conn.execute(f"SELECT COUNT(DISTINCT {_PEER_PREFIX}) FROM t7").fetchone()[0]
        return {"messages": messages, "chats": chats}


@functools.cache
def get_db() -> TelegramDB:
    """Process-wide TelegramDB, so the database is opened once per command."""
    return TelegramDB()
