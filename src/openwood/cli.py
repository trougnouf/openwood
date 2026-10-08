"""Command line interface for the openwood stove controller."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from . import config
from . import protocol as P
from .client import Stove


def float_or_none(v: str) -> float | None:
    """argparse type: empty string -> None (for env-file-driven flags)."""
    if v is None or not v.strip():
        return None
    return float(v)


def _fmt_state(st: P.StoveState, verbose: bool = False) -> str:
    lines = []
    if st.mode == P.MODE_AUTOMATIC:
        lines.append(f"mode:            automatic (intensity {st.manual_level})")
    elif st.mode == P.MODE_ROOM_TEMP:
        lines.append(f"mode:            room_temp (setpoint {st.room_setpoint:.1f} C)")
    elif st.mode == P.MODE_TEST:
        lines.append(f"mode:            test (air {st.manual_level}%)")
    else:
        lines.append(f"mode:            unknown({st.mode})")
    if st.stove_temp is not None:
        lines.append(f"stove temp:      {st.stove_temp} C")
    if st.room_temp is not None:
        lines.append(f"room temp:       {st.room_temp:.1f} C")
    if st.board_temp is not None:
        lines.append(f"board temp:      {st.board_temp:.1f} C")
    if st.intensity is not None:
        lines.append(f"intensity:       {st.intensity} %")
    if st.valves:
        gauges = st.valve_gauges
        parts = []
        for k in ("1", "2", "3"):
            if k in gauges:
                g = gauges[k]
                raw = st.valves.get(k)
                if g is None:
                    parts.append(f"v{k}=?")
                elif g < 0:
                    parts.append(f"v{k}=FAULT({raw})")
                else:
                    parts.append(f"v{k}={g:.0f}%")
        lines.append("air valves:      " + "  ".join(parts) + "  (gauge %, raw " +
                     "/".join(str(st.valves.get(k)) for k in ("1", "2", "3")) + ")")
    flags = []
    if st.door_open:
        flags.append("DOOR OPEN")
    if st.overfire:
        flags.append("OVERFIRE WARNING")
    if st.check_fuel:
        flags.append("CHECK FUEL")
    if st.burning:
        flags.append("BURNING")
    if st.extended_burn:
        flags.append("EXTENDED BURN")
    if st.alerts:
        flags.append("ALERTS ON (reload light)")
    if st.special_mode:
        flags.append(f"SPECIAL: {st.special_mode_name}")
    if flags:
        lines.append("flags:           " + ", ".join(flags))
    if st.burn_cycle is not None:
        lines.append(
            f"burn cycle:      {st.burn_cycle} ({st.burn_cycle_name}, "
            f"progress {st.burn_cycle_progress or 0}/4)"
        )
    lines.append(f"error:           {st.error_text}")
    if verbose:
        if st.firmware_version is not None:
            lines.append(f"firmware:        {st.firmware_version} ({st.firmware_release})")
        if st.datetime:
            lines.append(f"stove datetime:  {st.datetime}")
        if st.burn_minutes is not None:
            lines.append(f"burn minutes:    {st.burn_minutes}")
        if st.wifi_ssid is not None:
            lines.append(f"wifi ssid:       {st.wifi_ssid or '(none)'}")
        if st.ip_address is not None:
            lines.append(f"ip address:      {st.ip_address or '(none)'}")
        if st.wifi_status is not None:
            lines.append(f"wifi status:     {st.wifi_status or '(disconnected)'}")
        if st.update_url is not None:
            lines.append(f"update server:   {st.update_url}")

        if st.unknown_s is not None:
            lines.append(f"unknown_s:       {st.unknown_s}")
    return "\n".join(lines)


async def _stove(args) -> Stove:
    """Resolve which stove to use and return a connected Stove."""
    address = await config.resolve_address(getattr(args, "address", None))
    stove = Stove(address)
    await stove.connect()
    return stove


# -- commands ----------------------------------------------------------------


async def cmd_scan(args) -> int:
    devices = await Stove.scan(timeout=args.timeout)
    config.remember_seen(devices)
    if not devices:
        print("No Charnwood stove found.")
        return 1
    default, _ = config.get_default()
    for d in devices:
        mark = "   (default)" if d.address.lower() == (default or "").lower() else ""
        print(f"{d.address}  {d.name}{mark}")
    print("\nTo set as default:  openwood use <MAC>")
    return 0


async def cmd_use(args) -> int:
    address = args.address
    if not address:
        devices = await Stove.scan(timeout=args.timeout)
        if len(devices) == 1:
            address = devices[0].address
            print(f"# found one stove: {devices[0].name} at {address}")
        else:
            config.remember_seen(devices)
            if not devices:
                print("No Charnwood stove found.", file=sys.stderr)
                return 1
            print("Multiple stoves found; run `openwood use <MAC>`:", file=sys.stderr)
            for d in devices:
                print(f"    {d.address}  {d.name}", file=sys.stderr)
            return 2
    cfg = config.load()
    name = args.name or (cfg.get("seen") or {}).get(address)
    config.set_default(address, name)
    saved_name = (config.load().get("names") or {}).get(address)
    print(f"default stove set to {address}" + (f" ({saved_name})" if saved_name else ""))
    print(f"config: {config.config_path()}")
    return 0


async def cmd_stoves(args) -> int:
    cfg = config.load()
    default = cfg.get("default_address")
    names = cfg.get("names") or {}
    seen = cfg.get("seen") or {}
    rows = []
    if default:
        rows.append((default, names.get(default) or seen.get(default) or "Charnwood", True))
    for addr, name in seen.items():
        if addr != default:
            rows.append((addr, name, False))
    if not rows:
        print("No stoves configured yet. Run `openwood scan` then `openwood use <MAC>`.")
        return 1
    for addr, name, is_default in rows:
        print(f"{addr}  {name}" + ("   (default)" if is_default else ""))
    return 0


async def cmd_forget(args) -> int:
    cfg = config.load()
    changed = False
    if cfg.get("default_address") == args.address:
        cfg["default_address"] = None
        changed = True
    if args.address in (cfg.get("names") or {}):
        del cfg["names"][args.address]
        changed = True
    if args.address in (cfg.get("seen") or {}):
        del cfg["seen"][args.address]
        changed = True
    if changed:
        config.save(cfg)
        print(f"removed {args.address} from config")
    else:
        print(f"{args.address} was not in config")
    return 0


async def cmd_status(args) -> int:
    async with await _stove(args) as stove:
        st = await stove.read_all(include_meta=True)
        if args.json:
            print(json.dumps(st.to_dict(), indent=2))
        else:
            print(_fmt_state(st, verbose=True))
    return 0


async def cmd_watch(args) -> int:
    log_path = Path(args.log) if args.log else None
    if log_path and not log_path.exists():
        log_path.write_text(
            "timestamp,stove_temp,room_temp,board_temp,room_setpoint,intensity,"
            "mode,manual_level,burn_cycle,door_open,burning,extended_burn,check_fuel,alerts,valve1,valve2,valve3,error\n"
        )
    async with await _stove(args) as stove:
        if args.notify:
            try:
                await stove.subscribe_notifications()
            except Exception as e:
                print(f"notifications unavailable ({e}); falling back to polling",
                      file=sys.stderr)
        # seed the full state once; pushes only carry changed registers
        await stove.read_all()
        n = 0
        while args.count == 0 or n < args.count:
            try:
                age = stove.push_age()
                if not (args.notify and stove.notifications_active and
                        (age is None or age < 30)):
                    # polling mode, or the push stream has gone quiet
                    await stove.read_all()
                st = stove.state
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] read failed: {e}", file=sys.stderr)
                try:
                    await stove.connect()
                except Exception as e2:
                    print(f"reconnect failed: {e2}", file=sys.stderr)
                    await asyncio.sleep(5)
                continue
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}]\n{_fmt_state(st)}\n")
            if log_path:
                with log_path.open("a") as f:
                    f.write(
                        f"{stamp},{st.stove_temp},{st.room_temp},{st.board_temp},"
                        f"{st.room_setpoint},{st.intensity},{st.mode},{st.manual_level},"
                        f"{st.burn_cycle},{int(st.door_open or False)},{int(st.burning or False)},"
                        f"{int(st.extended_burn or False)},{int(st.check_fuel or False)},{int(st.alerts or False)},"
                        f"{st.valves.get('1')},{st.valves.get('2')},{st.valves.get('3')},{st.error_code}\n"
                    )
            n += 1
            await asyncio.sleep(args.interval)
    return 0


MODE_ALIASES = {
    "automatic": P.MODE_AUTOMATIC, "manual": P.MODE_AUTOMATIC,
    "room-temp": P.MODE_ROOM_TEMP, "auto": P.MODE_ROOM_TEMP,
    "test": P.MODE_TEST, "boost": P.MODE_TEST,
}


async def cmd_set_mode(args) -> int:
    async with await _stove(args) as stove:
        await stove.read_all()
        mode = MODE_ALIASES[args.mode]
        await stove.set_mode(mode, args.level, args.setpoint)
        print(f"mode set to {P.MODE_NAMES[mode]}")
    return 0


async def cmd_set_level(args) -> int:
    async with await _stove(args) as stove:
        await stove.read_all()
        await stove.set_intensity(args.level)
        print(f"burn intensity set to {args.level} (Automatic mode)")
    return 0


async def cmd_set_air(args) -> int:
    async with await _stove(args) as stove:
        await stove.set_test_air(args.percent)
        print(f"Test mode air set to {args.percent}%")
    return 0


async def cmd_set_temp(args) -> int:
    async with await _stove(args) as stove:
        await stove.read_all()
        await stove.set_room_setpoint(args.temp)
        print(f"room setpoint set to {args.temp:.1f} C (auto mode)")
    return 0


async def cmd_toggle(args) -> int:
    async with await _stove(args) as stove:
        on = args.on == "on"
        if args.what in ("extended-burn", "overnight"):
            await stove.set_extended_burn(on)
            print(f"extended burn {'on' if on else 'off'}")
        else:
            await stove.set_alerts(on)
            print(f"alerts (reload light) {'on' if on else 'off'}")
    return 0


async def cmd_special(args) -> int:
    value = {
        "powercut": P.SPECIAL_POWERCUT,
        "chimney-fire": P.SPECIAL_CHIMNEY_FIRE,
        "cancel": P.SPECIAL_CANCEL,
    }[args.mode]
    async with await _stove(args) as stove:
        await stove.special_mode(value)
        print(f"special mode command sent: {args.mode}")
    return 0


async def cmd_sync_time(args) -> int:
    async with await _stove(args) as stove:
        if not args.force:
            current = await stove.get_stove_time()
            print(f"stove time was: {current or '(empty)'}")
        now = await stove.sync_time()
        print(f"stove time set to {now}")
    return 0


async def cmd_wifi(args) -> int:
    async with await _stove(args) as stove:
        if args.action == "set":
            if not args.ssid or args.password is None:
                print("error: --ssid and --password required", file=sys.stderr)
                return 2
            await stove.set_wifi(args.ssid, args.password)
            print("wifi credentials sent; stove is connecting (check `status`)")
        elif args.action == "delete":
            await stove.delete_wifi()
            print("wifi credentials deleted")
        else:
            ssid = (await stove.read_meta(P.M_WIFI_SSID)).decode("ascii").strip("\x00")
            ip = (await stove.read_meta(P.M_IP_ADDRESS)).decode("ascii").strip("\x00")
            status = (await stove.read_meta(P.M_WIFI_STATUS)).decode("ascii").strip("\x00")
            print(f"ssid:    {ssid or '(none)'}")
            print(f"ip:      {ip or '(none)'}")
            print(f"status:  {status or '(disconnected)'}")
    return 0


async def cmd_firmware(args) -> int:
    import urllib.request

    async with await _stove(args) as stove:
        st = await stove.read_all(include_meta=True)
        print(f"installed version: {st.firmware_version}")
        print(f"installed release: {st.firmware_release}")
        print(f"update server:     {st.update_url or '(none reported)'}")
        if st.update_url:
            url = st.update_url.rstrip("/") + "/info"
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    info = r.read().decode("utf-8", "replace")
                print(f"available (from {url}):\n{info}")
            except Exception as e:
                print(f"could not fetch {url}: {e}", file=sys.stderr)
                return 1
    return 0


async def cmd_update(args) -> int:
    async with await _stove(args) as stove:
        if args.special:
            await stove.start_special_update()
            print("special update command sent")
        else:
            await stove.start_firmware_update()
            print("firmware update command sent; stove downloads from its update server")
        for _ in range(args.wait):
            await asyncio.sleep(5)
            status, err = await stove.read_update_status()
            print(f"update_status={status} op_error={err}")
            if err != 0:
                print(f"error: {P.error_text(err)}")
                return 1
            if status == 0:
                break
    return 0


async def cmd_delete_pairing(args) -> int:
    async with await _stove(args) as stove:
        await stove.delete_pairing()
        print("delete-pairing command sent")
    return 0


# -- parser ------------------------------------------------------------------


def _add_address(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "address",
        nargs="?",
        default=None,
        help="stove MAC address (optional; uses default stove or auto-discovers)",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="openwood", description="Charnwood E stove control")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="scan for stoves")
    p.add_argument("--timeout", type=float, default=12.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("use", help="set the default stove (MAC optional if only one in range)")
    p.add_argument("address", nargs="?", default=None)
    p.add_argument("--name", help="friendly name for the stove")
    p.add_argument("--timeout", type=float, default=12.0)
    p.set_defaults(func=cmd_use)

    p = sub.add_parser("stoves", help="list configured/seen stoves")
    p.set_defaults(func=cmd_stoves)

    p = sub.add_parser("forget", help="remove a stove from the config")
    p.add_argument("address")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("status", help="read full stove status")
    _add_address(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="poll status continuously")
    _add_address(p)
    p.add_argument("--notify", action="store_true",
                   help="use BLE push notifications instead of polling")
    p.add_argument("--interval", type=float, default=10.0)
    p.add_argument("--count", type=int, default=0, help="0 = forever")
    p.add_argument("--log", help="CSV file to append readings to")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "set-mode", help="set mode: automatic / room-temp / test"
    )
    _add_address(p)
    p.add_argument(
        "mode", choices=list(MODE_ALIASES),
        help="automatic=intensity mode, room-temp=thermostat, test=air percent"
        " (manual/auto/boost are deprecated aliases)",
    )
    p.add_argument("--level", type=int, help="intensity 1-5, or air percent 0-100 in test mode")
    p.add_argument("--setpoint", type=float, help="room setpoint in C (room-temp mode)")
    p.set_defaults(func=cmd_set_mode)

    p = sub.add_parser(
        "set-level", aliases=["set-intensity"], help="set burn intensity 1-5 (Automatic mode)"
    )
    _add_address(p)
    p.add_argument("level", type=int)
    p.set_defaults(func=cmd_set_level)

    p = sub.add_parser("set-air", help="set Test mode air percent 0-100 (service mode)")
    _add_address(p)
    p.add_argument("percent", type=int)
    p.set_defaults(func=cmd_set_air)

    p = sub.add_parser("set-temp", help="set room setpoint (C), switches to auto mode")
    _add_address(p)
    p.add_argument("temp", type=float)
    p.set_defaults(func=cmd_set_temp)

    p = sub.add_parser(
        "toggle", help="toggle extended burn / reload-light alerts"
    )
    _add_address(p)
    p.add_argument("what", choices=["extended-burn", "overnight", "alerts", "fuel-alert"])
    p.add_argument("on", choices=["on", "off"])
    p.set_defaults(func=cmd_toggle)

    p = sub.add_parser("special", help="powercut / chimney-fire / cancel")
    _add_address(p)
    p.add_argument("mode", choices=["powercut", "chimney-fire", "cancel"])
    p.set_defaults(func=cmd_special)

    p = sub.add_parser("sync-time", help="set stove clock from this computer")
    _add_address(p)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sync_time)

    p = sub.add_parser("wifi", help="show / set / delete wifi credentials")
    _add_address(p)
    p.add_argument("action", choices=["show", "set", "delete"])
    p.add_argument("--ssid")
    p.add_argument("--password")
    p.set_defaults(func=cmd_wifi)

    p = sub.add_parser("firmware", help="show firmware info and check for updates")
    _add_address(p)
    p.set_defaults(func=cmd_firmware)

    p = sub.add_parser("update", help="start firmware update on the stove")
    _add_address(p)
    p.add_argument("--special", action="store_true", help="special update (support mode)")
    p.add_argument("--wait", type=int, default=0, help="poll status for N*5 seconds")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("delete-pairing", help="delete pairing info on the stove")
    _add_address(p)
    p.set_defaults(func=cmd_delete_pairing)

    p = sub.add_parser("mqtt", help="run MQTT bridge for Home Assistant")
    _add_address(p)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--topic-prefix", default="homeassistant")
    p.add_argument("--poll-interval", type=float, default=15.0)
    p.add_argument("--name", default=None, help="entity name (defaults to configured name)")
    p.add_argument(
        "--ext-temp-topic",
        help="external thermostat: MQTT topic carrying a remote temperature "
        "(e.g. a bedroom sensor's state topic)",
    )
    p.add_argument(
        "--ext-target", type=float_or_none,
        help="target temperature at the external sensor (required with "
        "--ext-temp-topic)",
    )
    p.add_argument(
        "--ext-setpoint-low", type=float_or_none, default=16.0,
        help="stove setpoint when the remote room is too hot (default 16 = "
        "stove minimum output)",
    )
    p.add_argument(
        "--ext-setpoint-comfort", type=float_or_none,
        help="stove setpoint when the remote room is at/below target "
        "(default: the setpoint the stove is running at startup)",
    )
    p.add_argument(
        "--ext-hysteresis", type=float_or_none, default=0.5,
        help="hysteresis band around the target in C (default 0.5)",
    )
    p.add_argument(
        "--ext-min-interval", type=float_or_none, default=120.0,
        help="minimum seconds between stove setpoint writes (default 120)",
    )
    p.set_defaults(func=None)  # wired in main()

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s: %(message)s",
    )
    if args.command == "mqtt":
        from .mqtt_bridge import run_mqtt_bridge

        async def _mqtt(_args=None):
            args.address = await config.resolve_address(args.address)
            if args.ext_temp_topic and args.ext_target is None:
                print("error: --ext-temp-topic requires --ext-target",
                      file=sys.stderr)
                return 2
            if not args.name:
                cfg = config.load()
                args.name = (cfg.get("names") or {}).get(args.address) or "Charnwood stove"
            return await run_mqtt_bridge(args)

        args.func = _mqtt
    from bleak.exc import BleakError

    try:
        return asyncio.run(args.func(args))
    except ConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BleakError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
