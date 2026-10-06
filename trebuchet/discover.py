"""Descoberta do que a máquina tem: junta várias fontes numa só lista de dispositivos.

Fontes, da mais segura para a mais invasiva:
  1. printer.cfg (e os [include]): nomes, canbus_uuid, serial. Só leitura de ficheiros.
  2. /dev/serial/by-id: boards USB ligadas agora e o MCU (vem no nome).
  3. Moonraker (opcional): chip e versão do firmware de cada MCU declarado, com o Klipper a correr.
  4. can0: se o adaptador é uma bridge Klipper (driver gs_usb) ou outro (ex.: MCP2515).
  5. canbus_query (opcional, a pedido): nós CAN ainda sem id atribuído.

O que NÃO se consegue saber (e o programa não adivinha): o modelo da board (vários
modelos partilham o MCU) e se uma board já tem Katapult. O chip serve para validar o
perfil escolhido, não para o escolher sozinho.

Por verificar em hardware real: o formato exato da resposta do Moonraker (campos
mcu_constants/MCU) e se CANBUS_BRIDGE aparece nas constantes. O parser é tolerante:
se não encontrar o campo, deixa o chip por saber.
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

# Núcleo do nome do MCU para comparar perfil <-> chip (stm32f446xx == stm32f446).
_CORE = re.compile(r"(stm32[a-z]\d[a-z0-9]{2}|rp2040)")


# --- printer.cfg ------------------------------------------------------------------
@dataclass
class CfgMcu:
    name: str                 # "mcu" para a secção sem nome, senão o sufixo ([mcu toolhead] -> toolhead)
    serial: str = ""
    uuid: str = ""
    canbus_interface: str = ""
    source: str = ""          # ficheiro onde foi declarado


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
    """Lê as secções [mcu] e [mcu NOME], seguindo [include ...] (relativo ao ficheiro,
    com globs). Não valida o resto do ficheiro."""
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
        if raw[0] in " \t":        # continuação de valor multilinha
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
    """Resposta de /printer/objects/query?mcu&mcu%20nome. Tolerante: campos em falta ficam vazios."""
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
    """None se o Moonraker não responde (Klipper parado, sem Moonraker, URL errado)."""
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
    """Nome do driver do interface (gs_usb numa bridge Klipper). None se não existe."""
    link = sys_net / iface / "device" / "driver"
    try:
        return link.resolve().name if link.exists() else ("" if (sys_net / iface).exists() else None)
    except OSError:
        return None


# --- modelo -----------------------------------------------------------------------
@dataclass
class Device:
    name: str
    transport: str                 # usb | can | uart
    uuid: str = ""
    serial: str = ""
    device: str = ""
    chip: str = ""
    version: str = ""
    present: Optional[bool] = None   # visto agora; None = não consegui saber
    bridge: Optional[bool] = None    # None = por identificar
    app: str = ""                    # klipper | katapult, quando visível
    sources: list[str] = field(default_factory=list)


@dataclass
class Discovery:
    devices: list[Device] = field(default_factory=list)
    can_iface: str = "can0"
    can_driver: Optional[str] = None     # None = interface não existe; "" = driver desconhecido
    printer_cfg: str = ""
    moonraker: Optional[bool] = None     # None = não perguntado
    notes: list[str] = field(default_factory=list)

    @property
    def has_bridge_adapter(self) -> bool:
        return self.can_driver == "gs_usb"

    @property
    def bridge_unresolved(self) -> bool:
        """Há um adaptador gs_usb (bridge Klipper) mas nenhum nó está marcado como ponte."""
        can = [d for d in self.devices if d.transport == "can"]
        return self.has_bridge_adapter and bool(can) and not any(d.bridge for d in can)


def chip_core(text: str) -> str:
    m = _CORE.search(text.lower())
    return m.group(1) if m else ""


def chip_matches(profile_mcu: str, chip: str) -> Optional[bool]:
    """True/False quando ambos permitem comparar; None quando não dá (não bloqueia)."""
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
        d.notes.append("printer.cfg não encontrado (defino o caminho em trebuchet.cfg, [paths] printer_cfg).")

    # 2) USB agora
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
            d.notes.append("Moonraker não respondeu: sem chip/versão dos nós CAN declarados.")
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
        for x in can_devs:        # adaptador que não é bridge Klipper: nenhuma board o é
            x.bridge = False

    # 5) canbus_query (a pedido)
    if canbus_query:
        q = detect.query_can_klipper(settings, runner)
        if not q.ok:
            d.notes.append("canbus_query falhou: " + q.output.strip()[-200:])
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
        d.notes.append("O can0 é uma bridge Klipper (gs_usb), mas não sei qual dos nós CAN é a ponte: "
                       "diz-me tu.")
    return d


def flash_order(devices: list[Device]) -> list[Device]:
    """Fora do CAN primeiro (não dependem do barramento), nós CAN depois, a ponte em último.
    A regra 'ponte em último' é raciocínio nosso, não vem da documentação do Klipper."""
    def rank(x: Device) -> int:
        if x.bridge:
            return 2
        return 1 if x.transport == "can" else 0
    return sorted(devices, key=rank)   # sorted é estável: mantém a ordem do printer.cfg


def profile_interface_for(x: Device) -> str:
    if x.transport == "can":
        return "usb-can-bridge" if x.bridge else "can"
    return x.transport


def candidate_profiles(x: Device, profiles) -> list:
    """Perfis compatíveis com o dispositivo: mesma interface e, se o chip é conhecido, o mesmo MCU."""
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
            role = "ponte"
        elif x.transport == "can" and d.bridge_unresolved:
            role = "ponte?"
        else:
            role = x.transport
        seen = {True: "sim", False: "não", None: "?"}[x.present]
        ident = x.uuid or x.serial or x.device or "-"
        rows.append((x.name, role, x.chip or "?", x.version.split()[0] if x.version else "-",
                     ident, seen, x.app or "-"))
    head = ("Nome", "Tipo", "Chip", "Versão", "Identificador", "Ligada", "App")
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*head), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows] or ["(nada encontrado)"]
    iface = d.can_iface
    if d.can_driver is None:
        lines.append(f"\n{iface}: não existe")
    else:
        lines.append(f"\n{iface}: driver {d.can_driver or 'desconhecido'}"
                     + (" (bridge Klipper)" if d.has_bridge_adapter else ""))
    lines += [f"[!] {n}" for n in d.notes]
    return "\n".join(lines)
