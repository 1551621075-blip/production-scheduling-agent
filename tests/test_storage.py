from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scheduling_agent import storage
from scheduling_agent.errors import DomainError
from scheduling_agent.storage import MAX_JSON_BYTES, Principal, Store

TOKEN = "".join(("r6-operator-secret-", "x" * 24))
OTHER_TOKEN = "".join(("r6-other-secret-", "y" * 24))


@pytest.fixture
def store(tmp_path):
    # Darwin's pytest tmp root may start with the system /var symlink.
    return Store(tmp_path.resolve() / "store")


@pytest.fixture
def principal(store):
    return store.create_user("alice", TOKEN, "operator", ["plant-a", "plant-b"])


def create(store, principal, object_id="task-1", plant="plant-a", *, conn=None):
    kwargs = dict(
        kind="task",
        object_id=object_id,
        principal=principal,
        plant_id=plant,
        status="ready",
        payload={"number": 1},
    )
    if conn is not None:
        return store.create_object(conn, **kwargs)
    with store.transaction() as transaction:
        return store.create_object(transaction, **kwargs)


def update(store, principal, conn, *, expected_version=1, number=2):
    return store.update_object(
        conn,
        kind="task",
        object_id="task-1",
        principal=principal,
        expected_version=expected_version,
        status="complete",
        payload={"number": number},
    )


def assert_error(code, function, *args, status=None, **kwargs):
    with pytest.raises(DomainError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    if status is not None:
        assert caught.value.status == status
    assert TOKEN not in str(caught.value)
    assert OTHER_TOKEN not in str(caught.value)
    return caught.value


def write_raw(store, content):
    content_hash = hashlib.sha256(content).hexdigest()
    (store.artifacts / f"{content_hash}.json").write_bytes(content)
    return content_hash


def test_schema_and_pragmas_and_initialization(store):
    assert store.database.name == "data.sqlite3"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.artifacts.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database.stat().st_mode) == 0o600
    with store.connect() as conn:
        assert conn.row_factory is sqlite3.Row
        for pragma, expected in (
            ("journal_mode", "wal"),
            ("foreign_keys", 1),
            ("trusted_schema", 0),
            ("query_only", 1),
            ("synchronous", 2),
            ("application_id", 0x52365341),
            ("user_version", 1),
        ):
            assert conn.execute(f"PRAGMA {pragma}").fetchone()[0] == expected
        key = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(objects)")
            if row["pk"]
        }
        assert key == {"kind": 1, "object_id": 2}
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(calls)")]
        assert columns == [
            "call_id",
            "task_id",
            "owner",
            "lease_id",
            "status",
            "tokens",
            "elapsed_s",
            "response_hash",
            "error_code",
            "created_at_ms",
        ]
        for table in ("users", "objects", "receipts", "audit", "calls"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM users")
    reopened = Store(store.root)
    with reopened.transaction() as conn:
        assert conn.in_transaction
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
        conn.execute(
            "INSERT INTO calls(call_id,task_id,owner,lease_id,status,created_at_ms) "
            "VALUES('c','t','u','l','started',1)"
        )
        row = conn.execute("SELECT * FROM calls").fetchone()
        assert row["tokens"] is None
        assert row["elapsed_s"] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE calls SET status='invalid'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE calls SET tokens=-1")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE calls SET task_id=NULL")


@pytest.mark.parametrize("application,version", [(0x52355041, 1), (0x52365341, 2)])
def test_foreign_database_identity_rejected_without_migration(store, principal, application, version):
    create(store, principal)
    with store.transaction() as conn:
        conn.execute(f"PRAGMA application_id={application}")
        conn.execute(f"PRAGMA user_version={version}")
    assert_error("integrity", Store, store.root)
    conn = sqlite3.connect(store.database)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == application
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version
        assert conn.execute("SELECT object_id,owner,plant_id,version FROM objects").fetchall() == [
            ("task-1", "alice", "plant-a", 1)
        ]
    finally:
        conn.close()


