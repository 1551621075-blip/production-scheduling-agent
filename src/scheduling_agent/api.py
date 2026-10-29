"""Numeric-loopback HTTP boundary; scheduling and receiver authority stay server-side."""

import asyncio
import ipaddress
import json
import math
import re
import threading
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .actions import ActionService
from .agent import AgentEngine
from .domain import Identifier, Scenario, Snapshot
from .errors import DomainError
from .fixtures import demo_scenarios
from .schemas import (
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
from .service import SchedulingService
from .storage import Principal, Store

MAX_BODY = 1024 * 1024
MAX_BODY_SECONDS = 10.0
MAX_JSON_DEPTH = 32
ASSETS = {"app.js": "application/javascript", "style.css": "text/css"}
PUBLICATION_FIELDS = frozenset({
    "delivery_status", "receiver_status", "execution_status", "receiver_sequence",
    "accepted_operation_ids", "execution_result", "source_binding", "plan_hash",
    "recommendation_id", "recommendation_hash", "synthetic",
})
SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
}


def _error(code: str, message: str, status: int) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


def _public_publication(obj: dict) -> dict:
    """Project state, never raw signed messages or adapter internals.

    content_hash remains the stored object's opaque version binding; it is not a
    digest of this intentionally incomplete public payload.
    """
    result = {key: obj[key] for key in (
        "kind", "object_id", "owner", "plant_id", "status", "version", "content_hash",
    )}
    result["payload"] = {key: value for key, value in obj["payload"].items()
                         if key in PUBLICATION_FIELDS}
    return result


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite number")
    return result


def _bounded_int(value: str) -> int:
    if len(value.lstrip("-")) > 19:
        raise ValueError("Integer limit")
    result = int(value)
    if not -(2**63) <= result < 2**63:
        raise ValueError("Integer limit")
    return result


