"""Detection of the stuck-LIGHTING firmware bug and the rescue logic.

Background (field report, Aire 300 fw 2.3.82 - 2.1.78, seasoned softwood):
the auto-mode burn state machine can remain in LIGHTING / LIGHTING E for
hours although the firebox has long since reached operating temperature
(350-400+ C within ~20 min). While trapped in LIGHTING the air dampers
stay wide open, so the fuel burns at maximum rate and the stove drops
from LIGHTING straight to SLEEP. The transition to EARLY BURN only
happens reliably when the fire smolders slowly before catching.

Rescue lever (verified live on the real stove, 2026-10-29):
- Test mode (MODE=2) is NOT a lever: the firmware reverts every mode
  write to Automatic while the state machine sits in LIGHTING/LIGHTING E
  (and also in SLEEP). Verified for modes 0, 1 and 2; the valve never
  moves because the mode is reverted within ~20 s.
- The special powercut mode (DATA register 0x19 = 1) IS the lever: it is
  a different register and is not subject to the mode revert. It takes
  the state machine to POWER CUT MODE (14) in both SLEEP and LIGHTING E
  and drives the air valves to the manual's "nominal air setting"
  (Aire 300: v1~1%, v2~25%, v3~13% gauge). The emergency modes are
  designed to work at any time, including mid-burn (that is the point
  of the chimney-fire mode).
- Exit: register 0x19 = 3 (the app's Restart button). The controller
  reboots: the special mode flag clears, the mode resets to Automatic
  (intensity 3), Extended Burn turns off, and the state machine restarts
  (LIGHTING E if a fire is still in the box, SLEEP when cold).

Field result of the rescue (2026-10-29/30 experiment): engaged at the
peak-fall of a stuck fire, the damper at the nominal setting let the coal
bed settle into a slow sustained smolder (stove held 60-150 C for hours)
instead of burning out at full blast - i.e. the fire can be saved, not
just stopped. When the box is refuelled during powercut, the guard
reboots the controller (a "flare") so the fresh fuel re-ignites under the
state machine's normal lighting sequence.

`StuckGuard` is the state machine used by the MQTT bridge and the
`openwood guard` / `openwood rescue` CLI commands to do this
automatically. Feed it one stove state per loop iteration via step(); it
returns a list of actions for the caller to execute against the stove:

- "engage": write special_mode(1) (powercut) - damper to nominal air.
  Engaged when the fire is stuck in LIGHTING (E) and either
    * it has peaked at/above `peak_temp` since the last door open and is
      now falling off that peak by `fall_delta` (throttle-at-peak), or
    * it never peaked but has been hot (>= `temp`) in LIGHTING for
      `backstop_minutes` (weak-fire backstop).
- "exit": write special_mode(3) (restart) - the fire is out, leave
  powercut mode and restart the controller. Triggered by the state
  machine reaching SLEEP, or by the stove staying below `exit_temp` for
  `exit_minutes`.
- "refuel_flare": door held open for `door_gate` seconds while in
  powercut (the user is refuelling): write special_mode(3) to reboot the
  controller so the fresh fuel re-ignites. Only when `refuel_assist` is
  on. The guard disengages but keeps the mode it replaced, so it can
  re-engage if the re-lit fire sticks, or restore the mode once the fire
  establishes / dies.
- "restore": re-apply the mode/level/setpoint/extended-burn the guard
  replaced (the exit/flare restart resets them); retried until the stove
  actually holds the mode, since writes are reverted while the state
  machine is in LIGHTING.

The guard is off by default (it deviates from the firmware's behaviour
and the bug may be fixed upstream); enable it from Home Assistant or
`openwood guard --enable`. While disabled, step() resets its tracking and
does nothing.
"""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

from . import protocol as P

# Burn cycle states (values of DATA register 0d, see BURN_CYCLE_NAMES)
LIGHTING_STATES = (1, 18)      # LIGHTING / LIGHTING E
SLEEP_STATES = (0, 12, 13)     # STARTUP / STAND BY / SLEEP (fire out)
POWERCUT_STATE = 14            # POWER CUT MODE
CHIMNEY_FIRE_STATE = 15        # CHIMNEY FIRE RESPONSE MODE


def stuck_in_lighting(st: P.StoveState, temp_threshold: float = 150.0) -> bool:
    """True if the state machine appears stuck in the lighting phase.

    Stuck = the burn cycle is still LIGHTING (E) although the stove has
    long passed the lighting range, and we are not already in an
    emergency mode (powercut/chimney-fire hold the damper by design).
    """
    if st.burn_cycle is None or st.burn_cycle not in LIGHTING_STATES:
        return False
    if st.special_mode in (P.SPECIAL_POWERCUT, P.SPECIAL_CHIMNEY_FIRE):
        return False
    return (st.stove_temp or 0) >= temp_threshold


