from __future__ import annotations

from collections import deque
from heapq import heappop, heappush
from itertools import count
from pathlib import Path

import pandas as pd

from get_routes_for_products import PROCESSING_UNIT_MULTIPLIERS


DEFAULT_XLSX_PATH = (
    Path(__file__).resolve().parent
    / "SMT2020"
    / "SMT_2020 - Final"
    / "General Data"
    / "dataset 4"
    / "SMT_2020_Model_Data_-_LVHM_E.xlsx"
)
DEFAULT_STATE_ID = "default"
ROUTE_SHEET_PREFIX = "Route_Product_"
BATCHES_PER_ORDER = 200
REQUIRED_ROUTE_COLUMNS = {
    "STEP",
    "TOOLGROUP",
    "PROCESSING UNIT",
    "MEAN",
    "OFFSET",
    "SETUP",
    "WHEN",
}


class ToolGroup:
    """A collection of machines and the states in which they can operate."""

    def __init__(self, id: str):
        self.id = id
        self.states: list[State] = []
        self.machines: dict[State, list[Machine]] = {}

    def add_state(self, state: State) -> None:
        """Add a state and expose its machine list through ``machines``."""
        if any(existing.id == state.id for existing in self.states):
            raise ValueError(
                f"Tool group {self.id!r} already has state {state.id!r}"
            )
        self.states.append(state)
        self.machines[state] = state.machines
        for machine in state.machines:
            machine.tool_group = self
            machine.current_state = state

    def get_state(self, state_id: str) -> State:
        for state in self.states:
            if state.id == state_id:
                return state
        raise KeyError(f"Tool group {self.id!r} has no state {state_id!r}")

    def move_machine(self, machine: Machine, state: State) -> None:
        """Move a machine to a different state bucket."""
        if machine.current_state is state:
            return
        if machine.current_state is None:
            raise ValueError(f"Machine {machine.id!r} has no current state")
        self.machines[machine.current_state].remove(machine)
        self.machines[state].append(machine)
        machine.current_state = state

    def all_machines(self) -> list[Machine]:
        return [
            machine
            for state in self.states
            for machine in self.machines[state]
        ]


class State:
    """One possible setup of a tool group."""

    def __init__(
        self,
        id: str,
        machines: list[Machine] | None = None,
        time_needed_to_set_up: float = 0.0,
    ):
        self.id = id
        self.machines = machines if machines is not None else []
        self.time_needed_to_set_up = float(time_needed_to_set_up)
        self.setup_times_by_current_state: dict[str, float] = {}

    def setup_time_from(self, current_state: State | None) -> float:
        if current_state is not None:
            transition_time = self.setup_times_by_current_state.get(
                current_state.id
            )
            if transition_time is not None:
                return transition_time
        return self.time_needed_to_set_up


class Machine:
    """One tool which can process a single queued batch at a time."""

    def __init__(self, id):
        self.id = id
        self.queue: list[Batch] = []
        self.time_stamp = 0.0
        self.current_state: State | None = None
        self.tool_group: ToolGroup | None = None

    @property
    def timestamp(self) -> float:
        return self.time_stamp

    @timestamp.setter
    def timestamp(self, value: float) -> None:
        self.time_stamp = float(value)

    @property
    def is_open(self) -> bool:
        return not self.queue

    @property
    def current_batch(self) -> Batch | None:
        return self.queue[0] if self.queue else None


class Batch:
    """A single item progressing through one product route."""

    def __init__(self, id: str, steps: list[Step]):
        if not steps:
            raise ValueError("A batch must contain at least one step")
        self.id = id
        self.steps = steps
        self.current_step_index = 0
        self.total_number_of_steps = len(steps)
        self.finished = False
        self.time_stamp = 0.0

    @property
    def current_step(self) -> Step | None:
        if self.finished:
            return None
        return self.steps[self.current_step_index]

    @property
    def total_steps(self) -> int:
        return self.total_number_of_steps

    @property
    def timestamp(self) -> float:
        return self.time_stamp

    def mark_current_step_completed(self, time_completed_at: float) -> None:
        step = self.current_step
        if step is None:
            raise ValueError(f"Batch {self.id!r} is already finished")
        step.time_completed_at = float(time_completed_at)
        self.time_stamp = step.time_completed_at
        if self.current_step_index + 1 == self.total_number_of_steps:
            self.finished = True
        else:
            self.current_step_index += 1

    def clone(self) -> Batch:
        return Batch(self.id, [step.clone() for step in self.steps])


