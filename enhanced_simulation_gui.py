"""Multi-page control panel for the semiconductor manufacturing simulation.

Run with ``python enhanced_simulation_gui.py``.  This leaves
``simulation_gui.py`` unchanged.
"""

from __future__ import annotations

from collections import deque
from heapq import heappush
import tkinter as tk
from tkinter import messagebox, ttk

from ClassSetup import Batch, Order, Simulation, build_simulation_inputs


SIMULATED_MINUTES_PER_SECOND = 60
TICK_MILLISECONDS = 1_000


class ControllableSimulation(Simulation):
    """Simulation variant that can keep selected machines out of service."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.broken_machine_ids: set[str] = set()

    def set_machine_working(self, machine_id: str, working: bool) -> None:
        """Change an idle machine's availability (busy machines stop after work)."""
        if working:
            self.broken_machine_ids.discard(machine_id)
            for group_id, group in self.tool_groups.items():
                for machine in group.all_machines():
                    if machine.id == machine_id and not machine.queue:
                        if machine not in self._open_machines[group_id]:
                            self._open_machines[group_id].append(machine)
                        self._dispatch(group_id)
                        return
            return

        self.broken_machine_ids.add(machine_id)
        for available in self._open_machines.values():
            for machine in tuple(available):
                if machine.id == machine_id:
                    available.remove(machine)
                    return

    def _dispatch(self, tool_group_id: str) -> None:
        available = self._open_machines[tool_group_id]
        usable = deque(
            machine for machine in available if machine.id not in self.broken_machine_ids
        )
        self._open_machines[tool_group_id] = usable
        super()._dispatch(tool_group_id)

    def _complete_machine(self, machine: object) -> set[str]:
        affected = super()._complete_machine(machine)  # type: ignore[arg-type]
        if machine.id in self.broken_machine_ids:  # type: ignore[union-attr]
            self._open_machines[machine.tool_group.id].remove(machine)  # type: ignore[union-attr]
        return affected

    def add_batch(self, batch: Batch) -> None:
        """Add a newly created lot while retaining the current simulation state."""
        if batch.current_step is None:
            return
        order = next((item for item in self.orders if item.id == batch.id), None)
        if order is None:
            order = Order(batch.id, [])
            self.orders.append(order)
        order.batches.append(batch)
        self.total_batches += 1
        heappush(self._releases, (batch.release_time, next(self._event_ids), batch))


class SimulationControlWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Semiconductor Manufacturing Control Panel")
        self.root.minsize(820, 600)
        self.templates, orders, groups = build_simulation_inputs()
        self.simulation = ControllableSimulation(groups, orders)
        self.is_playing = False
        self._timer_id: str | None = None
        self._time_text = tk.StringVar()
        self._status_text = tk.StringVar()
        self._completed_text = tk.StringVar()
        self._activity_text = tk.StringVar()
        self.content = ttk.Frame(root, padding=18)
        self.content.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.show_home()

    def _clear(self) -> None:
        self.pause()
        # The simulation widgets are page-specific.  Do not try to refresh a
        # Treeview after its page has been destroyed.
        if hasattr(self, "machine_table"):
            del self.machine_table
        for child in self.content.winfo_children():
            child.destroy()
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(1, weight=1)

    def _page_title(self, title: str) -> None:
        top = ttk.Frame(self.content)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        top.columnconfigure(1, weight=1)
        ttk.Button(top, text="← Back", command=self.show_home).grid(row=0, column=0)
        ttk.Label(top, text=title, font=("TkDefaultFont", 16, "bold")).grid(
            row=0, column=1, sticky="w", padx=12
        )

    def show_home(self) -> None:
        self._clear()
        ttk.Label(
            self.content, text="Semiconductor Manufacturing", font=("TkDefaultFont", 20, "bold")
        ).grid(row=0, column=0, pady=(40, 8))
        ttk.Label(self.content, text="Choose an operation").grid(row=1, column=0, pady=(0, 20))
        buttons = (
            ("Completed jobs", self.show_completed_jobs),
            ("Add order", self.show_add_order),
            ("Create schedule", self.show_schedule),
            ("Simulate", self.show_simulation),
            ("Breakdowns", self.show_breakdowns),
        )
        menu = ttk.Frame(self.content)
        menu.grid(row=2, column=0)
        for row, (label, command) in enumerate(buttons):
            ttk.Button(menu, text=label, command=command, width=28).grid(
                row=row, column=0, pady=5, sticky="ew"
            )

    def show_completed_jobs(self) -> None:
        self._clear(); self._page_title("Completed jobs")
        table = self._table(("lot", "route", "wafers", "completed", "cycle"))
        headings = ("Lot", "Route", "Wafers", "Completed at", "Cycle time (hours)")
        self._headings(table, headings)
        for order in self.simulation.orders:
            for batch in order.batches:
                if batch.finished:
                    table.insert("", "end", values=(batch.lot_id, order.id, batch.number_of_wafers,
                        f"{batch.time_stamp:.1f}", f"{(batch.cycle_time or 0) / 60:.2f}"))

    def show_add_order(self) -> None:
        self._clear(); self._page_title("Add order")
        form = ttk.Frame(self.content); form.grid(row=1, column=0, sticky="nw")
        route = tk.StringVar(value=self.templates[0].id)
        wafers = tk.StringVar(value="25")
        release = tk.StringVar(value=str(int(self.simulation.global_timer)))
        priority = tk.StringVar(value="10")
        fields = (("Product route", route), ("Wafers", wafers), ("Release time (minutes)", release), ("Priority", priority))
        for row, (label, value) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            if label == "Product route":
                ttk.Combobox(form, textvariable=value, values=[item.id for item in self.templates], state="readonly", width=34).grid(row=row, column=1, padx=10, pady=5)
            else:
                ttk.Entry(form, textvariable=value, width=18).grid(row=row, column=1, sticky="w", padx=10, pady=5)

        def add() -> None:
            try:
                template = next(item for item in self.templates if item.id == route.get())
                lot_id = f"{template.id}-manual-{self.simulation.total_batches + 1}"
                batch = template.clone(lot_id=lot_id, number_of_wafers=int(wafers.get()),
                    release_time=float(release.get()), priority=int(priority.get()))
                if batch.release_time < self.simulation.global_timer:
                    batch.release_time = self.simulation.global_timer
                self.simulation.add_batch(batch)
                messagebox.showinfo("Order added", f"Added {lot_id}.")
                self.show_schedule()
            except (StopIteration, ValueError) as error:
                messagebox.showerror("Invalid order", str(error))
        ttk.Button(form, text="Add order", command=add).grid(row=len(fields), column=1, sticky="w", padx=10, pady=14)

    def show_schedule(self) -> None:
        self._clear(); self._page_title("Schedule")
        ttk.Label(self.content, text="Lots are shown in their release order. Use Add order to change the schedule.").grid(row=1, column=0, sticky="w", pady=(0, 8))
        table = self._table(("release", "lot", "route", "priority", "status"), row=2)
        self._headings(table, ("Release", "Lot", "Route", "Priority", "Status"))
        batches = [batch for order in self.simulation.orders for batch in order.batches]
        for batch in sorted(batches, key=lambda item: (item.release_time, item.lot_id)):
            status = "Completed" if batch.finished else ("Released" if batch.release_time <= self.simulation.global_timer else "Planned")
            table.insert("", "end", values=(f"{batch.release_time:.1f}", batch.lot_id, batch.id, batch.priority, status))

    def show_breakdowns(self) -> None:
        self._clear(); self._page_title("Machine breakdowns")
        ttk.Label(self.content, text="Uncheck a machine to take it out of service. Busy machines finish their current lot first.").grid(row=1, column=0, sticky="w", pady=(0, 8))
        outer = ttk.Frame(self.content); outer.grid(row=2, column=0, sticky="nsew")
        canvas = tk.Canvas(outer, highlightthickness=0); scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas); body.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw"); canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew"); scrollbar.grid(row=0, column=1, sticky="ns"); outer.columnconfigure(0, weight=1); outer.rowconfigure(0, weight=1)
        row = 0
        for group in self.simulation.tool_groups.values():
            ttk.Label(body, text=group.id, font=("TkDefaultFont", 10, "bold")).grid(row=row, column=0, sticky="w", pady=(9, 2)); row += 1
            for machine in group.all_machines():
                working = tk.BooleanVar(value=machine.id not in self.simulation.broken_machine_ids)
                ttk.Checkbutton(body, text=f"{machine.id} — working", variable=working,
                    command=lambda mid=machine.id, var=working: self.simulation.set_machine_working(mid, var.get())).grid(row=row, column=0, sticky="w", padx=16); row += 1

    def show_simulation(self) -> None:
        self._clear(); self._page_title("Simulation")
        summary = ttk.LabelFrame(self.content, text="Simulation clock", padding=12); summary.grid(row=1, column=0, sticky="ew")
        for row, variable in enumerate((self._time_text, self._status_text, self._completed_text, self._activity_text)):
            ttk.Label(summary, textvariable=variable, font=("TkDefaultFont", 14) if row == 0 else None).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 5, 0))
        self.progress = ttk.Progressbar(self.content, mode="determinate"); self.progress.grid(row=2, column=0, sticky="ew", pady=10)
        controls = ttk.Frame(self.content); controls.grid(row=3, column=0, sticky="w", pady=(0, 12))
        self.play_button = ttk.Button(controls, text="Play", command=self.play); self.play_button.grid(row=0, column=0)
        self.pause_button = ttk.Button(controls, text="Pause", command=self.pause); self.pause_button.grid(row=0, column=1, padx=8)
        ttk.Button(controls, text="Restart", command=self.restart).grid(row=0, column=2)
        ttk.Label(controls, text="Speed: 1 simulated work hour / real second").grid(row=0, column=3, padx=18)
        self.machine_table = self._table(("group", "idle", "busy", "waiting"), row=4, title="Tool-group activity")
        self._headings(self.machine_table, ("Tool group", "Idle machines", "Busy machines", "Waiting batches"))
        self._refresh_display()

    def _table(self, columns: tuple[str, ...], row: int = 1, title: str | None = None) -> ttk.Treeview:
        parent: tk.Misc = self.content
        if title:
            frame = ttk.LabelFrame(self.content, text=title, padding=8); frame.grid(row=row, column=0, sticky="nsew"); frame.columnconfigure(0, weight=1); frame.rowconfigure(0, weight=1); parent = frame
        table = ttk.Treeview(parent, columns=columns, show="headings")
        table.grid(row=0 if title else row, column=0, sticky="nsew")
        return table

    @staticmethod
    def _headings(table: ttk.Treeview, headings: tuple[str, ...]) -> None:
        for column, heading in zip(table["columns"], headings):
            table.heading(column, text=heading); table.column(column, width=145, anchor="center")

    def _refresh_display(self) -> None:
        if not hasattr(self, "machine_table"): return
        sim = self.simulation; hours = sim.global_timer / 60
        self._time_text.set(f"Workdays elapsed: {int(hours // 8):,}  |  Total hours passed: {hours:g}")
        self._status_text.set("Completed" if sim.is_finished else ("Playing" if self.is_playing else "Paused"))
        self._completed_text.set(f"Completed batches: {sim.completed_batches:,} of {sim.total_batches:,}")
        self._activity_text.set(f"Busy: {sim.busy_machine_count:,}  |  Idle: {sim.idle_machine_count:,}  |  Waiting: {sim.waiting_batch_count:,}  |  Not released: {sim.unreleased_batch_count:,}")
        self.progress.configure(maximum=max(sim.total_batches, 1), value=sim.completed_batches)
        self.play_button.state(["disabled"] if sim.is_finished else ["!disabled"]); self.pause_button.state(["!disabled"] if self.is_playing else ["disabled"])
        for group_id, values in sim.tool_group_activity().items():
            if self.machine_table.exists(group_id): self.machine_table.item(group_id, values=(group_id, *values))
            else: self.machine_table.insert("", "end", iid=group_id, values=(group_id, *values))

    def play(self) -> None:
        if self.simulation.is_finished: return
        self.is_playing = True; self._refresh_display(); self._schedule_tick()

    def pause(self) -> None:
        self.is_playing = False
        if self._timer_id is not None: self.root.after_cancel(self._timer_id); self._timer_id = None
        if hasattr(self, "machine_table"): self._refresh_display()

    def restart(self) -> None:
        self.pause(); self.templates, orders, groups = build_simulation_inputs(); self.simulation = ControllableSimulation(groups, orders); self._refresh_display()

    def _schedule_tick(self) -> None:
        if self.is_playing and self._timer_id is None: self._timer_id = self.root.after(TICK_MILLISECONDS, self._tick)

    def _tick(self) -> None:
        self._timer_id = None
        if not self.is_playing: return
        try: self.simulation.advance(SIMULATED_MINUTES_PER_SECOND)
        except Exception as error: self.pause(); messagebox.showerror("Simulation error", str(error)); return
        if self.simulation.is_finished: self.is_playing = False
        self._refresh_display(); self._schedule_tick()


def main() -> None:
    root = tk.Tk()
    try: SimulationControlWindow(root)
    except Exception as error:
        root.withdraw(); messagebox.showerror("Could not load simulation", str(error)); root.destroy(); raise
    root.mainloop()


if __name__ == "__main__":
    main()
