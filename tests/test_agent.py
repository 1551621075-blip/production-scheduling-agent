"""R6 engine integration: actual SQLite, LangGraph, CP-SAT and independent validation.

The explicitly TEST-only provider is not model-inference acceptance. Provider
subclasses inject choices/faults only; trusted scheduling results are never stubbed.
Crash fixtures persist actual journal states. Fake clocks, not sleeps, drive limits.
"""

from __future__ import annotations

import copy
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import httpx
import pytest
import requests

from scheduling_agent import agent as module
from scheduling_agent.agent import AgentEngine
from scheduling_agent.domain import (
    CompiledProblem,
    Plan,
    Scenario,
    Snapshot,
    canonical_digest,
    missing_inputs,
)
from scheduling_agent.errors import DomainError
from scheduling_agent.fixtures import demo_scenarios, demo_snapshots
from scheduling_agent.provider import ProviderError, ProviderReply, SyntheticProvider
from scheduling_agent.service import EVIDENCE_TOOLS, SchedulingService
from scheduling_agent.storage import Principal, Store
from scheduling_agent.validator import validate_plan

NOW = 1_800_000_000_000
TOKEN = "".join(("r6-agent-", "inert-test-", "token-", "x" * 32))
CRASH_LEASE = "lease-r6-crash"
FIRST_SEVEN = (
    "get_snapshot", "build_scenario", "compile_problem", "solve_schedule",
    "validate_schedule", "compare_schedules", "simulate_schedule",
)
NINE_TOOLS = {*FIRST_SEVEN, "request_human_input", "submit_schedule"}
ANSWERS = [
    {"field": "materials[0].initial_stock", "value": 0},
    {"field": "orders[0].due_min", "value": 8},
]


@pytest.fixture(autouse=True)
def no_network_processes_or_model_runtime(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("external I/O")
        raise AssertionError("R6 agent tests forbid networking and subprocess/model startup")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    yield
    assert attempts == [], "A swallowed tracing/network/process attempt is still a failure"


@dataclass
class Clock:
    ms: int = NOW
    seconds: float = 100.0

    def wall(self):
        return self.ms

    def monotonic(self):
        return self.seconds


class ChoiceProvider(SyntheticProvider):
    """Keep the real TEST provider protocol; replace only explicitly selected replies."""

    def __init__(self, hook=None):
        super().__init__()
        self.contexts = []
        self.hook = hook
        self.closed_count = 0

    def decide(self, context):
        self.contexts.append(copy.deepcopy(context))
        reply = super().decide(context)
        if self.hook is not None:
            replacement = self.hook(context, reply)
            if replacement is not None:
                return replacement
        return reply

    def close(self):
        self.closed_count += 1


class SessionProvider(ChoiceProvider):
    def __init__(self, hook=None):
        super().__init__(hook)
        self.sessions = self.exits = 0
        self.active = False
        self.in_session = []

    @contextmanager
    def session(self):
        self.sessions += 1
        self.active = True
        try:
            yield self
        finally:
            self.active = False
            self.exits += 1

    def decide(self, context):
        self.in_session.append(self.active)
        return super().decide(context)


@dataclass
class World:
    store: Store
    principal: Principal
    service: SchedulingService
    engine: AgentEngine
    provider: SyntheticProvider
    snapshot: Snapshot
    snapshot_id: str
    scenario: Scenario
    clock: Clock
    task_id: str

    def get(self):
        return self.engine.get(self.principal, self.task_id)

    def run(self):
        return self.engine.run(self.principal, self.task_id)

    def rows(self):
        with self.store.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM calls WHERE task_id=? ORDER BY created_at_ms,rowid", (self.task_id,),
            )]

    def recommendations(self):
        return self.store.list_objects("recommendation", self.principal)

    def patch(self, **changes):
        """Explicit durable crash/tamper fixture; never bypass the Store CAS API."""
        with self.store.transaction() as conn:
            obj = self.store.get_object("task", self.task_id, self.principal, conn=conn)
            payload = {**obj["payload"], **copy.deepcopy(changes)}
            return self.store.update_object(
                conn, kind="task", object_id=self.task_id, principal=self.principal,
                expected_version=obj["version"], status=payload["status"], payload=payload,
            )

    def journal(self, *, status, tokens=None, elapsed=0.0, action=None,
                call_id="call-r6-crash", lease_id=CRASH_LEASE):
        response_hash = self.store.put_artifact({"action": action}) if action is not None else None
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO calls(call_id,task_id,owner,lease_id,status,tokens,elapsed_s,"
                "response_hash,error_code,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (call_id, self.task_id, self.principal.user_id, lease_id, status,
                 tokens, elapsed, response_hash, None, self.clock.ms - 1),
            )
        return call_id

    def restart(self, provider=None):
        self.store = Store(self.store.root)
        self.service = SchedulingService(self.store, clock=self.clock.wall)
        self.provider = self.provider if provider is None else provider
        self.engine = AgentEngine(self.service, self.provider, clock=self.clock.wall,
                                  monotonic=self.clock.monotonic)

    def revise_source(self):
        data = self.service.get_snapshot(self.principal, self.snapshot_id)["payload"]
        data["revision"] += 1
        return self.service.import_snapshot(
            self.principal, Snapshot.model_validate(data), f"revision-{data['revision']}",
        )


@pytest.fixture
def world(tmp_path):
    counter = 0

    def make(case="normal", *, provider=None, scenario="baseline"):
        nonlocal counter
        counter += 1
        clock = Clock()
        store = Store(tmp_path.resolve() / f"r6-agent-store-{counter}")
        snapshot = demo_snapshots(clock.ms)[case]
        principal = store.create_user("r6-admin", TOKEN, "admin", [snapshot.plant_id])
        service = SchedulingService(store, clock=clock.wall)
        source = service.import_snapshot(principal, snapshot, "r6-import")
        selected = (Scenario(kind="baseline") if scenario == "baseline"
                    else demo_scenarios(snapshot)[scenario])
        provider = provider if provider is not None else ChoiceProvider()
        engine = AgentEngine(service, provider, clock=clock.wall, monotonic=clock.monotonic)
        task = engine.create(principal, source["object_id"], scenario=selected,
                             action_id="r6-create")
        return World(store, principal, service, engine, provider, snapshot, source["object_id"],
                     selected, clock, task["object_id"])

    return make


