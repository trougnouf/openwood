"""Charnwood E stove BLE protocol constants and parsers.

Reverse engineered from the Charnwood-E Android app v2.0.31 (see PROTOCOL.md).
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import struct
from dataclasses import dataclass, field

STOVE_SERVICE = "116eff00-d316-476f-b90e-e8b186ac4bc5"
LEGACY_SERVICE = "2a51ff00-8045-425a-99bb-60ac5015c409"

DATA_BASE = "85c7ff{:02x}-c814-4363-9cfa-b4469459dc22"
META_BASE = "7877ff{:02x}-93d5-4f70-b4ac-ec360a6944c9"


def data_uuid(index: int) -> str:
    return DATA_BASE.format(index)


def meta_uuid(index: int) -> str:
    return META_BASE.format(index)


# DATA register indices
R_STOVE_TEMP = 0x00
R_INTENSITY = 0x01  # raw 0-400 -> percent; app calls it 'intensity'
R_LIGHT_LEVEL = R_INTENSITY  # legacy alias
R_ROOM_TEMP = 0x02
R_ROOM_SETPOINT = 0x03
R_DOOR_OPEN = 0x04
R_OVERFIRE = 0x05
R_OVERNIGHT = 0x07
R_CHECK_FUEL = 0x08
R_BURNING = 0x09
R_BURN_CYCLE = 0x0D
R_MODE = 0x0F
R_ERROR = 0x11
R_MANUAL_LEVEL = 0x12
R_VALVE_2 = 0x13  # air inlet valve 2 raw position (app: f4707p)
R_VALVE_1 = 0x14  # air inlet valve 1 raw position (app: f4708q)
R_VALVE_3 = 0x15  # air inlet valve 3 raw position (app: f4709r)
R_UNKNOWN_S = 0x16
R_FUEL_ALERT = 0x17
R_BOARD_TEMP = 0x18
R_SPECIAL_MODE = 0x19

# META register indices
M_FIRMWARE_VERSION = 0x00
M_FIRMWARE_RELEASE = 0x01
M_LOCATION = 0x02
M_DATETIME = 0x03
M_WIFI_COMMAND = 0x04
M_WIFI_SSID = 0x05
M_WIFI_PASSWORD = 0x06
M_IP_ADDRESS = 0x07
M_WIFI_STATUS = 0x08
M_COMMAND = 0x09
M_UPDATE_STATUS = 0x0A
M_OP_ERROR = 0x0B
M_BURN_MINUTES = 0x0C
M_UPDATE_URL = 0x0E
M_FULL_STATUS = 0x0F

# META command values (register M_COMMAND)
CMD_START_UPDATE = 1
CMD_SPECIAL_UPDATE = 2
CMD_DELETE_PAIRING = 3

# WiFi command values (register M_WIFI_COMMAND)
WIFI_SAVE = 2
WIFI_DELETE = 3

# Special mode values (register R_SPECIAL_MODE)
SPECIAL_POWERCUT = 1
SPECIAL_CHIMNEY_FIRE = 2
SPECIAL_CANCEL = 3

# Modes (per the manual: blue=Automatic, green=Room Temperature, red=Test)
MODE_AUTOMATIC = 0
MODE_ROOM_TEMP = 1
MODE_TEST = 2

MODE_NAMES = {MODE_AUTOMATIC: "automatic", MODE_ROOM_TEMP: "room_temp", MODE_TEST: "test"}

SPECIAL_NAMES = {0: "none", SPECIAL_POWERCUT: "powercut", SPECIAL_CHIMNEY_FIRE: "chimney_fire"}

# Codes the app treats as non-errors.
_OK_CODES = {0, 0xC0, 0xE2, 0xE3, 0xE4}

ERROR_NAMES = {
    0x01: "FileSystemCheck - SD card not readable or not present",
    0x02: "SaveWiFiCredentials - WiFi credentials cannot be zero length",
    0x03: "ReadWiFiCredentials - No WiFi credentials have been saved in flash",
    0x04: "RecallWiFiCredentials - WiFi credentials backup file is corrupted",
    0x05: "RecallWiFiCredentialsBackup - Cannot delete WiFi credentials backup file from SD card",
    0x06: "WiFiStatus - Unable to connect to WiFi access point",
    0x07: "WiFiConnect - connection already established",
    0x08: "DownloadFile - cannot connect to update server",
    0x09: "DownloadFile - file cannot be created on SD card for update",
    0x0A: "CleanUpdates - update files cannot be deleted",
    0x0B: "CleanUpdates - update directory cannot be deleted",
    0x0C: "VerifyHash - query file cannot be opened for verification",
    0x0D: "VerifyHash - hash file cannot be opened for verification",
    0x0E: "VerifyHash - comparison between local hash and remote hash failed",
    0x0F: "UpdateESP - esp32 update file does not exist",
    0x10: "UpdateESP - esp32 update file has no contents",
    0x11: "UpdateESP - esp32 update did not write all data to flash",
    0x12: "UpdateESP - esp32 update has failed",
    0x13: "UpdateESP - not enough space in flash to begin update",
    0x14: "UpdateATM - file does not exist",
    0x15: "UpdateATM - no response from receiver",
    0x16: "UpdateATM - incomplete response from receiver",
    0x17: "UpdateATM - invalid response - bad start byte",
    0x18: "UpdateATM - invalid response - sequence counter mismatch",
    0x19: "UpdateATM - invalid response - invalid token",
    0x1A: "UpdateATM - invalid response - command mismatch",
    0x1B: "UpdateATM - receiver panic, command response: not ok",
    0x1C: "UpdateATM - invalid checksum received",
    0x1D: "UpdateATM - sent blocks do not match read blocks",
    0x1E: "Log file cannot be read",
    0x20: "BLE - bluetooth modem not sleeping",
    0x21: "BLE - bluetooth modem not waking up",
    0x22: "Update - no update available",
    0x23: "GetSerialNumber - Serial number not found",
    0x24: "GetSerialNumber - Serial number file not found",
    0x25: "SaveSerialNumber - Unable to write serial number",
    0x26: "Certificate file cannot be read",
    0x27: "Alternative certificate file cannot be read",
    0x30: "SSID or password length is zero",
    0xC0: "Packet receive timeout",
    0xC1: "Update - incoming data integrity error",
    0xC2: "Update - incoming command integrity error",
    0xC3: "Update - invalid command",
    0xC4: "Update - no response from receiver",
    0xE0: "Pinmap execution failure",
    0xE1: "Disk geometry read error base",
    0xE2: "Disk 1 geometry read error",
    0xE3: "Disk 2 geometry read error",
    0xE4: "Disk 3 geometry read error",
    0xE5: "Disk failed to initialise base error",
    0xE6: "Disk 1 failed to initialise",
    0xE7: "Disk 2 failed to initialise",
    0xE8: "Disk 3 failed to initialise",
    0xF0: "Failed to calibrate base error",
    0xF1: "Motor 1 failed to calibrate",
    0xF2: "Motor 2 failed to calibrate",
    0xF3: "Motor 3 failed to calibrate",
    0xF4: "Too frequent calibration base error",
    0xF5: "Motor 1 too frequent calibration error",
    0xF6: "Motor 2 too frequent calibration error",
    0xF7: "Motor 3 too frequent calibration error",
    0xF8: "Check disconnected peripheral connection 2, 3 or 4",
    0xF9: "Check disconnected stove thermocouple 5",
    0xFA: "Check disconnected room thermocouple 6",
}

# Raw burn-cycle values index into this state machine (from the app's
# burn_cycle_list resource). "E" states are the automated (auto-mode) burn.
BURN_CYCLE_NAMES = [
    "STARTUP",                       # 0
    "LIGHTING",                      # 1
    "EARLY BURN",                    # 2
    "EARLY BURN INEFFICIENT",        # 3
    "STEADY STATE EFFICIENT",        # 4
    "STEADY STATE INEFFICIENT",      # 5
    "LATE BURN EFFICIENT",           # 6
    "LATE BURN INEFFICIENT",         # 7
    "CHAR",                          # 8
    "OVERNIGHT",                     # 9
    "TEST",                          # 10
    "OVER-FIRE",                     # 11
    "STAND BY",                      # 12
    "SLEEP",                         # 13
    "POWER CUT MODE",                # 14
    "CHIMNEY FIRE RESPONSE MODE",    # 15
    "EMC TEST",                      # 16
    "PIDCAL",                        # 17
    "LIGHTING E",                    # 18
    "EARLY BURN E",                  # 19
    "STEADY STATE EFFICIENT E",      # 20
    "STEADY STATE INEFFICIENT E",    # 21
    "CHAR E",                        # 22
]

# Maps each state to the app's progress-ring position (0 = ring hidden).
BURN_CYCLE_STEPS = [0, 1, 2, 2, 3, 3, 3, 3, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 3, 4]


def error_is_ok(code: int) -> bool:
    return code in _OK_CODES or code == 0


def error_text(code: int) -> str:
    return ERROR_NAMES.get(code, f"unknown error 0x{code:02x}")


# --- raw value helpers ------------------------------------------------------


def int32_le(b: bytes) -> int:
    return struct.unpack("<i", b[:4].ljust(4, b"\x00"))[0]


def uint16_le(b: bytes) -> int:
    return struct.unpack("<H", b[:2].ljust(2, b"\x00"))[0]


def float32_le(b: bytes) -> float:
    return struct.unpack("<f", b[:4].ljust(4, b"\x00"))[0]


def hex16(s: str) -> int:
    """The app's h.q(): 4 hex chars -> signed 16-bit."""
    v = int(s, 16)
    return v - 0x10000 if v & 0x8000 else v