def test_object_identity_is_global_across_owners_and_plants(store, principal):
    original = create(store, principal)
    other = store.create_user("bob", OTHER_TOKEN, "admin", ["plant-a", "plant-b"])
    for who, plant in ((principal, "plant-b"), (other, "plant-a"), (other, "plant-b")):
        assert_error("conflict", create, store, who, "task-1", plant)
    assert store.get_object("task", "task-1", principal) == original
    assert_error("not_found", store.get_object, "task", "task-1", other, status=404)
    assert store.list_objects("task", other) == []
    with store.transaction() as conn:
        distinct_kind = store.create_object(
            conn, kind="schedule", object_id="task-1", principal=other,
            plant_id="plant-b", status="ready", payload={"synthetic": True},
        )
    assert distinct_kind["owner"] == other.user_id
    assert distinct_kind["plant_id"] == "plant-b"


def test_create_missing_ancestors_private(tmp_path):
    base = tmp_path.resolve() / "new" / "child"
    Store(base / "store")
    assert stat.S_IMODE(base.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


@pytest.mark.parametrize("root", ["relative", "".join(("~", "/data")), Path("."), None, 4])
def test_reject_nonabsolute_roots(root):
    assert_error("integrity", Store, root)


def test_parent_traversal_rejected(tmp_path):
    assert_error("integrity", Store, tmp_path.resolve() / "unused" / ".." / "store")
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("position", ["ancestor", "root", "artifacts", "database"])
def test_symlinks_rejected(tmp_path, position):
    base = tmp_path.resolve()
    target = base / "target"
    target.mkdir()
    root = base / "store"
    if position == "ancestor":
        (base / "alias").symlink_to(target, target_is_directory=True)
        root = base / "alias" / "new"
    elif position == "root":
        root.symlink_to(target, target_is_directory=True)
    elif position == "artifacts":
        root.mkdir()
        (root / "artifacts").symlink_to(target, target_is_directory=True)
    else:
        root.mkdir()
        (target / "db").write_bytes(b"untouched")
        (root / "data.sqlite3").symlink_to(target / "db")
    assert_error("integrity", Store, root)
    if position == "database":
        assert (target / "db").read_bytes() == b"untouched"
    else:
        assert list(target.iterdir()) == []


def test_nondirectory_root_rejected(tmp_path):
    root = tmp_path.resolve() / "file"
    root.write_bytes(b"untouched")
    assert_error("integrity", Store, root)
    assert root.read_bytes() == b"untouched"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "directory"])
def test_database_file_protection(store, tmp_path, suffix, attack):
    path = Path(str(store.database) + suffix)
    if path.exists():
        path.unlink()
    target = tmp_path.resolve() / "outside"
    target.write_bytes(b"untouched")
    if attack == "symlink":
        path.symlink_to(target)
    elif attack == "hardlink":
        os.link(target, path)
    elif attack == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    assert_error("integrity", store.authenticate, TOKEN)
    assert_error("integrity", Store, store.root)
    assert target.read_bytes() == b"untouched"


def test_replaced_or_missing_database_rejected(store):
    original = store.database.with_suffix(".original")
    store.database.rename(original)
    assert_error("integrity", store.authenticate, TOKEN)
    store.database.write_bytes(original.read_bytes())
    assert_error("integrity", store.authenticate, TOKEN)


@pytest.mark.parametrize("directory", ["root", "artifacts"])
def test_directory_replacement_rejected(store, directory):
    path = getattr(store, directory)
    moved = path.with_name(path.name + "-old")
    path.rename(moved)
    path.mkdir()
    assert_error("integrity", store.put_artifact, {"a": 1})
    if directory == "root":
        assert_error("integrity", store.authenticate, TOKEN)


def test_unrelated_database_not_initialized(tmp_path):
    root = tmp_path.resolve() / "root"
    root.mkdir()
    database = root / "data.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE unrelated(secret TEXT)")
    conn.commit()
    conn.close()
    assert_error("integrity", Store, root)
    conn = sqlite3.connect(database)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        assert tables == ["unrelated"]
    finally:
        conn.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("eventual", ["missing", "regular", "hardlink", "symlink", "zero"])
def test_darwin_transient_sidecars_bounded_retry(store, monkeypatch, suffix, eventual):
    original_stat = os.stat
    calls = []
    monkeypatch.setattr(storage.sys, "platform", "darwin")
    regular = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1)
    zero = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0)

    def fake_stat(path, *args, **kwargs):
        if path == "data.sqlite3" + suffix:
            calls.append(path)
            if len(calls) == 1 or eventual == "zero":
                return zero
            if eventual == "missing":
                raise FileNotFoundError
            if eventual == "regular":
                return regular
            if eventual == "hardlink":
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=2)
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_nlink=1)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", fake_stat)
    if eventual in ("missing", "regular"):
        store._check_database_files()
        assert len(calls) == 2
    else:
        assert_error("integrity", store._check_database_files)
        assert len(calls) == (3 if eventual == "zero" else 2)


