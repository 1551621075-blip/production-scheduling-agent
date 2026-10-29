"""Structural contract checks, independent of any scheduling implementation."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from scheduling_agent.domain import (
    MAX_OBJECTIVE,
    Alternative,
    CompiledProblem,
    InventoryPoint,
    Material,
    ObjectiveBreakdown,
    ObjectiveConfig,
    Plan,
    Scenario,
    Slot,
    Snapshot,
    SolveResult,
    SolverStatus,
    ValidationReport,
    Violation,
    Window,
    canonical_digest,
    canonical_json,
    missing_inputs,
)
from scheduling_agent.fixtures import demo_snapshots, tiny_snapshots


@pytest.fixture
def payload():
    return demo_snapshots(1_800_000_000_000)["normal"].model_dump(mode="json")


def _put(payload, path, value):
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value


@pytest.mark.parametrize("bad", [True, False, 1.0, "1"])
@pytest.mark.parametrize(
    "path",
    [
        ("revision",),
        ("observed_at_ms",),
        ("horizon_min",),
        ("freeze_until_min",),
        ("materials", 0, "initial_stock"),
        ("lots", 0, "quantity"),
        ("lots", 0, "release_min"),
        ("lots", 0, "completed_prefix"),
        ("lots", 0, "operations", 0, "alternatives", 0, "duration_min"),
        ("lots", 0, "operations", 0, "alternatives", 0, "resources", 0, "quantity"),
        ("items", 1, "bom", 0, "step_index"),
        ("items", 1, "bom", 0, "units_per_parent"),
        ("orders", 0, "due_min"),
        ("orders", 0, "priority"),
        ("orders", 0, "quantity"),
        ("supplies", 0, "quantity"),
        ("supplies", 0, "at_min"),
        ("resources", 0, "capacity"),
        ("machines", 0, "setups", 0, "duration_min"),
        ("machines", 0, "calendar", 0, "start_min"),
        ("commitments", 0, "start_min"),
        ("objective", "weighted_tardiness"),
    ],
)
def test_integer_fields_reject_coercion(payload, path, bad):
    _put(payload, path, bad)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize("bad", [0, 1, "true", "false"])
def test_qualification_requires_actual_boolean(bad):
    with pytest.raises(ValidationError):
        Alternative(machine_id="M1", duration_min=1, resources=(), qualified=bad)


@pytest.mark.parametrize("bad", ["with:colon", "space id", "../id", "x" * 81, "", 1, True])
def test_identifiers_match_store(payload, bad):
    payload["snapshot_id"] = bad
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


def test_eighty_character_identifier_is_accepted(payload):
    payload["snapshot_id"] = "x" * 80
    assert len(Snapshot.model_validate(payload).snapshot_id) == 80


def test_frozen_deep_models_and_json_roundtrip(payload):
    snapshot = Snapshot.model_validate(payload)
    assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    assert isinstance(snapshot.lots, tuple)
    assert isinstance(snapshot.lots[0].operations, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.horizon_min = 10
    with pytest.raises(ValidationError, match="frozen"):
        snapshot.lots[0].quantity = 9
    with pytest.raises(TypeError):
        snapshot.lots[0] = snapshot.lots[1]


@pytest.mark.parametrize("path", [(), ("lots", 0), ("machines", 0), ("objective",)])
def test_extra_fields_are_forbidden_at_every_level(payload, path):
    cursor = payload
    for key in path:
        cursor = cursor[key]
    cursor["invented_default"] = 1
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("materials", 0, "initial_stock"),
        ("orders", 0, "due_min"),
        ("supplies",),
        ("objective",),
        ("resources",),
        ("items", 0, "bom"),
        ("lots", 0, "completed_prefix"),
        ("lots", 0, "residual"),
    ],
)
def test_missing_structural_fields_are_not_guessed(payload, path):
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    del cursor[path[-1]]
    with pytest.raises(ValidationError, match="Field required"):
        Snapshot.model_validate(payload)


def test_explicit_missing_paths_preserve_observation():
    snapshot = demo_snapshots(1234)["missing"]
    assert snapshot.missing_inputs() == (
        "materials[0].initial_stock",
        "orders[0].due_min",
    )
    assert missing_inputs(snapshot) == snapshot.missing_inputs()
    assert snapshot.observed_at_ms == 1234
    assert demo_snapshots(1234)["normal"].missing_inputs() == ()


def test_only_designated_values_can_be_none(payload):
    payload["lots"][0]["quantity"] = None
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize(
    "path,value",
    [
        (("items", 1, "bom", 0, "component_item_id"), "absent"),
        (("lots", 0, "item_id"), "absent"),
        (("lots", 0, "operations", 0, "alternatives", 0, "machine_id"), "absent"),
        (("lots", 0, "operations", 0, "alternatives", 0, "resources", 0, "resource_id"), "absent"),
        (("machines", 0, "setups", 1, "resources", 0, "resource_id"), "absent"),
        (("orders", 0, "lot_id"), "absent"),
        (("orders", 0, "item_id"), "absent"),
        (("orders", 0, "quantity"), 4),
        (("supplies", 0, "item_id"), "absent"),
        (("materials", 0, "item_id"), "absent"),
        (("commitments", 0, "operation_id"), "absent"),
        (("commitments", 0, "machine_id"), "absent"),
        (("commitments", 0, "predecessor_id"), "absent"),
    ],
)
def test_invalid_cross_references_are_rejected(payload, path, value):
    _put(payload, path, value)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize(
    "collection",
    ["items", "materials", "machines", "resources", "lots", "orders", "supplies", "commitments"],
)
def test_duplicate_identifiers_rejected(payload, collection):
    payload[collection].append(payload[collection][0])
    with pytest.raises(ValidationError, match="duplicate"):
        Snapshot.model_validate(payload)


def test_operations_globally_unique_including_history(payload):
    payload["lots"][1]["operations"][0]["operation_id"] = "a_rough"
    with pytest.raises(ValidationError, match="global operation ID"):
        Snapshot.model_validate(payload)


def test_two_orders_cannot_take_same_lot(payload):
    payload["orders"].append({**payload["orders"][0], "order_id": "another_order"})
    with pytest.raises(ValidationError, match="same lot"):
        Snapshot.model_validate(payload)


def test_bom_cycles_and_out_of_route_indices_rejected(payload):
    payload["items"][0]["bom"] = [
        {"component_item_id": "amber_part", "step_index": 0, "units_per_parent": 1},
    ]
    with pytest.raises(ValidationError, match="BOM cycle"):
        Snapshot.model_validate(payload)
    payload["items"][0]["bom"] = []
    payload["items"][1]["bom"][0]["step_index"] = 2
    with pytest.raises(ValidationError, match="BOM step_index"):
        Snapshot.model_validate(payload)


def test_bom_index_must_exist_in_every_producing_lot(payload):
    payload["items"][1]["bom"][0]["step_index"] = 1
    copy = json.loads(json.dumps(payload["lots"][0]))
    copy["lot_id"] = "second_a"
    copy["operations"] = copy["operations"][:1]
    copy["operations"][0]["operation_id"] = "second_a_rough"
    payload["lots"].append(copy)
    with pytest.raises(ValidationError, match="BOM step_index"):
        Snapshot.model_validate(payload)


def test_duplicate_bom_step_and_resource_demands_rejected(payload):
    payload["items"][1]["bom"].append(payload["items"][1]["bom"][0])
    with pytest.raises(ValidationError, match="duplicate BOM"):
        Snapshot.model_validate(payload)
    payload["items"][1]["bom"].pop()
    demands = payload["lots"][0]["operations"][0]["alternatives"][0]["resources"]
    demands.append(demands[0])
    with pytest.raises(ValidationError, match="duplicate processing resource"):
        Snapshot.model_validate(payload)


def test_one_mode_per_machine_is_required(payload):
    alternatives = payload["lots"][0]["operations"][0]["alternatives"]
    alternatives.append({**alternatives[0], "duration_min": 1})
    with pytest.raises(ValidationError, match="operation machine alternative"):
        Snapshot.model_validate(payload)


def test_complete_setup_table_is_required(payload):
    payload["machines"][0]["setups"] = []
    with pytest.raises(ValidationError, match="incomplete setup table"):
        Snapshot.model_validate(payload)


def test_initial_to_target_setup_is_required(payload):
    payload["machines"][0]["initial_family"] = "cold"
    with pytest.raises(ValidationError, match="incomplete setup table"):
        Snapshot.model_validate(payload)


def test_known_infeasibility_is_not_a_schema_error(payload):
    payload["supplies"] = []
    payload["machines"][0]["calendar"] = []  # Covers frozen processing: solver must prove conflict.
    payload["machines"][0]["qualified_families"] = []
    payload["resources"][0]["qualified_families"] = []
    payload["lots"][0]["operations"][0]["alternatives"][0]["qualified"] = False
    payload["lots"][0]["operations"][0]["alternatives"][0]["resources"][0]["quantity"] = 2
    payload["lots"][0]["operations"][1]["alternatives"] = []
    assert Snapshot.model_validate(payload).missing_inputs() == ()


def test_soft_due_can_precede_release_or_exceed_horizon(payload):
    payload["lots"][0]["release_min"] = 4
    payload["orders"][0]["due_min"] = 0
    payload["orders"][1]["due_min"] = 1440
    assert Snapshot.model_validate(payload).orders[0].due_min == 0


@pytest.mark.parametrize(
    "path,value",
    [
        (("horizon_min",), 1441),
        (("horizon_min",), 0),
        (("materials", 0, "initial_stock"), -1),
        (("materials", 0, "initial_stock"), 10**12 + 1),
        (("lots", 0, "quantity"), 0),
        (("lots", 0, "quantity"), 1_000_001),
        (("resources", 0, "capacity"), 65),
        (("resources", 0, "capacity"), 0),
        (("objective", "weighted_tardiness"), 1001),
        (("orders", 0, "priority"), 1001),
        (("lots", 0, "release_min"), 13),
        (("supplies", 0, "at_min"), 13),
        (("freeze_until_min",), 13),
        (("machines", 0, "calendar", 1, "end_min"), 13),
        (("commitments", 0, "end_min"), 13),
    ],
)
def test_finite_bounds(payload, path, value):
    _put(payload, path, value)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(payload)


def test_remaining_operation_count_not_truncated(payload):
    payload["lots"][0]["operations"] = [
        {**payload["lots"][0]["operations"][0], "operation_id": f"step_{index}"}
        for index in range(24)
    ]
    with pytest.raises(ValidationError, match="more than 24 remaining operations"):
        Snapshot.model_validate(payload)


@pytest.mark.parametrize("start,end", [(0, 0), (3, 2)])
def test_half_open_windows_require_positive_length(start, end):
    with pytest.raises(ValidationError):
        Window(start_min=start, end_min=end)


def test_calendar_touching_allowed_overlapping_rejected(payload):
    payload["machines"][0]["calendar"] = [
        {"start_min": 0, "end_min": 4},
        {"start_min": 4, "end_min": 12},
    ]
    Snapshot.model_validate(payload)
    payload["machines"][0]["calendar"][1]["start_min"] = 3
    with pytest.raises(ValidationError, match="overlap"):
        Snapshot.model_validate(payload)


def test_residual_progress_shape_and_no_setup_residual(payload):
    residual = tiny_snapshots()["residual"]
    assert residual.lots[0].residual.remaining_min == 1
    candidate = residual.model_dump(mode="json")
    candidate["lots"][0]["release_min"] = 1
    with pytest.raises(ValidationError, match="release_min=0"):
        Snapshot.model_validate(candidate)
    candidate["lots"][0]["release_min"] = 0
    candidate["lots"][0]["residual"]["phase"] = "setup"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Snapshot.model_validate(candidate)
    candidate["lots"][0]["residual"].pop("phase")
    candidate["lots"][0]["residual"]["machine_id"] = "absent"
    with pytest.raises(ValidationError, match="residual machine"):
        Snapshot.model_validate(candidate)
    payload["lots"][0]["completed_prefix"] = 3
    with pytest.raises(ValidationError, match="completed_prefix"):
        Snapshot.model_validate(payload)


def test_completed_lot_cannot_have_active_order(payload):
    payload["lots"][0]["completed_prefix"] = 2
    with pytest.raises(ValidationError, match="remaining operations"):
        Snapshot.model_validate(payload)
    payload["orders"] = payload["orders"][1:]
    assert Snapshot.model_validate(payload).lots[0].completed_prefix == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "baseline", "at_min": 1},
        {"kind": "supply_delay", "supply_id": "s"},
        {"kind": "machine_outage", "machine_id": "m", "start_min": 2, "end_min": 2},
        {
            "kind": "machine_outage",
            "machine_id": "m",
            "start_min": 0,
            "end_min": 2,
            "supply_id": "s",
        },
        {"kind": "insert_order"},
        {"kind": "invented"},
    ],
)
def test_scenario_requires_exact_payload(kwargs):
    with pytest.raises(ValidationError):
        Scenario(**kwargs)


def test_scenario_rejects_mismatched_insert(payload):
    with pytest.raises(ValidationError, match="exactly match"):
        Scenario(kind="insert_order", lot=payload["lots"][0], order=payload["orders"][1])


def test_zero_objective_and_nonfinite_bounds_rejected():
    with pytest.raises(ValidationError):
        ObjectiveConfig(weighted_tardiness=0, setup_minutes=0, makespan=0, load_spread=0)
    # Worst allowed weighted tardiness is well inside the exact objective range.
    assert 20 * 1000 * 1440 * 1000 + 3 * 24 * 1440 * 1000 < MAX_OBJECTIVE < 2**53


def _plan():
    return Plan(
        plan_id="plan-demo",
        snapshot_hash="a" * 64,
        problem_hash="b" * 64,
        objective_hash="c" * 64,
        slots=(
            Slot(
                operation_id="op",
                machine_id="M1",
                setup_start_min=0,
                start_min=0,
                end_min=1,
                predecessor_id=None,
            ),
        ),
        objective=ObjectiveBreakdown(
            weighted_tardiness=0, setup_minutes=0, makespan=1, load_spread=0, total=1
        ),
    )


@pytest.mark.parametrize("status", [SolverStatus.FEASIBLE, SolverStatus.OPTIMAL])
def test_usable_solver_status_requires_candidate(status):
    result = SolveResult(
        status=status,
        mode="optimize",
        plan=_plan(),
        best_bound=1.0,
        wall_time_s=0.1,
        solver_version="9.15",
    )
    assert result.candidate == result.plan
    with pytest.raises(ValidationError, match="contain a plan"):
        SolveResult(
            status=status,
            mode="optimize",
            plan=None,
            best_bound=None,
            wall_time_s=0.1,
            solver_version="9.15",
        )


@pytest.mark.parametrize("status", [SolverStatus.UNKNOWN, SolverStatus.INFEASIBLE])
def test_nonfeasible_status_must_not_contain_candidate(status):
    result = SolveResult(
        status=status,
        mode="optimize",
        plan=None,
        best_bound=0.0,
        wall_time_s=0.1,
        solver_version="9.15",
    )
    assert result.plan is None
    with pytest.raises(ValidationError, match="contain a plan"):
        SolveResult(
            status=status,
            mode="optimize",
            plan=_plan(),
            best_bound=None,
            wall_time_s=0.1,
            solver_version="9.15",
        )


def test_error_explicit_and_feasibility_not_optimal():
    failure = SolveResult(
        status="ERROR",
        mode="optimize",
        plan=None,
        best_bound=None,
        wall_time_s=0.0,
        solver_version="9.15",
        error="MODEL_INVALID",
    )
    assert failure.status == SolverStatus.ERROR
    with pytest.raises(ValidationError, match="ERROR requires"):
        SolveResult(
            status="ERROR",
            mode="optimize",
            plan=None,
            best_bound=None,
            wall_time_s=0.0,
            solver_version="9.15",
        )
    with pytest.raises(ValidationError, match="not OPTIMAL"):
        SolveResult(
            status="OPTIMAL",
            mode="feasibility",
            plan=_plan(),
            best_bound=None,
            wall_time_s=0.0,
            solver_version="9.15",
        )
    with pytest.raises(ValidationError, match="no objective bound"):
        SolveResult(
            status="FEASIBLE",
            mode="feasibility",
            plan=_plan(),
            best_bound=1.0,
            wall_time_s=0.0,
            solver_version="9.15",
        )


@pytest.mark.parametrize("bad", [True, "0.1", float("inf"), float("nan")])
def test_solver_timing_is_strict_and_finite(bad):
    with pytest.raises(ValidationError):
        SolveResult(
            status="UNKNOWN",
            mode="optimize",
            plan=None,
            best_bound=None,
            wall_time_s=bad,
            solver_version="9.15",
        )


def test_validation_report_cannot_self_contradict():
    kwargs = dict(
        inventory=(),
        activities=(),
        snapshot_hash="a" * 64,
        problem_hash="b" * 64,
        objective_hash="c" * 64,
        plan_hash="d" * 64,
    )
    with pytest.raises(ValidationError):
        ValidationReport(valid=True, violations=(), objective=None, **kwargs)
    with pytest.raises(ValidationError):
        ValidationReport(valid=False, violations=(), objective=None, **kwargs)
    report = ValidationReport(
        valid=False,
        violations=(Violation(code="material_shortage", message="negative balance"),),
        objective=None,
        **kwargs,
    )
    assert not report.valid
    assert InventoryPoint(item_id="raw", at_min=0, received=0, consumed=1, balance=-1).balance == -1


def test_compiled_operation_lookup_is_immutable():
    problem = CompiledProblem(
        snapshot=tiny_snapshots()["handoff"],
        snapshot_hash="a" * 64,
        problem_hash="b" * 64,
        objective_hash="c" * 64,
        compiler_version="v1",
        operations=(),
    )
    assert problem.required_operations == ()
    with pytest.raises(TypeError):
        problem.operations_by_id["invented"] = None


def test_canonical_digest_json_interoperability_and_content_sensitivity():
    snapshot = demo_snapshots(1234)["normal"]
    assert canonical_digest(snapshot) == canonical_digest(snapshot.model_dump(mode="json"))
    assert canonical_digest({"z": [1, 2], "a": "合成"}) == canonical_digest(
        {"a": "合成", "z": (1, 2)}
    )
    expected = '{"a":"合成","z":[1,2]}'
    assert canonical_json({"z": [1, 2], "a": "合成"}) == expected
    assert (
        canonical_digest({"a": "合成", "z": [1, 2]})
        == hashlib.sha256(expected.encode()).hexdigest()
    )
    assert canonical_digest([1, 2]) != canonical_digest([2, 1])
    changed = snapshot.model_dump(mode="json")
    changed["observed_at_ms"] += 1
    assert canonical_digest(snapshot) != canonical_digest(changed)
    assert canonical_digest(Material(item_id="raw", initial_stock=None)) != canonical_digest(
        Material(item_id="raw", initial_stock=0),
    )
    with pytest.raises(ValueError):
        canonical_digest({"nan": float("nan")})
    with pytest.raises(TypeError):
        canonical_digest({1: "not a JSON object key"})


@pytest.mark.parametrize("bad", [False, 1, 0, 1.0, "true", "True", None])
def test_snapshot_synthetic_requires_boolean_true_before_literal_coercion(payload, bad):
    payload["synthetic"] = bad
    with pytest.raises(ValidationError, match="synthetic must be the boolean True"):
        Snapshot.model_validate(payload)
    with pytest.raises(ValidationError, match="synthetic must be the boolean True"):
        Snapshot.model_validate_json(json.dumps(payload))


def test_snapshot_synthetic_default_is_serialized_and_digest_bound(payload):
    assert payload["synthetic"] is True
    explicit = Snapshot.model_validate(payload)
    del payload["synthetic"]
    implicit = Snapshot.model_validate(payload)
    assert implicit.synthetic is True
    assert implicit == explicit
    assert canonical_digest(implicit) == canonical_digest(explicit)
    assert canonical_digest(implicit) != canonical_digest(payload)
    assert Snapshot.model_validate_json(implicit.model_dump_json()) == implicit
    with pytest.raises(ValidationError, match="frozen"):
        implicit.synthetic = False


def test_plan_hash_is_content_digest_without_serialized_self_reference():
    plan = _plan()
    dumped = plan.model_dump(mode="json")
    assert "plan_hash" not in dumped
    assert "plan_hash" not in Plan.model_fields
    assert "plan_hash" not in Plan.model_computed_fields
    assert plan.plan_hash == canonical_digest(dumped) == canonical_digest(plan)
    assert plan.plan_hash == Plan.model_validate_json(plan.model_dump_json()).plan_hash
    changed_id = Plan.model_validate({**dumped, "plan_id": "another-plan"})
    assert changed_id.plan_hash != plan.plan_hash
    changed_objective = Plan.model_validate(
        {**dumped, "objective": {**dumped["objective"], "total": 2}}
    )
    assert changed_objective.plan_hash != plan.plan_hash
    with pytest.raises(ValidationError):
        plan.plan_hash = "e" * 64


def test_insert_order_requires_explicit_due_but_snapshot_may_request_it(payload):
    payload["orders"][0]["due_min"] = None
    snapshot = Snapshot.model_validate(payload)
    assert snapshot.missing_inputs() == ("orders[0].due_min",)
    with pytest.raises(ValidationError, match="inserted order requires an explicit due_min"):
        Scenario(kind="insert_order", lot=snapshot.lots[0], order=snapshot.orders[0])
    payload["orders"][0]["due_min"] = 0
    accepted = Scenario(kind="insert_order", lot=payload["lots"][0], order=payload["orders"][0])
    assert accepted.order.due_min == 0