def intensity_percent(raw: int) -> int:
    return 100 if raw >= 400 else int(raw * 100 / 400)


# legacy alias
light_percent = intensity_percent


VALVE_FAULT = 1111


def valve_gauge(raw: int) -> float:
    """App's disc gauge position for a valve: 0..100% across a 270 degree dial.

    Pointer angle = ((1024 - raw) * 270 / 1024) - 135 degrees.
    """
    if raw == VALVE_FAULT:
        return -1.0  # fault marker (red dot in the app)
    v = min(max(raw, 0), 1024)
    return (1024 - v) / 1024 * 100.0


def stove_temp_display(raw: int) -> int:
    return 999 if raw > 1500 else raw


def room_temp_display(raw: float) -> float:
    return 99.9 if raw > 1000.0 else raw


def big_endian_minimal(n: int) -> bytes:
    """The app's BigInteger.valueOf(n).toByteArray()."""
    if n == 0:
        return b"\x00"
    length = (n.bit_length() + 7) // 8
    return n.to_bytes(length, "big")


# --- packed status strings ---------------------------------------------------


@dataclass
class PackedStatus:
    minutes_of_day: int = 0
    stove_temp: int = 0
    room_temp: float = 0.0
    board_temp: float = 0.0
    light_raw: int = 0
    room_setpoint: float = 21.0
    door_open: bool = False
    overfire: bool = False
    extended_burn: bool = False
    check_fuel: bool = False
    burning: bool = False
    burn_cycle: int = 0
    mode: int = 0
    error_code: int = 0
    manual_level: int = 0
    alerts: bool = False
    special_mode: int = 0
    valve_1_raw: int | None = None
    valve_2_raw: int | None = None
    valve_3_raw: int | None = None