class StuckGuard:
    """Automatic rescue for the stuck-LIGHTING bug (bridge / CLI side).

    Feed it one stove state per loop iteration via step(); it returns a
    list of actions for the caller to execute against the stove (see the
    module docstring). The guard remembers the mode it replaced so it can
    restore it after the exit/flare restart, and it adopts a powercut
    mode that was engaged out-of-band (e.g. `openwood rescue` from the
    CLI, or the app's emergency button) so it can still exit, refuel and
    restore properly.
    """

    def __init__(
        self,
        peak_temp: float = 300.0,
        fall_delta: float = 20.0,
        backstop_minutes: float = 60.0,
        temp: float = 150.0,
        exit_temp: float = 60.0,
        exit_minutes: float = 10.0,
        refuel_assist: bool = True,
        door_gate: float = 20.0,
        restore_interval: float = 300.0,
        restore_timeout: float = 4 * 3600.0,
        enabled: bool = True,
    ):
        self.peak_temp = peak_temp            # peak needed for throttle-at-peak
        self.fall_delta = fall_delta          # fall off the peak that triggers
        self.backstop_wait = backstop_minutes * 60.0  # weak-fire backstop
        self.temp = temp                      # "hot enough" for the backstop
        self.exit_temp = exit_temp            # fire is out below this
        self.exit_wait = exit_minutes * 60.0
        self.refuel_assist = refuel_assist    # flare on door-open in powercut
        self.door_gate = door_gate            # door must be held this long (s)
        self.restore_interval = restore_interval
        self.restore_timeout = restore_timeout
        self.enabled = enabled
        # tracking
        self.max_temp: float | None = None    # peak since the last door open
        self.backstop_since: float | None = None
        self.door_since: float | None = None
        self.cold_since: float | None = None
        self.engaged = False
        self.pending_engage_until: float | None = None
        self.pending_exit_until: float | None = None
        self.last_restore_attempt: float = 0.0
        self.restore_attempted = False
        self.restore_deadline: float | None = None
        self.original: tuple[int, int | None, float | None, bool | None] | None = None
        # last observation (used by state())
        self.last_bc: int | None = None
        self.last_temp: float = 0.0

    def state(self) -> str:
        """Coarse state for display / the HA sensor.

        One of: disabled, idle, tracking, engaged, smoldering, fire-out,
        restoring.
        """
        if not self.enabled:
            return "disabled"
        if not self.engaged and self.original is not None:
            return "restoring"
        if self.engaged:
            if self.last_temp < self.exit_temp:
                return "fire-out"
            if self.last_temp < self.temp:
                return "smoldering"
            return "engaged"
        if (self.last_bc is not None and self.last_bc in LIGHTING_STATES
                and (self.last_temp >= self.temp
                     or (self.max_temp or 0.0) >= self.peak_temp)):
            return "tracking"
        return "idle"

    def set_enabled(self, on: bool) -> None:
        """Enable/disable the guard at runtime.

        Disabling resets all tracking (including any in-progress
        engagement): the guard becomes fully inert and leaves the stove
        as-is for manual handling. Re-enabling starts fresh; an out-of-
        band powercut is re-adopted on the next step.
        """
        if self.enabled == on:
            return
        self.enabled = on
        if not on:
            self._reset()

    def _reset(self) -> None:
        self.max_temp = None
        self.backstop_since = None
        self.door_since = None
        self.cold_since = None
        self.engaged = False
        self.pending_engage_until = None
        self.pending_exit_until = None
        self.last_restore_attempt = 0.0
        self.restore_attempted = False
        self.restore_deadline = None
        self.original = None

    def _finish_engagement(self, now: float, pending_engage: bool = False) -> None:
        self.engaged = False
        self.cold_since = None
        self.backstop_since = None
        if pending_engage:
            # after a refuel flare the state machine restarts in LIGHTING
            # E: give it a window before a possible re-engage
            self.pending_engage_until = now + 120.0
        # the exit/flare write reboots the controller: block re-adoption
        # for a window, and give it a full restore interval before the
        # first restore attempt
        self.pending_exit_until = now + 120.0
        self.last_restore_attempt = now

    def _capture_original(self, st: P.StoveState, now: float) -> None:
        if self.original is None:
            self.original = (
                st.mode if st.mode is not None else P.MODE_AUTOMATIC,
                st.manual_level,
                st.room_setpoint,
                st.extended_burn,
            )
            self.restore_deadline = now + self.restore_timeout

    def step(self, st: P.StoveState, now: float) -> list[str]:
        actions: list[str] = []
        bc = st.burn_cycle
        temp = st.stove_temp or 0.0
        self.last_bc = bc
        self.last_temp = temp

        if not self.enabled:
            self._reset()
            return []

        # -- door open: re-anchor the peak epoch + backstop, track gate --
        if st.door_open:
            if self.door_since is None:
                self.door_since = now
            self.max_temp = temp
            self.backstop_since = None
        elif self.door_since is not None:
            self.door_since = None

        # -- track the peak since the last door open --------------------
        if bc in SLEEP_STATES:
            self.max_temp = None  # fire out: no peak to fall from
        elif self.max_temp is None or temp > self.max_temp:
            self.max_temp = temp

        # -- adopt an out-of-band powercut ------------------------------
        if (not self.engaged and bc in (POWERCUT_STATE, CHIMNEY_FIRE_STATE)
                and (self.pending_exit_until is None
                     or now >= self.pending_exit_until)):
            self.engaged = True
            self._capture_original(st, now)
            self.cold_since = None
            # the powercut is live: the pending-engage window (waiting for
            # our write to land, or the post-flare grace period) is done
            self.pending_engage_until = None

        # -- refuel assist: door held during powercut -> flare ----------
        if (self.engaged and self.refuel_assist and self.door_since is not None
                and now - self.door_since >= self.door_gate
                and (self.pending_engage_until is None
                     or now >= self.pending_engage_until)):
            self._finish_engagement(now, pending_engage=True)
            return ["refuel_flare"]

        if self.engaged:
            if bc in SLEEP_STATES:
                # the state machine reached SLEEP: the fire is out
                actions.append("exit")
                self._finish_engagement(now)
            elif temp < self.exit_temp:
                if self.cold_since is None:
                    self.cold_since = now
                if now - self.cold_since >= self.exit_wait:
                    # stayed cold long enough: the fire is out
                    actions.append("exit")
                    self._finish_engagement(now)
            else:
                self.cold_since = None
        else:
            stuck = (bc is not None and bc in LIGHTING_STATES
                     and st.special_mode not in
                     (P.SPECIAL_POWERCUT, P.SPECIAL_CHIMNEY_FIRE))
            if stuck:
                peak = self.max_temp or 0.0
                peak_fall = (peak >= self.peak_temp
                             and temp >= self.temp
                             and temp <= peak - self.fall_delta)
                if peak_fall and (self.pending_engage_until is None
                                  or now >= self.pending_engage_until):
                    # the fire established (peaked) but the state machine
                    # never left LIGHTING: throttle it before it burns out
                    self._capture_original(st, now)
                    self.restore_attempted = False
                    actions.append("engage")
                    self.pending_engage_until = now + 120.0
                elif temp >= self.temp:
                    # weak fire: never peaked, but hot and stuck in LIGHTING
                    if self.backstop_since is None:
                        self.backstop_since = now
                    if now - self.backstop_since >= self.backstop_wait and (
                            self.pending_engage_until is None
                            or now >= self.pending_engage_until):
                        self._capture_original(st, now)
                        self.restore_attempted = False
                        actions.append("engage")
                        self.pending_engage_until = now + 120.0
                else:
                    self.backstop_since = None
            else:
                self.backstop_since = None

        # Restore the mode we replaced. Mode writes are reverted while the
        # state machine is in LIGHTING (they stick once combustion is
        # established), so only attempt outside of it, and retry until the
        # stove actually holds the mode or the deadline passes.
        if (self.original is not None and not self.engaged
                and self.restore_deadline is not None
                and now < self.restore_deadline
                and (bc is None or bc not in LIGHTING_STATES)):
            if (self.restore_attempted and st.mode is not None
                    and st.mode == self.original[0]):
                # a restore has stuck (or the user re-applied the mode):
                # nothing left to do. The attempt flag matters because the
                # exit/flare reboot resets the mode to Automatic, which may
                # coincidentally equal the original mode while the level /
                # extended burn it also reset have not been restored yet.
                self.original = None
                self.restore_deadline = None
            elif now - self.last_restore_attempt >= self.restore_interval:
                actions.append("restore")
                self.last_restore_attempt = now
                self.restore_attempted = True

        return actions

    async def restore_mode(self, stove) -> None:
        """Re-apply the mode the guard replaced (async)."""
        if self.original is None:
            return
        mode, level, setp, ext = self.original
        await stove.set_mode(mode, level, setp)
        if ext is not None:
            await stove.set_extended_burn(ext)
