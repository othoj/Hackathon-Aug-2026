"""Aggregate-flow semiconductor digital twin implemented with Gurobi.

The factory structure and release schedule come from :mod:`ClassSetup`.
Initial WIP, recurring orders, initial default setup states, and operation
processing times are derived internally.  Every backup minimum is fixed to
zero.  Processing time per wafer is the route worksheet's ``MEAN`` divided by
1 for Wafer rows, 25 for Lot rows, or 200 for Batch rows.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import gurobipy as gp
from gurobipy import GRB

import ClassSetup


FlowStepKey = tuple[str, int]
MachineKey = tuple[str, int]
XKey = tuple[str, int, str, int, int]
QKey = tuple[str, int, int]
ZKey = tuple[str, int, str, int]
UKey = tuple[str, int, int]
CKey = tuple[str, int, str, str, int]


@dataclass(frozen=True)
class MILPConfig:
    """Time discretization, source workbook, and solver controls."""

    xlsx_path: Path = ClassSetup.DEFAULT_XLSX_PATH
    bucket_minutes: int = 30
    horizon_hours: int = 40
    time_limit_seconds: float | None = None
    mip_gap: float | None = None
    output_flag: int = 1

    @property
    def horizon_minutes(self) -> int:
        return self.horizon_hours * 60

    @property
    def number_of_buckets(self) -> int:
        if self.horizon_minutes % self.bucket_minutes:
            raise ValueError(
                "horizon_minutes must be divisible by bucket_minutes"
            )
        return self.horizon_minutes // self.bucket_minutes

    def validate(self) -> None:
        if self.bucket_minutes <= 0:
            raise ValueError("bucket_minutes must be positive")
        if self.horizon_hours <= 0:
            raise ValueError("horizon_hours must be positive")
        _ = self.number_of_buckets
        if (
            self.bucket_minutes != ClassSetup.MILP_BUCKET_MINUTES
            or self.number_of_buckets != ClassSetup.MILP_NUMBER_OF_BUCKETS
        ):
            raise ValueError("The order schedule requires 80 30-minute buckets")
        if self.time_limit_seconds is not None and self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if self.mip_gap is not None and self.mip_gap < 0:
            raise ValueError("mip_gap cannot be negative")


@dataclass(frozen=True)
class OperationSpec:
    step: int
    position: int
    group: str
    required_setup: str | None
    processing_time: float


@dataclass(frozen=True)
class FlowSpec:
    id: str
    route_id: str
    service_class: str
    weight: float
    operations: tuple[OperationSpec, ...]


@dataclass
class ModelData:
    """ClassSetup objects transformed into immutable MILP index data."""

    config: MILPConfig
    flows: dict[str, FlowSpec]
    arrivals: dict[tuple[str, int], float]
    orders: tuple[ClassSetup.MILPOrder, ...]
    initial_queue: dict[FlowStepKey, float]
    initial_setup: dict[MachineKey, str]
    tool_groups: dict[str, ClassSetup.ToolGroup]
    machines: dict[MachineKey, ClassSetup.Machine]
    states: dict[str, tuple[str, ...]]
    setup_times_minutes: dict[tuple[str, str, str], float]

    @property
    def times(self) -> range:
        return range(self.config.number_of_buckets)

    @property
    def required_flow_steps(self) -> set[FlowStepKey]:
        return {
            (flow.id, operation.step)
            for flow in self.flows.values()
            for operation in flow.operations
        }

@dataclass(frozen=True)
class MILPModel:
    model: gp.Model
    data: ModelData
    x: dict[XKey, gp.Var]
    q: dict[QKey, gp.Var]
    z: dict[ZKey, gp.Var]
    u: dict[UKey, gp.Var]
    c: dict[CKey, gp.Var]


def _machine_id(machine: ClassSetup.Machine) -> int:
    try:
        return int(machine.id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Machine ID {machine.id!r} is not an integer as expected"
        ) from exc


def _product_route_ids() -> tuple[str, ...]:
    return tuple(f"{ClassSetup.ROUTE_SHEET_PREFIX}{number}" for number in range(1, 11))


def _processing_time_per_wafer(step: ClassSetup.Step, route_id: str) -> float:
    """Convert a route row's MEAN to minutes per wafer."""

    try:
        processing_time = step.processing_time_per_wafer
    except ValueError as exc:
        raise ValueError(
            f"{route_id} step {step.route_step}: {exc}"
        ) from exc
    if not math.isfinite(processing_time) or processing_time <= 0:
        raise ValueError(
            f"{route_id} step {step.route_step} has invalid processing time "
            f"{processing_time!r} minutes per wafer"
        )
    return processing_time


