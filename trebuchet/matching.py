"""Which board is this? Ranks candidates for one discovered device, best first.

Sources, strongest first:
  1. the board was identified in an earlier run (remembered by CAN UUID / USB serial);
  2. a saved profile with the same interface and the same MCU;
  3. a board from Klipper's own list whose pins match what printer.cfg uses for this MCU;
  4. boards from Klipper's list with the same MCU and the expected interface.

Everything here SUGGESTS. The user confirms (Enter accepts the first one). The MCU comes from
what the running firmware reports, not from the silicon, so it filters candidates but is never
presented as a verified fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import catalog, discover, pinmatch
from .profiles import BoardProfile
from .registry import Registry


@dataclass
class Suggestion:
    kind: str                                  # remembered | profile | catalog
    title: str
    why: str
    profile: Optional[BoardProfile] = None
    entry: Optional[catalog.CatalogEntry] = None
    score: float = 0.0

    def line(self) -> str:
        return f"{self.title}   ({self.why})"


def _chip_ok(core: str, chips) -> bool:
    return not core or core in {discover.chip_core(c) for c in chips}


def suggest(device: discover.Device, profiles: list, entries: list, registry: Registry,
            printer_cfg: Optional[Path] = None, limit_catalog: int = 4) -> list:
    out: list[Suggestion] = []
    core = discover.chip_core(device.chip)
    want = discover.profile_interface_for(device)
    by_id = {p.id: p for p in profiles}
    used_profiles: set = set()
    used_entries: set = set()

    # 1. remembered
    key = Registry.board_key(device.uuid, device.serial)
    known = registry.recall_board(key)
    p = by_id.get(known.get("profile", ""))
    if p is not None:
        out.append(Suggestion("remembered", p.name, "recognised from last time", profile=p, score=2.0))
        used_profiles.add(p.id)

    # 2. saved profiles that fit
    for p in profiles:
        if p.id in used_profiles or p.interface != want:
            continue
        if core and discover.chip_core(p.mcu) and discover.chip_core(p.mcu) != core:
            continue
        why = "saved profile, same MCU" if core and discover.chip_core(p.mcu) == core else "saved profile"
        out.append(Suggestion("profile", p.name, why, profile=p, score=1.0))
        used_profiles.add(p.id)

    # 3. catalog by pins
    saved_catalog_ids = {p.catalog_id for p in profiles if p.catalog_id}
    if printer_cfg:
        for m in pinmatch.suggest(printer_cfg, device.name, entries, chip_hint=device.chip, top=limit_catalog):
            if m.entry.id in saved_catalog_ids:
                continue
            out.append(Suggestion("catalog", m.entry.name,
                                  f"Klipper's list, {m.score * 100:.0f}% of the pins match",
                                  entry=m.entry, score=m.score))
            used_entries.add(m.entry.id)

    # 4. catalog by MCU, only when pins gave nothing
    if not used_entries and core:
        fallback = [e for e in entries if e.family in ("stm32", "rp2040") and e.id not in saved_catalog_ids
                    and _chip_ok(core, e.chips) and core in {discover.chip_core(c) for c in e.chips}]
        fallback.sort(key=lambda e: (e.interface_hint != ("can" if "can" in want else "usb"), e.name))
        for e in fallback[:limit_catalog]:
            out.append(Suggestion("catalog", e.name, "Klipper's list, same MCU", entry=e, score=0.1))
    return out
