"""Suggest which board this is, by comparing the pins in printer.cfg with the pins of each catalog board.

Idea: Klipper's `config/generic-*.cfg` files declare each board's pins. If the user's
printer.cfg uses pins of an MCU, we look for catalog boards that have those pins.

Limits (to show the user, not hide):
  - this SUGGESTS, never decides: revisions with the same pins (v1.0/v1.1) are indistinguishable;
  - "bigger" boards contain the pins of smaller ones in the same family (Octopus Pro vs Octopus);
  - if printer.cfg uses aliases (`[board_pins]`) or few pins, there is little information;
  - only covers boards with a Klipper file and STM32/RP2040 MCUs (pins PA0..PK15, gpioN).
When the chip is known it narrows the candidates a lot: it is used to filter before scoring.
"""
from __future__ import annotations

import glob
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import catalog, discover

_PIN = re.compile(r"(?<![\w])[!^~<>=]*(?:(?P<chip>[A-Za-z_]\w*):)?(?P<pin>P[A-K]\d{1,2}|gpio\d{1,2})\b")
MIN_PINS = 4          # with fewer pins than this the comparison is worthless


@dataclass
class Match:
    entry: catalog.CatalogEntry
    score: float            # 0..1, weighted: rare pins count more
    shared: int             # user pins that the board also has
    total: int              # user pins used in the comparison


def _active_text(path: Path) -> str:
    out = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.lstrip().startswith(("#", ";")):
            continue
        out.append(re.split(r"\s[#;]", line, maxsplit=1)[0])
    return "\n".join(out)


def pins_in_text(text: str) -> dict[str, set]:
    """{mcu name ('' = the main one): {pins}}. The `mcu:` prefix decides which MCU a pin belongs to."""
    out: dict[str, set] = {}
    for m in _PIN.finditer(text):
        out.setdefault(m.group("chip") or "", set()).add(m.group("pin").upper()
                                                         if m.group("pin")[0] == "P" else m.group("pin"))
    return out


def user_pins(printer_cfg: Path) -> dict[str, set]:
    """Pins used in printer.cfg and its [include]s, per MCU ('mcu' = the main one)."""
    texts: list[str] = []
    seen: set = set()

    def walk(p: Path) -> None:
        try:
            real = p.resolve()
        except OSError:
            return
        if real in seen or not p.is_file():
            return
        seen.add(real)
        txt = _active_text(p)
        texts.append(txt)
        for m in re.finditer(r"^\[include\s+(.+?)\]\s*$", txt, re.M | re.I):
            pattern = m.group(1).strip()
            target = pattern if pattern.startswith("/") else str(p.parent / pattern)
            for f in sorted(glob.glob(target)):
                walk(Path(f))

    walk(printer_cfg)
    raw = pins_in_text("\n".join(texts))
    result = {("mcu" if k == "" else k): v for k, v in raw.items()}
    return result


def board_pins(entry: catalog.CatalogEntry) -> set:
    path = Path(entry.source)
    if not path.is_file():
        return set()
    merged: set = set()
    for pins in pins_in_text(_active_text(path)).values():
        merged |= pins
    return merged


def rank(pins: set, entries: list, *, chip_hint: str = "", top: int = 3) -> list:
    """Score the catalog boards. Each pin is worth more the fewer boards have it (IDF)."""
    if len(pins) < MIN_PINS:
        return []
    core = discover.chip_core(chip_hint)
    pool = []
    for e in entries:
        if e.family not in ("stm32", "rp2040"):
            continue
        if core and core not in {discover.chip_core(c) for c in e.chips}:
            continue
        bp = board_pins(e)
        if bp:
            pool.append((e, bp))
    if not pool:
        return []
    df: dict[str, int] = {}
    for _, bp in pool:
        for p in bp:
            df[p] = df.get(p, 0) + 1
    n = len(pool)

    def w(p: str) -> float:
        return math.log(1 + n / df.get(p, 1)) if p in df else 1.0   # pin no board has: it counts, but only in the denominator

    total_w = sum(w(p) for p in pins)
    results = []
    for e, bp in pool:
        shared = pins & bp
        score = sum(w(p) for p in shared) / total_w if total_w else 0.0
        results.append(Match(e, round(score, 3), len(shared), len(pins)))
    # score tie: the board with fewer pins in total is the tighter fit for what is used
    results.sort(key=lambda m: (-m.score, len(board_pins(m.entry))))
    return [m for m in results[:top] if m.score > 0]


def suggest(printer_cfg: Optional[Path], mcu_name: str, entries: list, *, chip_hint: str = "",
            top: int = 3) -> list:
    if not printer_cfg:
        return []
    pins = user_pins(printer_cfg).get(mcu_name, set())
    return rank(pins, entries, chip_hint=chip_hint, top=top)
