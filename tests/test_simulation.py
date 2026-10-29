"""Hand-checked event execution, not comparisons with copied candidate metrics."""

from __future__ import annotations

import json

import pytest

from scheduling_agent.compiler import build_scenario, compile_problem
from scheduling_agent.domain import (
    MAX_ITEMS,
    MAX_REMAINING_OPERATIONS,
    MAX_SUPPLIES,
    ObjectiveBreakdown,
    Plan,
    Scenario,
    Slot,
    Snapshot,
    canonical_digest,
)
from scheduling_agent.errors import DomainError
from scheduling_agent.simulation import (
    MAX_DISRUPTIONS,
    MAX_EVENTS,
    MAX_MATERIAL_POINTS,
    MAX_RESOURCE_SEGMENTS,
    simulate,
)
from scheduling_agent.validator import validate_plan


def _machine(machine_id, family, *, setup=0, horizon=8, setup_resources=()):
    return {
        "machine_id": machine_id, "initial_family": "idle", "qualified_families": [family],
        "calendar": [{"start_min": 0, "end_min": horizon}],
        "setups": [
            {"from_family": "idle", "to_family": family, "duration_min": setup,
             "resources": setup_resources},
            {"from_family": family, "to_family": family, "duration_min": 0, "resources": []},
        ],
    }


def _operation(operation_id, family, machine_id, *, duration=2, resources=()):
    return {"operation_id": operation_id, "family": family, "alternatives": [
        {"machine_id": machine_id, "duration_min": duration,
         "resources": resources, "qualified": True},
    ]}


def _lot(lot_id, item_id, operation, *, quantity=1):
    return {"lot_id": lot_id, "item_id": item_id, "quantity": quantity,
            "release_min": 0, "operations": [operation], "completed_prefix": 0, "residual": None}


def _order(order_id, lot_id, item_id, *, due=4, priority=1, quantity=1):
    return {"order_id": order_id, "lot_id": lot_id, "item_id": item_id,
            "quantity": quantity, "due_min": due, "priority": priority}


def _source(*, items, materials, machines, lots, orders=(), supplies=(), resources=(), horizon=8):
    return Snapshot.model_validate({
        "snapshot_id": "synthetic-events", "plant_id": "synthetic-plant", "revision": 1,
        "observed_at_ms": 1_800_000_000_000, "horizon_min": horizon,
        "items": items, "materials": materials, "machines": machines, "resources": resources,
        "lots": lots, "orders": orders, "supplies": supplies,
        "freeze_until_min": 0, "commitments": [],
        "objective": {"weighted_tardiness": 1, "setup_minutes": 1,
                      "makespan": 1, "load_spread": 1},
    })


def _slot(operation_id, machine_id, setup, start, end, predecessor=None):
    return Slot(operation_id=operation_id, machine_id=machine_id, setup_start_min=setup,
                start_min=start, end_min=end, predecessor_id=predecessor)


def _plan(problem, slots):
    # This test-only constructor computes bookkeeping for explicitly hand-picked
    # intervals. No solver selects the expected execution schedule.
    slots = tuple(slots)
    by_operation = {slot.operation_id: slot for slot in slots}
    lots = {lot.lot_id: lot for lot in problem.snapshot.lots}
    tardiness = sum(
        order.priority * max(0, by_operation[lots[order.lot_id].operations[-1].operation_id].end_min
                             - order.due_min)
        for order in problem.snapshot.orders
    )
    setup = sum(slot.start_min - slot.setup_start_min for slot in slots)
    makespan = max((slot.end_min for slot in slots), default=0)
    loads = [sum(slot.end_min - slot.setup_start_min for slot in slots
                 if slot.machine_id == machine.machine_id) for machine in problem.snapshot.machines]
    spread = max(loads) - min(loads)
    weights = problem.snapshot.objective
    objective = ObjectiveBreakdown(
        weighted_tardiness=tardiness, setup_minutes=setup, makespan=makespan, load_spread=spread,
        total=tardiness * weights.weighted_tardiness + setup * weights.setup_minutes
        + makespan * weights.makespan + spread * weights.load_spread,
    )
    return Plan(plan_id="hand-picked", snapshot_hash=problem.snapshot_hash,
                problem_hash=problem.problem_hash, objective_hash=problem.objective_hash,
                slots=slots, objective=objective)