def load_model_data(config: MILPConfig = MILPConfig()) -> ModelData:
    """Load ClassSetup data and construct the fixed 20-flow MILP index set."""

    config.validate()
    templates, _simulation_orders, tool_groups = ClassSetup.build_simulation_inputs(
        xlsx_path=config.xlsx_path,
        release_horizon_minutes=config.horizon_minutes,
    )
    templates_by_id = {template.id: template for template in templates}
    expected_routes = _product_route_ids()
    absent = [
        route_id
        for route_id in expected_routes
        if route_id not in templates_by_id
    ]
    if absent:
        raise ValueError(f"ClassSetup did not produce required routes: {absent}")

    flows: dict[str, FlowSpec] = {}
    for route_id in expected_routes:
        template = templates_by_id[route_id]
        operations: list[OperationSpec] = []
        seen_steps: set[int] = set()
        for position, step in enumerate(template.steps):
            if step.route_step is None:
                raise ValueError(f"{route_id} contains a route step without an ID")
            step_id = int(step.route_step)
            if step_id in seen_steps:
                raise ValueError(f"{route_id} repeats route step {step_id}")
            seen_steps.add(step_id)
            operations.append(
                OperationSpec(
                    step=step_id,
                    position=position,
                    group=step.tool_group_needed,
                    required_setup=step.setup_id,
                    processing_time=_processing_time_per_wafer(step, route_id),
                )
            )

        for service_class in ("regular", "hot"):
            flow_id = f"{route_id}|{service_class}"
            flows[flow_id] = FlowSpec(
                id=flow_id,
                route_id=route_id,
                service_class=service_class,
                weight=20.0 if service_class == "hot" else 10.0,
                operations=tuple(operations),
            )

    orders = ClassSetup.get_milp_orders()
    arrivals: dict[tuple[str, int], float] = defaultdict(float)
    for order in orders:
        if order.flow_id not in flows:
            raise ValueError(f"Order {order.id!r} uses unknown flow {order.flow_id!r}")
        arrivals[(order.flow_id, order.time_bucket)] += order.quantity

    initial_queue = {
        (flow.id, operation.step): 0.0
        for flow in flows.values()
        for operation in flow.operations
    }
    for product in range(1, 5):
        flow = flows[f"Route_Product_{product}|regular"]
        initial_queue[(flow.id, flow.operations[0].step)] = 200.0

    groups_by_id = {group.id: group for group in tool_groups}
    machines: dict[MachineKey, ClassSetup.Machine] = {}
    states: dict[str, tuple[str, ...]] = {}
    setup_times: dict[tuple[str, str, str], float] = {}
    used_groups = {operation.group for flow in flows.values() for operation in flow.operations}
    unknown_groups = sorted(used_groups - groups_by_id.keys())
    if unknown_groups:
        raise ValueError(f"Routes refer to unknown tool groups: {unknown_groups}")

    for group_id in sorted(used_groups):
        group = groups_by_id[group_id]
        group_states = tuple(state.id for state in group.states)
        if len(group_states) != len(set(group_states)):
            raise ValueError(f"Tool group {group_id!r} has duplicate state IDs")
        states[group_id] = group_states
        for machine in group.all_machines():
            key = (group_id, _machine_id(machine))
            if key in machines:
                raise ValueError(f"Duplicate qualified machine ID: {key!r}")
            machines[key] = machine
        for target in group.states:
            if target.id == ClassSetup.DEFAULT_STATE_ID:
                continue
            for current in group.states:
                if current.id == target.id:
                    continue
                setup_times[(group_id, current.id, target.id)] = float(
                    target.setup_time_from(current)
                )

    unknown_setups = sorted(
        {
            (operation.group, operation.required_setup)
            for flow in flows.values()
            for operation in flow.operations
            if operation.required_setup is not None
            and operation.required_setup not in states[operation.group]
        }
    )
    if unknown_setups:
        raise ValueError(
            f"Routes refer to setups not defined for their tool groups: {unknown_setups}"
        )

    invalid_transitions = sorted(
        key
        for key, minutes in setup_times.items()
        if not math.isfinite(minutes) or minutes <= 0
    )
    if invalid_transitions:
        raise ValueError(
            "Enabled setup transitions require finite positive durations; "
            f"invalid transitions: {invalid_transitions[:8]}"
        )

    initial_setup = {
        machine_key: ClassSetup.DEFAULT_STATE_ID for machine_key in machines
    }
    return ModelData(
        config=config,
        flows=flows,
        arrivals=dict(arrivals),
        orders=orders,
        initial_queue=initial_queue,
        initial_setup=initial_setup,
        tool_groups={group_id: groups_by_id[group_id] for group_id in used_groups},
        machines=machines,
        states=states,
        setup_times_minutes=setup_times,
    )


