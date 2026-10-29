"""Adversarial source-replay tests; no compiler or solver imports."""

from __future__ import annotations

from collections import defaultdict

import pytest

from scheduling_agent.domain import (
    CompiledOperation,
    CompiledProblem,
    ObjectiveBreakdown,
    Plan,
    Slot,
    Snapshot,
    canonical_digest,
)
from scheduling_agent.errors import DomainError
from scheduling_agent.validator import compare_plans, validate_plan


def operation(
    operation_id="op0", *, family="F", machine="M0", duration=2, demands=(), qualified=True
):
    return {
        "operation_id": operation_id,
        "family": family,
        "alternatives": [
            {
                "machine_id": machine,
                "duration_min": duration,
                "resources": demands,
                "qualified": qualified,
            }
        ],
    }


def lot(
    lot_id="L0", *, item="P", quantity=1, operations=None, release=0, completed=0, residual=None
):
    return {
        "lot_id": lot_id,
        "item_id": item,
        "quantity": quantity,
        "release_min": release,
        "operations": operations or [operation()],
        "completed_prefix": completed,
        "residual": residual,
    }


def snapshot(
    *,
    lots=None,
    orders=(),
    stocks=None,
    bom=(),
    supplies=(),
    setup=0,
    setup_demands=(),
    capacity=1,
    calendar=None,
    freeze=0,
    commitments=(),
    weights=None,
    horizon=12,
):
    stocks = {"P": 0, "C": 0} if stocks is None else stocks
    calendar = [{"start_min": 0, "end_min": horizon}] if calendar is None else calendar
    return Snapshot.model_validate(
        {
            "snapshot_id": "snapshot",
            "plant_id": "plant",
            "revision": 1,
            "observed_at_ms": 1_000,
            "horizon_min": horizon,
            "items": [
                {"item_id": item, "unit": "piece", "bom": bom if item == "P" else []}
                for item in stocks
            ],
            "materials": [
                {"item_id": item, "initial_stock": amount} for item, amount in stocks.items()
            ],
            "machines": [
                {
                    "machine_id": machine,
                    "initial_family": "F",
                    "qualified_families": ["F", "G"],
                    "calendar": calendar,
                    "setups": [
                        {
                            "from_family": source,
                            "to_family": target,
                            "duration_min": setup if source != target else 0,
                            "resources": setup_demands,
                        }
                        for source in ("F", "G")
                        for target in ("F", "G")
                    ],
                }
                for machine in ("M0", "M1")
            ],
            "resources": [
                {
                    "resource_id": "R0",
                    "capacity": capacity,
                    "qualified_families": ["F", "G"],
                    "calendar": calendar,
                }
            ],
            "lots": [lot()] if lots is None else lots,
            "orders": orders,
            "supplies": supplies,
            "freeze_until_min": freeze,
            "commitments": commitments,
            "objective": weights
            or {"weighted_tardiness": 3, "setup_minutes": 2, "makespan": 1, "load_spread": 1},
        }
    )


def reparse(source, **changes):
    return Snapshot.model_validate(source.model_dump(mode="python") | changes)


def problem_for(source):
    """Create transport records, not feasibility decisions, without the compiler."""
    items = {item.item_id: item for item in source.items}
    operations = []
    for batch in source.lots:
        for index in range(batch.completed_prefix, len(batch.operations)):
            op = batch.operations[index]
            residual = batch.residual if index == batch.completed_prefix else None
            operations.append(
                CompiledOperation(
                    operation_id=op.operation_id,
                    lot_id=batch.lot_id,
                    item_id=batch.item_id,
                    quantity=batch.quantity,
                    step_index=index,
                    family=op.family,
                    alternatives=op.alternatives,
                    release_min=batch.release_min,
                    predecessor_id=(
                        batch.operations[index - 1].operation_id
                        if index > batch.completed_prefix
                        else None
                    ),
                    is_terminal=index == len(batch.operations) - 1,
                    residual=residual,
                    consumes=tuple(
                        {
                            "item_id": line.component_item_id,
                            "quantity": batch.quantity * line.units_per_parent,
                        }
                        for line in items[batch.item_id].bom
                        if line.step_index == index and residual is None
                    ),
                )
            )
    payload = {
        "snapshot": source,
        "snapshot_hash": canonical_digest(source),
        "objective_hash": canonical_digest(source.objective),
        "compiler_version": "1",
        "operations": tuple(operations),
    }
    return CompiledProblem(**payload, problem_hash=canonical_digest(payload))


