# coding=utf-8
"""Private publication CAS ledger and immutable snapshots, without business rules.

Only an absent manifest returns ``(None, None)``. All other read failures raise
PublicationError. A failed remote write can have committed: callers must not
perform SMTP side effects after that error, and must reload for reconciliation.
S3 must implement conditional PutObject; there is no unconditional fallback.
The caller supplies JSON only (never current SMTP/cloud credentials).
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import closing
from pathlib import Path


DOCUMENT_LIMIT = 8 * 1024 * 1024
SNAPSHOT_LIMIT = 32 * 1024 * 1024
_PREFIX = "meta/publication-v1"
_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REVISION = re.compile(r"[0-9a-f]{32}\Z")
_SCHEMA = """
CREATE TABLE manifest (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    version TEXT NOT NULL,
    payload BLOB NOT NULL,
    digest TEXT NOT NULL
);
CREATE TABLE snapshots (
    snapshot_id TEXT PRIMARY KEY,
    payload BLOB NOT NULL,
    digest TEXT NOT NULL
);
PRAGMA user_version = 1;
"""


class PublicationError(RuntimeError):
    """Safe-to-log storage failure; messages never interpolate payload/provider text."""


class PublicationConflict(PublicationError):
    """CAS precondition failed, or an immutable snapshot ID has different content."""


def _validate_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise PublicationError("Publication payload is not strict JSON")


def _encode(document, limit):
    try:
        if type(document) is not dict:
            raise PublicationError("Publication payload must be a JSON object")
        _validate_json(document)
        result = json.dumps(document, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(result) > limit:
            raise PublicationError("Publication object exceeds size limit")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PublicationError("Publication payload cannot be serialized") from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(payload, limit):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
        raise PublicationError("Publication object is empty, invalid or oversized")
    try:
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
        # Reject NaN, Infinity, scalar roots and invalid Unicode on disk too.
        _encode(document, limit)
        return document
    except (ValueError, UnicodeError, RecursionError):
        raise PublicationError("Publication object is corrupt") from None


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _snapshot_id(value):
    if type(value) is not str or not _SNAPSHOT_ID.fullmatch(value):
        raise PublicationError("Invalid publication snapshot ID")
    return value


def _version(value):
    if value is not None and (type(value) is not str or not value or len(value) > 256
                              or value == "*" or any(ord(c) < 33 or ord(c) > 126 for c in value)):
        raise PublicationError("Invalid publication version token")


def _limits(document, snapshot):
    if (type(document) is not int or not 0 < document <= DOCUMENT_LIMIT
            or type(snapshot) is not int or not 0 < snapshot <= SNAPSHOT_LIMIT):
        raise PublicationError("Invalid publication size limits")


class LocalPublicationStore:
    """SQLite under data_dir/meta (0700), with a 0600 database and FULL commits.

    A complete empty database is atomically installed on first use. Existing
    zero-byte, truncated, or unrelated databases are errors, never bootstrap.
    Connections are short-lived, so separate workers/processes share SQLite CAS.
    """

    def __init__(self, data_dir, *, max_document_bytes=DOCUMENT_LIMIT,
                 max_snapshot_bytes=SNAPSHOT_LIMIT):
        _limits(max_document_bytes, max_snapshot_bytes)
        self.max_document_bytes = max_document_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self.path = Path(data_dir).absolute() / "meta" / "publication-v1.sqlite3"
        self._closed = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._private_path(self.path.parent, directory=True)
            if not self.path.exists() and not self.path.is_symlink():
                self._initialize()
            with closing(self._connect()) as connection:
                if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                    raise PublicationError("Publication database format is corrupt or unsupported")
                connection.execute("SELECT singleton, version, payload, digest FROM manifest LIMIT 0")
                connection.execute("SELECT snapshot_id, payload, digest FROM snapshots LIMIT 0")
        except (OSError, sqlite3.Error):
            raise PublicationError("Cannot open private publication database") from None

    @staticmethod
    def _private_path(path, *, directory=False):
        info = path.lstat()
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not correct_type or info.st_uid != os.geteuid() or (not directory and info.st_nlink != 1):
            raise PublicationError("Publication storage path is not a private owned file or directory")
        os.chmod(path, 0o700 if directory else 0o600)

    def _initialize(self):
        fd, temporary = tempfile.mkstemp(prefix=".publication-", dir=self.path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.executescript(_SCHEMA)
                connection.commit()
            with open(temporary, "rb") as handle:
                os.fsync(handle.fileno())
            try:
                os.link(temporary, self.path)
            except FileExistsError:
                pass  # Another process atomically installed the database first.
            os.unlink(temporary)
            temporary = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _connect(self):
        if self._closed:
            raise PublicationError("Publication store is closed")
        self._private_path(self.path.parent, directory=True)
        self._private_path(self.path)
        # mode=rw prevents a disappearing database from being recreated empty.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30)
        try:
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _row_document(row, limit):
        payload, digest = row
        if type(payload) is not bytes or type(digest) is not str or _digest(payload) != digest:
            raise PublicationError("Publication object checksum mismatch")
        return _decode(payload, limit)

    def _manifest(self, connection):
        row = connection.execute("SELECT version, length(payload) FROM manifest WHERE singleton=1").fetchone()
        if row is None:
            return None, None
        version, size = row
        if type(version) is not str or not _REVISION.fullmatch(version):
            raise PublicationError("Publication database version is corrupt")
        if type(size) is not int or not 0 < size <= self.max_document_bytes:
            raise PublicationError("Publication manifest is empty or oversized")
        content = connection.execute("SELECT payload, digest FROM manifest WHERE singleton=1").fetchone()
        return self._row_document(content, self.max_document_bytes), version

    def load(self) -> tuple[dict | None, str | None]:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return self._manifest(connection)
        except (OSError, sqlite3.Error):
            raise PublicationError("Cannot read publication database") from None

    def save(self, document: dict, expected_version: str | None) -> str:
        _version(expected_version)
        payload = _encode(document, self.max_document_bytes)
        version = uuid.uuid4().hex
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                _, current_version = self._manifest(connection)
                if current_version != expected_version:
                    raise PublicationConflict("Publication manifest changed")
                connection.execute(
                    "INSERT INTO manifest(singleton, version, payload, digest) VALUES(1, ?, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version, "
                    "payload=excluded.payload, digest=excluded.digest",
                    (version, payload, _digest(payload)),
                )
            return version
        except (OSError, sqlite3.Error):
            raise PublicationError("Cannot commit publication database; reload before further action") from None

    def _snapshot(self, connection, snapshot_id):
        row = connection.execute("SELECT length(payload) FROM snapshots WHERE snapshot_id=?",
                                 (snapshot_id,)).fetchone()
        if row is None:
            raise PublicationError("Publication snapshot is missing")
        if type(row[0]) is not int or not 0 < row[0] <= self.max_snapshot_bytes:
            raise PublicationError("Publication snapshot is empty or oversized")
        content = connection.execute("SELECT payload, digest FROM snapshots WHERE snapshot_id=?",
                                     (snapshot_id,)).fetchone()
        return self._row_document(content, self.max_snapshot_bytes)

    def put_snapshot(self, snapshot_id, payload):
        _snapshot_id(snapshot_id)
        encoded = _encode(payload, self.max_snapshot_bytes)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                exists = connection.execute("SELECT 1 FROM snapshots WHERE snapshot_id=?",
                                            (snapshot_id,)).fetchone()
                if exists:
                    existing = self._snapshot(connection, snapshot_id)
                    if _encode(existing, self.max_snapshot_bytes) != encoded:
                        raise PublicationConflict("Publication snapshot ID already has different content")
                else:
                    connection.execute("INSERT INTO snapshots VALUES (?, ?, ?)",
                                       (snapshot_id, encoded, _digest(encoded)))
        except (OSError, sqlite3.Error):
            raise PublicationError("Cannot persist immutable publication snapshot") from None

    def get_snapshot(self, snapshot_id):
        _snapshot_id(snapshot_id)
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                return self._snapshot(connection, snapshot_id)
        except (OSError, sqlite3.Error):
            raise PublicationError("Cannot read publication snapshot") from None

    def close(self):
        self._closed = True


def _remote_error(error):
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return None
    detail, metadata = response.get("Error", {}), response.get("ResponseMetadata", {})
    if not isinstance(detail, dict) or not isinstance(metadata, dict):
        return None
    code, status = detail.get("Code"), metadata.get("HTTPStatusCode")
    if code in ("NoSuchKey", "NotFound", "404", "Not Found") and (status is None or status == 404):
        return "missing"
    if code in ("PreconditionFailed", "412") and (status is None or status == 412):
        return "precondition"
    if code in ("ConditionalRequestConflict", "409") and (status is None or status == 409):
        return "conflict"
    if status in (401, 403):
        return "denied"
    if status == 501 or code in ("NotImplemented", "UnsupportedOperation"):
        return "unsupported"
    return None


class RemotePublicationStore:
    """Use the existing S3 client/bucket, without local caching or bucket listing.

    Version tokens are exact ETags. Each manifest includes a fresh revision to
    prevent ABA when a caller saves the same JSON again. Unknown write outcomes
    raise instead of retrying or returning a fabricated version. Snapshots are
    create-only, with a bounded read/compare after a precondition failure.
    """

    manifest_key = _PREFIX + "/manifest.json"

    def __init__(self, s3_client, bucket_name, *, max_document_bytes=DOCUMENT_LIMIT,
                 max_snapshot_bytes=SNAPSHOT_LIMIT):
        _limits(max_document_bytes, max_snapshot_bytes)
        if type(bucket_name) is not str or not bucket_name:
            raise PublicationError("Publication bucket is not configured")
        self.s3_client = s3_client
        self.bucket_name = bucket_name
        self.max_document_bytes = max_document_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self._closed = False
        try:
            members = s3_client.meta.service_model.operation_model("PutObject").input_shape.members
            supported = "IfMatch" in members and "IfNoneMatch" in members
        except Exception:
            supported = False
        if not supported:
            raise PublicationError("S3 SDK lacks conditional PutObject support; install the locked boto3/botocore versions")

    def _open(self):
        if self._closed:
            raise PublicationError("Publication store is closed")

    @staticmethod
    def _etag(response):
        version = response.get("ETag")
        _version(version)
        if version is None:
            raise PublicationError("S3 publication response has no version token")
        # A version is one strong ETag, never a wildcard or condition list. Do
        # not allow malformed provider values to broaden the next CAS request.
        if (len(version) < 3 or version[0] != '"' or version[-1] != '"'
                or any(char in version[1:-1] for char in ('"', "\\", "*", ","))):
            raise PublicationError("S3 publication version is not a single strong ETag")
        return version

    def _read(self, key, limit, *, allow_missing=False):
        self._open()
        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
        except Exception as error:
            kind = _remote_error(error)
            if kind == "missing":
                if allow_missing:
                    return None, None
                raise PublicationError("S3 publication snapshot is missing") from None
            if kind == "denied":
                raise PublicationError("S3 publication read was denied") from None
            raise PublicationError("S3 publication read is unavailable") from None
        body = None
        try:
            body = response["Body"]
            size = response.get("ContentLength")
            if type(size) is not int or not 0 < size <= limit:
                raise PublicationError("S3 publication object has invalid or oversized length")
            chunks, count = [], 0
            while count <= limit:
                chunk = body.read(min(64 * 1024, limit + 1 - count))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise PublicationError("S3 publication response is invalid")
                chunks.append(chunk)
                count += len(chunk)
            if count != size or count > limit:
                raise PublicationError("S3 publication object is truncated or oversized")
            return _decode(b"".join(chunks), limit), self._etag(response)
        except PublicationError:
            raise
        except Exception:
            raise PublicationError("Cannot decode S3 publication object") from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass  # Fully read/verified content does not depend on socket close.

    def _put(self, key, encoded, **condition):
        self._open()
        try:
            return self.s3_client.put_object(Bucket=self.bucket_name, Key=key, Body=encoded,
                                            ContentType="application/json", **condition)
        except Exception as error:
            kind = _remote_error(error)
            if kind == "precondition":
                raise PublicationConflict("S3 publication precondition failed") from None
            if kind == "conflict":
                raise PublicationConflict("S3 publication conditional write conflicted") from None
            if kind == "denied":
                raise PublicationError("S3 publication conditional write was denied") from None
            if kind == "unsupported":
                raise PublicationError("S3 service does not support the required conditional write") from None
            # Includes unsupported service/SDK input, denial and response loss.
            # Never format the original provider exception or attempt a plain PUT.
            raise PublicationError("S3 publication write failed or outcome is unknown; reload before further action") from None

    def load(self) -> tuple[dict | None, str | None]:
        # Small fixed envelope overhead in addition to the public payload bound.
        envelope, version = self._read(self.manifest_key, self.max_document_bytes + 1024, allow_missing=True)
        if envelope is None:
            return None, None
        if (set(envelope) != {"format", "revision", "document", "sha256"}
                or type(envelope["format"]) is not int or envelope["format"] != 1
                or type(envelope["revision"]) is not str or not _REVISION.fullmatch(envelope["revision"])):
            raise PublicationError("S3 publication manifest format is corrupt or unsupported")
        encoded = _encode(envelope["document"], self.max_document_bytes)
        if _digest(encoded) != envelope["sha256"]:
            raise PublicationError("S3 publication manifest checksum mismatch")
        return envelope["document"], version

    def save(self, document: dict, expected_version: str | None) -> str:
        if expected_version is not None:
            self._etag({"ETag": expected_version})
        encoded = _encode(document, self.max_document_bytes)
        # Decode the validated bytes so mutation by the caller cannot alter the
        # digest/body pairing while the request is being constructed.
        envelope = {"format": 1, "revision": uuid.uuid4().hex,
                    "document": _decode(encoded, self.max_document_bytes), "sha256": _digest(encoded)}
        condition = {"IfNoneMatch": "*"} if expected_version is None else {"IfMatch": expected_version}
        result = self._put(self.manifest_key, _encode(envelope, self.max_document_bytes + 1024), **condition)
        try:
            return self._etag(result)
        except Exception:
            raise PublicationError("S3 publication write has no usable receipt; reload before further action") from None

    def put_snapshot(self, snapshot_id, payload):
        _snapshot_id(snapshot_id)
        encoded = _encode(payload, self.max_snapshot_bytes)
        envelope = {"format": 1, "snapshot_id": snapshot_id, "sha256": _digest(encoded),
                    "payload": _decode(encoded, self.max_snapshot_bytes)}
        try:
            result = self._put(self._snapshot_key(snapshot_id), _encode(envelope, self.max_snapshot_bytes + 1024),
                               IfNoneMatch="*")
            try:
                self._etag(result)
            except Exception:
                raise PublicationError("S3 snapshot write has no usable receipt; reload before further action") from None
        except PublicationConflict:
            # Both 412 and concurrent 409 are safe only if a complete same-content
            # object can now be read; a missing object remains a hard read failure.
            if _encode(self.get_snapshot(snapshot_id), self.max_snapshot_bytes) != encoded:
                raise PublicationConflict("Publication snapshot ID already has different content") from None

    @staticmethod
    def _snapshot_key(snapshot_id):
        return _PREFIX + "/snapshots/" + snapshot_id + ".json"

    def get_snapshot(self, snapshot_id):
        _snapshot_id(snapshot_id)
        envelope, _ = self._read(self._snapshot_key(snapshot_id), self.max_snapshot_bytes + 1024)
        if (set(envelope) != {"format", "snapshot_id", "sha256", "payload"}
                or type(envelope["format"]) is not int or envelope["format"] != 1
                or envelope["snapshot_id"] != snapshot_id):
            raise PublicationError("S3 publication snapshot format is corrupt or unsupported")
        encoded = _encode(envelope["payload"], self.max_snapshot_bytes)
        if _digest(encoded) != envelope["sha256"]:
            raise PublicationError("S3 publication snapshot checksum mismatch")
        return envelope["payload"]

    def close(self):
        # This store borrows the backend client; it must not close that client.
        self._closed = True