def build_model(data: ModelData) -> MILPModel:
    """Construct the aggregate-flow MILP from workbook-derived parameters."""

    model = gp.Model("semiconductor_digital_twin")
    model.Params.OutputFlag = data.config.output_flag
    if data.config.time_limit_seconds is not None:
        model.Params.TimeLimit = data.config.time_limit_seconds
    if data.config.mip_gap is not None:
        model.Params.MIPGap = data.config.mip_gap

    x: dict[XKey, gp.Var] = {}
    q: dict[QKey, gp.Var] = {}
    z: dict[ZKey, gp.Var] = {}
    u: dict[UKey, gp.Var] = {}
    c: dict[CKey, gp.Var] = {}

    machines_by_group: dict[str, list[int]] = defaultdict(list)
    for group, machine in sorted(data.machines):
        machines_by_group[group].append(machine)

    for flow in data.flows.values():
        for operation in flow.operations:
            for time in data.times:
                q[(flow.id, operation.step, time)] = model.addVar(
                    lb=0.0,
                    vtype=GRB.CONTINUOUS,
                    name=_name("q", flow.id, operation.step, time),
                )
                for machine in machines_by_group[operation.group]:
                    x[(flow.id, operation.step, operation.group, machine, time)] = model.addVar(
                        lb=0.0,
                        vtype=GRB.CONTINUOUS,
                        name=_name("x", flow.id, operation.step, operation.group, machine, time),
                    )

    for (group, machine) in sorted(data.machines):
        for time in data.times:
            u[(group, machine, time)] = model.addVar(
                vtype=GRB.BINARY,
                name=_name("u", group, machine, time),
            )
            for state in data.states[group]:
                z[(group, machine, state, time)] = model.addVar(
                    vtype=GRB.BINARY,
                    name=_name("z", group, machine, state, time),
                )
            for old_state in data.states[group]:
                for new_state in data.states[group]:
                    if old_state == new_state or new_state == ClassSetup.DEFAULT_STATE_ID:
                        continue
                    c[(group, machine, old_state, new_state, time)] = model.addVar(
                        vtype=GRB.BINARY,
                        name=_name("c", group, machine, old_state, new_state, time),
                    )

    model.update()
    _add_queue_constraints(model, data, machines_by_group, x, q)
    _add_capacity_constraints(model, data, machines_by_group, x, z, u)
    _add_setup_constraints(model, data, machines_by_group, z, u, c)
    model.setObjective(
        gp.quicksum(
            flow.weight * q[(flow.id, operation.step, time)]
            for flow in data.flows.values()
            for operation in flow.operations
            for time in data.times
        ),
        GRB.MINIMIZE,
    )
    model.update()
    return MILPModel(model, data, x, q, z, u, c)


