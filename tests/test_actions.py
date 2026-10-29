"""Real Store, evidence, signatures and simulation; all credentials are runtime random."""
import secrets
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scheduling_agent.actions import ActionService
from scheduling_agent.domain import Scenario, Snapshot
from scheduling_agent.errors import DomainError
from scheduling_agent.fixtures import demo_snapshots
from scheduling_agent.service import EVIDENCE_TOOLS, SchedulingService
from scheduling_agent.storage import Store

NOW = 1_800_000_000_000


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path.resolve() / "actions-data")
    principal = store.create_user("alice", secrets.token_urlsafe(32), "reviewer", ["synthetic-plant"])
    clock = [NOW]
    service = SchedulingService(store, clock=lambda: clock[0])
    return SimpleNamespace(store=store, principal=principal, clock=clock, service=service,
                           actions=ActionService(service), snapshot=demo_snapshots(NOW)["normal"])


def expect(code, fn, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        fn(*args, **kwargs)
    assert caught.value.code == code
    return caught.value


def recommendation(env, *, snapshot=None, principal=None):
    principal = principal or env.principal
    snapshot = snapshot or env.snapshot
    source = env.service.import_snapshot(principal, snapshot, secrets.token_hex(12))
    with env.store.transaction() as conn:
        task = env.store.create_object(conn, kind="task", object_id="task-" + secrets.token_hex(10),
            principal=principal, plant_id=source["plant_id"], status="running", payload={
                "snapshot_id": source["object_id"],
                "source_binding": env.service.source_binding(conn, principal, source["object_id"]),
                "scenario": Scenario(kind="baseline").model_dump(mode="json"),
                "evidence": {}, "candidate": None,
            })
    for tool in EVIDENCE_TOOLS:
        with env.store.connect() as conn:
            prepared = env.service.prepare_tool(conn, principal, task, tool)
        result = env.service.compute_tool(prepared, tool)
        with env.store.transaction() as conn:
            record = env.service.issue_evidence(conn, principal, task, tool, result)
            payload = deepcopy(task["payload"])
            payload["evidence"][tool] = record
            if tool == "compare_schedules":
                payload["candidate"] = result["candidate"]
            task = env.store.update_object(conn, kind="task", object_id=task["object_id"],
                principal=principal, expected_version=task["version"], status="running", payload=payload)
    with env.store.transaction() as conn:
        return env.service.submit_schedule(conn, principal, task, task["payload"]["candidate"],
                                          task["payload"]["evidence"], "Synthetic candidate for review")


def publication(env, *, snapshot=None, principal=None):
    principal = principal or env.principal
    rec = recommendation(env, snapshot=snapshot, principal=principal)
    rec = env.actions.review(principal, rec["object_id"], action_id=secrets.token_hex(12),
                             recommendation_hash=rec["content_hash"], decision="approved", note="Reviewed")
    return env.actions.request_publication(principal, rec["object_id"], action_id=secrets.token_hex(12),
                                            recommendation_hash=rec["content_hash"], confirmed=True)


def accepted(env, *, principal=None, snapshot=None):
    principal = principal or env.principal
    pub = publication(env, principal=principal, snapshot=snapshot)
    pub = env.actions.deliver_local(principal, pub["object_id"], action_id=secrets.token_hex(12))
    if pub["status"] == "partial":
        pub = env.actions.advance_receiver(principal, pub["object_id"], action_id=secrets.token_hex(12))
    assert pub["status"] == "accepted"
    return pub


def test_review_freezes_hash_role_and_body(env):
    rec = recommendation(env)
    kwargs = dict(action_id="review", recommendation_hash=rec["content_hash"], decision="approved", note="Approved")
    expect("recommendation_changed", env.actions.review, env.principal, rec["object_id"],
           **{**kwargs, "recommendation_hash": secrets.token_hex(32)})
    result = env.actions.review(env.principal, rec["object_id"], **kwargs)
    assert result["status"] == "approved"
    assert env.actions.review(env.principal, rec["object_id"], **kwargs) == result
    expect("idempotency_conflict", env.actions.review, env.principal, rec["object_id"], **{**kwargs, "note": "Changed"})
    assert env.store.list_objects("publication", env.principal) == []
    env.store.set_user(env.principal.user_id, role="operator")
    expect("forbidden", env.actions.review, env.principal, rec["object_id"], **kwargs)


def test_review_canonical_decisions_and_nonblank_notes(env):
    rec = recommendation(env)
    kwargs = dict(action_id="review-canonical", recommendation_hash=rec["content_hash"],
                  decision="rejected", note="Not approved")
    for decision in ("approve", "reject"):
        expect("validation", env.actions.review, env.principal, rec["object_id"],
               **{**kwargs, "decision": decision})
    for note in ("", " \t\n"):
        expect("validation", env.actions.review, env.principal, rec["object_id"],
               **{**kwargs, "note": note})
    rejected = env.actions.review(env.principal, rec["object_id"], **kwargs)
    assert rejected["status"] == "rejected" and rejected["payload"]["review"]["decision"] == "rejected"
    expect("approval_required", env.actions.request_publication, env.principal, rec["object_id"],
           action_id="reject-publish", recommendation_hash=rejected["content_hash"], confirmed=True)
    pub = publication(env)
    for note in ("", " \t\n"):
        expect("validation", env.actions.cancel_publication, env.principal, pub["object_id"],
               action_id="blank-cancel", note=note)


def test_request_never_sends_and_unique_publication(env):
    pub = publication(env)
    assert pub["status"] == "requested"
    assert pub["payload"]["delivery_status"] == "not_sent"
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_records").fetchone()[0] == 0
    again = env.actions.request_publication(env.principal, pub["payload"]["recommendation_id"],
        action_id="request-again", recommendation_hash=pub["payload"]["recommendation_hash"], confirmed=True)
    assert again == pub


def test_partial_is_stage_only_then_full_lease_and_real_events(env):
    pub = publication(env)
    pub = env.actions.deliver_local(env.principal, pub["object_id"], action_id="deliver")
    assert pub["status"] == "partial"
    assert pub["payload"]["accepted_operation_ids"] == sorted(pub["payload"]["signed_request"]["payload"]["operation_ids"])[:1]
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_plant_leases").fetchone()[0] == 0
    expect("receiver_state", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="early", confirmed=True)
    pub = env.actions.advance_receiver(env.principal, pub["object_id"], action_id="advance")
    assert pub["status"] == "accepted" and pub["payload"]["execution_status"] == "none"
    expect("confirmation_required", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="no-confirm", confirmed=1)
    result = env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="execute", confirmed=True)
    assert result["status"] == "completed"
    assert result["payload"]["execution_result"]["synthetic"] is True
    assert result["payload"]["execution_result"]["events"]
    assert env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="execute", confirmed=True) == result
    expect("receiver_state", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="execute-again", confirmed=True)
    read = ActionService(env.service).readback(env.principal, pub["object_id"], action_id="restart-read")
    assert read["payload"]["execution_result"] == result["payload"]["execution_result"]