def slot(operation_id="op0", *, machine="M0", start=0, end=2, setup_start=None, previous=None):
    return Slot(
        operation_id=operation_id,
        machine_id=machine,
        setup_start_min=start if setup_start is None else setup_start,
        start_min=start,
        end_min=end,
        predecessor_id=previous,
    )


def plan_for(problem, slots, *, plan_id="candidate"):
    """Arithmetic of supplied times only; does not decide candidate feasibility."""
    slots = tuple(slots)
    by_id = {value.operation_id: value for value in slots}
    lots = {batch.lot_id: batch for batch in problem.snapshot.lots}
    tardiness = sum(
        order.priority
        * max(
            0, by_id[lots[order.lot_id].operations[-1].operation_id].end_min - (order.due_min or 0)
        )
        for order in problem.snapshot.orders
        if lots[order.lot_id].operations[-1].operation_id in by_id
    )
    setup = sum(value.start_min - value.setup_start_min for value in slots)
    makespan = max((value.end_min for value in slots), default=0)
    loads = defaultdict(int, {machine.machine_id: 0 for machine in problem.snapshot.machines})
    for value in slots:
        loads[value.machine_id] += value.end_min - value.setup_start_min
    spread = max(loads.values()) - min(loads.values())
    weights = problem.snapshot.objective
    objective = ObjectiveBreakdown(
        weighted_tardiness=tardiness,
        setup_minutes=setup,
        makespan=makespan,
        load_spread=spread,
        total=(
            weights.weighted_tardiness * tardiness
            + weights.setup_minutes * setup
            + weights.makespan * makespan
            + weights.load_spread * spread
        ),
    )
    return Plan(
        plan_id=plan_id,
        snapshot_hash=problem.snapshot_hash,
        problem_hash=problem.problem_hash,
        objective_hash=problem.objective_hash,
        slots=slots,
        objective=objective,
    )


def codes(problem, plan):
    return {violation.code for violation in validate_plan(problem, plan).violations}


def order(lot_id="L0", *, item="P", quantity=1, due=1, priority=2, order_id="O0"):
    return {
        "order_id": order_id,
        "lot_id": lot_id,
        "item_id": item,
        "quantity": quantity,
        "due_min": due,
        "priority": priority,
    }


def test_exact_objective_and_audit_bindings_without_optimality_claim():
    source = snapshot(lots=[lot(operations=[operation(family="G")])], orders=[order()], setup=1)
    problem = problem_for(source)
    candidate = plan_for(problem, [slot(start=1, end=3, setup_start=0)])
    report = validate_plan(problem, candidate)
    assert report.valid, report.violations
    assert report.objective.model_dump() == {
        "weighted_tardiness": 4,
        "setup_minutes": 1,
        "makespan": 3,
        "load_spread": 3,
        "total": 20,
    }
    assert report.plan_hash == canonical_digest(candidate)
    assert report.snapshot_hash == canonical_digest(source)
    terminal = [point for point in report.inventory if point.item_id == "P" and point.at_min == 3]
    assert [(point.received, point.consumed, point.balance) for point in terminal] == [(1, 1, 0)]
    assert [
        (event.kind, event.start_min, event.end_min)
        for event in report.activities
        if event.resource_kind == "machine"
    ] == [("setup", 0, 1), ("processing", 1, 3)]
    assert "optimal" not in report.model_dump()


@pytest.mark.parametrize("bad_slots", [[], [slot(), slot()], [slot("unknown")]])
def test_exact_pending_operation_coverage(bad_slots):
    problem = problem_for(snapshot())
    assert "OPERATION_COVERAGE" in codes(problem, plan_for(problem, bad_slots))


@pytest.mark.parametrize("field", ["snapshot_hash", "problem_hash", "objective_hash"])
def test_stale_plan_binding(field):
    problem = problem_for(snapshot())
    candidate = plan_for(problem, [slot()]).model_copy(update={field: "0" * 64})
    assert "PLAN_BINDING" in codes(problem, candidate)


def test_mutated_snapshot_and_compiled_hash_are_rejected():
    problem = problem_for(snapshot())
    candidate = plan_for(problem, [slot()])
    changed_source = reparse(problem.snapshot, revision=2)
    mutated = problem.model_copy(update={"snapshot": changed_source})
    assert {"SNAPSHOT_HASH", "PROBLEM_HASH", "PLAN_BINDING"} <= codes(mutated, candidate)
    changed_op = problem.operations[0].model_copy(update={"quantity": 2})
    mutated = problem.model_copy(update={"operations": (changed_op,)})
    assert {"PROBLEM_HASH", "COMPILED_SOURCE"} <= codes(mutated, candidate)
    payload = mutated.model_dump(exclude={"problem_hash"})
    mutated = mutated.model_copy(update={"problem_hash": canonical_digest(payload)})
    assert "COMPILED_SOURCE" in codes(mutated, plan_for(mutated, [slot()]))


