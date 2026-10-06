"""Sugere que board é, comparando os pins do printer.cfg com os pins de cada board do catálogo.

Ideia: os ficheiros `config/generic-*.cfg` do Klipper declaram os pins de cada board. Se o
printer.cfg do utilizador usa pins de um MCU, vemos que boards do catálogo têm esses pins.

Limites (a mostrar ao utilizador, não a esconder):
  - isto SUGERE, nunca decide: revisões com os mesmos pins (v1.0/v1.1) são indistinguíveis;
  - boards "maiores" contêm os pins das mais pequenas da mesma família (Octopus Pro vs Octopus);
  - se o printer.cfg usa aliases (`[board_pins]`) ou poucos pins, há pouca informação;
  - só cobre boards com ficheiro no Klipper e MCUs STM32/RP2040 (pins PA0..PK15, gpioN).
O chip, quando se conhece, reduz muito os candidatos: usa-se para filtrar antes de pontuar.
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
MIN_PINS = 4          # com menos pins do que isto a comparação não vale nada


@dataclass
class Match:
    entry: catalog.CatalogEntry
    score: float            # 0..1, ponderado: pins raros pesam mais
    shared: int             # pins do utilizador que a board também tem
    total: int              # pins do utilizador usados na comparação


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
    """{nome do mcu ('' = o principal): {pins}}. O prefixo `mcu:` decide a que MCU pertence."""
    out: dict[str, set] = {}
    for m in _PIN.finditer(text):
        out.setdefault(m.group("chip") or "", set()).add(m.group("pin").upper()
                                                         if m.group("pin")[0] == "P" else m.group("pin"))
    return out


def user_pins(printer_cfg: Path) -> dict[str, set]:
    """Pins usados no printer.cfg e nos seus [include], por MCU ('mcu' = o principal)."""
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
    """Pontua as boards do catálogo. Cada pin vale mais quanto menos boards o têm (IDF)."""
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
        return math.log(1 + n / df.get(p, 1)) if p in df else 1.0   # pin que nenhuma board tem: pesa, mas só no denominador

    total_w = sum(w(p) for p in pins)
    results = []
    for e, bp in pool:
        shared = pins & bp
        score = sum(w(p) for p in shared) / total_w if total_w else 0.0
        results.append(Match(e, round(score, 3), len(shared), len(pins)))
    # empate de pontuação: a board com menos pins no total é a mais "justa" ao que se usa
    results.sort(key=lambda m: (-m.score, len(board_pins(m.entry))))
    return [m for m in results[:top] if m.score > 0]


def suggest(printer_cfg: Optional[Path], mcu_name: str, entries: list, *, chip_hint: str = "",
            top: int = 3) -> list:
    if not printer_cfg:
        return []
    pins = user_pins(printer_cfg).get(mcu_name, set())
    return rank(pins, entries, chip_hint=chip_hint, top=top)
