"""bleak-based client for the Charnwood E stove."""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import asyncio
import datetime as dt
import struct
import time
import logging

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

from . import protocol as P

log = logging.getLogger(__name__)

CONNECTION_TIMEOUT = 45.0


def _ascii(b: bytes) -> str:
    return b.decode("ascii", errors="replace").strip("\x00").strip()


class Stove:
    """Connection to one Charnwood stove over BLE."""

    def __init__(self, address: str):
        self.address = address
        self._client: BleakClient | None = None
        self.state = P.StoveState()
        self.notifications_active = False
        self.last_push: float | None = None

    def push_age(self) -> float | None:
        """Seconds since the last notification push (None if never)."""
        return None if self.last_push is None else time.monotonic() - self.last_push

    @staticmethod
    async def read_wifi(ip: str, timeout: float = 2.5) -> P.StoveState:
        """Fetch the packed status snapshot over WiFi (~50 ms on LAN).

        Read-only: the stove serves GET /get-reading-set; control always
        requires BLE.
        """
        def fetch() -> bytes:
            import urllib.request

            with urllib.request.urlopen(
                f"http://{ip}/get-reading-set", timeout=timeout
            ) as r:
                return r.read()

        raw = (await asyncio.to_thread(fetch)).decode("ascii", "replace").strip()
        packed = P.parse_reading_set(raw)
        if packed is None:
            raise ConnectionError(
                f"unexpected reading-set payload from {ip} (len {len(raw)})"
            )
        st = P.StoveState()
        st.apply_packed(packed)
        return st

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    async def scan(timeout: float = 12.0, attempts: int = 4) -> list[BLEDevice]:
        """Return BLE devices advertising the Charnwood service.

        Retries when another client (e.g. blueman) is mid-discovery and
        BlueZ answers org.bluez.Error.InProgress.
        """
        wanted = (P.STOVE_SERVICE, P.LEGACY_SERVICE)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await Stove._scan_once(wanted, timeout)
            except Exception as e:
                last_exc = e
                if "InProgress" in type(e).__name__ or "InProgress" in str(e):
                    log.debug("discovery busy (attempt %d/%d), retrying", attempt + 1, attempts)
                    await asyncio.sleep(4)
                    continue
                raise
        raise BleakError(
            "the Bluetooth adapter is busy with another scan (from blueman, "
            "nubertctl, or another openwood); wait a few seconds and retry, "
            "or set a default stove with `openwood use` to avoid scanning"
        ) from last_exc

    @staticmethod
    async def _scan_once(wanted: tuple, timeout: float) -> list[BLEDevice]:
        try:
            return await BleakScanner.discover(
                timeout=timeout,
                service_uuids=list(wanted),
                return_adv=False,
            )
        except Exception as e:
            log.debug("filtered scan failed (%s); retrying unfiltered", e)
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        stoves = []
        for addr, (device, adv) in found.items():
            uuids = {u.lower() for u in (adv.service_uuids or [])}
            if any(u in uuids for u in wanted) or (device.name or "").startswith("Charnwood"):
                stoves.append(device)
        return stoves

    async def connect(self, timeout: float = CONNECTION_TIMEOUT) -> None:
        if self._client is not None and self._client.is_connected:
            return
        client = BleakClient(self.address)
        await client.connect(timeout=timeout)
        try:
            # The app negotiates MTU 80; BlueZ negotiates automatically,
            # but ask politely where the backend supports it.
            exchange = getattr(client, "exchange_mtu", None)
            if exchange:
                try:
                    await exchange(80)
                except Exception:  # pragma: no cover - backend dependent
                    pass
        except Exception:
            pass
        self._client = client
        log.debug("connected to %s", self.address)

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self._client = None

    async def __aenter__(self) -> "Stove":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _require(self) -> BleakClient:
        if not self.connected or self._client is None:
            raise ConnectionError("not connected to stove")
        return self._client

    # -- raw I/O -----------------------------------------------------------

    async def _read_with_retry(self, uuid: str, attempts: int = 3) -> bytes:
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return await asyncio.wait_for(
                    self._require().read_gatt_char(uuid), timeout=10.0
                )
            except Exception as e:
                last_exc = e
                log.debug("read %s attempt %d failed: %s", uuid, i + 1, e)
                await asyncio.sleep(0.3 * (i + 1))
        raise last_exc  # type: ignore[misc]

    async def read_data(self, index: int) -> bytes:
        return await self._read_with_retry(P.data_uuid(index))

    async def read_meta(self, index: int) -> bytes:
        return await self._read_with_retry(P.meta_uuid(index))

    async def _write_with_retry(self, uuid: str, value: bytes, attempts: int = 3) -> None:
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                await asyncio.wait_for(
                    self._require().write_gatt_char(uuid, value, response=True),
                    timeout=10.0,
                )
                return
            except Exception as e:
                last_exc = e
                log.debug("write %s attempt %d failed: %s", uuid, i + 1, e)
                await asyncio.sleep(0.3 * (i + 1))
        raise last_exc  # type: ignore[misc]

    async def write_data(self, index: int, value: bytes) -> None:
        await self._write_with_retry(P.data_uuid(index), value)

    async def write_meta(self, index: int, value: bytes) -> None:
        await self._write_with_retry(P.meta_uuid(index), value)

    async def subscribe_notifications(self) -> bool:
        """Subscribe to value-change pushes from the stove.

        The Android app only polls, but the stove firmware also notifies
        changed registers roughly every 5 s. Subscribing needs pacing
        (0.4 s between descriptor writes) or the ESP32 drops the link.
        Returns True if at least one subscription succeeded.
        """
        client = self._require()
        targets = [
            ch
            for svc in client.services
            for ch in svc.characteristics
            if "notify" in ch.properties and ch.uuid.startswith("85c7ff")
        ]
        ok = 0
        for ch in targets:
            try:
                await client.start_notify(ch, self._on_notify)
                ok += 1
            except Exception as e:
                log.debug("notify subscribe failed on %s: %s", ch.uuid, e)
            await asyncio.sleep(0.4)
        self.notifications_active = ok > 0
        log.debug("subscribed to %d/%d notifiable registers", ok, len(targets))
        return self.notifications_active

    def _on_notify(self, ch: BleakGATTCharacteristic, data: bytearray) -> None:
        idx = int(ch.uuid[6:8], 16)
        self.last_push = time.monotonic()
        P.apply_data_register(self.state, idx, bytes(data))

    # -- state -------------------------------------------------------------

    async def read_all(self, include_meta: bool = False) -> P.StoveState:
        """Poll all registers and return a full snapshot (like the app)."""
        st = P.StoveState()

        async def rdx(index: int) -> bytes | None:
            try:
                return await self.read_data(index)
            except Exception as e:
                log.warning("register %02x unreadable: %s", index, e)
                return None

        b_st = await rdx(P.R_STOVE_TEMP)
        if b_st is not None:
            st.stove_temp = P.stove_temp_display(P.int32_le(b_st))
        b_int = await rdx(P.R_INTENSITY)
        if b_int is not None:
            st.intensity = P.intensity_percent(P.int32_le(b_int))
        b_rt = await rdx(P.R_ROOM_TEMP)
        if b_rt is not None:
            st.room_temp = P.room_temp_display(P.float32_le(b_rt))
        b_bt = await rdx(P.R_BOARD_TEMP)
        if b_bt is not None:
            st.board_temp = P.float32_le(b_bt)
        b_sp = await rdx(P.R_ROOM_SETPOINT)
        if b_sp is not None:
            st.room_setpoint = P.float32_le(b_sp)
        b = await rdx(P.R_DOOR_OPEN)
        if b:
            st.door_open = b[0] == 1
        b = await rdx(P.R_OVERFIRE)
        if b:
            st.overfire = b[0] == 1
        b = await rdx(P.R_OVERNIGHT)
        if b:
            st.extended_burn = b[0] == 1
        b = await rdx(P.R_CHECK_FUEL)
        if b:
            st.check_fuel = b[0] == 1
        b = await rdx(P.R_BURNING)
        if b:
            st.burning = b[0] == 1
        b = await rdx(P.R_BURN_CYCLE)
        if b:
            st.burn_cycle = P.int32_le(b)
        b = await rdx(P.R_MODE)
        if b:
            st.mode = b[0]
        b = await rdx(P.R_MANUAL_LEVEL)
        if b:
            st.manual_level = b[0]
        b = await rdx(P.R_ERROR)
        if b:
            st.error_code = P.int32_le(b)
        b = await rdx(P.R_FUEL_ALERT)
        if b:
            st.alerts = b[0] == 1
        b = await rdx(P.R_SPECIAL_MODE)
        if b:
            st.special_mode = P.int32_le(b)
        b = await rdx(P.R_UNKNOWN_S)
        if b:
            st.unknown_s = b[0]
        b = await rdx(P.R_VALVE_1)
        v1 = P.int32_le(b) if b else None
        b = await rdx(P.R_VALVE_2)
        v2 = P.int32_le(b) if b else None
        b = await rdx(P.R_VALVE_3)
        v3 = P.int32_le(b) if b else None
        st.valves = {"1": v1, "2": v2, "3": v3}

        if include_meta:
            b = await self._try(self.read_meta(P.M_FIRMWARE_VERSION))
            if b is not None:
                st.firmware_version = P.int32_le(b)
            b = await self._try(self.read_meta(P.M_FIRMWARE_RELEASE))
            if b is not None:
                st.firmware_release = _ascii(b)
            b = await self._try(self.read_meta(P.M_LOCATION))
            if b is not None:
                st.location = _ascii(b)
            b = await self._try(self.read_meta(P.M_DATETIME))
            if b is not None:
                st.datetime = _ascii(b)
            b = await self._try(self.read_meta(P.M_BURN_MINUTES))
            if b is not None:
                st.burn_minutes = P.uint16_le(b)
            b = await self._try(self.read_meta(P.M_WIFI_SSID))
            if b is not None:
                st.wifi_ssid = _ascii(b)
            b = await self._try(self.read_meta(P.M_IP_ADDRESS))
            if b is not None:
                st.ip_address = _ascii(b)
            b = await self._try(self.read_meta(P.M_WIFI_STATUS))
            if b is not None:
                st.wifi_status = _ascii(b)
            b = await self._try(self.read_meta(P.M_UPDATE_URL))
            if b is not None:
                st.update_url = _ascii(b)

        self.state = st
        return st

    @staticmethod
    async def _try(coro) -> bytes | None:
        try:
            return await coro
        except Exception as e:
            log.warning("meta register unreadable: %s", e)
            return None

    async def read_full_status(self) -> P.StoveState:
        """Read the packed status string register (single-shot snapshot)."""
        raw = _ascii(await self.read_meta(P.M_FULL_STATUS))
        packed = P.parse_full_status(raw)
        if packed is None:
            raise ValueError(f"unexpected status string length {len(raw)}: {raw!r}")
        st = P.StoveState()
        st.apply_packed(packed)
        self.state = st
        return st

    # -- control (mirrors the Android app's writes) ------------------------

    async def set_mode(
        self,
        mode: int,
        manual_level: int | None = None,
        room_setpoint: float | None = None,
    ) -> None:
        """Set operating mode.

        The app always writes level + setpoint + mode together; we mirror
        that using current values where the caller did not supply one.
        """
        if mode not in (P.MODE_AUTOMATIC, P.MODE_ROOM_TEMP, P.MODE_TEST):
            raise ValueError("mode must be 0 (automatic), 1 (room-temp) or 2 (test)")
        cur = self.state
        level = manual_level if manual_level is not None else (cur.manual_level or 3)
        setp = room_setpoint if room_setpoint is not None else (cur.room_setpoint or 21.0)
        await self.write_data(P.R_MANUAL_LEVEL, P.big_endian_minimal(level))
        await self.write_data(
            P.R_ROOM_SETPOINT, struct.pack("<f", float(setp))
        )
        await self.write_data(P.R_MODE, bytes([mode]))
        self.state.mode = mode
        self.state.manual_level = level
        self.state.room_setpoint = float(setp)

    async def set_intensity(self, level: int) -> None:
        """Set burn intensity 1-5 (Automatic mode)."""
        if not 1 <= level <= 5:
            raise ValueError("intensity must be 1..5")
        await self.write_data(P.R_MANUAL_LEVEL, P.big_endian_minimal(level))
        await self.write_data(
            P.R_ROOM_SETPOINT, struct.pack("<f", float(self.state.room_setpoint or 21.0))
        )
        await self.write_data(P.R_MODE, bytes([P.MODE_AUTOMATIC]))
        self.state.mode = P.MODE_AUTOMATIC
        self.state.manual_level = level

    set_manual_level = set_intensity

    async def set_room_setpoint(self, celsius: float) -> None:
        """Set the room temperature setpoint (Room Temperature mode), 16.0-30.0 by 0.5."""
        if not 16.0 <= celsius <= 30.0:
            raise ValueError("setpoint must be between 16.0 and 30.0")
        setp = round(celsius * 2) / 2.0
        await self.write_data(
            P.R_ROOM_SETPOINT, struct.pack("<f", setp)
        )
        await self.write_data(P.R_MANUAL_LEVEL, P.big_endian_minimal(self.state.manual_level or 3))
        await self.write_data(P.R_MODE, bytes([P.MODE_ROOM_TEMP]))
        self.state.mode = P.MODE_ROOM_TEMP
        self.state.room_setpoint = setp

    async def set_test_air(self, percent: int) -> None:
        """Test mode: air control percent 0-100 (25/50/75/100 presets).

        Service mode; the stove reverts to Automatic when the door is opened.
        """
        if not 0 <= percent <= 100:
            raise ValueError("air percent must be 0..100")
        await self.write_data(P.R_MANUAL_LEVEL, P.big_endian_minimal(percent))
        await self.write_data(
            P.R_ROOM_SETPOINT, struct.pack("<f", float(self.state.room_setpoint or 21.0))
        )
        await self.write_data(P.R_MODE, bytes([P.MODE_TEST]))
        self.state.mode = P.MODE_TEST
        self.state.manual_level = percent

    async def boost(self) -> None:
        await self.set_test_air(50)

    async def set_extended_burn(self, on: bool) -> None:
        """Extended burn ("overnight" in the app): preserve a char firebed."""
        await self.write_data(P.R_OVERNIGHT, bytes([1 if on else 0, 0]))

    set_overnight = set_extended_burn

    async def set_alerts(self, on: bool) -> None:
        """Reload-light alerts ("fuel alert" in the app)."""
        await self.write_data(P.R_FUEL_ALERT, bytes([1 if on else 0]))

    set_fuel_alert = set_alerts

    async def special_mode(self, value: int) -> None:
        """1 = powercut mode, 2 = chimney fire mode, 3 = cancel/restart."""
        if value not in (P.SPECIAL_POWERCUT, P.SPECIAL_CHIMNEY_FIRE, P.SPECIAL_CANCEL):
            raise ValueError("special mode value must be 1, 2 or 3")
        await self.write_data(P.R_SPECIAL_MODE, bytes([value]))

    # -- meta operations ----------------------------------------------------

    async def sync_time(self) -> str:
        """Write local time to the stove (yyyyMMddHHmmss), like the app."""
        now = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        await self.write_meta(P.M_DATETIME, now.encode("ascii"))
        return now

    async def get_stove_time(self) -> str:
        return _ascii(await self.read_meta(P.M_DATETIME))

    async def set_wifi(self, ssid: str, password: str) -> None:
        """Provision WiFi credentials over BLE and apply them."""
        await self.write_meta(P.M_WIFI_SSID, ssid.encode("ascii"))
        await self.write_meta(P.M_WIFI_PASSWORD, password.encode("ascii"))
        await self.write_meta(P.M_WIFI_COMMAND, bytes([P.WIFI_SAVE]))

    async def delete_wifi(self) -> None:
        await self.write_meta(P.M_WIFI_COMMAND, bytes([P.WIFI_DELETE]))

    async def delete_pairing(self) -> None:
        await self.write_meta(P.M_COMMAND, bytes([P.CMD_DELETE_PAIRING]))

    async def start_firmware_update(self) -> None:
        await self.write_meta(P.M_COMMAND, bytes([P.CMD_START_UPDATE]))

    async def start_special_update(self) -> None:
        await self.write_meta(P.M_COMMAND, bytes([P.CMD_SPECIAL_UPDATE]))

    async def read_update_status(self) -> tuple[int, int]:
        """Return (update_status, op_error)."""
        status = (await self.read_meta(P.M_UPDATE_STATUS))[0]
        op_err = P.int32_le(await self.read_meta(P.M_OP_ERROR))
        return status, op_err
