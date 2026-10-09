"""MQTT bridge exposing the stove to Home Assistant (with discovery).

Optionally acts as an external thermostat: it subscribes to any MQTT
temperature topic (e.g. a bedroom sensor) and adjusts the stove's room
setpoint, since the stove itself only knows the sensor in its power cable.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import asyncio
import json
import logging
import time

from . import config
from . import protocol as P
from .client import Stove

log = logging.getLogger(__name__)


class DiscoveryBuilder:
    """Builds Home Assistant MQTT discovery payloads."""

    def __init__(self, node_id: str, name: str):
        self.node_id = node_id
        self.name = name
        self.device = {
            "identifiers": [f"openwood_{node_id}"],
            "name": name,
            "manufacturer": "Charnwood",
            "model": "Aire 300 (E series)",
        }

    def topic(self, kind: str, obj: str) -> str:
        return f"{self.node_id}/{kind}/{obj}"

    def climate(self) -> tuple[str, str, dict]:
        cfg = {
            "device": self.device,
            "name": self.name,
            "unique_id": f"{self.node_id}_climate",
            "modes": ["off", "heat", "auto", "test"],
            "mode_state_topic": self.topic("state", "hvac_mode"),
            "mode_command_topic": self.topic("command", "hvac_mode"),
            "current_temperature_topic": self.topic("state", "room_temp"),
            "temperature_state_topic": self.topic("state", "room_setpoint"),
            "temperature_command_topic": self.topic("command", "room_setpoint"),
            "temperature_command_template": "{{ value | float }}",
            "min_temp": 16,
            "max_temp": 30,
            "temp_step": 0.5,
            "precision": 0.5,
        }
        return "climate", f"{self.node_id}_climate", cfg

    def sensors(self) -> list[tuple[str, str, dict]]:
        entries: list[tuple[str, str, dict]] = []
        specs = [
            ("stove_temp", "Stove temperature", "temperature", "°C"),
            ("room_temp", "Room temperature", "temperature", "°C"),
            ("board_temp", "Board temperature", "temperature", "°C"),
            ("intensity", "Intensity", None, "%"),
            ("burn_cycle", "Burn cycle state", None, None),
            ("burn_cycle_name", "Burn cycle state name", None, None),
            ("error_code", "Error code", None, None),
            ("valve_1", "Air valve 1 position", None, "%"),
            ("valve_2", "Air valve 2 position", None, "%"),
            ("valve_3", "Air valve 3 position", None, "%"),
        ]
        for key, label, cls, unit in specs:
            cfg = {
                "device": self.device,
                "name": f"{self.name} {label}",
                "unique_id": f"{self.node_id}_{key}",
                "state_topic": self.topic("state", key),
            }
            if cls:
                cfg["device_class"] = cls
            if unit:
                cfg["unit_of_measurement"] = unit
            entries.append(("sensor", f"{self.node_id}_{key}", cfg))

        binary_specs = [
            ("door_open", "Door open", "door"),
            ("burning", "Burning", None),
            ("check_fuel", "Check fuel", None),
            ("overfire", "Overfire warning", "problem"),
            ("extended_burn", "Extended burn", None),
            ("has_error", "Error", "problem"),
        ]
        for key, label, cls in binary_specs:
            cfg = {
                "device": self.device,
                "name": f"{self.name} {label}",
                "unique_id": f"{self.node_id}_{key}",
                "state_topic": self.topic("state", key),
                "payload_on": "ON",
                "payload_off": "OFF",
            }
            if cls:
                cfg["device_class"] = cls
            entries.append(("binary_sensor", f"{self.node_id}_{key}", cfg))

        switches = [
            ("extended_burn", "Extended burn"),
            ("alerts", "Reload alerts"),
        ]
        for key, label in switches:
            cfg = {
                "device": self.device,
                "name": f"{self.name} {label}",
                "unique_id": f"{self.node_id}_{key}_switch",
                "state_topic": self.topic("state", key),
                "command_topic": self.topic("command", key),
                "payload_on": "ON",
                "payload_off": "OFF",
                "state_on": "ON",
                "state_off": "OFF",
            }
            entries.append(("switch", f"{self.node_id}_{key}_switch", cfg))

        lvl = {
            "device": self.device,
            "name": f"{self.name} manual level",
            "unique_id": f"{self.node_id}_manual_level",
            "state_topic": self.topic("state", "manual_level"),
            "command_topic": self.topic("command", "manual_level"),
            "min": 1,
            "max": 5,
            "step": 1,
            "mode": "box",
        }
        entries.append(("number", f"{self.node_id}_manual_level", lvl))
        return entries

    def ext_temp(self) -> tuple[str, str, dict]:
        cfg = {
            "device": self.device,
            "name": f"{self.name} External temperature",
            "unique_id": f"{self.node_id}_ext_temp",
            "state_topic": self.topic("state", "ext_temp"),
            "device_class": "temperature",
            "unit_of_measurement": "°C",
        }
        return "sensor", f"{self.node_id}_ext_temp", cfg


def parse_float(payload: str) -> float | None:
    """Parse a temperature from an MQTT payload.

    Accepts plain numbers ("21.5") or JSON with a "temperature" key
    (Zigbee2MQTT / ESPHome conventions), e.g. {"temperature": 21.5}.
    """
    try:
        return float(payload)
    except (TypeError, ValueError):
        pass
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            value = data.get("temperature")
            if value is not None:
                return float(value)
    except (TypeError, ValueError):
        pass
    return None


async def run_mqtt_bridge(args) -> int:
    import paho.mqtt.client as mqtt

    address = args.address
    source = getattr(args, "status_source", "auto")
    ip = config.get_ip(address) if source in ("auto", "wifi") else None
    if source == "wifi" and not ip:
        log.error("no stored stove IP; run `openwood status --source ble` once")
        return 2

    poll = float(getattr(args, "poll_interval", None) or 15.0)
    wifi_mode = ip is not None
    stove = Stove(address)
    ble_stream = False

    if wifi_mode:
        log.info("status source: WiFi http://%s/get-reading-set "
                 "(control commands open a short BLE link)", ip)
    else:
        await stove.connect()
        try:
            live = await stove.subscribe_notifications()
        except Exception as e:
            log.warning("notifications failed (%s); using polling only", e)
            live = False
        log.info("notification stream: %s", "active" if live else "off (polling)")
        # seed the full state once; pushes only carry changed registers
        st = await stove.read_all(include_meta=True)
        if st.ip_address:
            config.remember_ip(address, st.ip_address)
        ble_stream = True
    node = args.address.replace(":", "").lower()
    builder = DiscoveryBuilder(node, args.name)
    prefix = args.topic_prefix.rstrip("/")

    # --- external thermostat configuration -----------------------------
    # All ext_* flags are empty-tolerant ("" -> None, for env-file driven
    # services); Nones fall back to the documented defaults.
    ext_topic = getattr(args, "ext_temp_topic", None) or None
    ext_target = getattr(args, "ext_target", None)
    ext_low = getattr(args, "ext_setpoint_low", None)
    if ext_low is None:
        ext_low = 16.0
    ext_comfort = getattr(args, "ext_setpoint_comfort", None)
    if ext_comfort is None and wifi_mode:
        # remember whatever setpoint the stove is normally run at
        try:
            ext_comfort = (await Stove.read_wifi(ip)).room_setpoint or 23.0
        except Exception:
            ext_comfort = 23.0
    elif ext_comfort is None:
        ext_comfort = stove.state.room_setpoint or 23.0
    ext_hyst = getattr(args, "ext_hysteresis", None)
    if ext_hyst is None:
        ext_hyst = 0.5
    ext_min_interval = getattr(args, "ext_min_interval", None)
    if ext_min_interval is None:
        ext_min_interval = 120.0
    ext = {"temp": None, "applied": None, "last_write": 0.0}
    if ext_topic:
        log.info(
            "external thermostat: topic=%s target=%.1fC "
            "(hot -> setpoint %.0f, ok -> setpoint %.1f, hysteresis +-%.1f)",
            ext_topic, ext_target, ext_low, ext_comfort, ext_hyst,
        )

    loop = asyncio.get_running_loop()

    cmd_lock = asyncio.Lock()

    async def with_stove(fn):
        """Run a control coroutine: on the persistent link, or a short
        on-demand BLE connection when serving status over WiFi."""
        if ble_stream:
            await fn(stove)
        else:
            async with cmd_lock:
                async with Stove(address) as cmd_stove:
                    await fn(cmd_stove)

    async def handle_command(payload_topic: str, payload: str):
        log.info("command: %s <- %s", payload_topic, payload)
        if payload_topic.endswith("hvac_mode"):
            if payload == "off":
                # Charnwood has no "off"; use intensity 1 (smallest output)
                await with_stove(lambda s: s.set_intensity(1))
            elif payload == "test":
                await with_stove(lambda s: s.set_test_air(50))
            elif payload == "auto":
                await with_stove(lambda s: s.set_mode(P.MODE_ROOM_TEMP))
            else:  # heat -> Automatic mode at current intensity
                await with_stove(lambda s: s.set_mode(P.MODE_AUTOMATIC))
        elif payload_topic.endswith("room_setpoint"):
            setp = float(payload)
            await with_stove(lambda s: s.set_room_setpoint(setp))
        elif payload_topic.endswith("manual_level"):
            level = int(payload)
            await with_stove(lambda s: s.set_manual_level(level))
        elif payload_topic.endswith("extended_burn"):
            on = payload == "ON"
            await with_stove(lambda s: s.set_extended_burn(on))
        elif payload_topic.endswith("alerts"):
            on = payload == "ON"
            await with_stove(lambda s: s.set_alerts(on))

    def on_message(_client, _userdata, msg):
        payload = msg.payload.decode().strip()
        if ext_topic and msg.topic == ext_topic:
            value = parse_float(payload)
            if value is not None:
                ext["temp"] = value
            return
        try:
            asyncio.run_coroutine_threadsafe(
                handle_command(msg.topic, payload), loop
            )
        except Exception as e:
            log.error("dispatching command failed: %s", e)

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"openwood-{node}")
    except AttributeError:  # paho-mqtt 1.x
        client = mqtt.Client(client_id=f"openwood-{node}")
    if args.user:
        client.username_pw_set(args.user, args.password)

    mqtt_rc = {"code": None}

    def on_connect(_c, _u, _flags, rc, _props=None):
        code = rc.value if hasattr(rc, "value") else rc
        mqtt_rc["code"] = int(code)
        if code == 0:
            log.info("MQTT connected to %s:%s", args.host, args.port)
        else:
            log.critical(
                "MQTT broker refused the connection (code %s): check "
                "--host/--user/--password (broker users are created with "
                "mosquitto_passwd)", code,
            )

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    # publish discovery
    entries = [builder.climate()] + builder.sensors()
    if ext_topic:
        entries.append(builder.ext_temp())
    for kind, obj_id, cfg in entries:
        client.publish(f"{prefix}/{kind}/{obj_id}/config", json.dumps(cfg), retain=True)
        log.debug("discovery: %s/%s", kind, obj_id)

    for topic in [
        builder.topic("command", "hvac_mode"),
        builder.topic("command", "room_setpoint"),
        builder.topic("command", "manual_level"),
        builder.topic("command", "extended_burn"),
        builder.topic("command", "alerts"),
    ]:
        client.subscribe(topic)
    if ext_topic:
        client.subscribe(ext_topic)

    def publish_state(st: P.StoveState):
        def b(v) -> str:
            return "ON" if v else "OFF"

        topics = {
            "stove_temp": st.stove_temp,
            "room_temp": round(st.room_temp, 1) if st.room_temp is not None else None,
            "board_temp": round(st.board_temp, 1) if st.board_temp is not None else None,
            "intensity": st.intensity,
            "burn_cycle": st.burn_cycle,
            "burn_cycle_name": st.burn_cycle_name,
            "error_code": st.error_code,
            "door_open": b(st.door_open),
            "burning": b(st.burning),
            "check_fuel": b(st.check_fuel),
            "overfire": b(st.overfire),
            "extended_burn": b(st.extended_burn),
            "has_error": b(st.has_error),
            "room_setpoint": st.room_setpoint,
            "manual_level": st.manual_level,
        }
        gauges = st.valve_gauges
        for i in ("1", "2", "3"):
            g = gauges.get(i)
            if g is not None:
                topics[f"valve_{i}"] = round(g, 1)
        if ext_topic:
            topics["ext_temp"] = round(ext["temp"], 1) if ext["temp"] is not None else None
        for key, value in topics.items():
            if value is None:
                continue
            client.publish(builder.topic("state", key), str(value), retain=True)
        hvac = {"automatic": "heat", "room_temp": "auto", "test": "test"}.get(st.mode_name, "heat")
        client.publish(builder.topic("state", "hvac_mode"), hvac, retain=True)

    async def ext_thermostat_step(st):
        """Hysteresis control: adjust the stove setpoint from ext temperature."""
        if not ext_topic or ext["temp"] is None:
            return
        if st.mode != P.MODE_ROOM_TEMP:
            return  # don't fight Automatic/Test/other control
        now = time.monotonic()
        if now - ext["last_write"] < ext_min_interval:
            return
        t = ext["temp"]
        desired = None
        if t >= ext_target + ext_hyst:
            desired = ext_low
        elif t <= ext_target - ext_hyst:
            desired = ext_comfort
        if desired is not None and desired != ext["applied"]:
            await with_stove(lambda s: s.set_room_setpoint(desired))
            ext["applied"] = desired
            ext["last_write"] = now
            log.info(
                "external thermostat: %.1f C (target %.1f) -> setpoint %.1f",
                t, ext_target, desired,
            )

    log.info(
        "bridge running: stove=%s mqtt=%s:%s (source=%s, poll %ss%s)",
        address, args.host, args.port,
        f"wifi {ip}" if wifi_mode else "ble",
        poll,
        ", ext thermostat" if ext_topic else "",
    )
    last_publish = 0.0

    def state_snapshot(st):
        return tuple(sorted((k, str(v)) for k, v in st.to_dict().items()))

    last_snapshot = None
    wifi_failures = 0
    last_wifi_retry = 0.0
    try:
        while True:
            if mqtt_rc["code"] is not None and mqtt_rc["code"] != 0:
                return 2
            try:
                if wifi_mode:
                    st = await Stove.read_wifi(ip)
                    wifi_failures = 0
                else:
                    # If we fell back to BLE due to a WiFi outage, probe
                    # WiFi every 10 min and switch back when it revives
                    # (the stove's WiFi stack is unreliable in SLEEP).
                    if ip and time.monotonic() - last_wifi_retry > 600:
                        last_wifi_retry = time.monotonic()
                        try:
                            await Stove.read_wifi(ip)
                            log.info("WiFi is back; leaving the BLE stream")
                            try:
                                await stove.disconnect()
                            except Exception:
                                pass
                            wifi_mode = True
                            ble_stream = False
                            wifi_failures = 0
                        except Exception:
                            pass
                    age = stove.push_age()
                    needs_poll = (
                        not stove.notifications_active
                        or age is None
                        or age > max(poll, 30.0)
                    )
                    if needs_poll:
                        await stove.read_all()
                    st = stove.state
                await ext_thermostat_step(st)
                snap = state_snapshot(st)
                now = time.monotonic()
                if snap != last_snapshot and now - last_publish >= 1.0:
                    publish_state(st)
                    last_snapshot = snap
                    last_publish = now
            except Exception as e:
                if wifi_mode:
                    wifi_failures += 1
                    log.warning("WiFi status failed (%s); %d/5", e, wifi_failures)
                    if wifi_failures >= 5:
                        log.warning("WiFi unreliable; switching to the BLE stream")
                        wifi_mode = False
                        try:
                            await stove.connect()
                            await stove.subscribe_notifications()
                            st = await stove.read_all(include_meta=True)
                            if st.ip_address:
                                config.remember_ip(address, st.ip_address)
                            elif config.get_ip(address):
                                config.forget_ip(address)
                                log.info("stove reports no WiFi IP; "
                                         "forgot the stale one")
                            ble_stream = True
                        except Exception as e2:
                            log.error("BLE fallback connect failed: %s", e2)
                            await asyncio.sleep(10)
                        continue
                    await asyncio.sleep(poll)
                    continue
                log.warning("update failed (%s); reconnecting", e)
                try:
                    await stove.disconnect()
                except Exception:
                    pass
                await asyncio.sleep(3)
                try:
                    await stove.connect()
                    await stove.subscribe_notifications()
                except Exception as e2:
                    log.error("reconnect failed: %s", e2)
            await asyncio.sleep(poll if wifi_mode else
                                (1 if stove.notifications_active else poll))
    finally:
        client.loop_stop()
        client.disconnect()
        await stove.disconnect()
    return 0
