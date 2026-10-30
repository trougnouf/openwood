# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer
"""StuckGuard tests: synthetic-clock run of the rescue state machine.

Run: python -m tests.test_stuck
"""
from openwood import protocol as P
from openwood.stuck import (
    POWERCUT_STATE,
    StuckGuard,
    stuck_in_lighting,
)

LIGHTING_E = 18
EARLY_BURN_E = 2
SLEEP = 13


def st(burn_cycle=None, temp=None, special=None, mode=None, level=None,
       setp=None, ext=None, door=False):
    return P.StoveState(
        burn_cycle=burn_cycle, stove_temp=temp, special_mode=special,
        mode=mode, manual_level=level, room_setpoint=setp,
        extended_burn=ext, door_open=door,
    )


def _guard(**kw):
    base = dict(peak_temp=300.0, fall_delta=20.0, backstop_minutes=60.0,
                temp=150.0, exit_temp=60.0, exit_minutes=10.0,
                restore_interval=300.0, restore_timeout=4 * 3600.0)
    base.update(kw)
    return StuckGuard(**base)


# -- stuck_in_lighting -------------------------------------------------------


def test_stuck_requires_hot_lighting():
    assert stuck_in_lighting(st(LIGHTING_E, 350))
    assert not stuck_in_lighting(st(LIGHTING_E, 100))
    assert not stuck_in_lighting(st(EARLY_BURN_E, 350))
    assert not stuck_in_lighting(st(SLEEP, 350))
    assert not stuck_in_lighting(st(None, 350))


def test_stuck_ignores_emergency_modes():
    # powercut/chimney-fire hold the damper by design: not "stuck"
    assert not stuck_in_lighting(st(LIGHTING_E, 350, special=P.SPECIAL_POWERCUT))
    assert not stuck_in_lighting(st(LIGHTING_E, 350, special=P.SPECIAL_CHIMNEY_FIRE))


# -- guard: engage at peak fall ---------------------------------------------


def test_peak_fall_engage():
    g = _guard()
    t = 0.0
    # the fire established (peaked 350) but the state machine sits in LIGHTING E
    assert g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t) == []
    assert g.state() == "tracking"
    # 340: still within 20 C of the peak -> hold
    t += 60
    assert g.step(st(LIGHTING_E, 340, mode=1, level=3, setp=23.0, ext=True), t) == []
    # 330 = peak - 20: throttle at peak
    t += 60
    assert g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t) == ["engage"]
    # the stove reaches POWER CUT MODE shortly after; the guard adopts it
    t += 10
    assert g.step(st(POWERCUT_STATE, 330, special=1), t) == []
    assert g.state() == "engaged"
    assert g.original == (1, 3, 23.0, True)


def test_weak_fire_backstop():
    g = _guard()
    t = 0.0
    # weak fire: hot (>= 150) but never peaks at 300
    assert g.step(st(LIGHTING_E, 200, mode=0, level=3), t) == []
    assert g.state() == "tracking"
    t += 3599  # 59 min 59 s: not yet
    assert g.step(st(LIGHTING_E, 210, mode=0, level=3), t) == []
    t += 1  # 60 min hot in LIGHTING -> backstop engage
    assert g.step(st(LIGHTING_E, 210, mode=0, level=3), t) == ["engage"]


def test_state_idle_when_cold():
    g = _guard()
    assert g.step(st(LIGHTING_E, 100, mode=0, level=3), 0.0) == []
    assert g.state() == "idle"


def test_door_open_reanchors_peak():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)  # peak 350
    # door open (refuel): the peak epoch re-anchors to the current temp
    t += 30
    g.step(st(LIGHTING_E, 300, mode=0, level=3, door=True), t)
    # with the old peak of 350, 290 would have triggered (350-20=330);
    # with the re-anchored peak of 300, only <= 280 triggers
    t += 30
    assert g.step(st(LIGHTING_E, 290, mode=0, level=3), t) == []
    t += 30
    assert g.step(st(LIGHTING_E, 279, mode=0, level=3), t) == ["engage"]


# -- guard: refuel assist ----------------------------------------------------