def _reject_constant(_value: str):
    raise ValueError("Nonfinite number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _json_object(content: bytes) -> dict:
    text = content.decode("utf-8", errors="strict")
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON depth limit")
        elif char in "]}":
            depth -= 1
    result = json.loads(text, object_pairs_hook=_unique_object, parse_int=_bounded_int,
                        parse_float=_finite_float, parse_constant=_reject_constant)
    if type(result) is not dict:
        raise ValueError("Object required")
    # Reject escaped lone surrogates before persistence or error rendering.
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
    return result


def _authority(value: str, scheme: str) -> tuple[str, int]:
    if (scheme not in {"http", "https"} or not value
            or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
            or not re.fullmatch(r"(?:[0-9.]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", value)):
        raise ValueError("Invalid host")
    parsed = urlsplit("//" + value)
    if (not parsed.hostname or parsed.path or parsed.query or parsed.fragment
            or parsed.username is not None):
        raise ValueError("Invalid host")
    address = ipaddress.ip_address(parsed.hostname)
    if not address.is_loopback:
        raise ValueError("Numeric loopback required")
    if address.version == 6 and not value.startswith("["):
        raise ValueError("Bracketed IPv6 required")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    return address.compressed, port if port is not None else (443 if scheme == "https" else 80)


def _same_origin(value: str, scheme: str, authority, *, referer=False) -> bool:
    try:
        if (any(ord(c) <= 32 or ord(c) >= 127 for c in value) or "\\" in value
                or "#" in value or (not referer and "?" in value)):
            return False
        parsed = urlsplit(value)
        return (parsed.scheme == scheme
                and (referer or (not parsed.path and not parsed.query))
                and _authority(parsed.netloc, parsed.scheme) == authority)
    except ValueError:
        return False


class LocalBoundary:
    """Bound actual streams before routing; ignore proxy headers and never log request data."""

    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key, value) for key, value in message.get("headers", [])
                           if key.decode("latin-1").lower() not in SECURITY_HEADERS]
                message["headers"] = headers + [
                    (key.encode("ascii"), value.encode("ascii"))
                    for key, value in SECURITY_HEADERS.items()
                ]
            await send(message)

        try:
            await self._http(scope, receive, secure_send)
        except DomainError as exc:
            if not started:
                await _error(exc.code, exc.message, exc.status)(scope, receive, secure_send)
        except Exception:
            if not started:
                await _error("internal_error", "The request could not be completed", 500)(
                    scope, receive, secure_send)

    async def _http(self, scope, receive, send):
        headers = {}
        for key, value in scope["headers"]:
            headers.setdefault(key.lower(), []).append(value.decode("latin-1"))

        def single(name, default=""):
            values = headers.get(name, [])
            if len(values) > 1:
                status = 401 if name == b"authorization" else 400
                raise DomainError("invalid_header", "Invalid request headers", status)
            return values[0] if values else default

        scheme = scope.get("scheme", "http")
        try:
            authority = _authority(single(b"host"), scheme)
        except ValueError:
            raise DomainError("invalid_host", "A numeric loopback Host is required", 400) from None
        method, path = scope["method"], scope["path"]
        if method == "TRACE":
            raise DomainError("method_not_allowed", "Method not allowed", 405)
        # No route accepts query parameters, credentials or identity overrides.
        if scope.get("query_string"):
            raise DomainError("invalid_query", "Query parameters are not accepted", 400)
        public = method in {"GET", "HEAD"} and (
            path in {"/", "/health"} or path in {f"/static/{name}" for name in ASSETS})
        if not public:
            authorization = single(b"authorization")
            if (len(authorization) > 207 or not re.fullmatch(
                    r"Bearer [A-Za-z0-9._~+/\-]+=*", authorization, flags=re.IGNORECASE)):
                raise DomainError("unauthorized", "Authentication required", 401)
            principal = await run_in_threadpool(self.store.authenticate, authorization[7:])
            scope.setdefault("state", {})["principal"] = principal

        mutation = method in {"POST", "PUT", "PATCH", "DELETE"}
        if mutation:
            origin = single(b"origin")
            if b"origin" in headers and not _same_origin(origin, scheme, authority):
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            if single(b"sec-fetch-site") not in {"", "same-origin", "none"}:
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            referer = single(b"referer")
            if not origin and referer and not _same_origin(referer, scheme, authority, referer=True):
                raise DomainError("cross_origin", "Same-origin requests are required", 403)
            if single(b"content-encoding", "identity").lower() != "identity":
                raise DomainError("json_required", "Uncompressed UTF-8 JSON is required", 415)
            if not re.fullmatch(
                    r'\s*application/json\s*(?:;\s*charset\s*=\s*(?:utf-8|"utf-8")\s*)?',
                    single(b"content-type"), flags=re.IGNORECASE):
                raise DomainError("json_required", "An application/json object is required", 415)

        length = single(b"content-length")
        transfer = single(b"transfer-encoding")
        if ((length and not re.fullmatch(r"[0-9]{1,10}", length))
                or (b"content-length" in headers and not length)
                or (b"transfer-encoding" in headers and not transfer)
                or (transfer and (length or transfer.lower() != "chunked"))):
            raise DomainError("invalid_body", "Invalid request framing", 400)
        if length and int(length) > MAX_BODY:
            raise DomainError("body_too_large", "Request exceeds 1 MiB", 413)
        if not mutation and (transfer or (length and int(length) != 0)):
            raise DomainError("unexpected_body", "This method accepts no request body", 400)

        # Read even undeclared GET bodies; size and timeout limits apply to actual bytes.
        content = bytearray()
        try:
            async with asyncio.timeout(MAX_BODY_SECONDS):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        raise DomainError("invalid_body", "Invalid request stream", 400)
                    chunk = message.get("body", b"")
                    if len(content) + len(chunk) > MAX_BODY:
                        raise DomainError("body_too_large", "Request exceeds 1 MiB", 413)
                    content.extend(chunk)
                    if not mutation and content:
                        raise DomainError("unexpected_body", "This method accepts no request body", 400)
                    if length and len(content) > int(length):
                        raise DomainError("invalid_body", "Content length does not match", 400)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            raise DomainError("body_timeout", "Request body deadline exceeded", 408) from None
        if length and len(content) != int(length):
            raise DomainError("invalid_body", "Content length does not match", 400)
        if mutation:
            try:
                _json_object(bytes(content))
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise DomainError("invalid_json", "A bounded, unique-key, finite JSON object is required",
                                  400) from None

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(content), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _principal(request: Request) -> Principal:
    return request.state.principal


Authorized = Annotated[Principal, Depends(_principal)]


