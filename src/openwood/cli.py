"""Command line interface for the openwood stove controller."""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
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


def mqtt_password(args) -> str | None:
    """Resolve the broker password without leaking it in `ps` output.

    Priority: --password flag, then the MQTT_PASSWORD environment variable
    (what openwood-mqtt.service provides via its EnvironmentFile), then an
    interactive prompt. Returns None if the broker is anonymous (--user
    not given) or if no password can be found non-interactively.
    """
    if args.password:
        return args.password
    pw = os.environ.get("MQTT_PASSWORD") or None
    if pw:
        return pw
    if not args.user:
        return None
    if sys.stdin.isatty():
        return getpass.getpass(f"MQTT password for {args.user!r}: ") or None
    return None


def prompt_wifi_password(ssid: str) -> str | None:
    """Ask for the stove's WiFi password on the terminal (typed blind)."""
    pw = getpass.getpass(f"WiFi password for network {ssid!r}: ")
    if not pw:
        return None
    if getpass.getpass("Repeat password: ") != pw:
        print("error: passwords do not match", file=sys.stderr)
        return None
    return pw


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
    address = await config.resolve_address(getattr(args, "address", None))
    src = getattr(args, "source", "auto")

    if src in ("auto", "wifi"):
        ip = config.get_ip(address)
        if src == "wifi" and not ip:
            print(
                "error: no stored stove IP; run `openwood status --source ble` "
                "once (or `openwood wifi show`) to capture it",
                file=sys.stderr,
            )
            return 2
        if ip:
            try:
                st = await Stove.read_wifi(ip)
                print(f"# status via WiFi http://{ip}/get-reading-set",
                      file=sys.stderr)
                if args.json:
                    print(json.dumps(st.to_dict(), indent=2))
                else:
                    print(_fmt_state(st, verbose=False))
                return 0
            except Exception as e:
                if src == "wifi":
                    print(f"error: WiFi status failed: {e}", file=sys.stderr)
                    return 2
                print(f"# WiFi status failed ({e}); falling back to BLE",
                      file=sys.stderr)

    stove = Stove(address)
    await stove.connect()
    try:
        st = await stove.read_all(include_meta=True)
        if st.ip_address:
            config.remember_ip(address, st.ip_address)
            print(f"# stove IP remembered: {st.ip_address}", file=sys.stderr)
        elif config.get_ip(address):
            config.forget_ip(address)
            print("# stove reports no WiFi IP; forgot the stale one "
                  "(re-provision with `openwood wifi set`)", file=sys.stderr)
        if args.json:
            print(json.dumps(st.to_dict(), indent=2))
        else:
            print(_fmt_state(st, verbose=True))
    finally:
        await stove.disconnect()
    return 0


async def cmd_watch(args) -> int:
    log_path = Path(args.log) if args.log else None
    if log_path and not log_path.exists():
        log_path.write_text(
            "timestamp,stove_temp,room_temp,board_temp,room_setpoint,intensity,"
            "mode,manual_level,burn_cycle,door_open,burning,extended_burn,check_fuel,alerts,valve1,valve2,valve3,error\n"
        )
    address = await config.resolve_address(getattr(args, "address", None))
    src = getattr(args, "source", "auto")
    ip = config.get_ip(address) if src in ("auto", "wifi") else None
    if src == "wifi" and not ip:
        print("error: no stored stove IP; run `openwood status --source ble` "
              "once to capture it", file=sys.stderr)
        return 2

    wifi_failures = 0
    use_wifi = ip is not None
    stove = Stove(address)
    connected = False
    if not use_wifi:
        await stove.connect()
        connected = True
        if args.notify:
            try:
                await stove.subscribe_notifications()
            except Exception as e:
                print(f"notifications unavailable ({e}); falling back to polling",
                      file=sys.stderr)
        # seed the full state once; pushes only carry changed registers
        await stove.read_all()
    n = 0
    try:
        while args.count == 0 or n < args.count:
            try:
                if use_wifi:
                    st = await Stove.read_wifi(ip)
                    wifi_failures = 0
                else:
                    age = stove.push_age()
                    if not (args.notify and stove.notifications_active and
                            (age is None or age < 30)):
                        # polling mode, or the push stream has gone quiet
                        await stove.read_all()
                    st = stove.state
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] read failed: {e}", file=sys.stderr)
                if use_wifi:
                    wifi_failures += 1
                    if wifi_failures >= 5:
                        print("# WiFi unreliable; switching to BLE", file=sys.stderr)
                        use_wifi = False
                        try:
                            await stove.connect()
                            connected = True
                            await stove.read_all()
                        except Exception as e2:
                            print(f"BLE connect failed: {e2}", file=sys.stderr)
                        continue
                    await asyncio.sleep(min(args.interval, 5))
                    continue
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
    finally:
        if connected:
            await stove.disconnect()
    return 0