def parse_full_status(s: str) -> PackedStatus | None:
    """Parse the 56/60-char ASCII status string from register 7877ff0f."""
    s = s.strip("\x00").strip()
    if len(s) == 56:
        p_ofs, board_ofs = 3, slice(52, 56)
    elif len(s) == 60:
        p_ofs, board_ofs = 4, slice(55, 59)
    else:
        return None
    st = PackedStatus()
    st.minutes_of_day = int(s[8:11], 16)
    st.stove_temp = hex16(s[11:15])
    st.light_raw = int(s[15:18], 16)
    st.room_temp = hex16(s[18:22]) / 10.0
    st.room_setpoint = hex16(s[22:26]) / 2.0
    st.door_open = s[26] == "1"
    st.overfire = s[27] == "1"
    st.extended_burn = s[28] == "1"
    st.check_fuel = s[29] == "1"
    st.burning = s[30] == "1"
    st.burn_cycle = int(s[31:33], 16)
    st.mode = int(s[33], 16)
    st.error_code = int(s[34:38], 16)
    st.manual_level = int(s[38:40], 16)
    if len(s) == 56:
        st.board_temp = hex16(s[board_ofs]) / 10.0
        # 56-char variant uses 3-digit threshold fields we do not expose.
    else:
        st.board_temp = hex16(s[board_ofs]) / 10.0
    if len(s) == 60:
        st.alerts = s[53] != "0"
        st.special_mode = int(s[54], 16)
    else:
        st.alerts = s[50] != "0"
        st.special_mode = int(s[51], 16)
    _ = p_ofs
    return st


