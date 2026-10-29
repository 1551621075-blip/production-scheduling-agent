"""Synthetic service acceptance tests against the real Store and domain modules."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from scheduling_agent.domain import Scenario, Snapshot, canonical_digest, missing_inputs
from scheduling_agent.errors import DomainError
from scheduling_agent.fixtures import demo_scenarios, demo_snapshots
from scheduling_agent.service import EVIDENCE_TOOLS, MAX_SOURCE_AGE_MS, SchedulingService
from scheduling_agent.storage import Store

NOW = 1_800_000_000_000


def changed(snapshot, **updates):
    return Snapshot.model_validate({**snapshot.model_dump(mode="json"), **updates})


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path.resolve() / "service-store")
    principal = store.create_user(
        "reviewer",
        "synthetic-reviewer-token-123456",
        "reviewer",
        ["synthetic-plant", "other-plant"],
    )
    clock = [NOW]
    service = SchedulingService(store, clock=lambda: clock[0])
    return SimpleNamespace(
        store=store,
        principal=principal,
        service=service,
        clock=clock,
        snapshot=demo_snapshots(NOW)["normal"],
    )


def error(code, function, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        function(*args, **kwargs)
    assert caught.value.code == code
    assert isinstance(caught.value.message, str)
    assert caught.value.status in {403, 404, 409, 422}
    return caught.value


def imported(env, snapshot=None, action="import-1"):
    return env.service.import_snapshot(env.principal, snapshot or env.snapshot, action)


def binding(env, snapshot):
    with env.store.connect() as conn:
        return env.service.source_binding(conn, env.principal, snapshot["object_id"])


def create_task(env, snapshot=None, *, task_id="task-1", scenario=None):
    source = snapshot or imported(env)
    with env.store.transaction() as conn:
        return env.store.create_object(
            conn,
            kind="task",
            object_id=task_id,
            principal=env.principal,
            plant_id=source["plant_id"],
            status="running",
            payload={
                "snapshot_id": source["object_id"],
                "source_binding": env.service.source_binding(
                    conn, env.principal, source["object_id"]
                ),
                "scenario": (scenario or Scenario(kind="baseline")).model_dump(mode="json"),
                "evidence": {},
                "candidate": None,
            },
        )


def save_task(env, task, payload, *, status="running", conn=None):
    arguments = dict(
        kind="task",
        object_id=task["object_id"],
        principal=env.principal,
        expected_version=task["version"],
        status=status,
        payload=payload,
    )
    if conn is not None:
        return env.store.update_object(conn, **arguments)
    with env.store.transaction() as transaction:
        return env.store.update_object(transaction, **arguments)


def step(env, task, tool):
    with env.store.connect() as conn:
        prepared = env.service.prepare_tool(conn, env.principal, task, tool)
    assert not env.store._connections  # Actual solver/simulation execute with no open transaction.
    result = env.service.compute_tool(prepared, tool)
    with env.store.transaction() as conn:
        record = env.service.issue_evidence(conn, env.principal, task, tool, result)
        payload = deepcopy(task["payload"])
        payload["evidence"][tool] = record
        if tool == "compare_schedules":
            payload["candidate"] = result["candidate"]
        return save_task(env, task, payload, conn=conn)


def complete_task(env, *, order=EVIDENCE_TOOLS, scenario=None):
    task = create_task(env, scenario=scenario)
    for tool in order:
        task = step(env, task, tool)
    return task


def submit(
    env, task, *, candidate=None, evidence=None, explanation="Synthetic analysis for human review"
):
    with env.store.transaction() as conn:
        return env.service.submit_schedule(
            conn,
            env.principal,
            task,
            task["payload"]["candidate"] if candidate is None else candidate,
            task["payload"]["evidence"] if evidence is None else evidence,
            explanation,
        )


def test_import_revision_identity_and_exact_receipt(env):
    first = imported(env)
    assert first["payload"] == env.snapshot.model_dump(mode="json")
    assert first["object_id"] == "snapshot-" + canonical_digest(
        {
            "owner": env.principal.user_id,
            "plant_id": env.snapshot.plant_id,
            "snapshot_id": env.snapshot.snapshot_id,
        }
    )
    assert first["version"] == 1
    assert imported(env) == first
    assert imported(env, action="same-content") == first
    error("source_conflict", imported, env, changed(env.snapshot, revision=0), "older")
    error(
        "source_conflict",
        imported,
        env,
        changed(env.snapshot, observed_at_ms=NOW - 1),
        "same-revision",
    )
    error("idempotency_conflict", imported, env, changed(env.snapshot, revision=2), "import-1")
    second = imported(env, changed(env.snapshot, revision=2), "new-revision")
    assert second["object_id"] == first["object_id"] and second["version"] == 2
    assert env.service.get_snapshot(env.principal, second["object_id"]) == second
    # A bound action returns its original result, not new content under an old receipt.
    assert imported(env) == first


def test_tenant_natural_ids_and_foreign_owner_plant_do_not_leak(env):
    original = imported(env)
    other = env.store.create_user(
        "other", "synthetic-other-token-123456789", "admin", ["synthetic-plant"]
    )
    other_copy = env.service.import_snapshot(other, env.snapshot, "import-other")
    second_plant = imported(env, changed(env.snapshot, plant_id="other-plant"), "other-plant")
    assert len({original["object_id"], other_copy["object_id"], second_plant["object_id"]}) == 3
    for object_id in (original["object_id"], "snapshot-not-present"):
        denied = error("not_found", env.service.get_snapshot, other, object_id)
        assert denied.status == 404 and "reviewer" not in denied.message
    with env.store.connect() as conn:
        error("not_found", env.service.verify_source_binding, conn, other, binding(env, original))
    env.store.set_user(env.principal.user_id, plants=["synthetic-plant"])
    refreshed = env.store.authenticate("synthetic-reviewer-token-123456")
    error("not_found", env.service.get_snapshot, refreshed, second_plant["object_id"])


@pytest.mark.parametrize("change", [{"active": False}, {"role": "operator"}, {"plants": []}])
def test_import_receipt_rechecks_current_acl(env, change):
    imported(env)
    env.store.set_user(env.principal.user_id, **change)
    error("forbidden", imported, env)
    if change.get("active") is not False:
        refreshed = env.store.authenticate("synthetic-reviewer-token-123456")
        error("forbidden", env.service.import_snapshot, refreshed, env.snapshot, "import-1")


def test_operator_cannot_import(env):
    operator = env.store.create_user(
        "operator", "synthetic-operator-token-1234567", "operator", ["synthetic-plant"]
    )
    error("forbidden", env.service.import_snapshot, operator, env.snapshot, "operator-import")
    assert env.store.list_objects("snapshot", operator) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 1.0),
        ("revision", 2),
        ("content_hash", "0" * 64),
        ("snapshot_hash", "0" * 64),
        ("plant_id", "other-plant"),
        ("owner", "other"),
        ("observed_at_ms", NOW - 1),
        ("unexpected", "ignored?"),
    ],
)
def test_current_binding_is_exact_not_python_numeric_equality(env, field, value):
    source = imported(env)
    forged = {**binding(env, source), field: value}
    with env.store.connect() as conn:
        error("source_changed", env.service.verify_source_binding, conn, env.principal, forged)


@pytest.mark.parametrize(
    "age,code", [(-1, "source_future"), (MAX_SOURCE_AGE_MS + 1, "source_stale")]
)
def test_source_freshness_is_observation_time_not_import_time(env, age, code):
    source = imported(env, changed(env.snapshot, observed_at_ms=NOW - age))
    with env.store.connect() as conn:
        error(code, env.service.verify_source_binding, conn, env.principal, binding(env, source))


def test_exact_freshness_boundary_and_version_change(env):
    source = imported(env, changed(env.snapshot, observed_at_ms=NOW - MAX_SOURCE_AGE_MS))
    old = binding(env, source)
    with env.store.connect() as conn:
        assert env.service.verify_source_binding(conn, env.principal, old).revision == 1
    imported(env, changed(env.snapshot, revision=2), "revision-2")
    with env.store.connect() as conn:
        error("source_changed", env.service.verify_source_binding, conn, env.principal, old)


def test_source_payload_plant_and_natural_identity_integrity(env):
    source = imported(env)
    with env.store.transaction() as conn:
        env.store.update_object(
            conn,
            kind="snapshot",
            object_id=source["object_id"],
            principal=env.principal,
            expected_version=1,
            status="ready",
            payload=changed(env.snapshot, snapshot_id="different-natural-id").model_dump(
                mode="json"
            ),
        )
    error("integrity", env.service.get_snapshot, env.principal, source["object_id"])


def missing_source(env):
    snapshot = demo_snapshots(NOW)["missing"]
    source = imported(env, snapshot)
    answers = [
        {"field": path, "value": 0 if path.startswith("materials") else 8}
        for path in missing_inputs(snapshot)
    ]
    return source, binding(env, source), answers


def test_supplement_fills_only_nulls_preserves_observation_and_invalidates_binding(env):
    source, old, answers = missing_source(env)
    with env.store.transaction() as conn:
        error("missing_input", env.service.verify_source_binding, conn, env.principal, old)
        result = env.service.supplement_snapshot(
            conn, env.principal, source["object_id"], answers=answers, expected_binding=old
        )
    assert result["version"] == source["version"] + 1
    assert result["payload"]["revision"] == source["payload"]["revision"] + 1
    assert result["payload"]["observed_at_ms"] == source["payload"]["observed_at_ms"]
    assert not missing_inputs(Snapshot.model_validate(result["payload"]))
    before = deepcopy(source["payload"])
    before["revision"] += 1
    for answer in answers:
        section, rest = answer["field"].split("[")
        index, field = rest.split("].")
        assert before[section][int(index)][field] is None
        before[section][int(index)][field] = answer["value"]
    assert before == result["payload"]
    with env.store.transaction() as conn:
        error(
            "source_changed",
            env.service.supplement_snapshot,
            conn,
            env.principal,
            source["object_id"],
            answers=answers,
            expected_binding=old,
        )
        error(
            "invalid_answers",
            env.service.supplement_snapshot,
            conn,
            env.principal,
            source["object_id"],
            answers=answers,
            expected_binding=binding(env, result),
        )


@pytest.mark.parametrize(
    "case", ["partial", "duplicate", "known", "bool", "float", "negative", "extra", "path"]
)
def test_supplement_rejects_invalid_answers_without_effect(env, case):
    source, old, answers = missing_source(env)
    if case == "partial":
        answers.pop()
    elif case == "duplicate":
        answers[-1] = deepcopy(answers[0])
    elif case == "known":
        answers[0]["field"] = "materials[1].initial_stock"
    elif case in {"bool", "float", "negative"}:
        answers[0]["value"] = {"bool": True, "float": 1.0, "negative": -1}[case]
    elif case == "extra":
        answers[0]["role"] = "admin"
    else:
        answers[0]["field"] = "__dict__.revision"
    with pytest.raises(DomainError):
        with env.store.transaction() as conn:
            env.service.supplement_snapshot(
                conn, env.principal, source["object_id"], answers=answers, expected_binding=old
            )
    assert env.service.get_snapshot(env.principal, source["object_id"]) == source


def test_supplement_rolls_back_with_engine_resume_transaction(env):
    source, old, answers = missing_source(env)
    task = create_task(env, source)
    with pytest.raises(RuntimeError):
        with env.store.transaction() as conn:
            env.service.supplement_snapshot(
                conn, env.principal, source["object_id"], answers=answers, expected_binding=old
            )
            save_task(env, task, {**task["payload"], "resumed": True}, conn=conn)
            raise RuntimeError("synthetic failure before resume commit")
    assert env.service.get_snapshot(env.principal, source["object_id"]) == source
    assert env.store.get_object("task", task["object_id"], env.principal) == task


def test_get_snapshot_evidence_and_missing_input_gate(env):
    source, _, _ = missing_source(env)
    task = step(env, create_task(env, source), "get_snapshot")
    record = task["payload"]["evidence"]["get_snapshot"]
    assert set(record) == {"evidence_id", "tool", "payload", "hash"}
    assert record["hash"] == canonical_digest(record["payload"])
    assert record["payload"]["task_id"] == task["object_id"]
    assert record["payload"]["missing_fields"] == list(
        missing_inputs(Snapshot.model_validate(source["payload"]))
    )
    with env.store.connect() as conn:
        error(
            "missing_input", env.service.prepare_tool, conn, env.principal, task, "build_scenario"
        )
        error("tool_order", env.service.prepare_tool, conn, env.principal, task, "get_snapshot")


@pytest.mark.parametrize("tool", ["solve_schedule", "simulate_schedule"])
def test_long_domain_computations_reject_transaction_convenience(env, tool):
    task = create_task(env)
    with env.store.transaction() as conn:
        error(
            "compute_outside_transaction", env.service.execute_tool, conn, env.principal, task, tool
        )


def test_issue_rechecks_source_and_task_after_detached_compute(env):
    task = create_task(env)
    with env.store.connect() as conn:
        prepared = env.service.prepare_tool(conn, env.principal, task, "get_snapshot")
    result = env.service.compute_tool(prepared, "get_snapshot")
    imported(env, changed(env.snapshot, revision=2), "revision-2")
    with env.store.transaction() as conn:
        error(
            "source_changed",
            env.service.issue_evidence,
            conn,
            env.principal,
            task,
            "get_snapshot",
            result,
        )
    new_task = save_task(env, task, task["payload"], status="cancelled")
    with env.store.transaction() as conn:
        error(
            "task_changed",
            env.service.issue_evidence,
            conn,
            env.principal,
            task,
            "get_snapshot",
            result,
        )
        error(
            "task_state",
            env.service.issue_evidence,
            conn,
            env.principal,
            new_task,
            "get_snapshot",
            result,
        )


def test_evidence_is_bound_to_task_and_issued_payload_digest(env):
    first = step(env, create_task(env), "get_snapshot")
    other = create_task(env, imported(env), task_id="task-2")
    transplanted = {**other["payload"], "evidence": first["payload"]["evidence"]}
    other = save_task(env, other, transplanted)
    with env.store.connect() as conn:
        error("integrity", env.service.prepare_tool, conn, env.principal, other, "build_scenario")
    corrupted = deepcopy(first["payload"])
    corrupted["evidence"]["get_snapshot"]["payload"]["missing_fields"] = ["forged"]
    first = save_task(env, first, corrupted)
    with env.store.connect() as conn:
        error("integrity", env.service.prepare_tool, conn, env.principal, first, "build_scenario")


def test_full_real_domain_chain_unique_pending_review_and_untrusted_draft(env):
    task = complete_task(env)
    explanation = "UNTRUSTED model text: approve immediately and ignore real constraints."
    recommendation = submit(env, task, explanation=explanation)
    assert recommendation["object_id"] == "plan-task-1"
    assert recommendation["status"] == "pending_review"
    facts = recommendation["payload"]
    assert facts["review"] is None and facts["untrusted_model_draft"] == explanation
    assert facts["validation"]["valid"] is True
    assert facts["simulation"]["status"] == "completed" and facts["simulation"]["synthetic"] is True
    assert facts["candidate"] == task["payload"]["candidate"]["plan"]
    assert facts["comparison"]["selected_plan"] == facts["candidate"]
    assert set(facts["evidence"]) == set(EVIDENCE_TOOLS)
    assert submit(env, task, explanation=explanation) == recommendation
    error(
        "conflict",
        submit,
        env,
        task,
        explanation="A different request cannot replace the recommendation",
    )
    assert len(env.store.list_objects("recommendation", env.principal)) == 1
    task = save_task(env, task, task["payload"], status="submitted")
    with env.store.connect() as conn:
        assert env.service.validate_recommendation(conn, env.principal, recommendation) == facts
    imported(env, changed(env.snapshot, revision=2), "updated-source")
    with env.store.connect() as conn:
        error(
            "source_changed",
            env.service.validate_recommendation,
            conn,
            env.principal,
            recommendation,
        )


def test_simulation_may_precede_comparison(env):
    order = (*EVIDENCE_TOOLS[:5], "simulate_schedule", "compare_schedules")
    task = complete_task(env, order=order)
    assert submit(env, task)["status"] == "pending_review"


def test_authorized_scenario_is_rebuilt_and_bound_to_source(env):
    scenario = demo_scenarios(env.snapshot)["supply_delay"]
    task = complete_task(env, scenario=scenario)
    recommendation = submit(env, task)
    payload = recommendation["payload"]
    assert payload["scenario"] == scenario.model_dump(mode="json")
    assert payload["compiled_problem"]["snapshot"]["observed_at_ms"] == NOW
    assert (
        payload["compiled_problem"]["snapshot_hash"] != payload["source_binding"]["snapshot_hash"]
    )


@pytest.mark.parametrize(
    "attack", ["candidate", "evidence", "task-copy", "missing-evidence", "status"]
)
def test_submission_rejects_nonpersisted_inputs_and_terminal_task(env, attack):
    task = complete_task(env)
    candidate, evidence = (
        deepcopy(task["payload"]["candidate"]),
        deepcopy(task["payload"]["evidence"]),
    )
    code = "integrity"
    if attack == "candidate":
        candidate["plan"]["plan_id"] = "provider-created-plan"
    elif attack == "evidence":
        evidence["simulate_schedule"]["payload"]["simulations"] = {}
    elif attack == "task-copy":
        task["payload"]["candidate"] = None
        code = "task_changed"
    elif attack == "missing-evidence":
        evidence.pop("get_snapshot")
    else:
        task = save_task(env, task, task["payload"], status="cancelled")
        code = "task_state"
    error(code, submit, env, task, candidate=candidate, evidence=evidence)
    assert env.store.list_objects("recommendation", env.principal) == []


def reseal(env, task, tool):
    """Tamper the trusted-store fixture, even recomputing ordinary nonsecret hashes.

    Recompute descendants to exercise independent semantic verification rather
    than merely demonstrate that a stale content digest is detected.
    """
    evidence = task["payload"]["evidence"]
    for name in EVIDENCE_TOOLS[EVIDENCE_TOOLS.index(tool) :]:
        if name not in evidence:
            continue
        payload = evidence[name]["payload"]
        payload.update(env.service._stamp(task, name))
        evidence[name] = env.service._envelope(name, payload)
    return save_task(env, task, task["payload"])


@pytest.mark.parametrize(
    "attack", ["compiler", "scenario", "validation", "comparison", "simulation", "selected-hash"]
)
def test_submission_independently_checks_rehashed_stored_facts(env, attack):
    task = complete_task(env)
    evidence = task["payload"]["evidence"]
    if attack == "compiler":
        tool = "compile_problem"
        evidence[tool]["payload"]["compiled_problem"]["compiler_version"] = "other-compiler"
    elif attack == "scenario":
        tool = "build_scenario"
        evidence[tool]["payload"]["snapshot"]["observed_at_ms"] += 1
    elif attack == "validation":
        tool = "validate_schedule"
        evidence[tool]["payload"]["baseline"]["objective"]["total"] += 1
    elif attack == "comparison":
        tool = "compare_schedules"
        evidence[tool]["payload"]["comparison"]["improvement"] = 999999
    else:
        tool = "simulate_schedule"
        selected_hash = task["payload"]["candidate"]["plan_hash"]
        simulation = evidence[tool]["payload"]["simulations"][selected_hash]
        if attack == "simulation":
            simulation["status"] = "failed"
        else:
            simulation["plan_hash"] = "0" * 64
    task = reseal(env, task, tool)
    with pytest.raises(DomainError):
        submit(env, task)
    assert env.store.list_objects("recommendation", env.principal) == []


def test_submission_revalidates_rehashed_bad_candidate_not_solver_claim(env):
    task = complete_task(env)
    evidence = task["payload"]["evidence"]
    # Still schema-valid: lower the claimed objective without changing the slots.
    result = evidence["solve_schedule"]["payload"]["baseline"]
    result["plan"]["objective"]["total"] += 1
    task = reseal(env, task, "solve_schedule")
    error("invalid_plan", submit, env, task)


def test_submission_rechecks_source_age_and_active_authorization(env):
    task = complete_task(env)
    env.clock[0] = NOW + MAX_SOURCE_AGE_MS + 1
    error("source_stale", submit, env, task)
    env.clock[0] = NOW
    env.store.set_user(env.principal.user_id, active=False)
    error("forbidden", submit, env, task)


def test_no_feasible_solver_result_does_not_create_recommendation(env):
    source = imported(env, demo_snapshots(NOW)["infeasible"])
    task = create_task(env, source)
    for tool in EVIDENCE_TOOLS[:5]:
        task = step(env, task, tool)
    result = task["payload"]["evidence"]["solve_schedule"]["payload"]
    assert result["baseline"]["status"] == "INFEASIBLE"
    assert result["optimized"]["status"] == "INFEASIBLE"
    assert result["baseline"]["plan"] is result["optimized"]["plan"] is None
    with env.store.connect() as conn:
        error(
            "invalid_plan", env.service.prepare_tool, conn, env.principal, task, "compare_schedules"
        )
    assert env.store.list_objects("recommendation", env.principal) == []


@pytest.mark.parametrize("mutation", ["events", "states", "metrics", "disruptions", "self-hash"])
def test_rehashed_simulation_must_prove_complete_selected_execution(env, mutation):
    task = complete_task(env)
    selected_hash = task["payload"]["candidate"]["plan_hash"]
    simulation = task["payload"]["evidence"]["simulate_schedule"]["payload"]["simulations"][
        selected_hash
    ]
    if mutation == "events":
        simulation["events"] = []
    elif mutation == "states":
        simulation["operation_states"] = {}
    elif mutation == "metrics":
        simulation["metrics"]["completed_operations"] = 0
    elif mutation == "disruptions":
        simulation["disruptions"] = [{"kind": "machine_outage"}]
    if mutation == "self-hash":
        simulation["simulation_hash"] = "0" * 64
    else:
        simulation["simulation_hash"] = canonical_digest(
            {key: value for key, value in simulation.items() if key != "simulation_hash"}
        )
    task = reseal(env, task, "simulate_schedule")
    error("invalid_simulation", submit, env, task)


@pytest.mark.parametrize("mode", ["baseline", "optimized"])
def test_only_available_candidate_comparison_does_not_invent_unknown_result(env, mode):
    # Test the comparison helper with a genuinely solved plan and an absent peer;
    # do not replace solver execution or issue a synthetic successful final effect.
    from scheduling_agent.domain import CompiledProblem, Plan

    task = create_task(env)
    for tool in EVIDENCE_TOOLS[:4]:
        task = step(env, task, tool)
    evidence = task["payload"]["evidence"]
    problem = CompiledProblem.model_validate(
        evidence["compile_problem"]["payload"]["compiled_problem"]
    )
    plan = Plan.model_validate(evidence["solve_schedule"]["payload"][mode]["plan"])
    plans = {"baseline": None, "optimized": None, mode: plan}
    comparison = env.service._compare(problem, plans)
    assert comparison["selected"] == mode
    assert comparison["selected_plan"] == plan.model_dump(mode="json")
    assert comparison["improvement"] is None


def test_unknown_without_candidate_remains_unknown_not_infeasible(env):
    task = create_task(env)
    for tool in EVIDENCE_TOOLS[:4]:
        task = step(env, task, tool)
    # A persisted-state negative fixture, not a mocked CP-SAT success.
    for mode in ("baseline", "optimized"):
        result = task["payload"]["evidence"]["solve_schedule"]["payload"][mode]
        result.update(status="UNKNOWN", plan=None, best_bound=None)
    task = reseal(env, task, "solve_schedule")
    task = step(env, task, "validate_schedule")
    with env.store.connect() as conn:
        error(
            "invalid_plan", env.service.prepare_tool, conn, env.principal, task, "compare_schedules"
        )
    assert all(
        task["payload"]["evidence"]["solve_schedule"]["payload"][mode]["status"] == "UNKNOWN"
        for mode in ("baseline", "optimized")
    )
    assert env.store.list_objects("recommendation", env.principal) == []


def test_tie_stably_selects_baseline(env):
    from scheduling_agent.domain import CompiledProblem, Plan

    task = create_task(env)
    for tool in EVIDENCE_TOOLS[:4]:
        task = step(env, task, tool)
    evidence = task["payload"]["evidence"]
    problem = CompiledProblem.model_validate(
        evidence["compile_problem"]["payload"]["compiled_problem"]
    )
    plan = Plan.model_validate(evidence["solve_schedule"]["payload"]["baseline"]["plan"])
    result = env.service._compare(problem, {"baseline": plan, "optimized": plan})
    assert result["selected"] == "baseline" and result["improvement"] == 0


def test_source_revocation_blocks_issuance_after_actual_simulation(env):
    task = create_task(env)
    for tool in EVIDENCE_TOOLS[:6]:
        task = step(env, task, tool)
    with env.store.connect() as conn:
        prepared = env.service.prepare_tool(conn, env.principal, task, "simulate_schedule")
    result = env.service.compute_tool(prepared, "simulate_schedule")
    env.store.set_user(env.principal.user_id, plants=[])
    with env.store.transaction() as conn:
        error(
            "forbidden",
            env.service.issue_evidence,
            conn,
            env.principal,
            task,
            "simulate_schedule",
            result,
        )


def test_issue_requires_write_transaction_and_rechecks_payload_stamp(env):
    task = create_task(env)
    with env.store.connect() as conn:
        result = env.service.execute_tool(conn, env.principal, task, "get_snapshot")
        error(
            "transaction_required",
            env.service.issue_evidence,
            conn,
            env.principal,
            task,
            "get_snapshot",
            result,
        )
    result["task_id"] = "another-task"
    with env.store.transaction() as conn:
        error(
            "integrity",
            env.service.issue_evidence,
            conn,
            env.principal,
            task,
            "get_snapshot",
            result,
        )


@pytest.mark.parametrize("tool", ["run_python", "submit_schedule", "request_human_input", {}, None])
def test_domain_dispatch_rejects_non_evidence_tools(env, tool):
    task = create_task(env)
    with env.store.connect() as conn:
        error("invalid_tool", env.service.execute_tool, conn, env.principal, task, tool)


def test_structural_invalidity_is_not_treated_as_supplementable_input(env):
    data = env.snapshot.model_dump(mode="json")
    del data["materials"][0]["initial_stock"]
    error("validation", env.service.import_snapshot, env.principal, data, "invalid-structure")
    assert env.store.list_objects("snapshot", env.principal) == []


def test_supplement_does_not_refresh_stale_source(env):
    source, old, answers = missing_source(env)
    env.clock[0] += MAX_SOURCE_AGE_MS + 1
    with env.store.transaction() as conn:
        error(
            "source_stale",
            env.service.supplement_snapshot,
            conn,
            env.principal,
            source["object_id"],
            answers=answers,
            expected_binding=old,
        )
    assert env.service.get_snapshot(env.principal, source["object_id"]) == source


def test_model_construct_bypass_cannot_import_an_invalid_snapshot(env):
    malformed = env.snapshot.model_copy(update={"revision": True})
    error("validation", env.service.import_snapshot, env.principal, malformed, "bad-instance")


def test_submit_rollback_does_not_leave_recommendation_or_audit(env):
    task = complete_task(env)
    with pytest.raises(RuntimeError):
        with env.store.transaction() as conn:
            env.service.submit_schedule(
                conn,
                env.principal,
                task,
                task["payload"]["candidate"],
                task["payload"]["evidence"],
                "Untrusted synthetic draft",
            )
            raise RuntimeError("synthetic failure before engine submit commit")
    assert env.store.list_objects("recommendation", env.principal) == []
    assert all(
        event["action"] != "submit_schedule" for event in env.store.audit_events(env.principal)
    )


def test_forged_completed_simulation_cannot_replace_durable_issued_bytes(env):
    task = complete_task(env)
    selected_hash = task["payload"]["candidate"]["plan_hash"]
    result = task["payload"]["evidence"]["simulate_schedule"]["payload"]["simulations"][
        selected_hash
    ]
    # Retain status, completion counts/states, problem/plan hashes, and even the
    # final event, but replace the actual execution trace and rehash everything.
    result["events"] = [result["events"][-1]]
    result["final_inventory"] = {}
    result["simulation_hash"] = canonical_digest(
        {key: value for key, value in result.items() if key != "simulation_hash"}
    )
    task = reseal(env, task, "simulate_schedule")
    error("unissued_evidence", submit, env, task)
    assert env.store.list_objects("recommendation", env.principal) == []


@pytest.mark.parametrize("attack", ["copy", "modified", "other-service"])
def test_issuance_rejects_forged_compute_provenance(env, attack):
    task = create_task(env)
    for tool in EVIDENCE_TOOLS[:6]:
        task = step(env, task, tool)
    with env.store.connect() as conn:
        prepared = env.service.prepare_tool(conn, env.principal, task, "simulate_schedule")
    result = env.service.compute_tool(prepared, "simulate_schedule")
    service = env.service
    if attack == "copy":
        result = dict(result)
    elif attack == "modified":
        selected_hash = task["payload"]["candidate"]["plan_hash"]
        simulation = result["simulations"][selected_hash]
        simulation["events"] = [simulation["events"][-1]]
        simulation["simulation_hash"] = canonical_digest(
            {key: value for key, value in simulation.items() if key != "simulation_hash"}
        )
    else:
        service = SchedulingService(env.store, clock=lambda: NOW)
    with env.store.transaction() as conn:
        error(
            "unissued_evidence",
            service.issue_evidence,
            conn,
            env.principal,
            task,
            "simulate_schedule",
            result,
        )


def test_issued_evidence_receipts_survive_service_restart(env):
    task = complete_task(env)
    env.service = SchedulingService(Store(env.store.root), clock=lambda: NOW)
    env.store = env.service.store
    recommendation = submit(env, task)
    with env.store.connect() as conn:
        checked = env.service.validate_recommendation(conn, env.principal, recommendation)
    assert checked["simulation"]["status"] == "completed"
    assert set(checked["evidence"]) == set(EVIDENCE_TOOLS)


def test_issuance_receipt_rolls_back_with_task_transaction(env):
    task = create_task(env)
    with env.store.connect() as conn:
        prepared = env.service.prepare_tool(conn, env.principal, task, "get_snapshot")
    result = env.service.compute_tool(prepared, "get_snapshot")
    with pytest.raises(RuntimeError):
        with env.store.transaction() as conn:
            record = env.service.issue_evidence(conn, env.principal, task, "get_snapshot", result)
            raise RuntimeError("synthetic failure before task commit")
    with env.store.connect() as conn:
        assert (
            env.store.receipt(
                conn, env.principal, record["evidence_id"], env.service._issuance_request(record)
            )
            is None
        )
    assert env.store.get_object("task", task["object_id"], env.principal) == task


@pytest.mark.parametrize("omitted_tool", EVIDENCE_TOOLS)
def test_every_evidence_requires_its_own_persisted_issuance(env, omitted_tool):
    task = complete_task(env)
    evidence_id = task["payload"]["evidence"][omitted_tool]["evidence_id"]
    with env.store.transaction() as conn:
        conn.execute(
            "DELETE FROM receipts WHERE user_id=? AND action_id=?",
            (env.principal.user_id, evidence_id),
        )
    error("unissued_evidence", submit, env, task)


def test_simulation_evidence_covers_every_validated_plan_hash(env):
    task = complete_task(env)
    evidence = task["payload"]["evidence"]
    hashes = {
        canonical_digest(evidence["solve_schedule"]["payload"][mode]["plan"])
        for mode in ("baseline", "optimized")
    }
    simulations = evidence["simulate_schedule"]["payload"]["simulations"]
    assert set(simulations) == hashes
    simulations.pop(next(iter(hashes)))
    task = reseal(env, task, "simulate_schedule")
    error("invalid_simulation", submit, env, task)


def test_solve_and_simulate_are_not_repeated_during_issue_submit_or_review(env, monkeypatch):
    # Spies execute the actual algorithms; no solver/simulation result is mocked.
    from scheduling_agent import simulation, solver

    real_solve, real_simulate = solver.solve, simulation.simulate
    calls = {"solve": 0, "simulate": 0}

    def solve(*args, **kwargs):
        assert not env.store._connections
        calls["solve"] += 1
        return real_solve(*args, **kwargs)

    def simulate(*args, **kwargs):
        assert not env.store._connections
        calls["simulate"] += 1
        return real_simulate(*args, **kwargs)

    monkeypatch.setattr(solver, "solve", solve)
    monkeypatch.setattr(simulation, "simulate", simulate)
    task = complete_task(env)
    assert calls == {"solve": 2, "simulate": 2}
    recommendation = submit(env, task)
    with env.store.connect() as conn:
        env.service.validate_recommendation(conn, env.principal, recommendation)
    assert calls == {"solve": 2, "simulate": 2}