MODE_ALIASES = {
    "automatic": P.MODE_AUTOMATIC, "manual": P.MODE_AUTOMATIC,
    "room-temp": P.MODE_ROOM_TEMP, "auto": P.MODE_ROOM_TEMP,
    "test": P.MODE_TEST, "boost": P.MODE_TEST,
}


LIGHTING_STATES = (1, 18)  # LIGHTING / LIGHTING E


def _warn_if_lighting(st: P.StoveState) -> None:
    if st.burn_cycle in LIGHTING_STATES:
        print(
            f"# note: the stove is in {st.burn_cycle_name}; the firmware "
            "reverts mode changes until combustion is established - "
            "re-apply this once the burn cycle shows EARLY BURN or later "
            "(verified on the real stove)",
            file=sys.stderr,
        )


async def cmd_set_mode(args) -> int:
    async with await _stove(args) as stove:
        st = await stove.read_all()
        mode = MODE_ALIASES[args.mode]
        await stove.set_mode(mode, args.level, args.setpoint)
        print(f"mode set to {P.MODE_NAMES[mode]}")
        _warn_if_lighting(st)
    return 0


async def cmd_set_level(args) -> int:
    async with await _stove(args) as stove:
        st = await stove.read_all()
        _warn_if_lighting(st)
        await stove.set_intensity(args.level)
        print(f"burn intensity set to {args.level} (Automatic mode)")
    return 0


async def cmd_set_air(args) -> int:
    async with await _stove(args) as stove:
        st = await stove.read_all()
        _warn_if_lighting(st)
        await stove.set_test_air(args.percent)
        print(f"Test mode air set to {args.percent}%")
    return 0


async def cmd_set_temp(args) -> int:
    async with await _stove(args) as stove:
        st = await stove.read_all()
        _warn_if_lighting(st)
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


