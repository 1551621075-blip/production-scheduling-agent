"""Compile authorized whole lots and strictly typed, user-selected scenarios.

Compilation never fills unknown inventory/due dates, invents production, or
refreshes the observation timestamp. Known capacity/material conflicts remain
valid compiled problems for the actual solver to decide.
"""

from __future__ import annotations

from pydantic import ValidationError

from .domain import (
    MAX_OBJECTIVE,
    CompiledOperation,
    CompiledProblem,
    MaterialAmount,
    Scenario,
    Snapshot,
    canonical_digest,
    missing_inputs,
)
from .errors import DomainError

COMPILER_VERSION = "r6-v1"


def build_scenario(snapshot: Snapshot, scenario: Scenario) -> Snapshot:
    """Apply exactly one approved mutation, preserving source lineage and horizon."""
    if not isinstance(snapshot, Snapshot) or not isinstance(scenario, Scenario):
        raise DomainError("invalid_scenario", "snapshot and scenario must use the strict domain schema")
    # Revalidate even domain instances: model_copy/model_construct can bypass Pydantic validation.
    try:
        snapshot = Snapshot.model_validate(snapshot.model_dump(mode="python"))
        scenario = Scenario.model_validate(scenario.model_dump(mode="python"))
        payload = snapshot.model_dump(mode="python")
        if scenario.kind == "baseline":
            return snapshot
        if scenario.kind == "insert_order":
            payload["lots"] += (scenario.lot.model_dump(mode="python"),)
            payload["orders"] += (scenario.order.model_dump(mode="python"),)
        elif scenario.kind == "machine_outage":
            if scenario.end_min > snapshot.horizon_min:
                raise DomainError("invalid_scenario", "outage exceeds the unchanged planning horizon")
            machine_index = next(
                (i for i, machine in enumerate(snapshot.machines)
                 if machine.machine_id == scenario.machine_id), None
            )
            if machine_index is None:
                raise DomainError("invalid_scenario", "outage references an unknown machine")
            windows = []
            for window in snapshot.machines[machine_index].calendar:
                start, end = window.start_min, window.end_min
                if end <= scenario.start_min or start >= scenario.end_min:
                    windows.append({"start_min": start, "end_min": end})
                    continue
                if start < scenario.start_min:
                    windows.append({"start_min": start, "end_min": scenario.start_min})
                if end > scenario.end_min:
                    windows.append({"start_min": scenario.end_min, "end_min": end})
            payload["machines"][machine_index]["calendar"] = tuple(windows)
        else:  # Strict Scenario permits only supply_delay here.
            supply_index = next(
                (i for i, supply in enumerate(snapshot.supplies)
                 if supply.supply_id == scenario.supply_id), None
            )
            if supply_index is None:
                raise DomainError("invalid_scenario", "delay references an unknown supply")
            if not snapshot.supplies[supply_index].at_min < scenario.at_min <= snapshot.horizon_min:
                raise DomainError("invalid_scenario", "supply delay must be later and within the unchanged horizon")
            payload["supplies"][supply_index]["at_min"] = scenario.at_min
        result = Snapshot.model_validate(payload)
    except ValidationError as exc:
        raise DomainError("invalid_scenario", str(exc)) from exc
    if canonical_digest(result) == canonical_digest(snapshot):
        raise DomainError("invalid_scenario", "non-baseline scenario must change snapshot content")
    return result


def compile_problem(snapshot: Snapshot) -> CompiledProblem:
    """Expand all remaining operations and exact whole-lot BOM quantities."""
    if not isinstance(snapshot, Snapshot):
        raise DomainError("invalid_snapshot", "snapshot must use the strict domain schema")
    try:
        snapshot = Snapshot.model_validate(snapshot.model_dump(mode="python"))
    except ValidationError as exc:
        raise DomainError("invalid_snapshot", str(exc)) from exc
    missing = missing_inputs(snapshot)
    if missing:
        raise DomainError("missing_fields", "missing required values: " + ", ".join(missing))
    h = snapshot.horizon_min
    count = sum(len(lot.operations) - lot.completed_prefix for lot in snapshot.lots)
    weights = snapshot.objective
    upper = (
        weights.weighted_tardiness * h * sum(order.priority for order in snapshot.orders)
        + weights.setup_minutes * count * h
        + weights.makespan * h
        + weights.load_spread * h
    )
    if upper > MAX_OBJECTIVE:
        raise DomainError("objective_overflow", "exact objective upper bound exceeds supported limit")
    items = {item.item_id: item for item in snapshot.items}
    operations = []
    for lot in snapshot.lots:
        for index in range(lot.completed_prefix, len(lot.operations)):
            operation = lot.operations[index]
            residual = lot.residual if index == lot.completed_prefix else None
            consumes = () if residual is not None else tuple(
                MaterialAmount(
                    item_id=line.component_item_id, quantity=lot.quantity * line.units_per_parent
                )
                for line in items[lot.item_id].bom if line.step_index == index
            )
            operations.append(CompiledOperation(
                operation_id=operation.operation_id,
                lot_id=lot.lot_id,
                item_id=lot.item_id,
                quantity=lot.quantity,
                step_index=index,
                family=operation.family,
                alternatives=operation.alternatives,
                release_min=lot.release_min,
                predecessor_id=(None if index == lot.completed_prefix
                                else lot.operations[index - 1].operation_id),
                is_terminal=index == len(lot.operations) - 1,
                residual=residual,
                consumes=consumes,
            ))
    payload = {
        "snapshot": snapshot,
        "snapshot_hash": canonical_digest(snapshot),
        "objective_hash": canonical_digest(snapshot.objective),
        "compiler_version": COMPILER_VERSION,
        "operations": tuple(operations),
    }
    return CompiledProblem(**payload, problem_hash=canonical_digest(payload))
