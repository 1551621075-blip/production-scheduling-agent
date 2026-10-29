"""Strict public requests retain the scheduling domain's integer and identity boundaries."""

import copy
import json

import pytest
from pydantic import ValidationError

from scheduling_agent.domain import MAX_STOCK, Scenario, Snapshot
from scheduling_agent.fixtures import demo_snapshots
from scheduling_agent.schemas import (
    ActionRequest,
    CancelPublicationRequest,
    CreateTaskRequest,
    EmptyRequest,
    ExecuteRequest,
    PublicationRequest,
    ReconcileRequest,
    ResumeRequest,
    ReviewRequest,
    SnapshotRequest,
)

NOW = 1_800_000_000_000
HASH = "a" * 64
REQUESTS = (
    (ActionRequest, {"action_id": "r6-action"}),
    (SnapshotRequest, {"action_id": "r6-import",
                       "snapshot": demo_snapshots(NOW)["normal"].model_dump(mode="json")}),
    (CreateTaskRequest, {"action_id": "r6-create", "snapshot_id": "source-object",
                         "scenario": Scenario(kind="baseline").model_dump(mode="json")}),
    (ResumeRequest, {"action_id": "r6-resume", "answers": [
        {"field": "materials[0].initial_stock", "value": 0},
        {"field": "orders[0].due_min", "value": 8}], "note": "Explicit synthetic input"}),
    (ReviewRequest, {"action_id": "r6-review", "recommendation_hash": HASH,
                     "decision": "approved", "note": "Synthetic plan independently reviewed"}),
    (PublicationRequest, {"action_id": "r6-publication", "recommendation_hash": HASH,
                          "confirmed": True}),
    (ExecuteRequest, {"action_id": "r6-execute", "confirmed": True}),
    (CancelPublicationRequest, {"action_id": "r6-cancel", "note": "Cancel synthetic publication"}),
    (ReconcileRequest, {"action_id": "r6-reconcile", "call_id": "call-r6", "no_result": True,
                        "tokens": None, "note": "No result recovered; usage remains unknown"}),
)


@pytest.mark.parametrize("schema,body", REQUESTS)
def test_valid_requests_round_trip_json(schema, body):
    request = schema.model_validate(copy.deepcopy(body))
    assert request.model_dump(mode="json") == body
    assert schema.model_validate_json(json.dumps(body)).model_dump(mode="json") == body


@pytest.mark.parametrize("schema,body", REQUESTS)
@pytest.mark.parametrize("field", ["owner", "role", "token", "url", "path", "fault", "capability"])
def test_no_server_identity_files_or_receiver_controls(schema, body, field):
    with pytest.raises(ValidationError):
        schema.model_validate({**body, field: "untrusted"})


@pytest.mark.parametrize("schema,body", REQUESTS)
@pytest.mark.parametrize("value", [None, True, 12, "", "a" * 81, "../file", "a/b", " a", "a\n"])
def test_identifiers_are_strict_bounded_strings(schema, body, value):
    with pytest.raises(ValidationError):
        schema.model_validate({**body, "action_id": value})


@pytest.mark.parametrize("schema,body", [REQUESTS[4], REQUESTS[5]])
@pytest.mark.parametrize("value", [None, True, 123, "A" * 64, "a" * 63, "a" * 65, "g" * 64])
def test_review_and_publication_bind_exact_sha256(schema, body, value):
    with pytest.raises(ValidationError):
        schema.model_validate({**body, "recommendation_hash": value})


@pytest.mark.parametrize("schema,body,field", [
    (*REQUESTS[5], "confirmed"), (*REQUESTS[6], "confirmed"), (*REQUESTS[8], "no_result"),
])
@pytest.mark.parametrize("value", [False, 0, 1, 1.0, "true", "True", "1", None, [], {}])
def test_true_attestations_never_coerce(schema, body, field, value):
    with pytest.raises(ValidationError):
        schema.model_validate({**body, field: value})


@pytest.mark.parametrize("value", [True, False, -1, 100001, 1.0, "1", float("nan"), float("inf")])
def test_reconcile_usage_is_strict_bounded_integer(value):
    with pytest.raises(ValidationError):
        ReconcileRequest.model_validate({**REQUESTS[8][1], "tokens": value})


@pytest.mark.parametrize("value", [None, 0, 1, 100000])
def test_unknown_usage_is_not_zero(value):
    request = ReconcileRequest.model_validate({**REQUESTS[8][1], "tokens": value})
    assert type(request.tokens) is type(value)
    assert request.tokens == value


@pytest.mark.parametrize("schema,body", [REQUESTS[3], REQUESTS[4], REQUESTS[7], REQUESTS[8]])
@pytest.mark.parametrize("note", ["", " ", "\t\n", "　", "n" * 2001, None, 42, True])
def test_notes_are_nonblank_bounded_text(schema, body, note):
    with pytest.raises(ValidationError):
        schema.model_validate({**body, "note": note})