def test_rehash_cannot_launder_removed_bom_consumption_flag():
    source = snapshot(bom=[{"component_item_id": "C", "step_index": 0, "units_per_parent": 2}])
    problem = problem_for(source)
    changed = problem.operations[0].model_copy(update={"consumes": ()})
    payload = problem.model_dump(exclude={"problem_hash"}) | {"operations": (changed,)}
    mutated = CompiledProblem(**payload, problem_hash=canonical_digest(payload))
    violations = codes(mutated, plan_for(mutated, [slot()]))
    assert {"COMPILED_SOURCE", "MATERIAL_NEGATIVE"} <= violations


def test_plan_hash_changes_for_slot_mutation_even_with_same_plan_id():
    problem = problem_for(snapshot())
    left = plan_for(problem, [slot()])
    right = plan_for(problem, [slot(start=1, end=3)])
    assert left.plan_id == right.plan_id
    assert validate_plan(problem, left).plan_hash != validate_plan(problem, right).plan_hash


@pytest.mark.parametrize("value", [True, 0.0, "0"])
def test_bypassed_strict_slot_schema_is_not_trusted(value):
    problem = problem_for(snapshot())
    malformed = slot().model_copy(update={"start_min": value})
    candidate = plan_for(problem, [slot()]).model_copy(update={"slots": (malformed,)})
    assert "INVALID_STRUCTURE" in codes(problem, candidate)


def test_whole_lot_consumption_and_intermediate_negative_even_if_final_recovers():
    source = snapshot(
        lots=[lot(quantity=2)],
        stocks={"P": 0, "C": 3},
        bom=[{"component_item_id": "C", "step_index": 0, "units_per_parent": 2}],
        supplies=[{"supply_id": "S0", "item_id": "C", "quantity": 3, "at_min": 4}],
    )
    problem = problem_for(source)
    report = validate_plan(problem, plan_for(problem, [slot()]))
    assert "MATERIAL_NEGATIVE" in {value.code for value in report.violations}
    assert [
        (value.at_min, value.balance) for value in report.inventory if value.item_id == "C"
    ] == [
        (0, 3),
        (0, -1),
        (4, 2),
    ]


def test_simultaneous_double_spend_is_aggregated():
    source = snapshot(
        lots=[lot(), lot("L1", operations=[operation("op1", machine="M1")])],
        stocks={"P": 0, "C": 1},
        bom=[{"component_item_id": "C", "step_index": 0, "units_per_parent": 1}],
    )
    problem = problem_for(source)
    report = validate_plan(problem, plan_for(problem, [slot(), slot("op1", machine="M1")]))
    assert not report.valid
    assert any(
        point.item_id == "C" and point.consumed == 2 and point.balance == -1
        for point in report.inventory
    )


def test_receipts_before_consumption_at_exact_same_minute():
    source = snapshot(
        lots=[
            lot(),
            lot("component", item="C", operations=[operation("component_op", machine="M1")]),
        ],
        bom=[{"component_item_id": "C", "step_index": 0, "units_per_parent": 1}],
    )
    problem = problem_for(source)
    candidate = plan_for(problem, [slot(start=2, end=4), slot("component_op", machine="M1")])
    assert validate_plan(problem, candidate).valid


def test_order_delivery_prevents_using_already_delivered_component():
    source = snapshot(
        lots=[
            lot(),
            lot("component", item="C", operations=[operation("component_op", machine="M1")]),
        ],
        orders=[order("component", item="C", due=12)],
        bom=[{"component_item_id": "C", "step_index": 0, "units_per_parent": 1}],
    )
    problem = problem_for(source)
    candidate = plan_for(problem, [slot(start=2, end=4), slot("component_op", machine="M1")])
    assert "MATERIAL_NEGATIVE" in codes(problem, candidate)


