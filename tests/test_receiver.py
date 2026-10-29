"""Receiver authenticity, filesystem continuity, sequence and crash boundaries."""
import hashlib
import hmac
import os
import secrets
import stat
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from test_actions import NOW, accepted, expect, publication
from test_actions import env as env

from scheduling_agent.actions import ActionService
from scheduling_agent.errors import DomainError
from scheduling_agent.receiver import LocalReceiver
from scheduling_agent.storage import Store, _canonical


def test_capability_identity_signature_and_domain_separation(env):
    pub = publication(env)
    receiver = env.actions.receiver
    envelope = pub["payload"]["signed_request"]
    expect("receiver_capability", receiver.sign_request, object(), envelope["payload"])
    expect("receiver_capability", receiver.latest, object(), env.principal, envelope)
    other = LocalReceiver(env.store)
    expect("receiver_capability", receiver.sign_request, other._capability, envelope["payload"])
    forged = deepcopy(envelope)
    forged["signature"] = hmac.new(secrets.token_bytes(32), _canonical(forged["payload"]), hashlib.sha256).hexdigest()
    expect("receiver_signature", receiver.receive, env.actions._capability, env.principal, forged,
           validate=env.actions._approved, now_ms=NOW)
    for field, value in (("plan_hash", secrets.token_hex(32)), ("owner", "mallory"),
                         ("recommendation_hash", secrets.token_hex(32)), ("plant_id", "elsewhere")):
        tampered = deepcopy(envelope)
        tampered["payload"][field] = value
        with pytest.raises(DomainError):
            receiver.receive(env.actions._capability, env.principal, tampered, validate=env.actions._approved, now_ms=NOW)
    with pytest.raises(DomainError):
        receiver._verify("cancel", envelope)
    changed = deepcopy(envelope["payload"])
    changed["source_binding"]["revision"] += 1
    valid_but_wrong = receiver.sign_request(env.actions._capability, changed)
    expect("receiver_binding", receiver.receive, env.actions._capability, env.principal, valid_but_wrong,
           validate=env.actions._approved, now_ms=NOW)
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_records").fetchone()[0] == 0


def test_schema_rejects_extras_boolean_time_nonfinite_and_ids(env):
    envelope = publication(env)["payload"]["signed_request"]
    payload = envelope["payload"]
    variants = []
    variants.append({**payload, "unexpected": True})
    variants.append({**payload, "created_at_ms": True})
    variants.append({**payload, "expires_at_ms": float("inf")})
    variants.append({**payload, "synthetic": 1})
    variants.append({**payload, "operation_ids": payload["operation_ids"] * 2})
    variants.append({**payload, "operation_ids": list(reversed(payload["operation_ids"]))})
    variants.append({**payload, "source_binding": {**payload["source_binding"], "extra": "no"}})
    for malformed in variants:
        with pytest.raises(DomainError):
            env.actions.receiver.sign_request(env.actions._capability, malformed)


def test_key_restart_loss_and_replacement_are_failclosed(env):
    pub = publication(env)
    path = env.store.root / "receiver.key"
    assert path.stat().st_size == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.store.root.stat().st_mode) == 0o700
    before = path.read_bytes()
    restarted = LocalReceiver(Store(env.store.root))
    assert restarted.sign_request(restarted._capability, pub["payload"]["signed_request"]["payload"]) == pub["payload"]["signed_request"]
    assert before not in _canonical(pub)
    path.unlink()
    expect("receiver_key", LocalReceiver, env.store)
    assert not path.exists()
    path.write_bytes(secrets.token_bytes(32))
    path.chmod(0o600)
    expect("receiver_key", LocalReceiver, env.store)
    expect("receiver_key", env.actions.receiver.sign_request, env.actions._capability, pub["payload"]["signed_request"]["payload"])


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "wide", "short", "directory"])
def test_unsafe_key_file_is_rejected(env, tmp_path, unsafe):
    path = env.store.root / "receiver.key"
    if unsafe in ("symlink", "directory"):
        path.unlink()
    if unsafe == "symlink":
        other = tmp_path / "other-key"
        other.write_bytes(secrets.token_bytes(32))
        other.chmod(0o600)
        path.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(path, tmp_path / "linked-key")
    elif unsafe == "wide":
        path.chmod(0o640)
    elif unsafe == "short":
        path.write_bytes(secrets.token_bytes(17))
    else:
        path.mkdir(mode=0o700)
    expect("receiver_key", LocalReceiver, env.store)