def _add_queue_constraints(
    model: gp.Model,
    data: ModelData,
    machines_by_group: Mapping[str, Sequence[int]],
    x: Mapping[XKey, gp.Var],
    q: Mapping[QKey, gp.Var],
) -> None:
    for flow in data.flows.values():
        for position, operation in enumerate(flow.operations):
            for time in data.times:
                outflow = gp.quicksum(
                    x[(flow.id, operation.step, operation.group, machine, time)]
                    for machine in machines_by_group[operation.group]
                )
                previous_queue: gp.LinExpr | gp.Var | float = (
                    data.initial_queue[(flow.id, operation.step)]
                    if time == 0
                    else q[(flow.id, operation.step, time - 1)]
                )
                if position == 0:
                    inflow: gp.LinExpr | float = (
                        0.0
                        if time == 0
                        else data.arrivals.get((flow.id, time), 0.0)
                    )
                else:
                    predecessor = flow.operations[position - 1]
                    inflow = gp.quicksum(
                        x[(flow.id, predecessor.step, predecessor.group, machine, time)]
                        for machine in machines_by_group[predecessor.group]
                    )
                model.addConstr(
                    q[(flow.id, operation.step, time)]
                    == previous_queue + inflow - outflow,
                    name=_name("queue_balance", flow.id, operation.step, time),
                )
                model.addConstr(
                    outflow <= previous_queue + inflow,
                    name=_name("flow_available", flow.id, operation.step, time),
                )


def _add_capacity_constraints(
    model: gp.Model,
    data: ModelData,
    machines_by_group: Mapping[str, Sequence[int]],
    x: Mapping[XKey, gp.Var],
    z: Mapping[ZKey, gp.Var],
    u: Mapping[UKey, gp.Var],
) -> None:
    operations_by_group: dict[str, list[tuple[FlowSpec, OperationSpec]]] = defaultdict(list)
    for flow in data.flows.values():
        for operation in flow.operations:
            operations_by_group[operation.group].append((flow, operation))

    for group, flow_operations in operations_by_group.items():
        for machine in machines_by_group[group]:
            for time in data.times:
                workload = gp.quicksum(
                    operation.processing_time
                    * x[(flow.id, operation.step, group, machine, time)]
                    for flow, operation in flow_operations
                )
                model.addConstr(
                    workload
                    <= data.config.bucket_minutes * (1 - u[(group, machine, time)]),
                    name=_name("machine_capacity", group, machine, time),
                )
                for flow, operation in flow_operations:
                    if operation.required_setup is None:
                        continue
                    model.addConstr(
                        operation.processing_time
                        * x[(flow.id, operation.step, group, machine, time)]
                        <= data.config.bucket_minutes
                        * z[(group, machine, operation.required_setup, time)],
                        name=_name(
                            "setup_compatibility",
                            flow.id,
                            operation.step,
                            group,
                            machine,
                            time,
                        ),
                    )