def test_only_route_terminal_produces_finished_material():
    source = snapshot(
        lots=[
            lot(operations=[operation("a"), operation("b")]),
            lot("C_lot", item="C", operations=[operation("c", machine="M1")]),
        ]
    )
    data = source.model_dump()
    data["items"][1]["bom"] = [{"component_item_id": "P", "step_index": 0, "units_per_parent": 1}]
    source = Snapshot.model_validate(data)
    problem = problem_for(source)
    candidate = plan_for(
        problem,
        [
            slot("a"),
            slot("b", start=4, end=6, previous="a"),
            slot("c", machine="M1", start=2, end=4),
        ],
    )
    report = validate_plan(problem, candidate)
    assert "MATERIAL_NEGATIVE" in {v.code for v in report.violations}
    assert [
        (point.at_min, point.received)
        for point in report.inventory
        if point.item_id == "P" and point.received
    ] == [(6, 1)]


def test_completed_prefix_and_residual_do_not_reconsume_historical_material():
    source = snapshot(
        lots=[
            lot(
                operations=[operation("done"), operation("running", duration=6), operation("last")],
                completed=1,
                residual={"machine_id": "M0", "remaining_min": 2},
            )
        ],
        bom=[
            {"component_item_id": "C", "step_index": 0, "units_per_parent": 2},
            {"component_item_id": "C", "step_index": 1, "units_per_parent": 3},
        ],
    )
    problem = problem_for(source)
    candidate = plan_for(
        problem, [slot("running"), slot("last", start=2, end=4, previous="running")]
    )
    report = validate_plan(problem, candidate)
    assert report.valid, report.violations
    assert not any(point.consumed for point in report.inventory if point.item_id == "C")
    moved = plan_for(
        problem, [slot("running", start=1, end=3), slot("last", start=3, end=5, previous="running")]
    )
    assert "RESIDUAL_FROZEN" in codes(problem, moved)
    historical = plan_for(problem, [slot("done"), *candidate.slots])
    assert "OPERATION_COVERAGE" in codes(problem, historical)


def test_residual_commitment_is_frozen_even_with_zero_freeze_window():
    source = snapshot(
        lots=[
            lot(
                operations=[operation(duration=6)],
                residual={"machine_id": "M0", "remaining_min": 2},
            )
        ],
        commitments=[slot(start=1, end=3)],
        freeze=0,
    )
    problem = problem_for(source)
    # The contradictory commitment is valid source input, but cannot be erased.
    assert "FROZEN_SLOT" in codes(problem, plan_for(problem, [slot()]))


def test_fully_completed_lot_has_no_future_production_or_slots():
    problem = problem_for(snapshot(lots=[lot(completed=1)], stocks={"P": 1, "C": 0}))
    report = validate_plan(problem, plan_for(problem, []))
    assert report.valid
    assert report.objective.total == 0
    assert not any(point.received or point.consumed for point in report.inventory)


@pytest.mark.parametrize(
    "candidate_slot, expected",
    [
        (slot(end=3), "DURATION"),
        (slot(machine="M1"), "ALTERNATIVE"),
        (slot(start=11, end=13), "HORIZON"),
    ],
)
def test_processing_constraints(candidate_slot, expected):
    problem = problem_for(snapshot())
    assert expected in codes(problem, plan_for(problem, [candidate_slot]))


def test_explicit_false_and_machine_resource_qualifications():
    source = snapshot(lots=[lot(operations=[operation(qualified=False)])])
    problem = problem_for(source)
    assert "MACHINE_QUALIFICATION" in codes(problem, plan_for(problem, [slot()]))
    data = snapshot().model_dump()
    data["machines"][0]["qualified_families"] = []
    problem = problem_for(Snapshot.model_validate(data))
    assert "MACHINE_QUALIFICATION" in codes(problem, plan_for(problem, [slot()]))
    data = snapshot(
        lots=[lot(operations=[operation(demands=[{"resource_id": "R0", "quantity": 1}])])]
    ).model_dump()
    data["resources"][0]["qualified_families"] = []
    problem = problem_for(Snapshot.model_validate(data))
    assert "RESOURCE_QUALIFICATION" in codes(problem, plan_for(problem, [slot()]))


def test_empty_alternatives_is_known_infeasibility_not_missing():
    data = snapshot().model_dump()
    data["lots"][0]["operations"][0]["alternatives"] = []
    problem = problem_for(Snapshot.model_validate(data))
    violations = codes(problem, plan_for(problem, [slot()]))
    assert "ALTERNATIVE" in violations
    assert "MISSING_INPUT" not in violations