def _chain():
    tool = [{"resource_id": "tool", "quantity": 1}]
    crew = [{"resource_id": "crew", "quantity": 1}]
    snapshot = _source(
        items=[{"item_id": "raw", "unit": "piece", "bom": []},
               {"item_id": "component", "unit": "piece", "bom": [
                   {"component_item_id": "raw", "step_index": 0, "units_per_parent": 2}]},
               {"item_id": "product", "unit": "piece", "bom": [
                   {"component_item_id": "component", "step_index": 0, "units_per_parent": 1}]}],
        materials=[{"item_id": key, "initial_stock": 0} for key in ("raw", "component", "product")],
        machines=[_machine("m1", "C", setup=1, setup_resources=crew),
                  _machine("m2", "F", setup=1, setup_resources=crew)],
        resources=[{"resource_id": key, "capacity": 1, "qualified_families": ["C", "F"],
                    "calendar": [{"start_min": 0, "end_min": 8}]} for key in ("tool", "crew")],
        lots=[_lot("component-lot", "component", _operation("cut", "C", "m1", resources=tool)),
              _lot("product-lot", "product", _operation("assemble", "F", "m2", resources=tool))],
        supplies=[{"supply_id": "raw-delivery", "item_id": "raw", "quantity": 2, "at_min": 1}],
        orders=[_order("customer", "product-lot", "product", priority=3)],
    )
    problem = compile_problem(snapshot)
    plan = _plan(problem, [_slot("cut", "m1", 0, 1, 3), _slot("assemble", "m2", 2, 3, 5)])
    return problem, plan


def _parallel(*, supply=True):
    snapshot = _source(
        items=[{"item_id": "raw", "unit": "piece", "bom": []},
               {"item_id": "product", "unit": "piece", "bom": [
                   {"component_item_id": "raw", "step_index": 0, "units_per_parent": 1}]}],
        materials=[{"item_id": "raw", "initial_stock": 1},
                   {"item_id": "product", "initial_stock": 0}],
        machines=[_machine("m1", "F"), _machine("m2", "F")],
        lots=[_lot("lot1", "product", _operation("op1", "F", "m1")),
              _lot("lot2", "product", _operation("op2", "F", "m2"))],
        orders=[_order("o1", "lot1", "product"), _order("o2", "lot2", "product")],
        supplies=[{"supply_id": "s1", "item_id": "raw", "quantity": 1, "at_min": 0}]
        if supply else [],
    )
    problem = compile_problem(snapshot)
    return problem, _plan(problem, [_slot("op1", "m1", 0, 0, 2), _slot("op2", "m2", 0, 0, 2)])


def _kinds(result, minute):
    return [value["kind"] for value in result["events"] if value["at_min"] == minute]


def test_nominal_handcrafted_events_resources_and_actual_metrics():
    problem, plan = _chain()
    assert validate_plan(problem, plan).valid
    result = simulate(problem, plan)
    assert result["status"] == "completed"
    assert result["synthetic"] is True and result["causal_effect_proven"] is False
    assert result["source_hash"] == result["snapshot_hash"] == canonical_digest(problem.snapshot)
    assert result["problem_hash"] == problem.problem_hash
    assert result["plan_hash"] == canonical_digest(plan)
    assert result["simulation_hash"] == canonical_digest(
        {key: value for key, value in result.items() if key != "simulation_hash"}
    )
    assert [(e["at_min"], e["kind"]) for e in result["events"]] == [
        (0, "execution_started"), (0, "initial_stock"), (0, "initial_stock"), (0, "initial_stock"),
        (0, "setup_start"),
        (1, "setup_end"), (1, "supply_receipt"), (1, "process_input"), (1, "process_start"),
        (2, "setup_start"),
        (3, "process_end"), (3, "setup_end"), (3, "lot_output"),
        (3, "process_input"), (3, "process_start"),
        (5, "process_end"), (5, "lot_output"), (5, "order_delivery"), (5, "execution_completed"),
    ]
    assert result["final_inventory"] == {"component": 0, "product": 0, "raw": 0}
    assert [(e["at_min"], e["balance"]) for e in result["material_trace"]
            if e["item_id"] == "component"] == [(0, 0), (3, 1), (3, 0)]
    tool = [value for value in result["resource_usage"] if value["resource_id"] == "tool"]
    assert [(value["start_min"], value["end_min"], value["demand"]) for value in tool] == [(1, 3, 1), (3, 5, 1)]
    assert result["order_completions"] == [{
        "order_id": "customer", "lot_id": "product-lot", "item_id": "product", "quantity": 1,
        "completion_min": 5, "due_min": 4, "tardiness_min": 1, "priority": 3, "on_time": False,
    }]
    assert result["metrics"]["objective"] == {
        "weighted_tardiness": 3, "setup_minutes": 2, "makespan": 5, "load_spread": 0, "total": 10,
    }
    assert result["metrics"]["processing_minutes"] == 4
    assert result["metrics"]["machine_busy_minutes"] == {"m1": 3, "m2": 3}
    assert result["metrics"]["resource_peak_usage"] == {"crew": 1, "tool": 1}
    assert simulate(problem, plan) == result