def create_app(data_root: Path, provider, *, clock=None) -> FastAPI:
    """Construct services, not users, fixtures or inference; close the provider on shutdown."""
    store = Store(data_root)
    service = SchedulingService(store, clock=clock)
    actions = ActionService(service)
    engine = AgentEngine(service, provider, clock=clock)
    closed = False
    close_lock = threading.Lock()

    def close_provider():
        nonlocal closed
        with close_lock:
            if not closed:
                closed = True
                provider.close()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await run_in_threadpool(close_provider)

    app = FastAPI(title="Synthetic production scheduling", docs_url=None,
                  redoc_url=None, openapi_url=None, debug=False, redirect_slashes=False,
                  lifespan=lifespan)
    app.state.store, app.state.service = store, service
    app.state.actions, app.state.engine = actions, engine
    app.state.provider, app.state.close_provider = provider, close_provider
    app.add_middleware(LocalBoundary, store=store)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return _error(exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return _error("validation_error", "Invalid fields, types or ranges", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return _error("http_error", "Resource or method unavailable", exc.status_code)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(files("scheduling_agent").joinpath("templates/index.html").read_text("utf-8"))

    @app.get("/static/{name}")
    def asset(name: str):
        if name not in ASSETS:
            raise DomainError("not_found", "Resource not found", 404)
        return Response(files("scheduling_agent").joinpath("static", name).read_bytes(),
                        media_type=ASSETS[name])

    @app.get("/health")
    def health():
        return {"status": "ok", "provider": provider.fingerprint, "synthetic_only": True}

    @app.get("/api/me")
    def me(current: Authorized):
        store.require(current)
        return {"user_id": current.user_id, "role": current.role,
                "plants": list(current.plants), "acl_version": current.acl_version}

    @app.get("/api/snapshots")
    def snapshots(current: Authorized):
        return {"items": store.list_objects("snapshot", current)}

    @app.post("/api/snapshots")
    def import_snapshot(payload: SnapshotRequest, current: Authorized):
        return service.import_snapshot(current, payload.snapshot, action_id=payload.action_id)

    @app.get("/api/snapshots/{snapshot_id}")
    def snapshot(snapshot_id: Identifier, current: Authorized):
        return service.get_snapshot(current, snapshot_id)

    @app.get("/api/snapshots/{snapshot_id}/scenarios")
    def scenarios(snapshot_id: Identifier, current: Authorized):
        source = service.get_snapshot(current, snapshot_id)
        try:
            items = demo_scenarios(Snapshot.model_validate(source["payload"]))
        except ValueError:
            # Custom or shortage snapshots do not satisfy fixture-specific perturbations.
            items = {"baseline": Scenario(kind="baseline")}
        return {"items": {name: item.model_dump(mode="json") for name, item in items.items()}}

    @app.post("/api/tasks")
    def create_task(payload: CreateTaskRequest, current: Authorized):
        return engine.create(current, payload.snapshot_id, scenario=payload.scenario,
                             action_id=payload.action_id)

    @app.get("/api/tasks")
    def tasks(current: Authorized):
        return {"items": store.list_objects("task", current)}

    @app.get("/api/tasks/{task_id}")
    def task(task_id: Identifier, current: Authorized):
        return engine.get(current, task_id)

    @app.post("/api/tasks/{task_id}/run")
    def run(task_id: Identifier, payload: EmptyRequest, current: Authorized):
        # Independent sync workers keep cancel available while inference waits.
        return engine.run(current, task_id)

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel(task_id: Identifier, payload: ActionRequest, current: Authorized):
        return engine.cancel(current, task_id, action_id=payload.action_id)

    @app.post("/api/tasks/{task_id}/resume")
    def resume(task_id: Identifier, payload: ResumeRequest, current: Authorized):
        return engine.resume(current, task_id, **payload.model_dump())

    @app.post("/api/tasks/{task_id}/reconcile")
    def reconcile(task_id: Identifier, payload: ReconcileRequest, current: Authorized):
        return engine.reconcile(current, task_id, **payload.model_dump())

    @app.get("/api/recommendations")
    def recommendations(current: Authorized):
        return {"items": store.list_objects("recommendation", current)}

    @app.get("/api/recommendations/{recommendation_id}")
    def recommendation(recommendation_id: Identifier, current: Authorized):
        return store.get_object("recommendation", recommendation_id, current)

    @app.post("/api/recommendations/{recommendation_id}/review")
    def review(recommendation_id: Identifier, payload: ReviewRequest, current: Authorized):
        return actions.review(current, recommendation_id, **payload.model_dump())

    @app.post("/api/recommendations/{recommendation_id}/request-publication")
    def request_publication(recommendation_id: Identifier, payload: PublicationRequest,
                            current: Authorized):
        return _public_publication(
            actions.request_publication(current, recommendation_id, **payload.model_dump()))

    @app.get("/api/publications")
    def publications(current: Authorized):
        return {"items": [_public_publication(obj)
                          for obj in store.list_objects("publication", current)]}

    @app.get("/api/publications/{publication_id}")
    def publication(publication_id: Identifier, current: Authorized):
        return _public_publication(store.get_object("publication", publication_id, current))

    @app.post("/api/publications/{publication_id}/deliver-local")
    def deliver_local(publication_id: Identifier, payload: ActionRequest, current: Authorized):
        return _public_publication(
            actions.deliver_local(current, publication_id, action_id=payload.action_id))

    @app.post("/api/publications/{publication_id}/advance-receiver")
    def advance_receiver(publication_id: Identifier, payload: ActionRequest, current: Authorized):
        return _public_publication(
            actions.advance_receiver(current, publication_id, action_id=payload.action_id))

    @app.post("/api/publications/{publication_id}/readback")
    def readback(publication_id: Identifier, payload: ActionRequest, current: Authorized):
        return _public_publication(
            actions.readback(current, publication_id, action_id=payload.action_id))

    @app.post("/api/publications/{publication_id}/cancel")
    def cancel_publication(publication_id: Identifier, payload: CancelPublicationRequest,
                           current: Authorized):
        return _public_publication(
            actions.cancel_publication(current, publication_id, **payload.model_dump()))

    @app.post("/api/publications/{publication_id}/execute-synthetic")
    def execute_synthetic(publication_id: Identifier, payload: ExecuteRequest, current: Authorized):
        return _public_publication(
            actions.execute_synthetic(current, publication_id, **payload.model_dump()))

    @app.get("/api/audit")
    def audit(current: Authorized):
        return {"items": store.audit_events(current)}

    return app
