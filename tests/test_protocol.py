# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer
"""Parser regression tests. Run: python -m tests.test_protocol"""
import struct

from openwood import protocol as P

# Live capture from an Aire 300 (fw 2.3.36 - 2.1.29), values cross-checked
# against the BLE registers read at the same moment:
# stove 112 C, room 32.4 C, setpoint 23.0, intensity raw 1, valves 0/0/1023,
# board 20.3 C, burn cycle 18 (LIGHTING E), mode 1, dial 13, error 0xC0.
LIVE_59 = "013528904DC00700010144002E0010012100C00D0000000003FF21000CB"
assert len(LIVE_59) == 59


def test_reading_set_59_live():
    st = P.parse_reading_set(LIVE_59)
    assert st is not None
    assert st.stove_temp == 112
    assert st.light_raw == 1
    assert st.room_temp == 32.4
    assert st.room_setpoint == 23.0
    assert st.board_temp == 20.3
    assert st.mode == 1
    assert st.manual_level == 13
    assert st.burn_cycle == 18
    assert st.error_code == 0xC0
    assert st.extended_burn is True
    assert st.check_fuel is False
    assert st.door_open is False
    assert st.alerts is True
    assert st.special_mode == 0
    assert st.valve_1_raw == 0      # register 0x14
    assert st.valve_2_raw == 0      # register 0x13
    assert st.valve_3_raw == 1023   # register 0x15


def test_reading_set_valves_reach_state():
    st = P.StoveState()
    st.apply_packed(P.parse_reading_set(LIVE_59))
    assert st.valves == {"1": 0, "2": 0, "3": 1023}
    assert st.valve_gauges["3"] < 1.0  # 1023 -> gauge ~0%


def test_reading_set_unknown_length():
    assert P.parse_reading_set("0123456789") is None


def test_full_status_56():
    # built per the offsets in parse_full_status (56-char variant:
    # 3-digit valve fields), sum-checked to 56
    s = (
        "01352890"   # [0:8]  date
        "4DC"        # [8:11] minutes of day
        "0070"       # [11:15] stove temp
        "001"        # [15:18] light raw
        "0144"       # [18:22] room temp /10
        "002E"       # [22:26] setpoint /2
        "0"          # [26] door
        "0"          # [27] overfire
        "1"          # [28] extended burn
        "0"          # [29] check fuel
        "0"          # [30] burning
        "12"         # [31:33] burn cycle
        "1"          # [33] mode
        "00C0"       # [34:38] error
        "0D"         # [38:40] dial
        "000"        # [40:43] valve 2 raw
        "3FF"        # [43:46] valve 1 raw
        "000"        # [46:49] valve 3 raw
        "2"          # [49] unknown_s
        "1"          # [50] alerts
        "0"          # [51] special mode
        "00CB"       # [52:56] board temp /10
    )
    assert len(s) == 56, len(s)
    st = P.parse_full_status(s)
    assert st is not None
    assert st.stove_temp == 0x0070
    assert st.light_raw == 1
    assert st.room_temp == 32.4
    assert st.room_setpoint == 23.0
    assert st.mode == 1
    assert st.burn_cycle == 18
    assert st.error_code == 0xC0


def test_apply_packed_without_valves_is_safe():
    st = P.StoveState()
    packed = P.PackedStatus()
    packed.stove_temp = 100
    st.apply_packed(packed)  # must not raise (regression: missing fields)
    assert st.valves == {}


def test_register_roundtrip():
    st = P.StoveState()
    assert P.apply_data_register(st, 0x00, struct.pack("<i", 324))
    assert st.stove_temp == 324
    assert P.apply_data_register(st, 0x14, struct.pack("<i", 1023))
    assert st.valves["1"] == 1023
    assert not P.apply_data_register(st, 0xFE, b"\x00")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                failures += 1
                print("FAIL", name, "->", repr(e))
    raise SystemExit(1 if failures else 0)