def test_same_minute_receipts_before_all_withdrawals_and_delivery():
    problem, plan = _parallel()
    result = simulate(problem, plan)
    assert result["status"] == "completed"
    kinds = _kinds(result, 0)
    assert kinds.index("supply_receipt") < kinds.index("process_input")
    receipts_and_inputs = [value for value in result["material_trace"] if value["item_id"] == "raw"]
    assert [value["balance"] for value in receipts_and_inputs] == [1, 2, 1, 0]
    kinds = _kinds(result, 2)
    assert max(i for i, value in enumerate(kinds) if value == "lot_output") < kinds.index("order_delivery")
    assert len(result["order_completions"]) == 2


def test_invalid_stock_double_spend_rejected_before_execution():
    problem, plan = _parallel(supply=False)
    result = simulate(problem, plan)
    assert result["status"] == "blocked"
    assert result["blocked_reason"]["code"] == "INVALID_PLAN"
    assert "MATERIAL_NEGATIVE" in result["validation"]["violation_codes"]
    assert [value["kind"] for value in result["events"]] == ["validation_failed"]
    assert result["material_trace"] == result["resource_usage"] == result["order_completions"] == []
    assert result["metrics"]["execution_started"] is False


def test_changed_supply_aggregates_competing_orders_without_double_spend_or_healing():
    problem, plan = _parallel()
    result = simulate(problem, plan, disruptions=[{"kind": "supply_delay", "supply_id": "s1", "at_min": 1}])
    assert result["status"] == "blocked"
    assert result["executed_until_min"] == 0
    assert result["blocked_reason"] == {
        "code": "MATERIAL_NOT_READY", "at_min": 0, "operation_ids": ["op1", "op2"],
        "shortages": [{"item_id": "raw", "required": 2, "available": 1}],
    }
    assert result["final_inventory"]["raw"] == 1
    assert not any(value["kind"] in {"process_input", "process_start", "supply_receipt", "execution_completed"}
                   for value in result["events"])
    assert result["order_completions"] == []
    assert result["metrics"]["objective"] is None


def test_late_supply_does_not_replay_the_nominal_objective():
    problem, plan = _chain()
    result = simulate(problem, plan, disruptions=(Scenario(kind="supply_delay", supply_id="raw-delivery", at_min=2),))
    assert result["status"] == "blocked" and result["executed_until_min"] == 1
    assert result["metrics"]["setup_minutes"] == 1
    assert result["metrics"]["processing_minutes"] == 0
    assert result["metrics"]["objective"] is None
    assert result["metrics"]["makespan"] is None
    assert result["metrics"]["completed_operations"] == 0
    assert result["resource_usage"][0]["end_min"] == 1


def test_active_outage_preserves_exact_partial_work_and_consumed_material():
    problem, plan = _chain()
    result = simulate(problem, plan, disruptions=[{"kind": "machine_outage", "machine_id": "m1", "start_min": 2, "end_min": 4}])
    assert result["status"] == "blocked" and result["executed_until_min"] == 2
    assert result["blocked_reason"]["code"] == "MACHINE_OUTAGE"
    assert result["blocked_reason"]["active_operation_ids"] == ["cut"]
    assert result["metrics"]["setup_minutes"] == 1
    assert result["metrics"]["processing_minutes"] == 1
    assert result["final_inventory"] == {"component": 0, "product": 0, "raw": 0}
    assert result["order_completions"] == []
    assert result["operation_states"]["cut"] == "processing"
    partial = [value for value in result["resource_usage"] if not value["completed"]]
    assert len(partial) == 2
    assert all((value["start_min"], value["end_min"], value["planned_end_min"]) == (1, 2, 3)
               for value in partial)
    assert result["events"][-1]["kind"] == "execution_blocked"
    assert not any(value["kind"] == "lot_output" for value in result["events"])


