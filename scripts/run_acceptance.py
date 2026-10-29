"""Explicit local acceptance: real CP/validation/events, TEST tool selection or local inference.

Every case owns an isolated Store. Reusing a consumed plant epoch/operation ID is
intentionally forbidden by the receiver; this runner never relaxes that boundary.
No prompts, credentials, model configuration or model drafts enter the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

CASES = ("normal", "infeasible", "missing", "insert_order", "machine_outage", "supply_delay")
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class AcceptanceFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def error_code(error):
    """Never serialize arbitrary exception text, paths, prompts or credentials."""
    from scheduling_agent.errors import DomainError

    if isinstance(error, (AcceptanceFailure, DomainError)):
        code = error.code
        if type(code) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code):
            return code
    return "interrupted" if isinstance(error, KeyboardInterrupt) else "internal_error"


def expect_task(task, status, fallback):
    if task["status"] != status:
        code = task["payload"].get("error")
        if type(code) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code):
            raise AcceptanceFailure(code)
        raise AcceptanceFailure(fallback)


def absolute_path(value):
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or path.anchor != "/"
            or any(ord(char) < 32 or ord(char) == 127 for char in str(value))):
        raise argparse.ArgumentTypeError("An absolute local path without traversal is required")
    return path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=absolute_path,
                        help="NEW private output directory outside source; parent must exist")
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--test-provider", action="store_true",
                          help="TEST ONLY tool selector; no model inference")
    provider.add_argument("--runtime-config", type=absolute_path,
                          help="Explicit local runtime configuration; never discovered")
    parser.add_argument("--case", action="append", choices=CASES, dest="cases")
    args = parser.parse_args(argv)
    if args.cases and len(args.cases) != len(set(args.cases)):
        parser.error("Each case may be selected only once")
    args.cases = args.cases or (list(CASES) if args.test_provider else ["normal"])
    return args


@contextmanager
def directory_fd(path):
    """Open existing ancestors without resolving or following a single symlink."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def new_report(root):
    """Reserve a new output and report exclusively before any provider is created."""
    require(root.is_absolute() and ".." not in root.parts and root.anchor == "/",
            "output_path_invalid")
    require(not root.is_relative_to(SOURCE_ROOT), "output_inside_source")
    try:
        with directory_fd(root.parent) as parent:
            # No exist_ok: directories, regular files and dangling links are all refused.
            os.mkdir(root.name, mode=0o700, dir_fd=parent)
            fd = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                report_fd = os.open("acceptance.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                    | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    info = os.fstat(report_fd)
                    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                            "output_report_invalid")
                    with os.fdopen(report_fd, "w", encoding="utf-8") as stream:
                        report_fd = None
                        yield stream
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(fd)
                finally:
                    if report_fd is not None:
                        os.close(report_fd)
            finally:
                os.close(fd)
    except OSError:
        raise AcceptanceFailure("output_unavailable") from None


def make_provider(args):
    if args.test_provider:
        from scheduling_agent.provider import SyntheticProvider

        return SyntheticProvider()
    from scheduling_agent.managed_provider import ManagedLocalProvider

    return ManagedLocalProvider(args.runtime_config)


def close_provider(provider, report):
    try:
        provider.close()
        report["provider_close"] = {"status": "closed"}
    except (Exception, KeyboardInterrupt) as error:
        report["provider_close"] = {"status": "failed", "error_code": error_code(error)}
        report["status"] = "failed"


def calls_summary(store, principal, task_id):
    """Read the durable journal, not provider guesses or success-only token counts."""
    with store.connect() as conn:
        store.require(principal, conn=conn)
        rows = [dict(row) for row in conn.execute(
            "SELECT call_id,status,tokens,elapsed_s,response_hash,error_code FROM calls "
            "WHERE owner=? AND task_id=? ORDER BY created_at_ms,rowid",
            (principal.user_id, task_id),
        )]
    return {
        "count": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
        "known_tokens": sum(row["tokens"] for row in rows if row["tokens"] is not None),
        "unknown_token_calls": sum(row["tokens"] is None for row in rows),
        "calls": rows,
    }


