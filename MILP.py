"""Aggregate-flow semiconductor digital twin implemented with Gurobi.

The factory structure and release schedule come from :mod:`ClassSetup`.  Four
optimization-specific input families are deliberately not guessed:

* initial queue/WIP by flow and route step;
* per-machine processing capacity by flow and route step;
* minimum backup machines by tool group and setup;
* the initial setup of every physical machine.

Running this file without an input JSON file prints a compact report of those
missing values and exits before a Gurobi model is created.  The JSON format is
record-oriented so tuple-keyed Python mappings have an unambiguous encoding::

    {
      "initial_queue": [
        {"flow": "Route_Product_1|regular", "step": 1, "value": 0}
      ],
      "processing_capacity": [
        {"flow": "Route_Product_1|regular", "step": 1, "value": 25}
      ],
      "backup_machines": [
        {"group": "Implant_128", "setup": "SU128_1", "value": 1}
      ],
      "initial_setup": [
        {"group": "Implant_128", "machine": 1, "setup": "SU128_1"}
      ]
    }

``processing_capacity`` is wafer-flow capacity per eligible machine per
30-minute bucket.  It cannot safely be inferred from the workbook because its
routes mix Wafer, Lot, and Batch processing units.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import gurobipy as gp
from gurobipy import GRB

import ClassSetup


FlowStepKey = tuple[str, int]
GroupSetupKey = tuple[str, str]
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
        if self.time_limit_seconds is not None and self.time_limit_seconds <= 0:
            raise ValueError("time_limit_seconds must be positive")
        if self.mip_gap is not None and self.mip_gap < 0:
            raise ValueError("mip_gap cannot be negative")


@dataclass
class MILPInputs:
    """Parameters which are not defined by ``ClassSetup`` or its workbook."""

    initial_queue: dict[FlowStepKey, float] = field(default_factory=dict)
    processing_capacity: dict[FlowStepKey, float] = field(default_factory=dict)
    backup_machines: dict[GroupSetupKey, int] = field(default_factory=dict)
    initial_setup: dict[MachineKey, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, path: Path | str) -> "MILPInputs":
        with Path(path).open(encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict):
            raise ValueError("The inputs JSON root must be an object")

        result = cls()
        for row in _records(document, "initial_queue"):
            key = (str(row["flow"]), int(row["step"]))
            _insert_unique(result.initial_queue, key, float(row["value"]))
        for row in _records(document, "processing_capacity"):
            key = (str(row["flow"]), int(row["step"]))
            _insert_unique(result.processing_capacity, key, float(row["value"]))
        for row in _records(document, "backup_machines"):
            key = (str(row["group"]), str(row["setup"]))
            value = row["value"]
            if isinstance(value, bool) or not float(value).is_integer():
                raise ValueError(f"Backup minimum for {key!r} must be an integer")
            _insert_unique(result.backup_machines, key, int(value))
        for row in _records(document, "initial_setup"):
            key = (str(row["group"]), int(row["machine"]))
            _insert_unique(result.initial_setup, key, str(row["setup"]))
        return result


@dataclass(frozen=True)
class OperationSpec:
    step: int
    position: int
    group: str
    required_setup: str | None


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

    @property
    def required_backup_keys(self) -> set[GroupSetupKey]:
        return {
            (group, state)
            for group, states in self.states.items()
            for state in states
            if state != ClassSetup.DEFAULT_STATE_ID
        }


@dataclass(frozen=True)
class ValidationIssue:
    category: str
    message: str
    keys: tuple[Any, ...] = ()


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues

    def add(
        self,
        category: str,
        message: str,
        keys: Iterable[Any] = (),
    ) -> None:
        self.issues.append(ValidationIssue(category, message, tuple(keys)))

    def format(self, max_examples: int = 8) -> str:
        if self.is_valid:
            return "All required MILP inputs are defined and valid."
        lines = [f"MILP input validation failed ({len(self.issues)} categories):"]
        for issue in self.issues:
            lines.append(f"- {issue.category}: {issue.message}")
            if issue.keys:
                examples = issue.keys[:max_examples]
                lines.append("  examples: " + ", ".join(map(repr, examples)))
                omitted = len(issue.keys) - len(examples)
                if omitted:
                    lines.append(f"  ... and {omitted} more")
        return "\n".join(lines)


@dataclass
class MILPModel:
    model: gp.Model
    data: ModelData
    inputs: MILPInputs
    x: dict[XKey, gp.Var]
    q: dict[QKey, gp.Var]
    z: dict[ZKey, gp.Var]
    u: dict[UKey, gp.Var]
    c: dict[CKey, gp.Var]


def _records(document: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    value = document.get(name, [])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{name!r} must be a list of JSON objects")
    return value


def _insert_unique(mapping: dict, key: Any, value: Any) -> None:
    if key in mapping:
        raise ValueError(f"Duplicate input key: {key!r}")
    mapping[key] = value


def _machine_id(machine: ClassSetup.Machine) -> int:
    try:
        return int(machine.id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Machine ID {machine.id!r} is not an integer as expected"
        ) from exc


def _product_route_ids() -> tuple[str, ...]:
    return tuple(f"{ClassSetup.ROUTE_SHEET_PREFIX}{number}" for number in range(1, 11))


def load_model_data(config: MILPConfig = MILPConfig()) -> ModelData:
    """Load ClassSetup data and construct the fixed 20-flow MILP index set."""

    config.validate()
    templates, orders, tool_groups = ClassSetup.build_simulation_inputs(
        xlsx_path=config.xlsx_path,
        release_horizon_minutes=config.horizon_minutes,
    )
    templates_by_id = {template.id: template for template in templates}
    orders_by_id = {order.id: order for order in orders}
    expected_routes = _product_route_ids()
    absent = [
        route_id
        for route_id in expected_routes
        if route_id not in templates_by_id or route_id not in orders_by_id
    ]
    if absent:
        raise ValueError(f"ClassSetup did not produce required routes: {absent}")

    flows: dict[str, FlowSpec] = {}
    arrivals: dict[tuple[str, int], float] = defaultdict(float)
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
                )
            )

        route_lots = [
            lot for lot in orders_by_id[route_id].batches if not lot.is_engineering
        ]
        for service_class, is_hot in (("regular", False), ("hot", True)):
            flow_id = f"{route_id}|{service_class}"
            matching_lots = [lot for lot in route_lots if lot.is_hot is is_hot]
            weight = float(max((lot.priority for lot in matching_lots), default=20 if is_hot else 10))
            flows[flow_id] = FlowSpec(
                id=flow_id,
                route_id=route_id,
                service_class=service_class,
                weight=weight,
                operations=tuple(operations),
            )
            for lot in matching_lots:
                bucket = int(math.floor(lot.release_time / config.bucket_minutes))
                if 0 <= bucket < config.number_of_buckets:
                    arrivals[(flow_id, bucket)] += float(lot.number_of_wafers)

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

    return ModelData(
        config=config,
        flows=flows,
        arrivals=dict(arrivals),
        tool_groups={group_id: groups_by_id[group_id] for group_id in used_groups},
        machines=machines,
        states=states,
        setup_times_minutes=setup_times,
    )


def validate_inputs(data: ModelData, inputs: MILPInputs) -> ValidationReport:
    """Return every discoverable missing or inconsistent input category."""

    report = ValidationReport()
    flow_steps = data.required_flow_steps
    backup_keys = data.required_backup_keys
    machine_keys = set(data.machines)

    _report_key_coverage(report, "Q0 / initial_queue", flow_steps, inputs.initial_queue)
    _report_key_coverage(
        report,
        "PC / processing_capacity",
        flow_steps,
        inputs.processing_capacity,
    )
    _report_key_coverage(
        report,
        "B / backup_machines",
        backup_keys,
        inputs.backup_machines,
    )
    _report_key_coverage(
        report,
        "Z0 / initial_setup",
        machine_keys,
        inputs.initial_setup,
    )

    bad_q = sorted(
        key
        for key, value in inputs.initial_queue.items()
        if key in flow_steps and (not math.isfinite(value) or value < 0)
    )
    if bad_q:
        report.add("invalid initial queue", "values must be finite and nonnegative", bad_q)
    bad_pc = sorted(
        key
        for key, value in inputs.processing_capacity.items()
        if key in flow_steps and (not math.isfinite(value) or value <= 0)
    )
    if bad_pc:
        report.add("invalid processing capacity", "values must be finite and positive", bad_pc)
    bad_backup = sorted(
        key
        for key, value in inputs.backup_machines.items()
        if key in backup_keys and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
    )
    if bad_backup:
        report.add("invalid backup minimum", "values must be nonnegative integers", bad_backup)

    bad_initial_states = sorted(
        key
        for key, state in inputs.initial_setup.items()
        if key in machine_keys and state not in data.states[key[0]]
    )
    if bad_initial_states:
        report.add(
            "invalid initial setup",
            "setup is not valid for the machine's tool group",
            bad_initial_states,
        )

    for group, states in data.states.items():
        number_of_machines = sum(1 for machine in machine_keys if machine[0] == group)
        if all((group, state) in inputs.backup_machines for state in states if state != ClassSetup.DEFAULT_STATE_ID):
            total_backup = sum(
                inputs.backup_machines[(group, state)]
                for state in states
                if state != ClassSetup.DEFAULT_STATE_ID
            )
            if total_backup > number_of_machines:
                report.add(
                    "infeasible backup totals",
                    f"{group!r} requires {total_backup} states but has {number_of_machines} machines",
                    [(group, state) for state in states if state != ClassSetup.DEFAULT_STATE_ID],
                )

    if machine_keys <= inputs.initial_setup.keys() and backup_keys <= inputs.backup_machines.keys():
        initial_counts: dict[GroupSetupKey, int] = defaultdict(int)
        for (group, _machine), state in inputs.initial_setup.items():
            if (group, _machine) in machine_keys and state in data.states[group]:
                initial_counts[(group, state)] += 1
        unmet = sorted(
            key
            for key in backup_keys
            if initial_counts[key] < inputs.backup_machines[key]
        )
        if unmet:
            report.add(
                "initial backup shortfall",
                "initial setup allocation does not meet the requested backup minima",
                unmet,
            )

    invalid_transitions = sorted(
        key
        for key, minutes in data.setup_times_minutes.items()
        if not math.isfinite(minutes) or minutes <= 0
    )
    if invalid_transitions:
        report.add(
            "undefined setup transitions",
            "enabled transitions must have a finite positive duration",
            invalid_transitions,
        )
    return report


def _report_key_coverage(
    report: ValidationReport,
    label: str,
    expected: set[Any],
    supplied: Mapping[Any, Any],
) -> None:
    missing = sorted(expected - supplied.keys())
    extra = sorted(supplied.keys() - expected)
    if missing:
        report.add(label, f"{len(missing)} required values are missing", missing)
    if extra:
        report.add(f"unknown {label}", f"{len(extra)} supplied keys are not modeled", extra)


def build_model(data: ModelData, inputs: MILPInputs) -> MILPModel:
    """Construct the corrected aggregate-flow MILP after strict validation."""

    report = validate_inputs(data, inputs)
    if not report.is_valid:
        raise ValueError(report.format())

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
    _add_queue_constraints(model, data, inputs, machines_by_group, x, q)
    _add_capacity_constraints(model, data, inputs, machines_by_group, x, z, u)
    _add_setup_constraints(model, data, inputs, machines_by_group, z, u, c)
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
    return MILPModel(model, data, inputs, x, q, z, u, c)


def _add_queue_constraints(
    model: gp.Model,
    data: ModelData,
    inputs: MILPInputs,
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
                    inputs.initial_queue[(flow.id, operation.step)]
                    if time == 0
                    else q[(flow.id, operation.step, time - 1)]
                )
                if position == 0:
                    inflow: gp.LinExpr | float = data.arrivals.get((flow.id, time), 0.0)
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
    inputs: MILPInputs,
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
                    x[(flow.id, operation.step, group, machine, time)]
                    / inputs.processing_capacity[(flow.id, operation.step)]
                    for flow, operation in flow_operations
                )
                model.addConstr(
                    workload <= 1 - u[(group, machine, time)],
                    name=_name("machine_capacity", group, machine, time),
                )
                for flow, operation in flow_operations:
                    if operation.required_setup is None:
                        continue
                    capacity = inputs.processing_capacity[(flow.id, operation.step)]
                    model.addConstr(
                        x[(flow.id, operation.step, group, machine, time)]
                        <= capacity * z[(group, machine, operation.required_setup, time)],
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
    inputs: MILPInputs,
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
                    int(inputs.initial_setup[(group, machine)] == old_state)
                    if time == 0
                    else z[(group, machine, old_state, time - 1)]
                )
                model.addConstr(
                    gp.quicksum(starts_from_state) <= previous_state,
                    name=_name("setup_starts_from_state", group, machine, old_state, time),
                )

            for state in states:
                previous_state = (
                    int(inputs.initial_setup[(group, machine)] == state)
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

    for group, setup in sorted(data.required_backup_keys):
        minimum = inputs.backup_machines[(group, setup)]
        for time in data.times:
            model.addConstr(
                gp.quicksum(
                    z[(group, machine, setup, time)]
                    for machine in machines_by_group[group]
                )
                >= minimum,
                name=_name("backup_minimum", group, setup, time),
            )


def _name(prefix: str, *parts: Any) -> str:
    cleaned = [re.sub(r"[^A-Za-z0-9_.-]+", "_", str(part)) for part in parts]
    return f"{prefix}[{','.join(cleaned)}]"


def solve_model(milp: MILPModel) -> int:
    """Optimize a built model and print a compact status/solution summary."""

    milp.model.optimize()
    status = milp.model.Status
    status_name = {
        GRB.LOADED: "LOADED",
        GRB.OPTIMAL: "OPTIMAL",
        GRB.INFEASIBLE: "INFEASIBLE",
        GRB.INF_OR_UNBD: "INF_OR_UNBD",
        GRB.UNBOUNDED: "UNBOUNDED",
        GRB.TIME_LIMIT: "TIME_LIMIT",
        GRB.INTERRUPTED: "INTERRUPTED",
    }.get(status, str(status))
    print(f"Solver status: {status_name}")
    if milp.model.SolCount:
        print(f"Objective value: {milp.model.ObjVal:.6g}")
        print(f"Total changeovers started: {sum(var.X > 0.5 for var in milp.c.values())}")
        final_time = milp.data.config.number_of_buckets - 1
        final_queue = sum(
            variable.X
            for (flow, step, time), variable in milp.q.items()
            if time == final_time
        )
        print(f"Queue remaining after {milp.data.config.horizon_hours} hours: {final_queue:.6g}")
    return status


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, help="JSON file containing required MILP parameters")
    parser.add_argument("--xlsx", type=Path, default=ClassSetup.DEFAULT_XLSX_PATH)
    parser.add_argument("--time-limit", type=float)
    parser.add_argument("--mip-gap", type=float)
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
        inputs = MILPInputs.from_json(args.inputs) if args.inputs else MILPInputs()
        report = validate_inputs(data, inputs)
        if not report.is_valid:
            print(report.format())
            print(
                "\nStill undefined: Q0 initial WIP, PC per-bucket capacities, "
                "B backup minima, and Z0 initial machine setups."
            )
            return 2
        milp = build_model(data, inputs)
        status = solve_model(milp)
        return 0 if milp.model.SolCount else 3
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
        gp.GurobiError,
    ) as exc:
        print(f"MILP configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