@pytest.mark.parametrize(
    "platform,suffix", [("darwin", ""), ("darwin", "-journal"), ("linux", "-wal")]
)
def test_zero_link_exceptions_never_widened(store, monkeypatch, platform, suffix):
    original_stat = os.stat
    calls = []
    monkeypatch.setattr(storage.sys, "platform", platform)

    def fake_stat(path, *args, **kwargs):
        if path == "data.sqlite3" + suffix:
            calls.append(path)
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=0)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", fake_stat)
    assert_error("integrity", store._check_database_files)
    assert len(calls) == 1


def test_principal_frozen_and_no_token_persisted(store, principal):
    assert principal == Principal("alice", "operator", ("plant-a", "plant-b"), 1)
    with pytest.raises(FrozenInstanceError):
        principal.role = "admin"
    assert store.authenticate(TOKEN) == principal
    store.require(principal, "plant-a", {"operator"})
    with store.connect() as conn:
        row = dict(conn.execute("SELECT * FROM users").fetchone())
        assert row["token_hash"] == hashlib.sha256(TOKEN.encode()).hexdigest()
        assert TOKEN not in str(row)
    for path in store.root.iterdir():
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "token",
    [
        None,
        1,
        b"x" * 32,
        "short",
        "x" * 201,
        "x y" * 20,
        "!" * 40,
        "中" * 32,
        "x" * 31 + "\n",
        "\ud800" * 32,
    ],
)
def test_invalid_tokens_uniform_unauthorized(store, token):
    error = assert_error("unauthorized", store.authenticate, token, status=401)
    assert str(error) == "Authentication required"


def test_unknown_disabled_token_and_no_reenable_old_principal(store, principal):
    assert_error("unauthorized", store.authenticate, "unknown-" + "z" * 32, status=401)
    store.set_user("alice", active=False)
    assert_error("unauthorized", store.authenticate, TOKEN, status=401)
    assert_error("forbidden", store.require, principal, status=403)
    store.set_user("alice", active=True)
    current = store.authenticate(TOKEN)
    assert current.acl_version == 3
    assert_error("forbidden", store.require, principal)
    store.require(current)


@pytest.mark.parametrize(
    "field,value",
    [
        ("user_id", "../alice"),
        ("user_id", "x" * 81),
        ("user_id", "user\n"),
        ("user_id", 1),
        ("role", "root"),
        ("role", ["admin"]),
        ("token", "short"),
        ("token", "A" * 201),
        ("plants", ("a",)),
        ("plants", ["a", "a"]),
        ("plants", [None]),
        ("plants", ["../plant"]),
        ("plants", [str(i) for i in range(101)]),
    ],
)
def test_create_user_validation(store, field, value):
    args = dict(user_id="alice", token=TOKEN, role="operator", plants=["plant-a"])
    args[field] = value
    assert_error("validation", store.create_user, **args)
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_duplicate_user_or_token_no_overwrite(store, principal):
    assert_error("conflict", store.create_user, "alice", OTHER_TOKEN, "admin", ["plant-a"])
    assert_error("conflict", store.create_user, "bob", TOKEN, "admin", ["plant-a"])
    assert store.authenticate(TOKEN) == principal
    assert_error("unauthorized", store.authenticate, OTHER_TOKEN)


@pytest.mark.parametrize(
    "change", [dict(role="reviewer"), dict(plants=["plant-a"]), dict(active=False)]
)
def test_all_acl_changes_invalidate_old_principal(store, principal, change):
    store.set_user("alice", **change)
    assert_error("forbidden", store.require, principal)
    if change != dict(active=False):
        current = store.authenticate(TOKEN)
        assert current.acl_version == 2
        store.require(current)


@pytest.mark.parametrize("kwargs", [dict(active=1), dict(role="root"), dict(plants=["p", "p"])])
def test_set_user_invalid_does_not_revoke(store, principal, kwargs):
    assert_error("validation", store.set_user, "alice", **kwargs)
    store.require(principal)