class Order:
    """A collection of batches which all follow the same product route."""

    def __init__(self, id: str, batches: list[Batch]):
        self.id = id
        self.batches = batches


class Step:
    """One line of a Route_Product worksheet."""

    def __init__(
        self,
        tool_group_needed: str,
        time_needed: float,
        setup_id: str | None = None,
        setup_when: str | None = None,
        route_step: int | None = None,
    ):
        self.tool_group_needed = tool_group_needed
        self.time_needed = float(time_needed)
        self.time_completed_at: float | None = None
        self.setup_id = setup_id
        self.setup_when = setup_when
        self.route_step = route_step

    @property
    def tool_group(self) -> str:
        return self.tool_group_needed

    @property
    def setup_is_always_required(self) -> bool:
        return self.setup_when == "always"

    def clone(self) -> Step:
        return Step(
            self.tool_group_needed,
            self.time_needed,
            self.setup_id,
            self.setup_when,
            self.route_step,
        )


def _route_sheet_names(xlsx_path: Path) -> list[str]:
    """Return every product-route sheet in workbook order."""
    return [
        name
        for name in pd.ExcelFile(xlsx_path).sheet_names
        if name.startswith(ROUTE_SHEET_PREFIX)
    ]


def _read_route_pages(xlsx_path: Path) -> dict[str, pd.DataFrame]:
    """Read product-route pages which contain every required route column."""
    workbook = pd.ExcelFile(xlsx_path)
    pages: dict[str, pd.DataFrame] = {}
    for sheet_name in workbook.sheet_names:
        if not sheet_name.startswith(ROUTE_SHEET_PREFIX):
            continue
        route = pd.read_excel(workbook, sheet_name=sheet_name)
        if REQUIRED_ROUTE_COLUMNS.issubset(route.columns):
            pages[sheet_name] = route
    return pages


def _clean_optional_string(value) -> str | None:
    if pd.isna(value):
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _states_by_tool_group(
    route_pages: dict[str, pd.DataFrame],
) -> dict[str, dict[str, float]]:
    """Collect state IDs and route-level setup times for every tool group."""
    states: dict[str, dict[str, float]] = {}
    for route in route_pages.values():
        for _, row in route.iterrows():
            tool_group_id = _clean_optional_string(row["TOOLGROUP"])
            state_id = _clean_optional_string(row["SETUP"])
            if tool_group_id is None or state_id is None:
                continue
            setup_time = (
                0.0
                if "SETUP TIME" not in route or pd.isna(row["SETUP TIME"])
                else float(row["SETUP TIME"])
            )
            tool_group_states = states.setdefault(tool_group_id, {})
            previous_time = tool_group_states.get(state_id, 0.0)
            tool_group_states[state_id] = setup_time or previous_time
    return states


def _add_setup_sheet_times(
    xlsx_path: Path,
    tool_groups_by_id: dict[str, ToolGroup],
) -> None:
    """Add setup times omitted from routes and transition-specific times."""
    setup_definitions = pd.read_excel(xlsx_path, sheet_name="Setups")
    states_by_id = {
        state.id: state
        for tool_group in tool_groups_by_id.values()
        for state in tool_group.states
    }
    for _, row in setup_definitions.iterrows():
        new_state_id = _clean_optional_string(row["NEW SETUP"])
        if (
            new_state_id is None
            or new_state_id not in states_by_id
            or pd.isna(row["SETUP TIME"])
        ):
            continue
        state = states_by_id[new_state_id]
        setup_time = float(row["SETUP TIME"])
        current_state_id = _clean_optional_string(row["CURRENT SETUP"])
        if current_state_id is None:
            state.time_needed_to_set_up = setup_time
        else:
            state.setup_times_by_current_state[current_state_id] = setup_time
            if state.time_needed_to_set_up == 0.0:
                state.time_needed_to_set_up = setup_time


