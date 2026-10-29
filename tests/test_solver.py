"""Small known witnesses and impossible schedules, all using real local CP-SAT.

These tests do not implement the independent validator/oracle. Assertions target
specific public results and arithmetic witnesses rather than a second checker.
"""

import copy

import pytest
from ortools.sat.python import cp_model

from scheduling_agent.compiler import build_scenario, compile_problem
from scheduling_agent.domain import Scenario, Snapshot, SolverStatus, canonical_digest
from scheduling_agent.errors import DomainError
from scheduling_agent.fixtures import demo_snapshots, tiny_snapshots
from scheduling_agent.solver import solve


def _shop(durations=(1,), *, machines=1, horizon=12, families=None):
    """Build an explicit synthetic input, not a duplicate domain model."""
    families = families or ("a",) * len(durations)
    names = sorted(set(families) | {"a"})
    return {
        "snapshot_id": "synthetic-solver-test", "plant_id": "synthetic-plant",
        "revision": 1, "observed_at_ms": 0, "horizon_min": horizon,
        "items": [{"item_id": "part", "unit": "piece", "bom": []}],
        "materials": [{"item_id": "part", "initial_stock": 0}],
        "machines": [
            {"machine_id": f"m{index}", "initial_family": "a", "qualified_families": names,
             "calendar": [{"start_min": 0, "end_min": horizon}],
             "setups": [{"from_family": source, "to_family": target,
                         "duration_min": 0, "resources": []}
                        for source in names for target in names]}
            for index in range(machines)
        ],
        "resources": [],
        "lots": [
            {"lot_id": f"l{index}", "item_id": "part", "quantity": 1,
             "release_min": 0, "completed_prefix": 0, "residual": None,
             "operations": [{"operation_id": f"o{index}", "family": family,
                             "alternatives": [{"machine_id": f"m{machine}",
                                               "duration_min": duration, "qualified": True,
                                               "resources": []} for machine in range(machines)]}]}
            for index, (duration, family) in enumerate(zip(durations, families, strict=True))
        ],
        "orders": [], "supplies": [], "freeze_until_min": 0, "commitments": [],
        "objective": {"weighted_tardiness": 1, "setup_minutes": 1,
                      "makespan": 1, "load_spread": 1},
    }


def _run(payload, **kwargs):
    return solve(compile_problem(Snapshot.model_validate(payload)), **kwargs)


def _resource(payload, *, capacity=1, calendar=None, families=("a",)):
    payload["resources"] = [{
        "resource_id": "crew", "capacity": capacity, "qualified_families": list(families),
        "calendar": calendar or [{"start_min": 0, "end_min": payload["horizon_min"]}],
    }]
    for lot in payload["lots"]:
        for operation in lot["operations"]:
            for alternative in operation["alternatives"]:
                alternative["resources"] = [{"resource_id": "crew", "quantity": 1}]


def _fixed(operation, machine, start, end, *, setup=None, previous=None):
    return {"operation_id": operation, "machine_id": machine,
            "setup_start_min": start if setup is None else setup, "start_min": start,
            "end_min": end, "predecessor_id": previous}


@pytest.mark.parametrize("mode, status", [
    ("feasibility", SolverStatus.FEASIBLE), ("optimize", SolverStatus.OPTIMAL),
])
def test_actual_solver_demo_candidate_exact_objective_and_hash(mode, status):
    problem = compile_problem(demo_snapshots(1_800_000_000_000)["normal"])
    result = solve(problem, mode=mode)
    assert result.status == status and result.error is None
    assert result.solver_version == "OR-Tools 9.15.6755"
    assert result.wall_time_s >= 0
    assert (result.best_bound is None) == (mode == "feasibility")
    plan = result.plan
    assert plan.objective.model_dump() == {
        "weighted_tardiness": 0, "setup_minutes": 1, "makespan": 8,
        "load_spread": 3, "total": 13,
    }
    assert (plan.snapshot_hash, plan.problem_hash, plan.objective_hash) == (
        problem.snapshot_hash, problem.problem_hash, problem.objective_hash,
    )
    assert plan.plan_id == "plan-" + canonical_digest(plan.model_dump(mode="json", exclude={"plan_id"}))
    assert [slot.operation_id for slot in plan.slots] == ["a_finish", "a_rough", "b_finish"]
    assert not hasattr(plan, "valid") and not hasattr(result, "valid")