def test_require_validation_and_noop_set_user(store, principal):
    store.set_user("alice")
    store.require(principal)
    assert_error("not_found", store.set_user, "nobody", active=False)
    assert_error("forbidden", store.require, principal, "plant-c")
    assert_error("forbidden", store.require, principal, roles=["admin"])
    assert_error("validation", store.require, principal, roles="operator")
    assert_error("validation", store.require, principal, roles=["root"])
    assert_error("forbidden", store.require, object())
    assert_error("forbidden", store.require, replace(principal, role="admin"))
    assert_error("forbidden", store.require, replace(principal, acl_version=True))
    assert_error("forbidden", store.require, replace(principal, plants=["plant-a"]))
    assert_error("forbidden", store.require, replace(principal, plants=(1,)))
    assert_error("forbidden", store.require, replace(principal, user_id="nobody"))


@pytest.mark.parametrize(
    "sql",
    [
        'UPDATE users SET plants=\'["plant-a","plant-a"]\'',
        "UPDATE users SET plants='[\"plant-a\", NaN]'",
        "UPDATE users SET plants='{}'",
    ],
)
def test_corrupt_current_identity_is_denied(store, principal, sql):
    with store.transaction() as conn:
        conn.execute(sql)
    assert_error("forbidden", store.require, principal)
    assert_error("unauthorized", store.authenticate, TOKEN)


def test_canonical_artifact_roundtrip_and_reopen(store):
    payload = {"z": [None, True, False, 12, 1.5], "a": '汉字\\"\n'}
    expected = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    content_hash = store.put_artifact(payload)
    assert content_hash == hashlib.sha256(expected).hexdigest()
    path = store.artifacts / (content_hash + ".json")
    before = path.stat()
    assert stat.S_IMODE(before.st_mode) == 0o600
    assert before.st_nlink == 1
    assert path.read_bytes() == expected
    assert store.put_artifact(dict(reversed(list(payload.items())))) == content_hash
    assert path.stat().st_ino == before.st_ino
    assert store.get_artifact(content_hash) == payload
    assert Store(store.root).get_artifact(content_hash) == payload
    assert not list(store.artifacts.glob(".pending-*"))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        "text",
        {1: "value"},
        {"x": (1, 2)},
        {"x": {1, 2}},
        {"x": b"bytes"},
        {"x": float("nan")},
        {"x": float("inf")},
        {"x": -float("inf")},
        {"x": "\ud800"},
        {"\ud800": 1},
        {"x": object()},
    ],
)
def test_non_json_artifacts_rejected(store, payload):
    assert_error("invalid_json", store.put_artifact, payload)
    assert list(store.artifacts.iterdir()) == []


def test_cycles_and_shared_expansion_bounded(store):
    value = {}
    value["cycle"] = value
    assert_error("invalid_json", store.put_artifact, value)
    shared = ["x" * 10000] * 300
    assert_error("artifact_too_large", store.put_artifact, {"shared": shared}, status=413)
    assert list(store.artifacts.iterdir()) == []


def test_json_exact_size_and_depth_boundaries(store):
    exact = {"x": "a" * (MAX_JSON_BYTES - len(b'{"x":""}'))}
    content_hash = store.put_artifact(exact)
    assert (store.artifacts / (content_hash + ".json")).stat().st_size == MAX_JSON_BYTES
    assert store.get_artifact(content_hash) == exact
    assert_error("artifact_too_large", store.put_artifact, {"x": exact["x"] + "a"})
    assert_error("artifact_too_large", store.put_artifact, {"x": "中" * MAX_JSON_BYTES})
    nested = 1
    for _ in range(40):
        nested = {"x": nested}
    assert store.get_artifact(store.put_artifact(nested)) == nested
    assert_error("invalid_json", store.put_artifact, {"x": nested})


@pytest.mark.parametrize(
    "content",
    [
        b'{"x":1,"x":2}',
        b'{"nested":{"x":1,"x":2}}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1e999}',
        b"[]",
        b"null",
        b'{"x": 1}',
        b'{"z":1,"a":2}',
        b'{"x":"\\ud800"}',
        b"\xff",
        b'{"x":',
        b'{"x":1}\n',
        b'"string"',
        b'{"x":1e0}',
        b'{"x":' + b"[" * 5000 + b"0" + b"]" * 5000 + b"}",
        b'{"x":"' + b"a" * MAX_JSON_BYTES + b'"}',
    ],
)
def test_invalid_even_correctly_hashed_artifacts_rejected(store, content):
    assert_error("integrity", store.get_artifact, write_raw(store, content), status=409)