def build_tool_groups(
    xlsx_path: Path | str = DEFAULT_XLSX_PATH,
    route_pages: dict[str, pd.DataFrame] | None = None,
) -> list[ToolGroup]:
    """Build tool groups, machines, and route-derived states."""
    path = Path(xlsx_path)
    pages = route_pages if route_pages is not None else _read_route_pages(path)
    states_by_tool_group = _states_by_tool_group(pages)
    definitions = pd.read_excel(
        path,
        sheet_name="Toolgroups",
        usecols=["TOOLGROUP", "NUMBER OF TOOLS"],
    )

    tool_groups: list[ToolGroup] = []
    for _, row in definitions.iterrows():
        if pd.isna(row["TOOLGROUP"]) or pd.isna(row["NUMBER OF TOOLS"]):
            continue
        tool_group_id = str(row["TOOLGROUP"]).strip()
        number_of_tools = int(row["NUMBER OF TOOLS"])
        tool_group = ToolGroup(tool_group_id)
        default_machines = [
            Machine(machine_id) for machine_id in range(1, number_of_tools + 1)
        ]
        tool_group.add_state(State(DEFAULT_STATE_ID, default_machines))
        for state_id, setup_time in states_by_tool_group.get(
            tool_group_id, {}
        ).items():
            tool_group.add_state(
                State(state_id, time_needed_to_set_up=setup_time)
            )
        tool_groups.append(tool_group)

    _add_setup_sheet_times(path, {group.id: group for group in tool_groups})
    return tool_groups


def _processing_time_in_minutes(row: pd.Series) -> float:
    processing_unit = str(row["PROCESSING UNIT"]).strip().lower()
    if processing_unit not in PROCESSING_UNIT_MULTIPLIERS:
        raise ValueError(f"Unknown processing unit: {processing_unit!r}")
    return float(row["MEAN"]) * PROCESSING_UNIT_MULTIPLIERS[processing_unit]


def build_batch_templates(
    xlsx_path: Path | str = DEFAULT_XLSX_PATH,
    route_pages: dict[str, pd.DataFrame] | None = None,
) -> list[Batch]:
    """Create one batch template for every properly formatted route page."""
    path = Path(xlsx_path)
    pages = route_pages if route_pages is not None else _read_route_pages(path)
    templates: list[Batch] = []
    for route_name, route in pages.items():
        steps: list[Step] = []
        for _, row in route.iterrows():
            if (
                pd.isna(row["TOOLGROUP"])
                or pd.isna(row["MEAN"])
                or pd.isna(row["OFFSET"])
            ):
                continue
            setup_id = _clean_optional_string(row["SETUP"])
            setup_when = _clean_optional_string(row["WHEN"])
            steps.append(
                Step(
                    tool_group_needed=str(row["TOOLGROUP"]).strip(),
                    time_needed=_processing_time_in_minutes(row),
                    setup_id=setup_id,
                    setup_when=(
                        setup_when.lower() if setup_when is not None else None
                    ),
                    route_step=int(row["STEP"]),
                )
            )
        if steps:
            templates.append(Batch(route_name, steps))
    return templates


def build_orders(
    batch_templates: list[Batch],
    batches_per_order: int = BATCHES_PER_ORDER,
) -> list[Order]:
    """Create one order containing identical copies of each route template."""
    if batches_per_order <= 0:
        raise ValueError("batches_per_order must be positive")
    return [
        Order(
            template.id,
            [template.clone() for _ in range(batches_per_order)],
        )
        for template in batch_templates
    ]