def test_ack_loss_independent_readback_and_no_redispatch(env):
    pub = publication(env)
    pub = env.actions.deliver_local(env.principal, pub["object_id"], action_id="lost", fault="after_receive")
    assert pub["status"] == "unknown"
    assert pub["payload"]["receiver_receipt"] is None
    assert env.actions.deliver_local(env.principal, pub["object_id"], action_id="lost", fault="after_receive") == pub
    expect("delivery_state", env.actions.deliver_local, env.principal, pub["object_id"], action_id="blind")
    expect("receiver_state", env.actions.advance_receiver, env.principal, pub["object_id"], action_id="unknown-advance")
    read = ActionService(env.service).readback(env.principal, pub["object_id"], action_id="read")
    assert read["status"] == "partial" and read["payload"]["receiver_sequence"] == 1


def test_cancel_tombstone_late_receive_and_old_ack(env):
    pub = publication(env)
    envelope = pub["payload"]["signed_request"]
    cancelled = env.actions.cancel_publication(env.principal, pub["object_id"], action_id="cancel", note="Stop")
    late = env.actions.receiver.receive(env.actions._capability, env.principal, envelope,
                                       validate=env.actions._approved, now_ms=NOW)
    assert late["payload"]["receiver_status"] == "cancelled"
    assert env.actions._ack(env.principal, pub["object_id"], late)["status"] == "cancelled"
    expect("delivery_state", env.actions.deliver_local, env.principal, cancelled["object_id"], action_id="late-deliver")
    other = accepted(env)
    old_ack = other["payload"]["receiver_receipt"]
    env.actions.cancel_publication(env.principal, other["object_id"], action_id="cancel-accepted", note="Stop")
    assert env.actions._ack(env.principal, other["object_id"], old_ack)["status"] == "cancelled"
    with env.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM receiver_plant_leases").fetchone()[0] == 0