def _add_setup_constraints(
    model: gp.Model,
    data: ModelData,
    machines_by_group: Mapping[str, Sequence[int]],
    z: Mapping[ZKey, gp.Var],
    u: Mapping[UKey, gp.Var],
    c: Mapping[CKey, gp.Var],
) -> None:
    duration_buckets = {
        key: max(1, math.ceil(minutes / data.config.bucket_minutes))
        for key, minutes in data.setup_times_minutes.items()
    }
    for group, machine in sorted(data.machines):
        states = data.states[group]
        for time in data.times:
            active_changeovers = []
            for old_state in states:
                for new_state in states:
                    if old_state == new_state or new_state == ClassSetup.DEFAULT_STATE_ID:
                        continue
                    duration = duration_buckets[(group, old_state, new_state)]
                    first_start = max(0, time - duration + 1)
                    active_changeovers.extend(
                        c[(group, machine, old_state, new_state, start)]
                        for start in range(first_start, time + 1)
                    )
            model.addConstr(
                u[(group, machine, time)] == gp.quicksum(active_changeovers),
                name=_name("setup_occupancy", group, machine, time),
            )
            model.addConstr(
                gp.quicksum(z[(group, machine, state, time)] for state in states)
                + u[(group, machine, time)]
                == 1,
                name=_name("state_or_setup", group, machine, time),
            )
            starts = [
                c[(group, machine, old_state, new_state, time)]
                for old_state in states
                for new_state in states
                if old_state != new_state and new_state != ClassSetup.DEFAULT_STATE_ID
            ]
            model.addConstr(
                gp.quicksum(starts) <= 1,
                name=_name("one_setup_start", group, machine, time),
            )

            for old_state in states:
                starts_from_state = [
                    c[(group, machine, old_state, new_state, time)]
                    for new_state in states
                    if new_state != old_state and new_state != ClassSetup.DEFAULT_STATE_ID
                ]
                previous_state: gp.Var | int = (
                    int(data.initial_setup[(group, machine)] == old_state)
                    if time == 0
                    else z[(group, machine, old_state, time - 1)]
                )
                model.addConstr(
                    gp.quicksum(starts_from_state) <= previous_state,
                    name=_name("setup_starts_from_state", group, machine, old_state, time),
                )

            for state in states:
                previous_state = (
                    int(data.initial_setup[(group, machine)] == state)
                    if time == 0
                    else z[(group, machine, state, time - 1)]
                )
                departures = gp.quicksum(
                    c[(group, machine, state, new_state, time)]
                    for new_state in states
                    if new_state != state and new_state != ClassSetup.DEFAULT_STATE_ID
                )
                completions = []
                if state != ClassSetup.DEFAULT_STATE_ID:
                    for old_state in states:
                        if old_state == state:
                            continue
                        duration = duration_buckets[(group, old_state, state)]
                        start = time - duration
                        if start >= 0:
                            completions.append(c[(group, machine, old_state, state, start)])
                model.addConstr(
                    z[(group, machine, state, time)]
                    == previous_state - departures + gp.quicksum(completions),
                    name=_name("state_evolution", group, machine, state, time),
                )

def _name(prefix: str, *parts: Any) -> str:
    cleaned = [re.sub(r"[^A-Za-z0-9_.-]+", "_", str(part)) for part in parts]
    return f"{prefix}[{','.join(cleaned)}]"


def _status_name(status: int) -> str:
    return {
        GRB.LOADED: "LOADED",
        GRB.OPTIMAL: "OPTIMAL",
        GRB.INFEASIBLE: "INFEASIBLE",
        GRB.INF_OR_UNBD: "INF_OR_UNBD",
        GRB.UNBOUNDED: "UNBOUNDED",
        GRB.TIME_LIMIT: "TIME_LIMIT",
        GRB.INTERRUPTED: "INTERRUPTED",
    }.get(status, str(status))


