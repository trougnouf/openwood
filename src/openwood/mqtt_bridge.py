"""MQTT bridge exposing the stove to Home Assistant (with discovery).

Optionally acts as an external thermostat: it subscribes to any MQTT
temperature topic (e.g. a bedroom sensor) and adjusts the stove's room
setpoint, since the stove itself only knows the sensor in its power cable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

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
    try:
        return float(payload)
    except (TypeError, ValueError):
        return None


async def run_mqtt_bridge(args) -> int:
    import paho.mqtt.client as mqtt

    stove = Stove(args.address)
    await stove.connect()
    try:
        live = await stove.subscribe_notifications()
    except Exception as e:
        log.warning("notifications failed (%s); using polling only", e)
        live = False
    log.info("notification stream: %s", "active" if live else "off (polling)")
    # seed the full state once; pushes only carry changed registers
    await stove.read_all()
    poll = float(args.poll_interval)
    node = args.address.replace(":", "").lower()
    builder = DiscoveryBuilder(node, args.name)
    prefix = args.topic_prefix.rstrip("/")

    # --- external thermostat configuration -----------------------------
    ext_topic = getattr(args, "ext_temp_topic", None)
    ext_target = float(getattr(args, "ext_target", 0) or 0)
    ext_low = float(getattr(args, "ext_setpoint_low", 16))
    ext_comfort = getattr(args, "ext_setpoint_comfort", None)
    if ext_comfort is None:
        # remember whatever setpoint the stove is normally run at
        ext_comfort = stove.state.room_setpoint or 23.0
    ext_comfort = float(ext_comfort)
    ext_hyst = float(getattr(args, "ext_hysteresis", 0.5))
    ext_min_interval = float(getattr(args, "ext_min_interval", 120))
    ext = {"temp": None, "applied": None, "last_write": 0.0}
    if ext_topic:
        log.info(
            "external thermostat: topic=%s target=%.1fC "
            "(hot -> setpoint %.0f, ok -> setpoint %.1f, hysteresis +-%.1f)",
            ext_topic, ext_target, ext_low, ext_comfort, ext_hyst,
        )

    loop = asyncio.get_running_loop()

    async def handle_command(payload_topic: str, payload: str):
        log.info("command: %s <- %s", payload_topic, payload)
        if payload_topic.endswith("hvac_mode"):
            if payload == "off":
                # Charnwood has no "off"; use intensity 1 (smallest output)
                await stove.set_intensity(1)
            elif payload == "test":
                await stove.set_test_air(50)
            elif payload == "auto":
                await stove.set_mode(P.MODE_ROOM_TEMP)
            else:  # heat -> Automatic mode at current intensity
                await stove.set_mode(P.MODE_AUTOMATIC)
        elif payload_topic.endswith("room_setpoint"):
            await stove.set_room_setpoint(float(payload))
        elif payload_topic.endswith("manual_level"):
            await stove.set_manual_level(int(payload))
        elif payload_topic.endswith("extended_burn"):
            await stove.set_extended_burn(payload == "ON")
        elif payload_topic.endswith("alerts"):
            await stove.set_alerts(payload == "ON")

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

    async def ext_thermostat_step():
        """Hysteresis control: adjust the stove setpoint from ext temperature."""
        if not ext_topic or ext["temp"] is None:
            return
        if stove.state.mode != P.MODE_ROOM_TEMP:
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
            await stove.set_room_setpoint(desired)
            ext["applied"] = desired
            ext["last_write"] = now
            log.info(
                "external thermostat: %.1f C (target %.1f) -> setpoint %.1f",
                t, ext_target, desired,
            )

    log.info(
        "bridge running: stove=%s mqtt=%s:%s (push%s, fallback poll %ss%s)",
        args.address, args.host, args.port,
        " on" if live else " off", poll,
        ", ext thermostat" if ext_topic else "",
    )
    last_publish = 0.0

    def state_snapshot(st):
        return tuple(sorted((k, str(v)) for k, v in st.to_dict().items()))

    last_snapshot = None
    try:
        while True:
            try:
                age = stove.push_age()
                needs_poll = (
                    not stove.notifications_active
                    or age is None
                    or age > max(poll, 30.0)
                )
                if needs_poll:
                    await stove.read_all()
                await ext_thermostat_step()
                st = stove.state
                snap = state_snapshot(st)
                now = time.monotonic()
                if snap != last_snapshot and now - last_publish >= 1.0:
                    publish_state(st)
                    last_snapshot = snap
                    last_publish = now
            except Exception as e:
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
            await asyncio.sleep(1 if stove.notifications_active else poll)
    finally:
        client.loop_stop()
        client.disconnect()
        await stove.disconnect()
    return 0
