"""Deterministic, synthetic discrete-event execution of a frozen candidate.

A validator proves the nominal candidate first. This engine then owns fresh
inventory, resource occupancy, setup-family and route-progress state; validation
traces and the candidate's objective are never substituted for actual execution.
Execution disturbances stop the run rather than repair or reschedule the plan.
"""

from __future__ import annotations

import heapq
from collections import Counter, defaultdict
from typing import Any

from pydantic import ValidationError

from .domain import (
    MAX_ITEMS,
    MAX_LOTS,
    MAX_MACHINES,
    MAX_ORDERS,
    MAX_REMAINING_OPERATIONS,
    MAX_RESOURCES,
    MAX_SUPPLIES,
    CompiledProblem,
    Plan,
    Scenario,
    canonical_digest,
)
from .errors import DomainError
from .validator import validate_plan

MAX_DISRUPTIONS = MAX_MACHINES + MAX_SUPPLIES
# Includes every initial balance, phase transition, BOM withdrawal, receipt,
# delivery and disturbance, plus execution start and its one terminal event.
MAX_EVENTS = (
    2 + MAX_ITEMS + MAX_SUPPLIES + 2 * MAX_DISRUPTIONS
    + 4 * MAX_REMAINING_OPERATIONS + MAX_REMAINING_OPERATIONS * MAX_ITEMS
    + MAX_LOTS + MAX_ORDERS
)
MAX_MATERIAL_POINTS = (
    MAX_ITEMS + MAX_SUPPLIES + MAX_REMAINING_OPERATIONS * MAX_ITEMS + MAX_LOTS + MAX_ORDERS
)
MAX_RESOURCE_SEGMENTS = 2 * MAX_REMAINING_OPERATIONS * (1 + MAX_RESOURCES)