def test_cross_owner_whole_plant_exclusive_and_private(env):
    first = accepted(env)
    other = env.store.create_user("bob", secrets.token_urlsafe(32), "reviewer", ["synthetic-plant"])
    pub = publication(env, principal=other)
    env.actions.deliver_local(other, pub["object_id"], action_id="other-deliver")
    error = expect("plant_busy", env.actions.advance_receiver, other, pub["object_id"], action_id="other-advance")
    assert first["object_id"] not in error.message and "alice" not in error.message
    expect("not_found", env.actions.readback, other, first["object_id"], action_id="privacy")
    assert {p["object_id"] for p in env.store.list_objects("publication", other)} == {pub["object_id"]}
    env.actions.cancel_publication(env.principal, first["object_id"], action_id="release", note="Release")
    env.actions.readback(other, pub["object_id"], action_id="other-read")
    assert env.actions.advance_receiver(other, pub["object_id"], action_id="other-retry")["status"] == "accepted"


def test_epoch_and_stable_operations_survive_execution(env):
    first = accepted(env)
    env.actions.execute_synthetic(env.principal, first["object_id"], action_id="execute", confirmed=True)
    duplicate = publication(env)
    env.actions.deliver_local(env.principal, duplicate["object_id"], action_id="duplicate-stage")
    expect("source_used", env.actions.advance_receiver, env.principal, duplicate["object_id"], action_id="duplicate-full")
    env.actions.cancel_publication(env.principal, duplicate["object_id"], action_id="cancel-duplicate", note="Discard duplicate")
    newer = Snapshot.model_validate({**env.snapshot.model_dump(mode="json"), "revision": 2, "observed_at_ms": NOW + 1})
    env.clock[0] += 1
    second = publication(env, snapshot=newer)
    env.actions.deliver_local(env.principal, second["object_id"], action_id="new-stage")
    expect("operation_used", env.actions.advance_receiver, env.principal, second["object_id"], action_id="new-full")


def test_source_change_and_stale_gate(env):
    pub = publication(env)
    env.clock[0] += 86_400_001
    expect("source_stale", env.actions.deliver_local, env.principal, pub["object_id"], action_id="stale")
    env.clock[0] = NOW
    updated = Snapshot.model_validate({**env.snapshot.model_dump(mode="json"), "revision": 2})
    env.service.import_snapshot(env.principal, updated, "update")
    expect("source_changed", env.actions.deliver_local, env.principal, pub["object_id"], action_id="changed")
    assert env.actions.cancel_publication(env.principal, pub["object_id"], action_id="cancel-stale", note="Stop")["status"] == "cancelled"


def test_crashed_running_cannot_reexecute_or_fake_completion(env, monkeypatch):
    from scheduling_agent import simulation
    pub = accepted(env)
    def crash(problem, plan):
        assert not env.store._connections
        stored = env.store.get_object("publication", pub["object_id"], env.principal)
        assert stored["status"] == "running"
        raise KeyboardInterrupt
    monkeypatch.setattr(simulation, "simulate", crash)
    with pytest.raises(KeyboardInterrupt):
        env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="crash", confirmed=True)
    restarted = ActionService(env.service)
    read = restarted.readback(env.principal, pub["object_id"], action_id="read-crash")
    assert read["status"] == "running" and read["payload"]["execution_result"] is None
    assert restarted.execute_synthetic(env.principal, pub["object_id"], action_id="crash", confirmed=True)["status"] == "running"
    expect("receiver_state", restarted.execute_synthetic, env.principal, pub["object_id"], action_id="rerun", confirmed=True)
    expect("too_late", restarted.cancel_publication, env.principal, pub["object_id"], action_id="late", note="Stop")


