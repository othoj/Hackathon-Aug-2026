from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from heapq import heappop, heappush
from itertools import count
import json
from pathlib import Path
from random import Random

import pandas as pd

2
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
MINUTES_PER_WEEK = 7 * 24 * 60
DEFAULT_RELEASE_HORIZON_MINUTES = MINUTES_PER_WEEK
PRODUCTION_REGULAR_RELEASE_INTERVAL = MINUTES_PER_WEEK / 39
PRODUCTION_HOT_RELEASE_INTERVAL = MINUTES_PER_WEEK
PRODUCTION_WAFERS_PER_LOT = 25
ENGINEERING_RELEASE_OFFSETS = (8 * 60, 2 * 24 * 60 + 8 * 60)
ENGINEERING_LOTS_PER_RELEASE = 40
ENGINEERING_HOT_LOTS_PER_RELEASE = 8
DEFAULT_RANDOM_SEED = 2020
DEFAULT_MILP_INPUT_PATH = Path(__file__).resolve().parent / "milp_inputs.json"
DEFAULT_MILP_RESULT_PATH = Path(__file__).resolve().parent / "milp_results.json"
MILP_RESULT_SCHEMA_VERSION = 1
REQUIRED_ROUTE_COLUMNS = {
    "STEP",
    "TOOLGROUP",
    "PROCESSING UNIT",
    "MEAN",
    "OFFSET",
    "SETUP",
    "WHEN",
}


@dataclass(frozen=True)
class MILPScheduleEvent:
    """One nonzero processing assignment or setup start from a MILP solution."""

    time_bucket: int
    start_minute: float
    event_type: str
    group: str
    machine: int
    end_minute: float
    flow_id: str | None = None
    route_step: int | None = None
    quantity: float | None = None
    from_setup: str | None = None
    to_setup: str | None = None

    @property
    def details(self) -> str:
        if self.event_type == "PROCESS":
            return (
                f"{self.flow_id}, step {self.route_step}: "
                f"{self.quantity:.6g} wafers"
            )
        return (
            f"{self.from_setup} -> {self.to_setup} "
            f"(ends at {self.end_minute:.1f} min)"
        )


@dataclass(frozen=True)
class MILPResult:
    """Serializable solver summary and chronological production schedule."""

    status: str
    has_solution: bool
    objective: float | None
    runtime_seconds: float
    mip_gap: float | None
    bucket_minutes: int
    horizon_hours: int
    total_arrivals: float
    total_completed: float | None
    final_queue: float | None
    events: tuple[MILPScheduleEvent, ...] = ()
    schema_version: int = MILP_RESULT_SCHEMA_VERSION

    def summary_lines(self) -> tuple[str, ...]:
        objective = "n/a" if self.objective is None else f"{self.objective:.6g}"
        completed = (
            "n/a" if self.total_completed is None else f"{self.total_completed:.6g}"
        )
        final_queue = "n/a" if self.final_queue is None else f"{self.final_queue:.6g}"
        gap = "n/a" if self.mip_gap is None else f"{100 * self.mip_gap:.3g}%"
        return (
            f"Status: {self.status}",
            f"Objective: {objective}",
            f"Runtime: {self.runtime_seconds:.2f} seconds | MIP gap: {gap}",
            f"Arrivals: {self.total_arrivals:.6g} wafers | Completed: {completed}",
            f"Final queue: {final_queue} wafers | Schedule events: {len(self.events):,}",
        )


def save_milp_result(
    result: MILPResult,
    path: Path | str = DEFAULT_MILP_RESULT_PATH,
) -> Path:
    """Atomically save a MILP result for the GUI and other clients."""

    destination = Path(path)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    temporary.replace(destination)
    return destination


def load_milp_result(
    path: Path | str = DEFAULT_MILP_RESULT_PATH,
    *,
    missing_ok: bool = False,
) -> MILPResult | None:
    """Load and validate the schedule written by ``MILP.py``."""

    source = Path(path)
    if missing_ok and not source.exists():
        return None
    document = json.loads(source.read_text(encoding="utf-8"))
    version = int(document.get("schema_version", 0))
    if version != MILP_RESULT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported MILP result schema {version}; "
            f"expected {MILP_RESULT_SCHEMA_VERSION}"
        )
    events = tuple(MILPScheduleEvent(**event) for event in document.pop("events", []))
    return MILPResult(events=events, **document)


