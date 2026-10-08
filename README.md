# openwood — control a Charnwood E stove (Aire 300) from Linux

`openwood` is a Linux controller for Charnwood E-series stoves (Aire 300 etc.).
The BLE protocol was reverse engineered from the official Charnwood-E Android
app (v2.0.31) — see [PROTOCOL.md](PROTOCOL.md) for the full documentation.

## Disclaimer

This is an **unofficial, independent** project. It is not affiliated
with, endorsed by, or supported by Charnwood (J Grove & Sons Ltd) in any
way. "Charnwood" and product names are trademarks of their respective
owner and are used here only to describe compatibility.

The BLE protocol was reverse engineered from the official Android app
for interoperability with your own stove. The software is provided
**as is, without warranty of any kind** — you use it entirely at your
own risk. A stove is a real fire in your home: while the stove's own
safety systems remain in control at all times, an unofficial controller
can change its behaviour. Always attend a lit stove as the manufacturer
advises, and never rely on this software as a safety device.

## What you get

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

Three supported routes, depending on your setup:

### Arch Linux (works with or without Home Assistant)

The `openwood-git` package (PKGBUILD in `packaging/aur/openwood-git/`)
installs the CLI system-wide plus an optional systemd service for the
MQTT bridge:

```sh
# from the AUR, or build from the PKGBUILD in this repository:
makepkg -si
openwood scan    # then `openwood use` to remember the stove
```

Home Assistant is optional — the CLI works standalone. If you do run
HA, continue to the [Home Assistant](#home-assistant) section.

### Any distro, from source

```sh
cd openwood
python3 -m venv .venv
.venv/bin/pip install -e ".[mqtt]"
.venv/bin/openwood scan
```

Requires BlueZ (any normal desktop Linux) and a BLE-capable Bluetooth
adapter.

### Home Assistant

See the [Home Assistant](#home-assistant) section for the MQTT bridge;
it needs a broker (mosquitto) but runs independently of how openwood
itself was installed.

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

The bridge publishes the stove to MQTT with HA auto-discovery. It was
verified end to end against a local broker with a live stove: 21 entities
discovered, live state every ~5 s from the stove's change-push stream.

### 1. Install an MQTT broker

Home Assistant needs a broker. On Arch:

```sh
sudo pacman -S mosquitto
sudo systemctl edit --full mosquitto    # or edit /etc/mosquitto/mosquitto.conf:
```

```conf
listener 1883 0.0.0.0
allow_anonymous false
password_file /etc/mosquitto/passwd
```

```sh
sudo mosquitto_passwd -c /etc/mosquitto/passwd homeassistant   # HA's user
sudo mosquitto_passwd /etc/mosquitto/passwd openwood           # bridge's user
sudo systemctl enable --now mosquitto
```

(On HAOS, use the "Mosquitto broker" add-on instead and skip this step.)

### 2. Add the MQTT integration in Home Assistant

Settings → Devices & Services → Add Integration → **MQTT**. Point it at the
broker (from HA on the same machine: `127.0.0.1`, port 1883) with the
`homeassistant` credentials. Enable "MQTT discovery" if asked (it's on by
default).

### 3. Test the bridge manually

```sh
.venv/bin/openwood mqtt \
    --host 127.0.0.1 --user openwood --password <bridge password> \
    --name "Charnwood Aire 300" --poll-interval 20
```

The stove must already be paired and set as default (`openwood use`).
Within ~30 s, the device "Charnwood Aire 300" appears in HA under
Settings → Devices & Services → MQTT, with:

| Entity | Type | Notes |
|---|---|---|
| Charnwood Aire 300 | climate | modes: heat=Automatic, auto=Room Temp, test=Test; setpoint 16-30 C |
| Stove/Room/Board temperature | sensors | C |
| Intensity | sensor | live burn output % |
| Air valve 1/2/3 position | sensors | gauge %, -1 = fault |
| Burn cycle state (+ name) | sensors | e.g. "STEADY STATE EFFICIENT E" |
| Error code / Error | sensor + binary | |
| Door open, Burning, Check fuel, Overfire, Extended burn | binary sensors | |
| Extended burn, Reload alerts | switches | |
| Manual level | number | burn intensity 1-5 |

Note: "off" on the climate card is not a real stove mode - it sets
Automatic intensity 1 (smallest output), same as the app's minimum.

You can also verify with mosquitto: `mosquitto_sub -t '#' -v`.

### 4. Run it as a service

**Arch / AUR** — the package ships the unit, a dedicated `openwood` service
user and `/etc/openwood/mqtt.env`:

```sh
# build/install openwood-git from the AUR (PKGBUILD is kept in
# packaging/aur/openwood-git/ in this repository)
makepkg -si   # inside your AUR clone, or use your AUR helper

# pick the stove once (writes /etc/openwood/config.json):
sudo env OPENWOOD_CONFIG=/etc/openwood/config.json openwood use

# edit /etc/openwood/mqtt.env (MQTT_USER/MQTT_PASSWORD, STOVE_NAME,
# and EXT_* if you want the external thermostat), then:
sudo systemctl enable --now openwood-mqtt
journalctl -u openwood-mqtt -f
```

**Other distros / manual install** — create a unit yourself
(`/etc/systemd/system/openwood-mqtt.service`), adjusting paths and the user
(any user works; BlueZ's D-Bus policy allows system-bus clients):

```ini
[Unit]
Description=Charnwood Aire 300 MQTT bridge
After=bluetooth.target mosquitto.service

[Service]
Environment=OPENWOOD_CONFIG=/etc/openwood/config.json
ExecStart=/orb/Dev/openwood/.venv/bin/openwood mqtt \
    --host 127.0.0.1 --user openwood --password <bridge password> \
    --name "Charnwood Aire 300" --poll-interval 20
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### External temperature control (optional)

The stove only knows the temperature at its own power-cable sensor, so a
warm room elsewhere (e.g. a bedroom upstairs) is invisible to it. The
bridge can act as an external thermostat: it watches any MQTT
temperature topic and adjusts the stove setpoint accordingly:

```sh
.venv/bin/openwood mqtt \
    --host 127.0.0.1 --user openwood --password <pw> \
    --ext-temp-topic homeassistant/sensor/bedroom_temperature/state \
    --ext-target 21
```

- Remote sensor above `target + hysteresis` (default 0.5 C) -> stove
  setpoint drops to `--ext-setpoint-low` (default 16 = minimum output,
  keeps the firebed but stops heating hard).
- Remote sensor at/below `target - hysteresis` -> setpoint returns to the
  comfort value (`--ext-setpoint-comfort`, defaults to the setpoint the
  stove was running when the bridge started).
- Writes are throttled (`--ext-min-interval`, default 120 s) and only
  applied while the stove is in Room Temperature mode, so manual control
  from the stove button or the phone app always wins.
- The remote temperature also shows up in Home Assistant as an
  "External temperature" sensor on the stove device.

(Alternatively, with no code: an HA automation on the climate entity's
setpoint does the same thing.)

### Notes

- The bridge holds one persistent BLE connection and re-subscribes
  automatically after radio drops; the stove allows up to 3 connected
  devices (bridge + phone app is fine).
- State updates come from the stove's change-push notifications (~5 s);
  polling is only a fallback. HA automations can trigger on door open,
  check fuel (reload due), overfire warning, or errors.
- The stove thermostat uses the sensor on its power cable, so HA's
  room-temperature entity may read warmer than elsewhere in the room.

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
