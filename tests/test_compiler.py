"""Synthetic compiler and approved-scenario contracts (no external credentials)."""

import pytest
from pydantic import ValidationError

from scheduling_agent.compiler import COMPILER_VERSION, build_scenario, compile_problem
from scheduling_agent.domain import Scenario, Snapshot, canonical_digest
from scheduling_agent.errors import DomainError
from scheduling_agent.fixtures import demo_scenarios, demo_snapshots, tiny_snapshots


@pytest.fixture
def normal():
    return demo_snapshots(1_800_000_000_000)["normal"]


def test_compile_expands_whole_lot_bom_and_pending_route(normal):
    problem = compile_problem(normal)
    assert [op.operation_id for op in problem.operations] == ["a_rough", "a_finish", "b_finish"]
    rough, finish, historical = problem.operations
    assert rough.quantity == 3
    assert [(amount.item_id, amount.quantity) for amount in rough.consumes] == [("raw", 6)]
    assert rough.predecessor_id is None and not rough.is_terminal
    assert finish.predecessor_id == "a_rough" and finish.is_terminal and finish.consumes == ()
    assert historical.step_index == 1 and historical.predecessor_id is None
    assert historical.consumes == ()
    assert rough.alternatives == normal.lots[0].operations[0].alternatives
    with pytest.raises(TypeError):
        problem.operations_by_id["other"] = rough


def test_compile_residual_skips_only_already_issued_inputs():
    payload = tiny_snapshots()["handoff"].model_dump(mode="json")
    payload["lots"] = [payload["lots"][1]]
    payload["lots"][0]["residual"] = {"machine_id": "tiny_machine", "remaining_min": 2}
    problem = compile_problem(Snapshot.model_validate(payload))
    operation, = problem.operations
    assert operation.consumes == ()
    assert operation.residual.remaining_min == 2
    assert operation.is_terminal and operation.predecessor_id is None


def test_fully_completed_authorized_lot_has_no_future_operations():
    payload = tiny_snapshots()["handoff"].model_dump(mode="json")
    payload["lots"] = [payload["lots"][0]]
    payload["lots"][0]["completed_prefix"] = 1
    payload["orders"] = []
    problem = compile_problem(Snapshot.model_validate(payload))
    assert problem.operations == ()


def test_hashes_bind_source_time_objective_operations_and_compiler(normal):
    first = compile_problem(normal)
    assert first == compile_problem(normal)
    assert first.compiler_version == COMPILER_VERSION
    assert first.snapshot_hash == canonical_digest(normal)
    assert first.objective_hash == canonical_digest(normal.objective)
    assert first.problem_hash == canonical_digest(first.model_dump(mode="json", exclude={"problem_hash"}))
    changed = normal.model_dump(mode="json")
    changed["observed_at_ms"] += 1
    second = compile_problem(Snapshot.model_validate(changed))
    assert second.problem_hash != first.problem_hash
    assert second.snapshot_hash != first.snapshot_hash
    assert second.objective_hash == first.objective_hash
    changed["objective"]["makespan"] += 1
    third = compile_problem(Snapshot.model_validate(changed))
    assert third.objective_hash != second.objective_hash


def test_missing_inputs_rejected_with_all_exact_paths():
    missing = demo_snapshots(1_800_000_000_000)["missing"]
    with pytest.raises(DomainError) as caught:
        compile_problem(missing)
    assert caught.value.code == "missing_fields"
    for path in missing.missing_inputs():
        assert path in str(caught.value)


def test_compilation_does_not_relabel_known_shortages_or_refresh_time():
    snapshot = demo_snapshots(0)["infeasible"]
    problem = compile_problem(snapshot)
    assert problem.snapshot == snapshot
    assert problem.snapshot.observed_at_ms == 0


@pytest.mark.parametrize("kind", ["baseline", "insert_order", "machine_outage", "supply_delay"])
def test_approved_scenarios_keep_lineage_horizon_objective_and_commitments(normal, kind):
    result = build_scenario(normal, demo_scenarios(normal)[kind])
    assert result.observed_at_ms == normal.observed_at_ms
    assert result.revision == normal.revision
    assert result.snapshot_id == normal.snapshot_id
    assert result.horizon_min == normal.horizon_min
    assert result.commitments == normal.commitments
    assert result.freeze_until_min == normal.freeze_until_min
    assert result.objective == normal.objective
    assert (canonical_digest(result) == canonical_digest(normal)) == (kind == "baseline")
    assert normal == demo_snapshots(normal.observed_at_ms)["normal"]


def test_outage_subtracts_open_time_without_moving_frozen_slots(normal):
    result = build_scenario(normal, Scenario(
        kind="machine_outage", machine_id="M1", start_min=1, end_min=6
    ))
    assert [(w.start_min, w.end_min) for w in result.machines[0].calendar] == [(0, 1), (6, 12)]
    assert result.commitments[0] == normal.commitments[0]
    assert result.machines[1] == normal.machines[1]


@pytest.mark.parametrize("fields", [
    {"kind": "machine_outage", "machine_id": "unknown", "start_min": 0, "end_min": 1},
    {"kind": "machine_outage", "machine_id": "M1", "start_min": 4, "end_min": 5},
    {"kind": "machine_outage", "machine_id": "M1", "start_min": 10, "end_min": 13},
    {"kind": "supply_delay", "supply_id": "unknown", "at_min": 1},
    {"kind": "supply_delay", "supply_id": "raw_late", "at_min": 6},
    {"kind": "supply_delay", "supply_id": "raw_late", "at_min": 5},
    {"kind": "supply_delay", "supply_id": "raw_late", "at_min": 13},
])
def test_invalid_or_noop_mutations_are_rejected(normal, fields):
    with pytest.raises(DomainError, match=".") as caught:
        build_scenario(normal, Scenario(**fields))
    assert caught.value.code == "invalid_scenario"


def test_insert_duplicate_id_or_missing_setup_transition_is_not_invented(normal):
    original_lot, original_order = normal.lots[0], normal.orders[0]
    with pytest.raises(DomainError):
        build_scenario(normal, Scenario(kind="insert_order", lot=original_lot, order=original_order))
    scenario = demo_scenarios(normal)["insert_order"].model_dump(mode="json")
    scenario["lot"]["operations"][0]["family"] = "new_family"
    with pytest.raises(DomainError, match="incomplete setup table"):
        build_scenario(normal, Scenario.model_validate(scenario))


def test_strict_scenario_and_snapshot_instances_only(normal):
    with pytest.raises(DomainError):
        build_scenario(normal, {"kind": "baseline"})
    with pytest.raises(ValidationError):
        Scenario(kind="baseline", at_min=1)
    with pytest.raises(DomainError):
        compile_problem(normal.model_dump(mode="json"))


def test_copy_bypass_does_not_turn_structurally_invalid_data_into_problem(normal):
    with pytest.raises(DomainError):
        compile_problem(normal.model_copy(update={"horizon_min": True}))
    with pytest.raises(DomainError):
        build_scenario(normal, Scenario.model_construct(kind="baseline", at_min=1))


def test_bom_amount_uses_exact_large_integer_product():
    payload = tiny_snapshots()["handoff"].model_dump(mode="json")
    payload["lots"] = [payload["lots"][1]]
    payload["lots"][0]["quantity"] = 1_000_000
    payload["items"][1]["bom"][0]["units_per_parent"] = 1_000_000
    payload["orders"][0]["quantity"] = 1_000_000
    problem = compile_problem(Snapshot.model_validate(payload))
    assert problem.operations[0].consumes[0].quantity == 1_000_000_000_000
