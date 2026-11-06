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
from .advisor import ERROR
from .builder import BuildError, build_firmware, load_build
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
        self.plan_and_run(m, rebuild=True)

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
            self.prepare_board(m, mb, build=True)

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
        family = self._family(dev)
        sugg = matching.suggest(dev, profiles, entries, self.app.reg, cfg_path, family=family)
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
            entry = self.app.browse_catalog(dev.chip, family=family)
            profile = wizard.create(entry=entry, device=dev) if entry else None
        else:
            profile = wizard.create(device=dev)
        return (profile, dev.name) if profile else None

    def _family(self, dev: discover.Device) -> str:
        """A board in a ROM bootloader (DFU) reports no chip. Ask once, STM32 is the default."""
        core = discover.chip_core(dev.chip)
        if core:
            return ""
        i = self.io.choose("This board does not report its chip. Which family is it?",
                           ["STM32 (a DFU bootloader is STM32's)", "RP2040"], default=0, cancel="I don't know")
        return {0: "stm32", 1: "rp2040"}.get(i, "")

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

    def prepare_board(self, m: Machine, b: MachineBoard, build: bool = False) -> None:
        """Everything one board needs, in order, before moving on to the next:
        profile (already chosen) -> does it have Katapult? -> Katapult config if it needs one
        -> build (new boards only). Already-known answers are not asked again."""
        io = self.io
        p = self.app.profiles().get(b.profile_id)
        if p is None or p.problems():
            return                                     # the plan reports it and offers a repair
        if self.app.reg.katapult(m.slug, b.label) == "unknown":
            self._ask_katapult(m, b, p)
        state = self.app.reg.katapult(m.slug, b.label)
        if state == "no":
            if not ProfileWizard(self.s, io, self.app.runner).ensure_katapult_config(p):
                io.warn(f"No Katapult config for '{b.label}': the plan will not include it.")
        built = self.build_board(m, b, p, state) if build else None
        io.ok(self._summary(m, b, p, state) + ("" if built is None else
              ("; built" if built else "; NOT built (it will be retried before flashing)")))

    def build_board(self, m: Machine, b: MachineBoard, p: BoardProfile, state: str,
                    force: bool = False) -> bool:
        """Build what this board needs (Katapult only if it has none, then Klipper).
        Builds are kept on disk, so a board can be flashed later, on its own. Nothing is flashed here."""
        io = self.io
        kinds = (["katapult"] if state == "no" else []) + ["klipper"]
        for kind in kinds:
            if not force and load_build(self.s, m.slug, b.label, kind):
                continue
            while True:
                try:
                    res = build_firmware(self.s, self.app.runner, kind=kind, profile=p,
                                         machine_slug=m.slug, label=b.label)
                    io.ok(f"{kind} built ({res.source_version})")
                    break
                except BuildError as exc:
                    io.error(str(exc))
                    if not io.confirm("Try the build again?", default=False):
                        return False
        return True

    def _summary(self, m: Machine, b: MachineBoard, p: BoardProfile, state: str) -> str:
        link = p.interface + (f" {p.can_bitrate}" if p.can_bitrate else "")
        kat = {"yes": "Katapult already there", "no": "Katapult will be built and flashed first",
               "unknown": "Katapult state unknown"}[state]
        return f"{b.label}: {p.name}, {p.mcu}, {link}; {kat}"

    def _ask_katapult(self, m: Machine, b: MachineBoard, p: BoardProfile) -> None:
        io = self.io
        dry = self.app.runner.dry_run
        opts = ["Yes", "No"]
        if not dry:
            opts.append("Not sure: test it now (asks the board for its bootloader)")
        else:
            io.say("  (the test is not available in --dry-run: answer from what you know)")
        i = io.choose(f"Does '{b.label}' already have Katapult?", opts,
                      default=2 if len(opts) == 3 else None)
        if i in (0, 1):
            self.app.reg.set_katapult(m.slug, b.label, "yes" if i == 0 else "no")
        elif i == 2:
            result = self.app.flasher.active_katapult_test(m, b, p)
            if result in ("yes", "no"):
                self.app.reg.set_katapult(m.slug, b.label, result)
            else:
                io.warn("The test did not settle it.")
                j = io.choose(f"Does '{b.label}' already have Katapult?", ["Yes", "No"])
                if j is not None:
                    self.app.reg.set_katapult(m.slug, b.label, "yes" if j == 0 else "no")

    # -- plan, build, flash --------------------------------------------------------------
    def plan_and_run(self, m: Machine, rebuild: bool = False) -> None:
        """Plan, make sure the builds exist, then flash. `rebuild` forces fresh builds (use it when
        updating an existing machine: Klipper may have changed since the last build)."""
        io = self.io
        while True:
            for b in m.boards:                     # per board; known answers are skipped
                self.prepare_board(m, b)
            plan = self.app.plan_for(m)
            self.app.show_plan(plan)
            if not plan.has_errors:
                break
            if not self._repair(m, plan):
                return
        m_run = self._select_boards(m)
        if m_run is None:
            return
        if m_run is not m:
            plan = self.app.plan_for(m_run)
            if plan.has_errors:
                self.app.show_plan(plan)
                return
        profiles = self.app.profiles()
        for b in m_run.boards:                     # builds: only missing ones, or all when rebuilding
            if not self.build_board(m, b, profiles[b.profile_id],
                                    self.app.reg.katapult(m.slug, b.label), force=rebuild):
                io.warn(f"'{b.label}' is not built, so it cannot be flashed now.")
                return
        io.ok("Builds ready. Nothing has been written to any board yet.")
        if self.app.runner.dry_run:
            io.warn("--dry-run: nothing is flashed and no service is touched.")
        i = io.choose("What now?", ["Flash now (recommended)",
                                    "Save and exit (flash later: Machines > Flash)"], default=0)
        if i != 0:
            return
        if io.confirm("Flash now?", default=False):
            self.app.run_plan(m_run, phase="flash", save_as=m)

    def _select_boards(self, m: Machine):
        """Which boards to work on: all (default), one, or several. Leaving some out is allowed,
        with a warning about mixed versions. Returns the machine to run (m itself when all) or None."""
        io = self.io
        if len(m.boards) < 2:
            return m
        i = io.choose("Which boards?", [f"All {len(m.boards)} boards (recommended)",
                                        "Only one board", "Several: choose which ones"], default=0)
        if i is None:
            return None
        if i == 0:
            return m
        if i == 1:
            j = io.choose("Which board?", [b.label for b in m.boards])
            if j is None:
                return None
            keep = [m.boards[j]]
        else:
            skip: set = set()
            while True:
                rows = [f"[{'skip' if b.label in skip else 'update'}] {b.label}" for b in m.boards]
                j = io.choose("Select a board to toggle it (0 = done):", rows, cancel="Done")
                if j is None:
                    break
                skip ^= {m.boards[j].label}
            keep = [b for b in m.boards if b.label not in skip]
            if not keep:
                io.warn("You skipped every board: nothing to do.")
                return None
        left = [b.label for b in m.boards if b not in keep]
        if left:
            io.warn(f"Not touching: {', '.join(left)}. They keep their current firmware, so the machine "
                    "will run mixed Klipper versions until you update them.")
        return Machine(name=m.name, can_interface=m.can_interface, boards=keep)

    def _repair(self, m: Machine, plan) -> bool:
        """Errors are never a dead end: offer to fix them here. False = leave."""
        io = self.io
        bad = [b for b in m.boards if any(a.level == ERROR and a.board == b.label for a in plan.advice)]
        targets = bad or list(m.boards)
        opts = [f"Redo the profile of '{b.label}' (pick the board again, then menuconfig)" for b in targets]
        opts += [f"Remove '{b.label}' from this machine" for b in targets]
        opts.append("Leave it for now")
        i = io.choose("The plan has errors. What do you want to do?", opts, default=0 if bad else None,
                      cancel="Leave")
        if i is None or i == len(opts) - 1:
            return False
        n = len(targets)
        if i >= n:
            gone = targets[i - n]
            m.boards = [b for b in m.boards if b.label != gone.label]
            self.app.save(m)
            return bool(m.boards)
        b = targets[i]
        old = self.app.profiles().get(b.profile_id)
        hint = b.chip or (old.mcu if old else "")
        dev = None
        if b.chip:                                # keep checking against what the scan saw
            dev = discover.Device(b.label, "can" if b.uuid else "usb", uuid=b.uuid, serial=b.serial,
                                  chip=b.chip)
            dev.bridge = bool(old and old.is_bridge)
        entry = self.app.browse_catalog(hint)
        wizard = ProfileWizard(self.s, io, self.app.runner)
        profile = wizard.create(entry=entry, device=dev) if entry else wizard.create(device=dev)
        if profile:
            b.profile_id = profile.id
            self.app.save(m)
        return True

