"""Catalog of known boards, read from Klipper's own config files.

Each Klipper `config/generic-*.cfg` and `config/sample-*.cfg` starts with a comment
saying which MCU to compile for and with which bootloader/crystal (e.g. "STM32H723, 128KiB
bootloader, 25Mhz crystal"). We read that comment instead of keeping our own list: it always
matches your Klipper version and we don't make up data.

Limits (to acknowledge, not hide):
  - only boards that Klipper documents are listed: the newest on the market are missing;
  - the comment is free text. We extract the chip with a regular expression and show the
    full text; we don't try to turn it into a .config automatically;
  - the chip (and the text) come from Klipper, not the manufacturer. For a missing board,
    create the profile by hand, or add it to `<data>/catalog.toml`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import tomlmini

_CHIP = re.compile(r"\b(STM32[A-Z]\d[A-Z0-9]{2}|RP2040|LPC\d{4}|SAM[A-Z0-9]{4,}|AT32F\d+|"
                   r"AT90USB\d+|ATMEGA\d+[A-Z]*|HC32F\d+[A-Z0-9]*)\b", re.I)
_ACRONYMS = {"ebb", "skr", "gtr", "ez", "mz", "dip", "rrf", "hv", "cdy", "sb", "pro", "sbc"}
_LOWER_KEEP = {"pro": "Pro"}

VENDORS = (("bigtreetech", "BigTreeTech"), ("mellow", "Mellow (FLY)"), ("fysetc", "FYSETC"))
OTHER = "other"


def _vendor(v) -> str:
    v = str(v).lower()
    return OTHER if v == "outras" else v     # "outras" = old spelling, kept so old files still load

_VENDOR_BY_TOKEN = {"bigtreetech": "bigtreetech", "mellow": "mellow", "fly": "mellow",
                    "fysetc": "fysetc"}
_PRETTY_VENDOR = {"bigtreetech": "BigTreeTech", "mellow": "Mellow", "fysetc": "FYSETC"}


@dataclass
class CatalogEntry:
    id: str
    vendor: str            # bigtreetech | mellow | fysetc | other
    name: str
    chip: str              # lowercase; "" if the text doesn't say OR mentions several (see chips)
    family: str            # stm32 | rp2040 | other
    interface_hint: str    # usb | can (guess from the file name; confirm)
    notes: str             # Klipper comment, without the "See docs/..." line
    source: str            # source file
    chips: tuple = ()      # all chips the text mentions (the board may come in variants)


def _header(path: Path) -> list[str]:
    """Leading comment of the file, up to the 'See docs/...' line or the first content."""
    out: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if text.startswith("See docs/"):
                break
            out.append(text)
        elif not line.strip():
            if out and out[-1] != "":
                out.append("")
        else:
            break
    while out and out[-1] == "":
        out.pop()
    return out


def pretty_name(stem: str) -> str:
    """generic-bigtreetech-octopus-pro-v1.1 -> BigTreeTech Octopus PRO v1.1 (from the file name only,
    which is unique; Klipper's text is shown separately)."""
    words = []
    for tok in re.sub(r"^(generic|sample)-", "", stem).split("-"):
        if tok in _PRETTY_VENDOR:
            words.append(_PRETTY_VENDOR[tok])
        elif re.fullmatch(r"v\d[\w.]*", tok):
            words.append(tok)
        elif tok in _ACRONYMS or re.search(r"\d", tok):
            words.append(tok.upper())
        else:
            words.append(tok.capitalize())
    return " ".join(words)


def _vendor(stem: str) -> str:
    for token in stem.split("-"):
        if token in _VENDOR_BY_TOKEN:
            return _VENDOR_BY_TOKEN[token]
    return OTHER


def _family(chip: str) -> str:
    chip = chip.lower()
    if chip.startswith("stm32"):
        return "stm32"
    if chip.startswith("rp2040"):
        return "rp2040"
    return "other"


def parse_entry(path: Path) -> Optional[CatalogEntry]:
    lines = _header(path)
    text = " ".join(l for l in lines if l)
    m = _CHIP.search(text)
    if not m:
        return None                      # config examples, not boards
    chips = tuple(sorted({c.lower() for c in _CHIP.findall(text)}))
    chip = chips[0] if len(chips) == 1 else ""
    stem = path.stem
    return CatalogEntry(
        id=stem, vendor=_vendor(stem), name=pretty_name(stem), chip=chip, chips=chips,
        family=_family(chip or chips[0]),
        interface_hint="can" if "canbus" in stem else "usb",
        notes="\n".join(lines), source=str(path))


def load_klipper_catalog(klipper_dir: Path) -> list[CatalogEntry]:
    cfg = klipper_dir / "config"
    if not cfg.is_dir():
        return []
    out = []
    for p in sorted(list(cfg.glob("generic-*.cfg")) + list(cfg.glob("sample-*.cfg"))):
        e = parse_entry(p)
        if e:
            out.append(e)
    return out


def load_user_catalog(path: Path) -> list[CatalogEntry]:
    """<data>/catalog.toml: boards that Klipper doesn't ship. Format:

        [[board]]
        id = "fly-sb2040"
        vendor = "mellow"        # bigtreetech | mellow | fysetc | other
        name = "Mellow Fly-SB2040"
        chip = "rp2040"
        interface = "can"
        notes = "free text"    # optional
        source = "https://..."   # optional: where you got the data from
    """
    if not path.is_file():
        return []
    try:
        with path.open("rb") as fh:
            data = tomlmini.load(fh)
    except (tomlmini.TOMLDecodeError, OSError):
        return []
    out = []
    for b in data.get("board", []):
        if not b.get("id") or not b.get("name"):
            continue
        chip = str(b.get("chip", "")).lower()
        out.append(CatalogEntry(
            id=str(b["id"]), vendor=_vendor(b.get("vendor", OTHER)), name=str(b["name"]),
            chip=chip, chips=(chip,) if chip else (), family=_family(chip), interface_hint=str(b.get("interface", "usb")),
            notes=str(b.get("notes", "")), source=str(b.get("source", "catalog.toml"))))
    return out


def load_catalog(klipper_dir: Path, user_file: Path) -> list[CatalogEntry]:
    """User boards first (they override Klipper's with the same id)."""
    user = load_user_catalog(user_file)
    ids = {e.id for e in user}
    return user + [e for e in load_klipper_catalog(klipper_dir) if e.id not in ids]
