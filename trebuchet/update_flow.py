"""The main flow: scan what is connected -> identify each board -> plan -> build -> flash.

Design rules (they are the reason this module exists):
  - detect first, ask only what cannot be detected;
  - Enter always accepts the recommended option;
  - a board identified once is recognised next time (by CAN UUID / USB serial);
  - nothing is flashed without a plan and a confirmation, and never during a print.
"""
from __future__ import annotations

import socket
from typing import Optional

from . import catalog, discover, matching
from .profile_wizard import ProfileWizard
from .profiles import BoardProfile, Machine, MachineBoard, list_machines, slugify


class UpdateFlow:
    def __init__(self, app):
        self.app = app
        self.io = app.io
        self.s = app.s

    # -- entry -----------------------------------------------------------------------
    def run(self) -> None:
        io = self.io
        machines = list_machines(self.s.machines_dir)
        if machines:
            opts = [f"{m.name}  ({len(m.boards)} boards)" for m in machines]
            opts.append("A new machine: scan what is connected")
            i = io.choose("Which machine do you want to update?", opts, default=0)
            if i is None:
                return
            if i < len(machines):
                self.update_saved(machines[i])
                return
        self.new_machine()

    # -- saved machine ---------------------------------------------------------------
    def update_saved(self, m: Machine) -> None:
        self.app.show_state(m)
        self.plan_and_run(m)

    # -- new machine -----------------------------------------------------------------
    def new_machine(self) -> Optional[Machine]:
        io = self.io
        io.title("Scanning")
        look_can = False
        st = discover.can_driver(self.s.can_interface)
        if st is not None:
            look_can = io.confirm(f"{self.s.can_interface} exists. Also look for CAN boards that have no id "
                                  "yet (read-only)?", default=True)
        found = self.app.run_discovery(ask=False, canbus_query=look_can)
        if not found.devices:
            io.warn("I found no boards. Check: USB cable and power; for a blank board, hold its "
                    "BOOT button while plugging it in (DFU/BOOTSEL); for CAN, wiring, termination, "
                    f"and that {self.s.can_interface} is up.")
            io.pause()
            return None
        self._resolve_bridge(found)

        default_name = slugify(socket.gethostname())
        name = io.ask("\nName this machine", default_name)
        m = Machine(name=name, can_interface=self.s.can_interface)
        profiles = list(self.app.profiles().values())
        entries = catalog.load_catalog(self.s.klipper_dir, self.s.catalog_file)
        cfg_path = discover.find_printer_cfg(self.s.printer_cfg)

        devices = discover.flash_order(found.devices)
        for n, dev in enumerate(devices, 1):
            picked = self.identify(dev, n, len(devices), profiles, entries, cfg_path)
            if picked is None:
                continue
            profile, label = picked
            if profile.id not in {p.id for p in profiles}:
                profiles.append(profile)
            label = self._unique_label(m, label)
            mb = MachineBoard(label=label, profile_id=profile.id, uuid=dev.uuid, serial=dev.serial,
                              device=dev.device, chip=dev.chip)
            m.boards.append(mb)
            self.app.reg.remember_board(self.app.reg.board_key(dev.uuid, dev.serial), profile.id, label)
            self._seed_katapult_state(m, mb, dev)

        if not m.boards:
            io.warn("No board was added; nothing saved.")
            return None
        self.app.save(m)
        self.plan_and_run(m)
        return m

    # -- identification ----------------------------------------------------------------
    def identify(self, dev: discover.Device, n: int, total: int, profiles: list, entries: list,
                 cfg_path) -> Optional[tuple]:
        io = self.io
        io.title(f"Board {n} of {total}: {dev.name}")
        io.say(self._describe(dev))
        sugg = matching.suggest(dev, profiles, entries, self.app.reg, cfg_path)
        if sugg and sugg[0].kind == "remembered":
            io.ok(f"Recognised: {sugg[0].title}")
            if io.confirm("Use it?", default=True):
                return sugg[0].profile, dev.name
        opts = [s.line() for s in sugg]
        opts += ["Browse Klipper's list of boards", "Create a profile from scratch (menuconfig)",
                 "Skip this board"]
        i = io.choose("Which board is this?", opts, default=0 if sugg else None)
        if i is None or i == len(opts) - 1:
            return None
        wizard = ProfileWizard(self.s, io, self.app.runner)
        if i < len(sugg):
            s = sugg[i]
            profile = s.profile or wizard.create(entry=s.entry, device=dev)
        elif i == len(sugg):
            entry = self.app.browse_catalog(dev.chip)
            profile = wizard.create(entry=entry, device=dev) if entry else None
        else:
            profile = wizard.create(device=dev)
        return (profile, dev.name) if profile else None

    @staticmethod
    def _describe(dev: discover.Device) -> str:
        link = {"usb": "USB", "can": "CAN", "uart": "UART"}.get(dev.transport, dev.transport)
        ident = dev.uuid or dev.serial or dev.device or "-"
        chip = dev.chip or "unknown"
        src = ""
        if dev.chip:
            src = "  (reported by the running firmware)" if dev.app in ("", "klipper") else \
                  "  (reported by the board)"
        lines = [f"  Connection : {link}  {ident}",
                 f"  MCU        : {chip}{src}"]
        if dev.version:
            lines.append(f"  Firmware   : Klipper {dev.version.split()[0]}")
        elif dev.app:
            lines.append(f"  State      : {dev.app}")
        return "\n".join(lines)

    # -- helpers -----------------------------------------------------------------------
    def _resolve_bridge(self, found: discover.Discovery) -> None:
        if not found.bridge_unresolved:
            return
        can = [x for x in found.devices if x.transport == "can"]
        i = self.io.choose(f"{found.can_iface} is a Klipper USB-CAN bridge. Which board is the bridge "
                           "(the one plugged into the Pi by USB)?",
                           [f"{x.name}  ({x.uuid})" for x in can] + ["None of these / I don't know"])
        if i is not None and i < len(can):
            can[i].bridge = True

    @staticmethod
    def _unique_label(m: Machine, label: str) -> str:
        base, n = slugify(label), 2
        out = base
        while m.board(out):
            out = f"{base}-{n}"
            n += 1
        return out

    def _seed_katapult_state(self, m: Machine, mb: MachineBoard, dev: discover.Device) -> None:
        """What the scan already proves: a board in Katapult has it; a board in a ROM bootloader
        (DFU/BOOTSEL) is blank or was put there, so it needs Katapult flashed."""
        state = {"katapult": "yes", "dfu": "no", "bootsel": "no"}.get(dev.app, "")
        if state:
            self.app.reg.set_katapult(m.slug, mb.label, state)

    def _resolve_katapult(self, m: Machine) -> None:
        io = self.io
        unknown = [b for b in m.boards if self.app.reg.katapult(m.slug, b.label) == "unknown"]
        if not unknown:
            return
        names = ", ".join(b.label for b in unknown)
        if len(unknown) > 1:
            i = io.choose(f"Do these boards already have Katapult? ({names})",
                          ["Yes, all of them", "No, none of them (new boards)",
                           "Not sure: test each one (recommended)"], default=2)
            if i == 0 or i == 1:
                for b in unknown:
                    self.app.reg.set_katapult(m.slug, b.label, "yes" if i == 0 else "no")
                return
            if i is None:
                return
        for b in unknown:
            p = self.app.profiles().get(b.profile_id)
            i = io.choose(f"Does '{b.label}' already have Katapult?",
                          ["Yes", "No", "Not sure: test it now (asks the board for its bootloader)"],
                          default=2)
            if i == 0 or i == 1:
                self.app.reg.set_katapult(m.slug, b.label, "yes" if i == 0 else "no")
            elif i == 2 and p:
                self.app.flasher.active_katapult_test(m, b, p)

    # -- plan, build, flash --------------------------------------------------------------
    def plan_and_run(self, m: Machine) -> None:
        io = self.io
        self._resolve_katapult(m)
        wiz = ProfileWizard(self.s, io, self.app.runner)
        for b in m.boards:                         # Katapult config only for boards that need Katapult
            p = self.app.profiles().get(b.profile_id)
            if p and self.app.reg.katapult(m.slug, b.label) == "no":
                if not wiz.ensure_katapult_config(p):
                    io.warn(f"No Katapult config for '{b.label}': the plan will not include it.")
        plan = self.app.plan_for(m)
        self.app.show_plan(plan)
        if plan.has_errors:
            io.warn("Fix the errors above and run again (they are saved: nothing has to be typed twice).")
            return
        if self.app.runner.dry_run:
            io.warn("--dry-run: builds run, but nothing is flashed and no service is touched.")
        i = io.choose("What now?", ["Build and flash (recommended)", "Only build (flash later)",
                                    "Save and exit"], default=0)
        if i is None or i == 2:
            return
        if not self.app.run_plan(m, phase="build"):
            return
        io.ok("Everything built. Nothing has been written to any board yet.")
        if i == 1:
            return
        if io.confirm("Flash now?", default=False):
            self.app.run_plan(m, phase="flash")