def graph_summary(store, principal, task):
    from scheduling_agent.domain import canonical_digest
    from scheduling_agent.provider import TOOLS
    from scheduling_agent.service import DEPENDENCIES

    sequence = [event["detail"]["tool"] for event in reversed(store.audit_events(principal))
                if event["object_id"] == task["object_id"] and event["action"] == "tool_completed"]
    observed = set()
    for tool in sequence:
        if tool == "get_snapshot":
            # Supplement invalidates evidence, not call history; a fresh read is explicit.
            observed = set()
        if tool in DEPENDENCIES:
            require(tool not in observed and set(DEPENDENCIES[tool]) <= observed,
                    "graph_tool_order_invalid")
            observed.add(tool)
        elif tool == "request_human_input":
            require(observed == {"get_snapshot"}, "graph_input_order_invalid")
        elif tool == "submit_schedule":
            require(observed == set(DEPENDENCIES), "graph_submit_order_invalid")
        else:
            raise AcceptanceFailure("graph_unknown_tool")
    handled = task["payload"]["handled_call_ids"]
    require(len(sequence) == task["payload"]["tools"] == len(handled), "graph_tool_count_mismatch")
    calls = {row["call_id"]: row for row in calls_summary(store, principal, task["object_id"])["calls"]}
    for tool, call_id in zip(sequence, handled, strict=True):
        call = calls[call_id]
        require(call["status"] == "completed" and call["response_hash"] is not None,
                "graph_call_not_completed")
        reply = store.get_artifact(call["response_hash"])
        require(canonical_digest(reply) == call["response_hash"] and tool in TOOLS
                and reply["action"]["tool"] == tool, "graph_call_tool_mismatch")
    return sequence