def test_release_applies_to_setup_not_just_processing():
    source = snapshot(lots=[lot(operations=[operation(family="G")], release=1)], setup=1)
    problem = problem_for(source)
    assert "RELEASE" in codes(problem, plan_for(problem, [slot(start=1, end=3, setup_start=0)]))


def test_route_cannot_start_setup_before_previous_processing_finishes():
    source = snapshot(
        lots=[
            lot(operations=[operation("a", duration=3), operation("b", family="G", machine="M1")])
        ],
        setup=1,
    )
    problem = problem_for(source)
    candidate = plan_for(
        problem, [slot("a", end=3), slot("b", machine="M1", start=3, end=5, setup_start=2)]
    )
    assert "ROUTE_PRECEDENCE" in codes(problem, candidate)


def test_true_adjacent_setup_not_all_pairs_and_no_idle_inside_setup():
    source = snapshot(
        lots=[
            lot(operations=[operation("a", family="G"), operation("b", family="G"), operation("c")])
        ],
        setup=1,
    )
    problem = problem_for(source)
    candidate = plan_for(
        problem,
        [
            slot("a", start=1, end=3, setup_start=0),
            slot("b", start=4, end=6, previous="a"),
            slot("c", start=7, end=9, setup_start=6, previous="b"),
        ],
    )
    report = validate_plan(problem, candidate)
    assert report.valid, report.violations
    assert report.objective.setup_minutes == 2
    gap = plan_for(
        problem,
        [
            slot("a", start=2, end=4, setup_start=0),
            slot("b", start=4, end=6, previous="a"),
            candidate.slots[-1],
        ],
    )
    assert "SETUP_DURATION" in codes(problem, gap)
    bad_arc = candidate.model_copy(
        update={
            "slots": (
                candidate.slots[0],
                candidate.slots[1],
                candidate.slots[2].model_copy(update={"predecessor_id": "a"}),
            )
        }
    )
    assert "MACHINE_PREDECESSOR" in codes(problem, bad_arc)


def test_processing_and_setup_calendar_checked_separately():
    calendar = [{"start_min": 0, "end_min": 1}, {"start_min": 2, "end_min": 12}]
    source = snapshot(lots=[lot(operations=[operation(family="G")])], setup=1, calendar=calendar)
    problem = problem_for(source)
    # Processing fits but immediately preceding setup crosses the closure.
    assert "MACHINE_CALENDAR" in codes(
        problem, plan_for(problem, [slot(start=2, end=4, setup_start=1)])
    )
    # Setup fits but processing crosses the closure.
    assert "MACHINE_CALENDAR" in codes(
        problem, plan_for(problem, [slot(start=1, end=3, setup_start=0)])
    )


def test_touching_calendar_windows_have_no_closed_time():
    problem = problem_for(
        snapshot(calendar=[{"start_min": 0, "end_min": 1}, {"start_min": 1, "end_min": 12}])
    )
    assert validate_plan(problem, plan_for(problem, [slot()])).valid


def test_setup_resources_require_calendar_and_target_qualification():
    source = snapshot(
        lots=[lot(operations=[operation(family="G")])],
        setup=1,
        setup_demands=[{"resource_id": "R0", "quantity": 1}],
    )
    data = source.model_dump()
    data["resources"][0]["qualified_families"] = ["F"]
    data["resources"][0]["calendar"] = [{"start_min": 1, "end_min": 12}]
    problem = problem_for(Snapshot.model_validate(data))
    violations = codes(problem, plan_for(problem, [slot(start=1, end=3, setup_start=0)]))
    assert {"RESOURCE_QUALIFICATION", "RESOURCE_CALENDAR"} <= violations


def test_unary_and_cumulative_sweeps_release_before_acquire_at_ties():
    demand = [{"resource_id": "R0", "quantity": 1}]
    source = snapshot(
        lots=[
            lot(operations=[operation("a", demands=demand)]),
            lot("L1", operations=[operation("b", machine="M1", demands=demand)]),
        ]
    )
    problem = problem_for(source)
    overlap = plan_for(problem, [slot("a"), slot("b", machine="M1", start=1, end=3)])
    assert "RESOURCE_CAPACITY" in codes(problem, overlap)
    touching = plan_for(problem, [slot("a"), slot("b", machine="M1", start=2, end=4)])
    assert validate_plan(problem, touching).valid
    source = snapshot(
        lots=[lot(operations=[operation("a")]), lot("L1", operations=[operation("b")])]
    )
    problem = problem_for(source)
    candidate = plan_for(problem, [slot("a"), slot("b", start=1, end=3, previous="a")])
    assert {"MACHINE_OVERLAP", "MACHINE_CAPACITY"} <= codes(problem, candidate)
    touching = plan_for(problem, [slot("a"), slot("b", start=2, end=4, previous="a")])
    assert validate_plan(problem, touching).valid


