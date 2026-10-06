"""Perfis de board e definição de máquinas (ficheiros TOML).

Perfil  = o que é a board (MCU, interface, como grava o 1.º Katapult) + os
          ficheiros .config do Klipper e do Katapult que JÁ funcionam.
Máquina = quais boards existem num setup e como identificá-las (UUID/serial).

Layout em disco:
    <data>/profiles/<id>/profile.toml
    <data>/profiles/<id>/klipper.config
    <data>/profiles/<id>/katapult.config      (opcional)
    <data>/machines/<nome>.toml
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import tomlmini
from .tomlmini import TOMLDecodeError

# Âmbito da v1: só o que a documentação do Katapult cobre e que escolhemos suportar.
SUPPORTED_FAMILIES = ("stm32", "rp2040")
FAMILIES = SUPPORTED_FAMILIES + ("other",)
INTERFACES = ("usb", "can", "usb-can-bridge", "uart")
FIRST_KATAPULT_METHODS = ("dfu", "bootsel", "stm32flash", "stlink", "deployer", "other")
KATAPULT_STATES = ("yes", "no", "unknown")

_SLUG = re.compile(r"[^a-z0-9._-]+")


def slugify(text: str) -> str:
    s = _SLUG.sub("-", text.strip().lower()).strip("-")
    return s or "sem-nome"


# --- TOML (leitura com tomllib ou, em Python < 3.11, tomlmini; escrita mínima) -------------------------------
def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def dump_toml(data: dict, array_key: str | None = None) -> str:
    """Escreve um dict plano e, opcionalmente, uma lista de dicts em [[array_key]]."""
    lines: list[str] = []
    for k, v in data.items():
        if k == array_key or v is None:
            continue
        lines.append(f"{k} = {_toml_value(v)}")
    if array_key:
        for item in data.get(array_key, []):
            lines.append("")
            lines.append(f"[[{array_key}]]")
            for k, v in item.items():
                if v is None or v == "":
                    continue
                lines.append(f"{k} = {_toml_value(v)}")
    return "\n".join(lines) + "\n"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomlmini.load(fh)


# --- Perfil -------------------------------------------------------------------
@dataclass
class BoardProfile:
    id: str
    name: str
    mcu: str                      # texto livre, confirmado pelo utilizador (ex.: stm32f446)
    family: str                   # stm32 | rp2040 | other
    interface: str                # usb | can | usb-can-bridge | uart
    first_katapult_method: str    # dfu | bootsel | stm32flash | stlink | deployer | other
    can_bitrate: int | None = None
    uart_baud: int | None = None
    dfu_heater_warning: bool = True
    notes: str = ""
    dir: Path = field(default=Path("."), repr=False)

    @property
    def klipper_config(self) -> Path:
        return self.dir / "klipper.config"

    @property
    def katapult_config(self) -> Path:
        return self.dir / "katapult.config"

    @property
    def is_can(self) -> bool:
        return self.interface in ("can", "usb-can-bridge")

    @property
    def is_bridge(self) -> bool:
        return self.interface == "usb-can-bridge"

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.family not in FAMILIES:
            out.append(f"família inválida: {self.family!r}")
        if self.interface not in INTERFACES:
            out.append(f"interface inválida: {self.interface!r}")
        if self.first_katapult_method not in FIRST_KATAPULT_METHODS:
            out.append(f"método do 1.º Katapult inválido: {self.first_katapult_method!r}")
        if self.is_can and not self.can_bitrate:
            out.append("interface CAN sem can_bitrate")
        if not self.klipper_config.exists():
            out.append(f"falta {self.klipper_config.name} no perfil")
        return out

    def to_dict(self) -> dict:
        return {
            "name": self.name, "mcu": self.mcu, "family": self.family,
            "interface": self.interface,
            "first_katapult_method": self.first_katapult_method,
            "can_bitrate": self.can_bitrate, "uart_baud": self.uart_baud,
            "dfu_heater_warning": self.dfu_heater_warning, "notes": self.notes,
        }


def load_profile(profile_dir: Path) -> BoardProfile:
    data = _load_toml(profile_dir / "profile.toml")
    return BoardProfile(
        id=profile_dir.name,
        name=data.get("name", profile_dir.name),
        mcu=data.get("mcu", ""),
        family=data.get("family", "other"),
        interface=data.get("interface", "usb"),
        first_katapult_method=data.get("first_katapult_method", "other"),
        can_bitrate=data.get("can_bitrate"),
        uart_baud=data.get("uart_baud"),
        dfu_heater_warning=data.get("dfu_heater_warning", True),
        notes=data.get("notes", ""),
        dir=profile_dir,
    )


def list_profiles(profiles_dir: Path) -> list[BoardProfile]:
    if not profiles_dir.exists():
        return []
    out = []
    for d in sorted(profiles_dir.iterdir()):
        if (d / "profile.toml").exists():
            try:
                out.append(load_profile(d))
            except (TOMLDecodeError, OSError):
                continue
    return out


def save_profile(profiles_dir: Path, profile: BoardProfile) -> Path:
    d = profiles_dir / profile.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.toml").write_text(dump_toml(profile.to_dict()), encoding="utf-8")
    profile.dir = d
    return d


# --- Máquina ------------------------------------------------------------------
@dataclass
class MachineBoard:
    label: str                    # nome curto na máquina: main, toolhead, ...
    profile_id: str
    uuid: str = ""                # canbus_uuid (12 hex) para nós CAN / bridge
    serial: str = ""              # serial USB (parte final do nome em by-id)
    device: str = ""              # caminho explícito (obrigatório em UART)

    def to_dict(self) -> dict:
        return {"label": self.label, "profile": self.profile_id, "uuid": self.uuid,
                "serial": self.serial, "device": self.device}


@dataclass
class Machine:
    name: str
    can_interface: str = "can0"
    boards: list[MachineBoard] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    def board(self, label: str) -> MachineBoard | None:
        for b in self.boards:
            if b.label == label:
                return b
        return None

    def to_dict(self) -> dict:
        return {"name": self.name, "can_interface": self.can_interface,
                "board": [b.to_dict() for b in self.boards]}


def load_machine(path: Path) -> Machine:
    data = _load_toml(path)
    boards = [
        MachineBoard(
            label=b["label"], profile_id=b.get("profile", ""),
            uuid=b.get("uuid", "").lower(), serial=b.get("serial", ""),
            device=b.get("device", ""),
        )
        for b in data.get("board", [])
    ]
    return Machine(name=data.get("name", path.stem),
                   can_interface=data.get("can_interface", "can0"), boards=boards)


def list_machines(machines_dir: Path) -> list[Machine]:
    if not machines_dir.exists():
        return []
    out = []
    for p in sorted(machines_dir.glob("*.toml")):
        try:
            out.append(load_machine(p))
        except (TOMLDecodeError, OSError, KeyError):
            continue
    return out


def save_machine(machines_dir: Path, machine: Machine) -> Path:
    machines_dir.mkdir(parents=True, exist_ok=True)
    path = machines_dir / f"{machine.slug}.toml"
    path.write_text(dump_toml(machine.to_dict(), array_key="board"), encoding="utf-8")
    return path
