"""Board operations: command builders (pure) and executors.

Built-in safety rules:
  - everything that writes to a board goes through runner.run(mutating=True), so
    --dry-run never touches hardware;
  - every flash asks for confirmation, showing board, interface and file;
  - after flashing it checks that the board comes back; if it does not, it returns
    a failure and the plan stops (it never moves on to the next board);
  - `flashtool -q` only with single-node confirmation (Katapult warning).
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import detect, printguard
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
    data: str = ""          # e.g. discovered UUID


# --- command builders (pure, testable) --------------------------------
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
    # sudo because, without udev rules, a normal user cannot open the DFU device
    # (our assumption: check on your Pi; if you have udev rules you can drop sudo).
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

    # -- service ---------------------------------------------------------------------
    def stop_service(self) -> StepResult:
        """Stopping Klipper kills a print in progress, so check first. Moonraker unreachable
        is NOT treated as idle: the user has to say so."""
        st = printguard.print_state(self.s.moonraker_url)
        if st.busy:
            return StepResult(False, f"A print is in progress ({st.label}). Flashing stops Klipper and "
                                     "would ruin it. Wait for it to finish (or cancel it) and run again.")
        if not st.reachable:
            self.io.warn("Cannot check whether a print is running (Moonraker did not answer).")
            if not self.io.confirm("Stop Klipper anyway? Say yes only if the printer is idle",
                                   default=False):
                return StepResult(False, "Cancelled: could not confirm that the printer is idle.")
        res = self.run.run(cmd_service("stop", self.s.klipper_service), mutating=True)
        return StepResult(res.ok, "" if res.ok else f"could not stop the service:\n{res.output}")

    def start_service(self) -> StepResult:
        res = self.run.run(cmd_service("start", self.s.klipper_service), mutating=True)
        return StepResult(res.ok, "" if res.ok else f"could not start the service:\n{res.output}")

    # -- helpers ---------------------------------------------------------------------
    def _tool(self) -> Path | None:
        tool = self.s.find_flashtool()
        if not tool:
            self.io.warn("Cannot find Katapult's flashtool.py. Looked in:\n  "
                         + "\n  ".join(str(c) for c in self.s.flashtool_candidates()))
        return tool

    def _wait_usb(self, pred, timeout: float):
        return detect.wait_for(lambda: pred(detect.scan_usb(self.run)), timeout, 1.0, self.sleep)

    def _confirm_write(self, mb: MachineBoard, p: BoardProfile, what: str, target: str) -> bool:
        self.io.say(f"\n  Board:      {mb.label} ({p.name}, {p.mcu})")
        self.io.say(f"  Interface:  {p.interface}")
        self.io.say(f"  Action:     {what}")
        self.io.say(f"  Target:     {target}")
        return self.io.confirm("Confirm the flash?", default=False)

    # -- active "does it have Katapult?" test ------------------------------------------------------
    def active_katapult_test(self, machine: Machine, mb: MachineBoard, p: BoardProfile) -> str:
        """Asks Klipper for the bootloader and sees what shows up. Returns yes/no/unknown.
        If Katapult is not present, Klipper tries the platform bootloader
        (e.g. DFU on STM32): the board may stay in DFU until it is restarted."""
        tool = self._tool()
        if not tool:
            return "unknown"
        if p.dfu_heater_warning:
            self.io.warn("Without Katapult the board may drop into DFU: turn off heaters.")

        if p.interface == "usb":
            scan = detect.scan_usb(self.run)
            dev, cands = detect.pick_usb_serial(scan, serial=mb.serial, apps=("klipper",), mcu_hint=p.mcu)
            if dev is None:
                self.io.warn("Could not pick a single Klipper USB device "
                             f"({len(cands)} candidates). Set the board serial.")
                return "unknown"
            cmd = cmd_flashtool_usb(tool, dev.path, request_only=True)
        else:
            if not mb.uuid:
                self.io.warn("This board is missing its CAN UUID.")
                return "unknown"
            cmd = cmd_flashtool_can(tool, machine.can_interface, mb.uuid, request_only=True)

        self.io.say("\nI will ask this board's Klipper to reboot into the bootloader.")
        if not self.io.confirm(f"Continue with '{mb.label}'?", default=False):
            return "unknown"
        res = self.run.run(cmd, mutating=True)
        if res.dry_run:
            self.io.say("[dry-run] test not run; state remains unknown.")
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
            self.io.say("Appeared as Katapult: the board HAS Katapult (and is already in bootloader).")
        elif verdict == "no":
            self.io.say("Appeared as DFU/BOOTSEL: the board does NOT have Katapult. It is now in the ROM "
                        "bootloader: you can flash Katapult now, or power-cycle to return to Klipper.")
        else:
            self.io.warn("Nothing recognizable appeared. Restart the board (power-cycle) and check "
                         "with lsusb. The state stays unknown.")
        self.reg.set_katapult(machine.slug, mb.label, verdict)
        return verdict

    # -- first Katapult -----------------------------------------------------------------------
    def first_katapult(self, machine: Machine, mb: MachineBoard, p: BoardProfile,
                       katapult: BuildResult | None) -> StepResult:
        self.io.say("\n" + get_guide(p.first_katapult_method))
        method = p.first_katapult_method

        if method == "dfu" and katapult and katapult.bin:
            if p.dfu_heater_warning:
                self.io.warn("In DFU some boards may switch the heater on: turn off heaters.")
            for attempt in range(3):
                scan = detect.scan_usb(self.run)
                if scan.of_kind("stm32-dfu"):
                    break
                self.io.pause("No board in DFU (0483:df11) found. Put it in DFU and press Enter.")
            else:
                return StepResult(False, "no DFU device detected")
            if not self._confirm_write(mb, p, "flash Katapult via DFU", str(katapult.bin)):
                return StepResult(False, "cancelled by the user")
            res = self.run.run(cmd_dfu_util(katapult.bin), mutating=True, stream=True)
            if not res.ok:
                return StepResult(False, "dfu-util falhou")
        elif method == "bootsel" and katapult and katapult.uf2:
            mount = find_rp2040_mount()
            if mount is None and not self.run.dry_run:
                self.io.pause("Cannot find the RPI-RP2 drive mounted. Put the board in BOOTSEL, mount the drive "
                              "and press Enter.")
                mount = find_rp2040_mount()
            if mount is None and not self.run.dry_run:
                return StepResult(False, f"BOOTSEL drive not found; copy {katapult.uf2} manually")
            target = str(mount) if mount else "(RPI-RP2 drive)"
            if not self._confirm_write(mb, p, "copy katapult.uf2 to the BOOTSEL drive", target):
                return StepResult(False, "cancelled by the user")
            if self.run.dry_run:
                self.io.say(f"[dry-run] copy {katapult.uf2} -> {target}")
            else:
                shutil.copyfile(katapult.uf2, Path(mount) / katapult.uf2.name)
                self.sleep(2)
        else:
            self.io.say("This method is manual (see guide above).")
            if not self.io.confirm("Have you flashed Katapult and is the board in Katapult?", default=False):
                return StepResult(False, "Katapult not confirmed")

        # verification: for USB the board must appear as Katapult
        if p.interface == "usb" and not self.run.dry_run and method in ("dfu", "bootsel"):
            appeared = self._wait_usb(
                lambda sc: any(d.app == "katapult" for d in sc.serial) or sc.of_kind("katapult"), 20)
            if not appeared:
                return StepResult(False, "the board did not appear as Katapult after flashing")
        self.reg.set_katapult(machine.slug, mb.label, "yes")
        return StepResult(True, "Katapult flashed")

    # -- discover UUID (new CAN node) ---------------------------------------------------------
    def query_uuid(self, machine: Machine, mb: MachineBoard) -> StepResult:
        self.io.warn("'flashtool -q' is only safe with a SINGLE CAN node connected: with several, transmission "
                     "errors can occur and a node can go 'bus off' (unresponsive until restarted).")
        if not self.io.confirm(f"Is only board '{mb.label}' connected to the bus?", default=False):
            return StepResult(False, "connect only this board to the adapter and try again")
        q = detect.query_can_katapult(self.s, self.run, single_node_confirmed=True,
                                      iface=machine.can_interface)
        if self.run.dry_run:
            return StepResult(True, "[dry-run] UUID not queried")
        if len(q.nodes) != 1:
            return StepResult(False, f"expected 1 node, found {len(q.nodes)}.\n{q.output.strip()}")
        return StepResult(True, f"UUID: {q.nodes[0].uuid}", data=q.nodes[0].uuid)

    # -- flash Klipper ---------------------------------------------------------------------
    def flash_klipper(self, machine: Machine, mb: MachineBoard, p: BoardProfile,
                      build: BuildResult) -> StepResult:
        tool = self._tool()
        if not tool:
            return StepResult(False, "flashtool.py missing")
        fw = build.bin
        if fw is None:
            return StepResult(False, "Klipper build has no .bin")

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
        idx = self.io.choose("Several possible devices. Which one is board "
                             f"'{mb.label}'?", [f"{d.mcu}  {d.serial}  ({d.app})" for d in cands])
        return cands[idx] if idx is not None else None

    def _flash_usb(self, tool, mb, p, fw) -> StepResult:
        scan = detect.scan_usb(self.run)
        dev = self._choose_dev(scan, mb, p, ("klipper", "katapult"))
        if dev is None:
            return StepResult(False, f"could not find board '{mb.label}' over USB")
        if not self._confirm_write(mb, p, "flash Klipper via Katapult (USB)", f"{dev.path}  <-  {fw}"):
            return StepResult(False, "cancelled by the user")
        res = self.run.run(cmd_flashtool_usb(tool, dev.path, fw), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "flashing did not finish with 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")
        serial = dev.serial
        back = self._wait_usb(lambda sc: any(d.app == "klipper" and d.serial == serial for d in sc.serial), 25)
        if not back:
            return StepResult(False, "flashed, but the board did not reappear as Klipper (USB)")
        return StepResult(True, "flashed and verified (reappeared as Klipper)")

    def _flash_uart(self, tool, mb, p, fw) -> StepResult:
        if not mb.device:
            return StepResult(False, "UART board has no 'device'")
        if not self._confirm_write(mb, p, "flash Klipper via Katapult (UART)", f"{mb.device}  <-  {fw}"):
            return StepResult(False, "cancelled by the user")
        res = self.run.run(cmd_flashtool_usb(tool, mb.device, fw, baud=p.uart_baud), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "flashing did not finish with 'Programming Complete'")
        return StepResult(True, "flashed (no automatic verification over UART: check in Klipper)")

    def _flash_can(self, tool, machine, mb, p, fw) -> StepResult:
        if not mb.uuid:
            return StepResult(False, "missing CAN UUID")
        if not self._confirm_write(mb, p, "flash Klipper via Katapult (CAN)",
                                   f"{machine.can_interface} uuid {mb.uuid}  <-  {fw}"):
            return StepResult(False, "cancelled by the user")
        res = self.run.run(cmd_flashtool_can(tool, machine.can_interface, mb.uuid, fw),
                           mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "flashing did not finish with 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")

        def seen():
            q = detect.query_can_klipper(self.s, self.run, machine.can_interface)
            return any(n.uuid == mb.uuid and n.kind == "klipper" for n in q.nodes)
        if not detect.wait_for(seen, 30, 1.0, self.sleep):
            return StepResult(False, "flashed, but the UUID did not reappear as Klipper in canbus_query")
        return StepResult(True, "flashed and verified (UUID reappeared as Klipper)")

    def _flash_bridge(self, tool, machine, mb, p, fw) -> StepResult:
        scan = detect.scan_usb(self.run)
        kat, _ = detect.pick_usb_serial(scan, serial=mb.serial, apps=("katapult",), mcu_hint=p.mcu)
        if kat is None:
            if not mb.uuid:
                return StepResult(False, "missing the bridge's CAN UUID (needed to request the bootloader)")
            if p.dfu_heater_warning:
                self.io.warn("If the bridge has no Katapult it may drop into DFU: turn off heaters.")
            if not self.io.confirm(f"Request bootloader from bridge '{mb.label}' over CAN? (can0 will disappear)"):
                return StepResult(False, "cancelled by the user")
            self.run.run(cmd_flashtool_can(tool, machine.can_interface, mb.uuid, request_only=True),
                         mutating=True)
            if self.run.dry_run:
                self.io.say("[dry-run] next: flash via Katapult-USB and wait for can0.")
                return StepResult(True, "[dry-run]")
            found = self._wait_usb(lambda sc: detect.pick_usb_serial(
                sc, serial=mb.serial, apps=("katapult",), mcu_hint=p.mcu)[0], 20)
            kat = found or None
            if kat is None:
                return StepResult(False, "the bridge did not appear as Katapult over USB")

        if not self._confirm_write(mb, p, "flash Klipper (bridge) via Katapult-USB", f"{kat.path}  <-  {fw}"):
            return StepResult(False, "cancelled by the user")
        res = self.run.run(cmd_flashtool_usb(tool, kat.path, fw), mutating=True, stream=True)
        if not self._flashed(res):
            return StepResult(False, "flashing did not finish with 'Programming Complete'")
        if res.dry_run:
            return StepResult(True, "[dry-run]")

        up = detect.wait_for(lambda: detect.can_status(self.run, machine.can_interface).up, 30, 1.0, self.sleep)
        if not up:
            rate = p.can_bitrate or 1000000
            return StepResult(False, f"flashed, but {machine.can_interface} did not come back. Bring it up with:\n"
                                     f"  sudo ip link set {machine.can_interface} up type can bitrate {rate}\n"
                                     "(or use allow-hotplug in /etc/network/interfaces.d) and check again.")

        def seen():
            q = detect.query_can_klipper(self.s, self.run, machine.can_interface)
            return any(n.uuid == mb.uuid and n.kind == "klipper" for n in q.nodes)
        if mb.uuid and not detect.wait_for(seen, 30, 1.0, self.sleep):
            return StepResult(False, "can0 is back but the bridge does not show up in canbus_query")
        return StepResult(True, "bridge flashed and verified (can0 back)")
