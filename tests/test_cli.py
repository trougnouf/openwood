# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer
"""Password-resolution tests. Run: python -m tests.test_cli"""
import getpass
import os
from types import SimpleNamespace

from openwood import cli


class FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _patch_stdin(tty):
    orig = cli.sys.stdin
    cli.sys.stdin = FakeStdin(tty)
    return orig


def _restore_stdin(orig):
    cli.sys.stdin = orig


def _env(**kwargs):
    orig = {k: os.environ.get(k) for k in ("MQTT_PASSWORD",)}
    os.environ.pop("MQTT_PASSWORD", None)
    os.environ.update(kwargs)
    return orig


def _restore_env(orig):
    os.environ.pop("MQTT_PASSWORD", None)
    if orig["MQTT_PASSWORD"] is not None:
        os.environ["MQTT_PASSWORD"] = orig["MQTT_PASSWORD"]


def test_mqtt_password_flag_wins():
    args = SimpleNamespace(password="flag-pw", user="openwood")
    orig_env = _env(MQTT_PASSWORD="env-pw")
    orig_in = _patch_stdin(False)
    try:
        assert cli.mqtt_password(args) == "flag-pw"
    finally:
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_mqtt_password_from_env():
    args = SimpleNamespace(password=None, user="openwood")
    orig_env = _env(MQTT_PASSWORD="env-pw")
    orig_in = _patch_stdin(False)
    try:
        assert cli.mqtt_password(args) == "env-pw"
    finally:
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_mqtt_password_empty_env_is_none():
    # the service env-file can carry an empty MQTT_PASSWORD; that must not
    # count as a password (the caller then fails loudly / prompts)
    args = SimpleNamespace(password=None, user="openwood")
    orig_env = _env(MQTT_PASSWORD="")
    orig_in = _patch_stdin(False)
    try:
        assert cli.mqtt_password(args) is None
    finally:
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_mqtt_password_anonymous_needs_none():
    # no --user: anonymous broker, no password required or prompted
    args = SimpleNamespace(password=None, user=None)
    orig_env = _env()
    orig_in = _patch_stdin(True)
    try:
        assert cli.mqtt_password(args) is None
    finally:
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_mqtt_password_no_tty_no_env():
    # service context: no flag, no env, no terminal -> None (caller errors)
    args = SimpleNamespace(password=None, user="openwood")
    orig_env = _env()
    orig_in = _patch_stdin(False)
    try:
        assert cli.mqtt_password(args) is None
    finally:
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_mqtt_password_prompt():
    args = SimpleNamespace(password=None, user="openwood")
    orig_env = _env()
    orig_in = _patch_stdin(True)
    orig_getpass = cli.getpass.getpass
    cli.getpass.getpass = lambda prompt="": "typed-pw"
    try:
        assert cli.mqtt_password(args) == "typed-pw"
    finally:
        cli.getpass.getpass = orig_getpass
        _restore_env(orig_env)
        _restore_stdin(orig_in)


def test_wifi_prompt_match():
    typed = ["secret", "secret"]
    cli.getpass.getpass = lambda prompt="": typed.pop(0)
    try:
        assert cli.prompt_wifi_password("MyNet") == "secret"
    finally:
        cli.getpass.getpass = getpass.getpass


def test_wifi_prompt_mismatch():
    typed = ["secret", "typo"]
    cli.getpass.getpass = lambda prompt="": typed.pop(0)
    try:
        assert cli.prompt_wifi_password("MyNet") is None
    finally:
        cli.getpass.getpass = getpass.getpass


def test_wifi_prompt_empty():
    cli.getpass.getpass = lambda prompt="": ""
    try:
        assert cli.prompt_wifi_password("MyNet") is None
    finally:
        cli.getpass.getpass = getpass.getpass


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
