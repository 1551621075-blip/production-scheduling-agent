"""Same-database synthetic receiver; private HMAC state is not publisher evidence.

This is a local trusted-process boundary, not distributed exactly-once or a MES.
The complete plant lease deliberately excludes parallel plans, not just overlaps.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat

from .domain import MAX_REMAINING_OPERATIONS, canonical_digest
from .errors import DomainError
from .storage import _canonical, _decode, _identifier

_PROTOCOL = 1
_KEY_FILE = "receiver.key"
_REQUEST_FIELDS = frozenset({
    "protocol_version", "owner", "plant_id", "publication_id", "recommendation_id",
    "recommendation_hash", "plan_hash", "source_binding", "operation_ids",
    "created_at_ms", "expires_at_ms", "synthetic",
})
_RECEIPT_FIELDS = frozenset({
    "protocol_version", "owner", "plant_id", "publication_id", "request_hash",
    "plan_hash", "source_binding", "operation_ids", "accepted_operation_ids",
    "sequence", "receiver_status", "execution_status", "execution_id", "result",
    "synthetic",
})
_CANCEL_FIELDS = frozenset({
    "protocol_version", "owner", "plant_id", "publication_id", "request_hash",
    "created_at_ms", "note", "synthetic",
})
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS receiver_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), key_hash TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS receiver_records (publication_id TEXT PRIMARY KEY, owner TEXT NOT NULL, plant_id TEXT NOT NULL, request TEXT NOT NULL, receipt TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS receiver_delivery_intents (publication_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS receiver_intents_plant ON receiver_delivery_intents(plant_id)",
    "CREATE TABLE IF NOT EXISTS receiver_receipt_history (publication_id TEXT NOT NULL, sequence INTEGER NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(publication_id, sequence))",
    "CREATE TABLE IF NOT EXISTS receiver_plant_leases (plant_id TEXT PRIMARY KEY, publication_id TEXT UNIQUE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS receiver_executions (publication_id TEXT PRIMARY KEY, plant_id TEXT NOT NULL, revision INTEGER NOT NULL, observed_at_ms INTEGER NOT NULL, source_hash TEXT NOT NULL, execution_id TEXT UNIQUE NOT NULL)",
    "CREATE TABLE IF NOT EXISTS receiver_used_operations (plant_id TEXT NOT NULL, operation_id TEXT NOT NULL, publication_id TEXT NOT NULL, PRIMARY KEY(plant_id, operation_id))",
)


def _fail(code="receiver_integrity", message="Receiver state is unavailable or invalid"):
    return DomainError(code, message, 409)


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hash(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _integer(value):
    return type(value) is int and 0 <= value <= 253_402_300_799_999


def _ids(value):
    if type(value) is not list or not 1 <= len(value) <= MAX_REMAINING_OPERATIONS:
        raise _fail()
    for item in value:
        _identifier(item)
    if value != sorted(set(value)):
        raise _fail()


class LocalReceiver:
    """Backend-only entry points require this instance's unforgeable identity token."""

    def __init__(self, store):
        self.store = store
        self._capability = object()
        with store.transaction() as conn:
            store._check_conn(conn, write=True)
            initialized = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'receiver_%' LIMIT 1").fetchone() is not None
            for statement in _SCHEMA:
                conn.execute(statement)
            marker = conn.execute("SELECT key_hash FROM receiver_meta WHERE singleton=1").fetchone()
            existing = initialized or marker is not None or conn.execute(
                "SELECT 1 FROM receiver_records LIMIT 1"
            ).fetchone() is not None or conn.execute(
                "SELECT 1 FROM objects WHERE kind='publication' LIMIT 1"
            ).fetchone() is not None
            key = self._key(create=not existing)
            fingerprint = hashlib.sha256(key).hexdigest()
            if marker is not None:
                if not hmac.compare_digest(marker[0], fingerprint):
                    raise _fail("receiver_key", "Receiver key continuity is unavailable")
            elif existing:
                raise _fail("receiver_key", "Receiver key continuity is unavailable")
            else:
                conn.execute("INSERT INTO receiver_meta VALUES(1,?)", (fingerprint,))
            self._key_hash = fingerprint

    def _key(self, *, create=False):
        """Descriptor-relative no-follow I/O; loss, replacement and unsafe modes fail closed."""
        try:
            with self.store._root_directory() as directory:
                root = os.fstat(directory)
                if stat.S_IMODE(root.st_mode) != 0o700 or root.st_uid != os.getuid():
                    raise _fail("receiver_key", "Receiver private directory is unsafe")
                if create:
                    try:
                        fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    except FileExistsError:
                        pass
                    else:
                        try:
                            key = secrets.token_bytes(32)
                            if os.write(fd, key) != len(key):
                                raise _fail("receiver_key")
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                        os.fsync(directory)
                fd = os.open(_KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                try:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                            or stat.S_IMODE(before.st_mode) != 0o600
                            or before.st_uid != os.getuid() or before.st_size != 32):
                        raise _fail("receiver_key", "Receiver key file is unsafe")
                    key = os.read(fd, 33)
                    after = os.fstat(fd)
                    named = os.stat(_KEY_FILE, dir_fd=directory, follow_symlinks=False)
                    if (len(key) != 32 or not stat.S_ISREG(named.st_mode)
                            or named.st_nlink != 1 or stat.S_IMODE(named.st_mode) != 0o600
                            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
                            or (before.st_mtime_ns, before.st_ctime_ns, before.st_size)
                            != (after.st_mtime_ns, after.st_ctime_ns, after.st_size)):
                        raise _fail("receiver_key", "Receiver key changed during access")
                finally:
                    os.close(fd)
                if hasattr(self, "_key_hash") and not hmac.compare_digest(hashlib.sha256(key).hexdigest(), self._key_hash):
                    raise _fail("receiver_key", "Receiver key continuity is unavailable")
                if stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
                    raise _fail("receiver_key", "Receiver private directory changed during access")
                return key
        except OSError:
            raise _fail("receiver_key", "Receiver key is unavailable or unsafe") from None

    def _cap(self, capability):
        if capability is not self._capability:
            raise DomainError("receiver_capability", "Trusted receiver capability required", 403)

    @staticmethod
    def _shape(payload, domain):
        fields = {"request": _REQUEST_FIELDS, "receipt": _RECEIPT_FIELDS, "cancel": _CANCEL_FIELDS}[domain]
        if type(payload) is not dict or set(payload) != fields:
            raise _fail("receiver_schema")
        if type(payload["protocol_version"]) is not int or payload["protocol_version"] != _PROTOCOL or payload["synthetic"] is not True:
            raise _fail("receiver_schema")
        for name in ("owner", "plant_id", "publication_id"):
            _identifier(payload[name])
        if domain == "request":
            _identifier(payload["recommendation_id"])
            for name in ("recommendation_hash", "plan_hash"):
                if not _hash(payload[name]):
                    raise _fail("receiver_schema")
            if not _integer(payload["created_at_ms"]) or not _integer(payload["expires_at_ms"]) or payload["expires_at_ms"] <= payload["created_at_ms"]:
                raise _fail("receiver_schema")
        elif domain == "cancel":
            if not _integer(payload["created_at_ms"]) or not _hash(payload["request_hash"]) or type(payload["note"]) is not str or len(payload["note"]) > 2000:
                raise _fail("receiver_schema")
        else:
            if not _hash(payload["request_hash"]) or not _hash(payload["plan_hash"]) or not _integer(payload["sequence"]) or payload["sequence"] < 1:
                raise _fail("receiver_schema")
            if payload["receiver_status"] not in ("partial", "accepted", "cancelled") or payload["execution_status"] not in ("none", "running", "completed", "failed"):
                raise _fail("receiver_schema")
            accepted = payload["accepted_operation_ids"]
            if type(accepted) is not list or any(type(item) is not str for item in accepted) or accepted != sorted(set(accepted)):
                raise _fail("receiver_schema")
            _ids(payload["operation_ids"])
            if not set(accepted) <= set(payload["operation_ids"]):
                raise _fail("receiver_schema")
            if payload["receiver_status"] == "accepted" and accepted != payload["operation_ids"]:
                raise _fail("receiver_schema")
            if payload["execution_status"] != "none" and (payload["receiver_status"] != "accepted" or type(payload["execution_id"]) is not str):
                raise _fail("receiver_schema")
            if payload["execution_status"] == "none" and (payload["execution_id"] is not None or payload["result"] is not None):
                raise _fail("receiver_schema")
            if payload["execution_status"] == "running" and payload["result"] is not None:
                raise _fail("receiver_schema")
            if payload["execution_status"] in ("completed", "failed") and type(payload["result"]) is not dict:
                raise _fail("receiver_schema")
        if domain != "cancel":
            _ids(payload["operation_ids"])
            if type(payload["source_binding"]) is not dict or set(payload["source_binding"]) != {"object_id", "version", "content_hash", "revision", "plant_id", "owner", "observed_at_ms", "snapshot_hash"}:
                raise _fail("receiver_schema")
            binding = payload["source_binding"]
            _identifier(binding["object_id"])
            if (binding["owner"] != payload["owner"] or binding["plant_id"] != payload["plant_id"]
                    or not _integer(binding["revision"]) or not _integer(binding["observed_at_ms"])
                    or type(binding["version"]) is not int or binding["version"] < 1
                    or not _hash(binding["content_hash"]) or not _hash(binding["snapshot_hash"])):
                raise _fail("receiver_schema")
        _canonical(payload)

    def _sign(self, domain, payload):
        self._shape(payload, domain)
        body = _canonical(payload)
        signature = hmac.new(self._key(), b"r6-local-receiver/" + domain.encode("ascii") + b"\x00" + body, hashlib.sha256).hexdigest()
        return {"payload": _decode(body), "signature": signature}

    def _verify(self, domain, envelope):
        if type(envelope) is not dict or set(envelope) != {"payload", "signature"} or not _hash(envelope["signature"]):
            raise _fail("receiver_signature", "Invalid receiver signature")
        expected = self._sign(domain, envelope["payload"])
        if not hmac.compare_digest(expected["signature"], envelope["signature"]):
            raise _fail("receiver_signature", "Invalid receiver signature")
        return expected["payload"]

    def sign_request(self, capability, payload):
        self._cap(capability)
        return self._sign("request", payload)

    def sign_cancel(self, capability, payload):
        self._cap(capability)
        return self._sign("cancel", payload)

    def _authorized(self, conn, principal, request):
        self.store._check_conn(conn)
        self.store.require(principal, request["plant_id"], conn=conn)
        if principal.user_id != request["owner"]:
            raise DomainError("not_found", "Object is unavailable", 404)
        publication = self.store.get_object("publication", request["publication_id"], principal, conn=conn)
        if publication["plant_id"] != request["plant_id"] or publication["payload"].get("signed_request") != self._sign("request", request):
            raise _fail("receiver_binding", "Publication binding differs")

    def _record(self, conn, request):
        row = conn.execute("SELECT * FROM receiver_records WHERE publication_id=?", (request["publication_id"],)).fetchone()
        if row is None:
            return None
        stored_request = _decode(row["request"].encode())
        if self._verify("request", stored_request) != request or (row["owner"], row["plant_id"]) != (request["owner"], request["plant_id"]):
            raise _fail("receiver_binding")
        receipt = _decode(row["receipt"].encode())
        state = self._verify("receipt", receipt)
        self._bound_receipt(request, state)
        history = conn.execute("SELECT receipt FROM receiver_receipt_history WHERE publication_id=? AND sequence=?", (request["publication_id"], state["sequence"])).fetchone()
        if history is None or _decode(history[0].encode()) != receipt:
            raise _fail("receiver_sequence", "Latest receipt is not in the authoritative ledger")
        return receipt

    @staticmethod
    def _bound_receipt(request, receipt):
        for name in ("owner", "plant_id", "publication_id", "plan_hash", "source_binding", "operation_ids"):
            if receipt[name] != request[name]:
                raise _fail("receiver_binding")
        if receipt["request_hash"] != _digest(request):
            raise _fail("receiver_binding")

    def _save(self, conn, envelope, state):
        self.store._check_conn(conn, write=True)
        signed = self._sign("receipt", state)
        conn.execute(
            "INSERT INTO receiver_records VALUES(?,?,?,?,?) ON CONFLICT(publication_id) DO UPDATE SET receipt=excluded.receipt",
            (state["publication_id"], state["owner"], state["plant_id"], _canonical(envelope).decode(), _canonical(signed).decode()),
        )
        conn.execute("INSERT INTO receiver_receipt_history VALUES(?,?,?)", (state["publication_id"], state["sequence"], _canonical(signed).decode()))
        return signed

    @staticmethod
    def _initial(request):
        return {
            "protocol_version": _PROTOCOL, "synthetic": True,
            **{key: request[key] for key in ("owner", "plant_id", "publication_id", "plan_hash", "source_binding", "operation_ids")},
            "request_hash": _digest(request), "sequence": 1,
            "accepted_operation_ids": [], "receiver_status": "cancelled",
            "execution_status": "none", "execution_id": None, "result": None,
        }

    def _validated(self, conn, principal, request, validate, now_ms):
        if not _integer(now_ms) or not request["created_at_ms"] <= now_ms < request["expires_at_ms"]:
            raise _fail("publication_expired", "Publication is outside its validity window")
        problem, plan = validate(conn, principal, request)
        from .validator import validate_plan
        report = validate_plan(problem, plan)
        if not report.valid or canonical_digest(plan) != request["plan_hash"] or sorted(op.operation_id for op in problem.operations) != request["operation_ids"] or problem.snapshot.plant_id != request["plant_id"]:
            raise _fail("receiver_validation", "Frozen plan failed independent validation")
        return problem, plan

    def _unused(self, conn, request, snapshot):
        rows = conn.execute("SELECT revision,observed_at_ms FROM receiver_executions WHERE plant_id=?", (request["plant_id"],)).fetchall()
        if any(snapshot.revision <= row["revision"] or snapshot.observed_at_ms <= row["observed_at_ms"] for row in rows):
            raise _fail("source_used", "A newer source revision and observation are required")
        for operation_id in request["operation_ids"]:
            if conn.execute("SELECT 1 FROM receiver_used_operations WHERE plant_id=? AND operation_id=?", (request["plant_id"], operation_id)).fetchone():
                raise _fail("operation_used", "Plan contains previously admitted operations")

    def _lease(self, conn, request, snapshot):
        self._unused(conn, request, snapshot)
        lease = conn.execute("SELECT publication_id FROM receiver_plant_leases WHERE plant_id=?", (request["plant_id"],)).fetchone()
        if lease is not None and lease[0] != request["publication_id"]:
            raise _fail("plant_busy", "Plant has an active complete plan")
        if lease is None:
            conn.execute("INSERT INTO receiver_plant_leases VALUES(?,?)", (request["plant_id"], request["publication_id"]))

    def check_delivery_intent(self, capability, conn, plant_id, publication_id):
        self._cap(capability)
        self.store._check_conn(conn)
        if conn.execute("SELECT 1 FROM receiver_delivery_intents WHERE plant_id=? AND publication_id<>? LIMIT 1", (plant_id, publication_id)).fetchone():
            raise _fail("plant_busy", "Plant has an unresolved publication intent")

    def reserve_delivery_intent(self, capability, conn, request):
        self._cap(capability)
        self.store._check_conn(conn, write=True)
        self.check_delivery_intent(capability, conn, request["plant_id"], request["publication_id"])
        conn.execute("INSERT OR IGNORE INTO receiver_delivery_intents VALUES(?,?)", (request["publication_id"], request["plant_id"]))

    def acknowledge_intent(self, capability, conn, request):
        self._cap(capability)
        self.store._check_conn(conn, write=True)
        conn.execute("DELETE FROM receiver_delivery_intents WHERE publication_id=? AND plant_id=?", (request["publication_id"], request["plant_id"]))

    def receive(self, capability, principal, envelope, *, validate, now_ms):
        self._cap(capability)
        request = self._verify("request", envelope)
        with self.store.transaction() as conn:
            self._authorized(conn, principal, request)
            previous = self._record(conn, request)
            if previous is not None:
                return previous
            self.check_delivery_intent(capability, conn, request["plant_id"], request["publication_id"])
            problem, _ = self._validated(conn, principal, request, validate, now_ms)
            state = self._initial(request)
            state["accepted_operation_ids"] = request["operation_ids"][:1]
            state["receiver_status"] = "partial"
            if len(request["operation_ids"]) == 1:
                self._lease(conn, request, problem.snapshot)
                state["receiver_status"] = "accepted"
            return self._save(conn, envelope, state)

    def advance(self, capability, principal, envelope, *, validate, now_ms):
        self._cap(capability)
        request = self._verify("request", envelope)
        with self.store.transaction() as conn:
            self._authorized(conn, principal, request)
            receipt = self._record(conn, request)
            if receipt is None or receipt["payload"]["receiver_status"] != "partial":
                raise _fail("receiver_state", "Only partial reception can advance")
            self.check_delivery_intent(capability, conn, request["plant_id"], request["publication_id"])
            problem, _ = self._validated(conn, principal, request, validate, now_ms)
            self._lease(conn, request, problem.snapshot)
            state = receipt["payload"]
            state.update(sequence=state["sequence"] + 1, receiver_status="accepted", accepted_operation_ids=request["operation_ids"])
            return self._save(conn, envelope, state)

    def latest(self, capability, principal, envelope, *, conn=None):
        self._cap(capability)
        request = self._verify("request", envelope)
        if conn is None:
            with self.store.connect() as current:
                return self.latest(capability, principal, envelope, conn=current)
        self._authorized(conn, principal, request)
        return self._record(conn, request)

    def verify_receipt(self, capability, conn, principal, envelope, receipt):
        """Only database-authoritative sequences may drive the publisher reducer.

        A valid late ACK returns the *latest* receipt, never the stale state.
        """
        self._cap(capability)
        request = self._verify("request", envelope)
        self._authorized(conn, principal, request)
        incoming = self._verify("receipt", receipt)
        self._bound_receipt(request, incoming)
        latest = self._record(conn, request)
        if latest is None or incoming["sequence"] > latest["payload"]["sequence"]:
            raise _fail("receiver_sequence", "Receipt is not authoritative")
        historical = conn.execute("SELECT receipt FROM receiver_receipt_history WHERE publication_id=? AND sequence=?", (request["publication_id"], incoming["sequence"])).fetchone()
        if historical is None or _decode(historical[0].encode()) != receipt:
            raise _fail("receiver_sequence", "Receipt was not issued by the authoritative ledger")
        if incoming["sequence"] == latest["payload"]["sequence"] and receipt != latest:
            raise _fail("receiver_sequence", "Conflicting receipt sequence")
        return latest

    def cancel(self, capability, principal, envelope, cancellation, *, now_ms):
        self._cap(capability)
        request = self._verify("request", envelope)
        cancel = self._verify("cancel", cancellation)
        if any(cancel[key] != request[key] for key in ("owner", "plant_id", "publication_id")) or cancel["request_hash"] != _digest(request) or cancel["created_at_ms"] > now_ms:
            raise _fail("receiver_binding")
        with self.store.transaction() as conn:
            self._authorized(conn, principal, request)
            receipt = self._record(conn, request)
            if receipt is not None:
                state = receipt["payload"]
                if state["execution_status"] != "none":
                    raise _fail("too_late", "Synthetic execution has already started")
                if state["receiver_status"] == "cancelled":
                    return receipt
                state["sequence"] += 1
            else:
                state = self._initial(request)
            state["receiver_status"] = "cancelled"
            conn.execute("DELETE FROM receiver_plant_leases WHERE plant_id=? AND publication_id=?", (request["plant_id"], request["publication_id"]))
            return self._save(conn, envelope, state)

    def admit_execution(self, capability, conn, principal, envelope, *, validate, now_ms):
        self._cap(capability)
        self.store._check_conn(conn, write=True)
        request = self._verify("request", envelope)
        self._authorized(conn, principal, request)
        receipt = self._record(conn, request)
        if receipt is None or receipt["payload"]["receiver_status"] != "accepted" or receipt["payload"]["execution_status"] != "none":
            raise _fail("receiver_state", "Execution requires an unexecuted accepted plan")
        self.check_delivery_intent(capability, conn, request["plant_id"], request["publication_id"])
        problem, plan = self._validated(conn, principal, request, validate, now_ms)
        lease = conn.execute("SELECT publication_id FROM receiver_plant_leases WHERE plant_id=?", (request["plant_id"],)).fetchone()
        if lease is None or lease[0] != request["publication_id"]:
            raise _fail("receiver_lease", "Complete plant lease is required")
        self._unused(conn, request, problem.snapshot)
        execution_id = secrets.token_hex(24)
        conn.execute("INSERT INTO receiver_executions VALUES(?,?,?,?,?,?)", (request["publication_id"], request["plant_id"], problem.snapshot.revision, problem.snapshot.observed_at_ms, _digest(request["source_binding"]), execution_id))
        conn.executemany("INSERT INTO receiver_used_operations VALUES(?,?,?)", [(request["plant_id"], op, request["publication_id"]) for op in request["operation_ids"]])
        state = receipt["payload"]
        state.update(sequence=state["sequence"] + 1, execution_status="running", execution_id=execution_id)
        return self._save(conn, envelope, state), problem, plan

    def finish_execution(self, capability, conn, principal, envelope, *, execution_id, result, now_ms):
        self._cap(capability)
        self.store._check_conn(conn, write=True)
        request = self._verify("request", envelope)
        self._authorized(conn, principal, request)
        receipt = self._record(conn, request)
        if receipt is None:
            raise _fail("execution_fence")
        state = receipt["payload"]
        lease = conn.execute("SELECT publication_id FROM receiver_plant_leases WHERE plant_id=?", (request["plant_id"],)).fetchone()
        history = conn.execute("SELECT execution_id FROM receiver_executions WHERE publication_id=?", (request["publication_id"],)).fetchone()
        if state["execution_status"] != "running" or state["execution_id"] != execution_id or lease is None or lease[0] != request["publication_id"] or history is None or history[0] != execution_id:
            raise _fail("execution_fence", "Execution admission has changed")
        if type(result) is not dict or result.get("synthetic") is not True or result.get("status") not in ("completed", "blocked", "failed") or type(result.get("events")) is not list:
            raise _fail("execution_result", "Invalid synthetic execution result")
        _canonical(result)
        state.update(sequence=state["sequence"] + 1, execution_status="completed" if result["status"] == "completed" else "failed", result=result)
        signed = self._save(conn, envelope, state)
        conn.execute("DELETE FROM receiver_plant_leases WHERE plant_id=? AND publication_id=?", (request["plant_id"], request["publication_id"]))
        return signed