def test_outage_during_setup_retains_partial_setup_not_processing():
    problem, plan = _chain()
    payload = problem.snapshot.model_dump(mode="python")
    payload["machines"][0]["setups"][0]["duration_min"] = 2
    updated = compile_problem(Snapshot.model_validate(payload))
    candidate = _plan(updated, [_slot("cut", "m1", 0, 2, 4), _slot("assemble", "m2", 3, 4, 6)])
    result = simulate(updated, candidate, disruptions=[{"kind": "machine_outage", "machine_id": "m1", "start_min": 1, "end_min": 2}])
    assert result["status"] == "blocked"
    assert result["metrics"]["setup_minutes"] == 1
    assert result["metrics"]["processing_minutes"] == 0
    assert result["operation_states"]["cut"] == "setup"
    assert all(not value["completed"] and value["end_min"] == 1 for value in result["resource_usage"])


def test_future_frozen_assignment_outage_blocks_without_moving_it():
    problem, plan = _chain()
    source = problem.snapshot.model_copy(update={"freeze_until_min": 4, "commitments": plan.slots})
    problem = compile_problem(source)
    plan = _plan(problem, plan.slots)
    result = simulate(problem, plan, disruptions=[{"kind": "machine_outage", "machine_id": "m2", "start_min": 1, "end_min": 4}])
    assert result["status"] == "blocked" and result["executed_until_min"] == 1
    assert result["blocked_reason"]["operation_ids"] == ["assemble"]
    assert result["blocked_reason"]["active_operation_ids"] == []
    assert result["operation_states"]["assemble"] == "pending"
    assert not any(value.get("operation_id") == "assemble" for value in result["resource_usage"])
    assert result["plan_hash"] == canonical_digest(plan)


def test_prior_delivery_survives_a_later_unplanned_outage():
    problem, plan = _parallel()
    plan = _plan(problem, [_slot("op1", "m1", 0, 0, 2), _slot("op2", "m2", 3, 3, 5)])
    result = simulate(problem, plan, disruptions=[{"kind": "machine_outage", "machine_id": "m2", "start_min": 4, "end_min": 6}])
    assert result["status"] == "blocked"
    assert result["metrics"]["completed_operations"] == 1
    assert result["metrics"]["delivered_orders"] == 1
    assert result["order_completions"][0]["order_id"] == "o1"
    assert result["order_completions"][0]["completion_min"] == 2
    assert result["metrics"]["machine_busy_minutes"] == {"m1": 2, "m2": 1}
    assert result["metrics"]["objective"] is None


@pytest.mark.parametrize("machine,start,end", [("m1", 3, 5), ("m2", 0, 2)])
def test_half_open_outage_boundary_does_not_block_touching_assignment(machine, start, end):
    problem, plan = _chain()
    result = simulate(problem, plan, disruptions=[{"kind": "machine_outage", "machine_id": machine, "start_min": start, "end_min": end}])
    assert result["status"] == "completed"
    assert result["metrics"]["processing_minutes"] == 4


def test_touching_machine_calendar_windows_are_not_a_closed_minute():
    problem, plan = _chain()
    payload = problem.snapshot.model_dump(mode="python")
    payload["machines"][0]["calendar"] = ({"start_min": 0, "end_min": 2}, {"start_min": 2, "end_min": 8})
    updated = compile_problem(Snapshot.model_validate(payload))
    result = simulate(updated, _plan(updated, plan.slots))
    assert result["status"] == "completed"