def run_case(root, name, provider, case):
    from scheduling_agent.agent import AgentEngine
    from scheduling_agent.domain import Snapshot, canonical_digest, missing_inputs
    from scheduling_agent.fixtures import demo_scenarios, demo_snapshots
    from scheduling_agent.service import SchedulingService
    from scheduling_agent.storage import Store

    now = time.time_ns() // 1_000_000
    fixtures = demo_snapshots(now)
    source_snapshot = fixtures[name if name in fixtures else "normal"]
    scenario = demo_scenarios(fixtures["normal"])[name if name in CASES[3:] else "baseline"]
    store = Store(root)
    principal = store.create_user("acceptance-admin", secrets.token_urlsafe(32), "admin",
                                  ["synthetic-plant"])
    service = SchedulingService(store)
    engine = AgentEngine(service, provider)
    source = service.import_snapshot(principal, source_snapshot, action_id="import")
    # Use the returned owner-scoped object ID, never the fixture's natural snapshot ID.
    task = engine.create(principal, source["object_id"], scenario=scenario, action_id="create")
    task_id = task["object_id"]
    case.update(task_id=task_id, source_object_id=source["object_id"],
                scenario_kind=scenario.kind, scenario_hash=canonical_digest(scenario),
                observed_at_ms=now)
    try:
        task = engine.run(principal, task_id)
        case["initial_task_status"] = task["status"]
        if name == "missing":
            expect_task(task, "awaiting_input", "missing_not_awaiting_input")
            required = list(missing_inputs(source_snapshot))
            require(sorted(task["payload"]["awaiting_fields"]) == sorted(required),
                    "missing_requested_fields_mismatch")
            expected = {"materials[0].initial_stock": 0, "orders[0].due_min": 8}
            require(set(required) == set(expected), "missing_fixture_paths_changed")
            require(fixtures["normal"].materials[0].initial_stock == 0
                    and fixtures["normal"].orders[0].due_min == 8, "missing_fixture_values_changed")
            before = calls_summary(store, principal, task_id)
            answers = [{"field": field, "value": expected[field]} for field in required]
            resumed = engine.resume(principal, task_id, action_id="supplement", answers=answers,
                                    note="Explicit original synthetic fixture values, not inferred")
            require(calls_summary(store, principal, task_id) == before, "resume_changed_call_journal")
            updated = service.get_snapshot(principal, source["object_id"])
            expected_snapshot = source_snapshot.model_dump(mode="json")
            expected_snapshot["revision"] += 1
            expected_snapshot["materials"][0]["initial_stock"] = 0
            expected_snapshot["orders"][0]["due_min"] = 8
            require(updated["payload"] == expected_snapshot
                    and updated["version"] == source["version"] + 1, "supplement_source_mismatch")
            require(not missing_inputs(Snapshot.model_validate(updated["payload"])),
                    "supplement_still_missing")
            case["supplement"] = {"answers": answers, "before_revision": source_snapshot.revision,
                                  "after_revision": updated["payload"]["revision"],
                                  "observed_time_unchanged": True, "prior_calls_retained": True,
                                  "calls_before_resume": before["count"],
                                  "known_tokens_before_resume": before["known_tokens"],
                                  "resumed_status": resumed["status"]}
            task = engine.run(principal, task_id)
            after = calls_summary(store, principal, task_id)
            require(after["calls"][:before["count"]] == before["calls"], "resume_lost_prior_calls")
        case["task_status"] = task["status"]
        expect_task(task, "no_schedule" if name == "infeasible" else "submitted",
                    "infeasible_wrong_task_status" if name == "infeasible" else "task_not_submitted")
        with store.connect() as conn:
            binding = service.source_binding(conn, principal, source["object_id"])
        case["source_binding"] = binding
        require(task["payload"]["snapshot_id"] == source["object_id"]
                and task["payload"]["source_binding"] == binding, "task_source_mismatch")
        require(task["payload"]["scenario"] == scenario.model_dump(mode="json"),
                "task_scenario_mismatch")
        check_evidence(task, binding, case)
        before_terminal = calls_summary(store, principal, task_id)
        again = engine.run(principal, task_id)
        require(again == task and calls_summary(store, principal, task_id) == before_terminal,
                "terminal_rerun_changed_task_or_calls")
        case["terminal_rerun_no_model_call"] = True
        recommendations = store.list_objects("recommendation", principal)
        if name == "infeasible":
            require(task["status"] == "no_schedule", "infeasible_wrong_task_status")
            require(all(item["status"] == "INFEASIBLE" for item in case["cp_sat"].values()),
                    "infeasible_not_solver_proven")
            require(not recommendations and not store.list_objects("publication", principal),
                    "infeasible_created_publication")
            case.update(recommendations=0, publications=0)
        else:
            require(task["status"] == "submitted", "task_not_submitted")
            require(len(recommendations) == 1 and recommendations[0]["status"] == "pending_review",
                    "recommendation_not_pending_review")
            publication_flow(service, principal, task, recommendations[0], case,
                             crash=provider.is_test_provider and name == "normal")
            require(calls_summary(store, principal, task_id) == before_terminal,
                    "human_actions_called_model")
            case["human_actions_no_model_call"] = True
    finally:
        case["model_calls"] = calls_summary(store, principal, task_id)
        current = engine.get(principal, task_id)
        case["task_status"] = current["status"]
        if current["payload"].get("error") and current["status"] != "no_schedule":
            case["task_error_code"] = error_code(AcceptanceFailure(current["payload"]["error"]))
        case["graph_tool_order"] = graph_summary(store, principal, current)
        require(case["model_calls"]["count"] == current["payload"]["calls"]
                and case["model_calls"]["known_tokens"] == current["payload"]["tokens"],
                "call_accounting_mismatch")
        case["no_schedule_reason"] = current["payload"].get("no_schedule_reason")