def format_milp_result(result: MILPResult) -> str:
    """Return a human-readable summary and time-ordered schedule."""

    lines = ["MILP RESULTS", *result.summary_lines(), "", "SCHEDULE"]
    if not result.events:
        lines.append("No processing or setup events were produced.")
    for event in result.events:
        machine = f"{event.group}/{event.machine}"
        lines.append(
            f"T+{event.start_minute:08.1f} min | {event.event_type:7} | "
            f"{machine:30} | {event.details}"
        )
    return "\n".join(lines)


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
    """One wafer lot progressing through a product route.

    The class name is retained for compatibility, but an instance represents
    one production or engineering lot, not a diffusion-machine batch.
    """

    def __init__(
        self,
        id: str,
        steps: list[Step],
        *,
        lot_id: str | None = None,
        number_of_wafers: int = PRODUCTION_WAFERS_PER_LOT,
        release_time: float = 0.0,
        priority: int = 10,
        is_hot: bool = False,
        is_engineering: bool = False,
    ):
        if not steps:
            raise ValueError("A batch must contain at least one step")
        if number_of_wafers <= 0:
            raise ValueError("number_of_wafers must be positive")
        if release_time < 0:
            raise ValueError("release_time cannot be negative")
        self.id = id
        self.lot_id = lot_id if lot_id is not None else id
        self.steps = steps
        self.current_step_index = 0
        self.total_number_of_steps = len(steps)
        self.finished = False
        self.time_stamp = 0.0
        self.number_of_wafers = int(number_of_wafers)
        self.release_time = float(release_time)
        self.priority = int(priority)
        self.is_hot = is_hot
        self.is_engineering = is_engineering

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

    @property
    def cycle_time(self) -> float | None:
        if not self.finished:
            return None
        return self.time_stamp - self.release_time

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

    def clone(
        self,
        *,
        lot_id: str | None = None,
        number_of_wafers: int | None = None,
        release_time: float | None = None,
        priority: int | None = None,
        is_hot: bool | None = None,
        is_engineering: bool | None = None,
    ) -> Batch:
        return Batch(
            self.id,
            [step.clone() for step in self.steps],
            lot_id=lot_id if lot_id is not None else self.lot_id,
            number_of_wafers=(
                self.number_of_wafers
                if number_of_wafers is None
                else number_of_wafers
            ),
            release_time=(
                self.release_time if release_time is None else release_time
            ),
            priority=self.priority if priority is None else priority,
            is_hot=self.is_hot if is_hot is None else is_hot,
            is_engineering=(
                self.is_engineering
                if is_engineering is None
                else is_engineering
            ),
        )

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
    """Use the worksheet time directly, without wafer or lot multipliers."""
    return float(row["MEAN"])


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


def _release_times(interval: float, horizon: float) -> list[float]:
    release_times: list[float] = []
    release_time = 0.0
    while release_time < horizon - 1e-9:
        release_times.append(release_time)
        release_time += interval
    return release_times


def _production_lots(template: Batch, horizon: float) -> list[Batch]:
    product_number = template.id.removeprefix(ROUTE_SHEET_PREFIX)
    lots: list[Batch] = []
    for lot_number, release_time in enumerate(
        _release_times(PRODUCTION_REGULAR_RELEASE_INTERVAL, horizon), 1
    ):
        lots.append(
            template.clone(
                lot_id=f"Regular_Lot_{product_number}_{lot_number}",
                number_of_wafers=PRODUCTION_WAFERS_PER_LOT,
                release_time=release_time,
                priority=10,
                is_hot=False,
                is_engineering=False,
            )
        )
    for lot_number, release_time in enumerate(
        _release_times(PRODUCTION_HOT_RELEASE_INTERVAL, horizon), 1
    ):
        lots.append(
            template.clone(
                lot_id=f"HotLot_{product_number}_{lot_number}",
                number_of_wafers=PRODUCTION_WAFERS_PER_LOT,
                release_time=release_time,
                priority=20,
                is_hot=True,
                is_engineering=False,
            )
        )
    return sorted(lots, key=lambda lot: lot.release_time)


