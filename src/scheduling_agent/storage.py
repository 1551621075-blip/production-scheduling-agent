"""Local, strictly bounded JSON persistence and transactional authorization.

SQLite transactions cover all relational changes. Immutable artifacts may remain
unreferenced after a rollback; they are never published partially or overwritten.
The private local directory is the trust boundary, not a hostile operating system.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import DomainError

MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 40
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,200}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ROLES = frozenset({"admin", "reviewer", "operator"})
_APPLICATION_ID = 0x52365341
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id TEXT PRIMARY KEY NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','reviewer','operator')),
    plants TEXT NOT NULL,
    acl_version INTEGER NOT NULL CHECK(acl_version >= 1),
    active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS objects(
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    owner TEXT NOT NULL REFERENCES users(user_id),
    plant_id TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    content_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(kind, object_id)
);
CREATE INDEX IF NOT EXISTS objects_owner_plant ON objects(owner, plant_id, kind);
CREATE TABLE IF NOT EXISTS receipts(
    user_id TEXT NOT NULL REFERENCES users(user_id),
    action_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_hash TEXT NOT NULL,
    PRIMARY KEY(user_id, action_id)
);
CREATE TABLE IF NOT EXISTS audit(
    id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL REFERENCES users(user_id),
    plant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY(kind, object_id) REFERENCES objects(kind, object_id)
);
CREATE INDEX IF NOT EXISTS audit_owner_plant ON audit(owner, plant_id, id);
CREATE TABLE IF NOT EXISTS calls(
    call_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started','completed','failed','unknown')),
    tokens INTEGER NULL CHECK(tokens >= 0),
    elapsed_s REAL NOT NULL DEFAULT 0,
    response_hash TEXT NULL,
    error_code TEXT NULL,
    created_at_ms INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str
    plants: tuple[str, ...]
    acl_version: int


def _integrity() -> DomainError:
    return DomainError("integrity", "Stored data is unavailable or unsafe", 409)


def _invalid() -> DomainError:
    return DomainError("validation", "Invalid storage arguments", 422)


def _identifier(value) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _invalid()
    return value


def _plants(value) -> list[str]:
    if type(value) is not list or len(value) > 100:
        raise _invalid()
    result = [_identifier(plant) for plant in value]
    if len(set(result)) != len(result):
        raise _invalid()
    return result


def _limit(value) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise _invalid()
    return value


def _canonical(value, *, object_only: bool = True) -> bytes:
    """Count encoded bytes before allocating an expanded canonical document."""
    remaining = MAX_JSON_BYTES
    ancestors: set[int] = set()

    def charge(size):
        nonlocal remaining
        remaining -= size
        if remaining < 0:
            raise DomainError("artifact_too_large", "JSON exceeds the local size limit", 413)

    def string(item):
        if len(item) > remaining:
            raise DomainError("artifact_too_large", "JSON exceeds the local size limit", 413)
        charge(len(json.dumps(item, ensure_ascii=False).encode("utf-8")))

    def visit(item, depth):
        if depth > MAX_JSON_DEPTH:
            raise ValueError
        if item is None:
            charge(4)
        elif type(item) is bool:
            charge(4 if item else 5)
        elif type(item) is int:
            charge(len(str(item)))
        elif type(item) is float and math.isfinite(item):
            charge(len(json.dumps(item, allow_nan=False)))
        elif type(item) is str:
            string(item)
        elif type(item) in (dict, list):
            identity = id(item)
            if identity in ancestors:
                raise ValueError
            ancestors.add(identity)
            charge(2 + max(0, len(item) - 1))
            if type(item) is dict:
                charge(len(item))
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError
                    string(key)
                    visit(child, depth + 1)
            else:
                for child in item:
                    visit(child, depth + 1)
            ancestors.remove(identity)
        else:
            raise ValueError

    try:
        if object_only and type(value) is not dict:
            raise ValueError
        visit(value, 0)
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, RuntimeError):
        raise DomainError("invalid_json", "Invalid finite JSON document", 422) from None
    if len(encoded) > MAX_JSON_BYTES:
        raise DomainError("artifact_too_large", "JSON exceeds the local size limit", 413)
    return encoded


def _decode(content: bytes, *, object_only: bool = True):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def constant(_value):
        raise ValueError

    # Reject deep input before the JSON decoder can allocate nested containers.
    depth = 0
    quoted = escaped = False
    try:
        if len(content) > MAX_JSON_BYTES:
            raise ValueError
        for char in content:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char in (91, 123):
                depth += 1
                if depth > MAX_JSON_DEPTH + 1:
                    raise ValueError
            elif char in (93, 125):
                depth -= 1
        payload = json.loads(
            content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
        if _canonical(payload, object_only=object_only) != content:
            raise ValueError
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError, DomainError):
        raise _integrity() from None


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _regular(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _integrity()


@contextmanager
def _directory(path: Path, *, create: bool = False):
    """Walk from / using no-follow directory descriptors, including ancestors."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _publish(directory: int, source: str, destination: str):
    """Atomic exclusive rename avoids even a transient two-link artifact inode."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        flags = 4  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        flags = 1  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "Exclusive artifact publication is unavailable")
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(source), directory, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, "Artifact publication failed")


class Store:
    def __init__(self, root: Path):
        self._connections: dict[sqlite3.Connection, bool] = {}
        try:
            candidate = Path(root)
            if not candidate.is_absolute() or ".." in candidate.parts:
                raise _integrity()
            # No expanduser(), cwd-relative paths, symlink resolution, or profile access.
            self.root = candidate
            self.database = self.root / "data.sqlite3"
            self.artifacts = self.root / "artifacts"
            with _directory(self.root, create=True) as directory:
                self._root_identity = _identity(os.fstat(directory))
                try:
                    os.mkdir("artifacts", mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                with _directory(self.artifacts) as artifacts:
                    self._artifacts_identity = _identity(os.fstat(artifacts))
                try:
                    fd = os.open(
                        "data.sqlite3",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                except FileExistsError:
                    pass
                else:
                    try:
                        _regular(os.fstat(fd))
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.fsync(directory)
                info = os.stat("data.sqlite3", dir_fd=directory, follow_symlinks=False)
                _regular(info)
                self._database_identity = _identity(info)
            with self._connection(readonly=False, initializing=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    application = conn.execute("PRAGMA application_id").fetchone()[0]
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if application == 0:
                        if conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                            raise _integrity()
                    elif application != _APPLICATION_ID or version != 1:
                        raise _integrity()
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    conn.execute("PRAGMA user_version=1")
                    self._check_database_files()
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except (OSError, ValueError, TypeError):
            raise _integrity() from None

    @contextmanager
    def _root_directory(self):
        with _directory(self.root) as directory:
            if _identity(os.fstat(directory)) != self._root_identity:
                raise _integrity()
            yield directory
            with _directory(self.root) as current:
                if _identity(os.fstat(current)) != self._root_identity:
                    raise _integrity()

    def _check_database_files(self):
        try:
            with self._root_directory() as directory:
                for suffix in ("", "-wal", "-shm", "-journal"):
                    for attempt in range(3):
                        try:
                            info = os.stat(
                                "data.sqlite3" + suffix, dir_fd=directory, follow_symlinks=False
                            )
                        except FileNotFoundError:
                            if not suffix:
                                raise _integrity() from None
                            break
                        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            if not suffix and _identity(info) != self._database_identity:
                                raise _integrity()
                            break
                        # Darwin can observe a SQLite WAL/SHM inode just after its
                        # unlink. Re-stat the name at most twice; never accept the
                        # zero-link observation itself, symlinks, or hardlinks.
                        if (
                            sys.platform == "darwin"
                            and suffix in ("-wal", "-shm")
                            and stat.S_ISREG(info.st_mode)
                            and info.st_nlink == 0
                            and attempt < 2
                        ):
                            continue
                        raise _integrity()
        except OSError:
            raise _integrity() from None

    @contextmanager
    def _connection(self, *, readonly: bool, initializing: bool = False):
        conn = None
        try:
            self._check_database_files()
            # mode=rw never silently creates a replacement for a removed database.
            conn = sqlite3.connect(
                self.database.as_uri() + "?mode=rw", uri=True, timeout=15, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            self._check_database_files()
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA synchronous=FULL")
            journal = "PRAGMA journal_mode=WAL" if initializing else "PRAGMA journal_mode"
            if conn.execute(journal).fetchone()[0] != "wal":
                raise _integrity()
            if readonly:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
            self._connections[conn] = readonly
            yield conn
            self._check_database_files()
        except (sqlite3.Error, OSError):
            raise DomainError("storage", "Local storage operation failed", 409) from None
        finally:
            if conn is not None:
                self._connections.pop(conn, None)
                conn.close()

    @contextmanager
    def connect(self):
        """Query-only connection; related reads share one SQLite snapshot."""
        with self._connection(readonly=True) as conn:
            yield conn

    @contextmanager
    def transaction(self):
        with self._connection(readonly=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                self._check_database_files()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _check_conn(self, conn, *, write: bool = False):
        if (
            not isinstance(conn, sqlite3.Connection)
            or conn not in self._connections
            or not conn.in_transaction
            or (write and self._connections[conn])
        ):
            raise DomainError(
                "transaction_required", "An active store transaction is required", 409
            )

    @staticmethod
    def _principal(row) -> Principal:
        try:
            user_id = _identifier(row["user_id"])
            role = row["role"]
            if type(role) is not str or role not in _ROLES:
                raise ValueError
            plants = _plants(_decode(row["plants"].encode("utf-8"), object_only=False))
            version = row["acl_version"]
            if type(version) is not int or not 1 <= version <= 2**63 - 1:
                raise ValueError
            if type(row["active"]) is not int or row["active"] != 1:
                raise ValueError
            return Principal(user_id, role, tuple(plants), version)
        except (ValueError, TypeError, AttributeError, KeyError, DomainError):
            raise DomainError("forbidden", "Current authorization is required", 403) from None

    def create_user(self, user_id, token, role, plants: list[str]) -> Principal:
        user_id = _identifier(user_id)
        if type(token) is not str or _TOKEN.fullmatch(token) is None:
            raise _invalid()
        if type(role) is not str or role not in _ROLES:
            raise _invalid()
        checked = _plants(plants)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO users VALUES(?,?,?,?,1,1)",
                    (user_id, token_hash, role, _canonical(checked, object_only=False).decode()),
                )
            except sqlite3.IntegrityError:
                raise DomainError("conflict", "User identity already exists", 409) from None
        return Principal(user_id, role, tuple(checked), 1)

    def authenticate(self, token: str) -> Principal:
        if type(token) is not str or _TOKEN.fullmatch(token) is None:
            raise DomainError("unauthorized", "Authentication required", 401)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE token_hash=? AND active=1", (token_hash,)
            ).fetchone()
            if row is not None:
                try:
                    return self._principal(row)
                except DomainError:
                    pass
        raise DomainError("unauthorized", "Authentication required", 401)

    def require(self, principal, plant_id=None, roles=None, *, conn=None) -> None:
        if conn is None:
            with self.connect() as current:
                self.require(principal, plant_id, roles, conn=current)
            return
        self._check_conn(conn)
        if (
            type(principal) is not Principal
            or type(principal.user_id) is not str
            or type(principal.role) is not str
            or type(principal.plants) is not tuple
            or any(type(plant) is not str for plant in principal.plants)
            or type(principal.acl_version) is not int
        ):
            raise DomainError("forbidden", "Current authorization is required", 403)
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (principal.user_id,)).fetchone()
        if row is None or self._principal(row) != principal:
            raise DomainError("forbidden", "Current authorization is required", 403)
        if plant_id is not None and _identifier(plant_id) not in principal.plants:
            raise DomainError("forbidden", "Current authorization is required", 403)
        if roles is not None:
            if type(roles) not in (list, tuple, set, frozenset) or any(
                type(role) is not str or role not in _ROLES for role in roles
            ):
                raise _invalid()
            if principal.role not in roles:
                raise DomainError("forbidden", "Current authorization is required", 403)

    def set_user(self, user_id, *, active=None, role=None, plants=None) -> None:
        user_id = _identifier(user_id)
        if active is not None and type(active) is not bool:
            raise _invalid()
        if role is not None and (type(role) is not str or role not in _ROLES):
            raise _invalid()
        checked = None if plants is None else _plants(plants)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise DomainError("not_found", "User is unavailable", 404)
            if active is role is plants is None:
                return
            if type(row["acl_version"]) is not int or row["acl_version"] >= 2**63 - 1:
                raise _integrity()
            conn.execute(
                "UPDATE users SET active=?,role=?,plants=?,acl_version=acl_version+1 "
                "WHERE user_id=?",
                (
                    row["active"] if active is None else int(active),
                    row["role"] if role is None else role,
                    row["plants"]
                    if checked is None
                    else _canonical(checked, object_only=False).decode(),
                    user_id,
                ),
            )

    @contextmanager
    def _artifact_directory(self):
        try:
            with self._root_directory(), _directory(self.artifacts) as directory:
                if _identity(os.fstat(directory)) != self._artifacts_identity:
                    raise _integrity()
                yield directory
                with _directory(self.artifacts) as current:
                    if _identity(os.fstat(current)) != self._artifacts_identity:
                        raise _integrity()
        except OSError:
            raise _integrity() from None

    @staticmethod
    def _artifact_bytes(directory: int, filename: str) -> bytes:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            _regular(before)
            if not 0 < before.st_size <= MAX_JSON_BYTES:
                raise _integrity()
            chunks = []
            remaining = MAX_JSON_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
                chunks.append(chunk)
            content = b"".join(chunks)
            after = os.fstat(fd)
            named = os.stat(filename, dir_fd=directory, follow_symlinks=False)
            _regular(after)
            _regular(named)
            if (
                len(content) != before.st_size
                or len(content) != after.st_size
                or len(content) > MAX_JSON_BYTES
                or _identity(before) != _identity(named)
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise _integrity()
            return content
        finally:
            os.close(fd)

    def put_artifact(self, payload: dict) -> str:
        content = _canonical(payload)
        content_hash = hashlib.sha256(content).hexdigest()
        filename = content_hash + ".json"
        with self._artifact_directory() as directory:
            temporary = ".pending-" + secrets.token_hex(16)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    _regular(os.fstat(stream.fileno()))
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                    _regular(os.fstat(stream.fileno()))
                try:
                    _publish(directory, temporary, filename)
                except FileExistsError:
                    pass
                existing = self._artifact_bytes(directory, filename)
                if existing != content:
                    raise _integrity()
                # Existing identical hashes are still checked for all JSON bounds.
                _decode(existing)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return content_hash

    def get_artifact(self, content_hash: str) -> dict:
        if type(content_hash) is not str or _SHA256.fullmatch(content_hash) is None:
            raise _integrity()
        with self._artifact_directory() as directory:
            content = self._artifact_bytes(directory, content_hash + ".json")
        if hashlib.sha256(content).hexdigest() != content_hash:
            raise _integrity()
        return _decode(content)

    def _object(self, row) -> dict:
        try:
            result = {
                key: row[key]
                for key in (
                    "kind",
                    "object_id",
                    "owner",
                    "plant_id",
                    "status",
                    "version",
                    "content_hash",
                )
            }
            for key in ("kind", "object_id", "owner", "plant_id", "status"):
                _identifier(result[key])
            if type(result["version"]) is not int or not 1 <= result["version"] <= 2**63 - 1:
                raise ValueError
            result["payload"] = self.get_artifact(result["content_hash"])
            return result
        except (ValueError, TypeError, KeyError, DomainError):
            raise _integrity() from None

    def create_object(self, conn, *, kind, object_id, principal, plant_id, status, payload) -> dict:
        self._check_conn(conn, write=True)
        self.require(principal, plant_id, conn=conn)
        kind, object_id, plant_id, status = map(_identifier, (kind, object_id, plant_id, status))
        content_hash = self.put_artifact(payload)
        timestamp = time.time_ns() // 1_000_000
        try:
            conn.execute(
                "INSERT INTO objects VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    kind,
                    object_id,
                    principal.user_id,
                    plant_id,
                    status,
                    content_hash,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            raise DomainError("conflict", "Object identity already exists", 409) from None
        return self.get_object(kind, object_id, principal, conn=conn)

    def get_object(self, kind, object_id, principal, *, conn=None) -> dict:
        if conn is None:
            with self.connect() as current:
                return self.get_object(kind, object_id, principal, conn=current)
        self._check_conn(conn)
        self.require(principal, conn=conn)
        kind, object_id = map(_identifier, (kind, object_id))
        row = conn.execute(
            "SELECT * FROM objects WHERE kind=? AND object_id=? AND owner=?",
            (kind, object_id, principal.user_id),
        ).fetchone()
        if row is None or row["plant_id"] not in principal.plants:
            raise DomainError("not_found", "Object is unavailable", 404)
        return self._object(row)

    def update_object(
        self, conn, *, kind, object_id, principal, expected_version, status, payload
    ) -> dict:
        self._check_conn(conn, write=True)
        current = self.get_object(kind, object_id, principal, conn=conn)
        if type(expected_version) is not int or not 1 <= expected_version < 2**63 - 1:
            raise _invalid()
        status = _identifier(status)
        if current["version"] != expected_version:
            raise DomainError("conflict", "Object version has changed", 409)
        content_hash = self.put_artifact(payload)
        result = conn.execute(
            "UPDATE objects SET status=?,content_hash=?,version=version+1,updated_at_ms=? "
            "WHERE kind=? AND object_id=? AND owner=? AND version=?",
            (
                status,
                content_hash,
                time.time_ns() // 1_000_000,
                kind,
                object_id,
                principal.user_id,
                expected_version,
            ),
        )
        if result.rowcount != 1:
            raise DomainError("conflict", "Object version has changed", 409)
        return self.get_object(kind, object_id, principal, conn=conn)

    def list_objects(self, kind, principal, *, limit=100) -> list[dict]:
        kind, limit = _identifier(kind), _limit(limit)
        with self.connect() as conn:
            self.require(principal, conn=conn)
            placeholders = ",".join("?" for _ in principal.plants)
            rows = conn.execute(
                "SELECT * FROM objects WHERE kind=? AND owner=? "
                f"AND plant_id IN ({placeholders}) ORDER BY created_at_ms DESC,rowid DESC LIMIT ?",
                (kind, principal.user_id, *principal.plants, limit),
            ).fetchall()
            return [self._object(row) for row in rows]

    def audit(self, conn, principal, *, kind, object_id, action, detail: dict) -> None:
        self._check_conn(conn, write=True)
        current = self.get_object(kind, object_id, principal, conn=conn)
        action = _identifier(action)
        detail_hash = self.put_artifact(detail)
        conn.execute(
            "INSERT INTO audit(owner,plant_id,kind,object_id,action,detail_hash,created_at_ms) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                principal.user_id,
                current["plant_id"],
                kind,
                object_id,
                action,
                detail_hash,
                time.time_ns() // 1_000_000,
            ),
        )

    def audit_events(self, principal, *, limit=100) -> list[dict]:
        limit = _limit(limit)
        with self.connect() as conn:
            self.require(principal, conn=conn)
            placeholders = ",".join("?" for _ in principal.plants)
            rows = conn.execute(
                "SELECT a.* FROM audit a JOIN objects o ON "
                "a.kind=o.kind AND a.object_id=o.object_id AND a.owner=o.owner "
                "AND a.plant_id=o.plant_id WHERE a.owner=? "
                f"AND a.plant_id IN ({placeholders}) ORDER BY a.id DESC LIMIT ?",
                (principal.user_id, *principal.plants, limit),
            ).fetchall()
            events = []
            for row in rows:
                try:
                    event = {
                        key: row[key] for key in ("kind", "object_id", "action", "created_at_ms")
                    }
                    for key in ("kind", "object_id", "action"):
                        _identifier(event[key])
                    if type(event["created_at_ms"]) is not int or event["created_at_ms"] < 0:
                        raise ValueError
                    event["detail"] = self.get_artifact(row["detail_hash"])
                    events.append(event)
                except (ValueError, TypeError, DomainError):
                    raise _integrity() from None
            return events

    def receipt(self, conn, principal, action_id: str, request: dict) -> dict | None:
        self._check_conn(conn)
        self.require(principal, conn=conn)
        action_id = _identifier(action_id)
        request_hash = hashlib.sha256(_canonical(request)).hexdigest()
        row = conn.execute(
            "SELECT * FROM receipts WHERE user_id=? AND action_id=?",
            (principal.user_id, action_id),
        ).fetchone()
        if row is None:
            return None
        if type(row["request_hash"]) is not str or not _SHA256.fullmatch(row["request_hash"]):
            raise _integrity()
        if row["request_hash"] != request_hash:
            raise DomainError("idempotency_conflict", "Action identity is already bound", 409)
        return self.get_artifact(row["response_hash"])

    def record_receipt(self, conn, principal, action_id, request, response: dict) -> None:
        self._check_conn(conn, write=True)
        previous = self.receipt(conn, principal, action_id, request)
        response_hash = hashlib.sha256(_canonical(response)).hexdigest()
        if previous is not None:
            if hashlib.sha256(_canonical(previous)).hexdigest() != response_hash:
                raise DomainError("idempotency_conflict", "Action identity is already bound", 409)
            return
        response_hash = self.put_artifact(response)
        request_hash = hashlib.sha256(_canonical(request)).hexdigest()
        conn.execute(
            "INSERT INTO receipts VALUES(?,?,?,?)",
            (principal.user_id, action_id, request_hash, response_hash),
        )