def extract_result(milp: MILPModel) -> ClassSetup.MILPResult:
    """Convert Gurobi values into ClassSetup's chronological schedule schema."""

    has_solution = bool(milp.model.SolCount)
    events: list[ClassSetup.MILPScheduleEvent] = []
    total_completed: float | None = None
    final_queue: float | None = None
    objective: float | None = None
    mip_gap: float | None = None

    if has_solution:
        objective = float(milp.model.ObjVal)
        mip_gap = float(milp.model.MIPGap)
        bucket_minutes = milp.data.config.bucket_minutes
        for (flow, step, group, machine, time), variable in milp.x.items():
            if variable.X <= 1e-6:
                continue
            start_minute = float(time * bucket_minutes)
            events.append(
                ClassSetup.MILPScheduleEvent(
                    time_bucket=time,
                    start_minute=start_minute,
                    end_minute=start_minute + bucket_minutes,
                    event_type="PROCESS",
                    group=group,
                    machine=machine,
                    flow_id=flow,
                    route_step=step,
                    quantity=float(variable.X),
                )
            )
        for (group, machine, old_setup, new_setup, time), variable in milp.c.items():
            if variable.X <= 0.5:
                continue
            duration_buckets = math.ceil(
                milp.data.setup_times_minutes[(group, old_setup, new_setup)]
                / bucket_minutes
            )
            start_minute = float(time * bucket_minutes)
            events.append(
                ClassSetup.MILPScheduleEvent(
                    time_bucket=time,
                    start_minute=start_minute,
                    end_minute=start_minute + duration_buckets * bucket_minutes,
                    event_type="SETUP",
                    group=group,
                    machine=machine,
                    from_setup=old_setup,
                    to_setup=new_setup,
                )
            )
        events.sort(
            key=lambda event: (
                event.time_bucket,
                0 if event.event_type == "SETUP" else 1,
                event.group,
                event.machine,
                event.route_step or -1,
            )
        )
        total_completed = sum(
            variable.X
            for flow in milp.data.flows.values()
            for variable in (
                milp.x[(flow.id, flow.operations[-1].step, flow.operations[-1].group, machine, time)]
                for machine in (
                    key[1]
                    for key in milp.data.machines
                    if key[0] == flow.operations[-1].group
                )
                for time in milp.data.times
            )
        )
        final_time = milp.data.config.number_of_buckets - 1
        final_queue = sum(
            variable.X
            for (flow, step, time), variable in milp.q.items()
            if time == final_time
        )

    return ClassSetup.MILPResult(
        status=_status_name(milp.model.Status),
        has_solution=has_solution,
        objective=objective,
        runtime_seconds=float(milp.model.Runtime),
        mip_gap=mip_gap,
        bucket_minutes=milp.data.config.bucket_minutes,
        horizon_hours=milp.data.config.horizon_hours,
        total_arrivals=sum(milp.data.arrivals.values()),
        total_completed=total_completed,
        final_queue=final_queue,
        events=tuple(events),
    )


def solve_model(
    milp: MILPModel,
    result_path: Path | str = ClassSetup.DEFAULT_MILP_RESULT_PATH,
) -> ClassSetup.MILPResult:
    """Optimize, print the ordered schedule, and persist it for the GUI."""

    milp.model.optimize()
    result = extract_result(milp)
    print(ClassSetup.format_milp_result(result))
    saved_path = ClassSetup.save_milp_result(result, result_path)
    print(f"\nSchedule saved to: {saved_path}")
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", type=Path, default=ClassSetup.DEFAULT_XLSX_PATH)
    parser.add_argument("--time-limit", type=float)
    parser.add_argument("--mip-gap", type=float)
    parser.add_argument(
        "--results",
        type=Path,
        default=ClassSetup.DEFAULT_MILP_RESULT_PATH,
        help="JSON schedule read by enhanced_simulation_gui.py",
    )
    parser.add_argument("--quiet", action="store_true", help="disable Gurobi console logging")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    config = MILPConfig(
        xlsx_path=args.xlsx,
        time_limit_seconds=args.time_limit,
        mip_gap=args.mip_gap,
        output_flag=0 if args.quiet else 1,
    )
    try:
        data = load_model_data(config)
        milp = build_model(data)
        result = solve_model(milp, args.results)
        return 0 if result.has_solution else 3
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        gp.GurobiError,
    ) as exc:
        print(f"MILP configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