def test_resource_demand_quantities_and_setup_processing_share_same_pool():
    source = snapshot(
        lots=[
            lot(
                operations=[
                    operation("a", duration=3, demands=[{"resource_id": "R0", "quantity": 2}])
                ]
            ),
            lot("L1", operations=[operation("b", family="G", machine="M1")]),
        ],
        setup=1,
        setup_demands=[{"resource_id": "R0", "quantity": 1}],
        capacity=2,
    )
    problem = problem_for(source)
    candidate = plan_for(
        problem, [slot("a", end=3), slot("b", machine="M1", start=2, end=4, setup_start=1)]
    )
    assert "RESOURCE_CAPACITY" in codes(problem, candidate)


def test_frozen_fields_and_outage_cannot_be_repaired_by_moving():
    frozen = slot()
    problem = problem_for(snapshot(freeze=1, commitments=[frozen]))
    assert validate_plan(problem, plan_for(problem, [frozen])).valid
    assert "FROZEN_SLOT" in codes(problem, plan_for(problem, [slot(start=2, end=4)]))
    outage_source = reparse(
        problem.snapshot,
        machines=[
            machine.model_copy(update={"calendar": ()}) if machine.machine_id == "M0" else machine
            for machine in problem.snapshot.machines
        ],
    )
    outage = problem_for(outage_source)
    assert "MACHINE_CALENDAR" in codes(outage, plan_for(outage, [frozen]))
    assert "FROZEN_SLOT" in codes(outage, plan_for(outage, [slot(start=2, end=4)]))


def test_frozen_predecessor_is_a_field_not_only_times():
    source = snapshot(
        lots=[lot(operations=[operation("a"), operation("b")])],
        freeze=3,
        commitments=[slot("b", start=2, end=4, previous="a")],
    )
    problem = problem_for(source)
    candidate = plan_for(problem, [slot("a"), slot("b", start=2, end=4)])
    assert {"FROZEN_SLOT", "MACHINE_PREDECESSOR"} <= codes(problem, candidate)


def test_commitment_at_freeze_boundary_is_not_frozen():
    source = snapshot(freeze=2, commitments=[slot(start=2, end=4)])
    problem = problem_for(source)
    assert validate_plan(problem, plan_for(problem, [slot(start=4, end=6)])).valid


def test_missing_values_have_exact_paths_not_infeasibility_claim():
    problem = problem_for(snapshot(stocks={"P": 0, "C": None}, orders=[order(due=None)]))
    report = validate_plan(problem, plan_for(problem, [slot()]))
    assert not report.valid
    assert {(value.code, value.path) for value in report.violations} == {
        ("MISSING_INPUT", "materials[1].initial_stock"),
        ("MISSING_INPUT", "orders[0].due_min"),
    }


def test_objective_fields_not_only_total_are_checked():
    problem = problem_for(snapshot())
    candidate = plan_for(problem, [slot()])
    forged = candidate.objective.model_copy(update={"setup_minutes": 1})
    candidate = candidate.model_copy(update={"objective": forged})
    assert "OBJECTIVE_MISMATCH" in codes(problem, candidate)


def test_compare_revalidates_both_exact_objectives_and_stable_baseline_tie():
    problem = problem_for(snapshot())
    baseline = plan_for(problem, [slot(start=1, end=3)], plan_id="baseline")
    optimized = plan_for(problem, [slot()], plan_id="optimized")
    comparison = compare_plans(problem, baseline, optimized)
    assert comparison["selected"] == "optimized"
    assert comparison["selected_plan"]["plan_id"] == "optimized"
    assert comparison["improvement"] == 1
    tie = compare_plans(problem, optimized, optimized.model_copy(update={"plan_id": "tie"}))
    assert tie["selected"] == "baseline"
    worse = compare_plans(problem, optimized, baseline)
    assert worse["selected"] == "baseline" and worse["improvement"] == 0
    invalid = optimized.model_copy(update={"slots": ()})
    with pytest.raises(DomainError, match="independently validate"):
        compare_plans(problem, baseline, invalid)
    other_problem = problem_for(reparse(problem.snapshot, revision=2))
    with pytest.raises(DomainError):
        compare_plans(problem, baseline, plan_for(other_problem, [slot()]))