def _engineering_lots_by_route(
    templates_by_id: dict[str, Batch],
    horizon: float,
    random_seed: int,
) -> dict[str, list[Batch]]:
    route_ids = [f"{ROUTE_SHEET_PREFIX}E{i}" for i in range(1, 4)]
    lots_by_route = {route_id: [] for route_id in route_ids}
    lot_numbers = {route_id: 0 for route_id in route_ids}
    hot_lot_numbers = {route_id: 0 for route_id in route_ids}
    random = Random(random_seed)
    week_number = 0

    while week_number * MINUTES_PER_WEEK < horizon:
        week_start = week_number * MINUTES_PER_WEEK
        for release_index, release_offset in enumerate(
            ENGINEERING_RELEASE_OFFSETS
        ):
            release_time = week_start + release_offset
            if release_time >= horizon:
                continue
            for index in range(ENGINEERING_LOTS_PER_RELEASE):
                route_index = (
                    week_number * 2 * ENGINEERING_LOTS_PER_RELEASE
                    + release_index * ENGINEERING_LOTS_PER_RELEASE
                    + index
                ) % len(route_ids)
                route_id = route_ids[route_index]
                template = templates_by_id[route_id]
                is_hot = index < ENGINEERING_HOT_LOTS_PER_RELEASE
                counters = hot_lot_numbers if is_hot else lot_numbers
                counters[route_id] += 1
                product_number = route_id.removeprefix(
                    f"{ROUTE_SHEET_PREFIX}E"
                )
                lot_type = (
                    "Engineering_HotLot" if is_hot else "Engineering_Lot"
                )
                lots_by_route[route_id].append(
                    template.clone(
                        lot_id=(
                            f"{lot_type}_{product_number}_"
                            f"{counters[route_id]}"
                        ),
                        number_of_wafers=random.randint(1, 10),
                        release_time=release_time,
                        priority=20 if is_hot else 10,
                        is_hot=is_hot,
                        is_engineering=True,
                    )
                )
        week_number += 1
    return lots_by_route


