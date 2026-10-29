"""Tiny exhaustive oracle, independent of solver and validator constraint code.

Only the final comparison calls CP-SAT. Feasibility and optimal objective are
found first by enumerating source machine choices and all integer start times.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import product

import pytest

from scheduling_agent.domain import (
    CompiledOperation,
    CompiledProblem,
    ObjectiveBreakdown,
    Snapshot,
    SolverStatus,
    canonical_digest,
)


def _pending(source):
    return [
        (lot, index, operation)
        for lot in source.lots
        for index, operation in enumerate(lot.operations)
        if index >= lot.completed_prefix
    ]


def _evaluate(source, assignment):
    """A discrete minute occupancy grid, not an interval constraint replay.

    assignment maps op ID to (machine ID, processing start). Setup and machine
    adjacency are reconstructed. None means this complete assignment is invalid.
    """
    pending = _pending(source)
    machines = {machine.machine_id: machine for machine in source.machines}
    resources = {resource.resource_id: resource for resource in source.resources}
    operations = {
        operation.operation_id: (lot, index, operation) for lot, index, operation in pending
    }
    if set(assignment) != set(operations):
        return None
    slots = {}
    modes = {}
    sequences = defaultdict(list)
    for lot, index, operation in pending:
        machine_id, start = assignment[operation.operation_id]
        mode = next(
            (mode for mode in operation.alternatives if mode.machine_id == machine_id), None
        )
        if mode is None or not mode.qualified:
            return None
        if operation.family not in machines[machine_id].qualified_families:
            return None
        residual = lot.residual if index == lot.completed_prefix else None
        duration = residual.remaining_min if residual is not None else mode.duration_min
        if residual is not None and (machine_id, start) != (residual.machine_id, 0):
            return None
        if start < 0 or start + duration > source.horizon_min:
            return None
        slots[operation.operation_id] = {
            "operation_id": operation.operation_id,
            "machine_id": machine_id,
            "start_min": start,
            "end_min": start + duration,
        }
        modes[operation.operation_id] = mode
        sequences[machine_id].append(operation.operation_id)

    usage = defaultdict(lambda: [0] * source.horizon_min)
    for machine_id, ids in sequences.items():
        machine = machines[machine_id]
        family = machine.initial_family
        predecessor = None
        for operation_id in sorted(ids, key=lambda op_id: (slots[op_id]["start_min"], op_id)):
            lot, index, operation = operations[operation_id]
            slot = slots[operation_id]
            residual = lot.residual if index == lot.completed_prefix else None
            rule = next(
                rule
                for rule in machine.setups
                if rule.from_family == family and rule.to_family == operation.family
            )
            setup_length = 0 if residual is not None else rule.duration_min
            setup_start = slot["start_min"] - setup_length
            if setup_start < 0 or (residual is not None and predecessor is not None):
                return None
            slot["setup_start_min"] = setup_start
            slot["predecessor_id"] = predecessor
            if predecessor is not None and slots[predecessor]["end_min"] > setup_start:
                return None
            setup_resources = () if residual is not None else rule.resources
            for start, end, demands in (
                (setup_start, slot["start_min"], setup_resources),
                (slot["start_min"], slot["end_min"], modes[operation_id].resources),
            ):
                for minute in range(start, end):
                    if not any(
                        window.start_min <= minute < window.end_min for window in machine.calendar
                    ):
                        return None
                    usage[("machine", machine_id)][minute] += 1
                for demand in demands:
                    resource = resources[demand.resource_id]
                    if operation.family not in resource.qualified_families:
                        return None
                    for minute in range(start, end):
                        if not any(
                            window.start_min <= minute < window.end_min
                            for window in resource.calendar
                        ):
                            return None
                        usage[("resource", demand.resource_id)][minute] += demand.quantity
            family = operation.family
            predecessor = operation_id
    for (kind, resource_id), minutes in usage.items():
        capacity = 1 if kind == "machine" else resources[resource_id].capacity
        if any(amount > capacity for amount in minutes):
            return None
    for lot in source.lots:
        pending_route = lot.operations[lot.completed_prefix :]
        if (
            pending_route
            and slots[pending_route[0].operation_id]["setup_start_min"] < lot.release_min
        ):
            return None
        for first, second in zip(pending_route, pending_route[1:], strict=False):
            if slots[first.operation_id]["end_min"] > slots[second.operation_id]["setup_start_min"]:
                return None
    for commitment in source.commitments:
        lot, index, _ = operations[commitment.operation_id]
        is_residual = lot.residual is not None and index == lot.completed_prefix
        if commitment.setup_start_min < source.freeze_until_min or is_residual:
            if commitment.model_dump() != slots.get(commitment.operation_id):
                return None

    # Two phases per minute: all production/supply first, all input/delivery next.
    items = {item.item_id: item for item in source.items}
    lots = {lot.lot_id: lot for lot in source.lots}
    balances = {material.item_id: material.initial_stock for material in source.materials}
    if any(balance is None for balance in balances.values()):
        raise ValueError("oracle requires complete source material data")
    for minute in range(source.horizon_min + 1):
        for supply in source.supplies:
            if supply.at_min == minute:
                balances[supply.item_id] += supply.quantity
        for lot in source.lots:
            if lot.completed_prefix < len(lot.operations):
                if slots[lot.operations[-1].operation_id]["end_min"] == minute:
                    balances[lot.item_id] += lot.quantity
        for lot, index, operation in pending:
            if slots[operation.operation_id]["start_min"] != minute:
                continue
            if lot.residual is not None and index == lot.completed_prefix:
                continue
            for line in items[lot.item_id].bom:
                if line.step_index == index:
                    balances[line.component_item_id] -= lot.quantity * line.units_per_parent
        for order in source.orders:
            terminal = lots[order.lot_id].operations[-1].operation_id
            if slots[terminal]["end_min"] == minute:
                balances[order.item_id] -= order.quantity
        if any(amount < 0 for amount in balances.values()):
            return None

    tardiness = 0
    for order in source.orders:
        if order.due_min is None:
            raise ValueError("oracle requires complete source due dates")
        completion = slots[lots[order.lot_id].operations[-1].operation_id]["end_min"]
        tardiness += order.priority * max(0, completion - order.due_min)
    setup = sum(slot["start_min"] - slot["setup_start_min"] for slot in slots.values())
    makespan = max((slot["end_min"] for slot in slots.values()), default=0)
    machine_load = [
        sum(
            slot["end_min"] - slot["setup_start_min"]
            for slot in slots.values()
            if slot["machine_id"] == machine_id
        )
        for machine_id in machines
    ]
    spread = max(machine_load) - min(machine_load)
    weights = source.objective
    objective = ObjectiveBreakdown(
        weighted_tardiness=tardiness,
        setup_minutes=setup,
        makespan=makespan,
        load_spread=spread,
        total=weights.weighted_tardiness * tardiness
        + weights.setup_minutes * setup
        + weights.makespan * makespan
        + weights.load_spread * spread,
    )
    return objective, slots


def exhaustive_oracle(source):
    """Return exact best objective, or None iff all enumerated choices fail."""
    pending = _pending(source)
    if len(pending) > 4 or source.horizon_min > 12:
        raise ValueError("tiny oracle supports at most four operations and horizon twelve")
    if any(material.initial_stock is None for material in source.materials) or any(
        order.due_min is None for order in source.orders
    ):
        raise ValueError("tiny oracle requires complete inputs")
    choices = []
    for lot, index, operation in pending:
        residual = lot.residual if index == lot.completed_prefix else None
        choices.append(
            [
                (mode.machine_id, start)
                for mode in operation.alternatives
                for start in range(source.horizon_min)
                if residual is None or (mode.machine_id, start) == (residual.machine_id, 0)
            ]
        )
    best = None
    ids = [operation.operation_id for _, _, operation in pending]
    for vector in product(*choices):
        evaluation = _evaluate(source, dict(zip(ids, vector, strict=True)))
        if evaluation is not None:
            value = evaluation[0].total
            best = value if best is None else min(value, best)
    return best


def _transport(source):
    """Independent transport assembly only; no compiler helpers or constraints."""
    items = {item.item_id: item for item in source.items}
    records = []
    for lot, index, operation in _pending(source):
        residual = lot.residual if index == lot.completed_prefix else None
        records.append(
            CompiledOperation(
                operation_id=operation.operation_id,
                lot_id=lot.lot_id,
                item_id=lot.item_id,
                quantity=lot.quantity,
                step_index=index,
                family=operation.family,
                alternatives=operation.alternatives,
                release_min=lot.release_min,
                predecessor_id=lot.operations[index - 1].operation_id
                if index > lot.completed_prefix
                else None,
                is_terminal=index == len(lot.operations) - 1,
                residual=residual,
                consumes=tuple(
                    {
                        "item_id": line.component_item_id,
                        "quantity": lot.quantity * line.units_per_parent,
                    }
                    for line in items[lot.item_id].bom
                    if line.step_index == index and residual is None
                ),
            )
        )
    payload = {
        "snapshot": source,
        "snapshot_hash": canonical_digest(source),
        "objective_hash": canonical_digest(source.objective),
        "compiler_version": "1",
        "operations": tuple(records),
    }
    return CompiledProblem(**payload, problem_hash=canonical_digest(payload))


def _op(operation_id, *, family="F", machine="M0", duration=1, demands=(), alternatives=None):
    return {
        "operation_id": operation_id,
        "family": family,
        "alternatives": alternatives
        if alternatives is not None
        else [
            {
                "machine_id": machine,
                "duration_min": duration,
                "resources": demands,
                "qualified": True,
            }
        ],
    }


def _lot(lot_id, ops, *, item="P", quantity=1, completed=0, residual=None):
    return {
        "lot_id": lot_id,
        "item_id": item,
        "quantity": quantity,
        "release_min": 0,
        "operations": ops,
        "completed_prefix": completed,
        "residual": residual,
    }


def _case(name):
    horizon = 6
    full = [{"start_min": 0, "end_min": horizon}]
    payload = {
        "snapshot_id": name,
        "plant_id": "synthetic",
        "revision": 0,
        "observed_at_ms": 0,
        "horizon_min": horizon,
        "items": [
            {"item_id": "P", "unit": "piece", "bom": []},
            {"item_id": "C", "unit": "piece", "bom": []},
        ],
        "materials": [{"item_id": "P", "initial_stock": 0}, {"item_id": "C", "initial_stock": 0}],
        "machines": [
            {
                "machine_id": machine,
                "initial_family": "F",
                "qualified_families": ["F", "G"],
                "calendar": full,
                "setups": [
                    {
                        "from_family": before,
                        "to_family": after,
                        "duration_min": int(before != after),
                        "resources": [],
                    }
                    for before in ("F", "G")
                    for after in ("F", "G")
                ],
            }
            for machine in ("M0", "M1")
        ],
        "resources": [
            {"resource_id": "R", "capacity": 1, "qualified_families": ["F", "G"], "calendar": full}
        ],
        "lots": [_lot("L", [_op("a")])],
        "orders": [],
        "supplies": [],
        "freeze_until_min": 0,
        "commitments": [],
        "objective": {"weighted_tardiness": 3, "setup_minutes": 2, "makespan": 1, "load_spread": 1},
    }
    bom = {"component_item_id": "C", "step_index": 0, "units_per_parent": 1}
    demand = [{"resource_id": "R", "quantity": 1}]
    if name == "alternatives":
        payload["lots"] = [
            _lot(
                "L",
                [
                    _op(
                        "a",
                        alternatives=[
                            {
                                "machine_id": "M0",
                                "duration_min": 3,
                                "resources": [],
                                "qualified": True,
                            },
                            {
                                "machine_id": "M1",
                                "duration_min": 1,
                                "resources": [],
                                "qualified": True,
                            },
                        ],
                    )
                ],
            )
        ]
    elif name == "four_ops_adjacent_setup":
        payload["lots"] = [
            _lot("L", [_op("a", family="G"), _op("b", family="G"), _op("c"), _op("d")])
        ]
    elif name in ("calendar", "touching_calendars"):
        payload["lots"] = [_lot("L", [_op("a", family="G", duration=2)])]
        payload["machines"][0]["calendar"] = (
            [{"start_min": 0, "end_min": 1}, {"start_min": 2, "end_min": horizon}]
            if name == "calendar"
            else [{"start_min": 0, "end_min": 2}, {"start_min": 2, "end_min": horizon}]
        )
    elif name in ("resource_tie", "resource_overcapacity", "resource_calendar", "setup_resource"):
        payload["lots"] = [
            _lot("L", [_op("a", duration=2, demands=demand)]),
            _lot("L1", [_op("b", machine="M1", duration=2, demands=demand)]),
        ]
        if name == "resource_overcapacity":
            payload["lots"][0]["operations"][0]["alternatives"][0]["resources"][0]["quantity"] = 2
        elif name == "resource_calendar":
            payload["resources"][0]["calendar"] = [{"start_min": 1, "end_min": 5}]
        elif name == "setup_resource":
            payload["lots"][1]["operations"][0]["family"] = "G"
            payload["lots"][1]["operations"][0]["alternatives"][0]["resources"] = []
            for rule in payload["machines"][1]["setups"]:
                if rule["from_family"] != rule["to_family"]:
                    rule["resources"] = demand
    elif name in ("double_spend", "late_supply", "component_completion", "delivered_component"):
        payload["items"][0]["bom"] = [bom]
        if name == "double_spend":
            payload["materials"][1]["initial_stock"] = 1
            payload["lots"].append(_lot("L1", [_op("b", machine="M1")]))
        elif name == "late_supply":
            payload["lots"][0]["quantity"] = 2
            payload["supplies"] = [{"supply_id": "S", "item_id": "C", "quantity": 2, "at_min": 4}]
        else:
            payload["lots"].append(_lot("CL", [_op("b", machine="M1")], item="C"))
            if name == "delivered_component":
                payload["orders"] = [
                    {
                        "order_id": "CO",
                        "lot_id": "CL",
                        "item_id": "C",
                        "quantity": 1,
                        "due_min": 0,
                        "priority": 2,
                    }
                ]
    elif name in ("wip", "residual_outage"):
        payload["lots"] = [
            _lot(
                "L",
                [_op("done"), _op("a", duration=4), _op("b")],
                completed=1,
                residual={"machine_id": "M0", "remaining_min": 2},
            )
        ]
        payload["items"][0]["bom"] = [bom, dict(bom, step_index=1)]
        if name == "residual_outage":
            payload["machines"][0]["calendar"] = [{"start_min": 1, "end_min": horizon}]
    elif name in ("frozen", "frozen_outage", "frozen_predecessor"):
        payload["freeze_until_min"] = 3
        payload["commitments"] = [
            {
                "operation_id": "a",
                "machine_id": "M0",
                "setup_start_min": 1,
                "start_min": 1,
                "end_min": 2,
                "predecessor_id": None,
            }
        ]
        if name == "frozen_outage":
            payload["machines"][0]["calendar"] = [{"start_min": 2, "end_min": horizon}]
        elif name == "frozen_predecessor":
            payload["lots"].append(_lot("L1", [_op("b")]))
            payload["commitments"].append(
                {
                    "operation_id": "b",
                    "machine_id": "M0",
                    "setup_start_min": 2,
                    "start_min": 2,
                    "end_min": 3,
                    "predecessor_id": "a",
                }
            )
    elif name == "weighted_tardiness":
        payload["lots"] = [_lot("L", [_op("a", duration=2)]), _lot("L1", [_op("b", duration=2)])]
        payload["orders"] = [
            {
                "order_id": "O",
                "lot_id": "L",
                "item_id": "P",
                "quantity": 1,
                "due_min": 0,
                "priority": 1,
            },
            {
                "order_id": "O1",
                "lot_id": "L1",
                "item_id": "P",
                "quantity": 1,
                "due_min": 0,
                "priority": 5,
            },
        ]
    elif name == "load_spread":
        modes = [
            {"machine_id": machine, "duration_min": 1, "resources": [], "qualified": True}
            for machine in ("M0", "M1")
        ]
        payload["lots"] = [
            _lot("L", [_op("a", alternatives=modes)]),
            _lot("L1", [_op("b", alternatives=modes)]),
        ]
        payload["objective"] = {
            "weighted_tardiness": 0,
            "setup_minutes": 0,
            "makespan": 0,
            "load_spread": 1,
        }
    elif name in ("no_mode", "unqualified", "setup_unqualified"):
        if name == "no_mode":
            payload["lots"][0]["operations"][0]["alternatives"] = []
        elif name == "unqualified":
            payload["lots"][0]["operations"][0]["alternatives"][0]["qualified"] = False
        else:
            payload["lots"][0]["operations"][0]["family"] = "G"
            payload["resources"][0]["qualified_families"] = ["F"]
            for rule in payload["machines"][0]["setups"]:
                rule["resources"] = demand
    elif name == "release":
        payload["lots"][0]["release_min"] = 3
        payload["lots"][0]["operations"][0]["family"] = "G"
    elif name == "completed":
        payload["lots"][0]["completed_prefix"] = 1
    elif name != "single":
        raise AssertionError(f"unknown original synthetic case {name}")
    return Snapshot.model_validate(payload)


CASES = [
    "single",
    "alternatives",
    "four_ops_adjacent_setup",
    "calendar",
    "touching_calendars",
    "resource_tie",
    "resource_overcapacity",
    "resource_calendar",
    "setup_resource",
    "double_spend",
    "late_supply",
    "component_completion",
    "delivered_component",
    "wip",
    "residual_outage",
    "frozen",
    "frozen_outage",
    "frozen_predecessor",
    "weighted_tardiness",
    "load_spread",
    "no_mode",
    "unqualified",
    "setup_unqualified",
    "release",
    "completed",
]


@pytest.mark.parametrize("name", CASES)
def test_independent_exhaustive_oracle_matches_real_cp_sat(name):
    source = _case(name)
    optimum = exhaustive_oracle(source)
    # Do not import solver until the independent answer has been computed.
    from scheduling_agent.solver import solve

    result = solve(_transport(source), mode="optimize", time_limit_s=5.0, seed=41723)
    if optimum is None:
        assert result.status == SolverStatus.INFEASIBLE, result
        assert result.plan is None
    else:
        assert result.status == SolverStatus.OPTIMAL, result
        assert result.plan is not None
        assert result.plan.objective.total == optimum
        assignment = {
            slot.operation_id: (slot.machine_id, slot.start_min) for slot in result.plan.slots
        }
        evaluated = _evaluate(source, assignment)
        assert evaluated is not None, (
            "solver returned a candidate rejected by independent minute grid"
        )
        objective, expected_slots = evaluated
        assert result.plan.objective == objective
        assert len(result.plan.slots) == len(expected_slots)
        assert {
            slot.operation_id: slot.model_dump() for slot in result.plan.slots
        } == expected_slots


def test_oracle_rejects_oversize_rather_than_silently_sampling():
    source = _case("single")
    data = source.model_dump()
    data["horizon_min"] = 13
    with pytest.raises(ValueError, match="at most four"):
        exhaustive_oracle(Snapshot.model_validate(data))
    data = source.model_dump()
    data["lots"] = [_lot("L", [_op(f"op{index}") for index in range(5)])]
    with pytest.raises(ValueError, match="at most four"):
        exhaustive_oracle(Snapshot.model_validate(data))


def test_unknown_is_not_an_infeasibility_proof():
    source = _case("four_ops_adjacent_setup")
    assert exhaustive_oracle(source) is not None
    from scheduling_agent.solver import solve

    limited = solve(_transport(source), mode="optimize", time_limit_s=0.0, seed=41723)
    assert limited.status == SolverStatus.UNKNOWN
    assert limited.plan is None
    impossible = _case("double_spend")
    assert exhaustive_oracle(impossible) is None
    proved = solve(_transport(impossible), mode="optimize", time_limit_s=5.0, seed=41723)
    assert proved.status == SolverStatus.INFEASIBLE
    assert proved.plan is None


def test_feasibility_run_has_valid_candidate_without_optimality_claim():
    source = _case("weighted_tardiness")
    optimum = exhaustive_oracle(source)
    from scheduling_agent.solver import solve

    result = solve(_transport(source), mode="feasibility", time_limit_s=5.0, seed=41723)
    assert result.status == SolverStatus.FEASIBLE
    assert result.best_bound is None
    assert result.plan is not None
    evaluated = _evaluate(
        source, {slot.operation_id: (slot.machine_id, slot.start_min) for slot in result.plan.slots}
    )
    assert evaluated is not None
    assert evaluated[0] == result.plan.objective
    assert evaluated[0].total >= optimum


def test_oracle_explicitly_checks_slot_predecessor_in_frozen_commitment():
    source = _case("frozen_predecessor")
    assert _evaluate(source, {"a": ("M0", 1), "b": ("M0", 2)}) is not None
    data = source.model_dump()
    data["commitments"][1]["predecessor_id"] = None
    assert exhaustive_oracle(Snapshot.model_validate(data)) is None


def test_oracle_includes_every_objective_component_and_unused_machine():
    source = _case("weighted_tardiness")
    evaluation = _evaluate(source, {"b": ("M0", 0), "a": ("M0", 2)})
    assert evaluation is not None
    assert evaluation[0].model_dump() == {
        "weighted_tardiness": 14,
        "setup_minutes": 0,
        "makespan": 4,
        "load_spread": 4,
        "total": 50,
    }
    source = _case("four_ops_adjacent_setup")
    evaluation = _evaluate(source, {"a": ("M0", 1), "b": ("M0", 2), "c": ("M0", 4), "d": ("M0", 5)})
    assert evaluation is not None
    assert evaluation[0].setup_minutes == 2
    assert evaluation[0].total == 16


def test_oracle_inventory_cannot_spend_receipt_before_arrival():
    source = _case("late_supply")
    assert _evaluate(source, {"a": ("M0", 0)}) is None
    assert _evaluate(source, {"a": ("M0", 4)}) is not None
    source = _case("component_completion")
    assert _evaluate(source, {"a": ("M0", 0), "b": ("M1", 0)}) is None
    assert _evaluate(source, {"a": ("M0", 1), "b": ("M1", 0)}) is not None


def test_oracle_is_not_allowed_to_fill_missing_data():
    source = _case("single")
    data = source.model_dump()
    data["materials"][0]["initial_stock"] = None
    with pytest.raises(ValueError, match="complete inputs"):
        exhaustive_oracle(Snapshot.model_validate(data))
