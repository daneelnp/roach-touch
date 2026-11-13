"""roach-touch control window.

Run:  python3 python/app.py   (or double-click roach-touch.command on the Desktop)
Setup tab: open a session, connect (tick Simulate to practise with no Arduino),
go to rest. Then use the Single touch or Schedule tab.
Enter = the green button of the open tab (ignored while typing in a field).
Esc = ABORT.
"""
import queue
import re
import random
import time
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from runner import TEST_SPINE, Runner
from schedule import (MAX_TOUCHES, VARIED, add_references, add_visits, build_schedule,
                      count_touches, load_schedule, load_spines, save_schedule, touch_seconds)
from session import Session
from stimulator import LEAD_S, FakeStimulator, Stimulator, find_port

HERE = Path(__file__).parent
SPINES_CSV = HERE / "spines.csv"
DATA_ROOT = Path("~/Documents/NeuroProjects/roach-touch-data").expanduser()


def schedules_dir():
    """Saved schedules, shared by all sessions: <data root>/schedules/<name>.csv"""
    d = DATA_ROOT / "schedules"
    d.mkdir(parents=True, exist_ok=True)
    return d

TOUCH_FIELDS = [("amount_us", "push amount (us)", 100), ("push_ms", "push time (ms)", 200),
                ("hold_ms", "hold (ms)", 1000), ("return_ms", "return time (ms)", 300),
                ("gap_s", "rest before push (s)", 10)]

BTN_FONT = ("Helvetica", 17, "bold")
NEXT_FONT = ("Helvetica", 30, "bold")
PHASE_FONT = ("Helvetica", 40, "bold")
DETAIL_FONT = ("Helvetica", 17)

PHASE_COLORS = {"wait": "#e08a00", "move": "#e0453a", "done": "#2fa84f",
                "rest": "#3b82c4", "info": "#888888", "bad": "#e0453a"}
BUTTON_COLORS = {"green": "#2e9d4a", "red": "#d1382c", "amber": "#d98a00", "darkred": "#8e2a22"}