def test_refuel_flare():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t)  # engage
    t += 10
    g.step(st(POWERCUT_STATE, 300, special=1), t)  # adopted
    # the user opens the door (refuelling)
    t += 30
    assert g.step(st(POWERCUT_STATE, 150, special=1, door=True), t) == []
    # gate not reached yet (19 s)
    t += 19
    assert g.step(st(POWERCUT_STATE, 150, special=1, door=True), t) == []
    # door held 20 s -> flare (controller reboot so the fresh fuel re-ignites)
    t += 1
    assert g.step(st(POWERCUT_STATE, 150, special=1, door=True), t) == ["refuel_flare"]
    assert not g.engaged
    assert g.original == (1, 3, 23.0, True)  # kept for the restore
    assert g.state() == "restoring"
    # the state machine restarts in LIGHTING E; the re-lit fire peaks again
    t += 130  # past the 120 s post-flare grace window
    assert g.step(st(LIGHTING_E, 350, mode=0, level=3), t) == []
    t += 60
    assert g.step(st(LIGHTING_E, 329, mode=0, level=3), t) == ["engage"]


def test_refuel_assist_off():
    g = _guard(refuel_assist=False)
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=0, level=3), t)  # engage
    t += 10
    g.step(st(POWERCUT_STATE, 300, special=1), t)
    # door held open well past the gate: no flare
    t += 30
    g.step(st(POWERCUT_STATE, 150, special=1, door=True), t)
    t += 30
    assert g.step(st(POWERCUT_STATE, 150, special=1, door=True), t) == []


# -- guard: enabled toggle ---------------------------------------------------


def test_disabled_guard():
    g = _guard(enabled=False)
    t = 0.0
    # disabled: fully inert, no tracking
    assert g.step(st(LIGHTING_E, 350, mode=0, level=3), t) == []
    assert g.state() == "disabled"
    t += 3600
    assert g.step(st(LIGHTING_E, 330, mode=0, level=3), t) == []
    # re-enable: starts fresh (no engage from stale tracking)
    g.set_enabled(True)
    t += 1
    assert g.step(st(LIGHTING_E, 350, mode=0, level=3), t) == []
    assert g.state() == "tracking"


def test_disable_mid_engagement():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t)  # engage
    t += 10
    g.step(st(POWERCUT_STATE, 300, special=1), t)  # adopted
    assert g.state() == "engaged"
    # the user disables from HA: the guard becomes inert and drops all
    # tracking (the stove is left as-is for manual handling)
    g.set_enabled(False)
    assert g.state() == "disabled"
    assert g.original is None
    t += 60
    assert g.step(st(POWERCUT_STATE, 300, special=1), t) == []


# -- guard: full engage -> hold -> cold -> exit -> restore -------------------


def test_exit_and_restore():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t)  # engage
    t += 10
    g.step(st(POWERCUT_STATE, 330, special=1), t)  # adopted
    # in powercut, hot: hold
    t += 600
    assert g.state() == "engaged"
    # dropping below exit_temp starts the cold timer
    t += 60
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    assert g.state() == "fire-out"
    # brief warm blip resets the cold timer
    t += 300
    g.step(st(POWERCUT_STATE, 70, special=1), t)
    assert g.state() == "smoldering"
    t += 60
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    # 10 min cold -> exit (and no restore in the same step: the controller
    # is rebooting)
    t += 600
    assert g.step(st(POWERCUT_STATE, 40, special=1), t) == ["exit"]
    assert g.state() == "restoring"
    # first restore attempt only after one restore interval
    t += 299
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == []
    t += 1
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == ["restore"]
    # restore retried while the mode has not stuck (reboot reset it to 0)
    t += 300
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == ["restore"]
    # once the stove holds the original mode, the guard is done
    t += 1
    assert g.step(st(SLEEP, 30, mode=1, level=3, setp=23.0), t) == []
    assert g.original is None
    assert g.state() == "idle"