def parse_reading_set(s: str) -> PackedStatus | None:
    """Parse the HTTP /get-reading-set response (37 or 59 hex chars)."""
    s = s.strip()
    st = PackedStatus()
    if len(s) == 37:
        st.minutes_of_day = int(s[0:3], 16)
        st.room_temp = hex16(s[3:7]) / 10.0
        st.stove_temp = hex16(s[7:11])
        st.board_temp = hex16(s[11:15]) / 10.0
        st.light_raw = int(s[15:18], 16)
        st.door_open = s[27:29] != "00"
        st.burn_cycle = int(s[29:31], 16)
        st.manual_level = int(s[31:33], 16)
        st.mode = int(s[33:35], 16)
        st.room_setpoint = hex16(s[35:37]) / 2.0
        st.valve_1_raw = int(s[18:21], 16)   # register 0x14
        st.valve_2_raw = int(s[21:24], 16)   # register 0x13
        st.valve_3_raw = int(s[24:27], 16)   # register 0x15
        return st
    if len(s) == 59:
        st.minutes_of_day = int(s[8:11], 16)
        st.stove_temp = hex16(s[11:15])
        st.light_raw = int(s[15:18], 16)
        st.room_temp = hex16(s[18:22]) / 10.0
        st.room_setpoint = hex16(s[22:26]) / 2.0
        st.door_open = s[26] == "1"
        st.overfire = s[27] == "1"
        st.extended_burn = s[28] == "1"
        st.check_fuel = s[29] == "1"
        st.burning = s[30] == "1"
        st.burn_cycle = int(s[31:33], 16)
        st.mode = int(s[33], 16)
        st.error_code = int(s[34:38], 16)
        st.manual_level = int(s[38:40], 16)
        st.alerts = s[53] != "0"
        st.special_mode = int(s[54], 16)
        st.board_temp = hex16(s[55:59]) / 10.0
        st.valve_2_raw = int(s[40:44], 16)   # register 0x13
        st.valve_1_raw = int(s[44:48], 16)   # register 0x14
        st.valve_3_raw = int(s[48:52], 16)   # register 0x15
        return st
    return None


def apply_data_register(st: "StoveState", index: int, data: bytes) -> bool:
    """Update one field of a StoveState from a DATA register value.

    Returns True if the register was known and applied.
    """
    try:
        if index == R_STOVE_TEMP:
            st.stove_temp = stove_temp_display(int32_le(data))
        elif index == R_INTENSITY:
            st.intensity = intensity_percent(int32_le(data))
        elif index == R_ROOM_TEMP:
            st.room_temp = room_temp_display(float32_le(data))
        elif index == R_ROOM_SETPOINT:
            st.room_setpoint = float32_le(data)
        elif index == R_BOARD_TEMP:
            st.board_temp = float32_le(data)
        elif index == R_DOOR_OPEN:
            st.door_open = data[0] == 1
        elif index == R_OVERFIRE:
            st.overfire = data[0] == 1
        elif index == R_VALVE_1:
            st.valves["1"] = int32_le(data)
        elif index == R_VALVE_2:
            st.valves["2"] = int32_le(data)
        elif index == R_VALVE_3:
            st.valves["3"] = int32_le(data)
        elif index == R_OVERNIGHT:
            st.extended_burn = data[0] == 1
        elif index == R_CHECK_FUEL:
            st.check_fuel = data[0] == 1
        elif index == R_BURNING:
            st.burning = data[0] == 1
        elif index == R_BURN_CYCLE:
            st.burn_cycle = int32_le(data)
        elif index == R_MODE:
            st.mode = data[0]
        elif index == R_MANUAL_LEVEL:
            st.manual_level = data[0]
        elif index == R_ERROR:
            st.error_code = int32_le(data)
        elif index == R_FUEL_ALERT:
            st.alerts = data[0] == 1
        elif index == R_SPECIAL_MODE:
            st.special_mode = int32_le(data)
        elif index == R_UNKNOWN_S:
            st.unknown_s = data[0]
        else:
            return False
        return True
    except Exception:
        return False


