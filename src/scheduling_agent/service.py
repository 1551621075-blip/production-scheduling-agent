"""Authorized source lineage and trusted scheduling evidence.

The engine owns task transitions, leases and the call journal. Its compute boundary
is prepare_tool (read transaction), compute_tool (NO transaction), issue_evidence
(new write transaction after fencing), then persist the returned envelope on the
task in that same transaction. Content digests are not authenticity signatures.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from .domain import (
    CompiledProblem,
    Plan,
    Scenario,
    Snapshot,
    SolveResult,
    canonical_digest,
    canonical_json,
    missing_inputs,
)
from .errors import DomainError
from .storage import Principal, Store

MAX_SOURCE_AGE_MS = 24 * 60 * 60 * 1000
EVIDENCE_TOOLS = (
    "get_snapshot",
    "build_scenario",
    "compile_problem",
    "solve_schedule",
    "validate_schedule",
    "compare_schedules",
    "simulate_schedule",
)
DEPENDENCIES = {
    "get_snapshot": (),
    "build_scenario": ("get_snapshot",),
    "compile_problem": ("build_scenario",),
    "solve_schedule": ("compile_problem",),
    "validate_schedule": ("solve_schedule",),
    "compare_schedules": ("validate_schedule",),
    "simulate_schedule": ("validate_schedule",),
}


def _error(
    code="integrity", message="Scheduling evidence is unavailable or inconsistent", status=409
):
    return DomainError(code, message, status)


def _json(value: Any) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise _error("validation", "A finite JSON value is required", 422) from None


def _same(left: Any, right: Any) -> bool:
    # Python equality alone conflates True, 1, and 1.0.
    return canonical_json(_json(left)) == canonical_json(_json(right))


def _model(model, value):
    try:
        # JSON serialization can coerce a bypass-constructed bool into an int.
        # Revalidate the original Python values before producing any JSON.
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        return model.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        raise _error("validation", "Invalid scheduling data", 422) from None


def _exact(left, right):
    if not _same(left, right):
        raise _error()


class _ComputedPayload(dict):
    """Private, nonserialized compute-origin seal; public JSON shape remains a dict."""

    def __init__(self, payload, origin, seal):
        super().__init__(payload)
        self.origin = origin
        self.seal = seal


class SchedulingService:
    def __init__(self, store: Store, *, clock=None):
        self.store = store
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        # Ephemeral capability only for the compute -> issue hop, never persisted
        # or exposed to provider context. Durable authority is the Store receipt.
        self._compute_key = secrets.token_bytes(32)

    @staticmethod
    def snapshot_object_id(owner: str, plant_id: str, snapshot_id: str) -> str:
        return "snapshot-" + canonical_digest(
            {
                "owner": owner,
                "plant_id": plant_id,
                "snapshot_id": snapshot_id,
            }
        )

    def _snapshot(self, obj: dict) -> Snapshot:
        snapshot = _model(Snapshot, obj["payload"])
        if (
            obj["kind"] != "snapshot"
            or obj["status"] != "ready"
            or obj["plant_id"] != snapshot.plant_id
            or obj["object_id"]
            != self.snapshot_object_id(
                obj["owner"],
                snapshot.plant_id,
                snapshot.snapshot_id,
            )
            or obj["content_hash"] != canonical_digest(obj["payload"])
        ):
            raise _error()
        _exact(obj["payload"], snapshot.model_dump(mode="json"))
        return snapshot

    def import_snapshot(self, principal: Principal, snapshot: Snapshot, action_id: str) -> dict:
        snapshot = _model(Snapshot, snapshot)
        payload = snapshot.model_dump(mode="json")
        with self.store.transaction() as conn:
            self.store.require(principal, snapshot.plant_id, {"reviewer", "admin"}, conn=conn)
            object_id = self.snapshot_object_id(
                principal.user_id,
                snapshot.plant_id,
                snapshot.snapshot_id,
            )
            request = {"action": "import_snapshot", "snapshot": payload}
            # Reauthorize the target before a receipt can return its old contents.
            current = self._optional_object(conn, principal, "snapshot", object_id)
            if current is not None:
                self._snapshot(current)
            previous = self.store.receipt(conn, principal, action_id, request)
            if previous is not None:
                if current is None:
                    raise _error()
                return previous
            if current is None:
                result = self.store.create_object(
                    conn,
                    kind="snapshot",
                    object_id=object_id,
                    principal=principal,
                    plant_id=snapshot.plant_id,
                    status="ready",
                    payload=payload,
                )
            else:
                old = self._snapshot(current)
                if snapshot.revision < old.revision or (
                    snapshot.revision == old.revision and not _same(payload, current["payload"])
                ):
                    raise _error("source_conflict", "Snapshot revision must advance")
                if snapshot.revision == old.revision:
                    result = current
                else:
                    result = self.store.update_object(
                        conn,
                        kind="snapshot",
                        object_id=object_id,
                        principal=principal,
                        expected_version=current["version"],
                        status="ready",
                        payload=payload,
                    )
            self.store.audit(
                conn,
                principal,
                kind="snapshot",
                object_id=object_id,
                action="import_snapshot",
                detail={"action_id": action_id, "revision": snapshot.revision},
            )
            self.store.record_receipt(conn, principal, action_id, request, result)
            return result

    def _optional_object(self, conn, principal, kind, object_id):
        try:
            return self.store.get_object(kind, object_id, principal, conn=conn)
        except DomainError as exc:
            if exc.code != "not_found":
                raise
            return None

    def get_snapshot(self, principal: Principal, snapshot_id: str) -> dict:
        obj = self.store.get_object("snapshot", snapshot_id, principal)
        self._snapshot(obj)
        return obj

    def source_binding(self, conn, principal: Principal, snapshot_id: str) -> dict:
        obj = self.store.get_object("snapshot", snapshot_id, principal, conn=conn)
        snapshot = self._snapshot(obj)
        return {
            "object_id": obj["object_id"],
            "version": obj["version"],
            "content_hash": obj["content_hash"],
            "revision": snapshot.revision,
            "plant_id": snapshot.plant_id,
            "owner": obj["owner"],
            "observed_at_ms": snapshot.observed_at_ms,
            "snapshot_hash": canonical_digest(snapshot),
        }

    def verify_source_binding(
        self,
        conn,
        principal: Principal,
        binding: dict,
        *,
        require_complete: bool = True,
    ) -> Snapshot:
        if type(binding) is not dict or type(binding.get("object_id")) is not str:
            raise _error("source_changed", "Current source binding is required")
        current = self.source_binding(conn, principal, binding["object_id"])
        if not _same(binding, current):
            raise _error("source_changed", "Source has changed; start from the current snapshot")
        obj = self.store.get_object("snapshot", binding["object_id"], principal, conn=conn)
        snapshot = self._snapshot(obj)
        self._fresh(snapshot)
        if require_complete and missing_inputs(snapshot):
            raise _error("missing_input", "Explicit source inputs are required")
        return snapshot

    def _fresh(self, snapshot: Snapshot):
        now = self.clock()
        if type(now) is not int or now < 0:
            raise _error("clock", "A valid millisecond clock is required")
        age = now - snapshot.observed_at_ms
        if age < 0:
            raise _error("source_future", "Source observation is in the future")
        if age > MAX_SOURCE_AGE_MS:
            raise _error("source_stale", "Source observation is older than 24 hours")

    def supplement_snapshot(
        self,
        conn,
        principal: Principal,
        snapshot_id: str,
        *,
        answers: list[dict],
        expected_binding: dict,
    ) -> dict:
        """Engine resume calls this inside its task/receipt write transaction."""
        if type(expected_binding) is not dict or expected_binding.get("object_id") != snapshot_id:
            raise _error("source_changed", "Current source binding is required")
        snapshot = self.verify_source_binding(
            conn,
            principal,
            expected_binding,
            require_complete=False,
        )
        required = missing_inputs(snapshot)
        if not required or type(answers) is not list or len(answers) != len(required):
            raise _error("invalid_answers", "Supply every missing field exactly once", 422)
        values = {}
        for answer in answers:
            if (
                type(answer) is not dict
                or set(answer) != {"field", "value"}
                or type(answer["field"]) is not str
                or answer["field"] not in required
                or answer["field"] in values
                or type(answer["value"]) is not int
            ):
                raise _error(
                    "invalid_answers", "Only explicit missing integer fields may be filled", 422
                )
            values[answer["field"]] = answer["value"]
        payload = snapshot.model_dump(mode="json")
        for path, value in values.items():
            match = re.fullmatch(r"(materials|orders)\[(\d+)\]\.(initial_stock|due_min)", path)
            assert match is not None  # Paths originate from domain.missing_inputs, not the model.
            section, index, field = match.groups()
            payload[section][int(index)][field] = value
        payload["revision"] += 1
        updated = _model(Snapshot, payload)
        result = self.store.update_object(
            conn,
            kind="snapshot",
            object_id=snapshot_id,
            principal=principal,
            expected_version=expected_binding["version"],
            status="ready",
            payload=updated.model_dump(mode="json"),
        )
        self.store.audit(
            conn,
            principal,
            kind="snapshot",
            object_id=snapshot_id,
            action="supplement_snapshot",
            detail={
                "fields": list(required),
                "previous_binding": expected_binding,
                "revision": updated.revision,
            },
        )
        return result

    def _task(self, conn, principal, task, *, running=False):
        if type(task) is not dict or type(task.get("object_id")) is not str:
            raise _error("task_changed", "A persisted task object is required")
        current = self.store.get_object("task", task["object_id"], principal, conn=conn)
        if not _same(task, current):
            raise _error("task_changed", "Task has changed")
        if running and current["status"] != "running":
            raise _error("task_state", "Only a running task may issue or submit evidence")
        payload = current["payload"]
        binding = payload.get("source_binding")
        if (
            type(binding) is not dict
            or binding.get("owner") != current["owner"]
            or binding.get("plant_id") != current["plant_id"]
            or binding.get("object_id") != payload.get("snapshot_id")
        ):
            raise _error()
        _model(Scenario, payload.get("scenario"))
        self._check_evidence(current)
        return current

    @staticmethod
    def _stamp(task, tool):
        payload = task["payload"]
        evidence = payload.get("evidence", {})
        return {
            "task_id": task["object_id"],
            "owner": task["owner"],
            "plant_id": task["plant_id"],
            "source_binding": payload["source_binding"],
            "scenario_hash": canonical_digest(_model(Scenario, payload["scenario"])),
            "dependencies": {name: evidence[name]["hash"] for name in DEPENDENCIES[tool]},
        }

    @staticmethod
    def _envelope(tool, payload):
        return {
            "evidence_id": "ev-" + canonical_digest({"tool": tool, "payload": payload}),
            "tool": tool,
            "payload": _json(payload),
            "hash": canonical_digest(payload),
        }

    def _check_evidence(self, task, *, complete=False):
        evidence = task["payload"].get("evidence")
        if type(evidence) is not dict or not set(evidence) <= set(EVIDENCE_TOOLS):
            raise _error()
        if complete and set(evidence) != set(EVIDENCE_TOOLS):
            raise _error("missing_evidence", "All seven trusted evidence records are required")
        for tool, record in evidence.items():
            if (
                type(record) is not dict
                or set(record) != {"evidence_id", "tool", "payload", "hash"}
                or type(record["payload"]) is not dict
                or not set(DEPENDENCIES[tool]) <= set(evidence)
            ):
                raise _error()
            _exact(record, self._envelope(tool, record["payload"]))
            for key, value in self._stamp(task, tool).items():
                _exact(record["payload"].get(key), value)

    def prepare_tool(self, conn, principal: Principal, task: dict, tool: str) -> dict:
        if type(tool) is not str or tool not in EVIDENCE_TOOLS:
            raise _error("invalid_tool", "Unknown domain evidence tool", 422)
        current = self._task(conn, principal, task, running=True)
        evidence = current["payload"]["evidence"]
        if tool in evidence or not set(DEPENDENCIES[tool]) <= set(evidence):
            raise _error("tool_order", "Tool is not available for the current evidence")
        source = self.verify_source_binding(
            conn,
            principal,
            current["payload"]["source_binding"],
            require_complete=tool != "get_snapshot",
        )
        if tool in {"compare_schedules", "simulate_schedule"}:
            report = evidence["validate_schedule"]["payload"]
            if report.get("valid") is not True or report.get("valid_count", 0) < 1:
                raise _error("invalid_plan", "Independent candidate validation is required")
        return _json({"task": current, "snapshot": source, "tool": tool})

    @staticmethod
    def _compute_origin(task, tool):
        payload = task["payload"]
        return {
            "tool": tool,
            "task_id": task["object_id"],
            "owner": task["owner"],
            "plant_id": task["plant_id"],
            "source_binding": payload["source_binding"],
            "scenario": payload["scenario"],
            "evidence": payload["evidence"],
            "lease_id": payload.get("lease_id"),
            "pending_action": payload.get("pending_action"),
        }

    def _compute_seal(self, origin, payload):
        body = canonical_json({"origin": origin, "payload": payload}).encode("utf-8")
        return hmac.digest(self._compute_key, b"r6-compute-origin-v1\0" + body, "sha256")

    def compute_tool(self, prepared: dict, tool: str) -> dict:
        """Database-free trusted computation; call after leaving the read transaction.

        The returned dict has a private, nonserialized origin seal. Pass it directly
        to issue_evidence; never replace it with provider data or JSON round-trips.
        The engine must fence its lease and persist issue_evidence's result afterward.
        """
        if type(prepared) is not dict or set(prepared) != {"task", "snapshot", "tool"}:
            raise _error("validation", "A prepared compute context is required", 422)
        if type(tool) is not str or tool not in EVIDENCE_TOOLS or prepared["tool"] != tool:
            raise _error("invalid_tool", "Compute tool does not match its context", 422)
        task = prepared["task"]
        self._check_evidence(task)
        source = _model(Snapshot, prepared["snapshot"])
        _exact(canonical_digest(source), task["payload"]["source_binding"]["snapshot_hash"])
        self._fresh(source)
        if tool != "get_snapshot" and missing_inputs(source):
            raise _error("missing_input", "Explicit source inputs are required")
        evidence = task["payload"]["evidence"]
        if tool in evidence or not set(DEPENDENCIES[tool]) <= set(evidence):
            raise _error("tool_order", "Tool dependencies are incomplete")
        result = self._stamp(task, tool)
        if tool == "get_snapshot":
            result.update(
                snapshot=source.model_dump(mode="json"),
                snapshot_hash=canonical_digest(source),
                missing_fields=list(missing_inputs(source)),
            )
        elif tool == "build_scenario":
            from .compiler import build_scenario

            scenario = _model(Scenario, task["payload"]["scenario"])
            changed = build_scenario(source, scenario)
            result.update(
                scenario=scenario.model_dump(mode="json"),
                snapshot=changed.model_dump(mode="json"),
                snapshot_hash=canonical_digest(changed),
            )
        elif tool == "compile_problem":
            from .compiler import compile_problem

            changed = _model(Snapshot, evidence["build_scenario"]["payload"]["snapshot"])
            result["compiled_problem"] = compile_problem(changed).model_dump(mode="json")
        else:
            problem = _model(
                CompiledProblem,
                evidence["compile_problem"]["payload"]["compiled_problem"],
            )
            if tool == "solve_schedule":
                from .solver import solve

                result["baseline"] = solve(
                    problem, mode="feasibility", time_limit_s=5.0
                ).model_dump(mode="json")
                result["optimized"] = solve(problem, mode="optimize", time_limit_s=5.0).model_dump(
                    mode="json"
                )
            else:
                plans = self._plans(evidence)
                if tool == "validate_schedule":
                    from .validator import validate_plan

                    reports = {
                        name: validate_plan(problem, plan).model_dump(mode="json")
                        if plan is not None
                        else None
                        for name, plan in plans.items()
                    }
                    count = sum(
                        report is not None and report["valid"] for report in reports.values()
                    )
                    result.update(reports)
                    result.update(
                        valid=count > 0
                        and all(report is None or report["valid"] for report in reports.values()),
                        valid_count=count,
                    )
                elif tool == "compare_schedules":
                    comparison = self._compare(problem, plans)
                    plan = _model(Plan, comparison["selected_plan"])
                    result.update(
                        comparison=comparison,
                        candidate={
                            "plan_hash": canonical_digest(plan),
                            "plan": plan.model_dump(mode="json"),
                        },
                    )
                else:
                    from .simulation import simulate
                    from .validator import validate_plan

                    if any(
                        not validate_plan(problem, plan).valid
                        for plan in plans.values()
                        if plan is not None
                    ):
                        raise _error("invalid_plan", "Independent candidate validation failed")
                    if not any(plan is not None for plan in plans.values()):
                        raise _error("no_schedule", "No feasible candidate is available")
                    result["simulations"] = {
                        canonical_digest(plan): simulate(problem, plan)
                        for plan in plans.values()
                        if plan is not None
                    }
        payload = _json(result)
        origin = _json(self._compute_origin(task, tool))
        return _ComputedPayload(payload, origin, self._compute_seal(origin, payload))

    def execute_tool(self, conn, principal: Principal, task: dict, tool: str) -> dict:
        """Convenience for short domain tools; CP/simulation MUST use the split API."""
        if type(tool) is not str or tool not in EVIDENCE_TOOLS:
            raise _error("invalid_tool", "Unknown domain evidence tool", 422)
        if tool in {"solve_schedule", "simulate_schedule"}:
            raise _error(
                "compute_outside_transaction", "Solve and simulation require the split compute API"
            )
        return self.compute_tool(self.prepare_tool(conn, principal, task, tool), tool)

    def issue_evidence(
        self,
        conn,
        principal: Principal,
        task: dict,
        tool: str,
        payload: dict,
    ) -> dict:
        """Persist issuance, then return its envelope for the same-transaction task update."""
        self.store._check_conn(conn, write=True)
        prepared = self.prepare_tool(conn, principal, task, tool)
        if type(payload) is not _ComputedPayload:
            raise _error(
                "unissued_evidence", "Only the original trusted compute result may be issued"
            )
        for key, value in self._stamp(prepared["task"], tool).items():
            _exact(payload.get(key), value)
        origin = self._compute_origin(prepared["task"], tool)
        if not _same(payload.origin, origin) or not hmac.compare_digest(
            payload.seal,
            self._compute_seal(origin, payload),
        ):
            raise _error("unissued_evidence", "Trusted compute output or its origin has changed")
        record = self._envelope(tool, payload)
        self.store.record_receipt(
            conn,
            principal,
            record["evidence_id"],
            self._issuance_request(record),
            record,
        )
        return record

    @staticmethod
    def _issuance_request(record):
        return {"action": "domain_evidence_issued", "record": record}

    def _verify_issuance(self, conn, principal, evidence):
        for record in evidence.values():
            try:
                issued = self.store.receipt(
                    conn,
                    principal,
                    record["evidence_id"],
                    self._issuance_request(record),
                )
            except DomainError as exc:
                if exc.code != "idempotency_conflict":
                    raise
                raise _error(
                    "unissued_evidence", "Evidence differs from its issuance receipt"
                ) from None
            if issued is None or not _same(issued, record):
                raise _error("unissued_evidence", "Persisted trusted issuance is required")

    @staticmethod
    def _plans(evidence):
        data = evidence["solve_schedule"]["payload"]
        plans = {}
        for name, mode in (("baseline", "feasibility"), ("optimized", "optimize")):
            result = _model(SolveResult, data.get(name))
            if result.mode != mode or result.status == "ERROR":
                raise _error("solver_error", "Solver did not return a usable result")
            plans[name] = result.plan
        return plans

    @staticmethod
    def _compare(problem, plans):
        from .validator import compare_plans, validate_plan

        present = {name: plan for name, plan in plans.items() if plan is not None}
        if not present:
            raise _error("no_schedule", "No feasible candidate is available")
        if len(present) == 2:
            return compare_plans(problem, plans["baseline"], plans["optimized"])
        name, plan = next(iter(present.items()))
        report = validate_plan(problem, plan)
        if not report.valid:
            raise _error("invalid_plan", "Independent candidate validation failed")
        return {
            "selected": name,
            "selected_plan": plan.model_dump(mode="json"),
            "baseline_metrics": report.objective.model_dump(mode="json")
            if name == "baseline"
            else None,
            "optimized_metrics": report.objective.model_dump(mode="json")
            if name == "optimized"
            else None,
            "improvement": None,
        }

    @staticmethod
    def _check_simulation(problem, plan, result, *, completed=False):
        """Check persisted execution provenance, not a model claim of success.

        This deliberately does not rerun the event engine under the submit/action
        transaction. Issuance is a trusted backend operation; the digest only
        detects content inconsistency and is not an authenticity signature.
        """
        if type(result) is not dict:
            raise _error("invalid_simulation", "Bound synthetic execution evidence is required")
        expected = {
            "synthetic": True,
            "causal_effect_proven": False,
            "source_hash": problem.snapshot_hash,
            "snapshot_hash": problem.snapshot_hash,
            "problem_hash": problem.problem_hash,
            "objective_hash": problem.objective_hash,
            "plan_hash": canonical_digest(plan),
            "disruptions": [],
            "disruptions_hash": canonical_digest([]),
            "validation": {"valid": True, "violation_codes": []},
        }
        if any(not _same(result.get(key), value) for key, value in expected.items()) or (
            result.get("simulation_hash")
            != canonical_digest(
                {key: value for key, value in result.items() if key != "simulation_hash"}
            )
        ):
            raise _error(
                "invalid_simulation", "Execution evidence does not match the frozen problem"
            )
        if not completed:
            return
        metrics = result.get("metrics")
        events = result.get("events")
        states = {operation.operation_id: "completed" for operation in problem.operations}
        required_metrics = {
            "scope": "complete_execution",
            "execution_started": True,
            "completed_operations": len(states),
            "required_operations": len(states),
            "delivered_orders": len(problem.snapshot.orders),
            "required_orders": len(problem.snapshot.orders),
            "objective": plan.objective.model_dump(mode="json"),
        }
        if (
            result.get("status") != "completed"
            or result.get("blocked_reason") is not None
            or not _same(result.get("operation_states"), states)
            or type(metrics) is not dict
            or any(not _same(metrics.get(key), value) for key, value in required_metrics.items())
            or type(events) is not list
            or not events
            or type(events[-1]) is not dict
            or events[-1].get("kind") != "execution_completed"
        ):
            raise _error(
                "invalid_simulation", "Selected candidate needs completed synthetic execution"
            )

    def _submission_facts(self, conn, principal, task, candidate, evidence):
        from .compiler import build_scenario, compile_problem
        from .validator import validate_plan

        payload = task["payload"]
        _exact(candidate, payload.get("candidate"))
        _exact(evidence, payload.get("evidence"))
        self._check_evidence(task, complete=True)
        source = self.verify_source_binding(conn, principal, payload["source_binding"])
        scenario = _model(Scenario, payload["scenario"])
        changed = build_scenario(source, scenario)
        problem = compile_problem(changed)
        # Rebuild deterministic source/scenario/compiler facts, not the solver or
        # simulation. Those execute outside the DB transaction and are persisted
        # only by the trusted fenced engine, never by provider-supplied text.
        expected = {
            "get_snapshot": {
                "snapshot": source.model_dump(mode="json"),
                "snapshot_hash": canonical_digest(source),
                "missing_fields": [],
            },
            "build_scenario": {
                "scenario": scenario.model_dump(mode="json"),
                "snapshot": changed.model_dump(mode="json"),
                "snapshot_hash": canonical_digest(changed),
            },
            "compile_problem": {"compiled_problem": problem.model_dump(mode="json")},
        }
        for tool, facts in expected.items():
            _exact(evidence[tool]["payload"], {**self._stamp(task, tool), **facts})
        plans = self._plans(evidence)
        reports = {
            name: validate_plan(problem, plan).model_dump(mode="json") if plan is not None else None
            for name, plan in plans.items()
        }
        if not any(report is not None for report in reports.values()) or any(
            report is not None and not report["valid"] for report in reports.values()
        ):
            raise _error("invalid_plan", "Independent candidate validation failed")
        validation = {
            **self._stamp(task, "validate_schedule"),
            **reports,
            "valid": True,
            "valid_count": sum(report is not None for report in reports.values()),
        }
        _exact(evidence["validate_schedule"]["payload"], validation)
        comparison = self._compare(problem, plans)
        selected = _model(Plan, comparison["selected_plan"])
        selected_hash = canonical_digest(selected)
        _exact(candidate, {"plan_hash": selected_hash, "plan": selected.model_dump(mode="json")})
        _exact(
            evidence["compare_schedules"]["payload"],
            {
                **self._stamp(task, "compare_schedules"),
                "comparison": comparison,
                "candidate": candidate,
            },
        )
        simulation_payload = evidence["simulate_schedule"]["payload"]
        if set(simulation_payload) != set(self._stamp(task, "simulate_schedule")) | {"simulations"}:
            raise _error()
        simulations = simulation_payload.get("simulations")
        hashes = {canonical_digest(plan) for plan in plans.values() if plan is not None}
        if type(simulations) is not dict or set(simulations) != hashes:
            raise _error("invalid_simulation", "Simulation must cover every validated candidate")
        for plan in plans.values():
            if plan is not None:
                plan_hash = canonical_digest(plan)
                self._check_simulation(
                    problem,
                    plan,
                    simulations[plan_hash],
                    completed=plan_hash == selected_hash,
                )
        simulation = simulations[selected_hash]
        report = validate_plan(problem, selected)
        if not report.valid:
            raise _error("invalid_plan", "Independent selected candidate validation failed")
        self._verify_issuance(conn, principal, evidence)
        return {
            "task_id": task["object_id"],
            "snapshot_id": payload["snapshot_id"],
            "source_binding": payload["source_binding"],
            "scenario": scenario.model_dump(mode="json"),
            "compiled_problem": problem.model_dump(mode="json"),
            "candidate": selected.model_dump(mode="json"),
            "validation": report.model_dump(mode="json"),
            "comparison": comparison,
            "simulation": simulation,
            "evidence": evidence,
        }

    def submit_schedule(
        self,
        conn,
        principal: Principal,
        task: object,
        candidate: dict,
        evidence: dict,
        explanation: str,
    ) -> dict:
        """Only the trusted engine may call this, inside its final fenced transaction."""
        self.store._check_conn(conn, write=True)
        current = self._task(conn, principal, task, running=True)
        if (
            type(explanation) is not str
            or not 1 <= len(explanation) <= 2000
            or not explanation.strip()
        ):
            raise _error("validation", "A bounded explanation is required", 422)
        facts = self._submission_facts(conn, principal, current, candidate, evidence)
        payload = {**facts, "untrusted_model_draft": explanation, "review": None}
        object_id = "plan-" + current["object_id"]
        previous = self._optional_object(conn, principal, "recommendation", object_id)
        if previous is not None:
            if previous["status"] != "pending_review" or not _same(previous["payload"], payload):
                raise _error("conflict", "Task already has a recommendation")
            return previous
        result = self.store.create_object(
            conn,
            kind="recommendation",
            object_id=object_id,
            principal=principal,
            plant_id=current["plant_id"],
            status="pending_review",
            payload=payload,
        )
        self.store.audit(
            conn,
            principal,
            kind="recommendation",
            object_id=object_id,
            action="submit_schedule",
            detail={"task_id": current["object_id"], "plan_hash": candidate["plan_hash"]},
        )
        return result

    def validate_recommendation(self, conn, principal: Principal, recommendation: dict) -> dict:
        """Reauthorize and independently recheck frozen facts for human actions."""
        if type(recommendation) is not dict or type(recommendation.get("object_id")) is not str:
            raise _error()
        current = self.store.get_object(
            "recommendation",
            recommendation["object_id"],
            principal,
            conn=conn,
        )
        _exact(current, recommendation)
        payload = current["payload"]
        task = self.store.get_object("task", payload.get("task_id"), principal, conn=conn)
        self._task(conn, principal, task)
        if (
            current["object_id"] != "plan-" + task["object_id"]
            or current["plant_id"] != task["plant_id"]
        ):
            raise _error()
        facts = self._submission_facts(
            conn,
            principal,
            task,
            task["payload"].get("candidate"),
            payload.get("evidence"),
        )
        for key, value in facts.items():
            _exact(payload.get(key), value)
        return _json(payload)