@pytest.mark.parametrize(
    "content_hash", [None, 12, "../escape", "a" * 63, "A" * 64, "g" * 64, "a" * 64 + "\n"]
)
def test_invalid_artifact_references(store, content_hash):
    assert_error("integrity", store.get_artifact, content_hash)


def test_artifact_missing_hash_mismatch_and_existing_corruption(store):
    assert_error("integrity", store.get_artifact, "a" * 64)
    content_hash = store.put_artifact({"x": 1})
    path = store.artifacts / (content_hash + ".json")
    path.write_bytes(b'{"x":2}')
    assert_error("integrity", store.get_artifact, content_hash)
    assert_error("integrity", store.put_artifact, {"x": 1})
    assert path.read_bytes() == b'{"x":2}'
    assert not list(store.artifacts.glob(".pending-*"))
    path.write_bytes(b"")
    assert_error("integrity", store.get_artifact, content_hash)


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "directory"])
def test_artifact_unsafe_existing_not_followed_or_overwritten(store, tmp_path, attack):
    payload = {"x": 1}
    content_hash = store.put_artifact(payload)
    path = store.artifacts / (content_hash + ".json")
    target = tmp_path.resolve() / "target"
    target.write_bytes(path.read_bytes())
    path.unlink()
    if attack == "symlink":
        path.symlink_to(target)
    elif attack == "hardlink":
        os.link(target, path)
    elif attack == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    assert_error("integrity", store.get_artifact, content_hash)
    assert_error("integrity", store.put_artifact, payload)
    assert target.read_bytes() == b'{"x":1}'
    assert not list(store.artifacts.glob(".pending-*"))


def test_failed_artifact_publication_cleans_temporary(store, monkeypatch):
    def fail(*_args):
        raise OSError("simulated failure")

    monkeypatch.setattr(storage, "_publish", fail)
    assert_error("integrity", store.put_artifact, {"x": 1})
    assert list(store.artifacts.iterdir()) == []


def test_artifact_replacement_during_read_fails(store, monkeypatch):
    content_hash = store.put_artifact({"x": 1})
    path = store.artifacts / (content_hash + ".json")
    original_read = os.read
    replaced = False

    def replace_during_read(fd, size):
        nonlocal replaced
        data = original_read(fd, size)
        if not replaced:
            replaced = True
            path.unlink()
            path.write_bytes(b'{"x":1}')
        return data

    monkeypatch.setattr(storage.os, "read", replace_during_read)
    assert_error("integrity", store.get_artifact, content_hash)


def test_parallel_artifact_publish_and_read_are_single_link(store):
    barrier = threading.Barrier(8)
    payload = {"value": "concurrent" * 1000}

    def worker(_index):
        barrier.wait(timeout=5)
        for _ in range(12):
            content_hash = store.put_artifact(payload)
            assert store.get_artifact(content_hash) == payload
            assert (store.artifacts / (content_hash + ".json")).stat().st_nlink == 1
        return content_hash

    with ThreadPoolExecutor(max_workers=8) as executor:
        hashes = list(executor.map(worker, range(8)))
    assert len(set(hashes)) == 1
    assert len(list(store.artifacts.iterdir())) == 1


def test_object_shape_versions_and_optional_conn(store, principal):
    result = create(store, principal)
    assert set(result) == {
        "kind",
        "object_id",
        "owner",
        "plant_id",
        "status",
        "version",
        "content_hash",
        "payload",
    }
    assert result["owner"] == "alice"
    assert result["version"] == 1
    assert result["payload"] == {"number": 1}
    assert store.get_object("task", "task-1", principal) == result
    with store.transaction() as conn:
        store.require(principal, "plant-a", ["operator"], conn=conn)
        assert store.get_object("task", "task-1", principal, conn=conn) == result
        changed = update(store, principal, conn)
        assert changed["version"] == 2
        assert changed["status"] == "complete"
        assert changed["payload"] == {"number": 2}
    assert store.list_objects("task", principal) == [changed]
    with store.connect() as conn:
        assert store.get_object("task", "task-1", principal, conn=conn) == changed


