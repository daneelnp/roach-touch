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
import itertools
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


VARIED = ["amount_us", "push_ms", "hold_ms", "return_ms", "gap_s"]   # settings the builder can vary
MAX_TOUCHES = 20000


def count_touches(n_spines, values, mode, per_visit, rounds):
    """How many touches build_schedule would make (cheap; no list is built)."""
    combos = 1
    for k in VARIED:
        combos *= len(values[k])
    per = combos * per_visit if mode == "combinations" else per_visit
    return n_spines * rounds * per


def build_schedule(spine_names, values, rest_us, mode="combinations", per_visit=5,
                   rounds=1, shuffle_spines=True, shuffle_within=True,
                   block="custom", seed=None):
    """The general schedule maker.

    values      {setting: [value, ...]} for every setting in VARIED. One value
                = held constant; several = varied.
    mode        "combinations": every visit holds every combination of the
                    values, per_visit times each (balanced).
                "random": every visit holds per_visit touches, each setting
                    picked at random from its values (unbalanced).
    rounds      how many times each spine is visited.
    shuffle_spines   new random spine order every round (a round never starts
                     at the spine the last one ended on: that would silently
                     merge two visits). Off = the order given, every round.
    shuffle_within   (combinations only) random order inside a visit. Off =
                     the combinations in order, repeated per_visit times.
    """
    rng = random.Random(seed)
    combos = [dict(zip(VARIED, c)) for c in itertools.product(*(values[k] for k in VARIED))]
    rows, last = [], None
    for _ in range(rounds):
        order = list(spine_names)
        if shuffle_spines:
            rng.shuffle(order)
            while len(order) > 1 and order[0] == last:
                rng.shuffle(order)
        for spine in order:
            if mode == "combinations":
                visit = combos * per_visit
                if shuffle_within:
                    rng.shuffle(visit)
            elif mode == "random":
                visit = [{k: rng.choice(values[k]) for k in VARIED} for _ in range(per_visit)]
            else:
                raise ValueError(f"unknown mode {mode!r}")
            rows += [{"block": block, "spine_name": spine, "rest_us": rest_us, **c} for c in visit]
        last = order[-1]
    return rows


def touch_seconds(row):
    """Rough length of one touch: rest before it + push + hold + return."""
    return row["gap_s"] + (row["push_ms"] + row["hold_ms"] + row["return_ms"]) / 1000


def add_references(rows, spine, settings, every, unit="touches", count=3):
    """Insert reference hits: `count` identical touches at one fixed `spine`.

    They go at the start, at the end, and between two visits whenever at
    least `every` touches (unit="touches") or `every` minutes (unit="minutes",
    estimated from the schedule, not counting your moves) have passed since
    the last reference. Never inside a visit. settings: amount_us, push_ms,
    hold_ms, return_ms, gap_s, rest_us. Logged with block "reference"."""
    def ref_group():
        return [{"block": "reference", "spine_name": spine, **settings} for _ in range(count)]

    visits = []                                   # split into runs of the same spine
    for row in rows:
        if visits and visits[-1][-1]["spine_name"] == row["spine_name"]:
            visits[-1].append(row)
        else:
            visits.append([row])

    out, since = ref_group(), 0.0
    for i, visit in enumerate(visits):
        out += visit
        since += len(visit) if unit == "touches" else sum(map(touch_seconds, visit)) / 60
        if since >= every and i < len(visits) - 1:
            out += ref_group()
            since = 0.0
    return out + ref_group()


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