@pytest.mark.parametrize("name, expected", [
    ("handoff", SolverStatus.OPTIMAL), ("soft_due", SolverStatus.OPTIMAL),
    ("residual", SolverStatus.OPTIMAL), ("unqualified", SolverStatus.INFEASIBLE),
    ("shortage", SolverStatus.INFEASIBLE), ("batched_debits", SolverStatus.INFEASIBLE),
])
def test_tiny_known_feasibility_cases(name, expected):
    result = solve(compile_problem(tiny_snapshots()[name]))
    assert result.status == expected
    assert (result.plan is not None) == (expected == SolverStatus.OPTIMAL)
    if name == "handoff":
        by_id = {slot.operation_id: slot for slot in result.plan.slots}
        assert by_id["make_component"].end_min == by_id["make_finished"].start_min == 1
    if name == "soft_due":
        assert result.plan.objective.weighted_tardiness == 2
        assert result.plan.objective.total == 8


def test_true_zero_second_unknown_is_not_infeasibility():
    problem = compile_problem(demo_snapshots(0)["normal"])
    for mode in ("optimize", "feasibility"):
        result = solve(problem, mode=mode, time_limit_s=0.0)
        assert result.status == SolverStatus.UNKNOWN
        assert result.plan is None and result.error is None


def test_single_worker_objective_absence_and_real_cp_status_mapping(monkeypatch):
    original = cp_model.CpSolver.solve
    calls = []

    def observe(self, model, *args, **kwargs):
        status = original(self, model, *args, **kwargs)
        calls.append((self.parameters.num_search_workers, model.has_objective(), status))
        return status

    monkeypatch.setattr(cp_model.CpSolver, "solve", observe)
    baseline = _run(_shop(), mode="feasibility")
    optimized = _run(_shop(), mode="optimize")
    assert calls == [(1, False, cp_model.OPTIMAL), (1, True, cp_model.OPTIMAL)]
    assert baseline.status == SolverStatus.FEASIBLE and optimized.status == SolverStatus.OPTIMAL


