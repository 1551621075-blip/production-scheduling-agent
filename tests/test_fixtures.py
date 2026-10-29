"""Original fixtures are deterministic, structurally complete, and deliberately small."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scheduling_agent.domain import Snapshot, canonical_digest
from scheduling_agent.fixtures import (
    demo_scenarios,
    demo_snapshots,
    oracle_snapshots,
    tiny_snapshots,
)

NOW = 1_800_000_000_000


def test_demos_are_deterministic_independent_objects():
    first, second = demo_snapshots(NOW), demo_snapshots(NOW)
    assert tuple(first) == ("normal", "infeasible", "missing")
    assert first == second
    assert first["normal"] is not second["normal"]
    assert len({canonical_digest(snapshot) for snapshot in first.values()}) == 3
    assert all(snapshot.observed_at_ms == NOW for snapshot in first.values())
    assert all(snapshot.plant_id == "synthetic-plant" for snapshot in first.values())
    assert all(snapshot.revision == 1 for snapshot in first.values())


@pytest.mark.parametrize("bad_time", [True, False, "123", 123.0, -1])
def test_fixture_generator_does_not_coerce_clock(bad_time):
    with pytest.raises(ValidationError):
        demo_snapshots(bad_time)


def test_normal_has_whole_lot_bom_wip_resources_setup_and_freeze():
    snapshot = demo_snapshots(NOW)["normal"]
    assert snapshot.missing_inputs() == ()
    lots = {lot.lot_id: lot for lot in snapshot.lots}
    amber = next(item for item in snapshot.items if item.item_id == "amber_part")
    assert lots["lot_a"].quantity * amber.bom[0].units_per_parent == 6
    assert lots["lot_b"].completed_prefix == 1
    assert len(lots["lot_a"].operations[0].alternatives) == 2
    finish = lots["lot_a"].operations[1].alternatives[0]
    assert {demand.resource_id for demand in finish.resources} == {"operator", "fixture"}
    assert snapshot.freeze_until_min == 1
    assert snapshot.commitments[0].operation_id == "b_finish"
    assert snapshot.commitments[0].setup_start_min < snapshot.freeze_until_min
    assert all(len(machine.setups) == 4 for machine in snapshot.machines)
    assert any(
        rule.duration_min > 0 and rule.resources
        for machine in snapshot.machines
        for rule in machine.setups
    )
    assert all(
        Snapshot.model_validate_json(s.model_dump_json()) == s for s in demo_snapshots(NOW).values()
    )


def test_shortage_is_explicit_and_missing_is_not_infeasibility():
    cases = demo_snapshots(NOW)
    shortage = cases["infeasible"]
    assert shortage.supplies == ()
    assert all(material.initial_stock == 0 for material in shortage.materials)
    assert shortage.missing_inputs() == ()
    assert shortage.lots == cases["normal"].lots
    missing = cases["missing"]
    assert missing.missing_inputs() == ("materials[0].initial_stock", "orders[0].due_min")
    assert missing.supplies == cases["normal"].supplies
    assert missing.commitments == cases["normal"].commitments


def test_scenario_payloads_are_original_deterministic_and_nonmutating():
    snapshot = demo_snapshots(NOW)["normal"]
    before = canonical_digest(snapshot)
    scenarios = demo_scenarios(snapshot)
    assert tuple(scenarios) == ("baseline", "insert_order", "machine_outage", "supply_delay")
    assert scenarios == demo_scenarios(snapshot)
    assert canonical_digest(snapshot) == before
    assert snapshot.observed_at_ms == NOW
    assert scenarios["insert_order"].lot.lot_id == "lot_insert"
    assert scenarios["insert_order"].lot.quantity == scenarios["insert_order"].order.quantity == 2
    assert scenarios["insert_order"].lot.operations[0].operation_id == "insert_finish"
    assert scenarios["machine_outage"].machine_id == "M1"
    assert (scenarios["machine_outage"].start_min, scenarios["machine_outage"].end_min) == (6, 7)
    assert scenarios["supply_delay"].supply_id == "raw_early"
    assert scenarios["supply_delay"].at_min == 6


def test_demo_scenarios_fail_explicitly_for_other_fixture_families():
    with pytest.raises(ValueError, match="fixture items"):
        demo_scenarios(tiny_snapshots()["handoff"])
    with pytest.raises(ValueError, match="raw_early"):
        demo_scenarios(demo_snapshots(NOW)["infeasible"])


def test_tiny_oracle_cases_are_deterministic_and_within_exhaustive_bounds():
    cases = tiny_snapshots(NOW)
    assert cases == tiny_snapshots(NOW) == oracle_snapshots(NOW)
    assert {"handoff", "soft_due", "residual", "unqualified", "shortage"} <= cases.keys()
    for snapshot in cases.values():
        assert snapshot.horizon_min <= 12
        assert sum(len(lot.operations) - lot.completed_prefix for lot in snapshot.lots) <= 4
        assert not snapshot.missing_inputs()
        assert snapshot.observed_at_ms == NOW
        assert Snapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


def test_tiny_handoff_requires_production_and_explicit_bom_multiplication():
    snapshot = tiny_snapshots()["handoff"]
    assert all(material.initial_stock == 0 for material in snapshot.materials)
    assert snapshot.supplies == ()
    source, consumer = snapshot.lots
    assert source.quantity == 2
    product = next(item for item in snapshot.items if item.item_id == consumer.item_id)
    assert product.bom[0].component_item_id == source.item_id
    assert consumer.quantity * product.bom[0].units_per_parent == source.quantity


def test_tiny_soft_due_residual_unqualified_and_shortage_are_distinct():
    cases = tiny_snapshots()
    assert cases["soft_due"].orders[0].due_min == 0
    assert cases["residual"].lots[0].residual.remaining_min == 1
    assert not cases["unqualified"].lots[0].operations[0].alternatives[0].qualified
    assert cases["shortage"].materials[0].initial_stock == 1
    assert len(cases["shortage"].lots) == 1
    assert len({canonical_digest(snapshot) for snapshot in cases.values()}) == len(cases)


def test_tiny_batched_debits_cannot_double_spend_simultaneous_stock():
    snapshot = tiny_snapshots()["batched_debits"]
    assert snapshot.materials[0].initial_stock == 2
    assert len(snapshot.commitments) == len(snapshot.machines) == len(snapshot.lots) == 2
    assert all(slot.start_min == 0 for slot in snapshot.commitments)
    assert all(slot.setup_start_min < snapshot.freeze_until_min for slot in snapshot.commitments)
    assert len({slot.machine_id for slot in snapshot.commitments}) == 2
    product = next(item for item in snapshot.items if item.item_id == "finished")
    assert sum(lot.quantity * product.bom[0].units_per_parent for lot in snapshot.lots) == 4
