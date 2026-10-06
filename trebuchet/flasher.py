"""Operações sobre boards: construtores de comandos (puros) e executores.

Regras de segurança incorporadas:
  - tudo o que escreve numa board passa por runner.run(mutating=True), logo o
    --dry-run nunca toca em hardware;
  - cada gravação pede confirmação mostrando board, interface e ficheiro;
  - depois de gravar verifica que a board reaparece; se não reaparecer devolve
    falha e o plano pára (nunca avança para a board seguinte);
  - `flashtool -q` só com confirmação de nó único (aviso do Katapult).
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import detect
from .builder import BuildResult
from .config import Settings
from .guides import get_guide
from .profiles import BoardProfile, Machine, MachineBoard
from .registry import Registry
from .runner import Runner


class IO(Protocol):
    def say(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def confirm(self, text: str, default: bool = False) -> bool: ...
    def pause(self, text: str) -> None: ...
    def choose(self, title: str, options: list[str]) -> int | None: ...


@dataclass
class StepResult:
    ok: bool
    message: str = ""
    data: str = ""          # p.ex. UUID descoberto


# --- construtores de comandos (puros, testáveis) --------------------------------
def cmd_flashtool_usb(tool: Path, device: str, firmware: Path | None = None, *,
                      request_only: bool = False, baud: int | None = None) -> list[str]:
    cmd = ["python3", str(tool)]
    if request_only:
        cmd += ["-r"]
    cmd += ["-d", device]
    if baud:
        cmd += ["-b", str(baud)]
    if firmware and not request_only:
        cmd += ["-f", str(firmware)]
    return cmd


def cmd_flashtool_can(tool: Path, iface: str, uuid: str, firmware: Path | None = None, *,
                      request_only: bool = False) -> list[str]:
    cmd = ["python3", str(tool), "-i", iface, "-u", uuid]
    if request_only:
        cmd += ["-r"]
    elif firmware:
        cmd += ["-f", str(firmware)]
    return cmd


def _sudo() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


def cmd_dfu_util(firmware: Path, address: str = "0x08000000") -> list[str]:
    # sudo porque, sem regras udev, um utilizador normal não abre o dispositivo DFU
    # (assunção nossa: confirma no teu Pi; se tiveres regras udev podes tirar o sudo).
    return _sudo() + ["dfu-util", "-d", detect.STM32_DFU_ID, "-a", "0", "-R", "-D", str(firmware),
                      "-s", f"{address}:leave"]


def cmd_service(action: str, service: str) -> list[str]:
    return _sudo() + ["systemctl", action, service]


def find_rp2040_mount() -> Path | None:
    for root in ("/media", "/run/media", "/mnt"):
        r = Path(root)
        if not r.exists():
            continue
        for pattern in ("RPI-RP2", "*/RPI-RP2", "*/*/RPI-RP2"):
            for hit in r.glob(pattern):
                if hit.is_dir():
                    return hit
    return None


class Flasher:
    def __init__(self, settings: Settings, runner: Runner, registry: Registry, io: IO,
                 sleep=time.sleep):
        self.s = settings
        self.run = runner
        self.reg = registry
        self.io = io
        self.sleep = sleep

    # -- serviço ---------------------------------------------------------------------
    def stop_service(self) -> StepResult:
        res = self.run.run(cmd_service("stop", self.s.klipper_service), mutating=True)
        return StepResult(res.ok, "" if res.ok else f"não consegui parar o serviço:\n{res.output}")

    def start_service(self) -> StepResult:
        res = self.run.run(cmd_service("start", self.s.klipper_service), mutating=True)
        return StepResult(res.ok, "" if res.ok else f"não consegui arrancar o serviço:\n{res.output}")

    # -- utilitários ---------------------------------------------------------------------
    def _tool(self) -> Path | None:
        tool = self.s.find_flashtool()
        if not tool:
            self.io.warn("Não encontro o flashtool.py do Katapult. Procurei em:\n  "
                         + "\n  ".join(str(c) for c in self.s.flashtool_candidates()))
        return tool

    def _wait_usb(self, pred, timeout: float):
        return detect.wait_for(lambda: pred(detect.scan_usb(self.run)), timeout, 1.0, self.sleep)

    def _confirm_write(self, mb: MachineBoard, p: BoardProfile, what: str, target: str) -> bool:
        self.io.say(f"\n  Board:      {mb.label} ({p.name}, {p.mcu})")
        self.io.say(f"  Interface:  {p.interface}")
        self.io.say(f"  Ação:       {what}")
        self.io.say(f"  Alvo:       {target}")
        return self.io.confirm("Confirmas a gravação?", default=False)

    # -- teste ativo "tem Katapult?" ------------------------------------------------------
    def active_katapult_test(self, machine: Machine, mb: MachineBoard, p: BoardProfile) -> str:
        """Pede o bootloader ao Klipper e vê o que aparece. Devolve yes/no/unknown.
        Se o Katapult não existir, o Klipper tenta o bootloader da plataforma
        (p.ex. DFU em STM32): a board pode ficar em DFU até ser reiniciada."""
        tool = self._tool()
        if not tool:
            return "unknown"
        if p.dfu_heater_warning:
            self.io.warn("Se não houver Katapult a board pode cair em DFU: desliga aquecedores.")

        if p.interface == "usb":
            scan = detect.scan_usb(self.run)
            dev, cands = detect.pick_usb_serial(scan, serial=mb.serial, apps=("klipper",), mcu_hint=p.mcu)
            if dev is None:
                self.io.warn("Não consegui escolher um único dispositivo Klipper USB "
                             f"({len(cands)} candidatos). Define o serial da board.")
                return "unknown"
            cmd = cmd_flashtool_usb(tool, dev.path, request_only=True)
        else:
            if not mb.uuid:
                self.io.warn("Falta o UUID CAN desta board.")
                return "unknown"
            cmd = cmd_flashtool_can(tool, machine.can_interface, mb.uuid, request_only=True)

        self.io.say("\nVou pedir ao Klipper desta board para reiniciar em bootloader.")
        if not self.io.confirm(f"Continuar com '{mb.label}'?", default=False):
            return "unknown"
        res = self.run.run(cmd, mutating=True)
        if res.dry_run:
            self.io.say("[dry-run] teste não executado; estado continua desconhecido.")
            return "unknown"

        def look():
            scan = detect.scan_usb(self.run)
            if any(d.app == "katapult" for d in scan.serial) or scan.of_kind("katapult"):
                return "yes"
            if scan.of_kind("stm32-dfu") or scan.of_kind("rp2040-bootsel"):
                return "no"
            if p.is_can and not p.is_bridge:
                q = detect.query_can_klipper(self.s, self.run, machine.can_interface)
                if any(n.uuid == mb.uuid and n.kind == "katapult" for n in q.nodes):
                    return "yes"
            return ""

        verdict = detect.wait_for(look, 15, 1.5, self.sleep) or "unknown"
        if verdict == "yes":
            self.io.say("Apareceu em Katapult: a board TEM Katapult (e já está em bootloader).")
        elif verdict == "no":
            self.io.say("Apareceu em DFU/BOOTSEL: a board NÃO tem Katapult. Está agora no bootloader "
                        "de ROM: podes gravar o Katapult já, ou desligar/ligar para voltar ao Klipper.")
        else:
            self.io.warn("Não apareceu nada reconhecível. Reinicia a board (desliga/liga) e confirma "
                         "com lsusb. O estado fica desconhecido.")
        self.reg.set_katapult(machine.slug, mb.label, verdict)
        return verdict

    # -- 1.º Katapult -----------------------------------------------------------------------
    def first_katapult(self, machine: Machine, mb: MachineBoard, p: BoardProfile,
                       katapult: BuildResult | None) -> StepResult:
        self.io.say("\n" + get_guide(p.first_katapult_method))
        method = p.first_katapult_method

        if method == "dfu" and katapult and katapult.bin:
            if p.dfu_heater_warning:
                self.io.warn("Em DFU algumas boards podem ligar o aquecedor: desliga aquecedores.")
            for attempt in range(3):
                scan = detect.scan_usb(self.run)
                if scan.of_kind("stm32-dfu"):
                    break
                self.io.pause("Não vejo nenhuma board em DFU (0483:df11). Põe-na em DFU e carrega Enter.")
            else:
                return StepResult(False, "nenhum dispositivo DFU detetado")
            if not self._confirm_write(mb, p, "gravar Katapult por DFU", str(katapult.bin)):
                return StepResult(False, "cancelado pelo utilizador")
            res = self.run.run(cmd_dfu_util(katapult.bin), mutating=True, stream=True)
            if not res.ok:
                return StepResult(False, "dfu-util falhou")
        elif method == "bootsel" and katapult and katapult.uf2:
            mount = find_rp2040_mount()
            if mount is None and not self.run.dry_run:
                self.io.pause("Não encontro o disco RPI-RP2 montado. Põe a board em BOOTSEL, monta o disco "
                              "e carrega Enter.")
                mount = find_rp2040_mount()
            if mount is None and not self.run.dry_run:
                return StepResult(False, f"disco BOOTSEL não encontrado; copia {katapult.uf2} manualmente")
            target = str(mount) if mount else "(disco RPI-RP2)"
            if not self._confirm_write(mb, p, "copiar katapult.uf2 para o disco BOOTSEL", target):
                return StepResult(False, "cancelado pelo utilizador")
            if self.run.dry_run:
                self.io.say(f"[dry-run] copiar {katapult.uf2} -> {target}")
            else:
                shutil.copyfile(katapult.uf2, Path(mount) / katapult.uf2.name)
                self.sleep(2)
        else:
            self.io.say("Este método é manual (ver guia acima).")
            if not self.io.confirm("Já gravaste o Katapult e a board está em Katapult?", default=False):
                return StepResult(False, "Katapult não confirmado")

        # verificação: para USB a board deve aparecer como Katapult
        if p.interface == "usb" and not self.run.dry_run and method in ("dfu", "bootsel"):
            appeared = self._wait_usb(
                lambda sc: any(d.app == "katapult" for d in sc.serial) or sc.of_kind("katapult"), 20)
            if not appeared:
                return StepResult(False, "a board não apareceu como Katapult depois da gravação")
        self.reg.set_katapult(machine.slug, mb.label, "yes")
        return StepResult(True, "Katapult gravado")

    # -- descobrir UUID (nó CAN novo) ---------------------------------------------------------
    def query_uuid(self, machine: Machine, mb: MachineBoard) -> StepResult:
        self.io.warn("O 'flashtool -q' só é seguro com UM único nó CAN ligado: com vários podem surgir "
                     "erros de transmissão e um nó pode entrar em 'bus off' (fica sem resposta até reiniciar).")
        if not self.io.confirm(f"Só a board '{mb.label}' está ligada ao barramento?", default=False):
            return StepResult(False, "liga só esta board ao adaptador e tenta de novo")
        q = detect.query_can_katapult(self.s, self.run, single_node_confirmed=True,
                                      iface=machine.can_interface)
        if self.run.dry_run:
            return StepResult(True, "[dry-run] UUID não consultado")
        if len(q.nodes) != 1:
            return StepResult(False, f"esperava 1 nó e encontrei {len(q.nodes)}.\n{q.output.strip()}")
        return StepResult(True, f"UUID: {q.nodes[0].uuid}", data=q.nodes[0].uuid)

    # -- gravar Klipper ---------------------------------------------------------------------
    def flash_klipper(self, machine: Machine, mb: MachineBoard, p: BoardProfile,
                      build: BuildResult) -> StepResult:
        tool = self._tool()
        if not tool:
            return StepResult(False, "flashtool.py em falta")
        fw = build.bin
        if fw is None:
            return StepResult(False, "build do Klipper sem .bin")

        if p.interface == "usb":
            r = self._flash_usb(tool, mb, p, fw)
        elif p.interface == "uart":
            r = self._flash_uart(tool, mb, p, fw)
        elif p.interface == "can":
            r = self._flash_can(tool, machine, mb, p, fw)
        else:
            r = self._flash_bridge(tool, machine, mb, p, fw)

        if r.ok:
            self.reg.update(machine.slug, mb.label, klipper_version=build.source_version,
                            last_flash=time.strftime("%Y-%m-%dT%H:%M:%S"))
        return r

    @staticmethod
    def _flashed(res) -> bool:
        return res.dry_run or (res.ok and "Programming Complete" in res.output)

    def _choose_dev(self, scan, mb, p, apps):
        dev, cands = detect.pick_usb_serial(scan, serial=mb.serial, apps=apps, mcu_hint=p.mcu)
        if dev or not cands or mb.serial:
            return dev
        idx = self.io.choose("Vários dispositivos possíveis. Qual é a board "
                             f"'{mb.label}'?", [f"{d.mcu}  {d.serial}  ({d.app})" for d in cands])
        return cands[idx] if idx is not None else None

    def _flash_usb(self, tool, mb, p, fw) -> StepResult:
        scan = detect.scan_usb(self.run)
        dev = self._choose_dev(scan, mb, p, ("klipper", "katapult"))
        if dev is None:
            return StepResult(False, f"não encontrei a board '{mb.label}' por USB")
        if not self._confirm_write(mb, p, "gravar Klipper via Katapult (USB)", f"{dev.path}  <-  {fw}"):
            return StepResult(False, "cancelado pelo utilizador")
        res = self.run.run(cmd_flashtool_usb(tool, dev.path, fw), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "a gravação não terminou com 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")
        serial = dev.serial
        back = self._wait_usb(lambda sc: any(d.app == "klipper" and d.serial == serial for d in sc.serial), 25)
        if not back:
            return StepResult(False, "gravou, mas a board não voltou a aparecer como Klipper (USB)")
        return StepResult(True, "gravada e verificada (reapareceu como Klipper)")

    def _flash_uart(self, tool, mb, p, fw) -> StepResult:
        if not mb.device:
            return StepResult(False, "board UART sem 'device'")
        if not self._confirm_write(mb, p, "gravar Klipper via Katapult (UART)", f"{mb.device}  <-  {fw}"):
            return StepResult(False, "cancelado pelo utilizador")
        res = self.run.run(cmd_flashtool_usb(tool, mb.device, fw, baud=p.uart_baud), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "a gravação não terminou com 'Programming Complete'")
        return StepResult(True, "gravada (sem verificação automática em UART: confirma no Klipper)")

    def _flash_can(self, tool, machine, mb, p, fw) -> StepResult:
        if not mb.uuid:
            return StepResult(False, "falta o UUID CAN")
        if not self._confirm_write(mb, p, "gravar Klipper via Katapult (CAN)",
                                   f"{machine.can_interface} uuid {mb.uuid}  <-  {fw}"):
            return StepResult(False, "cancelado pelo utilizador")
        res = self.run.run(cmd_flashtool_can(tool, machine.can_interface, mb.uuid, fw),
                           mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "a gravação não terminou com 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")

        def seen():
            q = detect.query_can_klipper(self.s, self.run, machine.can_interface)
            return any(n.uuid == mb.uuid and n.kind == "klipper" for n in q.nodes)
        if not detect.wait_for(seen, 30, 1.0, self.sleep):
            return StepResult(False, "gravou, mas o UUID não reapareceu como Klipper no canbus_query")
        return StepResult(True, "gravada e verificada (UUID reapareceu como Klipper)")

    def _flash_bridge(self, tool, machine, mb, p, fw) -> StepResult:
        scan = detect.scan_usb(self.run)
        kat, _ = detect.pick_usb_serial(scan, serial=mb.serial, apps=("katapult",), mcu_hint=p.mcu)
        if kat is None:
            if not mb.uuid:
                return StepResult(False, "falta o UUID CAN da bridge (para pedir o bootloader)")
            if p.dfu_heater_warning:
                self.io.warn("Se a bridge não tiver Katapult pode cair em DFU: desliga aquecedores.")
            if not self.io.confirm(f"Pedir bootloader à bridge '{mb.label}' por CAN? (o can0 vai desaparecer)"):
                return StepResult(False, "cancelado pelo utilizador")
            self.run.run(cmd_flashtool_can(tool, machine.can_interface, mb.uuid, request_only=True),
                         mutating=True)
            if self.run.dry_run:
                self.io.say("[dry-run] a seguir: gravar por Katapult-USB e esperar pelo can0.")
                return StepResult(True, "[dry-run]")
            found = self._wait_usb(lambda sc: detect.pick_usb_serial(
                sc, serial=mb.serial, apps=("katapult",), mcu_hint=p.mcu)[0], 20)
            kat = found or None
            if kat is None:
                return StepResult(False, "a bridge não apareceu como Katapult por USB")

        if not self._confirm_write(mb, p, "gravar Klipper (bridge) via Katapult-USB", f"{kat.path}  <-  {fw}"):
            return StepResult(False, "cancelado pelo utilizador")
        res = self.run.run(cmd_flashtool_usb(tool, kat.path, fw), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "a gravação não terminou com 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")

        up = detect.wait_for(lambda: detect.can_status(self.run, machine.can_interface).up, 30, 1.0, self.sleep)
        if not up:
            rate = p.can_bitrate or 1000000
            return StepResult(False, f"gravou, mas {machine.can_interface} não voltou. Sobe-o com:\n"
                                     f"  sudo ip link set {machine.can_interface} up type can bitrate {rate}\n"
                                     "(ou usa allow-hotplug no /etc/network/interfaces.d) e verifica de novo.")

        def seen():
            q = detect.query_can_klipper(self.s, self.run, machine.can_interface)
            return any(n.uuid == mb.uuid and n.kind == "klipper" for n in q.nodes)
        if mb.uuid and not detect.wait_for(seen, 30, 1.0, self.sleep):
            return StepResult(False, "can0 voltou mas a bridge não aparece no canbus_query")
        return StepResult(True, "bridge gravada e verificada (can0 de volta)")