def test_duplicate_and_stale_object_updates_do_not_change_state(store, principal):
    original = create(store, principal)
    assert_error("conflict", create, store, principal)
    with store.transaction() as conn:
        assert_error("conflict", update, store, principal, conn, expected_version=2)
        assert_error("validation", update, store, principal, conn, expected_version=True)
        assert_error("validation", update, store, principal, conn, expected_version=0)
    assert store.get_object("task", "task-1", principal) == original


def test_owner_and_plant_access_are_hidden_even_from_admin(store, principal):
    create(store, principal)
    bob = store.create_user("bob", OTHER_TOKEN, "admin", ["plant-a", "plant-b"])
    for who, object_id in ((bob, "task-1"), (bob, "missing"), (principal, "missing")):
        error = assert_error("not_found", store.get_object, "task", object_id, who, status=404)
        assert str(error) == "Object is unavailable"
    assert store.list_objects("task", bob) == []
    with store.transaction() as conn:
        assert_error("not_found", update, store, bob, conn)
        assert_error(
            "not_found",
            store.audit,
            conn,
            bob,
            kind="task",
            object_id="task-1",
            action="read",
            detail={},
        )
    store.set_user("alice", plants=["plant-b"])
    current = store.authenticate(TOKEN)
    assert_error("not_found", store.get_object, "task", "task-1", current, status=404)
    assert store.list_objects("task", current) == []
    assert_error("forbidden", create, store, current, "task-2")


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.0, "1", None])
def test_listing_and_audit_limits_are_strict(store, principal, limit):
    assert_error("validation", store.list_objects, "task", principal, limit=limit)
    assert_error("validation", store.audit_events, principal, limit=limit)


def test_list_filters_before_limit_and_bounds_results(store, principal):
    with store.transaction() as conn:
        for index in range(105):
            create(store, principal, f"allowed-{index}", conn=conn)
        create(store, principal, "newest-hidden", "plant-b", conn=conn)
    store.set_user("alice", plants=["plant-a"])
    current = store.authenticate(TOKEN)
    assert len(store.list_objects("task", current)) == 100
    assert store.list_objects("task", current, limit=1)[0]["object_id"] == "allowed-104"
    store.set_user("alice", plants=[])
    current = store.authenticate(TOKEN)
    assert store.list_objects("task", current) == []
    assert store.audit_events(current) == []


def test_object_artifact_integrity_is_rechecked_on_read_and_update(store, principal):
    result = create(store, principal)
    (store.artifacts / (result["content_hash"] + ".json")).write_bytes(b'{"number":9}')
    assert_error("integrity", store.get_object, "task", "task-1", principal)
    assert_error("integrity", store.list_objects, "task", principal)
    with store.transaction() as conn:
        assert_error("integrity", update, store, principal, conn)


def test_external_closed_and_read_connections_cannot_write(store, principal):
    create(store, principal)
    foreign = sqlite3.connect(":memory:")
    try:
        foreign.execute("BEGIN")
        assert_error("transaction_required", store.require, principal, conn=foreign)
        assert_error("transaction_required", create, store, principal, conn=foreign)
    finally:
        foreign.close()
    with store.connect() as conn:
        assert_error("transaction_required", create, store, principal, "task-2", conn=conn)
        assert_error("transaction_required", update, store, principal, conn)
        assert_error("transaction_required", store.record_receipt, conn, principal, "a", {}, {})
    assert_error("transaction_required", store.require, principal, conn=conn)
    other = Store(store.root)
    with other.transaction() as conn:
        assert_error("transaction_required", store.require, principal, conn=conn)


