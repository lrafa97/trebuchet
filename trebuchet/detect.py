"""Deteção de dispositivos: USB (by-id + lsusb), nós CAN e estado do can0.

As funções `parse_*` são puras (recebem texto) para poderem ser testadas sem
hardware. As restantes chamam ferramentas do sistema através do Runner.

Origem dos identificadores:
  - 1d50:614e (Klipper) e 1d50:6177 (Katapult): constantes do flashtool.py do
    Katapult. O flashtool também usa o campo "manufacturer" do USB, porque o
    Katapult permite alterar VID/PID no menuconfig.
  - 0483:df11 (DFU de ROM STM32) e 2e8a:0003 (BOOTSEL RP2040): conhecimento
    geral, não retirado dos documentos lidos. Confirmar com `lsusb` numa board real.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings
from .runner import Runner

KLIPPER_USB_ID = "1d50:614e"
KATAPULT_USB_ID = "1d50:6177"
STM32_DFU_ID = "0483:df11"
RP2040_BOOTSEL_ID = "2e8a:0003"

BY_ID_DIR = Path("/dev/serial/by-id")

_BY_ID = re.compile(r"^usb-(?P<vendor>[^_]+)_(?P<mcu>.+)_(?P<serial>[^_]+?)-if\d+$")
_LSUSB = re.compile(r"ID\s+(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})\s*(?P<desc>.*)$")
_CANBUS_QUERY = re.compile(r"Found canbus_uuid=(?P<uuid>[0-9a-fA-F]{12}),\s*Application:\s*(?P<app>\S+)")
_FLASHTOOL_Q = re.compile(r"Detected UUID:\s*(?P<uuid>[0-9a-fA-F]{12}),\s*Application:\s*(?P<app>\S+)")


# --- tipos --------------------------------------------------------------------
@dataclass
class UsbSerial:
    path: str
    app: str        # klipper | katapult | other
    mcu: str
    serial: str


@dataclass
class UsbId:
    vidpid: str
    desc: str
    kind: str       # klipper | katapult | stm32-dfu | rp2040-bootsel | other


@dataclass
class UsbScan:
    serial: list[UsbSerial] = field(default_factory=list)
    ids: list[UsbId] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[UsbId]:
        return [i for i in self.ids if i.kind == kind]


@dataclass
class CanNode:
    uuid: str
    app: str            # texto original: Klipper, CanBoot, Katapult, Unknown
    @property
    def kind(self) -> str:
        a = self.app.lower()
        if a == "klipper":
            return "klipper"
        if a in ("canboot", "katapult"):
            return "katapult"
        return "unknown"


@dataclass
class CanQuery:
    nodes: list[CanNode]
    ok: bool
    output: str


@dataclass
class CanIface:
    exists: bool
    up: bool = False
    bitrate: int | None = None


# --- parsers ------------------------------------------------------------------
def parse_by_id_name(name: str) -> UsbSerial | None:
    m = _BY_ID.match(name)
    if not m:
        return None
    vendor = m.group("vendor").lower()
    app = vendor if vendor in ("klipper", "katapult") else ("katapult" if vendor == "canboot" else "other")
    return UsbSerial(path=str(BY_ID_DIR / name), app=app, mcu=m.group("mcu"), serial=m.group("serial"))


def classify_vidpid(vidpid: str) -> str:
    v = vidpid.lower()
    return {
        KLIPPER_USB_ID: "klipper",
        KATAPULT_USB_ID: "katapult",
        STM32_DFU_ID: "stm32-dfu",
        RP2040_BOOTSEL_ID: "rp2040-bootsel",
    }.get(v, "other")


def parse_lsusb(text: str) -> list[UsbId]:
    out: list[UsbId] = []
    for line in text.splitlines():
        m = _LSUSB.search(line)
        if m:
            vp = f"{m.group('vid').lower()}:{m.group('pid').lower()}"
            out.append(UsbId(vp, m.group("desc").strip(), classify_vidpid(vp)))
    return out


def parse_canbus_query(text: str) -> list[CanNode]:
    seen: dict[str, CanNode] = {}
    for m in _CANBUS_QUERY.finditer(text):
        seen[m.group("uuid").lower()] = CanNode(m.group("uuid").lower(), m.group("app"))
    return list(seen.values())


def parse_flashtool_query(text: str) -> list[CanNode]:
    seen: dict[str, CanNode] = {}
    for m in _FLASHTOOL_Q.finditer(text):
        seen[m.group("uuid").lower()] = CanNode(m.group("uuid").lower(), m.group("app"))
    return list(seen.values())


def parse_ip_link(text: str, ok: bool = True) -> CanIface:
    if not ok or not text.strip():
        return CanIface(False)
    flags = re.search(r"<([^>]*)>", text)
    up = bool(flags and "UP" in flags.group(1).split(","))
    br = re.search(r"bitrate\s+(\d+)", text)
    return CanIface(True, up, int(br.group(1)) if br else None)


# --- chamadas ao sistema --------------------------------------------------------
def list_by_id(by_id_dir: Path = BY_ID_DIR) -> list[UsbSerial]:
    if not by_id_dir.exists():
        return []
    out = []
    for p in sorted(by_id_dir.iterdir()):
        dev = parse_by_id_name(p.name)
        if dev:
            dev.path = str(p)
            out.append(dev)
    return out


def scan_usb(runner: Runner, by_id_dir: Path = BY_ID_DIR) -> UsbScan:
    res = runner.run(["lsusb"])
    ids = parse_lsusb(res.output) if res.ok else []
    return UsbScan(serial=list_by_id(by_id_dir), ids=ids)


def query_can_klipper(settings: Settings, runner: Runner, iface: str | None = None) -> CanQuery:
    """canbus_query.py do Klipper: só mostra nós ainda não reclamados por um host."""
    script = settings.canbus_query_script()
    if not script.exists():
        return CanQuery([], False, f"não encontro {script}")
    res = runner.run([settings.klippy_python(), script, iface or settings.can_interface],
                     timeout=20)
    return CanQuery(parse_canbus_query(res.output), res.ok, res.output)


def query_can_katapult(settings: Settings, runner: Runner, *, single_node_confirmed: bool,
                       iface: str | None = None) -> CanQuery:
    """flashtool.py -q. O Katapult avisa que só deve ser usado com UM nó na rede:
    com vários, podem surgir erros de transmissão e um nó entrar em 'bus off'.
    Por isso exige confirmação explícita."""
    if not single_node_confirmed:
        raise ValueError("flashtool -q só com um único nó CAN ligado (aviso do Katapult)")
    tool = settings.find_flashtool()
    if not tool:
        return CanQuery([], False, "não encontro o flashtool.py do Katapult")
    res = runner.run(["python3", tool, "-i", iface or settings.can_interface, "-q"], timeout=30)
    return CanQuery(parse_flashtool_query(res.output), res.ok, res.output)


def can_status(runner: Runner, iface: str) -> CanIface:
    res = runner.run(["ip", "-details", "-o", "link", "show", iface])
    return parse_ip_link(res.output, res.ok)


# --- espera/poll ----------------------------------------------------------------
def wait_for(predicate: Callable[[], object], timeout: float, interval: float = 1.0,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.monotonic):
    """Chama `predicate` até devolver algo verdadeiro ou esgotar o tempo.
    Devolve o último valor (falso se esgotou)."""
    deadline = clock() + timeout
    value = predicate()
    while not value and clock() < deadline:
        sleep(interval)
        value = predicate()
    return value


# --- escolha de dispositivo ---------------------------------------------------------
def pick_usb_serial(scan: UsbScan, *, serial: str = "", apps: tuple[str, ...] = ("klipper", "katapult"),
                    mcu_hint: str = "") -> tuple[UsbSerial | None, list[UsbSerial]]:
    """Escolhe um dispositivo série. Devolve (escolhido, candidatos).
    Se `serial` estiver definido, só aceita esse. Sem serial, só escolhe quando há
    exatamente um candidato; com vários devolve None para o utilizador decidir."""
    cands = [d for d in scan.serial if d.app in apps]
    if serial:
        cands = [d for d in cands if d.serial.lower() == serial.lower()]
    elif mcu_hint:
        narrowed = [d for d in cands if mcu_hint.lower() in d.mcu.lower()]
        cands = narrowed or cands
    return (cands[0] if len(cands) == 1 else None), cands