def test_cancel_execution_race_and_source_commit_fence(env, monkeypatch):
    from scheduling_agent import simulation
    pub = accepted(env)
    real = simulation.simulate
    def race(problem, plan):
        assert not env.store._connections
        expect("too_late", env.actions.cancel_publication, env.principal, pub["object_id"], action_id="racing-cancel", note="Stop")
        result = real(problem, plan)
        updated = Snapshot.model_validate({**env.snapshot.model_dump(mode="json"), "revision": 2})
        env.service.import_snapshot(env.principal, updated, "race-source")
        return result
    monkeypatch.setattr(simulation, "simulate", race)
    final = env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="race-execute", confirmed=True)
    assert final["status"] == "failed"
    assert final["payload"]["execution_result"]["events"]
    assert final["payload"]["execution_result"]["cause"] == "source_changed"


def test_cancel_before_execute_and_lease_is_mandatory(env):
    pub = accepted(env)
    with env.store.transaction() as conn:
        conn.execute("DELETE FROM receiver_plant_leases")
    expect("receiver_lease", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="missing-lease", confirmed=True)
    env.actions.cancel_publication(env.principal, pub["object_id"], action_id="cancel", note="Stop")
    expect("receiver_state", env.actions.execute_synthetic, env.principal, pub["object_id"], action_id="after-cancel", confirmed=True)


def test_single_operation_accepts_in_one_delivery(env):
    data = env.snapshot.model_dump(mode="json")
    data["lots"] = [data["lots"][1]]
    data["orders"] = [data["orders"][1]]
    single = Snapshot.model_validate(data)
    pub = publication(env, snapshot=single)
    assert env.actions.deliver_local(env.principal, pub["object_id"], action_id="single")["status"] == "accepted"


def test_unknown_intent_blocks_other_plan_until_authoritative_resolution(env):
    first = publication(env)
    second = publication(env)
    env.actions.deliver_local(env.principal, first["object_id"], action_id="lost", fault="after_receive")
    expect("plant_busy", env.actions.deliver_local, env.principal, second["object_id"], action_id="bypass")
    expect("plant_busy", publication, env)
    env.actions.readback(env.principal, first["object_id"], action_id="recover")
    assert env.actions.deliver_local(env.principal, second["object_id"], action_id="after-recover")["status"] == "partial"


def test_real_blocked_simulation_preserves_actual_events(env, monkeypatch):
    from scheduling_agent import simulation
    pub = accepted(env)
    real = simulation.simulate
    def disrupted(problem, plan):
        return real(problem, plan, disruptions=(Scenario(kind="machine_outage", machine_id="M1", start_min=0, end_min=1),))
    monkeypatch.setattr(simulation, "simulate", disrupted)
    final = env.actions.execute_synthetic(env.principal, pub["object_id"], action_id="blocked", confirmed=True)
    assert final["status"] == "failed"
    result = final["payload"]["execution_result"]
    assert result["status"] == "blocked" and result["events"]
    assert any(event["kind"] == "execution_blocked" for event in result["events"])
    read = ActionService(env.service).readback(env.principal, pub["object_id"], action_id="read-blocked")
    assert read["payload"]["execution_result"] == result


def test_new_epoch_with_explicit_new_operations_can_execute(env):
    first = accepted(env)
    env.actions.execute_synthetic(env.principal, first["object_id"], action_id="execute-old", confirmed=True)
    data = env.snapshot.model_dump(mode="json")
    data.update(revision=2, observed_at_ms=NOW + 1)
    for lot in data["lots"]:
        for operation in lot["operations"]:
            operation["operation_id"] += "-next"
    for commitment in data["commitments"]:
        commitment["operation_id"] += "-next"
        if commitment["predecessor_id"] is not None:
            commitment["predecessor_id"] += "-next"
    env.clock[0] += 1
    second = accepted(env, snapshot=Snapshot.model_validate(data))
    assert env.actions.execute_synthetic(env.principal, second["object_id"], action_id="execute-new", confirmed=True)["status"] == "completed"