def test_unsafe_private_root_and_exclusive_initialization(tmp_path):
    store = Store(tmp_path.resolve() / "keys-data")
    store.root.chmod(0o750)
    expect("receiver_key", LocalReceiver, store)
    store.root.chmod(0o700)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receivers = list(pool.map(lambda _: LocalReceiver(store), range(8)))
    assert len({r._key_hash for r in receivers}) == 1
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_meta").fetchone()[0] == 1


def test_key_replacement_between_open_and_stat_is_detected(env, monkeypatch):
    real_read = os.read
    replaced = False
    def racing_read(fd, amount):
        nonlocal replaced
        result = real_read(fd, amount)
        if amount == 33 and not replaced:
            replaced = True
            path = env.store.root / "receiver.key"
            replacement = env.store.root / "replacement-key"
            replacement.write_bytes(secrets.token_bytes(32))
            replacement.chmod(0o600)
            replacement.replace(path)
        return result
    monkeypatch.setattr(os, "read", racing_read)
    expect("receiver_key", LocalReceiver, env.store)


def test_actual_old_ack_does_not_regress_and_fabricated_sequence_rejected(env):
    pub = publication(env)
    first = env.actions.deliver_local(env.principal, pub["object_id"], action_id="first")
    old = first["payload"]["receiver_receipt"]
    current = env.actions.advance_receiver(env.principal, pub["object_id"], action_id="advance")
    assert env.actions._ack(env.principal, pub["object_id"], old)["status"] == "accepted"
    assert env.actions._ack(env.principal, pub["object_id"], current["payload"]["receiver_receipt"]) == current
    for receipt in (old, current["payload"]["receiver_receipt"]):
        modified = deepcopy(receipt["payload"])
        modified["receiver_status"] = "cancelled"
        signed = env.actions.receiver._sign("receipt", modified)
        expect("receiver_sequence", env.actions._ack, env.principal, pub["object_id"], signed)
    future = deepcopy(current["payload"]["receiver_receipt"]["payload"])
    future["sequence"] += 1
    expect("receiver_sequence", env.actions._ack, env.principal, pub["object_id"], env.actions.receiver._sign("receipt", future))


def test_unknown_without_receiver_receipt_cannot_reset_or_retry(env, monkeypatch):
    pub = publication(env)
    def crash(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(env.actions.receiver, "receive", crash)
    with pytest.raises(KeyboardInterrupt):
        env.actions.deliver_local(env.principal, pub["object_id"], action_id="crash-before-receive")
    read = ActionService(env.service).readback(env.principal, pub["object_id"], action_id="read")
    assert read["status"] == "unknown" and read["payload"]["receiver_receipt"] is None
    expect("delivery_state", env.actions.deliver_local, env.principal, pub["object_id"], action_id="blind")
    assert env.actions.cancel_publication(env.principal, pub["object_id"], action_id="cancel", note="Abort unknown")["status"] == "cancelled"


def test_receiver_commit_ack_cancel_race(env, monkeypatch):
    pub = publication(env)
    actual = env.actions.receiver.receive
    def race(*args, **kwargs):
        receipt = actual(*args, **kwargs)
        env.actions.cancel_publication(env.principal, pub["object_id"], action_id="race-cancel", note="Stop")
        return receipt
    monkeypatch.setattr(env.actions.receiver, "receive", race)
    result = env.actions.deliver_local(env.principal, pub["object_id"], action_id="race-deliver")
    assert result["status"] == "cancelled"
    assert result["payload"]["receiver_sequence"] == 2


def test_execution_revocation_never_completes_or_reexecutes(env, monkeypatch):
    from scheduling_agent import simulation
    pub = accepted(env)
    real = simulation.simulate
    def revoke(problem, plan):
        result = real(problem, plan)
        env.store.set_user(env.principal.user_id, active=False)
        return result
    monkeypatch.setattr(simulation, "simulate", revoke)
    expect("forbidden", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="execute", confirmed=True)
    with env.store.connect() as conn:
        row = conn.execute("SELECT receipt FROM receiver_records WHERE publication_id=?", (pub["object_id"],)).fetchone()
        import json
        assert json.loads(row[0])["payload"]["execution_status"] == "running"
        assert conn.execute("SELECT count(*) FROM receiver_used_operations").fetchone()[0] > 0


def test_receiver_result_and_execution_id_fence(env):
    pub = accepted(env)
    receiver = env.actions.receiver
    envelope = pub["payload"]["signed_request"]
    with env.store.transaction() as conn:
        receipt, _, _ = receiver.admit_execution(env.actions._capability, conn, env.principal, envelope,
                                                validate=env.actions._approved, now_ms=NOW)
    with env.store.transaction() as conn:
        expect("execution_fence", receiver.finish_execution, env.actions._capability, conn,
               env.principal, envelope, execution_id=secrets.token_hex(24),
               result={"synthetic": True, "status": "completed", "events": []}, now_ms=NOW)
    with env.store.transaction() as conn:
        expect("execution_result", receiver.finish_execution, env.actions._capability, conn,
               env.principal, envelope, execution_id=receipt["payload"]["execution_id"],
               result={"synthetic": False, "status": "completed", "events": []}, now_ms=NOW)


def test_repeated_readback_still_uses_independent_latest_receipt(env):
    pub = publication(env)
    env.actions.deliver_local(env.principal, pub["object_id"], action_id="deliver")
    initial = env.actions.readback(env.principal, pub["object_id"], action_id="same-read")
    receipt = env.actions.receiver.advance(env.actions._capability, env.principal,
        pub["payload"]["signed_request"], validate=env.actions._approved, now_ms=NOW)
    assert initial["status"] == "partial" and receipt["payload"]["receiver_status"] == "accepted"
    latest = env.actions.readback(env.principal, pub["object_id"], action_id="same-read")
    assert latest["status"] == "accepted"


def test_late_old_ack_cannot_regress_completed_execution(env):
    pub = accepted(env)
    old = pub["payload"]["receiver_receipt"]
    completed = env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="complete", confirmed=True)
    assert env.actions._ack(env.principal, pub["object_id"], old) == completed


