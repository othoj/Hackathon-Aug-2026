"""A polished single-window Tkinter application."""

import tkinter as tk
from tkinter import font as tkfont


class SimpleApp:
    """Single-window layout with a header and window-style controls."""

    COLORS = {
        "bg": "#f0f2f5",
        "surface": "#ffffff",
        "header": "#111827",
        "header_text": "#f9fafb",
        "border": "#d1d5db",
        "border_light": "#e5e7eb",
        "text": "#111827",
        "text_muted": "#6b7280",
        "accent": "#2563eb",
        "accent_hover": "#1d4ed8",
        "title_bar": "#f3f4f6",
        "dot_red": "#ef4444",
        "dot_yellow": "#f59e0b",
        "dot_green": "#22c55e",
    }

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("AI Coding")
        self.root.geometry("1100x720")
        self.root.minsize(960, 600)
        self.root.configure(bg=self.COLORS["bg"])
        self._setup_fonts()
        self._build_ui()

    def _setup_fonts(self) -> None:
        family = "Segoe UI" if "Segoe UI" in tkfont.families() else "DejaVu Sans"
        self.font_title = tkfont.Font(family=family, size=15, weight="bold")
        self.font_body = tkfont.Font(family=family, size=11)
        self.font_small = tkfont.Font(family=family, size=9)
        self.font_button = tkfont.Font(family=family, size=10, weight="bold")
        self.font_window_title = tkfont.Font(family=family, size=8)

    def _build_ui(self) -> None:
        self._build_header()
        self._build_toolbar()
        self._build_main()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=self.COLORS["header"], height=60)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="AI Coding",
            font=self.font_title,
            bg=self.COLORS["header"],
            fg=self.COLORS["header_text"],
        ).pack(side="left", padx=28, pady=16)

        tk.Label(
            header,
            text="Workspace",
            font=self.font_small,
            bg=self.COLORS["header"],
            fg="#9ca3af",
        ).pack(side="left", pady=16)

    def _build_toolbar(self) -> None:
        toolbar = tk.Frame(self.root, bg=self.COLORS["surface"], height=52)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        divider = tk.Frame(self.root, bg=self.COLORS["border_light"], height=1)
        divider.pack(fill="x")

        self._flat_button(
            toolbar,
            text="New Session",
            font=self.font_button,
            bg=self.COLORS["accent"],
            fg="#ffffff",
            active_bg=self.COLORS["accent_hover"],
            padx=18,
            pady=8,
        ).pack(side="right", padx=28, pady=10)

    def _build_main(self) -> None:
        main = tk.Frame(self.root, bg=self.COLORS["bg"])
        main.pack(fill="both", expand=True, padx=48, pady=36)

        tk.Label(
            main,
            text="Open a window",
            font=self.font_title,
            bg=self.COLORS["bg"],
            fg=self.COLORS["text"],
        ).pack(anchor="w")

        tk.Label(
            main,
            text="Choose one of the workspace windows below.",
            font=self.font_body,
            bg=self.COLORS["bg"],
            fg=self.COLORS["text_muted"],
        ).pack(anchor="w", pady=(6, 28))

        windows = tk.Frame(main, bg=self.COLORS["bg"])
        windows.pack(fill="both", expand=True)

        window_grid = (
            ("Active Jobs", "Manage Orders", "Schedule"),
            ("Machines", "Product Routes", None),
        )

        for row_index, row in enumerate(window_grid):
            windows.grid_rowconfigure(row_index, weight=1)
            for col_index, title in enumerate(row):
                windows.grid_columnconfigure(col_index, weight=1)
                if title is None:
                    continue
                cell = tk.Frame(windows, bg=self.COLORS["bg"])
                cell.grid(row=row_index, column=col_index, padx=12, pady=12, sticky="n")
                self._window_button(cell, title).pack()

    def _flat_button(
        self,
        parent: tk.Misc,
        *,
        text: str,
        font: tkfont.Font,
        bg: str,
        fg: str,
        active_bg: str,
        padx: int,
        pady: int,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            font=font,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=padx,
            pady=pady,
            cursor="hand2",
            command=lambda: None,
        )

    def _window_button(self, parent: tk.Misc, title: str) -> tk.Frame:
        shell = tk.Frame(parent, bg=self.COLORS["border"], padx=1, pady=1)

        card = tk.Frame(shell, bg=self.COLORS["surface"], width=220, height=140)
        card.pack()
        card.pack_propagate(False)

        title_bar = tk.Frame(card, bg=self.COLORS["title_bar"], height=30)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)

        controls = tk.Frame(title_bar, bg=self.COLORS["title_bar"])
        controls.pack(side="left", padx=8, pady=8)
        for color in (
            self.COLORS["dot_red"],
            self.COLORS["dot_yellow"],
            self.COLORS["dot_green"],
        ):
            dot = tk.Canvas(
                controls,
                width=8,
                height=8,
                bg=self.COLORS["title_bar"],
                highlightthickness=0,
                borderwidth=0,
            )
            dot.pack(side="left", padx=2)
            dot.create_oval(1, 1, 7, 7, fill=color, outline=color)

        tk.Label(
            title_bar,
            text=title,
            font=self.font_window_title,
            bg=self.COLORS["title_bar"],
            fg=self.COLORS["text_muted"],
        ).pack(side="left", padx=2)

        body = tk.Frame(card, bg=self.COLORS["surface"])
        body.pack(fill="both", expand=True, padx=14, pady=14)

        for width in (110, 80, 125):
            tk.Frame(
                body,
                bg=self.COLORS["border_light"],
                width=width,
                height=5,
            ).pack(anchor="w", pady=3)

        self._bind_clickable(shell, lambda: None)
        return shell

    def _bind_clickable(self, widget: tk.Misc, command) -> None:
        widget.bind("<Button-1>", lambda _event: command())
        widget.configure(cursor="hand2")
        for child in widget.winfo_children():
            self._bind_clickable(child, command)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    SimpleApp().run()


if __name__ == "__main__":
    main()
