"""Tkinter controls and live dashboard for the ClassSetup simulation.

Run with ``python simulation_gui.py``.  The display advances by one simulated
work hour for every second that it remains in the Playing state.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ClassSetup import Simulation, build_simulation_inputs


SIMULATED_MINUTES_PER_SECOND = 60
TICK_MILLISECONDS = 1_000


class SimulationWindow:
    """A live view and set of controls for one ClassSetup Simulation."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Semiconductor Manufacturing Simulation")
        self.root.minsize(760, 560)

        self.simulation = self._new_simulation()
        self.is_playing = False
        self._timer_id: str | None = None

        self._time_text = tk.StringVar()
        self._status_text = tk.StringVar()
        self._completed_text = tk.StringVar()
        self._activity_text = tk.StringVar()
        self._build_layout()
        self._refresh_display()

    @staticmethod
    def _new_simulation() -> Simulation:
        """Create fresh data through the actual ClassSetup input builders."""
        _, orders, tool_groups = build_simulation_inputs()
        return Simulation(tool_groups, orders)

    def _build_layout(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        ttk.Label(
            container,
            text="Semiconductor Manufacturing Simulation",
            font=("TkDefaultFont", 16, "bold"),
        ).grid(row=0, column=0, sticky="w")

        summary = ttk.LabelFrame(container, text="Simulation clock", padding=12)
        summary.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        summary.columnconfigure(0, weight=1)
        ttk.Label(summary, textvariable=self._time_text, font=("TkDefaultFont", 14)).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(summary, textvariable=self._status_text).grid(
            row=1, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Label(summary, textvariable=self._completed_text).grid(
            row=2, column=0, sticky="w", pady=(5, 0)
        )
        ttk.Label(summary, textvariable=self._activity_text).grid(
            row=3, column=0, sticky="w", pady=(5, 0))

        self.progress = ttk.Progressbar(container, mode="determinate")
        self.progress.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        controls = ttk.Frame(container)
        controls.grid(row=3, column=0, sticky="w", pady=12)
        self.play_button = ttk.Button(controls, text="Play", command=self.play)
        self.play_button.grid(row=0, column=0)
        self.pause_button = ttk.Button(controls, text="Pause", command=self.pause)
        self.pause_button.grid(row=0, column=1, padx=8)
        ttk.Button(controls, text="Restart", command=self.restart).grid(row=0, column=2)
        ttk.Label(
            controls,
            text="Speed: 1 simulated work hour / real second",
        ).grid(row=0, column=3, padx=(18, 0))

        machines = ttk.LabelFrame(container, text="Tool-group activity", padding=8)
        machines.grid(row=4, column=0, sticky="nsew")
        machines.columnconfigure(0, weight=1)
        machines.rowconfigure(0, weight=1)

        columns = ("tool_group", "idle", "busy", "waiting")
        self.machine_table = ttk.Treeview(
            machines,
            columns=columns,
            show="headings",
            height=14,
        )
        headings = {
            "tool_group": "Tool group",
            "idle": "Idle machines",
            "busy": "Busy machines",
            "waiting": "Waiting batches",
        }
        for column, heading in headings.items():
            self.machine_table.heading(column, text=heading)
            self.machine_table.column(
                column,
                anchor="center" if column != "tool_group" else "w",
                width=145,
            )
        scrollbar = ttk.Scrollbar(
            machines, orient="vertical", command=self.machine_table.yview
        )
        self.machine_table.configure(yscrollcommand=scrollbar.set)
        self.machine_table.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    @staticmethod
    def _format_time(total_minutes: float) -> str:
        total_hours = max(0.0, total_minutes / 60)
        completed_workdays = int(total_hours // 8)
        hours_into_workday = total_hours % 8
        return (
            f"Workdays elapsed: {completed_workdays:,}  |  "
            f"Current workday: {hours_into_workday:g} of 8 hours  |  "
            f"Total hours passed: {total_hours:g}"
        )

    def _refresh_display(self) -> None:
        simulation = self.simulation
        self._time_text.set(self._format_time(simulation.global_timer))

        if simulation.is_finished:
            status = "Completed"
        elif self.is_playing:
            status = "Playing"
        else:
            status = "Paused"
        self._status_text.set(status)
        self._completed_text.set(
            f"Completed batches: {simulation.completed_batches:,} of "
            f"{simulation.total_batches:,}"
        )
        self._activity_text.set(
            f"Busy: {simulation.busy_machine_count:,}  |  "
            f"Idle: {simulation.idle_machine_count:,}  |  "
            f"Waiting: {simulation.waiting_batch_count:,}  |  "
            f"Not released: {simulation.unreleased_batch_count:,}"
        )
        self.progress.configure(
            maximum=max(simulation.total_batches, 1),
            value=simulation.completed_batches,
        )
        self.play_button.state(["disabled"] if simulation.is_finished else ["!disabled"])
        self.pause_button.state(["!disabled"] if self.is_playing else ["disabled"])

        for group_id, (idle, busy, waiting) in simulation.tool_group_activity().items():
            values = (
                group_id,
                idle,
                busy,
                waiting,
            )
            if self.machine_table.exists(group_id):
                self.machine_table.item(group_id, values=values)
            else:
                self.machine_table.insert("", "end", iid=group_id, values=values)

    def play(self) -> None:
        if self.simulation.is_finished:
            return
        self.is_playing = True
        self._refresh_display()
        self._schedule_tick()

    def pause(self) -> None:
        self.is_playing = False
        if self._timer_id is not None:
            self.root.after_cancel(self._timer_id)
            self._timer_id = None
        self._refresh_display()

    def restart(self) -> None:
        self.pause()
        try:
            self.simulation = self._new_simulation()
        except Exception as error:
            messagebox.showerror("Could not restart simulation", str(error))
            return
        self._refresh_display()

    def _schedule_tick(self) -> None:
        if self.is_playing and self._timer_id is None:
            self._timer_id = self.root.after(TICK_MILLISECONDS, self._tick)

    def _tick(self) -> None:
        self._timer_id = None
        if not self.is_playing:
            return
        try:
            self.simulation.advance(SIMULATED_MINUTES_PER_SECOND)
        except Exception as error:
            self.pause()
            messagebox.showerror("Simulation error", str(error))
            return
        if self.simulation.is_finished:
            self.is_playing = False
        self._refresh_display()
        self._schedule_tick()


def main() -> None:
    root = tk.Tk()
    try:
        SimulationWindow(root)
    except Exception as error:
        root.withdraw()
        messagebox.showerror("Could not load simulation", str(error))
        root.destroy()
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