def publication_flow(service, principal, task, recommendation, case, *, crash):
    from scheduling_agent.actions import ActionService
    from scheduling_agent.domain import canonical_digest
    from scheduling_agent.errors import DomainError

    actions, store = ActionService(service), service.store
    problem, candidate = check_candidate(task, recommendation, case)
    states = case["action_states"] = []

    def record(action, obj):
        payload = obj["payload"]
        item = {"action": action, "status": obj["status"], "content_hash": obj["content_hash"]}
        for key in ("delivery_status", "receiver_status", "execution_status", "receiver_sequence"):
            if key in payload:
                item[key] = payload[key]
        states.append(item)
        return obj

    record("pending_review", recommendation)
    approved = record("review", actions.review(
        principal, recommendation["object_id"], action_id="approve",
        recommendation_hash=recommendation["content_hash"], decision="approved",
        note="Explicit local acceptance review of independently validated synthetic evidence"))
    require(approved["status"] == "approved", "review_not_approved")
    publication = record("request_publication", actions.request_publication(
        principal, approved["object_id"], action_id="publish",
        recommendation_hash=approved["content_hash"], confirmed=True))
    publication_id = publication["object_id"]
    case["publication_id"] = publication_id
    require(publication["status"] == "requested"
            and publication["payload"]["delivery_status"] == "not_sent", "publication_not_requested")
    require(publication["payload"]["source_binding"] == case["source_binding"]
            and publication["payload"]["plan_hash"] == canonical_digest(candidate),
            "publication_source_mismatch")
    delivered = record("deliver_local_after_receive" if crash else "deliver_local", actions.deliver_local(
        principal, publication_id, action_id="deliver", fault="after_receive" if crash else None))
    if crash:
        require(delivered["status"] == "unknown"
                and delivered["payload"]["delivery_status"] == "unknown", "lost_ack_not_unknown")
        persisted = store.get_object("publication", publication_id, principal)
        require(persisted == delivered and persisted["payload"]["receiver_receipt"] is None,
                "unknown_not_persisted")
        case["lost_ack"] = {"unknown_persisted": True, "automatic_redelivery": False}
        # A fresh service/receiver reads durable authority, never the old publication ACK.
        actions = ActionService(service)
        delivered = record("readback_after_lost_ack", actions.readback(
            principal, publication_id, action_id="readback-lost-ack"))
    require(delivered["status"] == "partial"
            and delivered["payload"]["receiver_status"] == "partial", "delivery_not_partial")
    require(delivered["payload"]["execution_status"] == "none", "partial_already_executed")
    try:
        actions.execute_synthetic(principal, publication_id, action_id="execute-while-partial",
                                  confirmed=True)
    except DomainError as error:
        require(error.code == "receiver_state", "partial_wrong_refusal")
        case["partial_execute_refused"] = {"error_code": error.code,
                                           "action_id": "execute-while-partial"}
    else:
        raise AcceptanceFailure("partial_execution_accepted")
    require(store.get_object("publication", publication_id, principal) == delivered,
            "partial_refusal_changed_publication")
    accepted = record("advance_receiver", actions.advance_receiver(
        principal, publication_id, action_id="advance"))
    require(accepted["status"] == "accepted", "receiver_not_accepted")
    actions = ActionService(service)
    accepted = record("readback_accepted", actions.readback(
        principal, publication_id, action_id="readback-accepted"))
    required_ids = sorted(operation.operation_id for operation in problem.operations)
    require(accepted["payload"]["accepted_operation_ids"] == required_ids
            and accepted["payload"]["receiver_status"] == "accepted"
            and accepted["payload"]["execution_status"] == "none", "accepted_readback_incomplete")
    completed = record("execute_synthetic", actions.execute_synthetic(
        principal, publication_id, action_id="execute-confirmed", confirmed=True))
    require(completed["status"] == "completed", "execution_not_completed")
    actions = ActionService(service)
    final = record("readback_completed", actions.readback(
        principal, publication_id, action_id="readback-completed"))
    require(final["status"] == "completed"
            and final["payload"]["execution_status"] == "completed", "completed_readback_failed")
    result = final["payload"]["execution_result"]
    check_simulation(result, problem, candidate)
    case["execution"] = simulation_summary(result)
    case["receiver_receipt_hash"] = canonical_digest(final["payload"]["receiver_receipt"])
    case["publication_status"] = final["status"]


