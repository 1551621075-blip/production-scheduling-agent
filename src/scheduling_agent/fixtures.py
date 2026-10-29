"""Original deterministic synthetic fixtures, never production observations."""

from __future__ import annotations

from .domain import (
    Alternative,
    BOMLine,
    Item,
    Lot,
    Machine,
    Material,
    ObjectiveConfig,
    Operation,
    Order,
    Residual,
    Resource,
    ResourceDemand,
    Scenario,
    SetupRule,
    Slot,
    Snapshot,
    Supply,
    Window,
)


def _setups() -> tuple[SetupRule, ...]:
    return tuple(
        SetupRule(
            from_family=source,
            to_family=target,
            duration_min=0 if source == target else 1,
            resources=()
            if source == target
            else (ResourceDemand(resource_id="operator", quantity=1),),
        )
        for source in ("amber", "blue")
        for target in ("amber", "blue")
    )


def _alternative(machine: str, duration: int, *, finish: bool = False) -> Alternative:
    resources = (ResourceDemand(resource_id="operator", quantity=1),)
    if finish:
        resources += (ResourceDemand(resource_id="fixture", quantity=1),)
    return Alternative(
        machine_id=machine, duration_min=duration, resources=resources, qualified=True
    )


def demo_snapshots(now_ms: int) -> dict[str, Snapshot]:
    """Normal, proven-material-shortage, and explicit-missing-input examples.

    A complete normal witness (not a solver result): B finish M1 [0,2),
    A rough M2 [2,4), A finish setup M1 [5,6), processing [6,8).
    B is frozen. A's quantity 3 multiplies its BOM coefficient 2 into 6 raw units.
    The M1 closure [4,5) forbids the otherwise tempting setup [4,5).
    """
    raw = Item(item_id="raw", unit="piece", bom=())
    amber = Item(
        item_id="amber_part",
        unit="piece",
        bom=(BOMLine(component_item_id="raw", step_index=0, units_per_parent=2),),
    )
    blue = Item(
        item_id="blue_part",
        unit="piece",
        bom=(BOMLine(component_item_id="raw", step_index=0, units_per_parent=1),),
    )
    insert_item = Item(
        item_id="insert_part",
        unit="piece",
        bom=(BOMLine(component_item_id="raw", step_index=0, units_per_parent=1),),
    )
    lot_a = Lot(
        lot_id="lot_a",
        item_id="amber_part",
        quantity=3,
        completed_prefix=0,
        residual=None,
        release_min=0,
        operations=(
            Operation(
                operation_id="a_rough",
                family="amber",
                alternatives=(
                    _alternative("M1", 3),
                    _alternative("M2", 2),
                ),
            ),
            Operation(
                operation_id="a_finish",
                family="amber",
                alternatives=(_alternative("M1", 2, finish=True),),
            ),
        ),
    )
    lot_b = Lot(
        lot_id="lot_b",
        item_id="blue_part",
        quantity=2,
        release_min=0,
        operations=(
            Operation(operation_id="b_rough", family="blue", alternatives=(_alternative("M2", 2),)),
            Operation(
                operation_id="b_finish",
                family="blue",
                alternatives=(_alternative("M1", 2, finish=True),),
            ),
        ),
        completed_prefix=1,
        residual=None,
    )
    normal = Snapshot(
        snapshot_id="synthetic-normal-v1",
        plant_id="synthetic-plant",
        revision=1,
        observed_at_ms=now_ms,
        horizon_min=12,
        items=(raw, amber, blue, insert_item),
        materials=tuple(
            Material(item_id=item.item_id, initial_stock=0)
            for item in (raw, amber, blue, insert_item)
        ),
        machines=(
            Machine(
                machine_id="M1",
                initial_family="blue",
                qualified_families=("amber", "blue"),
                calendar=(Window(start_min=0, end_min=4), Window(start_min=5, end_min=12)),
                setups=_setups(),
            ),
            Machine(
                machine_id="M2",
                initial_family="amber",
                qualified_families=("amber", "blue"),
                calendar=(Window(start_min=0, end_min=12),),
                setups=_setups(),
            ),
        ),
        resources=(
            Resource(
                resource_id="operator",
                capacity=1,
                qualified_families=("amber", "blue"),
                calendar=(Window(start_min=0, end_min=12),),
            ),
            Resource(
                resource_id="fixture",
                capacity=1,
                qualified_families=("amber", "blue"),
                calendar=(Window(start_min=0, end_min=12),),
            ),
        ),
        lots=(lot_a, lot_b),
        orders=(
            Order(
                order_id="order_a",
                lot_id="lot_a",
                item_id="amber_part",
                quantity=3,
                due_min=8,
                priority=2,
            ),
            Order(
                order_id="order_b",
                lot_id="lot_b",
                item_id="blue_part",
                quantity=2,
                due_min=2,
                priority=1,
            ),
        ),
        supplies=(
            Supply(supply_id="raw_early", item_id="raw", quantity=6, at_min=0),
            Supply(supply_id="raw_late", item_id="raw", quantity=2, at_min=6),
        ),
        freeze_until_min=1,
        commitments=(
            Slot(
                operation_id="b_finish",
                machine_id="M1",
                setup_start_min=0,
                start_min=0,
                end_min=2,
                predecessor_id=None,
            ),
        ),
        objective=ObjectiveConfig(
            weighted_tardiness=10, setup_minutes=2, makespan=1, load_spread=1
        ),
    )
    shortage = normal.model_dump(mode="json")
    shortage["snapshot_id"] = "synthetic-infeasible-v1"
    shortage["supplies"] = []
    missing = normal.model_dump(mode="json")
    missing["snapshot_id"] = "synthetic-missing-v1"
    missing["materials"][0]["initial_stock"] = None
    missing["orders"][0]["due_min"] = None
    return {
        "normal": normal,
        "infeasible": Snapshot.model_validate(shortage),
        "missing": Snapshot.model_validate(missing),
    }