def test_transaction_rollback_covers_objects_calls_audit_and_receipts(store, principal):
    request = {"operation": "execute", "target": "task-1", "payload": {"version": 1}}
    with pytest.raises(RuntimeError, match="abort"):
        with store.transaction() as conn:
            create(store, principal, conn=conn)
            update(store, principal, conn)
            store.audit(
                conn, principal, kind="task", object_id="task-1", action="execute", detail={}
            )
            store.record_receipt(conn, principal, "action-1", request, {"ok": True})
            conn.execute(
                "INSERT INTO calls(call_id,task_id,owner,lease_id,status,created_at_ms) "
                "VALUES('c','task-1','alice','l','started',1)"
            )
            raise RuntimeError("abort")
    with store.connect() as conn:
        for table in ("objects", "calls", "audit", "receipts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    # Immutable, unreferenced JSON is safe after rollback; no partial SQL is visible.
    assert store.list_objects("task", principal) == []
    assert store.audit_events(principal) == []
    with store.transaction() as conn:
        assert store.receipt(conn, principal, "action-1", request) is None
    create(store, principal)


def test_baseexception_rolls_back_and_releases_lock(store, principal):
    with pytest.raises(KeyboardInterrupt):
        with store.transaction() as conn:
            create(store, principal, conn=conn)
            raise KeyboardInterrupt
    assert store.list_objects("task", principal) == []
    create(store, principal)


def test_same_transaction_revocation_rejects_all_followup_work(store, principal):
    create(store, principal)
    with pytest.raises(DomainError) as caught:
        with store.transaction() as conn:
            update(store, principal, conn)
            conn.execute(
                "UPDATE users SET active=0,acl_version=acl_version+1 WHERE user_id='alice'"
            )
            store.record_receipt(conn, principal, "action-1", {}, {})
    assert caught.value.code == "forbidden"
    assert store.authenticate(TOKEN) == principal
    assert store.get_object("task", "task-1", principal)["version"] == 1
    with store.transaction() as conn:
        conn.execute("UPDATE users SET active=0,acl_version=acl_version+1 WHERE user_id='alice'")
        assert_error("forbidden", store.require, principal, conn=conn)
        assert_error("forbidden", store.get_object, "task", "task-1", principal, conn=conn)
        assert_error("forbidden", create, store, principal, "task-2", conn=conn)
        assert_error("forbidden", update, store, principal, conn)
        assert_error("forbidden", store.receipt, conn, principal, "a", {})
        assert_error(
            "forbidden",
            store.audit,
            conn,
            principal,
            kind="task",
            object_id="task-1",
            action="execute",
            detail={},
        )


def test_concurrent_cas_has_exactly_one_winner(store, principal):
    create(store, principal)
    barrier = threading.Barrier(2)

    def worker(index):
        local = Store(store.root)
        barrier.wait(timeout=5)
        try:
            with local.transaction() as conn:
                return update(local, principal, conn, number=index)["version"]
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, [2, 3]))
    assert sorted(map(str, results)) == ["2", "conflict"]
    assert store.get_object("task", "task-1", principal)["version"] == 2


def test_revocation_committing_before_waiting_writer_is_effective(store, principal):
    create(store, principal)
    local = Store(store.root)
    entered = threading.Event()

    def writer():
        entered.set()
        try:
            with local.transaction() as conn:
                update(local, principal, conn)
        except DomainError as error:
            return error.code
        return "unexpected success"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE users SET plants='[]',acl_version=acl_version+1 WHERE user_id='alice'"
            )
            future = executor.submit(writer)
            assert entered.wait(timeout=5)
        assert future.result(timeout=10) == "forbidden"
    with store.connect() as conn:
        assert conn.execute("SELECT version FROM objects").fetchone()[0] == 1


def test_audit_filtering_revocation_and_artifact_details(store, principal):
    bob = store.create_user("bob", OTHER_TOKEN, "reviewer", ["plant-a"])
    with store.transaction() as conn:
        for who, object_id, plant in (
            (principal, "a", "plant-a"),
            (principal, "b", "plant-b"),
            (bob, "other", "plant-a"),
        ):
            create(store, who, object_id, plant, conn=conn)
            store.audit(
                conn,
                who,
                kind="task",
                object_id=object_id,
                action="review",
                detail={"object": object_id},
            )
    events = store.audit_events(principal)
    assert [event["object_id"] for event in events] == ["b", "a"]
    assert all(
        set(event) == {"kind", "object_id", "action", "detail", "created_at_ms"} for event in events
    )
    assert all(event["created_at_ms"] > 0 for event in events)
    assert events[0]["detail"] == {"object": "b"}
    assert [event["object_id"] for event in store.audit_events(bob)] == ["other"]
    store.set_user("alice", plants=["plant-a"])
    assert_error("forbidden", store.audit_events, principal)
    current = store.authenticate(TOKEN)
    assert [event["object_id"] for event in store.audit_events(current, limit=1)] == ["a"]
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM audit WHERE object_id='a'").fetchone()
        assert "detail" not in row.keys()
        assert TOKEN not in str(dict(row))
        detail_hash = row["detail_hash"]
    (store.artifacts / (detail_hash + ".json")).unlink()
    assert_error("integrity", store.audit_events, current)