def check_evidence(task, source_binding, case):
    from scheduling_agent.domain import canonical_digest
    from scheduling_agent.service import DEPENDENCIES

    evidence = task["payload"]["evidence"]
    summaries = {}
    for tool, record in evidence.items():
        payload = record["payload"]
        require(record["tool"] == tool and record["hash"] == canonical_digest(payload),
                "evidence_hash_mismatch")
        require(record["evidence_id"] == "ev-" + canonical_digest({"tool": tool, "payload": payload}),
                "evidence_id_mismatch")
        require(payload["source_binding"] == source_binding
                and payload["task_id"] == task["object_id"], "evidence_source_mismatch")
        require(payload["scenario_hash"] == canonical_digest(task["payload"]["scenario"]),
                "evidence_scenario_mismatch")
        require(payload["dependencies"] == {name: evidence[name]["hash"]
                                              for name in DEPENDENCIES[tool]},
                "evidence_dependencies_mismatch")
        summaries[tool] = {"evidence_id": record["evidence_id"], "hash": record["hash"],
                           "dependencies": payload["dependencies"]}
    case["tool_evidence"] = summaries
    solve = evidence.get("solve_schedule", {}).get("payload")
    if solve:
        case["cp_sat"] = {name: {key: solve[name][key] for key in
                                ("status", "mode", "best_bound", "wall_time_s", "solver_version")}
                          for name in ("baseline", "optimized")}


def check_candidate(task, recommendation, case):
    from scheduling_agent.domain import CompiledProblem, Plan, canonical_digest
    from scheduling_agent.validator import compare_plans, validate_plan

    facts = recommendation["payload"]
    problem = CompiledProblem.model_validate(facts["compiled_problem"])
    candidate = Plan.model_validate(facts["candidate"])
    plan_hash = canonical_digest(candidate)
    require(task["payload"]["candidate"] == {"plan_hash": plan_hash,
                                             "plan": candidate.model_dump(mode="json")},
            "candidate_hash_mismatch")
    independent = validate_plan(problem, candidate)
    require(independent.valid and independent.plan_hash == plan_hash, "independent_invalid")
    require(independent.model_dump(mode="json") == facts["validation"],
            "independent_report_mismatch")
    require(facts["source_binding"] == task["payload"]["source_binding"]
            and facts["scenario"] == task["payload"]["scenario"], "recommendation_source_mismatch")
    solve = task["payload"]["evidence"]["solve_schedule"]["payload"]
    plans = {name: Plan.model_validate(solve[name]["plan"])
             for name in ("baseline", "optimized") if solve[name]["plan"] is not None}
    objectives = {}
    for name, plan in plans.items():
        report = validate_plan(problem, plan)
        require(report.valid, "comparison_candidate_invalid")
        objectives[name] = report.objective.model_dump(mode="json")
    comparison = facts["comparison"]
    require(comparison["selected"] in plans, "comparison_selection_invalid")
    require(comparison["selected_plan"] == candidate.model_dump(mode="json")
            and candidate == plans[comparison["selected"]], "comparison_selection_mismatch")
    require(independent.objective.total == min(item["total"] for item in objectives.values()),
            "comparison_not_minimum_actual_objective")
    if len(plans) == 2:
        require(comparison == compare_plans(problem, plans["baseline"], plans["optimized"]),
                "comparison_independent_mismatch")
    simulation = facts["simulation"]
    check_simulation(simulation, problem, candidate)
    case.update(
        candidate_hash=plan_hash,
        independent_validation={"valid": True, "hash": canonical_digest(independent),
                                "violation_count": len(independent.violations),
                                "objective": independent.objective.model_dump(mode="json"),
                                "inventory_points": len(independent.inventory),
                                "resource_activities": len(independent.activities)},
        comparison={"selected": comparison["selected"], "actual_objectives": objectives,
                    "improvement": comparison["improvement"]},
        simulation=simulation_summary(simulation),
    )
    return problem, candidate


def check_simulation(result, problem, candidate):
    from scheduling_agent.domain import canonical_digest

    require(result["status"] == "completed" and result["synthetic"] is True
            and result["causal_effect_proven"] is False, "simulation_not_completed_synthetic")
    require(result["plan_hash"] == canonical_digest(candidate)
            and result["problem_hash"] == problem.problem_hash
            and result["snapshot_hash"] == problem.snapshot_hash
            and result["source_hash"] == problem.snapshot_hash
            and result["objective_hash"] == problem.objective_hash, "simulation_binding_mismatch")
    require(result["simulation_hash"] == canonical_digest({key: value for key, value in
            result.items() if key != "simulation_hash"}), "simulation_hash_mismatch")
    require(result["metrics"]["completed_operations"] == len(problem.operations)
            and result["metrics"]["objective"] == candidate.objective.model_dump(mode="json"),
            "simulation_incomplete")


