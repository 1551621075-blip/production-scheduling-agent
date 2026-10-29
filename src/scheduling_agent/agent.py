"""Bounded single-agent graph; only fenced domain tools issue scheduling evidence."""

from __future__ import annotations

import copy
import math
import threading
import time
import uuid
from contextlib import contextmanager
from typing import TypedDict

from langchain_core.runnables.config import var_child_runnable_config
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import ValidationError

from .domain import Plan, Scenario, SolveResult, canonical_digest, canonical_json, missing_inputs
from .errors import DomainError
from .provider import MAX_REPORTED_TOKENS, TOOLS, ProviderError, ProviderReply
from .service import EVIDENCE_TOOLS

MAX_CALLS = 16
MAX_TOOLS = 14
MAX_TOKENS = 40000
MAX_SECONDS = 600.0
LEASE_SECONDS = 1200.0
TERMINAL = {"submitted", "no_schedule", "cancelled"}


class GraphState(TypedDict):
    task_id: str
    stop: bool


def _identifier(prefix):
    return prefix + uuid.uuid4().hex


def _note(value):
    if type(value) is not str or not value.strip() or len(value) > 2000:
        raise DomainError("invalid_note", "A bounded nonempty note is required", 422)
    return value


def _same(left, right):
    return canonical_json(left) == canonical_json(right)


