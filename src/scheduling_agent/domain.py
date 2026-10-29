"""Strict immutable scheduling contracts; no solver or compiler dependencies.

Times are integer minutes, quantities are indivisible item units, and intervals
are half open.  Structural validation does not turn known scheduling conflicts
into input errors: empty alternatives, zero inventory, and an outage covering a
commitment remain valid inputs for an infeasibility proof.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

MAX_HORIZON = 1440
MAX_REMAINING_OPERATIONS = 24
MAX_MACHINES = 8
MAX_RESOURCES = 12
MAX_ITEMS = 20
MAX_LOTS = 20
MAX_ORDERS = 20
MAX_SUPPLIES = 64
MAX_QUANTITY = 1_000_000
MAX_STOCK = 1_000_000_000_000
MAX_CAPACITY = 64
MAX_WEIGHT = 1000
# Safely below int64 and the exact-integer limit of an IEEE 754 double.
MAX_OBJECTIVE = 1_000_000_000_000

Identifier = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=512)]
Minute = Annotated[int, Field(strict=True, ge=0, le=MAX_HORIZON)]
Duration = Annotated[int, Field(strict=True, ge=1, le=MAX_HORIZON)]
Quantity = Annotated[int, Field(strict=True, ge=1, le=MAX_QUANTITY)]
Stock = Annotated[int, Field(strict=True, ge=0, le=MAX_STOCK)]
PositiveStock = Annotated[int, Field(strict=True, ge=1, le=MAX_STOCK)]
Capacity = Annotated[int, Field(strict=True, ge=1, le=MAX_CAPACITY)]
Weight = Annotated[int, Field(strict=True, ge=0, le=MAX_WEIGHT)]
Priority = Annotated[int, Field(strict=True, ge=1, le=MAX_WEIGHT)]
ObjectiveInteger = Annotated[int, Field(strict=True, ge=0, le=MAX_OBJECTIVE)]
Hash = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _unique(values: tuple[Any, ...], attribute: str, label: str) -> None:
    keys = [getattr(value, attribute) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {label}")


def _calendar(windows: tuple[Window, ...]) -> None:
    ordered = sorted(windows, key=lambda window: window.start_min)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if previous.end_min > current.start_min:
            raise ValueError("calendar windows overlap")


class Window(FrozenModel):
    start_min: Minute
    end_min: Minute

    @model_validator(mode="after")
    def ordered(self) -> Window:
        if self.end_min <= self.start_min:
            raise ValueError("window must have positive length")
        return self


class ResourceDemand(FrozenModel):
    resource_id: Identifier
    quantity: Capacity


class BOMLine(FrozenModel):
    component_item_id: Identifier
    step_index: Annotated[int, Field(strict=True, ge=0, le=23)]
    units_per_parent: Quantity


class Item(FrozenModel):
    item_id: Identifier
    unit: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=32)]
    bom: tuple[BOMLine, ...] = Field(max_length=MAX_ITEMS * 24)

    @model_validator(mode="after")
    def unique_lines(self) -> Item:
        if not self.unit.strip():
            raise ValueError("item unit must not be blank")
        keys = [(line.component_item_id, line.step_index) for line in self.bom]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate BOM component and step")
        return self


class Material(FrozenModel):
    item_id: Identifier
    initial_stock: Stock | None


class SetupRule(FrozenModel):
    from_family: Identifier
    to_family: Identifier
    duration_min: Minute
    resources: tuple[ResourceDemand, ...] = Field(max_length=MAX_RESOURCES)

    @model_validator(mode="after")
    def unique_resources(self) -> SetupRule:
        _unique(self.resources, "resource_id", "setup resource demand")
        return self


class Machine(FrozenModel):
    machine_id: Identifier
    initial_family: Identifier
    qualified_families: tuple[Identifier, ...] = Field(max_length=MAX_REMAINING_OPERATIONS)
    calendar: tuple[Window, ...] = Field(max_length=MAX_HORIZON)
    setups: tuple[SetupRule, ...] = Field(max_length=25 * 24)

    @model_validator(mode="after")
    def unique_fields(self) -> Machine:
        if len(set(self.qualified_families)) != len(self.qualified_families):
            raise ValueError("duplicate machine qualification")
        keys = [(rule.from_family, rule.to_family) for rule in self.setups]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate machine setup transition")
        _calendar(self.calendar)
        return self


class Resource(FrozenModel):
    resource_id: Identifier
    capacity: Capacity
    qualified_families: tuple[Identifier, ...] = Field(max_length=MAX_REMAINING_OPERATIONS)
    calendar: tuple[Window, ...] = Field(max_length=MAX_HORIZON)

    @model_validator(mode="after")
    def unique_fields(self) -> Resource:
        if len(set(self.qualified_families)) != len(self.qualified_families):
            raise ValueError("duplicate resource qualification")
        _calendar(self.calendar)
        return self


class Alternative(FrozenModel):
    machine_id: Identifier
    duration_min: Duration
    resources: tuple[ResourceDemand, ...] = Field(max_length=MAX_RESOURCES)
    qualified: StrictBool

    @model_validator(mode="after")
    def unique_resources(self) -> Alternative:
        _unique(self.resources, "resource_id", "processing resource demand")
        return self


class Operation(FrozenModel):
    operation_id: Identifier
    family: Identifier
    alternatives: tuple[Alternative, ...] = Field(max_length=MAX_MACHINES)

    @model_validator(mode="after")
    def unambiguous_machine_mode(self) -> Operation:
        _unique(self.alternatives, "machine_id", "operation machine alternative")
        return self


class Residual(FrozenModel):
    machine_id: Identifier
    remaining_min: Duration


class Lot(FrozenModel):
    lot_id: Identifier
    item_id: Identifier
    quantity: Quantity
    release_min: Minute
    operations: tuple[Operation, ...] = Field(min_length=1, max_length=24)
    completed_prefix: Annotated[int, Field(strict=True, ge=0, le=24)]
    residual: Residual | None

    @model_validator(mode="after")
    def valid_progress(self) -> Lot:
        _unique(self.operations, "operation_id", "lot operation ID")
        if self.completed_prefix > len(self.operations):
            raise ValueError("completed_prefix exceeds route length")
        if self.residual is not None:
            if self.completed_prefix == len(self.operations):
                raise ValueError("residual requires a pending operation")
            if self.release_min != 0:
                raise ValueError("residual requires release_min=0")
            operation = self.operations[self.completed_prefix]
            if self.residual.machine_id not in {a.machine_id for a in operation.alternatives}:
                raise ValueError("residual machine is not an operation alternative")
        return self


class Order(FrozenModel):
    order_id: Identifier
    lot_id: Identifier
    item_id: Identifier
    quantity: Quantity
    due_min: Minute | None
    priority: Priority


class Supply(FrozenModel):
    supply_id: Identifier
    item_id: Identifier
    quantity: PositiveStock
    at_min: Minute


class Slot(FrozenModel):
    operation_id: Identifier
    machine_id: Identifier
    setup_start_min: Minute
    start_min: Minute
    end_min: Minute
    predecessor_id: Identifier | None

    @model_validator(mode="after")
    def ordered(self) -> Slot:
        if not self.setup_start_min <= self.start_min < self.end_min:
            raise ValueError("slot requires setup_start_min <= start_min < end_min")
        if self.predecessor_id == self.operation_id:
            raise ValueError("operation cannot be its own machine predecessor")
        return self


class ObjectiveConfig(FrozenModel):
    weighted_tardiness: Weight
    setup_minutes: Weight
    makespan: Weight
    load_spread: Weight

    @model_validator(mode="after")
    def nonzero(self) -> ObjectiveConfig:
        if not any((self.weighted_tardiness, self.setup_minutes, self.makespan, self.load_spread)):
            raise ValueError("at least one objective weight must be nonzero")
        return self


class ObjectiveBreakdown(FrozenModel):
    weighted_tardiness: ObjectiveInteger
    setup_minutes: ObjectiveInteger
    makespan: ObjectiveInteger
    load_spread: ObjectiveInteger
    total: ObjectiveInteger


class Snapshot(FrozenModel):
    synthetic: Literal[True] = True
    snapshot_id: Identifier
    plant_id: Identifier
    revision: Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
    observed_at_ms: Annotated[int, Field(strict=True, ge=0, le=253_402_300_799_999)]
    horizon_min: Duration
    items: tuple[Item, ...] = Field(min_length=1, max_length=MAX_ITEMS)
    materials: tuple[Material, ...] = Field(min_length=1, max_length=MAX_ITEMS)
    machines: tuple[Machine, ...] = Field(min_length=1, max_length=MAX_MACHINES)
    resources: tuple[Resource, ...] = Field(max_length=MAX_RESOURCES)
    lots: tuple[Lot, ...] = Field(max_length=MAX_LOTS)
    orders: tuple[Order, ...] = Field(max_length=MAX_ORDERS)
    supplies: tuple[Supply, ...] = Field(max_length=MAX_SUPPLIES)
    freeze_until_min: Minute
    commitments: tuple[Slot, ...] = Field(max_length=MAX_REMAINING_OPERATIONS)
    objective: ObjectiveConfig

    @field_validator("synthetic", mode="before")
    @classmethod
    def require_synthetic_true(cls, value: Any) -> Literal[True]:
        if value is not True:
            raise ValueError("synthetic must be the boolean True")
        return True

    def missing_inputs(self) -> tuple[str, ...]:
        return missing_inputs(self)

    @model_validator(mode="after")
    def references_and_bounds(self) -> Snapshot:
        for values, attribute, label in (
            (self.items, "item_id", "item ID"),
            (self.materials, "item_id", "material item ID"),
            (self.machines, "machine_id", "machine ID"),
            (self.resources, "resource_id", "resource ID"),
            (self.lots, "lot_id", "lot ID"),
            (self.orders, "order_id", "order ID"),
            (self.supplies, "supply_id", "supply ID"),
            (self.commitments, "operation_id", "commitment operation ID"),
        ):
            _unique(values, attribute, label)
        items = {item.item_id: item for item in self.items}
        machines = {machine.machine_id: machine for machine in self.machines}
        resources = {resource.resource_id: resource for resource in self.resources}
        lots = {lot.lot_id: lot for lot in self.lots}
        if set(items) != {material.item_id for material in self.materials}:
            raise ValueError("materials must specify every item exactly once (None is allowed)")
        if self.freeze_until_min > self.horizon_min:
            raise ValueError("freeze_until_min exceeds horizon")
        for owner in (*self.machines, *self.resources):
            if any(window.end_min > self.horizon_min for window in owner.calendar):
                raise ValueError("calendar window exceeds horizon")
        for item in self.items:
            for line in item.bom:
                if line.component_item_id not in items:
                    raise ValueError("BOM references unknown component item")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(item_id: str) -> None:
            if item_id in visiting:
                raise ValueError("BOM cycle")
            if item_id in visited:
                return
            visiting.add(item_id)
            for line in items[item_id].bom:
                visit(line.component_item_id)
            visiting.remove(item_id)
            visited.add(item_id)

        for item_id in items:
            visit(item_id)
        all_operations = tuple(op for lot in self.lots for op in lot.operations)
        _unique(all_operations, "operation_id", "global operation ID")
        pending = {
            op.operation_id: op
            for lot in self.lots
            for op in lot.operations[lot.completed_prefix :]
        }
        if len(pending) > MAX_REMAINING_OPERATIONS:
            raise ValueError("more than 24 remaining operations")
        machine_families: dict[str, set[str]] = {machine_id: set() for machine_id in machines}

        def check_demands(demands: tuple[ResourceDemand, ...]) -> None:
            for demand in demands:
                if demand.resource_id not in resources:
                    raise ValueError("demand references unknown resource")

        for lot in self.lots:
            if lot.item_id not in items:
                raise ValueError("lot references unknown item")
            if lot.release_min > self.horizon_min:
                raise ValueError("lot release exceeds horizon")
            for line in items[lot.item_id].bom:
                if line.step_index >= len(lot.operations):
                    raise ValueError("BOM step_index is absent from a producing lot route")
            for operation in lot.operations:
                for alternative in operation.alternatives:
                    if alternative.machine_id not in machines:
                        raise ValueError("alternative references unknown machine")
                    check_demands(alternative.resources)
                    if operation.operation_id in pending:
                        machine_families[alternative.machine_id].add(operation.family)
        for machine in self.machines:
            for rule in machine.setups:
                check_demands(rule.resources)
            transitions = {(rule.from_family, rule.to_family) for rule in machine.setups}
            targets = machine_families[machine.machine_id]
            required = {
                (source, target)
                for source in targets | {machine.initial_family}
                for target in targets
            }
            if not required <= transitions:
                absent = sorted(required - transitions)
                raise ValueError(f"incomplete setup table for {machine.machine_id}: {absent}")
        order_lots: set[str] = set()
        for order in self.orders:
            if order.lot_id not in lots:
                raise ValueError("order references unknown lot")
            lot = lots[order.lot_id]
            if order.item_id != lot.item_id or order.quantity != lot.quantity:
                raise ValueError("order item and quantity must exactly match its lot")
            if order.lot_id in order_lots:
                raise ValueError("two orders cannot bind the same lot")
            if lot.completed_prefix == len(lot.operations):
                raise ValueError("order requires a lot with remaining operations")
            order_lots.add(order.lot_id)
        for supply in self.supplies:
            if supply.item_id not in items:
                raise ValueError("supply references unknown item")
            if supply.at_min > self.horizon_min:
                raise ValueError("supply time exceeds horizon")
        for commitment in self.commitments:
            if commitment.operation_id not in pending:
                raise ValueError("commitment references unknown or completed operation")
            operation = pending[commitment.operation_id]
            if commitment.machine_id not in {a.machine_id for a in operation.alternatives}:
                raise ValueError("commitment machine is not an operation alternative")
            if commitment.end_min > self.horizon_min:
                raise ValueError("commitment exceeds horizon")
            if commitment.predecessor_id is not None:
                if commitment.predecessor_id not in pending:
                    raise ValueError("commitment references unknown or completed predecessor")
                predecessor = pending[commitment.predecessor_id]
                if commitment.machine_id not in {a.machine_id for a in predecessor.alternatives}:
                    raise ValueError("commitment predecessor cannot use its machine")
        return self


class Scenario(FrozenModel):
    kind: Literal["baseline", "insert_order", "machine_outage", "supply_delay"]
    lot: Lot | None = None
    order: Order | None = None
    machine_id: Identifier | None = None
    start_min: Minute | None = None
    end_min: Minute | None = None
    supply_id: Identifier | None = None
    at_min: Minute | None = None

    @model_validator(mode="after")
    def exact_payload(self) -> Scenario:
        required = {
            "baseline": set(),
            "insert_order": {"lot", "order"},
            "machine_outage": {"machine_id", "start_min", "end_min"},
            "supply_delay": {"supply_id", "at_min"},
        }[self.kind]
        supplied = {
            field
            for field in (
                "lot",
                "order",
                "machine_id",
                "start_min",
                "end_min",
                "supply_id",
                "at_min",
            )
            if getattr(self, field) is not None
        }
        if supplied != required:
            raise ValueError(f"{self.kind} requires exactly {sorted(required)}")
        if self.kind == "machine_outage" and self.start_min >= self.end_min:
            raise ValueError("outage must have positive length")
        if self.kind == "insert_order":
            assert self.lot is not None and self.order is not None
            if self.order.due_min is None:
                raise ValueError("inserted order requires an explicit due_min")
            if (self.order.lot_id, self.order.item_id, self.order.quantity) != (
                self.lot.lot_id,
                self.lot.item_id,
                self.lot.quantity,
            ):
                raise ValueError("inserted order must exactly match inserted lot")
            if self.lot.completed_prefix != 0 or self.lot.residual is not None:
                raise ValueError("inserted lot must not contain historical progress")
        return self


class MaterialAmount(FrozenModel):
    item_id: Identifier
    quantity: PositiveStock


class CompiledOperation(FrozenModel):
    operation_id: Identifier
    lot_id: Identifier
    item_id: Identifier
    quantity: Quantity
    step_index: Annotated[int, Field(strict=True, ge=0, le=23)]
    family: Identifier
    alternatives: tuple[Alternative, ...] = Field(max_length=MAX_MACHINES)
    release_min: Minute
    predecessor_id: Identifier | None
    is_terminal: StrictBool
    residual: Residual | None
    consumes: tuple[MaterialAmount, ...] = Field(max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def unique_fields(self) -> CompiledOperation:
        _unique(self.alternatives, "machine_id", "compiled machine alternative")
        _unique(self.consumes, "item_id", "compiled consumption item")
        return self


class CompiledProblem(FrozenModel):
    snapshot: Snapshot
    snapshot_hash: Hash
    problem_hash: Hash
    objective_hash: Hash
    compiler_version: Identifier
    operations: tuple[CompiledOperation, ...] = Field(max_length=MAX_REMAINING_OPERATIONS)

    @model_validator(mode="after")
    def unique_operations(self) -> CompiledProblem:
        _unique(self.operations, "operation_id", "compiled operation ID")
        return self

    @property
    def operations_by_id(self) -> Mapping[str, CompiledOperation]:
        return MappingProxyType(
            {operation.operation_id: operation for operation in self.operations}
        )

    @property
    def required_operations(self) -> tuple[CompiledOperation, ...]:
        return self.operations


class Plan(FrozenModel):
    plan_id: Identifier
    snapshot_hash: Hash
    problem_hash: Hash
    objective_hash: Hash
    slots: tuple[Slot, ...] = Field(max_length=MAX_REMAINING_OPERATIONS)
    objective: ObjectiveBreakdown

    @property
    def plan_hash(self) -> str:
        """Digest serialized plan content without a serialized self-reference."""
        return canonical_digest(self.model_dump(mode="json"))


class SolverStatus(StrEnum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class SolveResult(FrozenModel):
    status: SolverStatus
    mode: Literal["feasibility", "optimize"]
    plan: Plan | None
    best_bound: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None
    wall_time_s: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    solver_version: Text
    error: Text | None = None

    @model_validator(mode="after")
    def honest_candidate(self) -> SolveResult:
        feasible = self.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
        if feasible != (self.plan is not None):
            raise ValueError("only OPTIMAL or FEASIBLE results must contain a plan")
        if (self.status == SolverStatus.ERROR) != (self.error is not None):
            raise ValueError("ERROR requires an error; other statuses must not contain one")
        if self.mode == "feasibility" and self.status == SolverStatus.OPTIMAL:
            raise ValueError("feasibility search must report FEASIBLE, not OPTIMAL")
        if self.mode == "feasibility" and self.best_bound is not None:
            raise ValueError("feasibility search has no objective bound")
        if self.status == SolverStatus.ERROR and self.best_bound is not None:
            raise ValueError("failed solve has no objective bound")
        return self

    @property
    def candidate(self) -> Plan | None:
        return self.plan


class Violation(FrozenModel):
    code: Identifier
    message: Text
    operation_id: Identifier | None = None
    path: Text | None = None


class InventoryPoint(FrozenModel):
    item_id: Identifier
    at_min: Minute
    received: Annotated[int, Field(strict=True, ge=0, le=100_000_000_000_000_000)]
    consumed: Annotated[int, Field(strict=True, ge=0, le=100_000_000_000_000_000)]
    balance: Annotated[
        int, Field(strict=True, ge=-100_000_000_000_000_000, le=100_000_000_000_000_000)
    ]


class Activity(FrozenModel):
    resource_id: Identifier
    resource_kind: Literal["machine", "resource"]
    operation_id: Identifier
    kind: Literal["setup", "processing"]
    start_min: Minute
    end_min: Minute
    demand: Capacity


class ValidationReport(FrozenModel):
    valid: StrictBool
    violations: tuple[Violation, ...]
    objective: ObjectiveBreakdown | None
    inventory: tuple[InventoryPoint, ...]
    activities: tuple[Activity, ...]
    snapshot_hash: Hash
    problem_hash: Hash
    objective_hash: Hash
    plan_hash: Hash

    @model_validator(mode="after")
    def honest_validity(self) -> ValidationReport:
        if self.valid and (self.violations or self.objective is None):
            raise ValueError("valid report requires objective and no violations")
        if not self.valid and not self.violations:
            raise ValueError("invalid report requires violations")
        return self


def missing_inputs(snapshot: Snapshot) -> tuple[str, ...]:
    """Exact editable JSON paths; absent structural fields are schema errors."""
    paths = [
        f"materials[{index}].initial_stock"
        for index, material in enumerate(snapshot.materials)
        if material.initial_stock is None
    ]
    paths.extend(
        f"orders[{index}].due_min"
        for index, order in enumerate(snapshot.orders)
        if order.due_min is None
    )
    return tuple(paths)


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Canonical UTF-8 JSON contract: sorted object keys, ordered arrays, no NaN."""
    return json.dumps(
        _json_value(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    """Content SHA-256, not an authenticity signature."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
