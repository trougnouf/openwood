# openwood — control a Charnwood E stove (Aire 300) from Linux

`openwood` is a Linux controller for Charnwood E-series stoves (Aire 300 etc.).
The BLE protocol was reverse engineered from the official Charnwood-E Android
app (v2.0.31) — see [PROTOCOL.md](PROTOCOL.md) for the full documentation.

Features (everything the Android app does):

- Scan for and pair with the stove over Bluetooth LE
- Live status: stove/room/board temperatures, mode, setpoint, light level,
  burn cycle progress, door-open / overfire / check-fuel warnings, error codes
- Control: manual level (1–5), auto mode with room setpoint (16–30 °C),
  boost mode, overnight burn, fuel alert toggle
- Special modes: powercut, chimney fire, cancel/restart
- WiFi provisioning (SSID/password over BLE, delete credentials)
- Clock sync from the computer
- Firmware: read version/release/update server, check for updates, trigger
  the on-stove update process
- Delete pairing info on the stove
- Continuous polling with optional CSV logging (like the app's graphs data)
- MQTT bridge with Home Assistant auto-discovery (climate + sensors + switches)

## Install

```sh
cd /orb/Dev/openwood
python3 -m venv .venv
.venv/bin/pip install -e ".[mqtt]"
```

Requires BlueZ (present on any normal Linux desktop) and a BLE-capable
Bluetooth adapter.

## Pairing (one time)

The stove only accepts connections after it has been paired with your
computer. The first time you connect:

1. Start a connection: `.venv/bin/openwood status <MAC>` (get the MAC from
   `openwood scan`).
2. The stove's light starts blinking blue — press the blue button on the
   stove to accept the pairing request.
3. Confirm the PIN on your computer ("the PIN is correct" prompt).

The bond is then stored; subsequent connections are automatic without the
button. If you ever want to start over: `openwood delete-pairing <MAC>`
(or `bluetoothctl remove <MAC>`).

## Usage

You normally don't type MAC addresses: `openwood use` saves a default stove
(and if exactly one stove is in range, it needs no argument at all). Every
command also works with an explicit address when you have more than one stove.

```sh
# one-time setup
.venv/bin/openwood use                  # scans, saves the stove as default
.venv/bin/openwood use 10:06:1C:E5:10:3E --name "Aire 300"   # or explicit
.venv/bin/openwood stoves               # list known stoves

# from now on, no address needed
.venv/bin/openwood status                # full status (firmware, wifi, clock)
.venv/bin/openwood status --json

# poll forever, log to CSV
.venv/bin/openwood watch --interval 30 --log readings.csv

# live mode: subscribe to the stove's change pushes (updates ~5 s)
.venv/bin/openwood watch --notify

# control (modes: automatic=intensity, room-temp=thermostat, test=air %)
.venv/bin/openwood set-mode room-temp --setpoint 22.5
.venv/bin/openwood set-mode automatic --level 3
.venv/bin/openwood set-mode test --level 50     # service mode: air 50%
.venv/bin/openwood set-temp 21.0                # room temperature mode
.venv/bin/openwood set-level 2                  # burn intensity 1-5
.venv/bin/openwood set-air 25                   # test mode air percent
.venv/bin/openwood toggle extended-burn on
.venv/bin/openwood toggle alerts off

# special modes
.venv/bin/openwood special powercut
.venv/bin/openwood special chimney-fire
.venv/bin/openwood special cancel

# housekeeping
.venv/bin/openwood sync-time
.venv/bin/openwood wifi set --ssid MyNet --password secret
.venv/bin/openwood wifi show
.venv/bin/openwood wifi delete
.venv/bin/openwood firmware
.venv/bin/openwood update                # trigger on-stove OTA
.venv/bin/openwood delete-pairing       # clear the bond on the stove side
.venv/bin/openwood forget <MAC>          # remove from config
```

Without a configured default, commands auto-discover the stove (uses it if
exactly one is in range, otherwise lists candidates). With multiple stoves,
pass an address explicitly or run `openwood use <MAC>`.

The config lives at `~/.config/openwood/config.json` (override with
`OPENWOOD_CONFIG`). If no writable user config directory exists — as on this
machine, where `$HOME` is read-only — openwood falls back to
`src/openwood/config.json` next to the package and tells you so.

## Home Assistant

Run the MQTT bridge on any always-on machine within Bluetooth range of the
stove (it can be the HA box itself if it has Bluetooth, or a small always-on
Linux box/NUC):

```sh
.venv/bin/openwood mqtt \
    --host homeassistant.local --user mqttuser --password mqttpass \
    --name "Charnwood Aire 300" --poll-interval 30
```

The bridge keeps a persistent BLE connection and uses the stove's
change-push notifications (updates roughly every 5 s) with polling as
fallback. The stove supports up to 3 connected devices, so the bridge and
your phone app can both stay connected.

Entities appear in Home Assistant automatically via MQTT discovery:

- **Climate** entity (modes: heat=manual, auto, boost; setpoint 16–30 °C,
  current temperature from the room sensor)
- Sensors: stove temp, room temp, board temp, light level, burn cycle,
  error code
- Binary sensors: door open, burning, check fuel, overfire warning,
  overnight, error
- Switches: overnight burn, fuel alert
- Number: manual level (1–5)

For a systemd service, drop this in `/etc/systemd/system/openwood-mqtt.service`
and adjust paths/credentials:

```ini
[Unit]
Description=Charnwood stove MQTT bridge
After=bluetooth.target

[Service]
ExecStart=/orb/Dev/openwood/.venv/bin/openwood mqtt 10:06:1C:E5:10:3E --host homeassistant.local --user mqttuser --password mqttpass
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## WiFi notes

Once the stove is on your WiFi (via `openwood wifi set` or the phone app),
it serves `http://<stove-ip>/get-reading-set` with a status snapshot — handy
for polling without Bluetooth (the Android app uses this too). BLE is still
required for provisioning and firmware commands.

## Project layout

```
apk/                  the original APK
decompiled/           jadx output (Java sources)
apktool-out/          apktool output (resources incl. error list)
tools/                 jadx
src/openwood/
  protocol.py         UUIDs, register map, parsers, error codes
  client.py           bleak-based Stove client
  cli.py              argparse CLI
  mqtt_bridge.py      Home Assistant MQTT bridge
PROTOCOL.md           full protocol documentation
```

## Caveats

- The stove's BLE link is short-range; with the stove downstairs and the
  adapter upstairs (RSSI ~ -78) some reads need retries. `openwood`
  retries each register 3 times and tolerates partial failures.
- The reverse engineering was done against app v2.0.31 / stove firmware
  "2.3.36 - 2.1.29"; register meanings are documented in PROTOCOL.md, and
  registers the app ignores are exposed raw where sensible.