def test_exit_via_sleep_state():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=0, level=3), t)  # engage
    t += 10
    g.step(st(POWERCUT_STATE, 300, special=1), t)  # adopted
    # the fire burned out and the state machine fell to SLEEP
    t += 60
    assert g.step(st(SLEEP, 80, mode=0, level=3), t) == ["exit"]
    assert g.state() == "restoring"
    assert g.original == (0, 3, None, None)
    # the reboot reset the mode to Automatic (which happens to equal the
    # original mode here), so the guard still performs one restore before
    # trusting the mode match
    t += 300
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == ["restore"]
    t += 1
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == []
    assert g.original is None


def test_no_restore_past_deadline():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=0, level=3), t)  # engage
    t += 120
    g.step(st(POWERCUT_STATE, 50, special=1), t)  # adopted
    t += 600
    g.step(st(POWERCUT_STATE, 40, special=1), t)  # exit
    t += 4 * 3600 + 600  # past the restore deadline
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == []
    assert g.original is not None  # remembered, but no more writes


# -- guard: adopts out-of-band powercut --------------------------------------


def test_adopts_external_powercut():
    g = _guard()
    t = 0.0
    # someone ran `openwood rescue` (or the app's emergency button)
    assert g.step(st(POWERCUT_STATE, 300, special=1, mode=1, level=3,
                     setp=23.0, ext=True), t) == []
    assert g.state() == "engaged"
    assert g.original == (1, 3, 23.0, True)
    # the guard can now exit it when the fire is out
    t += 60
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    t += 600
    assert g.step(st(POWERCUT_STATE, 40, special=1), t) == ["exit"]


def test_engage_retry_after_pending_window():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)
    t += 60
    assert g.step(st(LIGHTING_E, 330, mode=0, level=3), t) == ["engage"]
    # the write may not have landed (stove still in LIGHTING E): no second
    # engage within the 120 s pending window...
    t += 60
    assert g.step(st(LIGHTING_E, 330, mode=0, level=3), t) == []
    t += 50
    assert g.step(st(LIGHTING_E, 330, mode=0, level=3), t) == []
    # ...but retry once the window expires
    t += 10
    assert g.step(st(LIGHTING_E, 330, mode=0, level=3), t) == ["engage"]


# -- guard: restore skipped while in LIGHTING --------------------------------


def test_restore_skipped_in_lighting():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t)  # engage
    t += 120
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    t += 600
    g.step(st(POWERCUT_STATE, 40, special=1), t)  # exit -> controller reboots
    # the state machine restarted in LIGHTING E, but the box is cold:
    # mode writes are reverted there -> no restore yet
    t += 300
    assert g.step(st(LIGHTING_E, 40, mode=0, level=3), t) == []
    # the stove cools to SLEEP: restore now sticks
    t += 300
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == ["restore"]


def test_reengage_after_exit_restart():
    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=0, level=3), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=0, level=3), t)  # engage
    t += 120
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    t += 600
    g.step(st(POWERCUT_STATE, 40, special=1), t)  # exit -> reboot
    # the fire was still in the box: the state machine restarted in
    # LIGHTING E and the box is hot -> the guard re-engages
    t += 300
    assert g.step(st(LIGHTING_E, 200, mode=0, level=3), t) == ["engage"]


def test_restore_mode_writes():
    import asyncio

    class FakeStove:
        def __init__(self):
            self.calls = []

        async def set_mode(self, mode, level, setp):
            self.calls.append(("mode", mode, level, setp))

        async def set_extended_burn(self, on):
            self.calls.append(("ext", on))

    g = _guard()
    t = 0.0
    g.step(st(LIGHTING_E, 350, mode=1, level=3, setp=23.0, ext=True), t)
    t += 60
    g.step(st(LIGHTING_E, 330, mode=1, level=3, setp=23.0, ext=True), t)  # engage
    t += 120
    g.step(st(POWERCUT_STATE, 50, special=1), t)
    t += 600
    g.step(st(POWERCUT_STATE, 40, special=1), t)  # exit
    t += 300
    assert g.step(st(SLEEP, 30, mode=0, level=3), t) == ["restore"]
    fake = FakeStove()
    asyncio.run(g.restore_mode(fake))
    assert fake.calls == [("mode", 1, 3, 23.0), ("ext", True)]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("PASS", name)
            except Exception as e:
                failures += 1
                print("FAIL", name, "->", repr(e))
    raise SystemExit(1 if failures else 0)