async def cmd_rescue(args) -> int:
    """Rescue the stuck-LIGHTING bug: engage powercut mode (register
    0x19 = 1), which takes the state machine to POWER CUT MODE and drives
    the air valves to the nominal air setting - the fire then burns at a
    controlled rate instead of full blast. Verified on the real stove."""
    from .stuck import POWERCUT_STATE, stuck_in_lighting

    async with await _stove(args) as stove:
        st = await stove.read_all()
        stuck = stuck_in_lighting(st, args.temp)
        if not stuck and not args.force:
            print(
                f"not stuck: burn cycle = {st.burn_cycle_name} "
                f"(stove {st.stove_temp if st.stove_temp is not None else '?'} C); "
                "use --force to engage powercut mode anyway",
                file=sys.stderr,
            )
            return 3
        if stuck:
            print(
                f"stuck: {st.burn_cycle_name} with stove at "
                f"{st.stove_temp:.0f} C (threshold {args.temp:.0f} C); "
                "engaging powercut mode"
            )
        else:
            print("engaging powercut mode anyway (--force)")
        mode = st.mode if st.mode is not None else P.MODE_AUTOMATIC
        level, setp, ext = st.manual_level, st.room_setpoint, st.extended_burn
        try:
            await stove.special_mode(P.SPECIAL_POWERCUT)
        except Exception as e:
            print(f"write failed: {e}", file=sys.stderr)
            return 1
        deadline = time.monotonic() + args.wait
        engaged = False
        while time.monotonic() < deadline:
            await asyncio.sleep(10)
            try:
                st = await stove.read_all()
            except Exception as e:
                print(f"read failed: {e}", file=sys.stderr)
                continue
            if st.burn_cycle == POWERCUT_STATE or st.special_mode == P.SPECIAL_POWERCUT:
                engaged = True
                break
        if not engaged:
            print(
                f"no confirmation within {args.wait:.0f} s (last state: "
                f"{st.burn_cycle_name}); the write may still have landed - "
                "check with `openwood status`",
                file=sys.stderr,
            )
            return 1
        gauges = st.valve_gauges
        v = "  ".join(f"v{k}={gauges[k]:.0f}%" for k in ("1", "2", "3") if k in gauges)
        before = (
            f"mode={P.MODE_NAMES.get(mode, mode)}"
            + (f" level={level}" if mode == P.MODE_AUTOMATIC else "")
            + (f" setpoint={setp:.1f}C" if mode == P.MODE_ROOM_TEMP and setp else "")
            + (f" extended_burn={'on' if ext else 'off'}" if ext is not None else "")
        )
        print(
            f"engaged: {st.burn_cycle_name}, valves -> {v}\n"
            "the damper now holds the nominal air setting; the fire burns\n"
            "at a controlled rate instead of full blast.\n"
            "when the fire is out (stove < ~60 C for ~10 min):\n"
            "    openwood special cancel     # restarts the controller\n"
            f"NOTE: the restart resets the mode to Automatic (intensity 3)\n"
            f"and Extended Burn off. Before the rescue: {before} -\n"
            "re-apply them once the burn cycle shows EARLY BURN or later\n"
            "(mode writes are reverted while the stove sits in LIGHTING)."
        )
        return 0