def test_atomic_cancel_against_full_admission(env):
    pub = publication(env)
    env.actions.deliver_local(env.principal, pub["object_id"], action_id="stage")
    def advance():
        try:
            return env.actions.advance_receiver(env.principal, pub["object_id"], action_id="advance-race")
        except DomainError as exc:
            assert exc.code in ("receiver_state", "plant_busy")
    def cancel():
        return env.actions.cancel_publication(env.principal, pub["object_id"], action_id="cancel-race", note="Stop")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance), pool.submit(cancel)]
        for future in futures:
            future.result()
    final = env.actions.readback(env.principal, pub["object_id"], action_id="race-read")
    assert final["status"] == "cancelled"
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_plant_leases").fetchone()[0] == 0


def test_cross_owner_partial_cancel_cannot_release_another_lease(env):
    first = accepted(env)
    other = env.store.create_user("bob", secrets.token_urlsafe(32), "reviewer", ["synthetic-plant"])
    second = publication(env, principal=other)
    env.actions.deliver_local(other, second["object_id"], action_id="stage-other")
    env.actions.cancel_publication(other, second["object_id"], action_id="cancel-other", note="Stop")
    with env.store.connect() as conn:
        assert conn.execute("SELECT publication_id FROM receiver_plant_leases").fetchone()[0] == first["object_id"]


def test_readback_of_old_partial_does_not_settle_crashed_advance(env, monkeypatch):
    pub = publication(env)
    partial = env.actions.deliver_local(env.principal, pub["object_id"], action_id="stage")
    def crash(*args, **kwargs):
        read = env.actions.readback(env.principal, pub["object_id"], action_id="inflight-read")
        assert read["status"] == "unknown"
        raise KeyboardInterrupt
    monkeypatch.setattr(env.actions.receiver, "advance", crash)
    with pytest.raises(KeyboardInterrupt):
        env.actions.advance_receiver(env.principal, pub["object_id"], action_id="crashed-advance")
    restarted = ActionService(env.service)
    current = restarted.readback(env.principal, pub["object_id"], action_id="restart-read")
    assert current["status"] == "unknown"
    assert current["payload"]["receiver_sequence"] == partial["payload"]["receiver_sequence"]
    expect("receiver_state", restarted.advance_receiver, env.principal, pub["object_id"], action_id="blind-advance")
    assert restarted.cancel_publication(env.principal, pub["object_id"], action_id="resolve", note="Stop")["status"] == "cancelled"


