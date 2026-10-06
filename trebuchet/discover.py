"""Discover what the machine has: merges several sources into one device list.

Sources, from safest to most invasive:
  1. printer.cfg (and its [include]s): names, canbus_uuid, serial. File reads only.
  2. /dev/serial/by-id: USB boards connected right now, and the MCU (it is in the name).
  3. Moonraker (optional): chip and firmware version of each declared MCU, with Klipper running.
  4. can0: whether the adapter is a Klipper bridge (gs_usb driver) or another kind (e.g. MCP2515).
  5. canbus_query (optional, on request): CAN nodes that have no id assigned yet.

What CANNOT be known (and the program doesn't guess): the board model (several
models share an MCU) and whether a board already has Katapult. The chip is used to validate
the chosen profile, not to choose it on its own.

To verify on real hardware: the exact format of Moonraker's response (mcu_constants/MCU
fields) and whether CANBUS_BRIDGE shows up in the constants. The parser is tolerant:
if it doesn't find the field, the chip is left unknown.
"""
from __future__ import annotations

import glob
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import detect

BY_ID_PREFIX = "/dev/serial/by-id/"
_SECTION = re.compile(r"^\[(?P<name>[^\]]+)\]\s*$")
_KEY = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*(?P<val>.*)$")
_UUID = re.compile(r"^[0-9a-fA-F]{12}$")

# Core of the MCU name, to compare profile <-> chip (stm32f446xx == stm32f446).
_CORE = re.compile(r"(stm32[a-z]\d[a-z0-9]{2}|rp2040)")


# --- printer.cfg ------------------------------------------------------------------
@dataclass
class CfgMcu:
    name: str                 # "mcu" for the unnamed section, otherwise the suffix ([mcu toolhead] -> toolhead)
    serial: str = ""
    uuid: str = ""
    canbus_interface: str = ""
    source: str = ""          # file where it was declared


def find_printer_cfg(explicit: str = "", home: Optional[Path] = None) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    home = home or Path.home()
    for cand in (home / "printer_data" / "config" / "printer.cfg",
                 home / "klipper_config" / "printer.cfg"):
        if cand.exists():
            return cand
    return None


def _strip_inline_comment(val: str) -> str:
    return re.split(r"\s[#;]", val, maxsplit=1)[0].strip()


def parse_klipper_cfg(path: Path, _seen: Optional[set] = None) -> list[CfgMcu]:
    """Read the [mcu] and [mcu NAME] sections, following [include ...] (relative to the file,
    with globs). Does not validate the rest of the file."""
    seen = _seen if _seen is not None else set()
    try:
        real = path.resolve()
    except OSError:
        return []
    if real in seen or not path.is_file():
        return []
    seen.add(real)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    out: list[CfgMcu] = []
    current: Optional[CfgMcu] = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith(("#", ";")):
            continue
        if raw[0] in " \t":        # continuation of a multiline value
            continue
        m = _SECTION.match(raw.strip())
        if m:
            current = None
            name = m.group("name").strip()
            low = name.lower()
            if low.startswith("include "):
                pattern = name[8:].strip()
                target = pattern if pattern.startswith("/") else str(path.parent / pattern)
                for f in sorted(glob.glob(target)):
                    out.extend(parse_klipper_cfg(Path(f), seen))
            elif low == "mcu":
                current = CfgMcu(name="mcu", source=str(path))
                out.append(current)
            elif low.startswith("mcu "):
                current = CfgMcu(name=name[4:].strip(), source=str(path))
                out.append(current)
            continue
        if current is None:
            continue
        k = _KEY.match(raw)
        if not k:
            continue
        key, val = k.group("key").lower(), _strip_inline_comment(k.group("val"))
        if key == "serial":
            current.serial = val
        elif key == "canbus_uuid":
            current.uuid = val.lower()
        elif key == "canbus_interface":
            current.canbus_interface = val
    return out


# --- Moonraker --------------------------------------------------------------------
@dataclass
class McuInfo:
    chip: str = ""
    version: str = ""
    bridge: Optional[bool] = None


def parse_moonraker_mcus(text: str) -> dict[str, McuInfo]:
    """Response of /printer/objects/query?mcu&mcu%20name. Tolerant: missing fields are left empty."""
    try:
        status = json.loads(text)["result"]["status"]
    except (ValueError, KeyError, TypeError):
        return {}
    out: dict[str, McuInfo] = {}
    for obj, data in status.items():
        if not isinstance(data, dict) or not (obj == "mcu" or obj.startswith("mcu ")):
            continue
        name = "mcu" if obj == "mcu" else obj[4:].strip()
        consts = data.get("mcu_constants") if isinstance(data.get("mcu_constants"), dict) else {}
        chip = str(consts.get("MCU", "")) if consts else ""
        version = str(data.get("mcu_version", "") or "")
        bridge = True if "CANBUS_BRIDGE" in consts else None
        out[name] = McuInfo(chip=chip, version=version, bridge=bridge)
    return out