@pytest.mark.parametrize("change,code", [("calendar", "MACHINE_CALENDAR"), ("qualification", "MACHINE_QUALIFICATION"), ("resource", "RESOURCE_QUALIFICATION")])
def test_nominal_invalid_source_requirements_rejected_independently(change, code):
    problem, plan = _chain()
    payload = problem.snapshot.model_dump(mode="python")
    if change == "calendar":
        payload["machines"][0]["calendar"] = ({"start_min": 0, "end_min": 2},)
    elif change == "qualification":
        payload["lots"][0]["operations"][0]["alternatives"][0]["qualified"] = False
    else:
        payload["resources"][0]["qualified_families"] = ("F",)
    updated = compile_problem(Snapshot.model_validate(payload))
    result = simulate(updated, _plan(updated, plan.slots))
    assert result["status"] == "blocked"
    assert code in result["validation"]["violation_codes"]
    assert result["metrics"]["execution_started"] is False


def test_modified_objective_and_changed_source_hash_are_not_trusted():
    problem, plan = _chain()
    forged = plan.model_copy(update={"objective": plan.objective.model_copy(update={"total": 0})})
    result = simulate(problem, forged)
    assert result["status"] == "blocked" and result["metrics"]["execution_started"] is False
    changed = compile_problem(build_scenario(problem.snapshot, Scenario(kind="supply_delay", supply_id="raw-delivery", at_min=2)))
    result = simulate(changed, plan)
    assert result["status"] == "blocked"
    assert "PLAN_BINDING" in result["validation"]["violation_codes"]


def test_residual_and_completed_prefix_do_not_consume_or_produce_twice():
    snapshot = _source(
        items=[{"item_id": "raw", "unit": "piece", "bom": []},
               {"item_id": "product", "unit": "piece", "bom": [
                   {"component_item_id": "raw", "step_index": 0, "units_per_parent": 1},
                   {"component_item_id": "raw", "step_index": 1, "units_per_parent": 2}]}],
        materials=[{"item_id": "raw", "initial_stock": 0}, {"item_id": "product", "initial_stock": 0}],
        machines=[_machine("m1", "F")],
        lots=[{"lot_id": "lot", "item_id": "product", "quantity": 1, "release_min": 0,
               "operations": [_operation("old", "F", "m1"), _operation("residual", "F", "m1", duration=6)],
               "completed_prefix": 1, "residual": {"machine_id": "m1", "remaining_min": 2}}],
        orders=[_order("o", "lot", "product")],
    )
    problem = compile_problem(snapshot)
    result = simulate(problem, _plan(problem, [_slot("residual", "m1", 0, 0, 2)]))
    assert result["status"] == "completed"
    assert not any(value["kind"] in {"setup_start", "setup_end", "process_input"} for value in result["events"])
    assert not any(value.get("operation_id") == "old" for value in result["events"])
    assert result["metrics"]["processing_minutes"] == 2
    assert result["metrics"]["delivered_orders"] == 1
    assert result["final_inventory"] == {"product": 0, "raw": 0}


@pytest.mark.parametrize("disruptions", [
    {"kind": "machine_outage", "machine_id": "m1", "start_min": 1, "end_min": 2},
    ["outage"],
    [{"kind": "baseline"}],
    [{"kind": "machine_outage", "machine_id": "unknown", "start_min": 1, "end_min": 2}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": True, "end_min": 2}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 1.0, "end_min": 2}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 1, "end_min": 9}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 2, "end_min": 2}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 1, "end_min": 2, "production": True}],
    [{"kind": "supply_delay", "supply_id": "raw-delivery", "at_min": 1}],
    [{"kind": "supply_delay", "supply_id": "raw-delivery", "at_min": 9}],
    [{"kind": "supply_delay", "supply_id": "unknown", "at_min": 2}],
    [{"kind": "supply_delay", "supply_id": "raw-delivery", "at_min": 2},
     {"kind": "supply_delay", "supply_id": "raw-delivery", "at_min": 3}],
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 3, "end_min": 4}] * 2,
    [{"kind": "machine_outage", "machine_id": "m1", "start_min": 3, "end_min": 4}] * (MAX_DISRUPTIONS + 1),
])
def test_disruptions_are_strict_structured_and_bounded(disruptions):
    problem, plan = _chain()
    with pytest.raises(DomainError, match=".") as exc:
        simulate(problem, plan, disruptions=disruptions)
    assert exc.value.code == "INVALID_DISRUPTION"


def test_no_production_flag_or_executor_input_is_accepted():
    problem, plan = _chain()
    with pytest.raises(TypeError):
        simulate(problem, plan, production=True)
    with pytest.raises(TypeError):
        simulate(problem, plan, synthetic=False)
    with pytest.raises(TypeError):
        simulate(problem, plan, executor="remote")