async def cmd_guard(args) -> int:
    """Run the stuck-LIGHTING guard loop (see README, 'Stuck in LIGHTING').

    Polls the stove (WiFi if its IP is known, else the BLE stream), feeds
    each state to StuckGuard and executes the returned actions over a
    short on-demand BLE link (control always uses BLE, like the bridge).
    """
    from .stuck import StuckGuard

    if args.disable:
        config.set_stuck_guard(False)
        print("stuck guard disabled (persisted)")
        return 0
    if args.enable:
        config.set_stuck_guard(True)
    elif not config.get_stuck_guard():
        print(
            "stuck guard is disabled (it deviates from the firmware's "
            "behaviour; the bug may be fixed upstream).\n"
            "Enable it with:\n"
            "    openwood guard --enable\n"
            "or the 'Stuck guard' switch in Home Assistant.",
            file=sys.stderr,
        )
        return 3

    lock = config.GuardLock()
    if not lock.try_acquire():
        print(
            f"another guard is already running (lock held: "
            f"{config.guard_lock_path()}); exiting",
            file=sys.stderr,
        )
        return 3

    guard = StuckGuard(
        peak_temp=args.peak,
        fall_delta=args.fall_delta,
        backstop_minutes=args.backstop_minutes,
        temp=args.temp,
        exit_temp=args.exit_temp,
        exit_minutes=args.exit_minutes,
        refuel_assist=args.refuel_assist,
        door_gate=args.door_gate,
        enabled=True,
    )
    address = await config.resolve_address(getattr(args, "address", None))
    src = getattr(args, "source", "auto")
    ip = config.get_ip(address) if src in ("auto", "wifi") else None
    if src == "wifi" and not ip:
        print("error: no stored stove IP; run `openwood status --source ble` "
              "once to capture it, or use --source ble", file=sys.stderr)
        return 2

    cmd_lock = asyncio.Lock()
    stove = Stove(address)
    connected = False
    use_wifi = ip is not None
    if not use_wifi:
        await stove.connect()
        connected = True
        await stove.read_all()

    print(
        f"stuck guard running: stove={address} "
        f"source={'wifi ' + ip if use_wifi else 'ble'} "
        f"(poll {args.interval:.0f}s)\n"
        f"engage: peak >= {guard.peak_temp:.0f} C falling {guard.fall_delta:.0f} C "
        f"off the peak, or stuck hot >= {guard.temp:.0f} C for "
        f"{int(guard.backstop_wait // 60)} min\n"
        f"exit: < {guard.exit_temp:.0f} C for {int(guard.exit_wait // 60)} min "
        f"(refuel assist {'on' if guard.refuel_assist else 'off'})"
    )

    async def with_stove(fn):
        """Run a control coroutine: on the persistent link, or a short
        on-demand BLE connection when serving status over WiFi."""
        if connected:
            await fn(stove)
        else:
            async with cmd_lock:
                async with Stove(address) as cmd_stove:
                    await fn(cmd_stove)

    wifi_failures = 0
    try:
        while True:
            # a toggle from Home Assistant (or `openwood guard --disable`
            # in another terminal) stops the loop cleanly
            if not config.get_stuck_guard():
                print("stuck guard disabled; stopping")
                return 0
            try:
                if use_wifi:
                    st = await Stove.read_wifi(ip)
                    wifi_failures = 0
                else:
                    await stove.read_all()
                    st = stove.state
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] read failed: {e}",
                      file=sys.stderr)
                if use_wifi:
                    wifi_failures += 1
                    if wifi_failures >= 5:
                        print("# WiFi unreliable; switching to BLE", file=sys.stderr)
                        use_wifi = False
                        try:
                            await stove.connect()
                            connected = True
                            await stove.read_all()
                        except Exception as e2:
                            print(f"BLE connect failed: {e2}", file=sys.stderr)
                        await asyncio.sleep(min(args.interval, 5))
                        continue
                    await asyncio.sleep(min(args.interval, 5))
                    continue
                try:
                    await stove.connect()
                    connected = True
                except Exception as e2:
                    print(f"reconnect failed: {e2}", file=sys.stderr)
                await asyncio.sleep(5)
                continue
            for action in guard.step(st, time.monotonic()):
                stamp = time.strftime("%H:%M:%S")
                if action == "engage":
                    print(
                        f"[{stamp}] LIGHTING stuck at {st.stove_temp or 0:.0f} C; "
                        "engaging powercut mode (damper -> nominal air)"
                    )
                    try:
                        await with_stove(
                            lambda s: s.special_mode(P.SPECIAL_POWERCUT))
                    except Exception as e:
                        print(f"engage write failed: {e}", file=sys.stderr)
                elif action == "exit":
                    print(
                        f"[{stamp}] fire out; exiting powercut mode "
                        "(controller restarts)"
                    )
                    try:
                        await with_stove(
                            lambda s: s.special_mode(P.SPECIAL_CANCEL))
                    except Exception as e:
                        # the reboot drops the link mid-write; the command
                        # usually lands anyway
                        print(f"write dropped ({e}) - the restart usually "
                              "lands anyway", file=sys.stderr)
                elif action == "refuel_flare":
                    print(
                        f"[{stamp}] door held open in powercut mode; flaring "
                        "(controller restarts so the fresh fuel re-ignites)"
                    )
                    try:
                        await with_stove(
                            lambda s: s.special_mode(P.SPECIAL_CANCEL))
                    except Exception as e:
                        print(f"write dropped ({e}) - the restart usually "
                              "lands anyway", file=sys.stderr)
                elif action == "restore":
                    print(f"[{stamp}] restoring previous mode")
                    try:
                        await with_stove(guard.restore_mode)
                    except Exception as e:
                        print(f"restore failed ({e}); will retry", file=sys.stderr)
            await asyncio.sleep(args.interval)
    finally:
        lock.release()
        if connected:
            await stove.disconnect()
    return 0


async def cmd_report(args) -> int:
    from . import report as R

    try:
        samples = R.load_samples(args.logfile)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not samples:
        print("no samples found in the log", file=sys.stderr)
        return 2
    cycles = R.find_cycles(samples, min_burn_temp=args.min_temp)
    if args.csv_dir:
        paths = R.write_cycle_csvs(cycles, args.csv_dir)
        print(f"wrote {len(paths)} files to {args.csv_dir}", file=sys.stderr)
    if args.json:
        print(R.report_to_json(cycles))
    else:
        print(R.format_report(cycles))
    return 0