@pytest.mark.parametrize("answers", [
    [], {}, {"materials[0].initial_stock": 3},
    [{"field": "materials[0].initial_stock", "value": 0}] * 2,
    [{"field": "observed_at_ms", "value": 0}],
    [{"field": "revision", "value": 2}],
    [{"field": "materials[20].initial_stock", "value": 1}],
    [{"field": "materials[-1].initial_stock", "value": 1}],
    [{"field": "orders[00].due_min", "value": 1}],
    [{"field": "orders[0].initial_stock", "value": 1}],
    [{"field": "materials[0].due_min", "value": 1}],
    [{"field": "orders[0].due_min", "value": 1441}],
    [{"field": "materials[0].initial_stock", "value": MAX_STOCK + 1}],
    [{"field": "orders[0].due_min", "value": -1}],
    [{"field": "orders[0].due_min", "value": True}],
    [{"field": "orders[0].due_min", "value": 1.0}],
    [{"field": "orders[0].due_min", "value": "1"}],
    [{"field": "orders[0].due_min", "value": None}],
    [{"field": "orders[0].due_min", "value": 1, "owner": "other"}],
    [{"field": "orders[0].due_min", "value": float("nan")}],
])
def test_resume_accepts_only_unique_field_value_answers(answers):
    with pytest.raises(ValidationError):
        ResumeRequest.model_validate({**REQUESTS[3][1], "answers": answers})


def test_legacy_fields_dict_cannot_replace_resume_answers():
    with pytest.raises(ValidationError):
        ResumeRequest(action_id="resume", fields={"orders[0].due_min": 8}, note="measured")


@pytest.mark.parametrize("field,value", [
    ("materials[19].initial_stock", 0), ("materials[0].initial_stock", MAX_STOCK),
    ("orders[19].due_min", 1440), ("orders[0].due_min", 0),
])
def test_answer_bounds_inclusive_but_missing_source_gate_stays_in_service(field, value):
    request = ResumeRequest(action_id="resume", note="measured", answers=[
        {"field": field, "value": value}])
    assert request.model_dump()["answers"] == [{"field": field, "value": value}]


@pytest.mark.parametrize("decision", ["executed", "pending_review", "APPROVED", True, None])
def test_review_does_not_accept_execution_states(decision):
    with pytest.raises(ValidationError):
        ReviewRequest.model_validate({**REQUESTS[4][1], "decision": decision})


@pytest.mark.parametrize("path,value", [
    (("revision",), True), (("horizon_min",), 12.0), (("observed_at_ms",), "1800000000000"),
    (("owner",), "other"), (("machines", 0, "path"), "/not-an-input"),
    (("lots", 0, "quantity"), "3"), (("lots", 0, "quantity"), True),
    (("lots", 0, "operations", 0, "alternatives", 0, "qualified"), 1),
    (("materials", 0, "initial_stock"), 0.0), (("orders", 0, "due_min"), "8"),
    (("objective", "makespan"), -1),
])
def test_snapshot_requests_retain_nested_strict_domain_validation(path, value):
    body = copy.deepcopy(REQUESTS[1][1])
    target = body["snapshot"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        SnapshotRequest.model_validate(body)


@pytest.mark.parametrize("scenario", [
    {}, {"kind": "arbitrary"}, {"kind": "baseline", "url": "http://127.0.0.1"},
    {"kind": "baseline", "start_min": 0},
    {"kind": "machine_outage", "machine_id": "M1", "start_min": 2.0, "end_min": 3},
    {"kind": "machine_outage", "machine_id": "M1", "start_min": 2, "end_min": True},
    {"kind": "supply_delay", "supply_id": "raw_early", "at_min": "6"},
])
def test_task_scenario_is_a_validated_domain_object(scenario):
    with pytest.raises(ValidationError):
        CreateTaskRequest.model_validate({**REQUESTS[2][1], "scenario": scenario})


def test_source_and_scenario_remain_model_instances_at_boundary():
    source = SnapshotRequest.model_validate(REQUESTS[1][1])
    task = CreateTaskRequest.model_validate(REQUESTS[2][1])
    assert isinstance(source.snapshot, Snapshot)
    assert isinstance(task.scenario, Scenario)


def test_explicit_missing_values_not_imputed():
    request = SnapshotRequest(action_id="missing", snapshot=demo_snapshots(NOW)["missing"])
    assert request.snapshot.materials[0].initial_stock is None
    assert request.snapshot.orders[0].due_min is None


def test_empty_run_requires_empty_object_and_notes_stay_untrusted_text():
    assert EmptyRequest.model_validate({}).model_dump() == {}
    with pytest.raises(ValidationError):
        EmptyRequest.model_validate({"action_id": "not-permitted"})
    note = '<script>alert("untrusted text")</script>'
    assert ReviewRequest.model_validate({**REQUESTS[4][1], "note": note}).note == note
