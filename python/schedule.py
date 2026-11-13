"""Schedules: lists of touches, one row per touch, in the order they will be done.

A schedule row holds everything needed for one press except the trial number,
which is given out at the moment the press is sent (so it is never reused):

    block, spine_name, rest_us, amount_us, push_ms, hold_ms, return_ms, gap_s

gap_s is the rest BEFORE the touch: from the end of the previous touch to this
push (at least 3 s, the barcode lead).
Consecutive rows at the same spine form one "visit"; the app runs a visit on
its own and stops for you when the spine changes.
"""
import csv
import random

FIELDS = ["block", "spine_name", "rest_us", "amount_us",
          "push_ms", "hold_ms", "return_ms", "gap_s"]
INT_FIELDS = ["rest_us", "amount_us", "push_ms", "hold_ms", "return_ms"]


def load_spines(path):
    """Read spines.csv -> {name: code}, in file order."""
    spines = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            name, code = row["name"].strip(), int(row["code"])
            if not 1 <= code <= 255:
                raise ValueError(f"spine {name}: code must be 1-255 (0 is for test presses)")
            if name in spines or code in spines.values():
                raise ValueError(f"spine {name} / code {code} appears twice in {path}")
            spines[name] = code
    return spines


def _row(block, spine, settings):
    return {"block": block, "spine_name": spine,
            "rest_us": settings["rest_us"], "amount_us": settings["amount_us"],
            "push_ms": settings["push_ms"], "hold_ms": settings["hold_ms"],
            "return_ms": settings["return_ms"], "gap_s": settings["gap_s"]}


def spine_block(spines, touches_per_visit, rounds, settings, seed=None):
    """Shuffled rounds: every round visits every spine once, in a new random
    order, with touches_per_visit touches per visit. Two rounds never meet at
    the same spine (that would silently make one double-length visit)."""
    rng = random.Random(seed)
    rows, last = [], None
    for _ in range(rounds):
        order = list(spines)
        rng.shuffle(order)
        while len(order) > 1 and order[0] == last:
            rng.shuffle(order)
        for spine in order:
            rows += [_row("spine", spine, settings) for _ in range(touches_per_visit)]
        last = order[-1]
    return rows


def velocity_block(spine, push_ms_values, repeats, settings, seed=None):
    """One spine, each push duration repeated `repeats` times, fully shuffled."""
    rng = random.Random(seed)
    rows = []
    for push_ms in push_ms_values:
        for _ in range(repeats):
            row = _row("velocity", spine, settings)
            row["push_ms"] = int(push_ms)
            rows.append(row)
    rng.shuffle(rows)
    return rows


def add_visits(rows):
    """Number the visits (runs of the same spine) and the touches within them."""
    visit, prev = 0, None
    for row in rows:
        if row["spine_name"] != prev:
            visit, touch, prev = visit + 1, 0, row["spine_name"]
        touch += 1
        row["visit"], row["touch_in_visit"] = visit, touch
    return rows


def save_schedule(rows, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_schedule(path, spines):
    """Read a schedule you wrote yourself. Every spine must be in spines.csv."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = set(FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"schedule is missing columns: {', '.join(sorted(missing))}")
        for i, raw in enumerate(reader, start=2):        # line 1 is the header
            row = {k: raw[k].strip() for k in FIELDS}
            if row["spine_name"] not in spines:
                raise ValueError(f"line {i}: spine '{row['spine_name']}' is not in spines.csv")
            try:
                for k in INT_FIELDS:
                    row[k] = int(row[k])
                row["gap_s"] = float(row["gap_s"])
            except ValueError:
                raise ValueError(f"line {i}: a number is not a number") from None
            rows.append(row)
    return add_visits(rows)
