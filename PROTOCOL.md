# Charnwood E stove BLE protocol — reverse engineered

Reverse engineered from the Android app `com.charnwood.charnwoodcontrol_v2`
(Charnwood-E v2.0.31, versionCode 68). The stove controller is ESP32-based
(error strings reference ESP32 OTA, SD card, WiFi credentials, and an
"ATM" secondary MCU firmware).

## Transport

| Path | Usage |
|---|---|
| BLE (primary) | Control, status, WiFi provisioning, time sync |
| WiFi HTTP (after provisioning) | `GET http://<stove-ip>/get-reading-set` → status string |
| Vendor server | `GET <update-server>/info` → firmware metadata; `https://www.charnwood.com/fms?a=<model>&b=<serial>` |

- The stove advertises BLE service `116eff00-d316-476f-b90e-e8b186ac4bc5`
  (device name "Charnwood"). A legacy service UUID
  `2a51ff00-8045-425a-99bb-60ac5015c409` is also recognised by the app.
- Pairing: the stove requires a button press (blinking blue light) plus a
  numeric-comparison PIN confirm on the connecting device. The bond persists;
  `delete-pairing` (META register 09, value 3) clears it on the stove side.
- Actual GATT layout (verified on an Aire 300, firmware "2.3.36 - 2.1.29"):

  | Service | Characteristics |
  |---|---|
  | `2a51ff00-…` | 85c7ff00–08 |
  | `2a51ff01-…` | 85c7ff0a–13 |
  | `2a51ff02-…` | 85c7ff14–19 |
  | `116eff00-…` | 7877ff00–09 |
  | `116eff01-…` | 7877ff0a–10 (7877ff10 unknown, read+write) |

- The app requests MTU 80 and polls by *reading* characteristics
  (about every 5 s); it never subscribes to notifications.
- The stove firmware DOES support notifications: subscribed
  characteristics push on change, roughly every 5 s. Subscribing to many
  characteristics must be paced (~0.4 s between CCCD writes) or the
  ESP32 drops the connection. Pushes only carry *changed* registers,
  so seed the full state with one poll first.
- All writes are WRITE_TYPE_DEFAULT (with response).

## GATT characteristics

Two UUID families, indexed by a byte `XX` (hex):

- DATA registers: `85c7ffXX-c814-4363-9cfa-b4469459dc22`
- META registers: `7877ffXX-93d5-4f70-b4ac-ec360a6944c9`

### DATA registers (85c7ffXX) — little-endian values

| XX | Name | Type | R/W | Notes |
|----|------|------|-----|-------|
| 00 | stove_temp | int32 LE | R | °C; >1500 → display 999 |
| 01 | intensity | int | R | live combustion output; raw 0–400 → % = min(100, v·100/400). The app's DB column is misleadingly called `lightlevel`. |
| 02 | room_temp | float32 LE | R | °C; >1000 → display 99.9 |
| 03 | room_setpoint | float32 LE | R/W | 16.0–30.0 °C in 0.5 steps |
| 04 | door_open | u8 | R | 1 = open |
| 05 | overfire_warning | u8 | R | 1 = warning |
| 07 | overnight | u8 | R/W | write `[v, 0x00]` |
| 08 | check_fuel | u8 | R | 1 = refuel advised |
| 09 | burning | u8 | R | 1 = fire active |
| 0d | burn_cycle | int32 LE | R | state machine enum, see below |
| 0f | mode | u8 | R/W | 0 = manual, 1 = auto (room setpoint), 2 = boost |
| 11 | error_code | int32 LE | R | 0 / 192 (0xC0) / 226 / 227 / 228 = no error, else error list |
| 12 | dial_position | int (big-endian minimal) | R/W | output level 1–5 in manual; UI dial tick 1–29 in auto (setpoint = 16 + (tick−1)/2, e.g. 13 → 22 °C); 50 while boosting. Authoritative room setpoint is register 03. |
| 14 | valve_1 | int32 LE | R | air inlet valve 1 raw position |
| 13 | valve_2 | int32 LE | R | air inlet valve 2 raw position |
| 15 | valve_3 | int32 LE | R | air inlet valve 3 raw position |
| 16 | unknown_s | u8 | R | |
| 17 | fuel_alert | u8 | R/W | toggle "alerts: fuel" |
| 18 | board_temp | float32 LE | R | °C |
| 19 | special_mode | u8 | R/W | read: stove state; write: see below |