def test_insert_order_requires_scenario_compilation_not_execution():
    problem, plan = _chain()
    scenario = Scenario.model_validate({
        "kind": "insert_order", "lot": _lot("new-lot", "product", _operation("new-op", "F", "m2")),
        "order": _order("new-order", "new-lot", "product"),
    })
    with pytest.raises(DomainError) as exc:
        simulate(problem, plan, disruptions=[scenario])
    assert exc.value.code == "INVALID_DISRUPTION"


def test_compiled_changed_supply_is_executed_after_an_explicit_new_solve():
    from scheduling_agent.solver import solve

    problem, _ = _chain()
    snapshot = build_scenario(problem.snapshot, Scenario(kind="supply_delay", supply_id="raw-delivery", at_min=2))
    changed = compile_problem(snapshot)
    solved = solve(changed, time_limit_s=1.0)
    assert solved.plan is not None
    result = simulate(changed, solved.plan)
    assert result["status"] == "completed"
    receipt = next(value for value in result["events"] if value["kind"] == "supply_receipt")
    assert receipt["at_min"] == 2
    assert result["snapshot_hash"] != problem.snapshot_hash


def test_events_are_sorted_bounded_and_disruptions_have_canonical_order():
    problem, plan = _chain()
    disruptions = [Scenario(kind="machine_outage", machine_id="m1", start_min=3, end_min=4),
                   Scenario(kind="machine_outage", machine_id="m2", start_min=0, end_min=2)]
    result = simulate(problem, plan, disruptions=disruptions)
    assert result == simulate(problem, plan, disruptions=list(reversed(disruptions)))
    assert len(result["events"]) <= result["event_limit"] == MAX_EVENTS
    assert len(result["material_trace"]) <= MAX_MATERIAL_POINTS
    assert len(result["resource_usage"]) <= MAX_RESOURCE_SEGMENTS
    assert [value["at_min"] for value in result["events"]] == sorted(value["at_min"] for value in result["events"])
    assert [value["sequence"] for value in result["events"]] == list(range(len(result["events"])))
    assert all(0 <= value["at_min"] <= problem.snapshot.horizon_min for value in result["events"])
    assert len(json.dumps(result)) < 100_000


