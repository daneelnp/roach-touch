"""Runs a schedule, one visit at a time, in a background thread.

Rules (decided 2026-11-12/13):
- Within a visit (same spine), touches run on their own.
- When the next touch is at a different spine, the runner stops and waits for
  "Start visit" (you move the manipulator first).
- gap_s is the rest BEFORE a touch: the time from the end of the previous
  touch to this touch's push. The barcode goes out LEAD_S earlier so the push
  lands on time. This also holds across visits and for single touches.
- An aborted or failed touch is logged but not counted: the same schedule row
  is tried again on the next "Start visit", with a new trial number.

notify(kind, data) is called from the background thread. The window turns
these into screen updates.
"""
import threading
import time

from schedule import add_visits
from stimulator import LEAD_S

TEST_SPINE = ("test", 0)   # name and code used for single test presses


class Runner:
    def __init__(self, stim, session, spines, notify):
        self.stim, self.session, self.spines, self.notify = stim, session, spines, notify
        self.schedule, self.index = [], 0
        self.pause_evt, self.abort_evt = threading.Event(), threading.Event()
        self.thread = None
        self.last_end = None          # time.monotonic() when the last touch ended
        self.last_trial = None

    # ---------- state ----------
    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def set_schedule(self, rows):
        if self.busy:
            raise RuntimeError("stop the current visit first")
        if any(r["gap_s"] < LEAD_S for r in rows):
            raise RuntimeError(f"every 'rest before push' must be at least {LEAD_S:.0f} s (the barcode lead)")
        self.schedule, self.index = add_visits(list(rows)), 0

    def next_row(self):
        return self.schedule[self.index] if self.index < len(self.schedule) else None

    # ---------- controls (called from the window) ----------
    def start_visit(self):
        if self.next_row() is None:
            raise RuntimeError("no schedule loaded, or the schedule is finished")
        self._start(self._run_visit)

    def single_touch(self, settings, spine_name, wait_for_rest=True):
        """One touch outside any schedule, with these exact settings."""
        if spine_name == TEST_SPINE[0]:
            code, block = TEST_SPINE[1], "test"
        elif spine_name in self.spines:
            code, block = self.spines[spine_name], "manual"
        else:
            raise RuntimeError(f"'{spine_name}' is not in spines.csv")
        if wait_for_rest and settings["gap_s"] < LEAD_S:
            raise RuntimeError(f"'rest before push' must be at least {LEAD_S:.0f} s (the barcode lead)")
        self._start(lambda: self._single_touch(settings, spine_name, code, block, wait_for_rest))

    def go_to_rest(self, rest_us, ramp_ms):
        self._start(lambda: self._go_to_rest(rest_us, ramp_ms))

    def pause(self):
        self.pause_evt.set()

    def resume(self):
        self.pause_evt.clear()

    def abort(self):
        self.abort_evt.set()
        self.pause_evt.clear()        # an abort also cancels a pause
        if self.busy:
            self.stim.abort()

    def end_schedule(self):
        """Stop and unload the schedule. Returns (touches done, total)."""
        done, total = self.index, len(self.schedule)
        self.abort()
        if self.thread is not None:
            self.thread.join(timeout=15)
        self.schedule, self.index = [], 0
        return done, total

    # ---------- background work ----------
    def _start(self, job):
        if self.busy:
            raise RuntimeError("busy: wait for the current action to finish")
        self.abort_evt.clear()

        def wrapped():
            try:
                job()
            except Exception as e:          # report, never crash silently
                self.notify("error", str(e))
            finally:
                self.notify("idle", None)

        self.thread = threading.Thread(target=wrapped, daemon=True)
        self.thread.start()

    def _go_to_rest(self, rest_us, ramp_ms):
        self.notify("status", "Moving to rest")
        self.stim.go_to_rest(rest_us, ramp_ms)
        self.notify("status", "")

    def _wait(self, seconds):
        """Wait `seconds`, plus however long we are paused. True if aborted."""
        end = time.monotonic() + seconds
        told = False
        while time.monotonic() < end or self.pause_evt.is_set():
            if self.abort_evt.is_set():
                return True
            if self.pause_evt.is_set() and not told:
                self.notify("status", "Paused")
                told = True
            time.sleep(0.02)
        return False

    def _wait_rest_after_last(self, gap_s):
        """Wait so that the push (LEAD_S after sending) lands gap_s after the
        previous touch ended."""
        if self.last_end is None:
            return False
        remaining = gap_s - LEAD_S - (time.monotonic() - self.last_end)
        if remaining > 0:
            self.notify("resting", remaining)
        return self._wait(max(0.0, remaining))

    def _press(self, row, name, code, extra):
        trial = self.session.next_trial()
        self.notify("sending", {"trial": trial, "spine_name": name, **row})
        result = self.stim.press(trial, code, row["rest_us"], row["amount_us"],
                                 row["push_ms"], row["hold_ms"], row["return_ms"])
        # A press the Arduino refused ("err,...") never moved, so it doesn't
        # start the rest timer. Anything else (done, aborted, timeout) may have.
        if not result.get("message", "").startswith("err,"):
            self.last_end = time.monotonic()
        self.last_trial = trial
        entry = {"trial": trial, "spine_name": name, "spine_code": code,
                 **{k: row[k] for k in ("rest_us", "amount_us", "push_ms",
                                        "hold_ms", "return_ms", "gap_s")},
                 **extra, **result}
        self.session.log(entry)
        self.notify("result", entry)
        return result

    def _single_touch(self, settings, name, code, block, wait_for_rest):
        if wait_for_rest and self._wait_rest_after_last(settings["gap_s"]):
            self.notify("stopped", "aborted")
            return
        self._press(settings, name, code, {"block": block})

    def _run_visit(self):
        spine = self.schedule[self.index]["spine_name"]
        while True:
            row = self.schedule[self.index]
            if self._wait_rest_after_last(row["gap_s"]):
                self.notify("stopped", "aborted")
                return
            extra = {k: row[k] for k in ("block", "visit", "touch_in_visit")}
            result = self._press(row, spine, self.spines[spine], extra)
            if result["status"] != "done":
                self.notify("stopped", result.get("message", result["status"]))
                return                                  # same row is retried
            self.index += 1
            nxt = self.next_row()
            if nxt is None:
                self.notify("finished", None)
                return
            if nxt["spine_name"] != spine:
                self.notify("move", nxt)
                return