Writes to DATA registers:

- `mode` = write `[mode]`. The app always writes manual_level and
  room_setpoint together with mode (same triple-write for dial changes).
- `manual_level` value is sent as `BigInteger.valueOf(n).toByteArray()`
  (minimal-length big-endian; single byte for n ≤ 255).
- `special_mode` write values: `1` = powercut mode (stove state 1,
  forces mode 0 / level 2), `2` = chimney fire mode (state 2, forces
  mode 0 / level 1), `3` = cancel special mode / restart stove.

### META registers (7877ffXX)

| XX | Name | Type | R/W | Notes |
|----|------|------|-----|-------|
| 00 | firmware_version | int32 LE | R | displayed as decimal string |
| 01 | firmware_release | ascii | R | release name |
| 02 | location/model | ascii | R | |
| 03 | datetime | ascii | R/W | `yyyyMMddHHmmss`; app rewrites if drift > 60 s |
| 04 | wifi_command | u8 | W | `2` = save/apply credentials, `3` = delete saved network |
| 05 | wifi_ssid | ascii | R/W | |
| 06 | wifi_password | ascii | W | |
| 07 | ip_address | ascii | R | stove's WiFi IP |
| 08 | wifi_status | ascii | R | non-empty = WiFi connected |
| 09 | command | u8 | W | `1` = start firmware update, `2` = special update, `3` = delete pairing info |
| 0a | update_status | u8 | R | |
| 0b | op_error | int | R | error code during WiFi/firmware operations |
| 0c | burn_minutes | uint16 LE | R | minutes counter |
| 0e | update_url | ascii | R | firmware update server base URL |
| 0f | full_status | ascii | R | packed status string, see below |

### Packed status string (register 7877ff0f, ASCII hex digits)

56-character layout (60-char variant pads threshold fields to 4 digits and
board temp to [55:59]):

```
[0:8]   date (hex of yyyyMMdd)      [8:11]  minutes of day (hex)
[11:15] stove temp (signed16/1°C)   [15:18] light level raw (hex, /400→%)
[18:22] room temp (signed16, /10)   [22:26] room setpoint (signed16, /2)
[26]    door_open                   [27]    overfire
[28]    overnight                   [29]    check_fuel
[30]    burning                     [31:33] burn_cycle_progress
[33]    mode                        [34:38] error_code
[38:40] manual_level               [40:43] threshold_p  ([40:44] in 60-char)
[43:46] threshold_q ([44:48])       [46:49] threshold_r ([48:52])
[49]    unknown_s                  [50]    fuel_alert     [51]    special_mode/state
[52:56] board temp (signed16, /10)  (60-char: [52] s, [53] fuel_alert, [54] state, [55:59] board temp)
```

`h.q(str)`: 4 hex chars → signed 16-bit (sign-extend from bit 15).

The HTTP endpoint `/get-reading-set` returns the same style of string in a
37-char (legacy) or 59-char layout:

```
37-char: [0:3] minutes-of-day | [3:7] room temp /10 | [7:11] stove temp |
  [11:15] board temp /10 | [15:18] light raw | [18:21] threshold_q |
  [21:24] threshold_p | [24:27] threshold_r | [27:29] door |
  [29:31] burn cycle | [31:33] manual level | [33:35] mode | [35:37] setpoint /2

59-char: [0:8] date | [8:11] minutes | [11:15] stove temp | [15:18] light |
  [18:22] room temp | [22:26] setpoint | [26] door | [27] overfire |
  [28] overnight | [29] check_fuel | [30] burning | [31:33] burn cycle |
  [33] mode | [34:38] error | [38:40] level | [40:44] p | [44:48] q |
  [48:52] r | [52] s | [53] fuel_alert | [54] state | [55:59] board temp
```

## Modes (per the Aire 300/400/500/700 manual)

| Mode | Value | Light | Meaning | Dial |
|------|-------|-------|---------|------|
| Automatic | 0 | blue | burn intensity | intensity 1–5 (3 = default) |
| Room Temperature | 1 | green | room thermostat | setpoint 16–30 °C in 0.5 steps (presets 16/20/23/26/30) |
| Test | 2 | red | air control percent (service mode) | 0–100 % (presets shutdown/25/50/75/100) |