def moonraker_mcus(url: str, names: list[str], timeout: float = 3.0,
                   opener: Callable = urllib.request.urlopen) -> Optional[dict[str, McuInfo]]:
    """None if Moonraker doesn't respond (Klipper stopped, no Moonraker, wrong URL)."""
    objs = ["mcu" if n == "mcu" else f"mcu {n}" for n in names]
    if not objs:
        return {}
    query = "&".join(urllib.parse.quote(o, safe="") for o in objs)
    try:
        with opener(f"{url.rstrip('/')}/printer/objects/query?{query}", timeout=timeout) as resp:
            return parse_moonraker_mcus(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None


# --- can0 -------------------------------------------------------------------------
def can_driver(iface: str, sys_net: Path = Path("/sys/class/net")) -> Optional[str]:
    """Driver name of the interface (gs_usb on a Klipper bridge). None if it doesn't exist."""
    link = sys_net / iface / "device" / "driver"
    try:
        return link.resolve().name if link.exists() else ("" if (sys_net / iface).exists() else None)
    except OSError:
        return None


# --- model -----------------------------------------------------------------------
@dataclass
class Device:
    name: str
    transport: str                 # usb | can | uart
    uuid: str = ""
    serial: str = ""
    device: str = ""
    chip: str = ""
    version: str = ""
    present: Optional[bool] = None   # seen now; None = couldn't tell
    bridge: Optional[bool] = None    # None = not identified yet
    app: str = ""                    # klipper | katapult, when visible
    sources: list[str] = field(default_factory=list)


@dataclass
class Discovery:
    devices: list[Device] = field(default_factory=list)
    can_iface: str = "can0"
    can_driver: Optional[str] = None     # None = interface doesn't exist; "" = unknown driver
    printer_cfg: str = ""
    moonraker: Optional[bool] = None     # None = not asked
    notes: list[str] = field(default_factory=list)

    @property
    def has_bridge_adapter(self) -> bool:
        return self.can_driver == "gs_usb"

    @property
    def bridge_unresolved(self) -> bool:
        """There is a gs_usb adapter (Klipper bridge) but no node is marked as the bridge."""
        can = [d for d in self.devices if d.transport == "can"]
        return self.has_bridge_adapter and bool(can) and not any(d.bridge for d in can)


def chip_core(text: str) -> str:
    m = _CORE.search(text.lower())
    return m.group(1) if m else ""


def chip_matches(profile_mcu: str, chip: str) -> Optional[bool]:
    """True/False when both allow a comparison; None when they don't (doesn't block)."""
    a, b = chip_core(profile_mcu), chip_core(chip)
    if not a or not b:
        return None
    return a == b


def _is_host_mcu(c: CfgMcu) -> bool:
    return "klipper_host_mcu" in c.serial or c.name in ("rpi", "linux", "host")


def discover(settings, runner, *, printer_cfg: str = "", use_moonraker: bool = True,
             canbus_query: bool = False, by_id_dir: Path = detect.BY_ID_DIR,
             sys_net: Path = Path("/sys/class/net"),
             opener: Callable = urllib.request.urlopen) -> Discovery:
    d = Discovery(can_iface=settings.can_interface)
    devices: list[Device] = []

    # 1) printer.cfg
    cfg_path = find_printer_cfg(printer_cfg or getattr(settings, "printer_cfg", ""))
    if cfg_path:
        d.printer_cfg = str(cfg_path)
        for c in parse_klipper_cfg(cfg_path):
            if _is_host_mcu(c):
                continue
            if c.uuid and _UUID.match(c.uuid):
                devices.append(Device(c.name, "can", uuid=c.uuid, sources=["printer.cfg"]))
            elif c.serial.startswith(BY_ID_PREFIX):
                parsed = detect.parse_by_id_name(c.serial[len(BY_ID_PREFIX):])
                devices.append(Device(c.name, "usb", serial=parsed.serial if parsed else "",
                                      chip=parsed.mcu if parsed else "", sources=["printer.cfg"]))
            elif c.serial:
                devices.append(Device(c.name, "uart", device=c.serial, sources=["printer.cfg"]))
    else:
        d.notes.append("No printer.cfg: that's fine, I only use what is connected right now "
                       "(USB, DFU/BOOTSEL, CAN nodes). If you have one, set the path in "
                       "trebuchet.cfg, [paths] printer_cfg.")

    # 2) USB now
    scan = detect.scan_usb(runner, by_id_dir)
    for s in scan.serial:
        match = next((x for x in devices if x.transport == "usb" and x.serial and
                      x.serial.lower() == s.serial.lower()), None)
        if match:
            match.present, match.app = True, s.app
            match.chip = match.chip or s.mcu
            match.sources.append("usb")
        else:
            devices.append(Device(f"{s.mcu}-{s.serial[-4:]}", "usb", serial=s.serial, chip=s.mcu,
                                  present=True, app=s.app, sources=["usb"]))
    # Boards in ROM bootloader mode (STM32 DFU / RP2040 BOOTSEL) have no name in by-id:
    # they only show up in lsusb. The IDs 0483:df11 and 2e8a:0003 are general knowledge (confirm with lsusb).
    for kind, prefix, app, chip in (("stm32-dfu", "dfu", "dfu", ""), ("rp2040-bootsel", "bootsel", "bootsel", "rp2040")):
        for i, _ in enumerate(scan.of_kind(kind), 1):
            devices.append(Device(f"{prefix}-{i}", "usb", chip=chip, present=True, app=app,
                                  sources=["lsusb"]))
    for x in devices:
        if x.transport == "usb" and x.present is None:
            x.present = False

    # 3) Moonraker
    if use_moonraker:
        names = [x.name for x in devices if x.transport in ("can", "usb", "uart")
                 and "printer.cfg" in x.sources]
        info = moonraker_mcus(getattr(settings, "moonraker_url", "http://127.0.0.1:7125"),
                              names, opener=opener)
        d.moonraker = info is not None
        if info is None:
            d.notes.append("Moonraker did not respond: no chip/version for the declared CAN nodes.")
        else:
            for x in devices:
                i = info.get(x.name)
                if i:
                    x.chip = i.chip or x.chip
                    x.version = i.version
                    if i.bridge:
                        x.bridge = True
                    if x.transport == "can":
                        x.present = True
                    x.sources.append("moonraker")

    # 4) can0
    d.can_driver = can_driver(settings.can_interface, sys_net)
    can_devs = [x for x in devices if x.transport == "can"]
    if d.can_driver is not None and d.can_driver != "gs_usb":
        for x in can_devs:        # adapter is not a Klipper bridge: no board is
            x.bridge = False

    # 5) canbus_query (on request)
    if canbus_query:
        q = detect.query_can_klipper(settings, runner)
        if not q.ok:
            d.notes.append("canbus_query failed: " + q.output.strip()[-200:])
        for n in q.nodes:
            known = next((x for x in can_devs if x.uuid == n.uuid.lower()), None)
            if known:
                known.present, known.app = True, n.kind
                known.sources.append("canbus_query")
            else:
                devices.append(Device(f"can-{n.uuid[:4]}", "can", uuid=n.uuid.lower(), present=True,
                                      app=n.kind, sources=["canbus_query"]))

    d.devices = devices
    if d.bridge_unresolved:
        d.notes.append("can0 is a Klipper bridge (gs_usb), but I don't know which CAN node is the bridge: "
                       "please tell me.")
    return d


def flash_order(devices: list[Device]) -> list[Device]:
    """Non-CAN first (they don't depend on the bus), then CAN nodes, the bridge last.
    The 'bridge last' rule is our own reasoning, not from Klipper's documentation."""
    def rank(x: Device) -> int:
        if x.bridge:
            return 2
        return 1 if x.transport == "can" else 0
    return sorted(devices, key=rank)   # sorted is stable: keeps the printer.cfg order


def profile_interface_for(x: Device) -> str:
    if x.transport == "can":
        return "usb-can-bridge" if x.bridge else "can"
    return x.transport


def candidate_profiles(x: Device, profiles) -> list:
    """Profiles compatible with the device: same interface and, if the chip is known, the same MCU."""
    want = profile_interface_for(x)
    out = []
    for p in profiles:
        if p.interface != want:
            continue
        if x.chip and chip_matches(p.mcu, x.chip) is False:
            continue
        out.append(p)
    return out


def render(d: Discovery) -> str:
    rows = []
    for x in flash_order(d.devices):
        if x.bridge:
            role = "bridge"
        elif x.transport == "can" and d.bridge_unresolved:
            role = "bridge?"
        else:
            role = x.transport
        seen = {True: "yes", False: "no", None: "?"}[x.present]
        ident = x.uuid or x.serial or x.device or "-"
        rows.append((x.name, role, x.chip or "?", x.version.split()[0] if x.version else "-",
                     ident, seen, x.app or "-"))
    head = ("Name", "Type", "Chip", "Version", "Identifier", "Connected", "App")
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    if not rows:
        lines = ["(nothing found)"]
    else:
        lines = [fmt.format(*head), fmt.format(*("-" * w for w in widths))]
        lines += [fmt.format(*r) for r in rows]
    iface = d.can_iface
    if d.can_driver is None:
        lines.append(f"\n{iface}: does not exist")
    else:
        lines.append(f"\n{iface}: driver {d.can_driver or 'unknown'}"
                     + (" (bridge Klipper)" if d.has_bridge_adapter else ""))
    lines += [f"[!] {n}" for n in d.notes]
    return "\n".join(lines)