def demo_scenarios(snapshot: Snapshot) -> dict[str, Scenario]:
    """Four explicit perturbations of this module's normal-family snapshots.

    No source timestamp, revision, or frozen commitment is changed here.  The
    compiler is responsible for applying and validating the selected payload.
    """
    if not {"raw", "insert_part"} <= {item.item_id for item in snapshot.items}:
        raise ValueError("demo scenarios require the original synthetic fixture items")
    if "M1" not in {machine.machine_id for machine in snapshot.machines}:
        raise ValueError("demo scenarios require M1")
    if not {"operator", "fixture"} <= {resource.resource_id for resource in snapshot.resources}:
        raise ValueError("demo scenarios require operator and fixture resources")
    if "raw_early" not in {supply.supply_id for supply in snapshot.supplies}:
        raise ValueError("demo supply delay requires raw_early")
    if snapshot.horizon_min < 10:
        raise ValueError("demo scenarios require horizon_min >= 10")
    if "lot_insert" in {lot.lot_id for lot in snapshot.lots}:
        raise ValueError("demo insertion lot already exists")
    if "order_insert" in {order.order_id for order in snapshot.orders}:
        raise ValueError("demo insertion order already exists")
    if "insert_finish" in {op.operation_id for lot in snapshot.lots for op in lot.operations}:
        raise ValueError("demo insertion operation already exists")
    lot = Lot(
        lot_id="lot_insert",
        item_id="insert_part",
        quantity=2,
        completed_prefix=0,
        residual=None,
        release_min=2,
        operations=(
            Operation(
                operation_id="insert_finish",
                family="amber",
                alternatives=(_alternative("M1", 2, finish=True),),
            ),
        ),
    )
    order = Order(
        order_id="order_insert",
        lot_id="lot_insert",
        item_id="insert_part",
        quantity=2,
        due_min=9,
        priority=3,
    )
    return {
        "baseline": Scenario(kind="baseline"),
        "insert_order": Scenario(kind="insert_order", lot=lot, order=order),
        "machine_outage": Scenario(kind="machine_outage", machine_id="M1", start_min=6, end_min=7),
        "supply_delay": Scenario(kind="supply_delay", supply_id="raw_early", at_min=6),
    }


