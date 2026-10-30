# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer
"""report.py tests: HA export parsing, cycle splitting, CSV output.

Run: python -m tests.test_report
"""
from datetime import datetime, timedelta
from pathlib import Path

from openwood import report as R

BASE = datetime(2026, 10, 29, 8, 0, 0)
H = "homeassistant"


def _ts(i_min: int) -> str:
    return (BASE + timedelta(minutes=i_min)).isoformat()


def _ha_csv(rows: list[tuple[int, str, str]]) -> str:
    lines = ["entity_id,state,last_changed"]
    for i_min, entity, value in rows:
        lines.append(f"sensor.stove_{entity},{value},{_ts(i_min)}")
    return "\n".join(lines) + "\n"


def _write(tmp: Path, text: str) -> Path:
    p = tmp / "ha.csv"
    p.write_text(text)
    return p


# -- HA parsing ---------------------------------------------------------------


def test_numeric_state_entity_maps_to_name(tmp_path):
    # regression: the numeric ..._burn_cycle_state entity used to be treated
    # as a state *name* ("13"), so SLEEP never matched and cycles never ended
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "200"),
        (30, "burn_cycle_state", "13"),
        (40, "stove_temperature", "30"),
    ]))
    samples = R.load_samples(csv)
    assert len(samples) == 4
    assert samples[0].state == "LIGHTING E"
    assert samples[2].state == "SLEEP"
    assert samples[1].stove_temp == 200.0


def test_state_name_entity_and_unavailable_filtered(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state_name", "LIGHTING E"),
        (5, "stove_temperature", "unavailable"),
        (10, "stove_temperature", "120"),
        (15, "burn_cycle_state_name", "unknown"),
    ]))
    samples = R.load_samples(csv)
    assert [s.t for s in samples] == [BASE, BASE + timedelta(minutes=10)]
    assert samples[0].state == "LIGHTING E"
    assert samples[1].stove_temp == 120.0


def test_valve_gauges_are_percent(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "air_valve_1_position", "100"),
        (0, "air_valve_2_position", "25"),
        (0, "air_valve_3_position", "13"),
    ]))
    samples = R.load_samples(csv)
    assert samples[0].valve_gauges == {"1": 100.0, "2": 25.0, "3": 13.0}


# -- cycle splitting ----------------------------------------------------------


def test_cycle_ends_at_sleep_start(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "350"),
        (30, "burn_cycle_state", "13"),   # SLEEP starts here
        (40, "stove_temperature", "30"),   # still SLEEP
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    assert len(cycles) == 1
    c = cycles[0]
    assert c.start == BASE
    assert c.end == BASE + timedelta(minutes=30)  # SLEEP start, not end
    assert c.max_temp == 350.0
    assert c.advanced is False
    # forward-fill: samples between changes carry the state they were in;
    # the sample exactly at the SLEEP boundary belongs to SLEEP
    assert [s.state for s in c.samples] == ["LIGHTING E", "LIGHTING E", "SLEEP"]


def test_overnight_splits_cycles(tmp_path):
    # OVERNIGHT is a mode label, not a combustion state: a refuel after it
    # starts a new cycle at LIGHTING E
    rows = [
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "400"),
        (20, "burn_cycle_state", "19"),    # EARLY BURN E
        (40, "burn_cycle_state", "9"),     # OVERNIGHT
        (50, "burn_cycle_state", "18"),    # relight
        (60, "stove_temperature", "300"),
        (80, "burn_cycle_state", "13"),    # SLEEP
    ]
    csv = _write(tmp_path, _ha_csv(rows))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    assert len(cycles) == 2
    assert cycles[0].advanced is True
    assert cycles[0].end == BASE + timedelta(minutes=40)
    assert cycles[1].start == BASE + timedelta(minutes=50)
    assert cycles[1].end == BASE + timedelta(minutes=80)


