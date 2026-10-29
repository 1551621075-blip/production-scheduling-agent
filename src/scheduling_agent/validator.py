"""Independent replay of source-domain scheduling rules; never proves optimality.

The embedded Snapshot is authoritative. Compiled operation records are checked
for integrity but are never the source of scheduling or inventory constraints.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from pydantic import ValidationError

from .domain import (
    Activity,
    CompiledOperation,
    CompiledProblem,
    InventoryPoint,
    Lot,
    MaterialAmount,
    ObjectiveBreakdown,
    Operation,
    Plan,
    ResourceDemand,
    Slot,
    ValidationReport,
    Violation,
    Window,
    canonical_digest,
)
from .errors import DomainError


def _open(windows: tuple[Window, ...], start: int, end: int) -> bool:
    """Cover an interval by the calendar union, including touching windows."""
    if start == end:
        return True
    cursor = start
    for window in sorted(windows, key=lambda value: value.start_min):
        if window.end_min <= cursor:
            continue
        if window.start_min > cursor:
            return False
        cursor = window.end_min
        if cursor >= end:
            return True
    return False


def _source_operations(problem: CompiledProblem) -> dict[str, tuple[Lot, int, Operation]]:
    return {
        operation.operation_id: (lot, index, operation)
        for lot in problem.snapshot.lots
        for index, operation in enumerate(lot.operations)
        if index >= lot.completed_prefix
    }


def validate_plan(problem: CompiledProblem, plan: Plan) -> ValidationReport:
    """Recompute feasibility, exact objective and audit traces from source data.

    Hashes bind content, not identity or solver status. A successful replay says
    only that this particular candidate is feasible, never that it is optimal.
    """
    violations: list[Violation] = []
    inventory: list[InventoryPoint] = []
    activities: list[Activity] = []
    objective: ObjectiveBreakdown | None = None
    snapshot = problem.snapshot
    snapshot_hash = canonical_digest(snapshot)
    objective_hash = canonical_digest(snapshot.objective)
    problem_payload = problem.model_dump(mode="json", exclude={"problem_hash"})
    problem_hash = canonical_digest(problem_payload)
    plan_hash = canonical_digest(plan)

    def fail(
        code: str, message: str, operation_id: str | None = None, path: str | None = None
    ) -> None:
        violations.append(
            Violation(code=code, message=message, operation_id=operation_id, path=path)
        )

    def report() -> ValidationReport:
        return ValidationReport(
            valid=not violations,
            violations=tuple(violations),
            objective=objective,
            inventory=tuple(inventory),
            activities=tuple(activities),
            snapshot_hash=snapshot_hash,
            problem_hash=problem_hash,
            objective_hash=objective_hash,
            plan_hash=plan_hash,
        )

    # A frozen model's model_copy/model_construct can bypass Pydantic validation.
    # Reparse all nested data before interpreting any purportedly strict integers.
    try:
        problem = CompiledProblem.model_validate(problem.model_dump(mode="python"))
        plan = Plan.model_validate(plan.model_dump(mode="python"))
    except (ValidationError, TypeError, ValueError) as exc:
        fail("INVALID_STRUCTURE", str(exc)[:500])
        return report()
    snapshot = problem.snapshot
    if problem.snapshot_hash != snapshot_hash:
        fail("SNAPSHOT_HASH", "Embedded snapshot content does not match its hash.")
    if problem.objective_hash != objective_hash:
        fail("OBJECTIVE_HASH", "Embedded objective does not match its hash.")
    if problem.problem_hash != problem_hash:
        fail("PROBLEM_HASH", "Compiled problem content does not match its hash.")
    if (plan.snapshot_hash, plan.problem_hash, plan.objective_hash) != (
        snapshot_hash,
        problem_hash,
        objective_hash,
    ):
        fail("PLAN_BINDING", "Plan snapshot, problem or objective binding is stale.")
    missing = [
        f"materials[{index}].initial_stock"
        for index, material in enumerate(snapshot.materials)
        if material.initial_stock is None
    ]
    missing.extend(
        f"orders[{index}].due_min"
        for index, order in enumerate(snapshot.orders)
        if order.due_min is None
    )
    for path in missing:
        fail("MISSING_INPUT", "Known value is required before independent validation.", path=path)
    if missing:
        return report()

    source = _source_operations(problem)
    items = {item.item_id: item for item in snapshot.items}
    machines = {machine.machine_id: machine for machine in snapshot.machines}
    resources = {resource.resource_id: resource for resource in snapshot.resources}
    expected: dict[str, CompiledOperation] = {}
    for operation_id, (lot, index, operation) in source.items():
        residual = lot.residual if index == lot.completed_prefix else None
        expected[operation_id] = CompiledOperation(
            operation_id=operation_id,
            lot_id=lot.lot_id,
            item_id=lot.item_id,
            quantity=lot.quantity,
            step_index=index,
            family=operation.family,
            alternatives=operation.alternatives,
            release_min=lot.release_min,
            predecessor_id=(
                lot.operations[index - 1].operation_id if index > lot.completed_prefix else None
            ),
            is_terminal=index == len(lot.operations) - 1,
            residual=residual,
            consumes=tuple(
                MaterialAmount(
                    item_id=line.component_item_id, quantity=lot.quantity * line.units_per_parent
                )
                for line in items[lot.item_id].bom
                if line.step_index == index and residual is None
            ),
        )
    if {op.operation_id: op for op in problem.operations} != expected:
        fail("COMPILED_SOURCE", "Compiled operations differ from the embedded source route or BOM.")

    counts = Counter(slot.operation_id for slot in plan.slots)
    if set(counts) != set(source) or any(count != 1 for count in counts.values()):
        fail("OPERATION_COVERAGE", "Each and only each unfinished source operation needs one slot.")
    slots: dict[str, Slot] = {}
    by_machine: dict[str, list[Slot]] = defaultdict(list)

    def occupy(
        slot: Slot,
        kind: str,
        start: int,
        end: int,
        demands: tuple[ResourceDemand, ...],
        family: str,
    ) -> None:
        machine = machines[slot.machine_id]
        if not _open(machine.calendar, start, end):
            fail("MACHINE_CALENDAR", f"{kind} is outside the machine calendar.", slot.operation_id)
        if end > start:
            activities.append(
                Activity(
                    resource_id=slot.machine_id,
                    resource_kind="machine",
                    operation_id=slot.operation_id,
                    kind=kind,
                    start_min=start,
                    end_min=end,
                    demand=1,
                )
            )
        for demand in demands:
            resource = resources[demand.resource_id]
            if family not in resource.qualified_families:
                fail(
                    "RESOURCE_QUALIFICATION",
                    f"{kind} resource lacks target-family qualification.",
                    slot.operation_id,
                )
            if not _open(resource.calendar, start, end):
                fail(
                    "RESOURCE_CALENDAR",
                    f"{kind} is outside a shared resource calendar.",
                    slot.operation_id,
                )
            if end > start:
                activities.append(
                    Activity(
                        resource_id=demand.resource_id,
                        resource_kind="resource",
                        operation_id=slot.operation_id,
                        kind=kind,
                        start_min=start,
                        end_min=end,
                        demand=demand.quantity,
                    )
                )

    for slot in plan.slots:
        if slot.operation_id not in source:
            continue
        slots.setdefault(slot.operation_id, slot)
        lot, index, operation = source[slot.operation_id]
        if slot.end_min > snapshot.horizon_min:
            fail("HORIZON", "Operation ends outside the planning horizon.", slot.operation_id)
        alternative = next(
            (a for a in operation.alternatives if a.machine_id == slot.machine_id), None
        )
        if alternative is None or slot.machine_id not in machines:
            fail(
                "ALTERNATIVE",
                "Chosen machine is not an explicit operation alternative.",
                slot.operation_id,
            )
            continue
        by_machine[slot.machine_id].append(slot)
        machine = machines[slot.machine_id]
        if not alternative.qualified or operation.family not in machine.qualified_families:
            fail(
                "MACHINE_QUALIFICATION",
                "Machine alternative lacks explicit qualification.",
                slot.operation_id,
            )
        residual = lot.residual if index == lot.completed_prefix else None
        duration = residual.remaining_min if residual is not None else alternative.duration_min
        if slot.end_min - slot.start_min != duration:
            fail(
                "DURATION",
                "Processing duration differs from the whole-lot source duration.",
                slot.operation_id,
            )
        if residual is not None:
            if (
                slot.machine_id,
                slot.setup_start_min,
                slot.start_min,
                slot.end_min,
                slot.predecessor_id,
            ) != (residual.machine_id, 0, 0, residual.remaining_min, None):
                fail(
                    "RESIDUAL_FROZEN",
                    "Residual must remain first on its machine at time zero.",
                    slot.operation_id,
                )
        if index == lot.completed_prefix and slot.setup_start_min < lot.release_min:
            fail(
                "RELEASE", "First remaining setup begins before the lot release.", slot.operation_id
            )
        occupy(
            slot,
            "processing",
            slot.start_min,
            slot.end_min,
            alternative.resources,
            operation.family,
        )

    for lot in snapshot.lots:
        pending = lot.operations[lot.completed_prefix :]
        for previous, following in zip(pending, pending[1:], strict=False):
            if previous.operation_id in slots and following.operation_id in slots:
                if (
                    slots[previous.operation_id].end_min
                    > slots[following.operation_id].setup_start_min
                ):
                    fail(
                        "ROUTE_PRECEDENCE",
                        "Next setup begins before the preceding route step ends.",
                        following.operation_id,
                    )

    # Recover actual machine adjacency from occupied time, not candidate arcs.
    for machine_id, machine_slots in sorted(by_machine.items()):
        machine = machines[machine_id]
        previous: Slot | None = None
        previous_family = machine.initial_family
        for slot in sorted(
            machine_slots, key=lambda s: (s.setup_start_min, s.start_min, s.end_min, s.operation_id)
        ):
            lot, index, operation = source[slot.operation_id]
            predecessor_id = previous.operation_id if previous is not None else None
            if slot.predecessor_id != predecessor_id:
                fail(
                    "MACHINE_PREDECESSOR",
                    "Declared predecessor is not the actual adjacent operation.",
                    slot.operation_id,
                )
            if previous is not None and previous.end_min > slot.setup_start_min:
                fail(
                    "MACHINE_OVERLAP",
                    "Previous operation overlaps the next setup or processing.",
                    slot.operation_id,
                )
            residual = lot.residual if index == lot.completed_prefix else None
            if residual is None:
                rule = next(
                    rule
                    for rule in machine.setups
                    if (rule.from_family, rule.to_family) == (previous_family, operation.family)
                )
                if slot.start_min - slot.setup_start_min != rule.duration_min:
                    fail(
                        "SETUP_DURATION",
                        "Contiguous immediately preceding setup has wrong duration.",
                        slot.operation_id,
                    )
                occupy(
                    slot,
                    "setup",
                    slot.setup_start_min,
                    slot.start_min,
                    rule.resources,
                    operation.family,
                )
            previous = slot
            previous_family = operation.family

    for commitment in snapshot.commitments:
        lot, index, _ = source[commitment.operation_id]
        residual_commitment = lot.residual is not None and index == lot.completed_prefix
        if commitment.setup_start_min < snapshot.freeze_until_min or residual_commitment:
            if slots.get(commitment.operation_id) != commitment:
                fail(
                    "FROZEN_SLOT",
                    "Frozen commitment changed, including its machine predecessor.",
                    commitment.operation_id,
                )

    # Half-open sweeps: simultaneous ends release before simultaneous starts.
    grouped: dict[tuple[str, str], list[Activity]] = defaultdict(list)
    for activity in activities:
        grouped[(activity.resource_kind, activity.resource_id)].append(activity)
    for (kind, resource_id), group in sorted(grouped.items()):
        capacity = 1 if kind == "machine" else resources[resource_id].capacity
        changes: dict[int, int] = defaultdict(int)
        for activity in group:
            changes[activity.start_min] += activity.demand
            changes[activity.end_min] -= activity.demand
        load = 0
        for minute, delta in sorted(changes.items()):
            load += delta
            if load > capacity:
                fail(
                    "MACHINE_CAPACITY" if kind == "machine" else "RESOURCE_CAPACITY",
                    f"{kind} {resource_id} exceeds capacity at minute {minute}.",
                )

    receipts: dict[tuple[int, str], int] = defaultdict(int)
    deductions: dict[tuple[int, str], int] = defaultdict(int)
    for supply in snapshot.supplies:
        receipts[(supply.at_min, supply.item_id)] += supply.quantity
    for operation_id, (lot, index, _) in source.items():
        slot = slots.get(operation_id)
        if slot is None:
            continue
        residual = lot.residual is not None and index == lot.completed_prefix
        if not residual:
            for line in items[lot.item_id].bom:
                if line.step_index == index:
                    deductions[(slot.start_min, line.component_item_id)] += (
                        lot.quantity * line.units_per_parent
                    )
        if index == len(lot.operations) - 1:
            receipts[(slot.end_min, lot.item_id)] += lot.quantity
    for order in snapshot.orders:
        lot = next(lot for lot in snapshot.lots if lot.lot_id == order.lot_id)
        terminal = slots.get(lot.operations[-1].operation_id)
        if terminal is not None:
            deductions[(terminal.end_min, order.item_id)] += order.quantity
    balances = {material.item_id: material.initial_stock for material in snapshot.materials}
    for item_id, balance in sorted(balances.items()):
        inventory.append(
            InventoryPoint(item_id=item_id, at_min=0, received=0, consumed=0, balance=balance)
        )
    for minute, item_id in sorted(receipts.keys() | deductions.keys()):
        received = receipts[(minute, item_id)]
        consumed = deductions[(minute, item_id)]
        balances[item_id] += received
        balances[item_id] -= consumed
        inventory.append(
            InventoryPoint(
                item_id=item_id,
                at_min=minute,
                received=received,
                consumed=consumed,
                balance=balances[item_id],
            )
        )
        if balances[item_id] < 0:
            fail("MATERIAL_NEGATIVE", f"Item {item_id} has negative inventory at minute {minute}.")

    # Objective is recomputed even for otherwise-invalid, completely covered plans.
    if set(slots) == set(source) and len(plan.slots) == len(source):
        tardiness = 0
        lots = {lot.lot_id: lot for lot in snapshot.lots}
        for order in snapshot.orders:
            completion = slots[lots[order.lot_id].operations[-1].operation_id].end_min
            tardiness += order.priority * max(0, completion - order.due_min)
        setup_minutes = sum(slot.start_min - slot.setup_start_min for slot in slots.values())
        makespan = max((slot.end_min for slot in slots.values()), default=0)
        loads = {machine_id: 0 for machine_id in machines}
        for slot in slots.values():
            if slot.machine_id in loads:
                loads[slot.machine_id] += slot.end_min - slot.setup_start_min
        spread = max(loads.values()) - min(loads.values())
        weights = snapshot.objective
        objective = ObjectiveBreakdown(
            weighted_tardiness=tardiness,
            setup_minutes=setup_minutes,
            makespan=makespan,
            load_spread=spread,
            total=(
                weights.weighted_tardiness * tardiness
                + weights.setup_minutes * setup_minutes
                + weights.makespan * makespan
                + weights.load_spread * spread
            ),
        )
        if objective != plan.objective:
            fail(
                "OBJECTIVE_MISMATCH", "Claimed objective differs from exact independent arithmetic."
            )
    activities.sort(
        key=lambda value: (
            value.start_min,
            value.end_min,
            value.resource_kind,
            value.resource_id,
            value.operation_id,
            value.kind,
        )
    )
    return report()


def compare_plans(problem: CompiledProblem, baseline: Plan, optimized: Plan) -> dict:
    """Select the lower exact objective among two independently valid candidates."""
    reports = [validate_plan(problem, plan) for plan in (baseline, optimized)]
    if any(not report.valid for report in reports):
        raise DomainError("INVALID_COMPARISON", "Both candidates must independently validate.")
    baseline_report, optimized_report = reports
    assert baseline_report.objective is not None and optimized_report.objective is not None
    choose_optimized = optimized_report.objective.total < baseline_report.objective.total
    selected = optimized if choose_optimized else baseline
    selected_objective = (
        optimized_report.objective if choose_optimized else baseline_report.objective
    )
    return {
        "selected": "optimized" if choose_optimized else "baseline",
        "selected_plan": selected.model_dump(mode="json"),
        "baseline_metrics": baseline_report.objective.model_dump(mode="json"),
        "optimized_metrics": optimized_report.objective.model_dump(mode="json"),
        "improvement": baseline_report.objective.total - selected_objective.total,
    }