In Test mode the stove reverts to Automatic the next time the door is
opened. Emergency/special modes (DATA register 19): powercut (1, nominal
air for manual operation) and chimney fire (2, complete air shutdown);
write 3 to cancel/restart. Writing 3 in a normal state performs the
app's "Restart": the controller reboots (~45 s), valve positions hold,
the clock keeps running, WiFi re-connects from saved credentials, but the
mode resets to the default (Automatic, intensity 3), Extended Burn
switches off, and the burn state machine restarts at LIGHTING E
(verified on a live Aire 300). Extended burn (register 07): stove shuts down
to preserve a char firebed until refuelled. Alerts (register 17): pulses
the stove light blue when a reload is due.

The room temperature sensor is on the DC extension cable behind the
stove; the thermostat acts on that reading, which runs warmer than the
room at a distance. In Room Temperature mode the stove pursues the
setpoint only "once good combustion has been established" — right after
a refuel (LIGHTING E / EARLY BURN E states) the air stays open to relight
the fuel, so output overshoots even if the room is already at temperature.

## Error codes

From the app's `error_list` resource (ESP32 side): 0x01–0x1e filesystem /
WiFi / OTA errors, 0x20–0x21 BLE modem, 0x22 "no update available",
0x23–0x26 serial number, 0x27+ certificate/log errors. 192 (0xC0) and
226–228 are treated as non-errors by the UI.

## Burn cycle state machine

The DATA register 0d value is a stove state enum. The app's
`burn_cycle_list` resource names all 23 states; the progress ring (quarters
0–4, 0 = hidden) maps each state via `burn_cycle_progress_values`:

| Value | State | Ring |
|---|---|---|
| 0 | STARTUP | – |
| 1 | LIGHTING | 1 |
| 2 | EARLY BURN | 2 |
| 3 | EARLY BURN INEFFICIENT | 2 |
| 4 | STEADY STATE EFFICIENT | 3 |
| 5 | STEADY STATE INEFFICIENT | 3 |
| 6 | LATE BURN EFFICIENT | 3 |
| 7 | LATE BURN INEFFICIENT | 3 |
| 8 | CHAR | 4 |
| 9 | OVERNIGHT | 4 |
| 10 | TEST | – |
| 11 | OVER-FIRE | – |
| 12 | STAND BY | – |
| 13 | SLEEP | – |
| 14 | POWER CUT MODE | – |
| 15 | CHIMNEY FIRE RESPONSE MODE | – |
| 16 | EMC TEST | – |
| 17 | PIDCAL | – |
| 18 | LIGHTING E | 1 |
| 19 | EARLY BURN E | 2 |
| 20 | STEADY STATE EFFICIENT E | 3 |
| 21 | STEADY STATE INEFFICIENT E | 3 |
| 22 | CHAR E | 4 |

The "E" (electronic/automatic) states form the auto-mode burn loop:
refuelling from CHAR E restarts at LIGHTING E. ### Air inlet valve positions (registers 13/14/15)

The dashboard screen draws three disc gauges — the air inlet valve
positions (air control motors 1–3):

- valve 1 ← register 14, valve 2 ← register 13, valve 3 ← register 15
- gauge position = (1024 − raw) / 1024 of full sweep
  (pointer angle = ((1024 − raw) × 270 / 1024) − 135 degrees)
- raw = 1111 is a fault marker (red dot in the app)

Higher raw values point the gauge toward the low end. The burn
intensity shown on the same screen is register 01 (0–100%).

## App-only features (no stove protocol involved)

- Stove profiles (id, name, MAC, location, °C/°F) in local SQLite.
- History graphs: the app logs readings to a local `Readings` table
  (readingdt, stoveid, roomtemp, stovetemp, doorevent, stovestate,
  lightlevel, boardtemp) and charts them; the stove itself only provides
  the live snapshot and a minutes counter.

---
*This protocol documentation was produced by reverse engineering the
official Charnwood-E Android app for interoperability purposes. It is
an unofficial, independent description; Charnwood has not endorsed or
reviewed it. No warranty — verify against your own stove.*

*Documentation licensed GPL-3.0-or-later (see LICENSE). Does not grant
any rights over Charnwood's app or firmware.*