def simulation_summary(result):
    from scheduling_agent.domain import canonical_digest

    return {"status": result["status"], "synthetic": result["synthetic"],
            "causal_effect_proven": result["causal_effect_proven"],
            "simulation_hash": result["simulation_hash"],
            "event_count": len(result["events"]), "events_hash": canonical_digest(result["events"]),
            "metrics": result["metrics"]}


def check_api_contract(root):
    """Construct a separate local app only; no server, HTTP or model call is started."""
    from pydantic import ValidationError

    from scheduling_agent.api import create_app
    from scheduling_agent.provider import SyntheticProvider
    from scheduling_agent.schemas import ActionRequest

    provider = SyntheticProvider()
    result = {"status": "failed"}
    try:
        app = create_app(root, provider)
        schema = app.openapi()
        properties = schema["components"]["schemas"]
        require(all("fault" not in item.get("properties", {}) for item in properties.values()),
                "api_exposes_fault")
        route = schema["paths"]["/api/publications/{publication_id}/deliver-local"]["post"]
        require(all(item["name"] != "fault" for item in route.get("parameters", [])),
                "api_exposes_fault")
        body = route["requestBody"]["content"]["application/json"]["schema"]
        require(body == {"$ref": "#/components/schemas/ActionRequest"}, "api_delivery_contract")
        try:
            ActionRequest.model_validate({"action_id": "probe", "fault": "after_receive"})
        except ValidationError:
            pass
        else:
            raise AcceptanceFailure("api_accepts_fault")
        result.update(status="passed", fault_exposed=False, fault_body_rejected=True,
                      services_started=False, model_calls=0)
    except (Exception, KeyboardInterrupt) as error:
        result["error_code"] = error_code(error)
    finally:
        close_provider(provider, result)
    return result


def main(argv=None):
    args = parse_args(argv)
    report = {
        "schema_version": 1, "status": "failed", "synthetic_only": True,
        "causal_effect_proven": False, "production_execution": False,
        "mode": "TEST-tool-selector" if args.test_provider else "managed-local-inference",
        "model_inference": not args.test_provider,
        "case_isolation": "independent-store-per-case; original operation IDs preserved",
        "cases": [], "provider_close": {"status": "not_created"},
    }
    try:
        with new_report(args.data_root) as stream:
            provider = None
            started = time.monotonic()
            try:
                provider = make_provider(args)
                report["provider_fingerprint_sha256"] = hashlib.sha256(
                    provider.fingerprint.encode("utf-8")).hexdigest()
                for name in args.cases:
                    case = {"case": name, "status": "failed"}
                    report["cases"].append(case)
                    try:
                        run_case(args.data_root / name, name, provider, case)
                        case["status"] = "passed"
                    except (Exception, KeyboardInterrupt) as error:
                        case["error_code"] = error_code(error)
                        if isinstance(error, KeyboardInterrupt):
                            raise
                if args.test_provider:
                    report["api_contract"] = check_api_contract(args.data_root / "api-contract")
                report["status"] = ("passed" if all(case["status"] == "passed"
                                                    for case in report["cases"])
                                    and report.get("api_contract", {}).get("status", "passed") == "passed"
                                    else "failed")
            except (Exception, KeyboardInterrupt) as error:
                report["error_code"] = error_code(error)
                report["status"] = "failed"
            finally:
                if provider is not None:
                    close_provider(provider, report)
                report["elapsed_s"] = round(time.monotonic() - started, 3)
                json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
                stream.write("\n")
    except (Exception, KeyboardInterrupt) as error:
        # Unsafe/existing output roots are never modified, even to emit a failure report.
        print(json.dumps({"status": "failed", "error_code": error_code(error)}))
        return 1
    print(json.dumps({"status": report["status"], "cases": len(report["cases"])}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