def test_domain_caps_bound_a_large_real_execution_without_truncating():
    raw_items = [f"raw{i}" for i in range(MAX_ITEMS - 1)]
    bom = [{"component_item_id": key, "step_index": index, "units_per_parent": 1}
           for index in (0, 1) for key in raw_items]
    # Twelve two-step lots reach the 24-operation cap, with nineteen
    # material inputs for each operation and the full external-supply cap.
    lots = [
        {**_lot(f"lot{i}", "product", _operation(f"op{i}", "F", "m1", duration=1)),
         "operations": [_operation(f"op{i}", "F", "m1", duration=1),
                        _operation(f"extra{i}", "F", "m1", duration=1)]}
        for i in range(MAX_REMAINING_OPERATIONS // 2)
    ]
    snapshot = _source(
        items=[{"item_id": key, "unit": "piece", "bom": []} for key in raw_items]
        + [{"item_id": "product", "unit": "piece", "bom": bom}],
        materials=[{"item_id": key, "initial_stock": MAX_REMAINING_OPERATIONS} for key in raw_items]
        + [{"item_id": "product", "initial_stock": 0}],
        machines=[_machine("m1", "F", horizon=100)], lots=lots, horizon=100,
        supplies=[{"supply_id": f"s{i}", "item_id": "raw0", "quantity": 1, "at_min": i}
                  for i in range(MAX_SUPPLIES)],
    )
    problem = compile_problem(snapshot)
    slots = []
    previous = None
    for index, operation in enumerate(problem.operations):
        slots.append(_slot(operation.operation_id, "m1", index, index, index + 1, previous))
        previous = operation.operation_id
    result = simulate(problem, _plan(problem, slots))
    assert result["status"] == "completed"
    assert result["metrics"]["completed_operations"] == MAX_REMAINING_OPERATIONS
    assert sum(value["kind"] == "process_input" for value in result["events"]) == 24 * 19
    assert sum(value["kind"] == "supply_receipt" for value in result["events"]) == MAX_SUPPLIES
    assert len(result["events"]) <= MAX_EVENTS
    assert len(result["material_trace"]) <= MAX_MATERIAL_POINTS
    assert len(result["resource_usage"]) <= MAX_RESOURCE_SEGMENTS
    assert result["events"][-1]["kind"] == "execution_completed"
    assert result["executed_until_min"] == 63
    assert result["metrics"]["makespan"] == 24


def test_all_demo_scenarios_execute_only_freshly_solved_candidates():
    from scheduling_agent.fixtures import demo_scenarios, demo_snapshots
    from scheduling_agent.solver import solve

    snapshot = demo_snapshots(1_800_000_000_000)["normal"]
    completed_scenarios = set()
    for name, scenario in demo_scenarios(snapshot).items():
        problem = compile_problem(build_scenario(snapshot, scenario))
        solved = solve(problem, time_limit_s=1.0)
        if solved.plan is None:
            assert solved.status.value == "INFEASIBLE"
            continue
        result = simulate(problem, solved.plan)
        assert result["status"] == "completed", (name, result["blocked_reason"])
        assert result["metrics"]["objective"] == validate_plan(problem, solved.plan).objective.model_dump()
        assert result["metrics"]["delivered_orders"] == len(problem.snapshot.orders)
        assert all(value["balance"] >= 0 for value in result["material_trace"])
        completed_scenarios.add(name)
    assert {"baseline", "insert_order", "supply_delay"} <= completed_scenarios


def test_invalid_candidate_still_rejects_invalid_disruption_payload():
    problem, plan = _parallel(supply=False)
    with pytest.raises(DomainError) as exc:
        simulate(problem, plan, disruptions=[{"kind": "baseline", "production": True}])
    assert exc.value.code == "INVALID_DISRUPTION"


def test_invalid_candidate_retains_valid_disruption_binding_without_execution():
    problem, plan = _parallel(supply=False)
    outage = Scenario(kind="machine_outage", machine_id="m1", start_min=0, end_min=1)
    result = simulate(problem, plan, disruptions=[outage])
    assert result["status"] == "blocked"
    assert result["metrics"]["execution_started"] is False
    assert result["disruptions_hash"] == canonical_digest((outage,))
    assert result["disruptions"] == [outage.model_dump(mode="json", exclude_none=True)]


def test_residual_release_precedes_next_machine_acquisition_and_route_setup():
    snapshot = _source(
        items=[{"item_id": "product", "unit": "piece", "bom": []}],
        materials=[{"item_id": "product", "initial_stock": 0}],
        machines=[_machine("m1", "F")],
        lots=[{"lot_id": "lot", "item_id": "product", "quantity": 1, "release_min": 0,
               "operations": [_operation("residual", "F", "m1", duration=6),
                              _operation("finish", "F", "m1", duration=1)],
               "completed_prefix": 0, "residual": {"machine_id": "m1", "remaining_min": 2}}],
        orders=[_order("o", "lot", "product")],
    )
    problem = compile_problem(snapshot)
    plan = _plan(problem, [_slot("residual", "m1", 0, 0, 2), _slot("finish", "m1", 2, 2, 3, "residual")])
    result = simulate(problem, plan)
    assert result["status"] == "completed"
    assert _kinds(result, 2) == ["process_end", "setup_start", "setup_end", "process_start"]
    assert result["metrics"]["processing_minutes"] == 3
    assert sum(value["kind"] == "lot_output" for value in result["events"]) == 1


def test_empty_remaining_route_does_not_invent_new_receipts_or_orders():
    snapshot = _source(
        items=[{"item_id": "product", "unit": "piece", "bom": []}],
        materials=[{"item_id": "product", "initial_stock": 7}],
        machines=[_machine("m1", "F")],
        lots=[{**_lot("old-lot", "product", _operation("old", "F", "m1")),
               "completed_prefix": 1}],
    )
    problem = compile_problem(snapshot)
    result = simulate(problem, _plan(problem, []))
    assert result["status"] == "completed"
    assert result["final_inventory"] == {"product": 7}
    assert result["order_completions"] == result["resource_usage"] == []
    assert result["metrics"]["completed_operations"] == 0
    assert [value["kind"] for value in result["events"]] == ["execution_started", "initial_stock", "execution_completed"]