@pytest.mark.parametrize("receiver_status", ["partial", "accepted"])
def test_lost_ack_then_cancel_crash_preserves_pending_cancel(env, monkeypatch, receiver_status):
    if receiver_status == "accepted":
        data = env.snapshot.model_dump(mode="json")
        data["lots"] = [data["lots"][1]]
        data["orders"] = [data["orders"][1]]
        env.snapshot = type(env.snapshot).model_validate(data)
    pub = publication(env)
    other = publication(env)
    lost = env.actions.deliver_local(env.principal, pub["object_id"], action_id="lost-ack", fault="after_receive")
    assert lost["status"] == "unknown" and lost["payload"]["receiver_sequence"] == 0
    old_receipt = env.actions.receiver.latest(env.actions._capability, env.principal, pub["payload"]["signed_request"])
    assert old_receipt["payload"]["receiver_status"] == receiver_status
    assert old_receipt["payload"]["sequence"] == 1
    attempts = []
    def crash(*args, **kwargs):
        attempts.append(True)
        raise KeyboardInterrupt
    monkeypatch.setattr(env.actions.receiver, "cancel", crash)
    with pytest.raises(KeyboardInterrupt):
        env.actions.cancel_publication(env.principal, pub["object_id"], action_id="crashed-cancel", note="Stop")
    restarted = ActionService(env.service)
    actual_cancel = restarted.receiver.cancel
    monkeypatch.setattr(restarted.receiver, "cancel", crash)
    read = restarted.readback(env.principal, pub["object_id"], action_id="read-cancel")
    assert read["status"] == "unknown"
    assert read["payload"]["delivery_status"] == "unknown"
    assert read["payload"]["pending_action"]["action"] == "cancel_publication"
    assert restarted._ack(env.principal, pub["object_id"], old_receipt) == read
    assert restarted.cancel_publication(env.principal, pub["object_id"], action_id="crashed-cancel", note="Stop") == read
    assert len(attempts) == 1  # Replay/readback must never redispatch the unknown cancel.
    expect("receiver_state", restarted.advance_receiver, env.principal, pub["object_id"], action_id="blind-advance")
    expect("receiver_state", restarted.execute_synthetic, env.principal, pub["object_id"], action_id="blind-execute", confirmed=True)
    expect("plant_busy", restarted.deliver_local, env.principal, other["object_id"], action_id="bypass-cancel")
    with env.store.connect() as conn:
        assert conn.execute("SELECT publication_id FROM receiver_delivery_intents").fetchone()[0] == pub["object_id"]
        assert conn.execute("SELECT count(*) FROM receiver_executions").fetchone()[0] == 0
    monkeypatch.setattr(restarted.receiver, "cancel", actual_cancel)
    final = restarted.cancel_publication(env.principal, pub["object_id"], action_id="explicit-new-cancel", note="Resolve cancellation")
    assert final["status"] == "cancelled" and final["payload"]["pending_action"] is None
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_delivery_intents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM receiver_plant_leases").fetchone()[0] == 0


@pytest.mark.parametrize("execution_status", ["running", "completed"])
def test_crashed_cancel_reconciles_truthful_started_execution(env, monkeypatch, execution_status):
    from scheduling_agent import simulation
    pub = accepted(env)
    def crash(*args, **kwargs):
        raise KeyboardInterrupt
    if execution_status == "running":
        monkeypatch.setattr(simulation, "simulate", crash)
        with pytest.raises(KeyboardInterrupt):
            env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="started", confirmed=True)
    else:
        env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="started", confirmed=True)
    monkeypatch.setattr(env.actions.receiver, "cancel", crash)
    with pytest.raises(KeyboardInterrupt):
        env.actions.cancel_publication(env.principal, pub["object_id"], action_id="crashed-cancel", note="Stop")
    restarted = ActionService(env.service)
    read = restarted.readback(env.principal, pub["object_id"], action_id="truthful-read")
    assert read["status"] == execution_status
    assert read["payload"]["receiver_status"] == "accepted"
    assert read["payload"]["pending_action"] is None
    expect("too_late", restarted.cancel_publication, env.principal, pub["object_id"], action_id="crashed-cancel", note="Stop")
    expect("receiver_state", restarted.execute_synthetic, env.principal, pub["object_id"], action_id="blind-repeat", confirmed=True)


def test_existing_receiver_schema_without_key_marker_never_regenerates(env):
    with env.store.transaction() as conn:
        conn.execute("DELETE FROM receiver_meta")
    path = env.store.root / "receiver.key"
    path.unlink()
    expect("receiver_key", LocalReceiver, env.store)
    assert not path.exists()