async def cmd_restart(args) -> int:
    async with await _stove(args) as stove:
        try:
            await stove.special_mode(P.SPECIAL_CANCEL)
        except Exception as e:
            # the reboot drops the link mid-write; the command usually lands
            print(f"link dropped during write ({e}) - the restart usually "
                  "lands anyway; wait ~30 s and check with `openwood status`")
            return 0
        print(
            "restart command sent (register 0x19 = 3, same as the app's "
            "Restart). Allow ~30 s before the next status.\n"
            "NOTE (observed on the real stove): the controller reboots, "
            "WiFi re-connects from saved credentials and air valve "
            "positions hold, BUT the mode resets to the default "
            "(Automatic, intensity 3) and Extended Burn switches off - "
            "re-apply your mode afterwards if needed."
        )
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
            if not args.ssid:
                print("error: --ssid required", file=sys.stderr)
                return 2
            password = args.password
            if password is None:
                password = prompt_wifi_password(args.ssid)
                if password is None:
                    return 2
            await stove.set_wifi(args.ssid, password)
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
    p.add_argument("--source", choices=["auto", "wifi", "ble"], default="auto",
                   help="status transport: auto = WiFi if known, else BLE")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("watch", help="poll status continuously")
    _add_address(p)
    p.add_argument("--notify", action="store_true",
                   help="BLE mode: subscribe to change pushes instead of polling")
    p.add_argument("--source", choices=["auto", "wifi", "ble"], default="auto",
                   help="status transport: auto = WiFi if known, else BLE")
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

    p = sub.add_parser(
        "rescue",
        help="rescue the stuck-LIGHTING bug: engage powercut mode so the "
        "air damper holds the nominal air setting instead of staying wide "
        "open (see README, 'Stuck in LIGHTING')",
    )
    _add_address(p)
    p.add_argument("--temp", type=float, default=150.0,
                   help="stove temperature (C) at which LIGHTING counts as stuck")
    p.add_argument("--force", action="store_true",
                   help="engage even if the stove does not look stuck")
    p.add_argument("--wait", type=float, default=90.0,
                   help="seconds to wait for POWER CUT MODE confirmation")
    p.set_defaults(func=cmd_rescue)

    p = sub.add_parser(
        "guard",
        help="run the stuck-LIGHTING guard loop (auto-rescue: throttle at "
        "peak or after 60 min hot, exit when the fire is out, restore the "
        "previous mode, refuel assist). --enable/--disable set the "
        "persisted on/off (default off; also toggleable from Home "
        "Assistant)",
    )
    _add_address(p)
    p.add_argument("--source", choices=["auto", "wifi", "ble"], default="auto",
                   help="status transport: auto = WiFi if known, else BLE")
    p.add_argument("--interval", type=float, default=30.0,
                   help="poll interval in seconds (default 30)")
    p.add_argument("--enable", action="store_true",
                   help="enable the guard (persisted) and run it")
    p.add_argument("--disable", action="store_true",
                   help="disable the guard (persisted) and exit")
    p.add_argument("--peak", type=float, default=300.0,
                   help="peak stove temp (C) since the last door open needed "
                        "for throttle-at-peak engage (default 300)")
    p.add_argument("--fall-delta", type=float, default=20.0,
                   help="fall off the peak (C) that triggers engage (default 20)")
    p.add_argument("--backstop-minutes", type=float, default=60.0,
                   help="minutes hot (>= --temp) in LIGHTING before the "
                        "weak-fire backstop engages (default 60)")
    p.add_argument("--temp", type=float, default=150.0,
                   help="stove temp (C) at which LIGHTING counts as stuck (default 150)")
    p.add_argument("--exit-temp", type=float, default=60.0,
                   help="stove temp (C) below which the fire is considered out (default 60)")
    p.add_argument("--exit-minutes", type=float, default=10.0,
                   help="minutes cold before exiting powercut mode (default 10)")
    p.add_argument("--refuel-assist", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="refuel assist: door held open in powercut mode "
                        "reboots the controller so fresh fuel re-ignites "
                        "(default on; --no-refuel-assist)")
    p.add_argument("--door-gate", type=float, default=5.0,
                   help="seconds the door must be held open for the refuel "
                        "flare (default 5)")
    p.set_defaults(func=cmd_guard)

    p = sub.add_parser(
        "report",
        help="analyse a log (HA history export or `openwood watch --log` "
        "CSV) into burn cycles; --csv-dir writes per-cycle CSVs for support",
    )
    p.add_argument("logfile")
    p.add_argument("--csv-dir",
                   help="write one CSV per cycle + cycles_summary.csv here")
    p.add_argument("--json", action="store_true", help="machine-readable summary")
    p.add_argument("--min-temp", type=float, default=150.0,
                   help="drop lighting attempts that never reached this stove temp (C)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser(
        "restart",
        help="soft-restart the stove controller (same as the app's Restart; "
        "also brings WiFi back up from saved credentials). CAUTION: resets "
        "the mode to the default (Automatic, intensity 3) and Extended Burn "
        "off, and restarts the burn cycle",
    )
    _add_address(p)
    p.set_defaults(func=cmd_restart)

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
    p.add_argument("--password", help="omit to use the MQTT_PASSWORD environment "
                                     "variable, or to be prompted in a terminal")
    p.add_argument("--topic-prefix", default="homeassistant")
    p.add_argument("--poll-interval", type=float_or_none, default=15.0)
    p.add_argument("--name", default=None, help="entity name (defaults to configured name)")
    p.add_argument(
        "--status-source", type=str, default="auto",
        help="status transport: auto = WiFi if the stove IP is known, else "
        "the BLE notification stream. Control always uses BLE (a short "
        "on-demand link in WiFi mode).",
    )
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
    p.add_argument(
        "--stuck-guard", action="store_true",
        help="start the stuck-LIGHTING guard enabled (persisted; toggle at "
        "runtime from Home Assistant's 'Stuck guard' switch or "
        "`openwood guard --enable/--disable`)",
    )
    p.add_argument("--stuck-guard-peak", type=float_or_none, default=300.0,
                   help="peak stove temp (C) since the last door open needed "
                        "for throttle-at-peak engage (default 300)")
    p.add_argument("--stuck-guard-fall-delta", type=float_or_none, default=20.0,
                   help="fall off the peak (C) that triggers engage (default 20)")
    p.add_argument("--stuck-guard-backstop-minutes", type=float_or_none, default=60.0,
                   help="minutes hot (>= --stuck-guard-temp) in LIGHTING before "
                        "the weak-fire backstop engages (default 60)")
    p.add_argument("--stuck-guard-temp", type=float_or_none, default=150.0,
                   help="stove temp (C) at which LIGHTING counts as stuck (default 150)")
    p.add_argument("--stuck-guard-exit-temp", type=float_or_none, default=60.0,
                   help="stove temp (C) below which the fire is considered out (default 60)")
    p.add_argument("--stuck-guard-exit-minutes", type=float_or_none, default=10.0,
                   help="minutes cold before exiting powercut mode (default 10)")
    p.add_argument("--stuck-guard-refuel-assist",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="refuel assist: door held open in powercut mode "
                        "reboots the controller so fresh fuel re-ignites "
                        "(default on; --no-stuck-guard-refuel-assist)")
    p.add_argument("--stuck-guard-door-gate", type=float_or_none, default=5.0,
                   help="seconds the door must be held open for the refuel "
                        "flare (default 5)")
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
            if not (args.status_source or "auto").strip():
                args.status_source = "auto"
            if args.status_source not in ("auto", "wifi", "ble"):
                print(f"error: invalid --status-source: {args.status_source!r}",
                      file=sys.stderr)
                return 2
            if args.ext_temp_topic and args.ext_target is None:
                print("error: --ext-temp-topic requires --ext-target",
                      file=sys.stderr)
                return 2
            if not args.name:
                cfg = config.load()
                args.name = (cfg.get("names") or {}).get(args.address) or "Charnwood stove"
            args.password = mqtt_password(args)
            if args.user and not args.password:
                print("error: MQTT password required for "
                      f"{args.user!r}: use --password, set MQTT_PASSWORD, "
                      "or run in a terminal to be prompted", file=sys.stderr)
                return 2
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
