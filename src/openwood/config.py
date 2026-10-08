"""Configuration handling: known stoves and the default stove."""

# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Benoit Brummer

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from bleak.backends.device import BLEDevice

from .client import Stove

_PKG_DIR = Path(__file__).resolve().parent
_resolved: Path | None = None


def _candidates() -> list[Path]:
    paths = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        paths.append(Path(xdg) / "openwood" / "config.json")
    paths.append(Path.home() / ".config" / "openwood" / "config.json")
    paths.append(Path("/etc/openwood/config.json"))
    paths.append(_PKG_DIR / "config.json")
    return paths


def config_path() -> Path:
    """First existing config file, else the first writable location.

    $OPENWOOD_CONFIG is authoritative even if the file does not exist
    yet (first run: it gets created on first save).
    """
    global _resolved
    if _resolved is not None:
        return _resolved
    env = os.environ.get("OPENWOOD_CONFIG")
    if env:
        _resolved = Path(env)
        return _resolved
    for p in _candidates():
        if p.is_file():
            _resolved = p
            return p
    for p in _candidates():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            probe = p.parent / ".openwood-write-test"
            probe.touch()
            probe.unlink()
        except OSError:
            continue
        if p.parent == _PKG_DIR:
            print(
                f"# no writable user config dir; storing config in {p}",
                file=sys.stderr,
            )
        _resolved = p
        return p
    raise OSError("no writable location for openwood config (set OPENWOOD_CONFIG)")


def load() -> dict:
    try:
        return json.loads(config_path().read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def save(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n")


def remember_seen(devices: list[BLEDevice]) -> None:
    """Merge names of scanned stoves into the cache so `use` can list them."""
    cfg = load()
    seen = cfg.get("seen") or {}
    seen.update({d.address: d.name or "Charnwood" for d in devices})
    cfg["seen"] = seen
    try:
        save(cfg)
    except OSError:
        pass


def set_default(address: str, name: str | None = None) -> None:
    cfg = load()
    cfg["default_address"] = address
    if name:
        cfg.setdefault("names", {})[address] = name
    save(cfg)


def auto_save_default(address: str, name: str | None = None) -> bool:
    """Best-effort: remember a discovered stove as the default.

    Returns False (with a warning on stderr) when the config is not
    writable, e.g. $HOME is read-only and no writable fallback exists.
    """
    try:
        set_default(address, name)
        return True
    except OSError as e:
        print(
            f"# could not save the default stove ({e});\n"
            "# every command will re-scan. Set a default with:\n"
            "#   openwood use",
            file=sys.stderr,
        )
        return False


def get_default() -> tuple[str | None, str | None]:
    cfg = load()
    addr = cfg.get("default_address")
    name = (cfg.get("names") or {}).get(addr) if addr else None
    return addr, name


async def resolve_address(explicit: str | None, scan_timeout: float = 15.0) -> str:
    """Pick a stove: explicit arg > configured default > the only one found."""
    if explicit:
        return explicit
    addr, name = get_default()
    if addr:
        print(f"# using {name or 'stove'} at {addr} (default; see `openwood use`)",
              file=sys.stderr)
        return addr
    devices = await Stove.scan(timeout=scan_timeout)
    if len(devices) == 1:
        try:
            remember_seen(devices)
        except OSError:
            pass
        print(f"# using discovered stove {devices[0].name} at {devices[0].address}",
              file=sys.stderr)
        if auto_save_default(devices[0].address, devices[0].name):
            print("# remembered as default (undo with: openwood forget "
                  f"{devices[0].address})", file=sys.stderr)
        return devices[0].address
    if not devices:
        print(
            "error: no stove found in range and no default configured.\n"
            "Bring the stove online, then:  openwood scan && openwood use <MAC>",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print("error: multiple stoves in range; pick one with `openwood use <MAC>`:",
          file=sys.stderr)
    for d in devices:
        print(f"    {d.address}  {d.name}", file=sys.stderr)
    raise SystemExit(2)