def build_orders(
    batch_templates: list[Batch],
    release_horizon_minutes: float = DEFAULT_RELEASE_HORIZON_MINUTES,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> list[Order]:
    """Create dataset-4 production and engineering lot releases.

    Production lots follow the workbook constant release intervals.
    Engineering lots are released Monday and Wednesday at 08:00, total 80
    per complete week, with 20 percent hot lots and 1--10 wafers.
    """
    if release_horizon_minutes <= 0:
        raise ValueError("release_horizon_minutes must be positive")

    templates_by_id = {template.id: template for template in batch_templates}
    engineering_lots = _engineering_lots_by_route(
        templates_by_id, release_horizon_minutes, random_seed
    )
    orders: list[Order] = []
    for template in batch_templates:
        if template.id.startswith(f"{ROUTE_SHEET_PREFIX}E"):
            lots = engineering_lots[template.id]
        else:
            lots = _production_lots(template, release_horizon_minutes)
        orders.append(Order(template.id, lots))
    return orders


class Simulation:
    """Discrete-event scheduler for the configured machines and lot releases."""

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
        self._releases: list[tuple[float, int, Batch]] = []
        self._event_ids = count()
        self._load_release_events()

    def _load_release_events(self) -> None:
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
                heappush(
                    self._releases,
                    (batch.release_time, next(self._event_ids), batch),
                )

    def _release_lots(self) -> set[str]:
        affected_tool_groups: set[str] = set()
        while self._releases and self._releases[0][0] <= self.global_timer:
            _, _, batch = heappop(self._releases)
            step = batch.current_step
            if step is None:
                continue
            self._ready[step.tool_group_needed].append(batch)
            affected_tool_groups.add(step.tool_group_needed)
        return affected_tool_groups

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

    @property
    def is_finished(self) -> bool:
        """Whether every batch in the simulation has completed."""
        return self.completed_batches == self.total_batches

    @property
    def busy_machine_count(self) -> int:
        """Number of machines currently processing a batch."""
        return len(self._busy)

    @property
    def waiting_batch_count(self) -> int:
        """Number of released batches waiting for a machine."""
        return sum(len(batches) for batches in self._ready.values())

    @property
    def unreleased_batch_count(self) -> int:
        """Number of batches whose scheduled release time has not arrived."""
        return len(self._releases)

    @property
    def idle_machine_count(self) -> int:
        """Number of machines immediately available for work."""
        return sum(len(machines) for machines in self._open_machines.values())

    def tool_group_activity(self) -> dict[str, tuple[int, int, int]]:
        """Return idle-machine, busy-machine, and waiting-batch counts by group."""
        busy_by_group = {group_id: 0 for group_id in self.tool_groups}
        for _, _, machine in self._busy:
            busy_by_group[machine.tool_group.id] += 1
        return {
            group_id: (
                len(self._open_machines[group_id]),
                busy_by_group[group_id],
                len(self._ready[group_id]),
            )
            for group_id in self.tool_groups
        }

    def advance(self, minutes: float) -> float:
        """Advance the simulation by up to ``minutes`` of simulated time.

        Events are still processed in chronological order.  This lets an
        interface animate the event-driven simulation without changing its
        scheduling rules.
        """
        if minutes < 0:
            raise ValueError("minutes must be non-negative")

        target_time = self.global_timer + float(minutes)
        while self._busy or self._releases:
            next_completion = self._busy[0][0] if self._busy else float("inf")
            next_release = (
                self._releases[0][0] if self._releases else float("inf")
            )
            next_event_time = min(next_completion, next_release)
            if next_event_time > target_time:
                break

            self.global_timer = next_event_time
            affected_tool_groups = self._release_lots()
            while self._busy and self._busy[0][0] <= self.global_timer:
                _, _, machine = heappop(self._busy)
                affected_tool_groups.update(self._complete_machine(machine))
            for tool_group_id in affected_tool_groups:
                self._dispatch(tool_group_id)

        # Preserve the time at the final completion event instead of advancing
        # past the end of the simulation.
        if self._busy or self._releases:
            self.global_timer = target_time
        elif not self.is_finished:
            blocked = {
                group_id: len(batches)
                for group_id, batches in self._ready.items()
                if batches
            }
            raise RuntimeError(f"Simulation stopped with blocked batches: {blocked}")
        return self.global_timer

    def run(self) -> float:
        """Release scheduled lots, drain them, and return elapsed minutes."""
        while self._busy or self._releases:
            next_completion = self._busy[0][0] if self._busy else float("inf")
            next_release = (
                self._releases[0][0] if self._releases else float("inf")
            )
            self.global_timer = min(next_completion, next_release)
            affected_tool_groups = self._release_lots()
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
    release_horizon_minutes: float = DEFAULT_RELEASE_HORIZON_MINUTES,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> tuple[list[Batch], list[Order], list[ToolGroup]]:
    """Build templates, scheduled orders, and tool groups."""
    path = Path(xlsx_path)
    route_pages = _read_route_pages(path)
    tool_groups = build_tool_groups(path, route_pages)
    batch_templates = build_batch_templates(path, route_pages)
    orders = build_orders(
        batch_templates, release_horizon_minutes, random_seed
    )
    return batch_templates, orders, tool_groups


def main(
    run_simulation: bool = True,
    release_horizon_minutes: float = DEFAULT_RELEASE_HORIZON_MINUTES,
    random_seed: int = DEFAULT_RANDOM_SEED,
) -> tuple[list[Batch], list[Order], list[ToolGroup], Simulation]:
    batch_templates, orders, tool_groups = build_simulation_inputs(
        release_horizon_minutes=release_horizon_minutes,
        random_seed=random_seed,
    )
    simulation = Simulation(tool_groups, orders)
    if run_simulation:
        simulation.run()
    print(simulation.global_timer)
    return batch_templates, orders, tool_groups, simulation


if __name__ == "__main__":
    main()