@dataclass
class StoveState:
    """Full snapshot of the stove."""

    stove_temp: int | None = None
    room_temp: float | None = None
    board_temp: float | None = None
    room_setpoint: float | None = None
    intensity: int | None = None  # percent (live combustion output)
    mode: int | None = None
    manual_level: int | None = None
    extended_burn: bool | None = None
    alerts: bool | None = None
    door_open: bool | None = None
    overfire: bool | None = None
    check_fuel: bool | None = None
    burning: bool | None = None
    burn_cycle: int | None = None
    error_code: int | None = None
    special_mode: int | None = None
    burn_minutes: int | None = None
    firmware_version: int | None = None
    firmware_release: str | None = None
    location: str | None = None
    datetime: str | None = None
    wifi_ssid: str | None = None
    ip_address: str | None = None
    wifi_status: str | None = None
    update_url: str | None = None
    valves: dict = field(default_factory=dict)  # raw positions, keys '1','2','3'
    unknown_s: int | None = None

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, f"unknown({self.mode})")

    @property
    def burn_cycle_progress(self) -> int | None:
        if self.burn_cycle is None or self.burn_cycle >= len(BURN_CYCLE_STEPS):
            return None
        return BURN_CYCLE_STEPS[self.burn_cycle]

    @property
    def burn_cycle_name(self) -> str:
        if self.burn_cycle is None:
            return ""
        if self.burn_cycle < len(BURN_CYCLE_NAMES):
            return BURN_CYCLE_NAMES[self.burn_cycle]
        return f"unknown({self.burn_cycle})"

    @property
    def valve_gauges(self) -> dict:
        """Valve gauge positions in % (app dial); -1 marks a fault."""
        return {k: valve_gauge(v) if v is not None else None for k, v in self.valves.items()}

    @property
    def valve_faults(self) -> list:
        return [k for k, v in self.valves.items() if v == VALVE_FAULT]

    @property
    def error_text(self) -> str:
        if self.error_code is None:
            return ""
        if error_is_ok(self.error_code):
            return "OK"
        return error_text(self.error_code)

    @property
    def special_mode_name(self) -> str:
        return SPECIAL_NAMES.get(self.special_mode, f"unknown({self.special_mode})")

    @property
    def has_error(self) -> bool:
        return self.error_code is not None and not error_is_ok(self.error_code)

    def apply_packed(self, st: PackedStatus) -> None:
        self.stove_temp = stove_temp_display(st.stove_temp)
        self.room_temp = room_temp_display(st.room_temp)
        self.board_temp = st.board_temp
        self.room_setpoint = st.room_setpoint
        self.intensity = intensity_percent(st.light_raw)
        self.door_open = st.door_open
        self.overfire = st.overfire
        self.extended_burn = st.extended_burn
        self.check_fuel = st.check_fuel
        self.burning = st.burning
        self.burn_cycle = st.burn_cycle
        self.mode = st.mode
        self.error_code = st.error_code
        self.manual_level = st.manual_level
        self.alerts = st.alerts
        self.special_mode = st.special_mode
        if st.valve_1_raw is not None or st.valve_2_raw is not None \
                or st.valve_3_raw is not None:
            self.valves = {
                "1": st.valve_1_raw,
                "2": st.valve_2_raw,
                "3": st.valve_3_raw,
            }

    def to_dict(self) -> dict:
        d = {k: v for k, v in vars(self).items() if v is not None}
        d.pop("valves", None)
        d["valve_positions"] = self.valve_gauges
        d["valve_faults"] = self.valve_faults
        d["mode_name"] = self.mode_name
        d["special_mode_name"] = self.special_mode_name
        d["error_text"] = self.error_text
        d["burn_cycle_name"] = self.burn_cycle_name
        d["burn_cycle_progress"] = self.burn_cycle_progress
        return d
