from __future__ import annotations

from pathlib import Path

import pandas as pd

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


class State:
    """One possible setup of a tool group."""

    def __init__(self, id: str, machines: list[Machine] | None = None):
        self.id = id
        self.machines = machines if machines is not None else []


class Machine:
    def __init__(self, id):
        self.id = id


class Batch:
    pass


class Order:
    pass


class Step:
    pass


def _route_sheet_names(xlsx_path: Path) -> list[str]:
    """Return every product-route sheet in workbook order."""
    return [
        name
        for name in pd.ExcelFile(xlsx_path).sheet_names
        if name.startswith(ROUTE_SHEET_PREFIX)
    ]


def _states_by_tool_group(xlsx_path: Path) -> dict[str, list[str]]:
    """Collect unique, non-empty SETUP values from all product routes."""
    states: dict[str, list[str]] = {}
    for sheet_name in _route_sheet_names(xlsx_path):
        route = pd.read_excel(
            xlsx_path,
            sheet_name=sheet_name,
            usecols=["TOOLGROUP", "SETUP"],
        )
        for _, row in route.iterrows():
            if pd.isna(row["TOOLGROUP"]) or pd.isna(row["SETUP"]):
                continue
            tool_group_id = str(row["TOOLGROUP"]).strip()
            state_id = str(row["SETUP"]).strip()
            if not tool_group_id or not state_id:
                continue
            tool_group_states = states.setdefault(tool_group_id, [])
            if state_id not in tool_group_states:
                tool_group_states.append(state_id)
    return states


def build_tool_groups(
    xlsx_path: Path | str = DEFAULT_XLSX_PATH,
) -> list[ToolGroup]:
    """Build tool groups, machines, and route-derived states."""
    path = Path(xlsx_path)
    states_by_tool_group = _states_by_tool_group(path)
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
        for state_id in states_by_tool_group.get(tool_group_id, []):
            tool_group.add_state(State(state_id))
        tool_groups.append(tool_group)
    return tool_groups


def main() -> None:
    batch_templates = []
    orders = []
    tool_groups = build_tool_groups()
    _ = batch_templates, orders, tool_groups


if __name__ == "__main__":
    main()