def test_min_burn_temp_drops_cold_attempts(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (5, "stove_temperature", "90"),    # never caught
        (30, "burn_cycle_state", "13"),
        (40, "stove_temperature", "25"),
        (50, "burn_cycle_state", "18"),
        (60, "stove_temperature", "350"),  # this one did
        (90, "burn_cycle_state", "13"),
    ]))
    samples = R.load_samples(csv)
    all_cycles = R.find_cycles(samples)
    kept = R.find_cycles(samples, min_burn_temp=150)
    assert len(all_cycles) == 2
    assert len(kept) == 1
    assert kept[0].max_temp == 350.0


def test_open_cycle_at_end_of_log(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "300"),
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    assert len(cycles) == 1
    assert cycles[0].end == BASE + timedelta(minutes=10)


def test_blips_under_a_minute_are_dropped(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (1, "burn_cycle_state", "13"),
        (2, "burn_cycle_state", "18"),
        (30, "stove_temperature", "300"),
        (40, "burn_cycle_state", "13"),
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    assert len(cycles) == 1
    assert cycles[0].duration_min >= 37


# -- watch CSV format ---------------------------------------------------------


def test_watch_csv_raw_valves_converted(tmp_path):
    p = tmp_path / "watch.csv"
    p.write_text(
        R.WATCH_HEADER + "\n"
        "2026-10-29 08:00:00,20,,,21.0,0,0,3,18,0,0,0,0,0,1023,765,895,0\n"
        "2026-10-29 08:30:00,30,,,21.0,0,0,3,13,0,0,0,0,0,1023,765,895,0\n"
    )
    samples = R.load_samples(p)
    assert samples[0].state == "LIGHTING E"
    # raw 1023 -> ~0 % open... gauge = (1024-raw)/1024*100
    assert abs(samples[0].valve_gauges["1"] - 0.1) < 0.5
    assert abs(samples[0].valve_gauges["2"] - 25.2) < 0.5
    cycles = R.find_cycles(samples, min_burn_temp=150)
    assert len(cycles) == 0  # never got hot


# -- summary / output ---------------------------------------------------------


def test_summary_row_and_json(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (8, "stove_temperature", "200"),
        (13, "stove_temperature", "350"),
        (30, "burn_cycle_state", "13"),
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    row = cycles[0].summary_row()
    assert row["max_temp_c"] == 350.0
    assert row["time_to_200c_min"] == 8.0
    assert row["time_to_350c_min"] == 13.0
    assert row["advanced"] is False
    assert row["lighting_min"] == 30.0
    import json
    data = json.loads(R.report_to_json(cycles))
    assert data[0]["start"] == BASE.isoformat()


def test_write_cycle_csvs(tmp_path):
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "350"),
        (10, "air_valve_1_position", "100"),
        (30, "burn_cycle_state", "13"),
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    out = tmp_path / "out"
    paths = R.write_cycle_csvs(cycles, out)
    assert len(paths) == 2  # one cycle + summary
    cycle_csv = (out / paths[0].name).read_text().splitlines()
    assert cycle_csv[0].startswith("timestamp_utc,stove_temp_c")
    assert any("350" in line for line in cycle_csv)
    summary = (out / "cycles_summary.csv").read_text().splitlines()
    assert summary[0].startswith("start,end,duration_min")


def test_numeric_and_name_entities_agree(tmp_path):
    # the actual HA export carries both the numeric and the name entity
    csv = _write(tmp_path, _ha_csv([
        (0, "burn_cycle_state", "18"),
        (0, "burn_cycle_state_name", "LIGHTING E"),
        (0, "stove_temperature", "20"),
        (10, "stove_temperature", "350"),
        (30, "burn_cycle_state", "13"),
        (30, "burn_cycle_state_name", "SLEEP"),
    ]))
    cycles = R.find_cycles(R.load_samples(csv), min_burn_temp=150)
    assert len(cycles) == 1
    assert cycles[0].states == ["LIGHTING E", "SLEEP"]


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                    from pathlib import Path
                    import tempfile
                    with tempfile.TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print("PASS", name)
            except Exception as e:
                failures += 1
                print("FAIL", name, "->", repr(e))
    raise SystemExit(1 if failures else 0)