class ColorButton(tk.Label):
    """A coloured button (macOS ignores colours on normal buttons)."""

    def __init__(self, parent, text, color, command, width=16):
        self.color, self.command, self.enabled = BUTTON_COLORS[color], command, True
        super().__init__(parent, text=text, font=BTN_FONT, fg="white", bg=self.color,
                         width=width, padx=14, pady=10, cursor="hand2")
        self.bind("<Button-1>", lambda e: self.enabled and self.command())
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=self._darker()))
        self.bind("<Leave>", lambda e: self.config(bg=self.color if self.enabled else "#9a9a9a"))

    def _darker(self):
        r, g, b = (int(self.color[i:i + 2], 16) for i in (1, 3, 5))
        return "#%02x%02x%02x" % (int(r * .82), int(g * .82), int(b * .82))

    def set(self, text=None, enabled=True):
        self.enabled = enabled
        self.config(bg=self.color if enabled else "#9a9a9a",
                    fg="white" if enabled else "#e6e6e6",
                    cursor="hand2" if enabled else "arrow")
        if text is not None:
            self.config(text=text)


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.spines = load_spines(SPINES_CSV)
        self.session = self.stim = self.runner = None
        self.phase = None              # what the live display is showing
        self.phase_panels = []         # (big label, detail label) in each tab
        self.confirmed_visit = None    # visit number the user said the probe is at
        self.box_bg = self._color("systemTextBackgroundColor", "#2b2b2b")
        root.title("roach-touch")
        root.geometry("1100x860")
        root.minsize(900, 700)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Escape>", lambda e: self.on_abort())
        root.bind("<Return>", self.on_enter)
        self._build()
        self.root.after(100, self._poll)

    def _color(self, name, fallback):
        try:
            self.root.winfo_rgb(name)
            return name
        except tk.TclError:
            return fallback

    # ================================================================ layout
    def _build(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        self._build_header(outer)

        panes = ttk.PanedWindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(8, 0))

        self.tabs = ttk.Notebook(panes)
        setup = ttk.Frame(self.tabs, padding=16)
        self.single_tab = ttk.Frame(self.tabs, padding=14)
        self.sched_tab = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(setup, text="  Setup  ")
        self.tabs.add(self.single_tab, text="  Single touch  ")
        self.tabs.add(self.sched_tab, text="  Schedule  ")
        self._build_setup(setup)
        self._build_single(self.single_tab)
        self._build_schedule(self.sched_tab)
        panes.add(self.tabs, weight=3)

        log = ttk.Frame(panes, padding=(0, 8, 0, 0))
        self._build_log(log)
        panes.add(log, weight=2)

    def _build_header(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        self.session_label = ttk.Label(bar, text="● no session", foreground="gray", font=("Helvetica", 14))
        self.session_label.pack(side="left")
        self.conn_label = ttk.Label(bar, text="● not connected", foreground="gray", font=("Helvetica", 14))
        self.conn_label.pack(side="left", padx=18)
        ColorButton(bar, "ABORT  (Esc)", "red", self.on_abort, width=13).pack(side="right")
        ttk.Button(bar, text="Go to rest", command=self.on_rest).pack(side="right", padx=12)

    def _build_setup(self, tab):
        g = ttk.Frame(tab)
        g.pack(anchor="nw")
        pad = {"padx": 6, "pady": 8, "sticky": "w"}

        ttk.Label(g, text="Session").grid(row=0, column=0, **pad)
        self.session_name = tk.StringVar(value=f"{date.today()}_prep1")
        ttk.Entry(g, textvariable=self.session_name, width=24).grid(row=0, column=1, **pad)
        ttk.Button(g, text="Open", command=self.on_open_session).grid(row=0, column=2, **pad)

        ttk.Label(g, text="Port").grid(row=1, column=0, **pad)
        self.port = tk.StringVar(value=find_port() or "")
        ttk.Entry(g, textvariable=self.port, width=24).grid(row=1, column=1, **pad)
        self.connect_btn = ttk.Button(g, text="Connect", command=self.on_connect)
        self.connect_btn.grid(row=1, column=2, **pad)
        self.simulate = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="Simulate (no Arduino)", variable=self.simulate).grid(row=1, column=3, **pad)

        ttk.Label(g, text="Rest (us)").grid(row=2, column=0, **pad)
        self.rest_us = tk.StringVar(value="1500")
        ttk.Entry(g, textvariable=self.rest_us, width=10).grid(row=2, column=1, **pad)
        ttk.Label(g, text="Move time (ms)").grid(row=3, column=0, **pad)
        self.ramp_ms = tk.StringVar(value="1000")
        ttk.Entry(g, textvariable=self.ramp_ms, width=10).grid(row=3, column=1, **pad)

    def _touch_fields(self, parent, col0=0):
        """Labelled boxes for one set of touch settings. Returns the vars."""
        vars_ = {}
        for i, (key, text, default) in enumerate(TOUCH_FIELDS):
            ttk.Label(parent, text=text).grid(row=0, column=col0 + i, padx=8, sticky="w")
            v = tk.StringVar(value=str(default))
            ttk.Entry(parent, textvariable=v, width=10).grid(row=1, column=col0 + i, padx=8, pady=3, sticky="w")
            vars_[key] = v
        return vars_

    def _phase_panel(self, tab):
        """The big live display; fills the rest of the tab, text centred."""
        box = tk.Frame(tab, bg=self.box_bg)
        box.pack(fill="both", expand=True, pady=(16, 0))
        inner = tk.Frame(box, bg=self.box_bg)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        big = tk.Label(inner, text="", font=PHASE_FONT, bg=self.box_bg, justify="center")
        big.pack()
        detail = tk.Label(inner, text="", font=DETAIL_FONT, bg=self.box_bg, justify="center")
        detail.pack(pady=(4, 0))
        self.phase_panels.append((big, detail))

    def _build_single(self, tab):
        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        ttk.Label(grid, text="spine").grid(row=0, column=0, padx=8, sticky="w")
        self.single_spine = tk.StringVar(value=TEST_SPINE[0])
        ttk.Combobox(grid, textvariable=self.single_spine, state="readonly", width=9,
                     values=[TEST_SPINE[0], *self.spines]).grid(row=1, column=0, padx=8, sticky="w")
        self.single_settings = self._touch_fields(grid, col0=1)

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(14, 0))
        self.single_wait = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="wait for rest", variable=self.single_wait).pack(side="left", padx=8)
        self.send_btn = ColorButton(row, "SEND SINGLE TOUCH", "green", self.on_single_touch, width=20)
        self.send_btn.pack(side="right")
        self._phase_panel(tab)

    def _build_schedule(self, tab):
        btns = ttk.Frame(tab)
        btns.pack(fill="x", padx=8)
        ttk.Button(btns, text="Create schedule…", command=self.on_create_schedule).pack(side="left")
        ttk.Button(btns, text="Load schedule…", command=self.on_load_schedule).pack(side="left", padx=6)
        self.sched_label = ttk.Label(btns, text="no schedule", foreground="gray")
        self.sched_label.pack(side="left", padx=12)

        nxt = ttk.Frame(tab)
        nxt.pack(fill="x", padx=8, pady=(14, 0))
        self.next_label = tk.Label(nxt, text="NEXT: —", font=NEXT_FONT, anchor="w")
        self.next_label.pack(side="left")
        self.progress_label = ttk.Label(nxt, text="", font=("Helvetica", 14))
        self.progress_label.pack(side="left", padx=18)

        run = ttk.Frame(tab)
        run.pack(fill="x", padx=8, pady=(10, 0))
        self.go_btn = ColorButton(run, "No schedule", "green", self.on_go, width=16)
        self.go_btn.set(enabled=False)
        self.go_btn.pack(side="left")
        self.pause_btn = ColorButton(run, "Pause", "amber", self.on_pause, width=9)
        self.pause_btn.pack(side="left", padx=10)
        ColorButton(run, "END SCHEDULE", "darkred", self.on_end_schedule, width=13).pack(side="right")
        self._phase_panel(tab)

    def _build_log(self, parent):
        style = ttk.Style()
        style.configure("Log.Treeview", rowheight=24, font=("Menlo", 12))
        style.configure("Log.Treeview.Heading", font=("Helvetica", 12, "bold"))
        cols = [("trial", "trial", 60), ("time", "time", 80), ("spine", "spine", 80),
                ("block", "block", 80), ("status", "status", 80), ("push", "push", 130),
                ("hold", "hold", 80), ("note", "note", 360)]
        box = ttk.Frame(parent)
        box.pack(fill="both", expand=True)
        self.log = ttk.Treeview(box, columns=[c[0] for c in cols], show="headings", style="Log.Treeview")
        for key, head, width in cols:
            self.log.heading(key, text=head, anchor="w")
            self.log.column(key, width=width, anchor="w", stretch=(key == "note"))
        self.log.tag_configure("info", foreground="gray")
        self.log.tag_configure("aborted", foreground=PHASE_COLORS["wait"])
        self.log.tag_configure("err", foreground=PHASE_COLORS["bad"])
        sb = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        self.log.config(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        fl = ttk.Frame(parent)
        fl.pack(fill="x", pady=(8, 0))
        ttk.Label(fl, text="Flag last trial").pack(side="left")
        self.flag = tk.StringVar(value="bad")
        ttk.Entry(fl, textvariable=self.flag, width=8).pack(side="left", padx=6)
        self.note = tk.StringVar()
        ttk.Entry(fl, textvariable=self.note, width=50).pack(side="left", padx=6)
        ttk.Button(fl, text="Flag", command=self.on_flag).pack(side="left")

    # ================================================================ helpers
    def _say(self, text):
        """An info line in the log (not a trial)."""
        self.log.insert("", 0, values=("", datetime.now().strftime("%H:%M:%S"), "", "", "", "", "", text),
                        tags=("info",))

    def _log_trial(self, d):
        status = d["status"]
        tag = "err" if status == "err" else "aborted" if status == "aborted" else ""
        self.log.insert("", 0, iid=f"t{d['trial']}", tags=(tag,), values=(
            d["trial"], datetime.now().strftime("%H:%M:%S"), d["spine_name"], d.get("block", ""),
            status, f"{d['amount_us']} us / {d['push_ms']} ms", f"{d['hold_ms']} ms", d.get("message", "")))

    def _show_phase(self, big, detail="", color="info"):
        for b, d in self.phase_panels:
            b.config(text=big, fg=PHASE_COLORS[color])
            d.config(text=detail)

    def _status(self, text, color="info"):
        """A plain message (not a touch in progress)."""
        self.phase = None
        self._show_phase(text, "", color)

    def _tick(self):
        """Redraw the live display from the clock (10 times a second)."""
        ph, now = self.phase, time.monotonic()
        if ph is None:
            return
        if ph["kind"] == "resting":
            left = ph["end"] - now
            if left > 0:
                self._show_phase(f"Resting  {left:.1f} s", "", "rest")
        elif ph["kind"] == "press":
            d, el = ph["data"], now - ph["t0"]
            push, hold = d["push_ms"] / 1000, d["hold_ms"] / 1000
            who = f"trial {d['trial']} · {d['spine_name']}"
            if el < LEAD_S:
                self._show_phase(f"Push in {LEAD_S - el:.1f} s", who, "wait")
            elif el < LEAD_S + push:
                self._show_phase("PUSHING", who, "move")
            elif el < LEAD_S + push + hold:
                self._show_phase(f"HOLDING  {LEAD_S + push + hold - el:.1f} s", who, "move")
            else:
                self._show_phase("RETURNING", who, "wait")
        elif ph["kind"] == "done":
            d, left = ph["data"], ph["end"] - now
            if left > 0:
                self._show_phase("Done", f"trial {d['trial']} · {d['spine_name']} · rest {left:.1f} s", "done")
            else:
                self._show_phase("Ready", "", "done")
                self.phase = None

    def _read_settings(self, vars_):
        """Touch settings from one tab, plus the rest position from Setup."""
        try:
            out = {k: int(v.get()) for k, v in vars_.items() if k != "gap_s"}
            out["gap_s"] = float(vars_["gap_s"].get())
            out["rest_us"] = int(self.rest_us.get())
            return out
        except ValueError:
            messagebox.showerror("Settings", "Every setting must be a number.")
            return None

    def _need(self, session=True, connected=True):
        if session and self.session is None:
            messagebox.showwarning("Not ready", "Open a session first (Setup tab).")
            return False
        if connected and self.runner is None:
            messagebox.showwarning("Not ready", "Connect first (Setup tab).")
            return False
        return True

    def _try(self, fn):
        try:
            fn()
        except Exception as e:
            messagebox.showerror("roach-touch", str(e))

    def _show_next(self):
        row = self.runner.next_row() if self.runner else None
        if row is None:
            self.next_label.config(text="NEXT: —")
            self.progress_label.config(text="")
        else:
            n = len(self.runner.schedule)
            visits = self.runner.schedule[-1]["visit"]
            size = sum(1 for r in self.runner.schedule if r["visit"] == row["visit"])
            self.next_label.config(text=f"NEXT: {row['spine_name']}")
            self.progress_label.config(
                text=f"visit {row['visit']}/{visits}   ·   touch {row['touch_in_visit']}/{size}   ·   "
                     f"{self.runner.index + 1}/{n} overall   ·   push {row['push_ms']} ms")
        self._refresh_go()

    def _refresh_go(self):
        """The green schedule button: what it says depends on where we are."""
        row = self.runner.next_row() if self.runner else None
        if not self.runner or not self.runner.schedule:
            self.go_btn.set("No schedule", enabled=False)
        elif row is None:
            self.go_btn.set("Finished", enabled=False)
        elif self.runner.busy:
            self.go_btn.set("Running", enabled=False)
        elif row["visit"] != self.confirmed_visit:
            self.go_btn.set("SPINE MOVED", enabled=True)
        else:
            self.go_btn.set("CONTINUE", enabled=True)

    def _prompt_move(self, row):
        self._status(f"MOVE TO {row['spine_name']}", "wait")
        self._refresh_go()

    def _use_schedule(self, rows, label):
        path = self.session.schedule_path(label)
        save_schedule(rows, path)
        self.runner.set_schedule(rows)
        self.confirmed_visit = None
        visits = self.runner.schedule[-1]["visit"] if rows else 0
        self.sched_label.config(text=f"{label} · {len(rows)} touches · {visits} visits", foreground="")
        self._say(f"schedule: {len(rows)} touches ({path.name})")
        self._show_next()
        if self.runner.next_row():
            self._prompt_move(self.runner.next_row())

    # ================================================================ keys
    def on_enter(self, event):
        """Enter = the green button of the open tab, but never while typing."""
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox)):
            return
        tab = self.tabs.select()
        if tab == str(self.single_tab):
            self.on_single_touch()
        elif tab == str(self.sched_tab) and self.go_btn.enabled:
            self.on_go()

    # ================================================================ setup / header
    def on_open_session(self):
        name = self.session_name.get().strip()
        if not name:
            return
        if self.runner and self.runner.busy:
            messagebox.showwarning("Busy", "Wait for the current action to finish.")
            return
        self.session = Session(DATA_ROOT, name, SPINES_CSV)
        if self.runner:
            self.runner.session = self.session
        self.session_label.config(text=f"● {name}", foreground="green")
        self._say(f"session {name} (next trial {self.session.last_trial + 1})")

    def on_connect(self):
        if not self._need(session=True, connected=False):
            return
        if self.stim:                                   # disconnect
            if self.runner.busy:
                messagebox.showwarning("Busy", "Wait for the current action to finish.")
                return
            self.stim.close()
            self.stim = self.runner = None
            self.connect_btn.config(text="Connect")
            self.conn_label.config(text="● not connected", foreground="gray")
            self._say("disconnected")
            return
        sim = self.simulate.get()
        stim = FakeStimulator() if sim else Stimulator(self.port.get().strip())
        try:
            stim.connect()
        except Exception as e:
            messagebox.showerror("Connect", str(e))
            return
        self.stim = stim
        self.runner = Runner(stim, self.session, self.spines, self._from_runner)
        self.connect_btn.config(text="Disconnect")
        self.conn_label.config(text="● simulated" if sim else "● connected",
                               foreground="#a05a00" if sim else "green")
        self._say("connected (simulated)" if sim else "connected")

    def on_rest(self):
        if not self._need():
            return
        try:
            rest, ramp = int(self.rest_us.get()), int(self.ramp_ms.get())
        except ValueError:
            messagebox.showerror("Rest", "Rest and move time must be numbers.")
            return
        self._try(lambda: self.runner.go_to_rest(rest, ramp))

    def on_abort(self):
        if self.runner:
            self.runner.abort()
            self.pause_btn.set("Pause")
            self._status("Aborting", "bad")

    # ================================================================ single touch
    def on_single_touch(self):
        if not self._need():
            return
        s = self._read_settings(self.single_settings)
        if s:
            self._try(lambda: self.runner.single_touch(s, self.single_spine.get(), self.single_wait.get()))

    # ================================================================ schedule
    def on_load_schedule(self):
        if self._need():
            ScheduleLoader(self)

    def load_schedule_file(self, path):
        self._try(lambda: self._use_schedule(load_schedule(path, self.spines), Path(path).stem))

    def on_create_schedule(self):
        if not self._need() or self._builder_open():
            return
        ScheduleBuilder(self)

    def _builder_open(self):
        win = getattr(self, "builder_win", None)
        if win is not None and win.winfo_exists():
            win.lift()
            return True
        return False

    def on_go(self):
        """SPINE MOVED (start of a visit) or CONTINUE (retry after a stop)."""
        if self._need():
            row = self.runner.next_row()
            self.runner.resume()
            self.pause_btn.set("Pause")
            self._try(self.runner.start_visit)
            if row and self.runner.busy:
                self.confirmed_visit = row["visit"]
            self._refresh_go()

    def on_pause(self):
        if not self.runner:
            return
        if self.runner.pause_evt.is_set():
            self.runner.resume()
            self.pause_btn.set("Pause")
            self._status("")
        else:
            self.runner.pause()
            self.pause_btn.set("Resume")
            self._status("Pausing", "wait")

    def on_end_schedule(self):
        if not self.runner or not self.runner.schedule:
            return
        n, total = self.runner.index, len(self.runner.schedule)
        if not messagebox.askyesno("End schedule", f"{n} of {total} touches done."):
            return
        done, total = self.runner.end_schedule()
        self.confirmed_visit = None
        self.pause_btn.set("Pause")
        self.sched_label.config(text="no schedule", foreground="gray")
        self._say(f"schedule ended at {done}/{total}")
        self._show_next()
        self._status("")

    # ================================================================ flags / close
    def on_flag(self):
        if not self.runner or self.runner.last_trial is None:
            messagebox.showinfo("Flag", "No trial yet in this session.")
            return
        t, flag, note = self.runner.last_trial, self.flag.get(), self.note.get()
        self.session.flag(t, flag, note)
        iid = f"t{t}"
        if self.log.exists(iid):
            self.log.set(iid, "note", f"[{flag}] {note}".strip())
        self.note.set("")

    def on_close(self):
        if self.runner and self.runner.busy:
            if not messagebox.askyesno("Quit", "A touch is running. Abort it and quit?"):
                return
            self.runner.abort()
            self.runner.thread.join(timeout=5)
        if self.stim:
            self.stim.close()
        self.root.destroy()

    # ================================================================ runner -> screen
    def _from_runner(self, kind, data):
        self.events.put((kind, data))      # called from the background thread

    def _poll(self):
        while not self.events.empty():
            kind, d = self.events.get()
            if kind == "status":
                self._status(d, "wait" if d else "info")
            elif kind == "resting":
                self.phase = {"kind": "resting", "end": time.monotonic() + d + LEAD_S}
            elif kind == "sending":
                self.phase = {"kind": "press", "t0": time.monotonic(), "data": d}
            elif kind == "result":
                self._log_trial(d)
                if d["status"] == "done":
                    self.phase = {"kind": "done", "end": time.monotonic() + d["gap_s"], "data": d}
                elif d["status"] == "aborted":
                    self.phase = None
                    self._show_phase("ABORTED", f"trial {d['trial']}", "bad")
                else:
                    self.phase = None
                    self._show_phase("ERROR", d.get("message", ""), "bad")
                self._show_next()
            elif kind == "move":
                self._show_next()
                self._prompt_move(d)
            elif kind == "finished":
                self._status("Finished", "done")
                self._show_next()
            elif kind == "stopped":
                if not (self.runner and self.runner.schedule):
                    pass                      # schedule was ended on purpose
                elif self.phase is None or self.phase["kind"] != "done":
                    self._status("Stopped", "bad")
                self._show_next()
            elif kind == "error":
                self._status("ERROR", "bad")
                messagebox.showerror("roach-touch", d)
            elif kind == "idle":
                self._refresh_go()
        self._tick()
        self.root.after(100, self._poll)



