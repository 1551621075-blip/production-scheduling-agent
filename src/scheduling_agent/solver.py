"""Real single-worker CP-SAT search, not a plan validator.

Each machine has one optional-node circuit. Its selected incoming arcs create
only the *adjacent* setup, contiguous with processing. Inventory uses reservoir
ticks 2*t (all receipts) and 2*t+1 (all issues), including initial stock at tick 0.
All feasibility rules remain hard constraints, including during optimization.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict

import ortools
from ortools.sat.python import cp_model

from .domain import (
    MAX_OBJECTIVE,
    CompiledProblem,
    ObjectiveBreakdown,
    Plan,
    Slot,
    SolveResult,
    SolverStatus,
    canonical_digest,
)
from .errors import DomainError


class _Formulation:
    """Private CP variables only; all business records use the shared domain."""

    def __init__(self, problem: CompiledProblem):
        self.problem = problem
        self.snapshot = problem.snapshot
        self.horizon = self.snapshot.horizon_min
        self.model = cp_model.CpModel()
        self.operations = problem.operations_by_id
        self.resources = {r.resource_id: r for r in self.snapshot.resources}
        self.machines = {m.machine_id: m for m in self.snapshot.machines}
        self.setup_start = {}
        self.start = {}
        self.end = {}
        self.selected = {}
        self.incoming = {}
        self.machine_intervals = defaultdict(list)
        self.resource_intervals = defaultdict(list)
        self.resource_demands = defaultdict(list)
        self.machine_load_terms = defaultdict(list)
        self.setup_terms = []
        for operation in problem.operations:
            key = operation.operation_id
            self.setup_start[key] = self.model.new_int_var(0, self.horizon, f"ss:{key}")
            self.start[key] = self.model.new_int_var(0, self.horizon, f"s:{key}")
            self.end[key] = self.model.new_int_var(0, self.horizon, f"e:{key}")
            self.model.add(self.setup_start[key] <= self.start[key])
            self.model.add(self.end[key] >= self.start[key] + 1)
        self._processing()
        self._sequences()
        self._route_and_frozen()
        self._inventory()
        self._objective()
        for intervals in self.machine_intervals.values():
            self.model.add_no_overlap(intervals)
        for resource_id, intervals in self.resource_intervals.items():
            self.model.add_cumulative(
                intervals, self.resource_demands[resource_id], self.resources[resource_id].capacity
            )

    def _calendar(self, start, end, duration, present, windows, name):
        """A positive interval belongs wholly to one open window, never a gap."""
        choices = []
        ranges = []
        for window in sorted(windows, key=lambda value: value.start_min):
            if ranges and ranges[-1][1] == window.start_min:
                ranges[-1] = (ranges[-1][0], window.end_min)
            else:
                ranges.append((window.start_min, window.end_min))
        for index, (lower, upper) in enumerate(ranges):
            if upper - lower < duration:
                continue
            choice = self.model.new_bool_var(f"window:{name}:{index}")
            self.model.add(start >= lower).only_enforce_if(choice)
            self.model.add(end <= upper).only_enforce_if(choice)
            choices.append(choice)
        self.model.add(sum(choices) == present)

    def _resources_for(self, interval, start, end, duration, present, demands, family, name):
        for demand in demands:
            resource = self.resources[demand.resource_id]
            if family not in resource.qualified_families:
                self.model.add(present == 0)
            # A zero-length setup occupies nothing, but its qualifications still apply.
            if duration == 0:
                continue
            if demand.quantity > resource.capacity:
                self.model.add(present == 0)
            self.resource_intervals[resource.resource_id].append(interval)
            self.resource_demands[resource.resource_id].append(demand.quantity)
            self._calendar(
                start, end, duration, present, resource.calendar, f"{name}:{resource.resource_id}"
            )

    def _processing(self):
        for operation in self.problem.operations:
            key = operation.operation_id
            choices = []
            for alternative in operation.alternatives:
                machine = self.machines[alternative.machine_id]
                present = self.model.new_bool_var(f"selected:{key}:{machine.machine_id}")
                self.selected[key, machine.machine_id] = present
                choices.append(present)
                duration = alternative.duration_min
                if operation.residual is not None:
                    duration = operation.residual.remaining_min
                    self.model.add(present == int(machine.machine_id == operation.residual.machine_id))
                if not alternative.qualified or operation.family not in machine.qualified_families:
                    self.model.add(present == 0)
                interval = self.model.new_optional_interval_var(
                    self.start[key], duration, self.end[key], present,
                    f"processing:{key}:{machine.machine_id}",
                )
                self.machine_intervals[machine.machine_id].append(interval)
                self.machine_load_terms[machine.machine_id].append(duration * present)
                self._calendar(
                    self.start[key], self.end[key], duration, present, machine.calendar,
                    f"processing:{key}:{machine.machine_id}",
                )
                self._resources_for(
                    interval, self.start[key], self.end[key], duration, present,
                    alternative.resources, operation.family, f"processing:{key}:{machine.machine_id}",
                )
            # An explicitly empty or entirely unqualified alternative set is infeasible.
            self.model.add(sum(choices) == 1)

    def _incoming_setup(self, machine, previous, operation, arc):
        key = operation.operation_id
        predecessor_id = None if previous is None else previous.operation_id
        self.incoming[machine.machine_id, predecessor_id, key] = arc
        if operation.residual is not None:
            if previous is not None:
                self.model.add(arc == 0)
            else:
                self.model.add(self.setup_start[key] == 0).only_enforce_if(arc)
                self.model.add(self.start[key] == 0).only_enforce_if(arc)
            return
        source = machine.initial_family if previous is None else previous.family
        rule = next(
            rule for rule in machine.setups
            if (rule.from_family, rule.to_family) == (source, operation.family)
        )
        if previous is not None:
            self.model.add(self.end[predecessor_id] <= self.setup_start[key]).only_enforce_if(arc)
        self.model.add(self.start[key] == self.setup_start[key] + rule.duration_min).only_enforce_if(arc)
        self.setup_terms.append(rule.duration_min * arc)
        self.machine_load_terms[machine.machine_id].append(rule.duration_min * arc)
        interval = None
        if rule.duration_min:
            interval = self.model.new_optional_interval_var(
                self.setup_start[key], rule.duration_min, self.start[key], arc,
                f"setup:{machine.machine_id}:{predecessor_id}:{key}",
            )
            self.machine_intervals[machine.machine_id].append(interval)
            self._calendar(
                self.setup_start[key], self.start[key], rule.duration_min, arc, machine.calendar,
                f"setup:{machine.machine_id}:{predecessor_id}:{key}",
            )
        self._resources_for(
            interval, self.setup_start[key], self.start[key], rule.duration_min, arc,
            rule.resources, operation.family, f"setup:{machine.machine_id}:{predecessor_id}:{key}",
        )

    def _sequences(self):
        for machine in self.snapshot.machines:
            operations = [
                op for op in self.problem.operations
                if (op.operation_id, machine.machine_id) in self.selected
            ]
            if not operations:
                continue
            arcs = [(0, 0, self.model.new_bool_var(f"empty:{machine.machine_id}"))]
            for index, operation in enumerate(operations, 1):
                selected = self.selected[operation.operation_id, machine.machine_id]
                first = self.model.new_bool_var(f"first:{machine.machine_id}:{operation.operation_id}")
                last = self.model.new_bool_var(f"last:{machine.machine_id}:{operation.operation_id}")
                arcs.extend(((index, index, selected.negated()), (0, index, first), (index, 0, last)))
                self._incoming_setup(machine, None, operation, first)
                for before, previous in enumerate(operations, 1):
                    if before == index:
                        continue
                    arc = self.model.new_bool_var(
                        f"arc:{machine.machine_id}:{previous.operation_id}:{operation.operation_id}"
                    )
                    arcs.append((before, index, arc))
                    self._incoming_setup(machine, previous, operation, arc)
            self.model.add_circuit(arcs)

    def _route_and_frozen(self):
        for operation in self.problem.operations:
            key = operation.operation_id
            if operation.predecessor_id is None:
                self.model.add(self.setup_start[key] >= operation.release_min)
            else:
                self.model.add(self.end[operation.predecessor_id] <= self.setup_start[key])
        for commitment in self.snapshot.commitments:
            operation = self.operations[commitment.operation_id]
            if commitment.setup_start_min >= self.snapshot.freeze_until_min and operation.residual is None:
                continue
            key = commitment.operation_id
            self.model.add(self.selected[key, commitment.machine_id] == 1)
            self.model.add(self.setup_start[key] == commitment.setup_start_min)
            self.model.add(self.start[key] == commitment.start_min)
            self.model.add(self.end[key] == commitment.end_min)
            arc = self.incoming.get((commitment.machine_id, commitment.predecessor_id, key))
            self.model.add(False if arc is None else arc == 1)

    def _inventory(self):
        times = defaultdict(list)
        amounts = defaultdict(list)

        def event(item_id, at, quantity):
            if quantity:
                times[item_id].append(at)
                amounts[item_id].append(quantity)

        for material in self.snapshot.materials:
            if material.initial_stock is None:
                raise ValueError("compiled problem has missing initial inventory")
            event(material.item_id, 0, material.initial_stock)
        for supply in self.snapshot.supplies:
            event(supply.item_id, 2 * supply.at_min, supply.quantity)
        terminal = {}
        for operation in self.problem.operations:
            for material in operation.consumes:
                event(material.item_id, 2 * self.start[operation.operation_id] + 1, -material.quantity)
            if operation.is_terminal:
                terminal[operation.lot_id] = self.end[operation.operation_id]
                event(operation.item_id, 2 * self.end[operation.operation_id], operation.quantity)
        for order in self.snapshot.orders:
            event(order.item_id, 2 * terminal[order.lot_id] + 1, -order.quantity)
        for item_id, changes in amounts.items():
            # Total positive flow is an exact finite upper bound, not a capacity restriction.
            upper = sum(change for change in changes if change > 0)
            self.model.add_reservoir_constraint(times[item_id], changes, 0, upper)

    def _objective(self):
        model = self.model
        h = self.horizon
        terminal = {
            op.lot_id: self.end[op.operation_id] for op in self.problem.operations if op.is_terminal
        }
        tardiness_terms = []
        for order in self.snapshot.orders:
            if order.due_min is None:
                raise ValueError("compiled problem has missing due date")
            tardiness = model.new_int_var(0, h, f"tardiness:{order.order_id}")
            model.add_max_equality(tardiness, [0, terminal[order.lot_id] - order.due_min])
            tardiness_terms.append(order.priority * tardiness)
        self.weighted_tardiness = model.new_int_var(
            0, h * sum(order.priority for order in self.snapshot.orders), "weighted_tardiness"
        )
        model.add(self.weighted_tardiness == sum(tardiness_terms))
        self.setup_minutes = model.new_int_var(0, len(self.problem.operations) * h, "setup_minutes")
        model.add(self.setup_minutes == sum(self.setup_terms))
        self.makespan = model.new_int_var(0, h, "makespan")
        model.add_max_equality(self.makespan, [*self.end.values(), 0])
        loads = []
        for machine in self.snapshot.machines:
            load = model.new_int_var(0, h, f"load:{machine.machine_id}")
            model.add(load == sum(self.machine_load_terms[machine.machine_id]))
            loads.append(load)
        maximum = model.new_int_var(0, h, "load_max")
        minimum = model.new_int_var(0, h, "load_min")
        model.add_max_equality(maximum, loads)
        model.add_min_equality(minimum, loads)
        self.load_spread = model.new_int_var(0, h, "load_spread")
        model.add(self.load_spread == maximum - minimum)
        objective = self.snapshot.objective
        self.total = model.new_int_var(0, MAX_OBJECTIVE, "objective_total")
        model.add(self.total == (
            objective.weighted_tardiness * self.weighted_tardiness
            + objective.setup_minutes * self.setup_minutes
            + objective.makespan * self.makespan
            + objective.load_spread * self.load_spread
        ))

    def slots(self, solver):
        slots = []
        for key in sorted(self.operations):
            machine_id = next(
                alternative.machine_id for alternative in self.operations[key].alternatives
                if solver.boolean_value(self.selected[key, alternative.machine_id])
            )
            predecessor_id = next(
                previous for (machine, previous, operation), arc in self.incoming.items()
                if machine == machine_id and operation == key and solver.boolean_value(arc)
            )
            slots.append(Slot(
                operation_id=key, machine_id=machine_id, setup_start_min=solver.value(self.setup_start[key]),
                start_min=solver.value(self.start[key]), end_min=solver.value(self.end[key]),
                predecessor_id=predecessor_id,
            ))
        return tuple(slots)

    def objective(self, solver):
        # Read integer variable values, never round the solver's floating objective.
        return ObjectiveBreakdown(**{
            name: solver.value(getattr(self, name))
            for name in ("weighted_tardiness", "setup_minutes", "makespan", "load_spread", "total")
        })


def solve(
    problem: CompiledProblem, *, mode="optimize", time_limit_s=5.0, seed=41723
) -> SolveResult:
    """Run actual CP-SAT; returned candidates still require independent validation.

    A zero-second limit is passed through to CP-SAT and can return UNKNOWN.
    Satisfaction search reports FEASIBLE even when CP-SAT calls it OPTIMAL.
    Invalid invocation raises DomainError; formulation/engine failures are ERROR.
    """
    if (
        mode not in ("feasibility", "optimize")
        or isinstance(time_limit_s, bool)
        or not isinstance(time_limit_s, (int, float))
        or not 0 <= time_limit_s <= 300
        or not math.isfinite(time_limit_s)
        or type(seed) is not int
        or not 0 <= seed <= 2**31 - 1
    ):
        raise DomainError(
            "invalid_solver_parameters",
            "mode must be feasibility/optimize, time_limit_s finite in [0,300], seed int in [0,2**31-1]",
        )
    if not isinstance(problem, CompiledProblem):
        raise DomainError("invalid_problem", "problem must use the strict compiled domain schema")
    started = time.perf_counter()
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.random_seed = seed
    solver.parameters.log_search_progress = False
    solver.parameters.log_to_stdout = False

    def result(status, plan=None, best_bound=None, error=None):
        return SolveResult(
            status=status, mode=mode, plan=plan, best_bound=best_bound,
            wall_time_s=float(time.perf_counter() - started),
            solver_version=f"OR-Tools {ortools.__version__}", error=error,
        )

    try:
        formulation = _Formulation(problem)
        if mode == "optimize":
            formulation.model.minimize(formulation.total)
        model_error = formulation.model.validate()
        if model_error:
            return result(SolverStatus.ERROR, error=("invalid CP-SAT model: " + model_error)[:512])
        status = solver.solve(formulation.model)
        if status == cp_model.MODEL_INVALID:
            return result(SolverStatus.ERROR, error="CP-SAT rejected the model")
        if status == cp_model.INFEASIBLE:
            return result(SolverStatus.INFEASIBLE)
        if status == cp_model.UNKNOWN:
            bound = float(solver.best_objective_bound) if mode == "optimize" else None
            return result(SolverStatus.UNKNOWN, best_bound=bound)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return result(SolverStatus.ERROR, error="CP-SAT returned an unrecognized status")
        slots = formulation.slots(solver)
        objective = formulation.objective(solver)
        payload = {
            "snapshot_hash": problem.snapshot_hash, "problem_hash": problem.problem_hash,
            "objective_hash": problem.objective_hash, "slots": slots, "objective": objective,
        }
        plan = Plan(plan_id="plan-" + canonical_digest(payload), **payload)
        public_status = (
            SolverStatus.OPTIMAL if mode == "optimize" and status == cp_model.OPTIMAL
            else SolverStatus.FEASIBLE
        )
        bound = float(solver.best_objective_bound) if mode == "optimize" else None
        return result(public_status, plan=plan, best_bound=bound)
    except Exception as exc:
        return result(SolverStatus.ERROR, error=f"solver failure ({type(exc).__name__}): {exc}"[:512])
