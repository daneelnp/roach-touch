"""Talking to the Arduino running stimulator.ino.

Stimulator      the real board, over USB serial.
FakeStimulator  a pretend board with the same commands, for practice and tests.

Both have the same methods: connect, go_to_rest, press, abort, close.
press() returns a dict: {"status": "done" | "aborted" | "err", ...}.
"""
import os
import threading
import time

import serial
import serial.tools.list_ports

BAUD = 115200
FIRMWARE_ID = "ready,roach-touch-stimulator,1"   # what stimulator.ino says on start-up
LEAD_S = 3.0          # must match LEAD_MS in stimulator.ino
MIN_US, MAX_US = 600, 2400   # must match the limits in stimulator.ino
TIME_LIMITS = {"push_ms": 10000, "hold_ms": 60000, "return_ms": 10000}   # must match MAX_*_MS in stimulator.ino
SMOOTH_MIN_MS = 50    # faster pushes/returns: the servo can't follow exactly (warning only)
TIME_NAMES = {"push_ms": "push time", "hold_ms": "hold", "return_ms": "return time"}


def time_problems(settings):
    """settings: {push_ms, hold_ms, return_ms: a number or a list of numbers}.
    Returns (errors, warnings). Errors = the Arduino would refuse the touch.
    Warnings = under SMOOTH_MIN_MS for push/return time: allowed, but the servo
    only updates every 20 ms, so the real motion won't match what was asked."""
    errors, warnings = [], []
    for key, top in TIME_LIMITS.items():
        vals = settings.get(key, [])
        vals = vals if isinstance(vals, list) else [vals]
        if any(not 0 <= v <= top for v in vals):
            errors.append(f"{TIME_NAMES[key]} must be 0-{top} ms")
        fast = [v for v in vals if 0 <= v < SMOOTH_MIN_MS]
        if key != "hold_ms" and fast:
            warnings.append(f"{TIME_NAMES[key]} under {SMOOTH_MIN_MS} ms "
                            f"({', '.join(map(str, sorted(set(fast))))}): the servo can't follow this exactly")
    return errors, warnings


def find_port():
    """The first port that looks like an Arduino, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == 0x2341 or "usbmodem" in p.device:
            return p.device
    return None


def _parse_done(parts):
    keys = ["lead_us", "push_us", "hold_us", "return_us"]
    return {k: int(v) for k, v in zip(keys, parts[3:7])}


class Stimulator:
    def __init__(self, port):
        self.port = port
        self.ser = None
        self.write_lock = threading.Lock()
        self.pressing = False      # True only while a press is running on the board

    def connect(self):
        """Open the port. This restarts the Uno, so wait for it to introduce
        itself. Refuse anything that isn't the stimulator program: any other
        sketch could misread our commands and move the servo unexpectedly."""
        self.ser = serial.Serial(self.port, BAUD, timeout=0.2)
        deadline = time.time() + 6
        heard = []
        while time.time() < deadline:
            line = self._readline(0.5)
            if line == FIRMWARE_ID:
                return
            if line:
                heard.append(line)
        self.close()
        raise RuntimeError(
            "This Arduino is not running the stimulator program, so nothing was sent to it.\n"
            "Upload arduino/stimulator/stimulator.ino first.\n"
            f"(It said: {' | '.join(heard) or 'nothing'})")

    def present(self):
        """Is the USB port still there? (Only looks; sends nothing.)"""
        return os.path.exists(self.port)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass                       # already gone (unplugged)
            self.ser = None

    def _send(self, line):
        with self.write_lock:
            self.ser.write((line + "\n").encode())

    def _send_command(self, line):
        """Start a new command: drop any leftover replies first, so an old
        line can never be mistaken for the answer to this one."""
        self.ser.reset_input_buffer()
        self._send(line)

    def _readline(self, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if line:
                return line
        return None

    def go_to_rest(self, rest_us, ramp_ms):
        self._send_command(f"R,{rest_us},{ramp_ms}")
        first = self._readline(2)
        if first is None or not first.startswith("ok,R"):
            raise RuntimeError(f"rest command refused: {first}")
        done = self._readline(ramp_ms / 1000 + 3)
        if done is None or not done.startswith("done,R"):
            raise RuntimeError(f"no 'done' after rest command: {done}")

    def press(self, trial, spine_code, rest_us, amount_us, push_ms, hold_ms, return_ms):
        self.pressing = True
        try:
            return self._press(trial, spine_code, rest_us, amount_us, push_ms, hold_ms, return_ms)
        except Exception as e:             # unplugged mid-press: log it, don't crash
            return {"status": "err", "message": f"lost connection to the Arduino ({e})"}
        finally:
            self.pressing = False

    def _press(self, trial, spine_code, rest_us, amount_us, push_ms, hold_ms, return_ms):
        self._send_command(f"P,{trial},{spine_code},{rest_us},{amount_us},{push_ms},{hold_ms},{return_ms}")
        first = self._readline(2)
        if first is None:
            return {"status": "err", "message": "no reply from the Arduino"}
        if first.startswith("err"):
            return {"status": "err", "message": first}
        if first != f"ok,{trial},{spine_code}":
            return {"status": "err", "message": f"wrong echo: {first}"}
        timeout = LEAD_S + (push_ms + hold_ms + return_ms) / 1000 + 5
        last = self._readline(timeout)
        if last is None:
            return {"status": "err", "message": "no 'done' (timed out)"}
        parts = last.split(",")
        if parts[0] == "aborted":
            return {"status": "aborted"}
        if parts[0] == "done" and parts[1:3] == [str(trial), str(spine_code)]:
            return {"status": "done", **_parse_done(parts)}
        return {"status": "err", "message": f"unexpected reply: {last}"}

    def abort(self):
        """Only signal the board if a press is actually running."""
        if self.ser and self.pressing:
            try:
                self._send("X")
            except Exception:
                pass                       # unplugged: nothing to tell


class FakeStimulator:
    """Behaves like the board (same checks, same timing) but moves nothing."""

    def __init__(self, port=None, speedup=1.0):
        self.port = "simulated"
        self.speedup = speedup        # >1 runs faster than real time (for tests)
        self.attached = False
        self.pos = None
        self._abort = threading.Event()

    def connect(self):
        time.sleep(0.2 / self.speedup)

    def present(self):
        return True

    def close(self):
        pass

    def _wait(self, seconds):
        """Sleep, but stop early on abort. Returns True if aborted."""
        return self._abort.wait(seconds / self.speedup)

    def go_to_rest(self, rest_us, ramp_ms):
        if not MIN_US <= rest_us <= MAX_US:
            raise RuntimeError("rest command refused: err,rest outside limits")
        if self.attached:
            time.sleep(ramp_ms / 1000 / self.speedup)
        self.attached, self.pos = True, rest_us

    def press(self, trial, spine_code, rest_us, amount_us, push_ms, hold_ms, return_ms):
        if not self.attached or rest_us != self.pos:
            return {"status": "err", "message": "err,not at rest; send R first"}
        if not MIN_US <= rest_us + amount_us <= MAX_US:
            return {"status": "err", "message": "err,push outside limits"}
        self._abort.clear()
        if self._wait(LEAD_S):
            return {"status": "aborted"}
        aborted = self._wait(push_ms / 1000) or self._wait(hold_ms / 1000)
        time.sleep(return_ms / 1000 / self.speedup)
        if aborted:
            return {"status": "aborted"}
        return {"status": "done", "lead_us": int(LEAD_S * 1e6), "push_us": push_ms * 1000,
                "hold_us": hold_ms * 1000, "return_us": return_ms * 1000}

    def abort(self):
        self._abort.set()