def tiny_snapshots(now_ms: int = 0) -> dict[str, Snapshot]:
    """Small independent-oracle inputs (all H <= 8 and <= 2 pending ops).

    Includes a same-minute component handoff, batched over-consumption, residual
    WIP, explicit unqualification, and a soft-due schedule with unavoidable delay.
    No precomputed solver status is injected into these records.
    """
    base = Snapshot(
        snapshot_id="tiny-handoff-v1",
        plant_id="synthetic-plant",
        revision=1,
        observed_at_ms=now_ms,
        horizon_min=4,
        items=(
            Item(item_id="component", unit="piece", bom=()),
            Item(
                item_id="finished",
                unit="piece",
                bom=(BOMLine(component_item_id="component", step_index=0, units_per_parent=2),),
            ),
        ),
        materials=(
            Material(item_id="component", initial_stock=0),
            Material(item_id="finished", initial_stock=0),
        ),
        machines=(
            Machine(
                machine_id="tiny_machine",
                initial_family="tiny",
                qualified_families=("tiny",),
                calendar=(Window(start_min=0, end_min=4),),
                setups=(
                    SetupRule(from_family="tiny", to_family="tiny", duration_min=0, resources=()),
                ),
            ),
        ),
        resources=(),
        lots=(
            Lot(
                lot_id="component_lot",
                item_id="component",
                quantity=2,
                completed_prefix=0,
                residual=None,
                release_min=0,
                operations=(
                    Operation(
                        operation_id="make_component",
                        family="tiny",
                        alternatives=(
                            Alternative(
                                machine_id="tiny_machine",
                                duration_min=1,
                                resources=(),
                                qualified=True,
                            ),
                        ),
                    ),
                ),
            ),
            Lot(
                lot_id="finished_lot",
                item_id="finished",
                quantity=1,
                completed_prefix=0,
                residual=None,
                release_min=0,
                operations=(
                    Operation(
                        operation_id="make_finished",
                        family="tiny",
                        alternatives=(
                            Alternative(
                                machine_id="tiny_machine",
                                duration_min=1,
                                resources=(),
                                qualified=True,
                            ),
                        ),
                    ),
                ),
            ),
        ),
        orders=(
            Order(
                order_id="tiny_order",
                lot_id="finished_lot",
                item_id="finished",
                quantity=1,
                due_min=2,
                priority=1,
            ),
        ),
        supplies=(),
        freeze_until_min=0,
        commitments=(),
        objective=ObjectiveConfig(weighted_tardiness=3, setup_minutes=1, makespan=1, load_spread=0),
    )
    late = base.model_dump(mode="json")
    late["snapshot_id"] = "tiny-soft-due-v1"
    late["orders"][0]["due_min"] = 0
    residual = base.model_dump(mode="json")
    residual["snapshot_id"] = "tiny-residual-v1"
    residual["lots"][0]["residual"] = Residual(
        machine_id="tiny_machine",
        remaining_min=1,
    ).model_dump(mode="json")
    unqualified = base.model_dump(mode="json")
    unqualified["snapshot_id"] = "tiny-unqualified-v1"
    unqualified["lots"][0]["operations"][0]["alternatives"][0]["qualified"] = False
    overconsume = base.model_dump(mode="json")
    overconsume["snapshot_id"] = "tiny-shortage-v1"
    overconsume["lots"] = [overconsume["lots"][1]]
    overconsume["materials"][0]["initial_stock"] = 1
    batched = base.model_dump(mode="json")
    batched["snapshot_id"] = "tiny-batched-debits-v1"
    batched["materials"][0]["initial_stock"] = 2
    batched["lots"] = [batched["lots"][1]]
    second = batched["lots"][0].copy()
    second["lot_id"] = "second_finished_lot"
    second["operations"] = [
        {
            "operation_id": "make_second_finished",
            "family": "tiny",
            "alternatives": [
                {
                    "machine_id": "tiny_machine_2",
                    "duration_min": 1,
                    "resources": [],
                    "qualified": True,
                }
            ],
        }
    ]
    batched["lots"].append(second)
    batched["machines"].append({**batched["machines"][0], "machine_id": "tiny_machine_2"})
    batched["commitments"] = [
        {
            "operation_id": operation_id,
            "machine_id": machine_id,
            "setup_start_min": 0,
            "start_min": 0,
            "end_min": 1,
            "predecessor_id": None,
        }
        for operation_id, machine_id in (
            ("make_finished", "tiny_machine"),
            ("make_second_finished", "tiny_machine_2"),
        )
    ]
    batched["freeze_until_min"] = 1
    return {
        "handoff": base,
        "soft_due": Snapshot.model_validate(late),
        "residual": Snapshot.model_validate(residual),
        "unqualified": Snapshot.model_validate(unqualified),
        "shortage": Snapshot.model_validate(overconsume),
        "batched_debits": Snapshot.model_validate(batched),
    }


# Explicit alias for callers naming the independent test purpose rather than size.
oracle_snapshots = tiny_snapshots
