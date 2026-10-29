"""HTTP acceptance uses real SQLite and scheduling, with TEST-only/mocked model boundaries."""

import asyncio
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from scheduling_agent import api
from scheduling_agent.domain import Scenario, Snapshot
from scheduling_agent.fixtures import demo_snapshots
from scheduling_agent.provider import ProviderError, SyntheticProvider

NOW = 1_800_000_000_000
BASE = "http://127.0.0.1:8006"
TOKEN = "".join(("api-test-", "owner-token-", "a" * 24))
OTHER_TOKEN = "".join(("api-test-", "other-token-", "b" * 24))


class NoInferenceProvider:
    fingerprint = "TEST-boundary-no-inference"
    is_test_provider = True
    metadata = {"provider": "TEST-no-inference"}

    def __init__(self):
        self.closes = 0

    def decide(self, context):
        raise AssertionError("This boundary test must not invoke inference")

    def session(self):
        return nullcontext(self)

    def close(self):
        self.closes += 1


@pytest.fixture
def boundary(tmp_path):
    application = api.create_app(tmp_path.resolve() / "data", NoInferenceProvider(), clock=lambda: NOW)
    snapshot = demo_snapshots(NOW)["normal"]
    store = application.state.store
    owner = store.create_user("api-owner", TOKEN, "admin", [snapshot.plant_id])
    other = store.create_user("api-other", OTHER_TOKEN, "admin", [snapshot.plant_id])
    with TestClient(application, base_url=BASE) as client:
        yield application, client, owner, other, snapshot


def auth(token=TOKEN, **extra):
    return {"Authorization": "Bearer " + token, **extra}


def post(client, path, body, token=TOKEN):
    return client.post(path, headers=auth(token, Origin=BASE), json=body)


def import_snapshot(client, snapshot, action_id="import-one"):
    result = post(client, "/api/snapshots", {
        "action_id": action_id, "snapshot": snapshot.model_dump(mode="json")})
    assert result.status_code == 200, result.text
    return result.json()


def create_task(client, snapshot, action_id="create-task"):
    source = import_snapshot(client, snapshot)
    result = post(client, "/api/tasks", {
        "action_id": action_id, "snapshot_id": source["object_id"], "scenario": {"kind": "baseline"}})
    assert result.status_code == 200, result.text
    return result.json()


def private_publication(owner):
    """A read/projection probe, not receiver-authorized state."""
    return {
        "kind": "publication", "object_id": "pub-one", "owner": owner.user_id,
        "plant_id": "synthetic-plant", "status": "requested", "version": 1,
        "content_hash": "a" * 64,
        "signing_key": "never-public-root-secret",
        "payload": {
            "recommendation_id": "rec-one", "recommendation_hash": "b" * 64,
            "plan_hash": "c" * 64, "source_binding": {"object_id": "source-one"},
            "delivery_status": "not_sent", "receiver_status": "none", "execution_status": "none",
            "receiver_sequence": 0, "accepted_operation_ids": [], "execution_result": None,
            "synthetic": True,
            "signed_request": {"signature": "never-public-request-signature"},
            "receiver_receipt": {"signature": "never-public-receipt-signature"},
            "capability": "never-public-capability", "signing_key": "never-public-key",
            "pending_action": {"adapter_internal": "never-public-internal"},
        },
    }


def assert_public_publication(value):
    assert set(value) == {"kind", "object_id", "owner", "plant_id", "status",
                          "version", "content_hash", "payload"}
    assert set(value["payload"]) <= {
        "delivery_status", "receiver_status", "execution_status", "receiver_sequence",
        "accepted_operation_ids", "execution_result", "source_binding", "plan_hash",
        "recommendation_id", "recommendation_hash", "synthetic",
    }
    assert "never-public" not in str(value)


def submitted(boundary):
    application, client, _, _, snapshot = boundary
    application.state.engine.provider = SyntheticProvider()
    task = create_task(client, snapshot)
    response = post(client, f"/api/tasks/{task['object_id']}/run", {})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted", response.text
    recommendations = client.get("/api/recommendations", headers=auth()).json()["items"]
    assert len(recommendations) == 1
    return recommendations[0]


def requested(boundary):
    _, client, *_ = boundary
    recommendation = submitted(boundary)
    route = "/api/recommendations/" + recommendation["object_id"]
    reviewed = post(client, route + "/review", {
        "action_id": "review-one", "recommendation_hash": recommendation["content_hash"],
        "decision": "approved", "note": "Independent review of synthetic schedule"})
    assert reviewed.status_code == 200, reviewed.text
    publication = post(client, route + "/request-publication", {
        "action_id": "publication-one", "recommendation_hash": reviewed.json()["content_hash"],
        "confirmed": True})
    assert publication.status_code == 200, publication.text
    return publication.json()