def test_receipts_bind_every_request_field_and_response(store, principal):
    request = {"operation": "approve", "target": "task-1", "payload": {"version": 1}}
    response = {"ok": True, "object": "task-1"}
    with store.transaction() as conn:
        assert store.receipt(conn, principal, "act-1", request) is None
        store.record_receipt(conn, principal, "act-1", request, response)
        store.record_receipt(
            conn, principal, "act-1", dict(reversed(list(request.items()))), response
        )
        assert store.receipt(conn, principal, "act-1", request) == response
        for key, value in (
            ("operation", "execute"),
            ("target", "task-2"),
            ("payload", {"version": 2}),
        ):
            changed = dict(request, **{key: value})
            assert_error("idempotency_conflict", store.receipt, conn, principal, "act-1", changed)
            assert_error(
                "idempotency_conflict",
                store.record_receipt,
                conn,
                principal,
                "act-1",
                changed,
                response,
            )
        assert_error(
            "idempotency_conflict",
            store.record_receipt,
            conn,
            principal,
            "act-1",
            request,
            {"ok": False},
        )
    with store.connect() as conn:
        assert store.receipt(conn, principal, "act-1", request) == response
        row = conn.execute("SELECT * FROM receipts").fetchone()
        assert row["request_hash"] == hashlib.sha256(storage._canonical(request)).hexdigest()
        assert store.get_artifact(row["response_hash"]) == response


def test_empty_receipts_and_owner_scoping(store, principal):
    bob = store.create_user("bob", OTHER_TOKEN, "operator", [])
    with store.transaction() as conn:
        store.record_receipt(conn, principal, "act", {}, {})
        store.record_receipt(conn, principal, "act", {}, {})
        assert store.receipt(conn, principal, "act", {}) == {}
        assert store.receipt(conn, bob, "act", {}) is None
        store.record_receipt(conn, bob, "act", {}, {"owner": "bob"})
        assert store.receipt(conn, principal, "act", {}) == {}
        assert store.receipt(conn, bob, "act", {}) == {"owner": "bob"}


@pytest.mark.parametrize(
    "request_data,response", [([], {}), ({}, []), ({"x": math.nan}, {}), ({}, None)]
)
def test_receipt_json_validation(store, principal, request_data, response):
    with store.transaction() as conn:
        assert_error(
            "invalid_json", store.record_receipt, conn, principal, "act", request_data, response
        )
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_receipt_integrity_and_revocation_rechecked(store, principal):
    with store.transaction() as conn:
        store.record_receipt(conn, principal, "act", {}, {"ok": True})
        response_hash = conn.execute("SELECT response_hash FROM receipts").fetchone()[0]
    store.set_user("alice", role="reviewer")
    with store.transaction() as conn:
        assert_error("forbidden", store.receipt, conn, principal, "act", {})
        assert_error("forbidden", store.record_receipt, conn, principal, "act", {}, {"ok": True})
    current = store.authenticate(TOKEN)
    (store.artifacts / (response_hash + ".json")).write_bytes(b'{"ok":false}')
    with store.transaction() as conn:
        assert_error("integrity", store.receipt, conn, current, "act", {})
        conn.execute("UPDATE receipts SET request_hash='invalid'")
        assert_error("integrity", store.receipt, conn, current, "act", {})


@pytest.mark.parametrize("same_request", [True, False])
def test_parallel_receipt_writers_are_atomic(store, principal, same_request):
    barrier = threading.Barrier(2)

    def worker(index):
        local = Store(store.root)
        barrier.wait(timeout=5)
        request = {
            "operation": "execute",
            "target": "task",
            "payload": 1 if same_request else index,
        }
        try:
            with local.transaction() as conn:
                local.record_receipt(conn, principal, "act", request, {"ok": True})
            return "ok"
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, [1, 2]))
    assert sorted(results) == (["ok", "ok"] if same_request else ["idempotency_conflict", "ok"])
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_sqlite_errors_are_generic_and_rollback(store, principal):
    with pytest.raises(DomainError) as caught:
        with store.transaction() as conn:
            create(store, principal, conn=conn)
            conn.execute("not valid SQL " + TOKEN)
    assert caught.value.code == "storage"
    assert TOKEN not in str(caught.value)
    assert caught.value.__suppress_context__
    assert store.list_objects("task", principal) == []