def test_solver_engine_exception_is_explicit_error(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(cp_model.CpSolver, "solve", broken)
    result = _run(_shop())
    assert result.status == SolverStatus.ERROR and result.plan is None
    assert "RuntimeError" in result.error and result.best_bound is None


def test_actual_model_invalid_is_not_infeasible(monkeypatch):
    original = cp_model.CpModel.validate

    def invalid(self):
        # Real OR-Tools validation of a deliberately malformed reservoir, not a fabricated status.
        self.proto.constraints.add().reservoir.max_level = -1
        return original(self)

    monkeypatch.setattr(cp_model.CpModel, "validate", invalid)
    result = _run(_shop())
    assert result.status == SolverStatus.ERROR and result.plan is None
    assert "invalid CP-SAT model" in result.error


@pytest.mark.parametrize("kwargs", [
    {"mode": "pretend"}, {"time_limit_s": -1}, {"time_limit_s": True},
    {"time_limit_s": float("nan")}, {"time_limit_s": float("inf")},
    {"time_limit_s": "1"}, {"time_limit_s": 301}, {"time_limit_s": 10**1000},
    {"seed": True},
    {"seed": -1}, {"seed": 2**31}, {"seed": 1.2},
])
def test_invalid_search_parameters_rejected(kwargs):
    with pytest.raises(DomainError, match="mode must"):
        _run(_shop(), **kwargs)


def test_seeded_search_stable_candidate_not_wall_clock():
    problem = compile_problem(demo_snapshots(0)["normal"])
    first, second = solve(problem), solve(problem)
    assert first.plan == second.plan and first.status == second.status


def test_empty_pending_problem_is_valid_zero_objective():
    data = _shop()
    data["lots"][0]["completed_prefix"] = 1
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert result.plan.slots == () and result.plan.objective.total == 0


def test_flexible_alternative_avoids_known_unqualified_fast_machine():
    data = _shop((1,), machines=2)
    alternatives = data["lots"][0]["operations"][0]["alternatives"]
    alternatives[0]["qualified"] = False
    alternatives[1]["duration_min"] = 3
    result = _run(data)
    slot, = result.plan.slots
    assert slot.machine_id == "m1" and slot.end_min - slot.start_min == 3


@pytest.mark.parametrize("reason", ["empty", "alternative", "machine", "resource", "setup_resource"])
def test_all_machine_and_resource_qualification_gates_are_hard(reason):
    data = _shop()
    operation = data["lots"][0]["operations"][0]
    if reason == "empty":
        operation["alternatives"] = []
    elif reason == "alternative":
        operation["alternatives"][0]["qualified"] = False
    elif reason == "machine":
        data["machines"][0]["qualified_families"] = []
    else:
        _resource(data, families=())
        if reason == "setup_resource":
            operation["alternatives"][0]["resources"] = []
            data["machines"][0]["setups"][0]["resources"] = [
                {"resource_id": "crew", "quantity": 1}
            ]
    assert _run(data).status == SolverStatus.INFEASIBLE


def test_unary_machine_cannot_overlap_mandatory_lots():
    assert _run(_shop((2, 2), horizon=3)).status == SolverStatus.INFEASIBLE


@pytest.mark.parametrize("count, capacity, expected", [
    (2, 2, SolverStatus.OPTIMAL), (3, 2, SolverStatus.INFEASIBLE),
    (2, 1, SolverStatus.INFEASIBLE),
])
def test_shared_homogeneous_pool_capacity(count, capacity, expected):
    data = _shop((2,) * count, machines=count, horizon=2)
    _resource(data, capacity=capacity)
    assert _run(data).status == expected


def test_shared_capacity_release_before_same_minute_reacquisition():
    data = _shop((2, 2), machines=2, horizon=4)
    _resource(data)
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert sorted((slot.start_min, slot.end_min) for slot in result.plan.slots) == [(0, 2), (2, 4)]


@pytest.mark.parametrize("target", ["machine", "resource"])
def test_positive_processing_never_crosses_closed_calendar_gap(target):
    data = _shop((3,), horizon=4)
    calendar = [{"start_min": 0, "end_min": 2}, {"start_min": 3, "end_min": 4}]
    if target == "machine":
        data["machines"][0]["calendar"] = calendar
    else:
        _resource(data, calendar=calendar)
    assert _run(data).status == SolverStatus.INFEASIBLE


@pytest.mark.parametrize("target", ["machine", "resource"])
def test_touching_calendar_windows_have_no_closed_gap(target):
    data = _shop((3,), horizon=4)
    calendar = [{"start_min": 0, "end_min": 2}, {"start_min": 2, "end_min": 4}]
    if target == "machine":
        data["machines"][0]["calendar"] = calendar
    else:
        _resource(data, calendar=calendar)
    assert _run(data).status == SolverStatus.OPTIMAL


def test_sequence_circuit_charges_only_actual_neighbor_setups():
    data = _shop((1, 1, 1), families=("a", "a", "b"), horizon=12)
    for rule in data["machines"][0]["setups"]:
        pair = rule["from_family"], rule["to_family"]
        rule["duration_min"] = {("a", "a"): 0, ("a", "b"): 2,
                                ("b", "a"): 7, ("b", "b"): 0}[pair]
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert result.plan.objective.setup_minutes == 2 and result.plan.objective.makespan == 5
    slots = sorted(result.plan.slots, key=lambda slot: slot.start_min)
    assert slots[0].predecessor_id is None
    assert slots[1].predecessor_id == slots[0].operation_id
    assert slots[2].operation_id == "o2" and slots[2].predecessor_id == slots[1].operation_id
    assert slots[2].setup_start_min == 2 and slots[2].start_min == 4


def test_setup_is_contiguous_processing_not_idle_after_early_setup():
    data = _shop((1,), horizon=5)
    _resource(data, calendar=[{"start_min": 0, "end_min": 2}])
    data["lots"][0]["operations"][0]["alternatives"][0]["resources"] = []
    data["lots"][0]["release_min"] = 1
    data["machines"][0]["setups"][0].update(
        duration_min=2, resources=[{"resource_id": "crew", "quantity": 1}]
    )
    assert _run(data).status == SolverStatus.INFEASIBLE
    data["lots"][0]["release_min"] = 0
    result = _run(data)
    slot, = result.plan.slots
    assert (slot.setup_start_min, slot.start_min, slot.end_min) == (0, 2, 3)


def test_setup_cannot_cross_a_closed_shift_and_resources_are_shared():
    data = _shop((1, 1), machines=2, horizon=3)
    _resource(data)
    for lot in data["lots"]:
        for alternative in lot["operations"][0]["alternatives"]:
            alternative["resources"] = []
    for machine in data["machines"]:
        machine["setups"][0].update(
            duration_min=2, resources=[{"resource_id": "crew", "quantity": 1}]
        )
    assert _run(data).status == SolverStatus.INFEASIBLE
    data["lots"] = data["lots"][:1]
    data["resources"][0]["calendar"] = [
        {"start_min": 0, "end_min": 1}, {"start_min": 2, "end_min": 3}
    ]
    assert _run(data).status == SolverStatus.INFEASIBLE


def test_route_predecessor_finishes_before_next_setup_not_only_processing():
    data = _shop((3, 1), machines=2, families=("a", "b"), horizon=6)
    first = data["lots"][0]["operations"][0]
    second = data["lots"][1]["operations"][0]
    first["alternatives"] = first["alternatives"][:1]
    second["alternatives"] = second["alternatives"][1:]
    data["lots"] = [{**data["lots"][0], "operations": [first, second]}]
    for rule in data["machines"][1]["setups"]:
        if rule["to_family"] == "b":
            rule["duration_min"] = 2
    result = _run(data)
    first_slot, next_slot = result.plan.slots
    assert first_slot.end_min == next_slot.setup_start_min == 3
    assert next_slot.start_min == 5 and next_slot.end_min == 6
    data["lots"][0]["release_min"] = 1
    assert _run(data).status == SolverStatus.INFEASIBLE


def test_frozen_predecessor_and_entire_slot_are_hard():
    data = _shop((1, 1), horizon=3)
    data["freeze_until_min"] = 2
    data["commitments"] = [_fixed("o0", "m0", 0, 1), _fixed("o1", "m0", 1, 2)]
    assert _run(data).status == SolverStatus.INFEASIBLE
    data["commitments"][1]["predecessor_id"] = "o0"
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert [slot.model_dump(mode="json") for slot in result.plan.slots] == data["commitments"]


def test_freeze_boundary_is_strict_and_outage_does_not_unfreeze_commitment():
    data = _shop((2,), horizon=6)
    data["freeze_until_min"] = 1
    data["commitments"] = [_fixed("o0", "m0", 1, 3)]
    result = _run(data)
    assert result.plan.slots[0].start_min == 0  # starts exactly at freeze limit: not frozen
    data["commitments"] = [_fixed("o0", "m0", 0, 2)]
    snapshot = Snapshot.model_validate(data)
    changed = build_scenario(snapshot, Scenario(
        kind="machine_outage", machine_id="m0", start_min=1, end_min=2
    ))
    assert solve(compile_problem(changed)).status == SolverStatus.INFEASIBLE


def test_outage_covering_frozen_setup_is_true_infeasibility():
    data = _shop((1,), horizon=5)
    data["machines"][0]["setups"][0]["duration_min"] = 2
    data["freeze_until_min"] = 1
    data["commitments"] = [_fixed("o0", "m0", 2, 3, setup=0)]
    changed = build_scenario(Snapshot.model_validate(data), Scenario(
        kind="machine_outage", machine_id="m0", start_min=0, end_min=1
    ))
    assert solve(compile_problem(changed)).status == SolverStatus.INFEASIBLE


def test_residual_remaining_time_no_setup_first_and_still_produces_output():
    data = _shop((8, 1), horizon=4)
    data["lots"][0]["residual"] = {"machine_id": "m0", "remaining_min": 2}
    data["machines"][0]["setups"][0]["duration_min"] = 1
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    residual, following = result.plan.slots
    assert (residual.setup_start_min, residual.start_min, residual.end_min) == (0, 0, 2)
    assert residual.predecessor_id is None and following.predecessor_id == "o0"
    assert result.plan.objective.setup_minutes == 1 and result.plan.objective.makespan == 4
    data["commitments"] = [_fixed("o0", "m0", 1, 3)]
    assert _run(data).status == SolverStatus.INFEASIBLE  # residual frozen even freeze_until=0


def test_residual_material_was_issued_but_terminal_order_is_delivered():
    data = tiny_snapshots()["handoff"].model_dump(mode="json")
    data["lots"] = [data["lots"][1]]
    data["lots"][0]["residual"] = {"machine_id": "tiny_machine", "remaining_min": 1}
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL and result.plan.slots[0].start_min == 0
    assert result.plan.objective.makespan == 1


def test_initial_credit_at_tick_zero_and_same_time_supply_precede_consumption():
    data = tiny_snapshots()["shortage"].model_dump(mode="json")
    data["materials"][0]["initial_stock"] = 2
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL and result.plan.slots[0].start_min == 0
    data["materials"][0]["initial_stock"] = 0
    data["supplies"] = [{"supply_id": "s", "item_id": "component", "quantity": 2, "at_min": 2}]
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL and result.plan.slots[0].start_min == 2


def test_future_receipts_cannot_cover_intermediate_shortage():
    data = tiny_snapshots()["shortage"].model_dump(mode="json")
    data["materials"][0]["initial_stock"] = 0
    data["supplies"] = [{"supply_id": "s", "item_id": "component", "quantity": 2, "at_min": 3}]
    data["freeze_until_min"] = 1
    data["commitments"] = [_fixed("make_finished", "tiny_machine", 0, 1)]
    assert _run(data).status == SolverStatus.INFEASIBLE


def test_order_delivery_and_component_use_cannot_double_spend_same_receipt():
    data = tiny_snapshots()["handoff"].model_dump(mode="json")
    data["orders"].append({"order_id": "component_sale", "lot_id": "component_lot",
                           "item_id": "component", "quantity": 2, "due_min": 4, "priority": 1})
    assert _run(data).status == SolverStatus.INFEASIBLE
    data["materials"][0]["initial_stock"] = 2
    assert _run(data).status == SolverStatus.OPTIMAL


def test_multilevel_bom_authorized_whole_lots_and_same_minute_handoffs():
    data = _shop((1, 1, 1), horizon=3)
    data["items"] = [
        {"item_id": "raw", "unit": "piece", "bom": []},
        {"item_id": "middle", "unit": "piece", "bom": [
            {"component_item_id": "raw", "step_index": 0, "units_per_parent": 2}]},
        {"item_id": "part", "unit": "piece", "bom": [
            {"component_item_id": "middle", "step_index": 0, "units_per_parent": 2}]},
    ]
    data["materials"] = [{"item_id": item, "initial_stock": 0} for item in ("raw", "middle", "part")]
    for lot, item, quantity in zip(data["lots"], ("raw", "middle", "part"), (4, 2, 1), strict=True):
        lot.update(item_id=item, quantity=quantity)
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert [(slot.start_min, slot.end_min) for slot in result.plan.slots] == [(0, 1), (1, 2), (2, 3)]
    data["lots"][0]["quantity"] = 3
    assert _run(data).status == SolverStatus.INFEASIBLE


def test_soft_due_weighted_priority_setup_makespan_and_unused_machine_spread():
    data = _shop((2,), machines=2, horizon=8)
    data["lots"][0]["operations"][0]["alternatives"] = data["lots"][0]["operations"][0]["alternatives"][:1]
    data["machines"][0]["setups"][0]["duration_min"] = 1
    data["orders"] = [{"order_id": "sale", "lot_id": "l0", "item_id": "part",
                       "quantity": 1, "due_min": 1, "priority": 3}]
    data["objective"] = {"weighted_tardiness": 2, "setup_minutes": 5,
                         "makespan": 7, "load_spread": 11}
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    assert result.plan.objective.model_dump() == {
        "weighted_tardiness": 6, "setup_minutes": 1, "makespan": 3,
        "load_spread": 3, "total": 71,
    }


def test_load_spread_optimization_uses_all_machines_including_unused():
    data = _shop((1, 1, 1), machines=2, horizon=3)
    data["objective"] = {"weighted_tardiness": 0, "setup_minutes": 0,
                         "makespan": 0, "load_spread": 1}
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL and result.plan.objective.total == 1
    assert len({slot.machine_id for slot in result.plan.slots}) == 2


def test_setup_and_processing_compete_for_same_shared_resource():
    data = _shop((2, 1), machines=2, families=("a", "b"), horizon=3)
    _resource(data, families=("a", "b"))
    data["lots"][0]["operations"][0]["alternatives"] = data["lots"][0]["operations"][0]["alternatives"][:1]
    data["lots"][1]["operations"][0]["alternatives"] = data["lots"][1]["operations"][0]["alternatives"][1:]
    data["lots"][1]["operations"][0]["alternatives"][0]["resources"] = []
    for rule in data["machines"][1]["setups"]:
        if rule["to_family"] == "b":
            rule.update(duration_min=2, resources=[{"resource_id": "crew", "quantity": 1}])
    assert _run(data).status == SolverStatus.INFEASIBLE
    data["resources"][0]["capacity"] = 2
    assert _run(data).status == SolverStatus.OPTIMAL


def test_bom_at_later_route_step_consumes_at_processing_not_setup():
    data = tiny_snapshots()["handoff"].model_dump(mode="json")
    data["lots"] = [data["lots"][1]]
    operation = copy.deepcopy(data["lots"][0]["operations"][0])
    operation["operation_id"] = "finish_later"
    data["lots"][0]["operations"].append(operation)
    data["items"][1]["bom"][0]["step_index"] = 1
    data["supplies"] = [{"supply_id": "s", "item_id": "component", "quantity": 2, "at_min": 3}]
    data["machines"][0]["setups"][0]["duration_min"] = 1
    result = _run(data)
    assert result.status == SolverStatus.OPTIMAL
    slots = {slot.operation_id: slot for slot in result.plan.slots}
    assert slots["make_finished"].end_min == slots["finish_later"].setup_start_min == 2
    assert slots["finish_later"].start_min == 3 and slots["finish_later"].end_min == 4