class ScheduleBuilder:
    """The Create schedule pop-up. Remembers what you typed until the app closes."""
    memory = {}

    def __init__(self, app):
        self.app = app
        self.win = app.builder_win = tk.Toplevel(app.root)
        self.win.title("Create schedule")
        self.win.transient(app.root)
        f = self.f = ttk.Frame(self.win, padding=16)
        f.pack(fill="both", expand=True)
        self.v, self.r = {}, 0
        names = {k: t for k, t, _ in TOUCH_FIELDS}
        defaults = {k: d for k, _, d in TOUCH_FIELDS}

        # ---- spines
        self._section("Spines")
        sp = ttk.Frame(f)
        sp.grid(row=self._row(), column=0, columnspan=4, sticky="w")
        for i, name in enumerate(app.spines):
            ttk.Checkbutton(sp, text=name, variable=self._var(f"spine:{name}", True)).grid(
                row=i // 9, column=i % 9, sticky="w", padx=(0, 12))

        # ---- settings
        self._section("Settings")
        r = self._row()
        ttk.Label(f, text="vary").grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="value(s), comma-separated when varied", foreground="gray").grid(
            row=r, column=2, columnspan=2, sticky="w")
        for key in VARIED:
            r = self._row()
            ttk.Label(f, text=names[key]).grid(row=r, column=0, sticky="w", pady=2)
            ttk.Checkbutton(f, variable=self._var(f"vary:{key}", False)).grid(row=r, column=1, sticky="w")
            ttk.Entry(f, textvariable=self._var(f"val:{key}", str(defaults[key])), width=28).grid(
                row=r, column=2, columnspan=2, sticky="w")

        # ---- combine
        self._section("Combine")
        r = self._row()
        ttk.Radiobutton(f, text="every combination", value="combinations",
                        variable=self._var("mode", "combinations")).grid(row=r, column=0, sticky="w")
        ttk.Radiobutton(f, text="random each touch", value="random",
                        variable=self.v["mode"]).grid(row=r, column=2, sticky="w")
        r = self._row()
        self.per_label = ttk.Label(f)
        self.per_label.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Entry(f, textvariable=self._var("per_visit", "5"), width=6).grid(row=r, column=2, sticky="w")
        self._entry_row("rounds (visits per spine)", "rounds", "4", width=6)
        ttk.Checkbutton(f, text="shuffle spine order each round", variable=self._var("shuffle_spines", True)).grid(
            row=self._row(), column=0, columnspan=4, sticky="w")
        self.within_box = ttk.Checkbutton(f, text="shuffle touches within a visit",
                                          variable=self._var("shuffle_within", True))
        self.within_box.grid(row=self._row(), column=0, columnspan=4, sticky="w")

        # ---- reference hit
        self._section("Reference hit")
        ttk.Checkbutton(f, text="add reference hits", variable=self._var("ref_on", False)).grid(
            row=self._row(), column=0, columnspan=4, sticky="w")
        self.ref_widgets = []
        r = self._row()
        self._ref(ttk.Label(f, text="reference spine")).grid(row=r, column=0, sticky="w", pady=2)
        self._ref(ttk.Combobox(f, textvariable=self._var("ref_spine", next(iter(app.spines))),
                               values=list(app.spines), state="readonly", width=8)).grid(row=r, column=2, sticky="w")
        rf = ttk.Frame(f)
        rf.grid(row=self._row(), column=0, columnspan=4, sticky="w", pady=(4, 2))
        for i, key in enumerate(VARIED):
            self._ref(ttk.Label(rf, text=names[key])).grid(row=0, column=i, sticky="w", padx=(0, 10))
            self._ref(ttk.Entry(rf, textvariable=self._var(f"ref:{key}", str(defaults[key])), width=10)).grid(
                row=1, column=i, sticky="w", padx=(0, 10))
        r = self._row()
        self._ref(ttk.Label(f, text="every")).grid(row=r, column=0, sticky="w", pady=2)
        ev = ttk.Frame(f)
        ev.grid(row=r, column=2, columnspan=2, sticky="w")
        self._ref(ttk.Entry(ev, textvariable=self._var("ref_every", "30"), width=6)).pack(side="left")
        self._ref(ttk.Combobox(ev, textvariable=self._var("ref_unit", "touches"), values=["touches", "minutes"],
                               state="readonly", width=8)).pack(side="left", padx=6)
        self._ref(ttk.Label(ev, text="(between visits, plus start and end)", foreground="gray")).pack(side="left")
        r = self._row()
        self._ref(ttk.Label(f, text="touches each time")).grid(row=r, column=0, sticky="w", pady=2)
        self._ref(ttk.Entry(f, textvariable=self._var("ref_count", "3"), width=6)).grid(row=r, column=2, sticky="w")

        # ---- save
        self._section("Save")
        self._entry_row("block name (log column)", "block", "custom")
        self._entry_row("schedule name", "name", "", width=28)
        if not self.v["name"].get():
            self.v["name"].set(f"{self.v['block'].get()}_{date.today():%m%d}")

        # ---- summary + buttons
        self.summary = ttk.Label(f, text="", font=("Helvetica", 13), justify="left")
        self.summary.grid(row=self._row(), column=0, columnspan=4, sticky="w", pady=(14, 6))
        bb = ttk.Frame(f)
        bb.grid(row=self._row(), column=0, columnspan=4, sticky="e")
        ttk.Button(bb, text="Cancel", command=self.win.destroy).pack(side="left", padx=6)
        self.create_btn = ttk.Button(bb, text="Save and load", command=self.create)
        self.create_btn.pack(side="left")

        for var in self.v.values():
            var.trace_add("write", lambda *_: self.update())
        self.update()

    # ---- layout helpers
    def _row(self):
        self.r += 1
        return self.r

    def _section(self, title):
        r = self._row()
        if r > 1:
            ttk.Separator(self.f).grid(row=r, column=0, columnspan=4, sticky="ew", pady=(10, 4))
            r = self._row()
        ttk.Label(self.f, text=title, font=("Helvetica", 13, "bold")).grid(row=r, column=0, sticky="w", pady=(0, 4))

    def _var(self, key, default):
        """A remembered form value: what you typed last time, else default."""
        value = ScheduleBuilder.memory.get(key, default)
        self.v[key] = tk.BooleanVar(value=value) if isinstance(default, bool) else tk.StringVar(value=value)
        return self.v[key]

    def _entry_row(self, label, key, default, width=16):
        r = self._row()
        ttk.Label(self.f, text=label).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Entry(self.f, textvariable=self._var(key, default), width=width).grid(row=r, column=2, columnspan=2, sticky="w")

    def _ref(self, widget):
        self.ref_widgets.append(widget)
        return widget

    def g(self, key):
        return self.v[key].get()

    # ---- reading the form
    def read(self):
        """-> (what to build, None) or (None, error text)."""
        names = {k: t for k, t, _ in TOUCH_FIELDS}
        spines = [n for n in self.app.spines if self.g(f"spine:{n}")]
        if not spines:
            return None, "Pick at least one spine."
        values = {}
        for key in VARIED:
            parts = [p.strip() for p in self.g(f"val:{key}").split(",") if p.strip()]
            if not parts:
                return None, f"{names[key]}: empty."
            if len(parts) > 1 and not self.g(f"vary:{key}"):
                return None, f"{names[key]}: several values, but 'vary' is not ticked."
            try:
                values[key] = [float(p) if key == "gap_s" else int(p) for p in parts]
            except ValueError:
                return None, f"{names[key]}: not a number."
        if min(values["gap_s"]) < LEAD_S:
            return None, f"rest before push must be at least {LEAD_S:.0f} s."
        try:
            per, rounds = int(self.g("per_visit")), int(self.g("rounds"))
            rest_us = int(self.app.rest_us.get())
        except ValueError:
            return None, "Touches per visit, rounds and the rest position must be whole numbers."
        if per < 1 or rounds < 1:
            return None, "Touches per visit and rounds must be at least 1."
        mode = self.g("mode")
        if count_touches(len(spines), values, mode, per, rounds) > MAX_TOUCHES:
            return None, f"More than {MAX_TOUCHES} touches."

        ref = None
        if self.g("ref_on"):
            try:
                settings = {k: float(self.g(f"ref:{k}")) if k == "gap_s" else int(self.g(f"ref:{k}"))
                            for k in VARIED}
                every, count = float(self.g("ref_every")), int(self.g("ref_count"))
            except ValueError:
                return None, "Reference hit: every box must be one number."
            if settings["gap_s"] < LEAD_S:
                return None, f"Reference hit: rest before push must be at least {LEAD_S:.0f} s."
            if every <= 0 or count < 1:
                return None, "Reference hit: 'every' and 'touches each time' must be above 0."
            ref = dict(spine=self.g("ref_spine"), settings={**settings, "rest_us": rest_us},
                       every=every, unit=self.g("ref_unit"), count=count)

        block = self.g("block").strip() or "custom"
        if "," in block:
            return None, "Block name can't contain a comma."
        name = self.g("name").strip()
        if not name:
            return None, "Give the schedule a name."
        if not re.fullmatch(r"[A-Za-z0-9_\- ]+", name):
            return None, "Schedule name: letters, numbers, spaces, - and _ only."
        build = dict(spine_names=spines, values=values, rest_us=rest_us, mode=mode, per_visit=per,
                     rounds=rounds, shuffle_spines=self.g("shuffle_spines"),
                     shuffle_within=self.g("shuffle_within"), block=block)
        return dict(build=build, ref=ref, name=name), None

    @staticmethod
    def make(kw, seed):
        rows = build_schedule(**kw["build"], seed=seed)
        if kw["ref"]:
            rows = add_references(rows, **kw["ref"])
        return rows

    def update(self):
        combos = self.g("mode") == "combinations"
        self.per_label.config(text="repeats of each combination per visit" if combos else "touches per visit")
        self.within_box.state(["!disabled"] if combos else ["disabled"])
        for w in self.ref_widgets:
            w.state(["!disabled"] if self.g("ref_on") else ["disabled"])
        kw, err = self.read()
        if err:
            self.summary.config(text=err, foreground=PHASE_COLORS["bad"])
            self.create_btn.state(["disabled"])
            return
        b = kw["build"]
        rows = add_visits(self.make(kw, seed=0))
        n_ref = sum(r["block"] == "reference" for r in rows)
        n_main = len(rows) - n_ref
        n_sp = len(b["spine_names"])
        line1 = (f"{n_sp} spines × {n_main // (n_sp * b['rounds'] * b['per_visit'])} combinations × "
                 f"{b['per_visit'] * b['rounds']} repeats = {n_main} touches" if combos else
                 f"{n_sp} spines × {b['rounds']} rounds × {b['per_visit']} = {n_main} touches")
        if n_ref:
            line1 += f"\n+ {n_ref} reference touches at spine {kw['ref']['spine']}"
        visits = rows[-1]["visit"]
        minutes = sum(map(touch_seconds, rows)) / 60
        exists = (schedules_dir() / f"{kw['name']}.csv").exists()
        self.summary.config(text=f"{line1}\n{visits} visits · about {minutes:.0f} min + {visits} moves"
                                 + (f"\n'{kw['name']}' already exists and will be replaced" if exists else ""),
                            foreground="")
        self.create_btn.state(["!disabled"])

    def create(self):
        kw, err = self.read()
        if err:
            return
        path = schedules_dir() / f"{kw['name']}.csv"
        if path.exists() and not messagebox.askyesno(
                "Replace?", f"A saved schedule called '{kw['name']}' already exists. Replace it?",
                parent=self.win):
            return
        ScheduleBuilder.memory = {k: v.get() for k, v in self.v.items() if k != "name"}
        rows = self.make(kw, seed=random.randrange(1_000_000))
        save_schedule(rows, path)
        self.win.destroy()
        self.app._try(lambda: self.app._use_schedule(rows, kw["name"]))