def test_health_public_no_inference_no_seeding_and_shutdown_close(tmp_path):
    provider = NoInferenceProvider()
    application = api.create_app(tmp_path.resolve() / "empty", provider)
    assert provider.closes == 0
    with TestClient(application, base_url=BASE) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "provider": provider.fingerprint,
                                   "synthetic_only": True}
        assert provider.closes == 0
        with application.state.store.connect() as connection:
            for table in ("users", "objects", "calls", "audit"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert client.get("/api/me").status_code == 401
    assert provider.closes == 1
    application.state.close_provider()
    assert provider.closes == 1
    assert application.openapi_url is application.docs_url is application.redoc_url is None


def test_static_assets_and_errors_have_local_security_headers(boundary):
    _, client, *_ = boundary
    for path in ("/", "/static/app.js", "/static/style.css", "/health", "/api/me"):
        response = client.get(path)
        assert response.status_code == (401 if path == "/api/me" else 200), (path, response.text)
        for name, value in api.SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", [
    "/docs", "/redoc", "/openapi.json", "/static/other.js", "/api/users", "/api/models",
    "/api/path", "/api/receiver/sign", "/api/receiver/callback", "/api/receiver/capability",
])
def test_no_docs_signing_callbacks_paths_or_user_administration(boundary, path):
    _, client, *_ = boundary
    assert client.get(path, headers=auth()).status_code == 404
    if path.startswith("/api/"):
        assert post(client, path, {}).status_code == 404


@pytest.mark.parametrize("host", [
    "localhost:8006", "evil.example:8006", "0.0.0.0:8006", "192.168.1.5:8006",
    "127.0.0.1.evil:8006", "2130706433:8006", "127.1:8006", "127.0.0.1:0", "127.0.0.1:65536",
    "127.0.0.1:", "127.0.0.1:8006@evil", "[::1]junk:8006", "127.0.0.1:+8006",
    "[::1%25lo0]:8006", "[::ffff:127.0.0.1]:8006",
])
def test_numeric_loopback_host_required_without_forwarded_override(boundary, host):
    _, client, *_ = boundary
    result = client.get("/health", headers={"Host": host, "X-Forwarded-Host": "127.0.0.1:8006"})
    assert result.status_code == 400
    assert result.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host", ["127.0.0.1:8006", "127.0.0.2", "[::1]:8006", "[0:0:0:0:0:0:0:1]"])
def test_numeric_loopback_hosts_ignore_proxy_headers(boundary, host):
    _, client, *_ = boundary
    assert client.get("/health", headers={"Host": host, "X-Forwarded-Host": "evil.example",
                                        "X-Forwarded-Proto": "https"}).status_code == 200


def test_duplicate_sensitive_headers_rejected(boundary):
    _, client, *_ = boundary
    assert client.get("/health", headers=[("Host", "127.0.0.1"), ("Host", "127.0.0.1")]).status_code == 400
    assert client.get("/api/me", headers=[("Authorization", "Bearer " + TOKEN)] * 2).status_code == 401
    for key, value in (("Origin", BASE), ("Content-Length", "2"),
                       ("Content-Type", "application/json"), ("Sec-Fetch-Site", "same-origin")):
        headers = [("Authorization", "Bearer " + TOKEN), (key, value), (key, value)]
        if key != "Content-Type":
            headers.append(("Content-Type", "application/json"))
        assert client.post("/api/tasks/t/run", headers=headers, content=b"{}").status_code == 400