def _disruptions(problem: CompiledProblem, values: Any) -> tuple[Scenario, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > MAX_DISRUPTIONS:
        raise DomainError("INVALID_DISRUPTION", "Disruptions must be a bounded list or tuple.")
    if not values:
        return ()
    machines = {machine.machine_id for machine in problem.snapshot.machines}
    supplies = {supply.supply_id: supply for supply in problem.snapshot.supplies}
    parsed: list[Scenario] = []
    seen: set[str] = set()
    delayed: set[str] = set()
    for value in values:
        if not isinstance(value, (Scenario, dict)):
            raise DomainError("INVALID_DISRUPTION", "Each disruption must be a structured Scenario.")
        try:
            scenario = Scenario.model_validate(
                value.model_dump(mode="python") if isinstance(value, Scenario) else value
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise DomainError("INVALID_DISRUPTION", "Disruption violates the Scenario schema.") from exc
        if scenario.kind == "machine_outage":
            if (scenario.machine_id not in machines
                    or scenario.end_min > problem.snapshot.horizon_min):
                raise DomainError("INVALID_DISRUPTION", "Outage machine or horizon is invalid.")
        elif scenario.kind == "supply_delay":
            supply = supplies.get(scenario.supply_id)
            if (supply is None or scenario.at_min <= supply.at_min
                    or scenario.at_min > problem.snapshot.horizon_min
                    or scenario.supply_id in delayed):
                raise DomainError("INVALID_DISRUPTION", "Supply delay must be unique and strictly later.")
            delayed.add(scenario.supply_id)
        else:
            raise DomainError(
                "INVALID_DISRUPTION",
                "Only machine_outage and supply_delay execute here; build and solve inserted orders.",
            )
        key = canonical_digest(scenario)
        if key in seen:
            raise DomainError("INVALID_DISRUPTION", "Duplicate execution disruption.")
        seen.add(key)
        parsed.append(scenario)
    return tuple(sorted(parsed, key=canonical_digest))


class _Execution:
    def __init__(self, problem: CompiledProblem, plan: Plan, disruptions: tuple[Scenario, ...]):
        self.problem = problem
        self.plan = plan
        self.snapshot = problem.snapshot
        self.disruptions = disruptions
        self.now = 0
        self.events: list[dict] = []
        self.material: list[dict] = []
        self.usage: list[dict] = []
        self.deliveries: list[dict] = []
        self.blocked_reason: dict | None = None
        self.queue: list[tuple[int, str, str]] = []
        self.slots = {slot.operation_id: slot for slot in plan.slots}
        self.source = {
            operation.operation_id: (lot, index, operation)
            for lot in self.snapshot.lots
            for index, operation in enumerate(lot.operations)
            if index >= lot.completed_prefix
        }
        self.items = {item.item_id: item for item in self.snapshot.items}
        self.machines = {machine.machine_id: machine for machine in self.snapshot.machines}
        self.resources = {resource.resource_id: resource for resource in self.snapshot.resources}
        self.supplies = {supply.supply_id: supply for supply in self.snapshot.supplies}
        self.orders = {order.lot_id: order for order in self.snapshot.orders}
        self.stock = {material.item_id: material.initial_stock for material in self.snapshot.materials}
        self.state = dict.fromkeys(self.source, "pending")
        self.family = {key: machine.initial_family for key, machine in self.machines.items()}
        self.previous = dict.fromkeys(self.machines)
        self.machine_busy: dict[str, str] = {}
        self.resource_load: Counter[str] = Counter()
        self.resource_peak = dict.fromkeys(self.resources, 0)
        self.open_usage: dict[str, list[dict]] = {}
        self.completed: dict[str, int] = {}
        self.completed_lots: set[str] = set()
        self.active_outages: dict[str, Scenario] = {}
        self.outages = {
            canonical_digest(value): value for value in disruptions if value.kind == "machine_outage"
        }
        self.delays = {
            value.supply_id: value.at_min for value in disruptions if value.kind == "supply_delay"
        }
        for key, outage in self.outages.items():
            self.schedule(outage.start_min, "outage_start", key)
            self.schedule(outage.end_min, "outage_end", key)
        for supply in self.snapshot.supplies:
            self.schedule(self.delays.get(supply.supply_id, supply.at_min), "supply", supply.supply_id)
        for operation_id, slot in self.slots.items():
            if not self.residual(operation_id):
                self.schedule(slot.setup_start_min, "setup_start", operation_id)
                if slot.start_min > slot.setup_start_min:
                    self.schedule(slot.start_min, "setup_end", operation_id)
            self.schedule(slot.start_min, "process_start", operation_id)
            self.schedule(slot.end_min, "process_end", operation_id)

    def schedule(self, minute: int, kind: str, key: str) -> None:
        heapq.heappush(self.queue, (minute, kind, key))

    def emit(self, kind: str, **fields: Any) -> None:
        if len(self.events) >= MAX_EVENTS:
            raise DomainError("SIMULATION_LIMIT", "Execution event limit exceeded; no trace truncated.")
        self.events.append({"sequence": len(self.events), "at_min": self.now, "kind": kind, **fields})

    def block(self, code: str, **fields: Any) -> bool:
        self.blocked_reason = {"code": code, "at_min": self.now, **fields}
        self.emit("execution_blocked", code=code, **fields)
        return False

    def residual(self, operation_id: str) -> bool:
        lot, index, _ = self.source[operation_id]
        return lot.residual is not None and index == lot.completed_prefix

    def inputs(self, operation_id: str) -> dict[str, int]:
        lot, index, _ = self.source[operation_id]
        if self.residual(operation_id):
            return {}
        return {
            line.component_item_id: lot.quantity * line.units_per_parent
            for line in self.items[lot.item_id].bom if line.step_index == index
        }

    def alternative(self, operation_id: str):
        operation = self.source[operation_id][2]
        return next(value for value in operation.alternatives
                    if value.machine_id == self.slots[operation_id].machine_id)

    def material_change(self, kind: str, item_id: str, delta: int, **fields: Any) -> None:
        self.stock[item_id] += delta
        point = {
            "at_min": self.now, "kind": kind, "item_id": item_id,
            "unit": self.items[item_id].unit, "received": max(0, delta),
            "consumed": max(0, -delta), "balance": self.stock[item_id], **fields,
        }
        self.material.append(point)
        self.emit(kind, **{key: value for key, value in point.items() if key not in {"kind", "at_min"}})

    def calendar_open(self, windows, start: int, end: int) -> bool:
        if start == end:
            return True
        # Check for an uncovered gap by consuming the requested interval. Adjacent
        # open windows are a continuous calendar, not an artificial closure.
        uncovered = [(start, end)]
        for window in windows:
            remaining = []
            for left, right in uncovered:
                if window.end_min <= left or window.start_min >= right:
                    remaining.append((left, right))
                else:
                    if left < window.start_min:
                        remaining.append((left, window.start_min))
                    if window.end_min < right:
                        remaining.append((window.end_min, right))
            uncovered = remaining
            if not uncovered:
                return True
        return False

    def availability(self, operation_id: str, kind: str, end: int, demands) -> bool:
        slot = self.slots[operation_id]
        operation = self.source[operation_id][2]
        machine = self.machines[slot.machine_id]
        if not self.alternative(operation_id).qualified or operation.family not in machine.qualified_families:
            return self.block("MACHINE_QUALIFICATION", operation_id=operation_id)
        if not self.calendar_open(machine.calendar, self.now, end):
            return self.block("MACHINE_CALENDAR", operation_id=operation_id, phase=kind)
        if slot.machine_id in self.machine_busy:
            return self.block("MACHINE_BUSY", operation_id=operation_id)
        for outage in self.active_outages.values():
            if (outage.machine_id == slot.machine_id
                    and self.now < outage.end_min and outage.start_min < end):
                return self.block("MACHINE_OUTAGE", operation_id=operation_id)
        for demand in demands:
            resource = self.resources[demand.resource_id]
            if operation.family not in resource.qualified_families:
                return self.block("RESOURCE_QUALIFICATION", operation_id=operation_id,
                                  resource_id=demand.resource_id)
            if not self.calendar_open(resource.calendar, self.now, end):
                return self.block("RESOURCE_CALENDAR", operation_id=operation_id,
                                  resource_id=demand.resource_id)
            if end > self.now and self.resource_load[demand.resource_id] + demand.quantity > resource.capacity:
                return self.block("RESOURCE_CAPACITY", operation_id=operation_id,
                                  resource_id=demand.resource_id)
        return True

    def route_ready(self, operation_id: str) -> bool:
        lot, index, _ = self.source[operation_id]
        slot = self.slots[operation_id]
        if self.state[operation_id] != "pending":
            return self.block("OPERATION_STATE", operation_id=operation_id)
        if index > lot.completed_prefix:
            preceding = lot.operations[index - 1].operation_id
            if self.state[preceding] != "completed":
                return self.block("ROUTE_NOT_READY", operation_id=operation_id)
        elif self.now < lot.release_min:
            return self.block("RELEASE_NOT_READY", operation_id=operation_id)
        if self.previous[slot.machine_id] != slot.predecessor_id:
            return self.block("MACHINE_PREDECESSOR", operation_id=operation_id)
        return True

    def acquire(self, operation_id: str, kind: str, end: int, demands) -> None:
        machine_id = self.slots[operation_id].machine_id
        self.machine_busy[machine_id] = operation_id
        intervals = [{"resource_kind": "machine", "resource_id": machine_id, "demand": 1}]
        for demand in demands:
            resource_id = demand.resource_id
            self.resource_load[resource_id] += demand.quantity
            self.resource_peak[resource_id] = max(self.resource_peak[resource_id], self.resource_load[resource_id])
            intervals.append({"resource_kind": "resource", "resource_id": resource_id,
                              "demand": demand.quantity})
        self.open_usage[operation_id] = [
            {**value, "operation_id": operation_id, "kind": kind,
             "start_min": self.now, "planned_end_min": end}
            for value in intervals
        ]

    def release(self, operation_id: str) -> None:
        intervals = self.open_usage.pop(operation_id)
        for interval in intervals:
            if interval["resource_kind"] == "resource":
                self.resource_load[interval["resource_id"]] -= interval["demand"]
            self.usage.append({**interval, "end_min": self.now, "completed": True})
        del self.machine_busy[self.slots[operation_id].machine_id]

    def start_setup(self, operation_id: str) -> bool:
        if not self.route_ready(operation_id):
            return False
        slot = self.slots[operation_id]
        operation = self.source[operation_id][2]
        rule = next((rule for rule in self.machines[slot.machine_id].setups
                     if (rule.from_family, rule.to_family) ==
                     (self.family[slot.machine_id], operation.family)), None)
        if rule is None or rule.duration_min != slot.start_min - self.now:
            return self.block("SETUP_STATE", operation_id=operation_id)
        if not self.availability(operation_id, "setup", slot.start_min, rule.resources):
            return False
        self.emit("setup_start", operation_id=operation_id, machine_id=slot.machine_id,
                  from_family=self.family[slot.machine_id], to_family=operation.family)
        if rule.duration_min:
            self.acquire(operation_id, "setup", slot.start_min, rule.resources)
            self.state[operation_id] = "setup"
        else:
            self.state[operation_id] = "ready"
            self.family[slot.machine_id] = operation.family
            self.emit("setup_end", operation_id=operation_id, machine_id=slot.machine_id)
        return True

    def finish(self, operation_id: str, kind: str) -> bool:
        required_state = "setup" if kind == "setup_end" else "processing"
        if self.state[operation_id] != required_state or operation_id not in self.open_usage:
            return self.block("OPERATION_STATE", operation_id=operation_id, phase=kind)
        self.release(operation_id)
        slot = self.slots[operation_id]
        self.emit(kind, operation_id=operation_id, machine_id=slot.machine_id)
        if kind == "setup_end":
            self.family[slot.machine_id] = self.source[operation_id][2].family
            self.state[operation_id] = "ready"
        else:
            self.completed[operation_id] = self.now
            self.state[operation_id] = "completed"
            self.previous[slot.machine_id] = operation_id
        return True

    def start_processing(self, operation_ids: list[str]) -> bool:
        inputs: Counter[str] = Counter()
        demands: Counter[str] = Counter()
        for operation_id in operation_ids:
            slot = self.slots[operation_id]
            if self.residual(operation_id):
                if not self.route_ready(operation_id):
                    return False
            elif self.state[operation_id] != "ready":
                return self.block("OPERATION_STATE", operation_id=operation_id)
            alternative = self.alternative(operation_id)
            if not self.availability(operation_id, "processing", slot.end_min, alternative.resources):
                return False
            inputs.update(self.inputs(operation_id))
            demands.update({demand.resource_id: demand.quantity for demand in alternative.resources})
        shortages = [
            {"item_id": key, "required": quantity, "available": self.stock[key]}
            for key, quantity in sorted(inputs.items()) if quantity > self.stock[key]
        ]
        if shortages:
            return self.block("MATERIAL_NOT_READY", operation_ids=operation_ids, shortages=shortages)
        for resource_id, quantity in sorted(demands.items()):
            if self.resource_load[resource_id] + quantity > self.resources[resource_id].capacity:
                return self.block("RESOURCE_CAPACITY", resource_id=resource_id,
                                  operation_ids=operation_ids)
        # Preflight the whole minute, not each order independently: stock cannot
        # be promised twice. All receipts have already happened in this batch.
        for operation_id in operation_ids:
            lot = self.source[operation_id][0]
            for item_id, quantity in sorted(self.inputs(operation_id).items()):
                self.material_change("process_input", item_id, -quantity,
                                     operation_id=operation_id, lot_id=lot.lot_id)
        for operation_id in operation_ids:
            slot = self.slots[operation_id]
            alternative = self.alternative(operation_id)
            self.acquire(operation_id, "processing", slot.end_min, alternative.resources)
            self.state[operation_id] = "processing"
            self.family[slot.machine_id] = self.source[operation_id][2].family
            self.emit("process_start", operation_id=operation_id, machine_id=slot.machine_id,
                      residual=self.residual(operation_id))
        return True

    def run(self) -> dict:
        self.emit("execution_started")
        for item_id, balance in sorted(self.stock.items()):
            self.material_change("initial_stock", item_id, 0, initial_stock=balance)
        for supply_id, minute in sorted(self.delays.items()):
            self.emit("supply_delay", supply_id=supply_id,
                      original_at_min=self.supplies[supply_id].at_min, actual_at_min=minute)
        while self.queue and self.blocked_reason is None:
            self.now = self.queue[0][0]
            batch: dict[str, list[str]] = defaultdict(list)
            while self.queue and self.queue[0][0] == self.now:
                _, kind, key = heapq.heappop(self.queue)
                batch[kind].append(key)
            terminal_lots = []
            # Every release precedes every acquisition at this minute.
            for kind in ("process_end", "setup_end"):
                for operation_id in batch[kind]:
                    if not self.finish(operation_id, kind):
                        return self.result()
                    lot, index, _ = self.source[operation_id]
                    if kind == "process_end" and index == len(lot.operations) - 1:
                        terminal_lots.append(lot)
                        self.completed_lots.add(lot.lot_id)
            # Every receipt precedes every withdrawal, including delivery.
            for supply_id in batch["supply"]:
                supply = self.supplies[supply_id]
                self.material_change("supply_receipt", supply.item_id, supply.quantity,
                                     supply_id=supply_id, original_at_min=supply.at_min)
            for lot in sorted(terminal_lots, key=lambda value: value.lot_id):
                self.material_change("lot_output", lot.item_id, lot.quantity, lot_id=lot.lot_id)
            for lot in sorted(terminal_lots, key=lambda value: value.lot_id):
                order = self.orders.get(lot.lot_id)
                if order is not None:
                    if self.stock[order.item_id] < order.quantity:
                        self.block("DELIVERY_NOT_READY", order_id=order.order_id)
                        return self.result()
                    self.material_change("order_delivery", order.item_id, -order.quantity,
                                         order_id=order.order_id, lot_id=lot.lot_id)
                    self.deliveries.append({
                        "order_id": order.order_id, "lot_id": lot.lot_id, "item_id": order.item_id,
                        "quantity": order.quantity, "completion_min": self.now, "due_min": order.due_min,
                        "tardiness_min": max(0, self.now - order.due_min),
                        "priority": order.priority, "on_time": self.now <= order.due_min,
                    })
            for key in batch["outage_end"]:
                outage = self.active_outages.pop(key)
                self.emit("machine_outage_end", machine_id=outage.machine_id, disruption_id=key)
            for key in batch["outage_start"]:
                outage = self.outages[key]
                self.active_outages[key] = outage
                self.emit("machine_outage_start", machine_id=outage.machine_id,
                          end_min=outage.end_min, disruption_id=key)
                affected = sorted(
                    operation_id for operation_id, slot in self.slots.items()
                    if slot.machine_id == outage.machine_id and self.state[operation_id] != "completed"
                    and slot.setup_start_min < outage.end_min and outage.start_min < slot.end_min
                )
                if affected:
                    self.block("MACHINE_OUTAGE", machine_id=outage.machine_id,
                               operation_ids=affected,
                               active_operation_ids=sorted(op for op in affected if op in self.open_usage))
                    return self.result()
            for operation_id in batch["setup_start"]:
                if not self.start_setup(operation_id):
                    return self.result()
            if not self.start_processing(batch["process_start"]):
                return self.result()
        if len(self.completed) != len(self.source) or len(self.deliveries) != len(self.orders):
            self.block("INCOMPLETE_EXECUTION")
        else:
            self.emit("execution_completed")
        return self.result()

    def result(self) -> dict:
        completed = self.blocked_reason is None
        usage = self.usage + [
            {**value, "end_min": self.now, "completed": False}
            for intervals in self.open_usage.values() for value in intervals
        ]
        usage.sort(key=lambda value: (
            value["start_min"], value["end_min"], value["resource_kind"],
            value["resource_id"], value["operation_id"], value["kind"],
        ))
        if len(self.material) > MAX_MATERIAL_POINTS or len(usage) > MAX_RESOURCE_SEGMENTS:
            raise DomainError("SIMULATION_LIMIT", "Execution trace limit exceeded; no trace truncated.")
        busy = dict.fromkeys(self.machines, 0)
        setup_minutes = processing_minutes = 0
        for interval in usage:
            if interval["resource_kind"] == "machine":
                duration = interval["end_min"] - interval["start_min"]
                busy[interval["resource_id"]] += duration
                if interval["kind"] == "setup":
                    setup_minutes += duration
                else:
                    processing_minutes += duration
        weighted_tardiness = sum(value["priority"] * value["tardiness_min"] for value in self.deliveries)
        last_completion = max(self.completed.values(), default=0)
        spread = max(busy.values()) - min(busy.values())
        objective = None
        if completed:
            weights = self.snapshot.objective
            objective = {
                "weighted_tardiness": weighted_tardiness, "setup_minutes": setup_minutes,
                "makespan": last_completion, "load_spread": spread,
                "total": weights.weighted_tardiness * weighted_tardiness
                + weights.setup_minutes * setup_minutes + weights.makespan * last_completion
                + weights.load_spread * spread,
            }
        return {
            "status": "completed" if completed else "blocked",
            "executed_until_min": self.now, "blocked_reason": self.blocked_reason,
            "events": self.events, "material_trace": self.material, "resource_usage": usage,
            "order_completions": self.deliveries, "operation_states": dict(sorted(self.state.items())),
            "final_inventory": dict(sorted(self.stock.items())),
            "metrics": {
                "scope": "complete_execution" if completed else "partial_execution",
                "execution_started": True, "completed_operations": len(self.completed),
                "required_operations": len(self.source), "completed_lots": len(self.completed_lots),
                "delivered_orders": len(self.deliveries), "required_orders": len(self.orders),
                "on_time_orders": sum(value["on_time"] for value in self.deliveries),
                "weighted_tardiness": weighted_tardiness, "setup_minutes": setup_minutes,
                "processing_minutes": processing_minutes, "makespan": last_completion if completed else None,
                "last_completion_min": last_completion, "load_spread": spread,
                "machine_busy_minutes": dict(sorted(busy.items())),
                "resource_peak_usage": dict(sorted(self.resource_peak.items())), "objective": objective,
            },
        }


def simulate(problem: CompiledProblem, plan: Plan, *, disruptions=()) -> dict:
    """Validate, then execute a fixed schedule; never sends production commands.

    Optional disruptions use the domain Scenario schema, limited to outages and
    delays. Inserted orders must instead be built, compiled and solved upstream.
    A blocked run has a precise partial trace and no complete objective value.
    """
    if not isinstance(problem, CompiledProblem) or not isinstance(plan, Plan):
        raise DomainError("INVALID_SIMULATION_INPUT", "Expected a CompiledProblem and Plan.")
    validation = validate_plan(problem, plan)
    if validation.valid:
        # The validator reparses to defend against model_copy/model_construct.
        # Execute that same strict representation, not bypassed nested dicts.
        problem = CompiledProblem.model_validate(problem.model_dump(mode="python"))
        plan = Plan.model_validate(plan.model_dump(mode="python"))
    normalized = _disruptions(problem, disruptions)
    if not validation.valid:
        codes = sorted({value.code for value in validation.violations})
        result = {
            "status": "blocked", "executed_until_min": 0,
            "blocked_reason": {"code": "INVALID_PLAN", "at_min": 0, "violation_codes": codes},
            "events": [{"sequence": 0, "at_min": 0, "kind": "validation_failed",
                        "code": "INVALID_PLAN", "violation_codes": codes}],
            "material_trace": [], "resource_usage": [], "order_completions": [],
            "operation_states": {}, "final_inventory": {},
            "metrics": {"scope": "not_executed", "execution_started": False,
                        "completed_operations": 0, "delivered_orders": 0, "objective": None},
        }
    else:
        result = _Execution(problem, plan, normalized).run()
    result.update({
        "synthetic": True, "causal_effect_proven": False,
        "source_hash": validation.snapshot_hash, "snapshot_hash": validation.snapshot_hash,
        "problem_hash": validation.problem_hash, "objective_hash": validation.objective_hash,
        "plan_hash": validation.plan_hash, "disruptions_hash": canonical_digest(normalized),
        "disruptions": [value.model_dump(mode="json", exclude_none=True) for value in normalized],
        "validation": {"valid": validation.valid,
                       "violation_codes": sorted({value.code for value in validation.violations})},
        "event_limit": MAX_EVENTS,
    })
    result["simulation_hash"] = canonical_digest(result)
    return result