class AgentEngine:
    def __init__(self, service, provider, *, clock=None, monotonic=None):
        self.service = service
        self.store = service.store
        self.provider = provider
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.monotonic = monotonic or time.monotonic
        self._provider_lock = threading.Lock()

    def _now(self):
        value = self.clock()
        if type(value) is not int or not 0 <= value <= 9007199254740991:
            raise DomainError("clock_invalid", "Invalid local clock")
        return value

    def _provider_identity(self):
        fingerprint = self.provider.fingerprint
        if type(fingerprint) is not str or not fingerprint.strip() or len(fingerprint) > 512:
            raise DomainError("provider_identity", "An explicit provider fingerprint is required")
        return fingerprint

    def _usage(self, conn, task_id, owner):
        rows = conn.execute(
            "SELECT status,tokens,elapsed_s FROM calls WHERE task_id=? AND owner=?",
            (task_id, owner),
        ).fetchall()
        return {
            "calls": len(rows),
            "tokens": sum(row["tokens"] or 0 for row in rows),
            "unknown_calls": sum(row["status"] in {"started", "unknown"} for row in rows),
            "call_elapsed_s": sum(row["elapsed_s"] for row in rows),
        }

    def _load(self, conn, principal, task_id):
        obj = self.store.get_object("task", task_id, principal, conn=conn)
        p = obj["payload"]
        usage = self._usage(conn, task_id, principal.user_id)
        p["calls"] = usage["calls"]
        p["tokens"] = usage["tokens"]
        p["unknown_calls"] = usage["unknown_calls"]
        accounted = p.get("accounted_call_elapsed_s", 0.0)
        p["elapsed_s"] += max(0.0, usage["call_elapsed_s"] - accounted)
        p["accounted_call_elapsed_s"] = usage["call_elapsed_s"]
        return obj

    def _save(self, conn, principal, obj, p):
        return self.store.update_object(
            conn, kind="task", object_id=obj["object_id"], principal=principal,
            expected_version=obj["version"], status=p["status"], payload=p,
        )

    def _audit(self, conn, principal, obj, action, detail):
        self.store.audit(conn, principal, kind="task", object_id=obj["object_id"],
                         action=action, detail=detail)

    def _snapshot(self, conn, principal, p):
        return self.service.verify_source_binding(
            conn, principal, p["source_binding"], require_complete=False,
        )

    def create(self, principal, snapshot_id, *, scenario: Scenario, action_id):
        if not isinstance(scenario, Scenario):
            raise DomainError("validation", "An explicit scheduling scenario is required", 422)
        try:
            scenario = Scenario.model_validate(scenario.model_dump(mode="json"))
        except (ValidationError, TypeError, ValueError):
            raise DomainError("validation", "Invalid scheduling scenario", 422) from None
        request = {"operation": "create_task", "snapshot_id": snapshot_id,
                   "scenario": scenario.model_dump(mode="json")}
        with self.store.transaction() as conn:
            binding = self.service.source_binding(conn, principal, snapshot_id)
            old = self.store.receipt(conn, principal, action_id, request)
            if old is not None:
                return old
            self.service.verify_source_binding(conn, principal, binding, require_complete=False)
            task_id = _identifier("task-")
            payload = {
                "snapshot_id": snapshot_id, "source_binding": binding,
                "scenario": scenario.model_dump(mode="json"), "evidence": {}, "candidate": None,
                "status": "created", "calls": 0, "tools": 0, "tokens": 0,
                "unknown_calls": 0, "elapsed_s": 0.0, "accounted_call_elapsed_s": 0.0,
                "error": None, "no_schedule_reason": None,
                "pending_action": None, "awaiting_fields": [], "question": None,
                "lease_id": None, "lease_until_ms": 0,
                "provider_fingerprint": self._provider_identity(),
                "synthetic": True, "recommendation_id": None, "handled_call_ids": [],
            }
            obj = self.store.create_object(
                conn, kind="task", object_id=task_id, principal=principal,
                plant_id=binding["plant_id"], status="created", payload=payload,
            )
            self._audit(conn, principal, obj, "task_created", {"snapshot_id": snapshot_id})
            self.store.record_receipt(conn, principal, action_id, request, obj)
            return obj

    def get(self, principal, task_id):
        with self.store.connect() as conn:
            return self._load(conn, principal, task_id)

    def _fence(self, obj, lease_id):
        p = obj["payload"]
        if p["lease_id"] != lease_id or p["status"] != "running":
            raise DomainError("worker_stopped", "Task was stopped or replaced")
        if self._now() >= p["lease_until_ms"]:
            raise DomainError("lease_expired", "Worker lease expired")

    def _budget(self, p, *, call=False, tool=False):
        if p["unknown_calls"]:
            raise DomainError("model_unknown", "Unknown model result requires reconciliation")
        if p["tokens"] >= MAX_TOKENS or p["elapsed_s"] >= MAX_SECONDS:
            raise DomainError("budget_exhausted", "Task budget exhausted")
        if (call and p["calls"] >= MAX_CALLS) or ((call or tool) and p["tools"] >= MAX_TOOLS):
            raise DomainError("budget_exhausted", "Task budget exhausted")

    @staticmethod
    def _solver_results(p):
        try:
            payload = p["evidence"]["solve_schedule"]["payload"]
            results = {name: SolveResult.model_validate(payload[name])
                       for name in ("baseline", "optimized")}
            if results["baseline"].mode != "feasibility" or results["optimized"].mode != "optimize":
                raise ValueError
            return results
        except (KeyError, TypeError, ValueError):
            raise DomainError("solver_error", "Solver evidence is unavailable or inconsistent") from None

    def _schedule_outcome(self, p):
        """No candidate is not the same as an invalid candidate or an infeasibility proof."""
        evidence = p["evidence"]
        if "solve_schedule" not in evidence:
            return None
        results = self._solver_results(p)
        present = {name: result for name, result in results.items() if result.plan is not None}
        if "validate_schedule" not in evidence:
            # The service rejects ERROR results at its validation boundary. There is
            # nothing to validate when both runs lack plans; retain their distinct
            # statuses and terminate honestly instead of inventing validation evidence.
            if not present and any(result.status == "ERROR" for result in results.values()):
                return "solver_error"
            return None
        report = evidence["validate_schedule"]["payload"]
        if (
            type(report.get("valid_count")) is not int
            or report["valid_count"] != len(present)
            or any(report.get(name) is not None for name in results if name not in present)
        ):
            raise DomainError("invalid_plan", "Independent candidate validation failed")
        if not present:
            if report.get("valid") is not False:
                raise DomainError("invalid_plan", "Empty candidate validation is inconsistent")
            statuses = {result.status for result in results.values()}
            if statuses == {"INFEASIBLE"}:
                return "proven_infeasible"
            if "ERROR" in statuses:
                return "solver_error"
            return "search_unknown"
        if report.get("valid") is not True or any(
            type(report.get(name)) is not dict or report[name].get("valid") is not True
            for name in present
        ):
            raise DomainError("invalid_plan", "Independent candidate validation failed")
        if any(result.status == "ERROR" for result in results.values()):
            raise DomainError("solver_error", "A solver run failed; submission is blocked")
        return None

    @staticmethod
    def _candidate(record):
        try:
            candidate = record["payload"]["candidate"]
            if type(candidate) is not dict or set(candidate) != {"plan_hash", "plan"}:
                raise ValueError
            plan = Plan.model_validate(candidate["plan"])
            if (candidate["plan_hash"] != canonical_digest(plan)
                    or not _same(candidate["plan"], plan.model_dump(mode="json"))):
                raise ValueError
            return copy.deepcopy(candidate)
        except (KeyError, TypeError, ValueError):
            raise DomainError("invalid_plan", "Comparison did not issue a bound candidate") from None

    def _simulation_ready(self, p):
        evidence = p["evidence"]
        candidate = self._candidate(evidence["compare_schedules"])
        if not _same(candidate, p["candidate"]):
            raise DomainError("candidate_changed", "Candidate differs from issued comparison")
        plans = self._solver_results(p)
        hashes = {canonical_digest(result.plan) for result in plans.values() if result.plan is not None}
        simulations = evidence["simulate_schedule"]["payload"].get("simulations")
        if type(simulations) is not dict or set(simulations) != hashes:
            raise DomainError("invalid_simulation", "Simulation must cover every validated candidate")
        selected = simulations.get(candidate["plan_hash"])
        plan = candidate["plan"]
        if (
            type(selected) is not dict or selected.get("synthetic") is not True
            or selected.get("status") != "completed"
            or selected.get("plan_hash") != candidate["plan_hash"]
            or selected.get("problem_hash") != plan["problem_hash"]
            or selected.get("snapshot_hash") != plan["snapshot_hash"]
        ):
            raise DomainError("invalid_simulation", "Selected candidate needs completed bound simulation")

    def _available(self, p, snapshot):
        issued = set(p["evidence"])
        if "get_snapshot" not in issued:
            return ["get_snapshot"]
        if missing_inputs(snapshot):
            return ["request_human_input"]
        if self._schedule_outcome(p) is not None:
            return []
        for tool in ("build_scenario", "compile_problem", "solve_schedule", "validate_schedule"):
            if tool not in issued:
                return [tool]
        menu = [tool for tool in ("compare_schedules", "simulate_schedule") if tool not in issued]
        if menu:
            return menu
        self._simulation_ready(p)
        return ["submit_schedule"]

    def _model_context(self, task_id, p, snapshot):
        # The provider projects issued payloads to an explicit bounded fact whitelist.
        # No provider metadata, endpoint, identity, note or authorization enters this context.
        return copy.deepcopy({
            "task_id": task_id, "context_hash": canonical_digest(p["source_binding"]),
            "available_tools": self._available(p, snapshot), "evidence": p["evidence"],
            "candidate": p["candidate"], "required_input": list(missing_inputs(snapshot)),
        })

    def _pending_action(self, conn, principal, task_id, p):
        pending = p["pending_action"]
        if (type(pending) is not dict or set(pending) != {"call_id", "action"}
                or type(pending["call_id"]) is not str):
            raise DomainError("integrity", "Invalid pending model action")
        if pending["call_id"] in p["handled_call_ids"]:
            raise DomainError("integrity", "Model response has already been consumed")
        row = conn.execute(
            "SELECT status,response_hash FROM calls WHERE call_id=? AND task_id=? AND owner=?",
            (pending["call_id"], task_id, principal.user_id),
        ).fetchone()
        if row is None or row["status"] != "completed" or not row["response_hash"]:
            raise DomainError("integrity", "Pending action is not bound to a completed response")
        response = self.store.get_artifact(row["response_hash"])
        if set(response) != {"action"} or not _same(response["action"], pending["action"]):
            raise DomainError("integrity", "Pending action differs from recorded model response")
        return pending["action"]

    def _validate_action(self, action, p, snapshot):
        if type(action) is not dict or set(action) != {"tool", "arguments"}:
            raise DomainError("invalid_action", "Model action has an invalid shape")
        tool, args = action["tool"], action["arguments"]
        if type(tool) is not str or tool not in TOOLS or tool not in self._available(p, snapshot):
            raise DomainError("tool_unavailable", "Tool is not available in this state")
        if type(args) is not dict:
            raise DomainError("invalid_action", "Tool arguments must be an object")
        if tool == "request_human_input":
            if set(args) != {"question", "fields"}:
                raise DomainError("invalid_action", "Invalid human input request")
            _note(args["question"])
            fields = args["fields"]
            required = list(missing_inputs(snapshot))
            if (type(fields) is not list or any(type(field) is not str for field in fields)
                    or sorted(fields) != sorted(required)):
                raise DomainError("invalid_action", "Human input fields must match all missing inputs")
        elif tool == "submit_schedule":
            if set(args) != {"candidate_hash", "evidence_ids", "explanation"}:
                raise DomainError("invalid_action", "Invalid submission arguments")
            _note(args["explanation"])
            ids = args["evidence_ids"]
            expected = [record["evidence_id"] for record in p["evidence"].values()]
            if type(ids) is not list or any(type(item) is not str for item in ids):
                raise DomainError("invalid_evidence", "Invalid evidence references")
            if len(set(expected)) != len(expected) or sorted(ids) != sorted(expected):
                raise DomainError("invalid_evidence", "Submission must cite all issued evidence once")
            if (type(args["candidate_hash"]) is not str
                    or args["candidate_hash"] != p["candidate"]["plan_hash"]):
                raise DomainError("candidate_changed", "Submission candidate does not match frozen hash")
        elif args:
            raise DomainError("invalid_action", "This tool accepts no arguments")
        return tool, args

    def _elapsed(self, started, base):
        value = self.monotonic()
        if type(value) not in (int, float) or not math.isfinite(value) or value < started:
            raise DomainError("clock_invalid", "Invalid monotonic clock")
        return base + value - started

    def _account(self, call_id, task_id, owner, lease_id, *, status, tokens, elapsed, response=None,
                 error=None):
        if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= MAX_REPORTED_TOKENS):
            status, tokens, response, error = "unknown", None, None, "invalid_usage"
        response_hash = self.store.put_artifact(response) if response is not None else None
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT task_id,owner,lease_id,status,tokens,elapsed_s,error_code FROM calls "
                "WHERE call_id=?", (call_id,),
            ).fetchone()
            if row is None or (row["task_id"], row["owner"], row["lease_id"]) != (
                task_id, owner, lease_id,
            ):
                raise DomainError("call_fence", "Model journal ownership changed")
            if row["status"] == "failed" and row["error_code"] == "reconciled_no_result":
                # A late original worker may increase known cost, never resurrect its action.
                conn.execute(
                    "UPDATE calls SET tokens=?,elapsed_s=? WHERE call_id=?",
                    (max(row["tokens"] or 0, tokens or 0), max(row["elapsed_s"], elapsed), call_id),
                )
                return
            if row["status"] not in {"started", "unknown"}:
                raise DomainError("call_fence", "Model journal was already settled")
            cursor = conn.execute(
                "UPDATE calls SET status=?,tokens=?,elapsed_s=?,response_hash=?,error_code=? "
                "WHERE call_id=? AND task_id=? AND owner=? AND lease_id=? "
                "AND (status='started' OR (status='unknown' AND error_code='interrupted_call' "
                "AND tokens IS NULL))",
                (status, tokens, elapsed, response_hash, error, call_id, task_id, owner, lease_id),
            )
            if cursor.rowcount != 1:
                raise DomainError("call_fence", "Model journal ownership changed")

    def _stop(self, principal, task_id, lease_id, code, elapsed=None):
        with self.store.transaction() as conn:
            if callable(elapsed):
                try:
                    elapsed = elapsed(conn)
                except Exception:
                    elapsed = None
            obj = self._load(conn, principal, task_id)
            p = obj["payload"]
            if p["lease_id"] != lease_id:
                if code in {"provider_unknown", "provider_cleanup_unknown"} and (
                    p["lease_id"] is None and p["status"] in TERMINAL | {"awaiting_input"}
                ):
                    p["error"] = code
                    obj = self._save(conn, principal, obj, p)
                    self._audit(conn, principal, obj, "provider_cleanup_failed", {"code": code})
                return obj
            if p["status"] in TERMINAL:
                conn.execute(
                    "UPDATE calls SET status='unknown',error_code='interrupted_call' "
                    "WHERE task_id=? AND owner=? AND lease_id=? AND status='started'",
                    (task_id, principal.user_id, lease_id),
                )
                p.update(lease_id=None, lease_until_ms=0)
                if code in {"provider_cleanup_unknown", "provider_unknown"}:
                    p["error"] = code
                if elapsed is not None and math.isfinite(elapsed):
                    p["elapsed_s"] = max(p["elapsed_s"], elapsed)
                return self._save(conn, principal, obj, p)
            conn.execute(
                "UPDATE calls SET status='unknown',error_code='interrupted_call' "
                "WHERE task_id=? AND owner=? AND lease_id=? AND status='started'",
                (task_id, principal.user_id, lease_id),
            )
            p["status"] = "blocked"
            p["error"] = code
            if code == "source_changed":
                # Retain the completed journal and its cost, not a stale dispatch intent.
                # The unchanged binding fences future runs before journal recovery; this
                # is not a committed tool operation and must not mark the call handled.
                p["pending_action"] = None
            p["lease_id"] = None
            p["lease_until_ms"] = 0
            if elapsed is not None and math.isfinite(elapsed):
                p["elapsed_s"] = max(p["elapsed_s"], elapsed)
            obj = self._save(conn, principal, obj, p)
            self._audit(conn, principal, obj, "task_blocked", {"code": code})
            return obj

    @contextmanager
    def _provider_scope(self):
        # The provider owns startup/cleanup. Never replace it or fall back to another provider.
        with self.provider.session():
            yield

    def run(self, principal, task_id):
        self.get(principal, task_id)
        if not self._provider_lock.acquire(blocking=False):
            raise DomainError("provider_busy", "A local model run is already active")
        try:
            return self._run(principal, task_id)
        finally:
            self._provider_lock.release()

    def _run(self, principal, task_id):
        lease_id = _identifier("lease-")
        started = self.monotonic()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise DomainError("clock_invalid", "Invalid monotonic clock")
        with self.store.transaction() as conn:
            obj = self._load(conn, principal, task_id)
            p = obj["payload"]
            if p["status"] in TERMINAL or p["status"] == "awaiting_input":
                if (p["status"] == "cancelled" and p["unknown_calls"]
                        and (p["lease_id"] is None or self._now() >= p["lease_until_ms"])):
                    conn.execute(
                        "UPDATE calls SET status='unknown',error_code='interrupted_call' "
                        "WHERE task_id=? AND owner=? AND status='started'",
                        (task_id, principal.user_id),
                    )
                    p.update(lease_id=None, lease_until_ms=0)
                    return self._save(conn, principal, obj, p)
                return obj
            if p["lease_id"] is not None and self._now() < p["lease_until_ms"]:
                raise DomainError("busy", "Task already has an active worker")
            if p["unknown_calls"]:
                conn.execute(
                    "UPDATE calls SET status='unknown',error_code='interrupted_call' "
                    "WHERE task_id=? AND owner=? AND status='started'",
                    (task_id, principal.user_id),
                )
                p.update(status="blocked", error="model_unknown", lease_id=None, lease_until_ms=0)
                return self._save(conn, principal, obj, p)
            self._snapshot(conn, principal, p)
            if p["provider_fingerprint"] != self._provider_identity():
                raise DomainError("provider_changed", "Resume requires the original provider")
            self._budget(p)
            p.update(status="running", error=None, lease_id=lease_id,
                     lease_until_ms=self._now() + int(LEASE_SECONDS * 1000))
            base = p["elapsed_s"]
            previous_call_elapsed = p["accounted_call_elapsed_s"]
            self._save(conn, principal, obj, p)

        def runtime_elapsed(conn):
            previous = conn.execute(
                "SELECT COALESCE(SUM(elapsed_s),0) FROM calls "
                "WHERE task_id=? AND owner=? AND lease_id<>?",
                (task_id, principal.user_id, lease_id),
            ).fetchone()[0]
            return self._elapsed(started, base) + max(0.0, previous - previous_call_elapsed)

        def agent_node(state):
            with self.store.transaction() as conn:
                obj = self._load(conn, principal, task_id)
                self._fence(obj, lease_id)
                p = obj["payload"]
                p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
                snapshot = self._snapshot(conn, principal, p)
                self._budget(p)
                if p["pending_action"] is not None:
                    self._save(conn, principal, obj, p)
                    return {"stop": False}
                completed = conn.execute(
                    "SELECT call_id,response_hash FROM calls WHERE task_id=? AND owner=? "
                    "AND status='completed' ORDER BY created_at_ms,call_id",
                    (task_id, principal.user_id),
                ).fetchall()
                unhandled = [row for row in completed if row["call_id"] not in p["handled_call_ids"]]
                if unhandled:
                    if len(unhandled) != 1 or not unhandled[0]["response_hash"]:
                        raise DomainError("integrity", "Ambiguous unhandled model response")
                    response = self.store.get_artifact(unhandled[0]["response_hash"])
                    if set(response) != {"action"}:
                        raise DomainError("integrity", "Invalid stored model response")
                    p["pending_action"] = {
                        "call_id": unhandled[0]["call_id"], "action": response["action"],
                    }
                    self._save(conn, principal, obj, p)
                    return {"stop": False}
                menu = self._available(p, snapshot)
                if not menu:
                    reason = self._schedule_outcome(p)
                    if reason is None:
                        raise DomainError("integrity", "Scheduling state has no supported next action")
                    p.update(status="no_schedule", lease_id=None, lease_until_ms=0,
                             error=reason, no_schedule_reason=reason)
                    obj = self._save(conn, principal, obj, p)
                    self._audit(conn, principal, obj, "no_schedule", {"reason": reason})
                    return {"stop": True}
                self._budget(p, call=True)
                model_context = self._model_context(task_id, p, snapshot)
                context_digest = canonical_digest(model_context)
                call_id = _identifier("call-")
                conn.execute(
                    "INSERT INTO calls(call_id,task_id,owner,lease_id,status,created_at_ms) "
                    "VALUES(?,?,?,?,'started',?)",
                    (call_id, task_id, principal.user_id, lease_id, self._now()),
                )
                self._save(conn, principal, obj, p)
            sent = False
            call_start = None
            tokens = None
            reply = None
            accounted = False
            try:
                call_start = self.monotonic()
                if type(call_start) not in (int, float) or not math.isfinite(call_start):
                    raise DomainError("clock_invalid", "Invalid monotonic clock")
                with self.store.transaction() as conn:
                    current = self._load(conn, principal, task_id)
                    self._fence(current, lease_id)
                    current_p = current["payload"]
                    current_p["elapsed_s"] = max(current_p["elapsed_s"], runtime_elapsed(conn))
                    reserved = conn.execute(
                        "SELECT status FROM calls WHERE call_id=? AND task_id=? AND owner=? "
                        "AND lease_id=?", (call_id, task_id, principal.user_id, lease_id),
                    ).fetchone()
                    if reserved is None or reserved["status"] != "started":
                        raise DomainError("call_fence", "Reserved call was changed before dispatch")
                    self._budget({**current_p, "calls": current_p["calls"] - 1,
                                  "unknown_calls": current_p["unknown_calls"] - 1}, call=True)
                    current_snapshot = self._snapshot(conn, principal, current_p)
                    if (current_p["pending_action"] is not None or context_digest != canonical_digest(
                        self._model_context(task_id, current_p, current_snapshot),
                    )):
                        raise DomainError("task_changed", "Model context changed before dispatch")
                    self._save(conn, principal, current, current_p)
                sent = True
                reply = self.provider.decide(model_context)
                if not isinstance(reply, ProviderReply):
                    raise DomainError("invalid_reply", "Provider returned an invalid reply")
                tokens = reply.tokens
                if type(tokens) is not int or not 1 <= tokens <= MAX_REPORTED_TOKENS:
                    raise DomainError("invalid_usage", "Provider returned invalid usage")
                elapsed = self._elapsed(call_start, 0.0)
                self._account(call_id, task_id, principal.user_id, lease_id, status="completed",
                              tokens=tokens, elapsed=elapsed, response={"action": reply.action})
                accounted = True
            except Exception as exc:
                if accounted:
                    raise
                if isinstance(exc, ProviderError):
                    tokens = exc.tokens
                    sent = exc.sent
                if not sent:
                    tokens = 0
                known = type(tokens) is int and 0 <= tokens <= MAX_REPORTED_TOKENS
                error = exc.code if known and isinstance(exc, DomainError) else "model_unknown"
                try:
                    if call_start is None:
                        elapsed = 0.0
                    else:
                        elapsed = max(0.0, self.monotonic() - call_start)
                    if not math.isfinite(elapsed):
                        elapsed = 0.0
                except Exception:
                    elapsed = 0.0
                unknown = not known or (sent and error in {
                    "provider_unknown", "provider_cleanup_unknown", "model_unknown",
                })
                self._account(call_id, task_id, principal.user_id, lease_id,
                              status="unknown" if unknown else "failed", tokens=tokens if known else None,
                              elapsed=elapsed, error=error)
                raise DomainError(error, "Provider call did not produce a usable result") from None
            finally:
                with self.store.transaction() as conn:
                    row = conn.execute(
                        "SELECT status FROM calls WHERE call_id=? AND task_id=? AND owner=? "
                        "AND lease_id=?", (call_id, task_id, principal.user_id, lease_id),
                    ).fetchone()
                    if row is None or row["status"] not in {"completed", "failed", "unknown"}:
                        raise DomainError("call_fence", "Model journal did not reach a recorded state")
            with self.store.transaction() as conn:
                obj = self._load(conn, principal, task_id)
                self._fence(obj, lease_id)
                p = obj["payload"]
                p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
                self._budget(p)
                self._snapshot(conn, principal, p)
                p["pending_action"] = {"call_id": call_id, "action": reply.action}
                self._pending_action(conn, principal, task_id, p)
                self._save(conn, principal, obj, p)
            return {"stop": False}

        def guard_node(state):
            with self.store.transaction() as conn:
                obj = self._load(conn, principal, task_id)
                self._fence(obj, lease_id)
                p = obj["payload"]
                p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
                self._budget(p, tool=True)
                snapshot = self._snapshot(conn, principal, p)
                self._validate_action(self._pending_action(conn, principal, task_id, p), p, snapshot)
                self._save(conn, principal, obj, p)
            return {"stop": False}

        def commit_tool(conn, obj, tool):
            p = obj["payload"]
            p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
            self._budget(p)
            p["tools"] += 1
            p["handled_call_ids"].append(p["pending_action"]["call_id"])
            p["pending_action"] = None
            obj = self._save(conn, principal, obj, p)
            self._audit(conn, principal, obj, "tool_completed", {"tool": tool})
            return {"stop": p["status"] != "running"}

        def tools_node(state):
            with self.store.transaction() as conn:
                obj = self._load(conn, principal, task_id)
                self._fence(obj, lease_id)
                p = obj["payload"]
                p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
                self._budget(p, tool=True)
                snapshot = self._snapshot(conn, principal, p)
                tool, args = self._validate_action(
                    self._pending_action(conn, principal, task_id, p), p, snapshot,
                )
                pending = copy.deepcopy(p["pending_action"])
                # _load changes usage/watermarks. Services require this EXACT persisted object.
                obj = self._save(conn, principal, obj, p)
                p = obj["payload"]
                if tool in EVIDENCE_TOOLS:
                    prepared = self.service.prepare_tool(conn, principal, obj, tool)
                elif tool == "request_human_input":
                    p.update(status="awaiting_input", awaiting_fields=args["fields"],
                             question=args["question"], lease_id=None, lease_until_ms=0)
                    return commit_tool(conn, obj, tool)
                else:
                    rec = self.service.submit_schedule(
                        conn, principal, obj, p["candidate"], p["evidence"], args["explanation"],
                    )
                    p.update(status="submitted", recommendation_id=rec["object_id"],
                             lease_id=None, lease_until_ms=0)
                    return commit_tool(conn, obj, tool)

            # ALL seven computations, especially CP-SAT and simulation, run without a DB transaction.
            result = self.service.compute_tool(prepared, tool)

            with self.store.transaction() as conn:
                obj = self._load(conn, principal, task_id)
                self._fence(obj, lease_id)
                p = obj["payload"]
                p["elapsed_s"] = max(p["elapsed_s"], runtime_elapsed(conn))
                self._budget(p, tool=True)
                snapshot = self._snapshot(conn, principal, p)
                checked_tool, _ = self._validate_action(
                    self._pending_action(conn, principal, task_id, p), p, snapshot,
                )
                if checked_tool != tool or not _same(p["pending_action"], pending):
                    raise DomainError("task_changed", "Pending action changed during computation")
                obj = self._save(conn, principal, obj, p)
                envelope = self.service.issue_evidence(conn, principal, obj, tool, result)
                p = obj["payload"]
                p["evidence"][tool] = envelope
                if tool == "compare_schedules":
                    p["candidate"] = self._candidate(envelope)
                return commit_tool(conn, obj, tool)

        graph = StateGraph(GraphState)
        graph.add_node("agent", agent_node)
        graph.add_node("guard", guard_node)
        graph.add_node("tools", tools_node)
        graph.add_edge(START, "agent")
        graph.add_conditional_edges("agent", lambda s: END if s["stop"] else "guard")
        graph.add_edge("guard", "tools")
        graph.add_conditional_edges("tools", lambda s: END if s["stop"] else "agent")
        # RunnableConfig merges inherited callbacks even when invoke passes []. Isolate the
        # per-context parent config as well; never mutate process-wide tracing settings.
        config_token = var_child_runnable_config.set({"callbacks": []})
        try:
            with tracing_context(enabled=False), self._provider_scope():
                graph.compile().invoke({"task_id": task_id, "stop": False},
                                       config={"recursion_limit": 80, "callbacks": []})
        except Exception as exc:
            code = exc.code if isinstance(exc, DomainError) else "worker_failed"
            self._stop(principal, task_id, lease_id, code, runtime_elapsed)
        finally:
            var_child_runnable_config.reset(config_token)
        return self.get(principal, task_id)

    def cancel(self, principal, task_id, *, action_id):
        request = {"operation": "cancel", "task_id": task_id}
        with self.store.transaction() as conn:
            obj = self._load(conn, principal, task_id)
            old = self.store.receipt(conn, principal, action_id, request)
            if old is not None:
                return old
            p = obj["payload"]
            if p["status"] in TERMINAL:
                raise DomainError("terminal", "Terminal task cannot be cancelled")
            # Retain the lease until an in-flight original worker accounts its reliable usage.
            p.update(status="cancelled", error="cancelled", pending_action=None)
            obj = self._save(conn, principal, obj, p)
            self._audit(conn, principal, obj, "task_cancelled", {})
            self.store.record_receipt(conn, principal, action_id, request, obj)
            return obj

    def resume(self, principal, task_id, *, action_id, answers: list[dict], note):
        _note(note)
        request = {"operation": "supplement", "task_id": task_id, "answers": answers, "note": note}
        with self.store.transaction() as conn:
            obj = self._load(conn, principal, task_id)
            p = obj["payload"]
            if p["provider_fingerprint"] != self._provider_identity():
                raise DomainError("provider_changed", "Resume requires the original provider")
            old = self.store.receipt(conn, principal, action_id, request)
            if old is not None:
                return old
            if p["status"] != "awaiting_input":
                raise DomainError("state", "Task is not waiting for human input")
            snapshot = self._snapshot(conn, principal, p)
            required = list(missing_inputs(snapshot))
            if sorted(p["awaiting_fields"]) != sorted(required):
                raise DomainError("invalid_input", "Requested fields differ from current missing inputs")
            if (type(answers) is not list or any(
                type(answer) is not dict or set(answer) != {"field", "value"}
                or type(answer["field"]) is not str or type(answer["value"]) is not int
                for answer in answers
            ) or sorted(answer["field"] for answer in answers) != sorted(required)):
                raise DomainError("invalid_input", "Supply exactly the requested missing integer fields", 422)
            self.service.supplement_snapshot(
                conn, principal, p["snapshot_id"], answers=answers,
                expected_binding=p["source_binding"],
            )
            binding = self.service.source_binding(conn, principal, p["snapshot_id"])
            p.update(source_binding=binding, evidence={}, candidate=None, pending_action=None,
                     awaiting_fields=[], question=None, status="created", error=None,
                     no_schedule_reason=None, lease_id=None, lease_until_ms=0)
            obj = self._save(conn, principal, obj, p)
            self._audit(conn, principal, obj, "human_supplement", {"fields": required, "note": note})
            self.store.record_receipt(conn, principal, action_id, request, obj)
            return obj

    def reconcile(self, principal, task_id, *, call_id, action_id, tokens: int | None, note,
                  no_result=True):
        if no_result is not True:
            raise DomainError("attestation_required", "Explicit no-result attestation required", 422)
        if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= MAX_REPORTED_TOKENS):
            raise DomainError("invalid_usage", "Invalid token accounting", 422)
        _note(note)
        request = {"operation": "reconcile", "task_id": task_id, "call_id": call_id,
                   "no_result": no_result, "tokens": tokens, "note": note}
        with self.store.transaction() as conn:
            obj = self._load(conn, principal, task_id)
            self.store.require(principal, obj["plant_id"], {"admin"}, conn=conn)
            old = self.store.receipt(conn, principal, action_id, request)
            if old is not None:
                return old
            p = obj["payload"]
            if p["lease_id"] is not None and self._now() < p["lease_until_ms"]:
                raise DomainError("busy", "Cannot reconcile an active worker")
            row = conn.execute("SELECT * FROM calls WHERE call_id=? AND task_id=? AND owner=?",
                               (call_id, task_id, principal.user_id)).fetchone()
            if row is not None and row["status"] == "started":
                conn.execute(
                    "UPDATE calls SET status='unknown',error_code='interrupted_call' "
                    "WHERE call_id=? AND status='started'", (call_id,),
                )
            elif row is None or row["status"] != "unknown":
                raise DomainError("state", "Call is not awaiting reconciliation")
            known = row["tokens"] or 0
            charge = known + max(0, MAX_TOKENS - p["tokens"]) if tokens is None else tokens
            if charge < known:
                raise DomainError("invalid_usage", "Known usage cannot be decreased")
            conn.execute("UPDATE calls SET status='failed',tokens=?,error_code='reconciled_no_result' "
                         "WHERE call_id=? AND status='unknown'", (charge, call_id))
            usage = self._usage(conn, task_id, principal.user_id)
            status = ("cancelled" if p["status"] == "cancelled" else
                      "blocked" if usage["unknown_calls"] else "created")
            error = "cancelled" if status == "cancelled" else "model_unknown" if status == "blocked" else None
            p.update(status=status, pending_action=None, error=error, lease_id=None, lease_until_ms=0,
                     calls=usage["calls"], tokens=usage["tokens"], unknown_calls=usage["unknown_calls"])
            obj = self._save(conn, principal, obj, p)
            self._audit(conn, principal, obj, "call_reconciled", {"call_id": call_id,
                        "tokens": charge, "no_result": True, "note": note})
            self.store.record_receipt(conn, principal, action_id, request, obj)
            return obj
