"""One session = one prep = one folder.

    <data root>/<session name>/
        log.csv            one row per press, written the moment it finishes
        flags.csv          flags and notes added afterwards (log.csv is never rewritten)
        spines.csv         copy of the spine table used in this session
        schedule_*.csv     every schedule loaded or generated
        session_info.txt   fill in by hand: animal, leg, electrode distances, ...

Opening an existing session continues its trial numbers, so a trial number is
never used twice in one session.
"""
import csv
import shutil
from datetime import datetime
from pathlib import Path

LOG_FIELDS = ["trial", "time", "block", "spine_name", "spine_code", "visit", "touch_in_visit",
              "rest_us", "amount_us", "push_ms", "hold_ms", "return_ms", "gap_s",
              "status", "lead_us", "push_us", "hold_us", "return_us", "message"]
FLAG_FIELDS = ["trial", "time", "flag", "note"]

INFO_TEMPLATE = """session: {name}
created: {created}

animal / date of dissection:
leg (left/right, which pair):
leg orientation (which side up):
electrode 1 position (mm from femur-tibia joint):
electrode 2 position (mm from femur-tibia joint):
electrode 3 position (mm from femur-tibia joint):
reference electrode position:
EScope channels (ai0/ai1/ai2 = electrodes, ai3 = sync):
amplifier gain / filters:
photos taken (file names):
notes:
"""


def _now():
    return datetime.now().isoformat(timespec="milliseconds")


class Session:
    def __init__(self, root, name, spines_path):
        self.name = name
        self.dir = Path(root).expanduser() / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / "log.csv"
        self.flags_path = self.dir / "flags.csv"

        if not (self.dir / "spines.csv").exists():
            shutil.copy(spines_path, self.dir / "spines.csv")
        info = self.dir / "session_info.txt"
        if not info.exists():
            info.write_text(INFO_TEMPLATE.format(name=name, created=_now()))
        for path, fields in [(self.log_path, LOG_FIELDS), (self.flags_path, FLAG_FIELDS)]:
            if not path.exists():
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(fields)

        self.last_trial = self._last_trial_in_log()

    def _last_trial_in_log(self):
        with open(self.log_path, newline="") as f:
            trials = [int(r["trial"]) for r in csv.DictReader(f) if r["trial"]]
        return max(trials, default=0)

    def next_trial(self):
        self.last_trial += 1
        return self.last_trial

    def log(self, entry):
        entry = {"time": _now(), **entry}
        with open(self.log_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore").writerow(entry)

    def flag(self, trial, flag, note):
        with open(self.flags_path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FLAG_FIELDS).writerow(
                {"trial": trial, "time": _now(), "flag": flag, "note": note})

    def schedule_path(self, label):
        stamp = datetime.now().strftime("%H%M%S")
        return self.dir / f"schedule_{stamp}_{label}.csv"