class Simulation:
    """Discrete-event scheduler for the configured machines and orders."""

    def __init__(self, tool_groups: list[ToolGroup], orders: list[Order]):
        self.tool_groups = {group.id: group for group in tool_groups}
        self.orders = orders
        self.global_timer = 0.0
        self.completed_batches = 0
        self.total_batches = sum(len(order.batches) for order in orders)
        self._ready: dict[str, deque[Batch]] = {
            group_id: deque() for group_id in self.tool_groups
        }
        self._open_machines: dict[str, deque[Machine]] = {
            group_id: deque(group.all_machines())
            for group_id, group in self.tool_groups.items()
        }
        self._busy: list[tuple[float, int, Machine]] = []
        self._event_ids = count()
        self._load_initial_batches()

    def _load_initial_batches(self) -> None:
        for order in self.orders:
            for batch in order.batches:
                step = batch.current_step
                if step is None:
                    self.completed_batches += 1
                    continue
                if step.tool_group_needed not in self.tool_groups:
                    raise KeyError(
                        f"Unknown tool group {step.tool_group_needed!r} "
                        f"in batch {batch.id!r}"
                    )
                self._ready[step.tool_group_needed].append(batch)

    def _setup_machine_for_step(
        self,
        machine: Machine,
        tool_group: ToolGroup,
        step: Step,
    ) -> float:
        if step.setup_id is None:
            return 0.0
        new_state = tool_group.get_state(step.setup_id)
        setup_is_needed = (
            step.setup_is_always_required
            or machine.current_state is not new_state
        )
        setup_time = (
            new_state.setup_time_from(machine.current_state)
            if setup_is_needed
            else 0.0
        )
        tool_group.move_machine(machine, new_state)
        return setup_time

    def _dispatch(self, tool_group_id: str) -> None:
        waiting = self._ready[tool_group_id]
        available = self._open_machines[tool_group_id]
        tool_group = self.tool_groups[tool_group_id]
        while waiting and available:
            batch = waiting.popleft()
            machine = available.popleft()
            step = batch.current_step
            if step is None:
                continue
            setup_time = self._setup_machine_for_step(
                machine, tool_group, step
            )
            machine.queue.append(batch)
            machine.time_stamp = (
                self.global_timer + setup_time + step.time_needed
            )
            heappush(
                self._busy,
                (machine.time_stamp, next(self._event_ids), machine),
            )

    def _complete_machine(self, machine: Machine) -> set[str]:
        batch = machine.queue.pop(0)
        batch.mark_current_step_completed(machine.time_stamp)
        affected_tool_groups = {machine.tool_group.id}
        self._open_machines[machine.tool_group.id].append(machine)
        next_step = batch.current_step
        if next_step is None:
            self.completed_batches += 1
        else:
            self._ready[next_step.tool_group_needed].append(batch)
            affected_tool_groups.add(next_step.tool_group_needed)
        return affected_tool_groups

    def run(self) -> float:
        """Run until every batch is finished and return elapsed minutes."""
        for tool_group_id in self.tool_groups:
            self._dispatch(tool_group_id)

        while self._busy:
            self.global_timer = self._busy[0][0]
            affected_tool_groups: set[str] = set()
            while self._busy and self._busy[0][0] <= self.global_timer:
                _, _, machine = heappop(self._busy)
                affected_tool_groups.update(self._complete_machine(machine))
            for tool_group_id in affected_tool_groups:
                self._dispatch(tool_group_id)

        if self.completed_batches != self.total_batches:
            blocked = {
                group_id: len(batches)
                for group_id, batches in self._ready.items()
                if batches
            }
            raise RuntimeError(f"Simulation stopped with blocked batches: {blocked}")
        return self.global_timer


def simulate(tool_groups: list[ToolGroup], orders: list[Order]) -> float:
    """Convenience function which runs a simulation and returns its timer."""
    return Simulation(tool_groups, orders).run()


def build_simulation_inputs(
    xlsx_path: Path | str = DEFAULT_XLSX_PATH,
    batches_per_order: int = BATCHES_PER_ORDER,
) -> tuple[list[Batch], list[Order], list[ToolGroup]]:
    """Build templates, orders, and tool groups with one workbook read."""
    path = Path(xlsx_path)
    route_pages = _read_route_pages(path)
    tool_groups = build_tool_groups(path, route_pages)
    batch_templates = build_batch_templates(path, route_pages)
    orders = build_orders(batch_templates, batches_per_order)
    return batch_templates, orders, tool_groups


def main(
    run_simulation: bool = True,
) -> tuple[list[Batch], list[Order], list[ToolGroup], Simulation]:
    batch_templates, orders, tool_groups = build_simulation_inputs()
    simulation = Simulation(tool_groups, orders)
    if run_simulation:
        simulation.run()
    return batch_templates, orders, tool_groups, simulation


if __name__ == "__main__":
    main()