@pytest.mark.parametrize("extra", [
    {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": ""},
    {"Origin": "http://localhost:8006"}, {"Origin": "http://127.0.0.1:8005"},
    {"Origin": "https://127.0.0.1:8006"}, {"Origin": BASE + "/"}, {"Origin": BASE + "?x=1"},
    {"Origin": BASE + "?"}, {"Origin": BASE + "#"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"}, {"Referer": "http://evil.example/form"},
])
def test_writes_reject_cross_origin_even_with_valid_bearer(boundary, extra):
    _, client, *_ = boundary
    assert client.post("/api/tasks/no-task/run", headers=auth(**extra), json={}).status_code == 403


@pytest.mark.parametrize("extra", [
    {}, {"Origin": BASE}, {"Referer": BASE + "/workbench"},
    {"Sec-Fetch-Site": "same-origin", "Origin": BASE}, {"Sec-Fetch-Site": "none"},
])
def test_same_origin_and_native_bearer_clients_can_write(boundary, extra):
    _, client, _, _, snapshot = boundary
    response = client.post("/api/snapshots", headers=auth(**extra),
                           json={"action_id": "import", "snapshot": snapshot.model_dump(mode="json")})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("authorization", [
    "", "Basic abc", "Bearer nope", "Bearer  " + TOKEN, "Bearer " + TOKEN + " ", "Bearer " + "a" * 201,
])
def test_bearer_required_never_echoed(boundary, authorization):
    _, client, *_ = boundary
    response = client.get("/api/me", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text


def test_identity_owner_plant_and_active_are_server_authorized(boundary):
    application, client, owner, _, snapshot = boundary
    source = import_snapshot(client, snapshot)
    route = "/api/snapshots/" + source["object_id"]
    assert source["owner"] == owner.user_id
    assert client.get("/api/me", headers=auth()).json() == {
        "user_id": owner.user_id, "role": "admin", "plants": list(owner.plants), "acl_version": 1}
    assert client.get(route, headers=auth()).json() == source
    assert client.get(route, headers=auth(OTHER_TOKEN)).status_code == 404
    assert client.get(route + "/scenarios", headers=auth(OTHER_TOKEN)).status_code == 404
    assert client.get("/api/snapshots", headers=auth(OTHER_TOKEN)).json() == {"items": []}
    assert client.get("/api/audit", headers=auth(OTHER_TOKEN)).json() == {"items": []}
    assert client.get("/api/audit", headers=auth()).json()["items"]
    for query in ("token=" + TOKEN, "owner=other", "role=admin", "path=/not-read"):
        response = client.get("/api/me?" + query, headers=auth())
        assert response.status_code == 400
        assert TOKEN not in response.text
    application.state.store.set_user(owner.user_id, plants=[])
    assert client.get("/api/snapshots", headers=auth()).json() == {"items": []}
    assert client.get(route, headers=auth()).status_code in {403, 404}
    replay = post(client, "/api/snapshots", {
        "action_id": "import-one", "snapshot": snapshot.model_dump(mode="json")})
    assert replay.status_code == 403
    application.state.store.set_user(owner.user_id, active=False)
    assert client.get("/api/me", headers=auth()).status_code == 401


def test_source_import_requires_reviewer_and_permitted_plant(boundary):
    application, client, owner, _, snapshot = boundary
    body = {"action_id": "import-role", "snapshot": snapshot.model_dump(mode="json")}
    application.state.store.set_user(owner.user_id, role="operator")
    assert post(client, "/api/snapshots", body).status_code == 403
    application.state.store.set_user(owner.user_id, role="reviewer")
    assert post(client, "/api/snapshots", body).status_code == 200
    body["snapshot"]["plant_id"] = "foreign-plant"
    body["action_id"] = "foreign-import"
    assert post(client, "/api/snapshots", body).status_code == 403


def test_acl_change_after_authentication_rechecked(boundary, monkeypatch):
    application, client, owner, *_ = boundary
    original = application.state.store.authenticate

    def changed(token):
        principal = original(token)
        application.state.store.set_user(owner.user_id, plants=[])
        return principal

    monkeypatch.setattr(application.state.store, "authenticate", changed)
    assert client.get("/api/me", headers=auth()).status_code == 403


@pytest.mark.parametrize("body", [
    b"[]", b"null", b'"text"', b"", b'{"x":1,"x":2}', b'{"x":{"same":1,"same":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e309}',
    b'{"x":9223372036854775808}', b'{"x":"\\ud800"}', b'{"x":"\xff"}', b"\xef\xbb\xbf{}",
    b'{"x":' + b"[" * 40 + b"0" + b"]" * 40 + b"}",
])
def test_json_is_unique_finite_bounded_utf8_object(boundary, body):
    _, client, *_ = boundary
    response = client.post("/api/tasks/t/run", content=body,
                           headers=auth(**{"Content-Type": "application/json"}))
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_json"


@pytest.mark.parametrize("content_type", [
    "text/plain", "application/json; charset=utf-16", "application/json; arbitrary=1",
    "application/x-www-form-urlencoded", "application/json; charset=utf-8; charset=utf-8",
])
def test_only_uncompressed_utf8_json_accepted(boundary, content_type):
    _, client, *_ = boundary
    assert client.post("/api/tasks/t/run", content=b"{}", headers=auth(**{
        "Content-Type": content_type})).status_code == 415
    assert client.post("/api/tasks/t/run", content=b"{}", headers=auth(**{
        "Content-Type": "application/json", "Content-Encoding": "gzip"})).status_code == 415


def test_schema_strictness_and_idempotent_revisioned_source_import(boundary):
    _, client, _, _, snapshot = boundary
    source = import_snapshot(client, snapshot)
    assert import_snapshot(client, snapshot) == source
    base = {"action_id": "import-one", "snapshot": snapshot.model_dump(mode="json")}
    changed = copy.deepcopy(base)
    changed["snapshot"]["revision"] += 1
    assert post(client, "/api/snapshots", changed).status_code == 409
    changed["action_id"] = "new-revision"
    updated = post(client, "/api/snapshots", changed)
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == source["version"] + 1
    assert updated.json()["object_id"] == source["object_id"]
    assert post(client, "/api/snapshots", {**base, "action_id": "old-revision"}).status_code == 409
    for field, value in (("revision", True), ("horizon_min", 12.0), ("observed_at_ms", str(NOW))):
        body = copy.deepcopy(base)
        body["snapshot"][field] = value
        assert post(client, "/api/snapshots", body).status_code == 422
    for field in ("owner", "role", "token", "path", "url"):
        response = post(client, "/api/snapshots", {**base, field: TOKEN})
        assert response.status_code == 422
        assert TOKEN not in response.text


def test_declared_actual_chunked_body_limits_and_framing(boundary):
    _, client, *_ = boundary
    route = "/api/tasks/t/run"
    headers = auth(**{"Content-Type": "application/json"})
    assert client.post(route, headers=headers, content=b" " * (api.MAX_BODY + 1)).status_code == 413
    assert client.post(route, content=b"{}", headers={
        **headers, "Content-Length": str(api.MAX_BODY + 1)}).status_code == 413
    assert client.post(route, headers=headers, content=iter([b" " * api.MAX_BODY, b" "])).status_code == 413
    for length in ("-1", "+2", "2,2", "", "1", "3"):
        assert client.post(route, content=b"{}", headers={
            **headers, "Content-Length": length}).status_code == 400
    assert client.post(route, content=b"{}", headers={
        **headers, "Content-Length": "2", "Transfer-Encoding": "chunked"}).status_code == 400
    assert client.request("GET", "/health", content=b"{}").status_code == 400
    assert client.request("TRACE", "/health").status_code == 405


def test_body_deadline_includes_wait_for_stream(boundary, monkeypatch):
    application, _, *_ = boundary
    monkeypatch.setattr(api, "MAX_BODY_SECONDS", 0.01)

    class SlowReceive:
        async def __call__(self, scope, receive, send):
            async def slow():
                await asyncio.sleep(0.05)
                return await receive()
            await application(scope, slow if scope["type"] == "http" else receive, send)

    # Avoid a second lifespan; this test is only a streaming middleware probe.
    client = TestClient(SlowReceive(), base_url=BASE)
    response = post(client, "/api/tasks/t/run", {})
    assert response.status_code == 408
    assert response.json()["error"]["code"] == "body_timeout"
    client.close()


def test_no_websocket_receiver_or_unbounded_transport(boundary):
    _, client, *_ = boundary
    with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect("/api/receiver"):
        pytest.fail("WebSocket transport must be closed")
    assert error.value.code == 1008


def test_authorized_scenario_metadata_contains_only_domain_scenarios(boundary):
    _, client, _, _, snapshot = boundary
    source = import_snapshot(client, snapshot)
    response = client.get(f"/api/snapshots/{source['object_id']}/scenarios", headers=auth())
    assert response.status_code == 200
    assert set(response.json()["items"]) == {"baseline", "insert_order", "machine_outage", "supply_delay"}
    for value in response.json()["items"].values():
        Scenario.model_validate(value)
    missing_supply = import_snapshot(client, demo_snapshots(NOW)["infeasible"], "import-infeasible")
    response = client.get(f"/api/snapshots/{missing_supply['object_id']}/scenarios", headers=auth())
    assert set(response.json()["items"]) == {"baseline"}


def test_task_create_cancel_idempotency_and_crossowner_denial(boundary):
    _, client, _, _, snapshot = boundary
    task = create_task(client, snapshot)
    route = "/api/tasks/" + task["object_id"]
    assert task["status"] == "created"
    assert task["payload"]["calls"] == 0
    assert create_task(client, snapshot) == task
    assert client.get(route, headers=auth()).json() == task
    assert client.get("/api/tasks", headers=auth()).json()["items"][0]["object_id"] == task["object_id"]
    assert client.get(route, headers=auth(OTHER_TOKEN)).status_code == 404
    assert post(client, route + "/cancel", {"action_id": "wrong"}, OTHER_TOKEN).status_code == 404
    assert post(client, route + "/run", {"action_id": "not-empty"}).status_code == 422
    cancelled = post(client, route + "/cancel", {"action_id": "cancel-one"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert post(client, route + "/cancel", {"action_id": "cancel-one"}).json() == cancelled.json()
    assert client.get("/api/recommendations", headers=auth()).json() == {"items": []}
    assert client.get("/api/publications", headers=auth()).json() == {"items": []}


@pytest.mark.parametrize("kind,plural", [
    ("task", "tasks"), ("recommendation", "recommendations"), ("publication", "publications"),
])
def test_all_object_gets_and_lists_reauthorize_plant_and_owner(boundary, kind, plural):
    application, client, owner, _, snapshot = boundary
    if kind == "task":
        obj = create_task(client, snapshot)
    else:
        # Read-boundary fixtures are deliberately not eligible for review or publication.
        with application.state.store.transaction() as conn:
            obj = application.state.store.create_object(
                conn, kind=kind, object_id="read-boundary-" + kind, principal=owner,
                plant_id=snapshot.plant_id, status="created", payload={"synthetic": True})
    route = f"/api/{plural}/{obj['object_id']}"
    assert client.get(route, headers=auth()).status_code == 200
    assert client.get(route, headers=auth(OTHER_TOKEN)).status_code == 404
    assert client.get(f"/api/{plural}", headers=auth(OTHER_TOKEN)).json() == {"items": []}
    application.state.store.set_user(owner.user_id, plants=[])
    assert client.get(route, headers=auth()).status_code in {403, 404}
    assert client.get(f"/api/{plural}", headers=auth()).json() == {"items": []}


def test_task_source_must_be_owned_and_scenario_creation_idempotency_bound(boundary):
    _, client, _, _, snapshot = boundary
    source = import_snapshot(client, snapshot)
    body = {"snapshot_id": source["object_id"], "scenario": {"kind": "baseline"}, "action_id": "create"}
    assert post(client, "/api/tasks", body, OTHER_TOKEN).status_code == 404
    assert post(client, "/api/tasks", body).status_code == 200
    body["scenario"] = {"kind": "supply_delay", "supply_id": "raw_early", "at_min": 6}
    assert post(client, "/api/tasks", body).status_code == 409


def test_running_route_does_not_block_cancellation(boundary, monkeypatch):
    application, client, _, _, snapshot = boundary
    task = create_task(client, snapshot)
    route = "/api/tasks/" + task["object_id"]
    entered, release = threading.Event(), threading.Event()

    def blocking_run(principal, task_id):
        entered.set()
        assert release.wait(5)
        return application.state.engine.get(principal, task_id)

    monkeypatch.setattr(application.state.engine, "run", blocking_run)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(post, client, route + "/run", {})
        try:
            assert entered.wait(5)
            assert not future.done()
            cancelled = post(client, route + "/cancel", {"action_id": "cancel-running"})
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
        finally:
            release.set()
        assert future.result(timeout=5).json()["status"] == "cancelled"


@pytest.mark.parametrize("path,method,body,expected", [
    ("/api/tasks/task-one/resume", "resume", {"action_id": "a1", "answers": [
        {"field": "orders[0].due_min", "value": 8}], "note": "Measured"},
     {"action_id": "a1", "answers": [{"field": "orders[0].due_min", "value": 8}], "note": "Measured"}),
    ("/api/tasks/task-one/reconcile", "reconcile", {"action_id": "a1", "call_id": "call-one",
        "no_result": True, "note": "Confirmed no result"}, {"action_id": "a1", "call_id": "call-one",
        "no_result": True, "tokens": None, "note": "Confirmed no result"}),
    ("/api/recommendations/rec-one/review", "review", {"action_id": "a1", "recommendation_hash": "a" * 64,
        "decision": "approved", "note": "Reviewed"}, {"action_id": "a1", "recommendation_hash": "a" * 64,
        "decision": "approved", "note": "Reviewed"}),
    ("/api/recommendations/rec-one/request-publication", "request_publication", {"action_id": "a1",
        "recommendation_hash": "a" * 64, "confirmed": True}, {"action_id": "a1",
        "recommendation_hash": "a" * 64, "confirmed": True}),
    ("/api/publications/pub-one/deliver-local", "deliver_local", {"action_id": "a1"}, {"action_id": "a1"}),
    ("/api/publications/pub-one/advance-receiver", "advance_receiver", {"action_id": "a1"}, {"action_id": "a1"}),
    ("/api/publications/pub-one/readback", "readback", {"action_id": "a1"}, {"action_id": "a1"}),
    ("/api/publications/pub-one/cancel", "cancel_publication", {"action_id": "a1", "note": "Cancel"},
     {"action_id": "a1", "note": "Cancel"}),
    ("/api/publications/pub-one/execute-synthetic", "execute_synthetic", {"action_id": "a1", "confirmed": True},
     {"action_id": "a1", "confirmed": True}),
])
def test_routes_delegate_exact_service_contracts(boundary, monkeypatch, path, method, body, expected):
    application, client, owner, *_ = boundary
    target = application.state.engine if path.startswith("/api/tasks/") else application.state.actions
    is_publication = path.startswith("/api/publications/") or method == "request_publication"
    result = private_publication(owner) if is_publication else {"delegated": True}
    original_result = copy.deepcopy(result)
    delegated = Mock(return_value=result)
    monkeypatch.setattr(target, method, delegated)
    response = post(client, path, body)
    assert response.status_code == 200, response.text
    delegated.assert_called_once_with(owner, path.split("/")[3], **expected)
    if is_publication:
        assert_public_publication(response.json())
        assert response.json()["content_hash"] == result["content_hash"]
        assert result == original_result  # Projection must not mutate durable service state.


def test_publication_list_and_detail_never_return_raw_signed_envelopes(boundary):
    application, client, owner, _, snapshot = boundary
    private = private_publication(owner)
    with application.state.store.transaction() as conn:
        stored = application.state.store.create_object(
            conn, kind="publication", object_id="pub-projection", principal=owner,
            plant_id=snapshot.plant_id, status="requested", payload=private["payload"])
    detailed = client.get("/api/publications/pub-projection", headers=auth())
    assert detailed.status_code == 200, detailed.text
    assert_public_publication(detailed.json())
    assert detailed.json()["content_hash"] == stored["content_hash"]
    listed = client.get("/api/publications", headers=auth())
    assert listed.status_code == 200
    assert listed.json()["items"] == [detailed.json()]
    persisted = application.state.store.get_object("publication", "pub-projection", owner)
    assert persisted == stored
    assert "signed_request" in persisted["payload"]
    assert "receiver_receipt" in persisted["payload"]


def test_rejected_review_passes_unchanged_and_old_aliases_are_forbidden(boundary, monkeypatch):
    application, client, owner, *_ = boundary
    delegated = Mock(return_value={"status": "rejected"})
    monkeypatch.setattr(application.state.actions, "review", delegated)
    body = {"action_id": "reject", "recommendation_hash": "a" * 64,
            "decision": "rejected", "note": "Decline this synthetic candidate"}
    response = post(client, "/api/recommendations/rec-one/review", body)
    assert response.status_code == 200
    delegated.assert_called_once_with(owner, "rec-one", **body)
    for alias in ("approve", "reject"):
        assert post(client, "/api/recommendations/rec-one/review", {
            **body, "decision": alias}).status_code == 422
    delegated.assert_called_once()


def test_source_import_passes_validated_snapshot_and_action_id(boundary, monkeypatch):
    application, client, owner, _, snapshot = boundary
    delegated = Mock(return_value={"delegated": True})
    monkeypatch.setattr(application.state.service, "import_snapshot", delegated)
    response = post(client, "/api/snapshots", {
        "action_id": "source-one", "snapshot": snapshot.model_dump(mode="json")})
    assert response.status_code == 200
    delegated.assert_called_once_with(owner, snapshot, action_id="source-one")
    assert isinstance(delegated.call_args.args[1], Snapshot)


@pytest.mark.parametrize("path,body", [
    ("/api/publications/p/deliver-local", {"action_id": "a", "fault": "after_receive"}),
    ("/api/publications/p/deliver-local", {"action_id": "a", "url": "http://127.0.0.1"}),
    ("/api/publications/p/deliver-local", {"action_id": "a", "capability": "forged"}),
    ("/api/publications/p/advance-receiver", {"action_id": "a", "operation_ids": ["chosen"]}),
    ("/api/publications/p/readback", {"action_id": "a", "receiver_status": "accepted"}),
    ("/api/publications/p/execute-synthetic", {"action_id": "a", "confirmed": 1}),
    ("/api/publications/p/cancel", {"action_id": "a", "note": " "}),
    ("/api/tasks/t/reconcile", {"action_id": "a", "call_id": "c", "no_result": 1, "note": "x"}),
    ("/api/tasks/t/reconcile", {"action_id": "a", "call_id": "c", "no_result": True, "tokens": True, "note": "x"}),
    ("/api/tasks/t/resume", {"action_id": "a", "answers": [{"field": "orders[0].due_min", "value": 8.0}], "note": "x"}),
    ("/api/tasks/t/resume", {"action_id": "a", "fields": {"orders[0].due_min": 8}, "note": "x"}),
])
def test_no_fault_receiver_authority_paths_or_coerced_numbers(boundary, path, body):
    _, client, *_ = boundary
    assert post(client, path, body).status_code == 422


def test_missing_inputs_resume_preserves_calls_and_observation_then_submits(boundary):
    application, client, _, _, _ = boundary
    application.state.engine.provider = SyntheticProvider()
    snapshot = demo_snapshots(NOW)["missing"]
    task = create_task(client, snapshot)
    route = "/api/tasks/" + task["object_id"]
    waiting = post(client, route + "/run", {})
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["status"] == "awaiting_input", waiting.text
    payload = waiting.json()["payload"]
    assert set(payload["awaiting_fields"]) == {"materials[0].initial_stock", "orders[0].due_min"}
    assert payload["calls"] > 0
    invalid = {"action_id": "missing-answer", "answers": [
        {"field": "orders[0].due_min", "value": 8}], "note": "Only one measured field"}
    assert post(client, route + "/resume", invalid).status_code == 422
    body = {"action_id": "resume-measured", "answers": [
        {"field": "materials[0].initial_stock", "value": 0}, {"field": "orders[0].due_min", "value": 8}],
        "note": "Explicit original synthetic inputs"}
    resumed = post(client, route + "/resume", body)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["payload"]["calls"] == payload["calls"]
    assert post(client, route + "/resume", body).json() == resumed.json()
    source = client.get("/api/snapshots/" + task["payload"]["snapshot_id"], headers=auth()).json()
    assert source["payload"]["observed_at_ms"] == snapshot.observed_at_ms
    assert source["payload"]["revision"] == snapshot.revision + 1
    assert client.get("/api/recommendations", headers=auth()).json() == {"items": []}
    result = post(client, route + "/run", {})
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "submitted", result.text


def test_infeasible_fixture_is_no_schedule_not_a_fabricated_recommendation(boundary):
    application, client, *_ = boundary
    application.state.engine.provider = SyntheticProvider()
    task = create_task(client, demo_snapshots(NOW)["infeasible"])
    response = post(client, f"/api/tasks/{task['object_id']}/run", {})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "no_schedule", response.text
    assert client.get("/api/recommendations", headers=auth()).json() == {"items": []}
    assert client.get("/api/publications", headers=auth()).json() == {"items": []}


def test_restarted_app_replays_import_and_task_creation_without_inference(boundary):
    application, client, _, _, snapshot = boundary
    task = create_task(client, snapshot)
    restarted_provider = NoInferenceProvider()
    restarted = api.create_app(application.state.store.root, restarted_provider, clock=lambda: NOW)
    with TestClient(restarted, base_url=BASE) as restarted_client:
        assert create_task(restarted_client, snapshot) == task
        assert restarted_client.get("/api/tasks", headers=auth()).json()["items"] == [task]
        with restarted.state.store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert restarted_provider.closes == 1


def test_review_role_owner_plant_and_publication_are_separate(boundary):
    application, client, owner, _, _ = boundary
    recommendation = submitted(boundary)
    route = "/api/recommendations/" + recommendation["object_id"]
    body = {"action_id": "review-role", "recommendation_hash": recommendation["content_hash"],
            "decision": "approved", "note": "Synthetic candidate reviewed"}
    assert client.get(route, headers=auth(OTHER_TOKEN)).status_code == 404
    assert post(client, route + "/review", body, OTHER_TOKEN).status_code == 404
    application.state.store.set_user(owner.user_id, role="operator")
    assert post(client, route + "/review", body).status_code == 403
    application.state.store.set_user(owner.user_id, role="reviewer")
    approved = post(client, route + "/review", body)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert post(client, route + "/review", body).json() == approved.json()
    assert client.get("/api/publications", headers=auth()).json() == {"items": []}
    application.state.store.set_user(owner.user_id, plants=[])
    assert post(client, route + "/review", body).status_code in {403, 404}
    assert client.get("/api/recommendations", headers=auth()).json() == {"items": []}


def test_publication_partial_acceptance_and_synthetic_execution_are_distinct(boundary):
    _, client, *_ = boundary
    publication = requested(boundary)
    route = "/api/publications/" + publication["object_id"]
    assert publication["status"] == "requested"
    assert client.get(route, headers=auth(OTHER_TOKEN)).status_code == 404
    assert post(client, route + "/deliver-local", {"action_id": "foreign"}, OTHER_TOKEN).status_code == 404
    assert post(client, route + "/execute-synthetic", {"action_id": "too-early", "confirmed": True}).status_code == 409
    partial = post(client, route + "/deliver-local", {"action_id": "deliver"})
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "partial"
    assert post(client, route + "/execute-synthetic", {"action_id": "still-early", "confirmed": True}).status_code == 409
    complete = post(client, route + "/advance-receiver", {"action_id": "advance"})
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "accepted"
    executed = post(client, route + "/execute-synthetic", {"action_id": "execute", "confirmed": True})
    assert executed.status_code == 200, executed.text
    assert executed.json()["status"] == "completed"
    assert executed.json()["payload"]["execution_status"] == "completed"
    assert post(client, route + "/execute-synthetic", {
        "action_id": "execute", "confirmed": True}).json() == executed.json()


def test_lost_ack_recovery_uses_readback_not_http_fault_controls(boundary):
    application, client, owner, *_ = boundary
    publication = requested(boundary)
    # Fault injection is allowed only through the trusted local test adapter, never HTTP.
    unknown = application.state.actions.deliver_local(
        owner, publication["object_id"], action_id="lost-ack", fault="after_receive")
    assert unknown["status"] == "unknown"
    route = "/api/publications/" + publication["object_id"]
    before = client.get(route, headers=auth()).json()
    assert before["status"] == "unknown"
    # Reconstruct all services against the durable database and existing receiver key.
    restarted = api.create_app(application.state.store.root, NoInferenceProvider(), clock=lambda: NOW)
    with TestClient(restarted, base_url=BASE) as recovered_client:
        assert recovered_client.get(route, headers=auth()).json()["status"] == "unknown"
        assert post(recovered_client, route + "/deliver-local", {
            "action_id": "must-not-resend"}).status_code == 409
        readback = post(recovered_client, route + "/readback", {"action_id": "readback-authoritative"})
        assert readback.status_code == 200, readback.text
        assert readback.json()["status"] == "partial"
        assert post(recovered_client, route + "/readback", {
            "action_id": "readback-authoritative"}).json() == readback.json()
        cancelled = post(recovered_client, route + "/cancel", {
            "action_id": "cancel-publication", "note": "Cancel recovered synthetic plan"})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"


def test_unknown_model_usage_reconciliation_is_admin_attestation(boundary):
    application, client, owner, _, snapshot = boundary

    class UnknownProvider(NoInferenceProvider):
        fingerprint = "TEST-unknown-response"

        def decide(self, context):
            raise ProviderError("test_unknown", "Synthetic response unknown", tokens=None)

    application.state.engine.provider = UnknownProvider()
    task = create_task(client, snapshot)
    route = "/api/tasks/" + task["object_id"]
    blocked = post(client, route + "/run", {})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "blocked", blocked.text
    with application.state.store.connect() as conn:
        calls = conn.execute("SELECT * FROM calls WHERE task_id=?", (task["object_id"],)).fetchall()
    assert len(calls) == 1
    assert calls[0]["tokens"] is None
    body = {"action_id": "reconcile", "call_id": calls[0]["call_id"], "tokens": 17,
            "no_result": True, "note": "No usable model result; synthetic usage independently checked"}
    application.state.store.set_user(owner.user_id, role="reviewer")
    assert post(client, route + "/reconcile", body).status_code == 403
    application.state.store.set_user(owner.user_id, role="admin")
    reconciled = post(client, route + "/reconcile", body)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["payload"]["tokens"] == 17
    assert post(client, route + "/reconcile", body).json() == reconciled.json()
    assert post(client, route + "/reconcile", {**body, "tokens": 18}).status_code == 409
    assert client.get("/api/recommendations", headers=auth()).json() == {"items": []}


def test_internal_failures_sanitized_without_token_echo(boundary, monkeypatch):
    application, client, *_ = boundary
    monkeypatch.setattr(application.state.store, "audit_events",
                        Mock(side_effect=RuntimeError("sensitive-data-" + TOKEN)))
    response = client.get("/api/audit", headers=auth())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "sensitive-data" not in response.text
    assert TOKEN not in response.text