def error(code, operation, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        operation(*args, **kwargs)
    assert caught.value.code == code
    assert TOKEN not in str(caught.value)
    return caught.value


def assert_no_publication(w):
    for kind in ("publication", "execution", "receiver", "device_receipt"):
        assert w.store.list_objects(kind, w.principal) == []


def assert_no_recommendation(w):
    assert w.recommendations() == []
    assert_no_publication(w)


def unvalidated_reply(action, tokens=37):
    """Corrupt a reply AFTER the provider validator to exercise engine enforcement."""
    reply = ProviderReply({"tool": "get_snapshot", "arguments": {}}, tokens)
    object.__setattr__(reply, "action", copy.deepcopy(action))
    return reply


def assert_run_rejected(w, code):
    try:
        task = w.run()
    except DomainError as exc:
        assert exc.code == code
    else:
        assert task["status"] == "blocked"
        assert task["payload"]["error"] == code
    assert_no_recommendation(w)


def reconcile(w, call_id, *, action_id="r6-reconcile", tokens=19):
    return w.engine.reconcile(
        w.principal, w.task_id, action_id=action_id, call_id=call_id, tokens=tokens,
        note="TEST explicit no-result accounting, not a recovered model answer.", no_result=True,
    )


def test_actual_test_provider_not_model_acceptance_runs_real_solver_to_review(world):
    provider = SyntheticProvider()
    w = world(provider=provider)
    assert provider.is_test_provider is True
    assert "TEST" in provider.metadata["provider"]
    task = w.run()
    assert task["status"] == "submitted"
    assert task["payload"]["provider_fingerprint"] == provider.fingerprint
    assert task["payload"]["calls"] == task["payload"]["tools"] == 8
    assert task["payload"]["tokens"] == 8 * 128
    rec = w.recommendations()[0]
    assert rec["status"] == "pending_review"
    assert rec["payload"]["simulation"]["synthetic"] is True
    assert "TEST ONLY" in rec["payload"]["untrusted_model_draft"]
    assert_no_publication(w)


@pytest.mark.parametrize("first", ["compare_schedules", "simulate_schedule"])
def test_real_graph_order_exact_evidence_and_restart_no_rerun(world, first):
    def choose(context, reply):
        if first in context["available_tools"]:
            return ProviderReply({"tool": first, "arguments": {}}, 128)
        return reply

    w = world(provider=ChoiceProvider(choose))
    assert w.provider.contexts == []
    task = w.run()
    p = task["payload"]
    assert task["status"] == p["status"] == "submitted"
    assert (p["calls"], p["tools"], p["tokens"], p["unknown_calls"]) == (8, 8, 1024, 0)
    assert p["pending_action"] is None and p["lease_id"] is None
    assert set(module.TOOLS) == NINE_TOOLS and len(module.TOOLS) == 9
    assert set(p["evidence"]) == set(EVIDENCE_TOOLS) == set(FIRST_SEVEN)
    assert [ctx["available_tools"] for ctx in w.provider.contexts[:5]] == [
        [tool] for tool in FIRST_SEVEN[:5]
    ]
    assert set(w.provider.contexts[5]["available_tools"]) == {
        "compare_schedules", "simulate_schedule",
    }
    other = "simulate_schedule" if first == "compare_schedules" else "compare_schedules"
    assert w.provider.contexts[6]["available_tools"] == [other]
    assert w.provider.contexts[7]["available_tools"] == ["submit_schedule"]
    assert all(set(ctx) == {"task_id", "context_hash", "available_tools", "evidence",
                           "candidate", "required_input"} for ctx in w.provider.contexts)
    assert all(ctx["context_hash"] == canonical_digest(p["source_binding"])
               for ctx in w.provider.contexts)
    ids = []
    for tool, record in p["evidence"].items():
        assert set(record) == {"evidence_id", "tool", "payload", "hash"}
        assert record["tool"] == tool
        assert record["hash"] == canonical_digest(record["payload"])
        assert record["evidence_id"] == "ev-" + canonical_digest({
            "tool": tool, "payload": record["payload"],
        })
        assert record["payload"]["task_id"] == w.task_id
        assert record["payload"]["source_binding"] == p["source_binding"]
        assert record["payload"]["scenario_hash"] == canonical_digest(w.scenario)
        ids.append(record["evidence_id"])
    assert len(set(ids)) == 7
    problem = CompiledProblem.model_validate(
        p["evidence"]["compile_problem"]["payload"]["compiled_problem"],
    )
    solved = p["evidence"]["solve_schedule"]["payload"]
    assert solved["baseline"]["mode"] == "feasibility"
    assert solved["baseline"]["status"] == "FEASIBLE"
    assert solved["optimized"]["mode"] == "optimize"
    assert solved["optimized"]["status"] in {"OPTIMAL", "FEASIBLE"}
    for key in ("baseline", "optimized"):
        assert validate_plan(problem, Plan.model_validate(solved[key]["plan"])).valid is True
    candidate = Plan.model_validate(p["candidate"]["plan"])
    assert validate_plan(problem, candidate).valid is True
    assert p["candidate"]["plan_hash"] == canonical_digest(candidate)
    simulations = p["evidence"]["simulate_schedule"]["payload"]["simulations"]
    assert set(simulations) == {canonical_digest(solved[key]["plan"])
                                for key in ("baseline", "optimized")}
    assert all(sim["synthetic"] is True and sim["status"] == "completed"
               for sim in simulations.values())
    rec = w.recommendations()[0]
    assert rec["object_id"] == "plan-" + w.task_id == p["recommendation_id"]
    assert rec["payload"]["candidate"] == candidate.model_dump(mode="json")
    assert rec["payload"]["evidence"] == p["evidence"]
    assert rec["payload"]["review"] is None
    assert rec["payload"]["simulation"] == simulations[p["candidate"]["plan_hash"]]
    assert all(row["status"] == "completed" and row["tokens"] == 128 for row in w.rows())
    assert set(p["handled_call_ids"]) == {row["call_id"] for row in w.rows()}
    for row in w.rows():
        assert set(w.store.get_artifact(row["response_hash"])) == {"action"}
    assert_no_publication(w)
    w.restart()
    assert w.run()["payload"]["recommendation_id"] == rec["object_id"]
    assert len(w.rows()) == len(w.provider.contexts) == 8
    assert len(w.recommendations()) == 1


@pytest.mark.parametrize("scenario", ["insert_order", "machine_outage", "supply_delay"])
def test_explicit_scenario_is_immutable_and_preserves_source_lineage(world, scenario):
    w = world(scenario=scenario)
    before = w.service.get_snapshot(w.principal, w.snapshot_id)
    task = w.run()
    assert task["status"] == "submitted"
    p = task["payload"]
    assert p["scenario"] == w.scenario.model_dump(mode="json")
    built = p["evidence"]["build_scenario"]["payload"]
    changed = Snapshot.model_validate(built["snapshot"])
    assert built["scenario"] == p["scenario"]
    assert changed.observed_at_ms == w.snapshot.observed_at_ms
    assert changed.revision == w.snapshot.revision
    assert changed.commitments == w.snapshot.commitments
    assert canonical_digest(changed) != canonical_digest(w.snapshot)
    assert w.service.get_snapshot(w.principal, w.snapshot_id) == before
    assert w.recommendations()[0]["payload"]["scenario"] == p["scenario"]
    assert_no_publication(w)


def test_infeasible_real_solver_stops_without_candidate_or_fabricated_result(world):
    w = world("infeasible")
    task = w.run()
    assert task["status"] == "no_schedule"
    assert task["payload"]["candidate"] is None
    solved = task["payload"]["evidence"]["solve_schedule"]["payload"]
    for name in ("baseline", "optimized"):
        assert solved[name]["status"] == "INFEASIBLE"
        assert solved[name]["plan"] is None
    assert set(task["payload"]["evidence"]) == set(FIRST_SEVEN[:5])
    assert task["payload"]["no_schedule_reason"] == "proven_infeasible"
    assert task["payload"]["calls"] == task["payload"]["tools"] == 5
    assert w.run() == task
    assert len(w.provider.contexts) == 5
    assert_no_recommendation(w)


def test_missing_real_test_provider_asks_exact_paths_and_never_guesses(world):
    w = world("missing")
    task = w.run()
    assert task["status"] == "awaiting_input"
    p = task["payload"]
    assert p["awaiting_fields"] == list(missing_inputs(w.snapshot)) == [
        "materials[0].initial_stock", "orders[0].due_min",
    ]
    assert p["calls"] == p["tools"] == 2 and p["tokens"] == 256
    assert p["candidate"] is None
    assert set(p["evidence"]) == {"get_snapshot"}
    assert w.provider.contexts[1]["available_tools"] == ["request_human_input"]
    assert w.provider.contexts[1]["required_input"] == p["awaiting_fields"]
    assert w.run() == task and len(w.provider.contexts) == 2
    assert w.service.get_snapshot(w.principal, w.snapshot_id)["payload"] == (
        w.snapshot.model_dump(mode="json")
    )
    assert_no_recommendation(w)


def test_resume_exact_answers_new_revision_without_time_or_budget_refresh(world):
    w = world("missing")
    w.run()
    w.patch(elapsed_s=19.5)
    before = w.get()
    kwargs = {"action_id": "r6-resume", "answers": copy.deepcopy(ANSWERS),
              "note": "All missing synthetic observations explicitly supplied."}
    task = w.engine.resume(w.principal, w.task_id, **kwargs)
    assert task["status"] == "created"
    p = task["payload"]
    for key in ("calls", "tools", "tokens", "elapsed_s"):
        assert p[key] == before["payload"][key]
    assert p["candidate"] is None and p["pending_action"] is None
    assert p["evidence"] == {} and p["awaiting_fields"] == []
    assert p["scenario"] == before["payload"]["scenario"]
    source = w.service.get_snapshot(w.principal, w.snapshot_id)
    assert source["version"] == source["payload"]["revision"] == 2
    assert source["payload"]["observed_at_ms"] == NOW
    assert p["source_binding"] != before["payload"]["source_binding"]
    assert p["source_binding"]["observed_at_ms"] == NOW
    assert len(w.provider.contexts) == 2
    assert w.engine.resume(w.principal, w.task_id, **kwargs) == task
    error("idempotency_conflict", w.engine.resume, w.principal, w.task_id,
          **{**kwargs, "note": "Changed note must not replay."})
    completed = w.run()
    assert completed["status"] == "submitted"
    assert completed["payload"]["calls"] == completed["payload"]["tools"] == 10
    assert completed["payload"]["tokens"] == 1280
    assert completed["payload"]["elapsed_s"] == 19.5
    assert all(record["payload"]["source_binding"] == p["source_binding"]
               for record in completed["payload"]["evidence"].values())
    assert_no_publication(w)


@pytest.mark.parametrize("attack", [
    "missing", "duplicate", "extra", "known", "bool", "float", "negative", "timestamp",
    "extra-answer-key", "blank-note",
])
def test_bad_human_answers_are_atomic_not_silent_coercions(world, attack):
    w = world("missing")
    before = w.run()
    answers = copy.deepcopy(ANSWERS)
    note = "Only exact missing integers may be supplied."
    if attack == "missing":
        answers.pop()
    elif attack == "duplicate":
        answers[1] = copy.deepcopy(answers[0])
    elif attack == "extra":
        answers.append({"field": "orders[1].due_min", "value": 2})
    elif attack == "known":
        answers[1]["field"] = "orders[1].due_min"
    elif attack in {"bool", "float", "negative"}:
        answers[0]["value"] = {"bool": True, "float": 0.0, "negative": -1}[attack]
    elif attack == "timestamp":
        answers[0] = {"field": "observed_at_ms", "value": NOW + 1}
    elif attack == "extra-answer-key":
        answers[0]["source"] = "model"
    else:
        note = "  "
    with pytest.raises(DomainError):
        w.engine.resume(w.principal, w.task_id, action_id="r6-bad-resume",
                        answers=answers, note=note)
    assert w.get() == before
    assert w.service.get_snapshot(w.principal, w.snapshot_id)["payload"] == (
        w.snapshot.model_dump(mode="json")
    )
    assert len(w.provider.contexts) == 2


def test_stale_missing_answers_cannot_refresh_observation(world):
    w = world("missing")
    before = w.run()
    w.clock.ms += 86_400_001
    error("source_stale", w.engine.resume, w.principal, w.task_id,
          action_id="r6-stale-resume", answers=ANSWERS, note="Original source is now stale.")
    assert w.get() == before
    assert w.service.get_snapshot(w.principal, w.snapshot_id)["payload"]["observed_at_ms"] == NOW


@pytest.mark.parametrize("tool", FIRST_SEVEN)
def test_all_first_seven_tools_reject_arguments_without_stripping_or_dispatch(world, tool):
    def attack(_context, reply):
        if reply.action["tool"] == tool:
            return unvalidated_reply({"tool": tool, "arguments": {"override": True}})
        return reply

    w = world(provider=ChoiceProvider(attack))
    task = w.run()
    assert task["status"] == "blocked"
    assert task["payload"]["error"] == "invalid_action"
    assert tool not in task["payload"]["evidence"]
    assert task["payload"]["tokens"] == 128 * (len(w.rows()) - 1) + 37
    assert w.rows()[-1]["status"] == "completed"
    assert task["payload"]["tools"] == len(w.rows()) - 1
    assert_no_recommendation(w)


@pytest.mark.parametrize("tool,arguments", [
    ("solve_schedule", {"schedule": []}),
    ("build_scenario", {"scenario": {"kind": "baseline"}}),
    ("compile_problem", {"objective": {"weighted_tardiness": 0}}),
    ("get_snapshot", {"snapshot_id": "other-owner-object"}),
    ("get_snapshot", {"url": "http://127.0.0.1:1/source"}),
    ("get_snapshot", {"owner": "other", "role": "admin"}),
    ("publish_schedule", {}),
    ("execute_synthetic", {"confirmed": True}),
    ("request_publication", {}),
    ("python", {"code": "pass"}),
    ("sql", {"query": "SELECT 1"}),
    ("submit_schedule", {"candidate_hash": "0" * 64, "evidence_ids": ["forged"],
                         "explanation": "skip all trusted tools"}),
])
def test_model_schedules_objects_urls_weights_and_publishers_cannot_bypass_tools(
    world, tool, arguments,
):
    w = world(provider=ChoiceProvider(lambda _context, _reply: unvalidated_reply({
        "tool": tool, "arguments": arguments,
    })))
    task = w.run()
    assert task["status"] == "blocked"
    assert task["payload"]["tools"] == 0
    assert task["payload"]["evidence"] == {} and task["payload"]["candidate"] is None
    assert task["payload"]["tokens"] == 37 and task["payload"]["calls"] == 1
    assert w.rows()[0]["status"] == "completed"
    assert_no_recommendation(w)


@pytest.mark.parametrize("forgery", ["missing", "extra", "duplicate", "foreign", "candidate",
                                     "schedule", "explanation-type"])
def test_submit_requires_exact_candidate_hash_all_issued_ids_and_schema(world, forgery):
    foreign = None
    if forgery == "foreign":
        other = world()
        foreign = other.run()["payload"]["evidence"]["get_snapshot"]["evidence_id"]

    def attack(_context, reply):
        if reply.action["tool"] != "submit_schedule":
            return reply
        action = copy.deepcopy(reply.action)
        args = action["arguments"]
        if forgery == "missing":
            args["evidence_ids"].pop()
        elif forgery == "extra":
            args["evidence_ids"].append("ev-forged")
        elif forgery == "duplicate":
            args["evidence_ids"][-1] = args["evidence_ids"][0]
        elif forgery == "foreign":
            args["evidence_ids"][0] = foreign
        elif forgery == "candidate":
            args["candidate_hash"] = "0" * 64
        elif forgery == "schedule":
            args["schedule"] = {"slots": []}
        else:
            args["explanation"] = {"approved": True}
        return unvalidated_reply(action, 128)

    w = world(provider=ChoiceProvider(attack))
    task = w.run()
    assert task["status"] == "blocked"
    assert task["payload"]["tools"] == 7
    assert task["payload"]["tokens"] == 1024
    assert all(row["status"] == "completed" for row in w.rows())
    assert_no_recommendation(w)


@pytest.mark.parametrize("fields", [[], ["materials[0].initial_stock"],
                                     ["materials[0].initial_stock"] * 2,
                                     ["materials[0].initial_stock", "orders[1].due_min"]])
def test_model_question_must_name_every_and_only_current_missing_field(world, fields):
    def attack(_context, reply):
        if reply.action["tool"] == "request_human_input":
            return unvalidated_reply({"tool": "request_human_input", "arguments": {
                "question": "TEST exact missing values only", "fields": fields,
            }}, 128)
        return reply

    w = world("missing", provider=ChoiceProvider(attack))
    task = w.run()
    assert task["status"] == "blocked" and task["payload"]["tools"] == 1
    assert task["payload"]["tokens"] == 256
    assert_no_recommendation(w)


@pytest.mark.parametrize("change", ["revision", "stale", "future", "scenario", "candidate",
                                    "evidence"])
def test_source_and_durable_facts_rechecked_after_model_reply_before_submission(world, change):
    w = None

    def tamper(_context, reply):
        if reply.action["tool"] != "submit_schedule":
            return reply
        if change == "revision":
            w.revise_source()
        elif change == "stale":
            w.clock.ms += 86_400_001
        elif change == "future":
            w.clock.ms -= 1
        elif change == "scenario":
            w.patch(scenario=demo_scenarios(w.snapshot)["supply_delay"].model_dump(mode="json"))
        elif change == "candidate":
            candidate = copy.deepcopy(w.get()["payload"]["candidate"])
            candidate["plan"]["slots"][0]["end_min"] += 1
            candidate["plan_hash"] = canonical_digest(candidate["plan"])
            w.patch(candidate=candidate)
            action = copy.deepcopy(reply.action)
            action["arguments"]["candidate_hash"] = candidate["plan_hash"]
            return unvalidated_reply(action, 128)
        else:
            evidence = copy.deepcopy(w.get()["payload"]["evidence"])
            record = evidence["simulate_schedule"]
            record["payload"]["simulations"] = {}
            record["hash"] = canonical_digest(record["payload"])
            record["evidence_id"] = "ev-" + canonical_digest({
                "tool": record["tool"], "payload": record["payload"],
            })
            w.patch(evidence=evidence)
        return reply

    w = world(provider=ChoiceProvider(tamper))
    task = w.run()
    assert task["status"] == "blocked"
    assert task["payload"]["tokens"] == 1024
    assert task["payload"]["tools"] == 7
    assert_no_recommendation(w)


@pytest.mark.parametrize("change,code", [("revision", "source_changed"),
                                         ("stale", "source_stale"),
                                         ("future", "source_future")])
def test_source_changed_stale_or_future_rejected_before_any_model_dispatch(world, change, code):
    w = world()
    if change == "revision":
        w.revise_source()
    elif change == "stale":
        w.clock.ms += 86_400_001
    else:
        w.clock.ms -= 1
    assert_run_rejected(w, code)
    assert w.provider.contexts == [] and w.rows() == []


def test_create_and_cancel_action_receipts_bind_target_scenario_and_owner(world):
    w = world()
    before = w.get()
    assert w.engine.create(w.principal, w.snapshot_id, scenario=w.scenario,
                           action_id="r6-create") == before
    error("idempotency_conflict", w.engine.create, w.principal, w.snapshot_id,
          scenario=demo_scenarios(w.snapshot)["supply_delay"], action_id="r6-create")
    other_task = w.engine.create(w.principal, w.snapshot_id, scenario=w.scenario,
                                 action_id="r6-other-task")
    assert other_task["object_id"] != w.task_id
    cancelled = w.engine.cancel(w.principal, w.task_id, action_id="r6-cancel")
    assert w.engine.cancel(w.principal, w.task_id, action_id="r6-cancel") == cancelled
    error("idempotency_conflict", w.engine.cancel, w.principal, other_task["object_id"],
          action_id="r6-cancel")
    assert w.run()["status"] == "cancelled" and w.provider.contexts == []
    assert_no_recommendation(w)


@pytest.mark.parametrize("operation", ["get", "run", "cancel", "resume", "reconcile", "create"])
def test_even_admin_cannot_use_another_owners_snapshot_task_or_receipt(world, operation):
    w = world()
    other = w.store.create_user("r6-other", "r6-other-inert-token-" + "y" * 32,
                                "admin", [w.snapshot.plant_id])
    fn = getattr(w.engine, operation)
    kwargs = {}
    target = w.task_id
    if operation == "create":
        target = w.snapshot_id
        kwargs = {"scenario": w.scenario, "action_id": "r6-create"}
    elif operation == "cancel":
        kwargs = {"action_id": "r6-other-cancel"}
    elif operation == "resume":
        kwargs = {"action_id": "r6-other-resume", "answers": ANSWERS, "note": "foreign"}
    elif operation == "reconcile":
        kwargs = {"action_id": "r6-other-reconcile", "call_id": "foreign", "tokens": 0,
                  "note": "foreign", "no_result": True}
    with pytest.raises(DomainError) as caught:
        fn(other, target, **kwargs)
    assert caught.value.status in {403, 404}
    assert w.provider.contexts == [] and w.rows() == []


@pytest.mark.parametrize("change", ["active", "plants", "role"])
def test_action_receipt_replay_still_requires_current_authorization(world, change):
    w = world()
    w.engine.cancel(w.principal, w.task_id, action_id="r6-cancel")
    w.store.set_user(w.principal.user_id, **{
        "active": {"active": False}, "plants": {"plants": []}, "role": {"role": "operator"},
    }[change])
    with pytest.raises(DomainError) as caught:
        w.engine.cancel(w.principal, w.task_id, action_id="r6-cancel")
    assert caught.value.status in {401, 403, 404}
    assert w.provider.contexts == []


def test_documented_budget_constants():
    assert (module.MAX_CALLS, module.MAX_TOOLS, module.MAX_TOKENS) == (16, 14, 40_000)
    assert (module.MAX_SECONDS, module.LEASE_SECONDS) == (600.0, 1200.0)


@pytest.mark.parametrize("budget", ["calls", "tools", "tokens", "time"])
def test_existing_budget_exhaustion_never_dispatches_or_resets_on_restart(world, budget):
    w = world()
    if budget == "calls":
        for index in range(module.MAX_CALLS):
            w.journal(status="failed", tokens=0, call_id=f"call-budget-{index}")
    elif budget == "tokens":
        w.journal(status="failed", tokens=module.MAX_TOKENS)
    elif budget == "tools":
        w.patch(tools=module.MAX_TOOLS)
    else:
        w.patch(elapsed_s=module.MAX_SECONDS)
    count = len(w.rows())
    w.restart()
    assert_run_rejected(w, "budget_exhausted")
    assert w.provider.contexts == [] and len(w.rows()) == count


@pytest.mark.parametrize("budget", ["tokens", "time"])
def test_budget_crossed_in_model_call_bills_reply_without_dispatching_tool(world, budget):
    w = None

    def exceed(_context, reply):
        if budget == "time":
            w.clock.seconds += module.MAX_SECONDS + 0.25
            return ProviderReply(reply.action, 43)
        return ProviderReply(reply.action, module.MAX_TOKENS)

    w = world(provider=ChoiceProvider(exceed))
    task = w.run()
    assert task["status"] == "blocked" and task["payload"]["error"] == "budget_exhausted"
    assert task["payload"]["tools"] == 0
    assert task["payload"]["tokens"] == (43 if budget == "time" else module.MAX_TOKENS)
    assert w.rows()[0]["status"] == "completed"
    if budget == "time":
        assert task["payload"]["elapsed_s"] == module.MAX_SECONDS + 0.25
        assert w.rows()[0]["elapsed_s"] == module.MAX_SECONDS + 0.25
    assert_no_recommendation(w)


@pytest.mark.parametrize("code,tokens,sent,status", [
    ("provider_unavailable", None, False, "failed"),
    ("provider_invalid", 411, True, "failed"),
    ("provider_invalid", 0, True, "failed"),
    ("provider_unknown", None, True, "unknown"),
    ("provider_unknown", 79, True, "unknown"),
    ("provider_unknown", 0, True, "unknown"),
])
def test_provider_faults_account_known_usage_separately_from_unknown_outcomes(
    world, code, tokens, sent, status,
):
    def fault(_context, _reply):
        raise ProviderError(code, "TEST injected provider failure", tokens=tokens, sent=sent)

    w = world(provider=ChoiceProvider(fault))
    task = w.run()
    assert task["status"] == "blocked"
    assert len(w.rows()) == 1
    row = w.rows()[0]
    assert row["status"] == status
    assert row["tokens"] == (0 if not sent else tokens)
    assert row["response_hash"] is None
    assert task["payload"]["tokens"] == (tokens or 0)
    assert task["payload"]["unknown_calls"] == (status == "unknown")
    assert task["payload"]["tools"] == 0 and task["payload"]["pending_action"] is None
    if status == "unknown":
        w.restart()
        assert w.run()["status"] == "blocked"
        assert len(w.provider.contexts) == len(w.rows()) == 1
    assert_no_recommendation(w)


@pytest.mark.parametrize("tokens", [None, True, -1, 100001, float("nan"), "128"])
def test_untrusted_usage_cannot_become_free_success(world, tokens):
    def corrupt(_context, reply):
        object.__setattr__(reply, "tokens", tokens)
        return reply

    w = world(provider=ChoiceProvider(corrupt))
    task = w.run()
    assert task["status"] == "blocked"
    assert task["payload"]["unknown_calls"] == 1 and task["payload"]["tools"] == 0
    assert w.rows()[0]["status"] == "unknown"
    assert w.rows()[0]["tokens"] is None and w.rows()[0]["response_hash"] is None
    assert_no_recommendation(w)


def test_actual_solver_unknown_is_not_called_proven_infeasible(world, monkeypatch):
    from scheduling_agent import solver

    actual_solve = solver.solve
    calls = []

    def zero_limit_real_cp_sat(problem, **kwargs):
        # Only the time limit changes: UNKNOWN comes from real CP-SAT, not a stub.
        kwargs["time_limit_s"] = 0.0
        result = actual_solve(problem, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(solver, "solve", zero_limit_real_cp_sat)
    w = world()
    task = w.run()
    assert len(calls) == 2 and all(result.status == "UNKNOWN" for result in calls)
    assert task["status"] == "no_schedule"
    assert task["payload"]["no_schedule_reason"] == "search_unknown"
    assert task["payload"]["candidate"] is None
    assert all(result.plan is None for result in calls)
    assert_no_recommendation(w)


@pytest.mark.parametrize("failed_mode", ["both", "feasibility", "optimize"])
def test_zero_candidate_solver_error_is_distinct_from_unknown_or_infeasible(
    world, monkeypatch, failed_mode,
):
    from scheduling_agent import solver

    actual_solve = solver.solve
    actual_backend = solver.cp_model.CpSolver.solve
    current_mode = None
    backend_calls = []

    def backend_fault_or_actual(solver_instance, model, *args, **kwargs):
        backend_calls.append(current_mode)
        if failed_mode == "both" or current_mode == failed_mode:
            # Exercise the real solver's exception -> ERROR conversion, not a
            # provider-supplied status or a fabricated service/SolveResult stub.
            raise RuntimeError("TEST injected local CP-SAT backend failure")
        return actual_backend(solver_instance, model, *args, **kwargs)

    def bounded_actual_solve(problem, **kwargs):
        nonlocal current_mode
        current_mode = kwargs["mode"]
        if failed_mode != "both" and current_mode != failed_mode:
            # The other candidate is genuinely UNKNOWN from a zero-limit search.
            kwargs["time_limit_s"] = 0.0
        return actual_solve(problem, **kwargs)

    monkeypatch.setattr(solver.cp_model.CpSolver, "solve", backend_fault_or_actual)
    monkeypatch.setattr(solver, "solve", bounded_actual_solve)
    w = world()
    task = w.run()
    p = task["payload"]
    assert backend_calls == ["feasibility", "optimize"]
    assert task["status"] == "no_schedule"
    assert p["error"] == p["no_schedule_reason"] == "solver_error"
    assert p["candidate"] is None and p["pending_action"] is None
    assert p["calls"] == p["tools"] == 4 and p["tokens"] == 512
    assert set(p["evidence"]) == set(FIRST_SEVEN[:4])
    results = p["evidence"]["solve_schedule"]["payload"]
    for label, mode in (("baseline", "feasibility"), ("optimized", "optimize")):
        result = results[label]
        assert result["mode"] == mode and result["plan"] is None
        expected = "ERROR" if failed_mode in {"both", mode} else "UNKNOWN"
        assert result["status"] == expected
        if expected == "ERROR":
            assert "TEST injected local CP-SAT backend failure" in result["error"]
        else:
            assert result["error"] is None
    w.restart()
    assert w.run()["payload"]["no_schedule_reason"] == "solver_error"
    assert backend_calls == ["feasibility", "optimize"]
    assert len(w.provider.contexts) == len(w.rows()) == 4
    assert_no_recommendation(w)


def test_provider_change_cannot_silently_fallback_or_reuse_issued_work(world):
    w = world()
    replacement = ChoiceProvider()
    replacement._fingerprint = "different-explicit-test-provider"
    w.restart(provider=replacement)
    error("provider_changed", w.run)
    assert replacement.contexts == [] and w.rows() == []
    assert_no_recommendation(w)


def test_resume_keeps_exhausted_call_budget_and_never_auto_runs(world):
    w = world("missing")
    waiting = w.run()
    for index in range(16 - waiting["payload"]["calls"]):
        w.journal(status="failed", tokens=0, call_id=f"call-before-resume-{index}")
    w.patch(elapsed_s=590.0)
    task = w.engine.resume(w.principal, w.task_id, action_id="r6-resume-capped",
                           answers=ANSWERS, note="Missing values do not reset old accounting.")
    assert task["payload"]["calls"] == 16
    assert task["payload"]["elapsed_s"] == 590.0 and task["payload"]["tokens"] == 256
    assert len(w.provider.contexts) == 2
    assert_run_rejected(w, "budget_exhausted")
    assert len(w.rows()) == 16 and len(w.provider.contexts) == 2


def test_known_failure_requires_explicit_run_and_never_switches_provider(world):
    failed = False

    def fail_once(_context, reply):
        nonlocal failed
        if not failed:
            failed = True
            raise ProviderError("provider_invalid", "TEST known failure", tokens=411)
        return reply

    w = world(provider=ChoiceProvider(fail_once))
    assert w.run()["status"] == "blocked"
    assert len(w.rows()) == len(w.provider.contexts) == 1
    assert_no_recommendation(w)
    w.restart()
    task = w.run()
    assert task["status"] == "submitted"
    assert task["payload"]["calls"] == 9
    assert task["payload"]["tokens"] == 411 + 8 * 128
    assert task["payload"]["provider_fingerprint"] == w.provider.fingerprint


def test_started_call_crash_becomes_unknown_no_automatic_model_retry(world):
    w = world()
    call_id = w.journal(status="started")
    w.patch(status="running", lease_id=CRASH_LEASE, lease_until_ms=w.clock.ms - 1)
    w.restart()
    task = w.run()
    assert task["status"] == "blocked" and task["payload"]["error"] == "model_unknown"
    assert task["payload"]["unknown_calls"] == 1
    assert w.rows()[0]["call_id"] == call_id and w.rows()[0]["status"] == "unknown"
    assert w.rows()[0]["tokens"] is None
    assert w.run()["status"] == "blocked"
    assert w.provider.contexts == [] and len(w.rows()) == 1
    assert_no_recommendation(w)


@pytest.mark.parametrize("with_pending", [False, True])
def test_completed_model_crash_replays_exact_action_once_without_rerun(world, with_pending):
    w = world()
    action = {"tool": "get_snapshot", "arguments": {}}
    call_id = w.journal(status="completed", tokens=37, elapsed=2.5, action=action)
    pending = {"call_id": call_id, "action": action} if with_pending else None
    w.patch(status="running", lease_id=CRASH_LEASE, lease_until_ms=w.clock.ms - 1,
            pending_action=pending)
    w.restart()
    task = w.run()
    assert task["status"] == "submitted"
    assert task["payload"]["calls"] == task["payload"]["tools"] == 8
    assert task["payload"]["tokens"] == 37 + 7 * 128
    assert task["payload"]["elapsed_s"] == 2.5
    assert len(w.provider.contexts) == 7
    assert w.provider.contexts[0]["available_tools"] == ["build_scenario"]
    assert task["payload"]["handled_call_ids"].count(call_id) == 1
    w.restart()
    assert w.run()["status"] == "submitted"
    assert len(w.rows()) == 8 and len(w.provider.contexts) == 7
    assert len(w.recommendations()) == 1


@pytest.mark.parametrize("attack", ["mismatch", "foreign", "handled", "unknown"])
def test_pending_action_cannot_borrow_unrelated_used_or_unmatched_journal(world, attack):
    w = world()
    action = {"tool": "get_snapshot", "arguments": {}}
    stored = {"tool": "get_snapshot", "arguments": {"forged": True}} if attack == "mismatch" else action
    call_id = w.journal(status="unknown" if attack == "unknown" else "completed", tokens=37,
                        action=None if attack == "unknown" else stored)
    if attack == "foreign":
        with w.store.transaction() as conn:
            conn.execute("UPDATE calls SET task_id=? WHERE call_id=?", ("other-task", call_id))
    changes = {"status": "running", "lease_id": CRASH_LEASE,
               "lease_until_ms": w.clock.ms - 1,
               "pending_action": {"call_id": call_id, "action": action}}
    if attack == "handled":
        changes["handled_call_ids"] = [call_id]
    w.patch(**changes)
    w.restart()
    task = w.run()
    assert task["status"] == "blocked" and task["payload"]["tools"] == 0
    assert w.provider.contexts == []
    assert_no_recommendation(w)


@pytest.mark.parametrize("tokens", [19, None])
def test_reconcile_no_result_is_idempotent_preserves_usage_and_never_invents_action(world, tokens):
    w = world()
    call_id = w.journal(status="unknown")
    w.patch(status="blocked", error="model_unknown")
    task = reconcile(w, call_id, tokens=tokens)
    assert task["status"] == "created"
    assert task["payload"]["pending_action"] is None and task["payload"]["evidence"] == {}
    row = w.rows()[0]
    assert row["status"] == "failed" and row["error_code"] == "reconciled_no_result"
    assert row["response_hash"] is None
    assert row["tokens"] == (module.MAX_TOKENS if tokens is None else tokens)
    assert task["payload"]["tokens"] == row["tokens"]
    assert task["payload"]["unknown_calls"] == 0
    assert reconcile(w, call_id, tokens=tokens) == task
    error("idempotency_conflict", reconcile, w, call_id, tokens=20)
    if tokens is None:
        assert_run_rejected(w, "budget_exhausted")
    assert w.provider.contexts == []
    assert_no_recommendation(w)


@pytest.mark.parametrize("field,value", [
    ("no_result", False), ("no_result", 1), ("tokens", True), ("tokens", -1),
    ("tokens", 100001), ("note", " "),
])
def test_reconcile_requires_explicit_bounded_no_result_attestation(world, field, value):
    w = world()
    call_id = w.journal(status="unknown", tokens=79)
    w.patch(status="blocked", error="model_unknown")
    kwargs = {"action_id": "r6-bad-reconcile", "call_id": call_id, "no_result": True,
              "tokens": 79, "note": "No synthetic response recovered."}
    kwargs[field] = value
    before = w.rows()
    with pytest.raises(DomainError):
        w.engine.reconcile(w.principal, w.task_id, **kwargs)
    assert w.rows() == before


def test_reconcile_cannot_reduce_known_usage_and_requires_current_admin(world):
    w = world()
    call_id = w.journal(status="unknown", tokens=79)
    w.patch(status="blocked", error="model_unknown")
    error("invalid_usage", reconcile, w, call_id, tokens=78)
    w.store.set_user(w.principal.user_id, role="operator")
    w.principal = w.store.authenticate(TOKEN)
    with pytest.raises(DomainError) as caught:
        reconcile(w, call_id, tokens=79)
    assert caught.value.status == 403
    assert w.rows()[0]["status"] == "unknown"


def test_model_wait_outside_db_transaction_allows_concurrent_cancel_and_billing(world):
    entered, release = threading.Event(), threading.Event()

    def pause(_context, reply):
        entered.set()
        assert release.wait(10), "TEST model gate was not released"
        return ProviderReply(reply.action, 57)

    w = world(provider=ChoiceProvider(pause))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(w.run)
        try:
            assert entered.wait(10), "No started model call"
            assert w.rows()[0]["status"] == "started"
            assert w.engine.cancel(w.principal, w.task_id, action_id="r6-inflight-cancel")[
                "status"
            ] == "cancelled"
            w.clock.seconds += 7.0
        finally:
            release.set()
        task = future.result(timeout=10)
    assert task["status"] == "cancelled"
    assert task["payload"]["tokens"] == 57 and task["payload"]["elapsed_s"] == 7.0
    assert task["payload"]["tools"] == 0 and task["payload"]["pending_action"] is None
    assert w.rows()[0]["status"] == "completed" and w.rows()[0]["tokens"] == 57
    assert_no_recommendation(w)


def test_revocation_while_model_waits_still_accounts_trustworthy_usage(world):
    w = None

    def revoke(_context, reply):
        w.store.set_user(w.principal.user_id, plants=[])
        return ProviderReply(reply.action, 73)

    w = world(provider=ChoiceProvider(revoke))
    with pytest.raises(DomainError) as caught:
        w.run()
    assert caught.value.status in {401, 403, 404}
    assert w.rows()[0]["status"] == "completed" and w.rows()[0]["tokens"] == 73
    w.store.set_user(w.principal.user_id, plants=[w.snapshot.plant_id])
    w.principal = w.store.authenticate(TOKEN)
    task = w.get()
    assert task["payload"]["tokens"] == 73
    assert task["payload"]["tools"] == 0 and task["payload"]["pending_action"] is None
    assert_no_recommendation(w)


@pytest.mark.parametrize("same_engine", [False, True])
def test_concurrent_run_lease_prevents_duplicate_model_dispatch(world, same_engine):
    entered, release = threading.Event(), threading.Event()

    def pause(_context, reply):
        if not entered.is_set():
            entered.set()
            assert release.wait(10)
        return reply

    w = world(provider=ChoiceProvider(pause))
    other = w.engine if same_engine else AgentEngine(
        w.service, w.provider, clock=w.clock.wall, monotonic=w.clock.monotonic,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(w.run)
        try:
            assert entered.wait(10)
            with pytest.raises(DomainError) as caught:
                other.run(w.principal, w.task_id)
            assert caught.value.code in {"provider_busy", "busy"}
            assert len(w.rows()) == len(w.provider.contexts) == 1
        finally:
            release.set()
        assert future.result(timeout=10)["status"] == "submitted"
    assert len(w.rows()) == len(w.provider.contexts) == 8
    assert len(w.recommendations()) == 1


@pytest.mark.parametrize("change", ["expiry", "replacement"])
def test_model_reply_after_lease_loss_is_billed_but_cannot_dispatch_or_clear_new_lease(world, change):
    w = None

    def lose_lease(_context, reply):
        if change == "expiry":
            w.clock.ms += int(module.LEASE_SECONDS * 1000) + 1
        else:
            w.patch(lease_id="lease-replacement", lease_until_ms=w.clock.ms + 1_200_000)
        return ProviderReply(reply.action, 67)

    w = world(provider=ChoiceProvider(lose_lease))
    task = w.run()
    if change == "expiry":
        assert task["status"] == "blocked" and task["payload"]["error"] == "lease_expired"
    else:
        assert task["payload"]["lease_id"] == "lease-replacement"
    assert task["payload"]["tokens"] == 67 and task["payload"]["tools"] == 0
    assert w.rows()[0]["status"] == "completed"
    assert_no_recommendation(w)


@pytest.mark.parametrize("change", ["cancel", "revision"])
def test_real_solve_outside_db_transaction_allows_concurrent_change_and_fences_persist(
    world, monkeypatch, change,
):
    from scheduling_agent import solver

    entered, release = threading.Event(), threading.Event()
    actual_solve = solver.solve
    invocations = []
    w = world()

    def gated_real_solve(problem, **kwargs):
        invocations.append(kwargs.copy())
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "Real solve gate was not released"
            w.clock.seconds += 23.0
        return actual_solve(problem, **kwargs)

    monkeypatch.setattr(solver, "solve", gated_real_solve)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(w.run)
        try:
            assert entered.wait(10), "Agent did not reach real CP-SAT"
            # A second SQLite writer MUST commit while the solve is in flight.
            if change == "cancel":
                assert w.engine.cancel(w.principal, w.task_id, action_id="r6-solve-cancel")[
                    "status"
                ] == "cancelled"
            else:
                assert w.revise_source()["version"] == 2
        finally:
            release.set()
        task = future.result(timeout=10)
    assert [call["mode"] for call in invocations] == ["feasibility", "optimize"]
    assert all(call["time_limit_s"] <= 5.0 for call in invocations)
    assert task["status"] == ("cancelled" if change == "cancel" else "blocked")
    if change == "revision":
        assert task["payload"]["error"] == "source_changed"
    assert set(task["payload"]["evidence"]) == set(FIRST_SEVEN[:3])
    assert task["payload"]["tools"] == 3
    assert task["payload"]["calls"] == 4 and task["payload"]["tokens"] == 512
    assert task["payload"]["elapsed_s"] == 23.0
    assert w.rows()[-1]["call_id"] not in task["payload"]["handled_call_ids"]
    assert task["payload"]["pending_action"] is None
    assert_no_recommendation(w)


@pytest.mark.parametrize("limit", ["lease", "time"])
def test_real_compute_expiry_cannot_persist_or_submit_and_elapsed_is_charged(world, monkeypatch, limit):
    w = world()
    actual_compute = w.service.compute_tool
    computed = []

    def timed_real_compute(prepared, tool):
        result = actual_compute(prepared, tool)
        if tool == "solve_schedule":
            computed.append(result)
            w.clock.seconds += 601.0 if limit == "time" else 31.0
            if limit == "lease":
                w.clock.ms += 1_200_001
        return result

    monkeypatch.setattr(w.service, "compute_tool", timed_real_compute)
    task = w.run()
    assert computed and computed[0]["baseline"]["plan"] is not None
    assert task["status"] == "blocked"
    assert task["payload"]["error"] == ("budget_exhausted" if limit == "time" else "lease_expired")
    assert set(task["payload"]["evidence"]) == set(FIRST_SEVEN[:3])
    assert task["payload"]["elapsed_s"] == (601.0 if limit == "time" else 31.0)
    assert task["payload"]["tokens"] == 512 and task["payload"]["tools"] == 3
    assert_no_recommendation(w)


@pytest.mark.parametrize("normalize_via_run", [False, True])
def test_cancelled_started_call_normalizes_after_expiry_and_reconcile_never_revives(
    world, normalize_via_run,
):
    w = world()
    call_id = w.journal(status="started")
    w.patch(status="running", lease_id=CRASH_LEASE,
            lease_until_ms=w.clock.ms + int(module.LEASE_SECONDS * 1000))
    assert w.engine.cancel(w.principal, w.task_id, action_id="r6-cancel-crashed")[
        "status"
    ] == "cancelled"
    error("busy", reconcile, w, call_id, tokens=41)
    assert w.rows()[0]["status"] == "started"
    w.clock.ms += int(module.LEASE_SECONDS * 1000) + 1
    w.restart()
    if normalize_via_run:
        task = w.run()
        assert task["status"] == "cancelled" and task["payload"]["unknown_calls"] == 1
        assert task["payload"]["lease_id"] is None
        assert w.rows()[0]["status"] == "unknown"
    settled = reconcile(w, call_id, tokens=41)
    assert settled["status"] == "cancelled" and settled["payload"]["unknown_calls"] == 0
    assert settled["payload"]["tokens"] == 41
    assert settled["payload"]["pending_action"] is None and settled["payload"]["lease_id"] is None
    assert w.rows()[0]["status"] == "failed"
    assert w.rows()[0]["error_code"] == "reconciled_no_result"
    assert reconcile(w, call_id, tokens=41) == settled
    assert w.run()["status"] == "cancelled"
    assert w.provider.contexts == []
    assert_no_recommendation(w)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("settled_tokens,late_tokens,settled_elapsed,late_elapsed", [
    (29, 83, 8.0, 12.0), (83, 29, 12.0, 8.0),
])
def test_late_reply_after_manual_no_result_reconciliation_only_increases_accounting(
    world, cancelled, settled_tokens, late_tokens, settled_elapsed, late_elapsed,
):
    entered, release = threading.Event(), threading.Event()

    def pause(_context, reply):
        entered.set()
        assert release.wait(10)
        return ProviderReply(reply.action, late_tokens)

    w = world(provider=ChoiceProvider(pause))
    recovery = AgentEngine(w.service, w.provider, clock=w.clock.wall,
                           monotonic=w.clock.monotonic)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(w.run)
        try:
            assert entered.wait(10)
            call_id = w.rows()[0]["call_id"]
            w.clock.ms += 1_200_001
            assert recovery.run(w.principal, w.task_id)["status"] == "blocked"
            assert w.rows()[0]["status"] == "unknown"
            if cancelled:
                recovery.cancel(w.principal, w.task_id, action_id="r6-cancel-before-settle")
            with w.store.transaction() as conn:
                conn.execute("UPDATE calls SET elapsed_s=? WHERE call_id=?",
                             (settled_elapsed, call_id))
            settled = reconcile(w, call_id, tokens=settled_tokens)
            assert settled["status"] == ("cancelled" if cancelled else "created")
            w.clock.seconds += late_elapsed
        finally:
            release.set()
        task = future.result(timeout=10)
    row = w.rows()[0]
    assert row["status"] == "failed" and row["error_code"] == "reconciled_no_result"
    assert row["tokens"] == max(settled_tokens, late_tokens)
    assert row["elapsed_s"] == max(settled_elapsed, late_elapsed)
    assert row["response_hash"] is None
    assert task["status"] == ("cancelled" if cancelled else "created")
    assert task["payload"]["tokens"] == max(settled_tokens, late_tokens)
    assert task["payload"]["unknown_calls"] == task["payload"]["tools"] == 0
    assert task["payload"]["pending_action"] is None and task["payload"]["evidence"] == {}
    assert len(w.provider.contexts) == len(w.rows()) == 1
    w.restart()
    for _ in range(3):
        assert w.get()["payload"]["elapsed_s"] == max(settled_elapsed, late_elapsed)
    # Idempotency replays the original receipt, never reapplies its obsolete charge.
    assert reconcile(w, call_id, tokens=settled_tokens) == settled
    assert w.rows()[0] == row
    assert_no_recommendation(w)


@pytest.mark.parametrize("checkpoint", [30.0, 595.0])
def test_crash_recovery_adds_only_uncheckpointed_model_elapsed_once(world, checkpoint):
    w = world()
    w.journal(status="failed", tokens=11, elapsed=4.0, call_id="call-checkpointed")
    w.patch(elapsed_s=checkpoint, accounted_call_elapsed_s=4.0, status="running",
            lease_id=CRASH_LEASE, lease_until_ms=w.clock.ms - 1)
    action = {"tool": "get_snapshot", "arguments": {}}
    w.journal(status="completed", tokens=37, elapsed=9.0, action=action)
    w.restart()
    expected = checkpoint + 9.0
    for _ in range(3):
        assert w.get()["payload"]["elapsed_s"] == expected
    if expected >= module.MAX_SECONDS:
        assert_run_rejected(w, "budget_exhausted")
        assert w.provider.contexts == [] and len(w.rows()) == 2
    else:
        task = w.run()
        assert task["status"] == "submitted" and task["payload"]["elapsed_s"] == expected
        assert task["payload"]["calls"] == 9
        assert task["payload"]["tokens"] == 11 + 37 + 7 * 128
        assert len(w.provider.contexts) == 7
        w.restart()
        assert w.run()["payload"]["elapsed_s"] == expected


def test_live_elapsed_checkpoint_does_not_double_count_call_time(world):
    w = None

    def advance(_context, reply):
        w.clock.seconds += 3.0
        return reply

    w = world(provider=ChoiceProvider(advance))
    w.patch(elapsed_s=10.0, accounted_call_elapsed_s=0.0)
    task = w.run()
    assert task["status"] == "submitted"
    assert sum(row["elapsed_s"] for row in w.rows()) == 24.0
    assert task["payload"]["elapsed_s"] == 34.0
    w.restart()
    for _ in range(3):
        assert w.get()["payload"]["elapsed_s"] == 34.0
    assert w.run()["payload"]["elapsed_s"] == 34.0
    assert len(w.provider.contexts) == 8


def settled_old_call(w):
    call_id = w.journal(status="unknown", tokens=11)
    w.patch(status="blocked")
    settled = reconcile(w, call_id, tokens=11)
    return call_id, settled


def report_late(w, engine, call_id, *, tokens=11, elapsed=30.0):
    engine._account(
        call_id, w.task_id, w.principal.user_id, CRASH_LEASE,
        status="completed", tokens=tokens, elapsed=elapsed,
        response={"action": {"tool": "get_snapshot", "arguments": {}}},
    )


def test_old_lease_elapsed_correction_adds_disjoint_new_model_and_real_tool_time(world, monkeypatch):
    w = old_engine = old_call_id = None
    corrected = False

    def late_elapsed(_context, reply):
        nonlocal corrected
        if not corrected:
            corrected = True
            w.clock.seconds += 10.0
            report_late(w, old_engine, old_call_id)
        return reply

    w = world(provider=ChoiceProvider(late_elapsed))
    old_call_id, _ = settled_old_call(w)
    w.patch(elapsed_s=550.0, accounted_call_elapsed_s=0.0)
    old_engine = w.engine
    w.restart()
    actual_compute = w.service.compute_tool
    computed = []

    def timed_real_compute(prepared, tool):
        result = actual_compute(prepared, tool)
        if tool == "build_scenario":
            computed.append(result)
            w.clock.seconds += 40.0
        return result

    monkeypatch.setattr(w.service, "compute_tool", timed_real_compute)
    task = w.run()
    assert corrected is True and len(computed) == 1
    assert computed[0]["snapshot"] == w.snapshot.model_dump(mode="json")
    assert task["status"] == "blocked" and task["payload"]["error"] == "budget_exhausted"
    assert task["payload"]["elapsed_s"] == 630.0
    assert task["payload"]["calls"] == 3 and task["payload"]["tokens"] == 267
    assert task["payload"]["tools"] == 1 and set(task["payload"]["evidence"]) == {"get_snapshot"}
    assert [row["elapsed_s"] for row in w.rows()] == [30.0, 10.0, 0.0]
    assert w.rows()[0]["status"] == "failed" and w.rows()[0]["response_hash"] is None
    w.restart()
    assert w.get()["payload"]["elapsed_s"] == 630.0
    assert_run_rejected(w, "budget_exhausted")
    assert len(w.provider.contexts) == 2


@pytest.mark.parametrize("change", ["tokens", "source", "fence", "time"])
def test_last_predispatch_recheck_after_started_reservation_prevents_sending(world, change):
    w = world()
    old_call_id, _ = settled_old_call(w)
    old_engine = w.engine
    w.restart()
    injected = False

    def monotonic():
        nonlocal injected
        started = [row for row in w.rows() if row["status"] == "started"]
        if started and not injected:
            injected = True
            assert w.provider.contexts == []
            if change == "tokens":
                report_late(w, old_engine, old_call_id, tokens=40_000, elapsed=0.0)
            elif change == "source":
                w.revise_source()
            elif change == "fence":
                w.patch(lease_id="replacement-worker", lease_until_ms=w.clock.ms + 1_200_000)
            else:
                w.clock.seconds += 600.0
        return w.clock.seconds

    w.engine.monotonic = monotonic
    task = w.run()
    assert injected is True
    assert w.provider.contexts == []
    assert task["payload"]["tools"] == 0 and task["payload"]["pending_action"] is None
    assert task["payload"]["unknown_calls"] == 0
    if change == "fence":
        assert task["payload"]["lease_id"] == "replacement-worker"
    else:
        assert task["status"] == "blocked"
        assert task["payload"]["error"] == ("source_changed" if change == "source" else "budget_exhausted")
    rows = w.rows()
    assert len(rows) == 2
    assert rows[1]["status"] == "failed" and rows[1]["tokens"] == 0
    assert rows[1]["response_hash"] is None
    if change == "tokens":
        assert rows[0]["tokens"] == task["payload"]["tokens"] == 40_000
        assert rows[0]["status"] == "failed" and rows[0]["response_hash"] is None
    assert_no_recommendation(w)


def test_atomic_stop_elapsed_sample_and_save_include_late_old_lease_and_cleanup(world, monkeypatch):
    w = None

    def billed_failure(_context, _reply):
        w.clock.seconds += 10.0
        raise ProviderError("provider_invalid", "TEST billed failure before cleanup", tokens=37)

    class SlowCleanupProvider(SessionProvider):
        @contextmanager
        def session(self):
            try:
                with super().session():
                    yield self
            finally:
                w.clock.seconds += 40.0

    provider = SlowCleanupProvider(billed_failure)
    w = world(provider=provider)
    old_call_id, _ = settled_old_call(w)
    w.patch(elapsed_s=550.0, accounted_call_elapsed_s=0.0)
    old_engine = w.engine
    w.restart()
    actual_stop = w.engine._stop
    samples = []
    injections = []

    def inject_before_stop(principal, task_id, lease_id, code, elapsed=None):
        assert code == "provider_invalid" and w.clock.seconds == 150.0
        report_late(w, old_engine, old_call_id)
        injections.append(old_call_id)
        assert callable(elapsed), "Sampling outside the save transaction can lose late elapsed"
        actual_sample = elapsed

        def sample(conn):
            value = actual_sample(conn)
            old_duration = conn.execute("SELECT elapsed_s FROM calls WHERE call_id=?",
                                        (old_call_id,)).fetchone()[0]
            samples.append((conn.in_transaction, old_duration, value))
            return value

        return actual_stop(principal, task_id, lease_id, code, sample)

    monkeypatch.setattr(w.engine, "_stop", inject_before_stop)
    task = w.run()
    assert injections == [old_call_id]
    assert samples == [(True, 30.0, 630.0)]
    assert task["status"] == "blocked" and task["payload"]["error"] == "provider_invalid"
    assert task["payload"]["elapsed_s"] == 630.0
    assert task["payload"]["accounted_call_elapsed_s"] == 40.0
    assert task["payload"]["tokens"] == 48
    assert task["payload"]["tools"] == task["payload"]["unknown_calls"] == 0
    assert task["payload"]["pending_action"] is None
    assert [row["elapsed_s"] for row in w.rows()] == [30.0, 10.0]
    assert provider.sessions == provider.exits == 1
    w.restart()
    for _ in range(3):
        assert w.get()["payload"]["elapsed_s"] == 630.0
    assert_run_rejected(w, "budget_exhausted")
    assert len(w.provider.contexts) == 1


@pytest.mark.parametrize("identity", ["task_id", "owner", "lease_id"])
def test_late_reconciled_accounting_still_requires_original_journal_identity(world, identity):
    w = world()
    old_call_id, _ = settled_old_call(w)
    with w.store.transaction() as conn:
        # Column names are a fixed TEST parametrization, never caller-controlled SQL.
        conn.execute(f"UPDATE calls SET {identity}=? WHERE call_id=?",
                     ("replaced-identity", old_call_id))
        before = dict(conn.execute("SELECT * FROM calls WHERE call_id=?", (old_call_id,)).fetchone())
    error("call_fence", report_late, w, w.engine, old_call_id, tokens=97, elapsed=20.0)
    with w.store.connect() as conn:
        after = dict(conn.execute("SELECT * FROM calls WHERE call_id=?", (old_call_id,)).fetchone())
    assert after == before
    assert after["tokens"] == 11 and after["response_hash"] is None
    assert w.provider.contexts == []
    assert_no_recommendation(w)


@pytest.mark.parametrize("seconds", [float("nan"), float("inf")])
def test_bad_monotonic_clock_never_creates_model_call(world, seconds):
    w = world()
    w.clock.seconds = seconds
    error("clock_invalid", w.run)
    assert w.provider.contexts == [] and w.rows() == []


@pytest.mark.parametrize("case", ["normal", "missing", "infeasible"])
def test_provider_session_once_per_graph_and_close_only_app_shutdown(world, case):
    provider = SessionProvider()
    w = world(case, provider=provider)
    task = w.run()
    assert task["status"] in {"submitted", "awaiting_input", "no_schedule"}
    assert provider.sessions == provider.exits == 1
    assert provider.in_session and all(provider.in_session)
    assert provider.closed_count == 0
    w.run()
    assert provider.closed_count == 0


def test_actual_langgraph_three_nodes_tracing_and_callbacks_explicitly_disabled(world, monkeypatch):
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.runnables import RunnableConfig
    from langchain_core.runnables.config import var_child_runnable_config
    from langgraph.graph.state import CompiledStateGraph
    from langsmith import tracing_context
    from langsmith.utils import tracing_is_enabled

    called = []

    class SpyCallback(BaseCallbackHandler):
        def on_chain_start(self, *_args, **_kwargs):
            called.append("chain-start")

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "r6-inert-test-value-not-a-credential")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "https://r6-invalid.example.invalid")

    def inspect_tracing(_context, reply):
        assert tracing_is_enabled() is False
        return reply

    w = world(provider=ChoiceProvider(inspect_tracing))
    observed = []
    actual_invoke = CompiledStateGraph.invoke

    def observed_actual_invoke(graph, *args, **kwargs):
        nodes = set(graph.get_graph().nodes) - {"__start__", "__end__"}
        observed.append(nodes)
        assert len(nodes) == 3
        assert not any("publish" in node or "execute" in node or "review" in node for node in nodes)
        assert kwargs["config"]["callbacks"] == []
        return actual_invoke(graph, *args, **kwargs)

    monkeypatch.setattr(CompiledStateGraph, "invoke", observed_actual_invoke)
    token = var_child_runnable_config.set(RunnableConfig(callbacks=[SpyCallback()]))
    try:
        with tracing_context(enabled=True):
            assert w.run()["status"] == "submitted"
    finally:
        var_child_runnable_config.reset(token)
    assert observed == [{"agent", "guard", "tools"}]
    assert called == [], "Ambient callbacks must not observe graph inputs or outputs"
    assert_no_publication(w)
