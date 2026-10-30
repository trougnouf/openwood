"""Burn-cycle analysis of stove logs (for support tickets / review).

Reads either
- a Home Assistant history export (entity_id,state,last_changed), or
- an `openwood watch --log` CSV,

splits the timeline into burn cycles (LIGHTING -> ... -> SLEEP) and
summarises each one: duration, peak temperature, time to 200/350 C,
time spent in the lighting phase, whether the state machine ever
advanced past LIGHTING, and valve/intensity ranges when those entities
are present in the log.

With --csv-dir it writes one CSV per cycle plus a cycles_summary.csv,
ready to attach to a support request.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import protocol as P

WATCH_HEADER = (
    "timestamp,stove_temp,room_temp,board_temp,room_setpoint,intensity,"
    "mode,manual_level,burn_cycle,door_open,burning,extended_burn,check_fuel,"
    "alerts,valve1,valve2,valve3,error"
)

# HA entity_id matching: (normalised substring, field)
# NOTE: "burncyclestatename" must come before "burncyclestate" (it contains
# it), and the numeric "..._burn_cycle_state" entity maps to state_raw.
_ENTITY_RULES = [
    ("burncyclestatename", "state_name"),
    ("burncyclestate", "state_raw"),
    ("burncycle", "state_raw"),
    ("stovetemperature", "stove_temp"),
    ("stovetemp", "stove_temp"),
    ("roomtemperature", "room_temp"),
    ("roomtemp", "room_temp"),
    ("boardtemp", "board_temp"),
    ("intensity", "intensity"),
    ("lightlevel", "intensity"),
    ("valve1", "valve_1"),
    ("valve2", "valve_2"),
    ("valve3", "valve_3"),
    ("valve_1", "valve_1"),
    ("valve_2", "valve_2"),
    ("valve_3", "valve_3"),
    ("hvacmode", "hvac_mode"),
    ("roomsetpoint", "room_setpoint"),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _field_for(entity_id: str) -> str | None:
    n = _norm(entity_id)
    for frag, name in _ENTITY_RULES:
        if _norm(frag) in n:
            return name
    return None


@dataclass
class Sample:
    t: datetime
    stove_temp: float | None = None
    room_temp: float | None = None
    board_temp: float | None = None
    intensity: float | None = None
    state: str | None = None      # burn cycle state name
    valve_gauges: dict = field(default_factory=dict)  # '1'/'2'/'3' -> %


@dataclass
class Cycle:
    start: datetime
    end: datetime
    samples: list[Sample]
    states: list[str]             # ordered state names visited
    seg_starts: list = field(default_factory=list)  # start time per segment

    @property
    def duration_min(self) -> float:
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def max_temp(self) -> float | None:
        vals = [s.stove_temp for s in self.samples if s.stove_temp is not None]
        return max(vals) if vals else None

    def time_to(self, celsius: float) -> float | None:
        """Minutes from cycle start until the stove first reaches celsius."""
        for s in self.samples:
            if s.stove_temp is not None and s.stove_temp >= celsius:
                return (s.t - self.start).total_seconds() / 60.0
        return None

    @property
    def min_temp_first_40m(self) -> float | None:
        vals = [
            s.stove_temp
            for s in self.samples
            if s.stove_temp is not None
            and (s.t - self.start).total_seconds() < 40 * 60
        ]
        return min(vals) if vals else None

    @property
    def lighting_min(self) -> float:
        """Minutes spent in LIGHTING / LIGHTING E segments."""
        total = 0.0
        for i, name in enumerate(self.states):
            if name in ("LIGHTING", "LIGHTING E"):
                end = self.seg_starts[i + 1] if i + 1 < len(self.seg_starts) else self.end
                total += (end - self.seg_starts[i]).total_seconds() / 60.0
        return total

    @property
    def advanced(self) -> bool:
        return any(
            n in (
                "EARLY BURN", "EARLY BURN E", "STEADY STATE EFFICIENT",
                "STEADY STATE EFFICIENT E", "STEADY STATE INEFFICIENT",
                "STEADY STATE INEFFICIENT E", "LATE BURN EFFICIENT",
                "LATE BURN INEFFICIENT", "CHAR", "CHAR E",
            )
            for n in self.states
        )

    def valve_range(self, key: str) -> tuple[float, float] | None:
        vals = [
            s.valve_gauges[key]
            for s in self.samples
            if key in s.valve_gauges
        ]
        return (min(vals), max(vals)) if vals else None

    def intensity_range(self) -> tuple[float, float] | None:
        vals = [s.intensity for s in self.samples if s.intensity is not None]
        return (min(vals), max(vals)) if vals else None

    def summary_row(self) -> dict:
        row = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_min": round(self.duration_min, 1),
            "max_temp_c": self.max_temp,
            "time_to_200c_min": self.time_to(200),
            "time_to_350c_min": self.time_to(350),
            "min_temp_first_40m_c": self.min_temp_first_40m,
            "lighting_min": round(self.lighting_min, 1),
            "advanced": self.advanced,
            "states": " -> ".join(dict.fromkeys(self.states)),
        }
        for k in ("1", "2", "3"):
            rng = self.valve_range(k)
            if rng:
                row[f"valve_{k}_gauge_pct"] = f"{rng[0]:.0f}..{rng[1]:.0f}"
        irng = self.intensity_range()
        if irng:
            row["intensity_pct"] = f"{irng[0]:.0f}..{irng[1]:.0f}"
        return row


def load_samples(path: str | Path) -> list[Sample]:
    """Load a timeline of samples from an HA export or a watch CSV."""
    path = Path(path)
    with path.open(newline="") as f:
        first = f.readline().strip()
        f.seek(0)
        if first.startswith("entity_id,"):
            return _load_ha(f)
        if first.startswith("timestamp,"):
            return _load_watch(f)
    raise ValueError(f"unrecognised log format: {path} (first line: {first!r})")


def _parse_ts(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _f(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_ha(f) -> list[Sample]:
    by_time: dict[datetime, Sample] = {}
    for row in csv.DictReader(f):
        entity = (row.get("entity_id") or "").strip()
        value = (row.get("state") or "").strip()
        ts = (row.get("last_changed") or "").strip()
        if not entity or not value or not ts:
            continue
        if value in ("unavailable", "unknown", "None", ""):
            continue
        field = _field_for(entity)
        if field is None:
            continue
        t = _parse_ts(ts)
        s = by_time.setdefault(t, Sample(t=t))
        if field == "stove_temp":
            s.stove_temp = _f(value)
        elif field == "room_temp":
            s.room_temp = _f(value)
        elif field == "board_temp":
            s.board_temp = _f(value)
        elif field == "intensity":
            s.intensity = _f(value)
        elif field in ("valve_1", "valve_2", "valve_3"):
            # HA valve sensors already carry the gauge percent
            # (the bridge publishes (1024-raw)/1024*100), not the raw value
            raw = _f(value)
            if raw is not None:
                s.valve_gauges[field[-1]] = raw
        elif field == "state_raw":
            try:
                v = int(float(value))
            except ValueError:
                continue
            if 0 <= v < len(P.BURN_CYCLE_NAMES):
                s.state = P.BURN_CYCLE_NAMES[v]
        elif field == "state_name":
            s.state = value
    return [by_time[t] for t in sorted(by_time)]


def _load_watch(f) -> list[Sample]:
    out = []
    for row in csv.DictReader(f):
        ts = (row.get("timestamp") or "").strip()
        if not ts:
            continue
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            t = _parse_ts(ts)
        s = Sample(t=t)
        s.stove_temp = _f(row.get("stove_temp"))
        s.room_temp = _f(row.get("room_temp"))
        s.board_temp = _f(row.get("board_temp"))
        s.intensity = _f(row.get("intensity"))
        bc = _f(row.get("burn_cycle"))
        if bc is not None and 0 <= int(bc) < len(P.BURN_CYCLE_NAMES):
            s.state = P.BURN_CYCLE_NAMES[int(bc)]
        for k in ("1", "2", "3"):
            raw = _f(row.get(f"valve{k}"))
            if raw is not None:
                s.valve_gauges[k] = P.valve_gauge(int(raw))
        out.append(s)
    return out


def find_cycles(samples: list[Sample], min_burn_temp: float | None = None) -> list[Cycle]:
    """Split the timeline into cycles: LIGHTING -> ... -> SLEEP.

    OVERNIGHT is also a boundary: it is a mode label (the user switched
    to Overnight mode while the fire kept burning), not a combustion
    state, and a refuel after it starts a new cycle at LIGHTING E.

    With min_burn_temp, cycles that never reached that stove temperature
    (lighting attempts that did not catch) are dropped.
    """
    LIGHTING = ("LIGHTING", "LIGHTING E")
    SLEEP = ("SLEEP", "STAND BY", "STARTUP", "OVERNIGHT")
    # state segments (name, start_time); end = next segment start
    segs: list[tuple[str, datetime]] = []
    for s in samples:
        if s.state is None:
            continue
        if segs and segs[-1][0] == s.state:
            continue
        segs.append((s.state, s.t))
    end_times = [segs[i + 1][1] for i in range(len(segs) - 1)] + \
        [samples[-1].t if samples else segs[-1][1]]

    cycles: list[Cycle] = []
    cur: list[tuple[str, datetime, datetime]] = []
    for (name, start), end in zip(segs, end_times):
        if name in LIGHTING and not cur:
            cur = [(name, start, end)]
        elif cur:
            cur.append((name, start, end))
            if name in SLEEP:
                # the cycle ends when SLEEP/OVERNIGHT begins, not when the
                # SLEEP segment ends
                c = _make_cycle(cur, samples, end=start)
                if c is not None:
                    cycles.append(c)
                cur = []
    if cur:  # cycle still open at end of log
        c = _make_cycle(cur, samples)
        if c is not None:
            cycles.append(c)
    if min_burn_temp is not None:
        cycles = [c for c in cycles if (c.max_temp or 0) >= min_burn_temp]
    return cycles


def _make_cycle(segs: list[tuple[str, datetime, datetime]],
                samples: list[Sample],
                end: datetime | None = None) -> Cycle | None:
    start = segs[0][1]
    if end is None:
        end = segs[-1][2]
    if (end - start).total_seconds() < 60:
        return None  # spurious blip (e.g. a single push)
    cyc_samples = [s for s in samples if start <= s.t <= end]
    if not any(s.stove_temp is not None for s in cyc_samples):
        return None
    states = [n for n, _, _ in segs]
    seg_starts = [st for _, st, _ in segs]
    # forward-fill the state machine state so every sample of the cycle
    # carries the state it was in (HA only records state changes)
    i = 0
    for s in cyc_samples:
        while i + 1 < len(seg_starts) and s.t >= seg_starts[i + 1]:
            i += 1
        s.state = states[i]
    return Cycle(start=start, end=end, samples=cyc_samples,
                 states=states, seg_starts=seg_starts)


def write_cycle_csvs(cycles: list[Cycle], out_dir: str | Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, c in enumerate(cycles, 1):
        p = out_dir / f"cycle_{i:02d}_{c.start:%Y%m%d_%H%M}.csv"
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp_utc", "stove_temp_c", "room_temp_c", "board_temp_c",
                "intensity_pct", "burn_cycle_state",
                "valve_1_gauge_pct", "valve_2_gauge_pct", "valve_3_gauge_pct",
            ])
            for s in c.samples:
                w.writerow([
                    s.t.isoformat().replace("+00:00", "Z"),
                    "" if s.stove_temp is None else s.stove_temp,
                    "" if s.room_temp is None else s.room_temp,
                    "" if s.board_temp is None else s.board_temp,
                    "" if s.intensity is None else s.intensity,
                    s.state or "",
                    *[s.valve_gauges.get(k, "") for k in ("1", "2", "3")],
                ])
        paths.append(p)
    summary = out_dir / "cycles_summary.csv"
    with summary.open("w", newline="") as f:
        rows = [c.summary_row() for c in cycles]
        if rows:
            keys = []
            for r in rows:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    paths.append(summary)
    return paths


def format_report(cycles: list[Cycle]) -> str:
    if not cycles:
        return "no complete burn cycles found in the log"
    lines = [f"{len(cycles)} burn cycles:"]
    for i, c in enumerate(cycles, 1):
        def m(v):
            return f"{v:.0f}m" if v is not None else "  -"
        lines.append(
            f"cycle {i:2d}: {c.start:%m-%d %H:%M} -> {c.end:%m-%d %H:%M}  "
            f"dur={c.duration_min:6.0f}min  "
            f"max={c.max_temp if c.max_temp is not None else 0:5.0f}C  "
            f"t200={m(c.time_to(200)):>4}  t350={m(c.time_to(350)):>4}  "
            f"lighting={c.lighting_min:6.0f}min  "
            f"advanced={'yes' if c.advanced else 'NO '}"
        )
        lines.append(f"           path: {' -> '.join(dict.fromkeys(c.states))}")
        v = c.valve_range("1")
        if v:
            lines.append(
                f"           valve1 gauge: {v[0]:.0f}..{v[1]:.0f} %  "
                f"(100 = wide open)"
            )
    return "\n".join(lines)


def report_to_json(cycles: list[Cycle]) -> str:
    return json.dumps([c.summary_row() for c in cycles], indent=2)