class ScheduleLoader:
    """The Load schedule pop-up: saved schedules, newest first, or browse for a file."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("Load schedule")
        self.win.transient(app.root)
        f = ttk.Frame(self.win, padding=16)
        f.pack(fill="both", expand=True)

        cols = [("name", 220), ("touches", 70), ("visits", 60), ("spines", 180), ("saved", 120)]
        self.tree = ttk.Treeview(f, columns=[c for c, _ in cols], show="headings", height=12,
                                 selectmode="browse")
        for c, w in cols:
            self.tree.heading(c, text=c, anchor="w")
            self.tree.column(c, width=w, anchor="w", stretch=(c == "spines"))
        self.tree.pack(fill="both", expand=True)
        self.paths = {}
        files = sorted(schedules_dir().glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            try:
                rows = load_schedule(path, app.spines)
                used = {r["spine_name"] for r in rows}
                info = (len(rows), rows[-1]["visit"] if rows else 0,
                        ", ".join(n for n in app.spines if n in used))
            except Exception as e:
                info = ("?", "?", f"can't load: {e}")
            saved = datetime.fromtimestamp(path.stat().st_mtime).strftime("%b %d %H:%M")
            self.paths[self.tree.insert("", "end", values=(path.stem, *info, saved))] = path
        if files:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
        else:
            ttk.Label(f, text="No saved schedules yet.", foreground="gray").pack(anchor="w", pady=(6, 0))
        self.tree.bind("<Double-1>", lambda e: self.load())

        bb = ttk.Frame(f)
        bb.pack(fill="x", pady=(10, 0))
        ttk.Button(bb, text="Browse files…", command=self.browse).pack(side="left")
        ttk.Button(bb, text="Load", command=self.load).pack(side="right")
        ttk.Button(bb, text="Cancel", command=self.win.destroy).pack(side="right", padx=6)

    def load(self):
        sel = self.tree.selection()
        if sel and sel[0] in self.paths:
            path = self.paths[sel[0]]
            self.win.destroy()
            self.app.load_schedule_file(path)

    def browse(self):
        path = filedialog.askopenfilename(parent=self.win, title="Schedule CSV",
                                          initialdir=str(DATA_ROOT), filetypes=[("CSV", "*.csv")])
        if path:
            self.win.destroy()
            self.app.load_schedule_file(path)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
