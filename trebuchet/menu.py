"""Menus: home screen with status, update flow, machines, profiles, devices, guides, doctor."""
from __future__ import annotations

import grp
import importlib.util
import os
import shutil
import sys

from . import __version__, catalog, detect, discover, printguard
from .advisor import ERROR, INFO, WARN, Plan, advise
from .config import Settings
from .executor import execute_plan
from .flasher import Flasher
from .guides import GUIDES, get_guide
from .profile_wizard import ProfileWizard
from .profiles import (BoardProfile, Machine, MachineBoard, list_machines, list_profiles,
                       save_machine, slugify)
from .registry import Registry
from .runner import Runner
from .ui import TerminalIO
from .update_flow import UpdateFlow

KAT_LABEL = {"yes": "yes", "no": "no", "unknown": "?"}


def _in_group(name: str) -> bool:
    """True if the user belongs to the group (or is root)."""
    if os.geteuid() == 0:
        return True
    try:
        return grp.getgrnam(name).gr_gid in os.getgroups()
    except KeyError:
        return False


class App:
    def __init__(self, settings: Settings, io: TerminalIO | None = None):
        self.s = settings
        self.s.ensure_dirs()
        self.io = io or TerminalIO()
        self.runner = Runner(dry_run=settings.dry_run, log_file=settings.log_file, echo=self.io.say)
        self.reg = Registry(settings.registry_file)
        self.flasher = Flasher(settings, self.runner, self.reg, self.io)

    # ------------------------------------------------------------------ helpers
    def profiles(self) -> dict[str, BoardProfile]:
        return {p.id: p for p in list_profiles(self.s.profiles_dir)}

    def kat_state(self, m: Machine) -> dict[str, str]:
        return {b.label: self.reg.katapult(m.slug, b.label) for b in m.boards}

    def save(self, m: Machine) -> None:
        save_machine(self.s.machines_dir, m)

    def plan_for(self, m: Machine) -> Plan:
        return advise(m, self.profiles(), self.kat_state(m))

    def show_plan(self, plan: Plan) -> None:
        io = self.io
        marks = {ERROR: "[x]", WARN: "[!]", INFO: "[i]"}
        io.say("\nAdvice:")
        if not plan.advice:
            io.say("  (nothing to warn about)")
        for a in plan.advice:
            who = f"[{a.board}] " if a.board else ""
            io.say(f"  {marks[a.level]} {who}{a.text}")
        if plan.steps:
            io.say("\nPlan (in order):")
            for i, st in enumerate(plan.steps, 1):
                io.say(f"  {i:>2}. {st.text}" + ("   (manual)" if st.manual else ""))
        elif plan.has_errors:
            io.say("\nNo plan while there are errors.")

    def run_plan(self, m: Machine, phase: str) -> bool:
        plan = self.plan_for(m)                      # state may have changed since the last look
        if plan.has_errors:
            self.show_plan(plan)
            return False
        return execute_plan(plan, m, self.profiles(), self.s, self.runner, self.reg, self.flasher,
                            self.io, self.save, phase=phase)

    # ------------------------------------------------------------------ status
    def _describe_repo(self, path) -> str:
        if not (path / "Makefile").exists():
            return "not installed"
        res = self.runner.run(["git", "-C", str(path), "describe", "--tags", "--always", "--dirty"],
                              timeout=5)
        return res.output.strip().splitlines()[0] if res.ok and res.output.strip() else "installed"

    def status_lines(self) -> list[str]:
        io = self.io
        kl = self._describe_repo(self.s.klipper_dir)
        kat = self._describe_repo(self.s.katapult_dir)
        pr = printguard.print_state(self.s.moonraker_url, timeout=1.5)
        can = detect.can_status(self.runner, self.s.can_interface)
        can_txt = ("not found" if not can.exists else
                   f"{'up' if can.up else 'down'}" + (f" @ {can.bitrate}" if can.bitrate else ""))
        bad = "not installed"
        return [
            f"  Klipper    {io.bad(kl) if kl == bad else kl}",
            f"  Katapult   {io.bad(kat) if kat == bad else kat}",
            f"  Printer    {io.bad(pr.label) if pr.busy else pr.label}",
            f"  {self.s.can_interface:<10} {can_txt}",
        ]

    # ------------------------------------------------------------------ home
    def main(self) -> None:
        io = self.io
        while True:
            io.banner(f"Klipper & Katapult flash helper  v{__version__}"
                      + ("   [dry-run]" if self.s.dry_run else ""))
            for line in self.status_lines():
                io.say(line)
            io.rule()
            k = io.menu("Main menu", [
                "Update a machine            scan, identify, plan, build, flash",
                "Machines                    saved setups",
                "Board profiles              settings that work, per board",
                "Detect connected devices    read-only",
                "First-Katapult guides",
                "Doctor                      check tools and permissions",
            ], back="Exit")
            if k is None:
                return
            (self.update, self.machines_menu, self.profiles_menu, self.detect_menu,
             self.guides_menu, self.doctor)[k]()

    def update(self) -> None:
        UpdateFlow(self).run()

    # ------------------------------------------------------------------ discovery
    def run_discovery(self, *, ask: bool = True, canbus_query: bool = False,
                      use_moonraker: bool = True) -> "discover.Discovery":
        io = self.io
        if ask:
            canbus_query = io.confirm("Also run canbus_query? (finds CAN nodes that have no id yet; "
                                      "nodes Klipper already uses do not answer)", default=False)
        found = discover.discover(self.s, self.runner, use_moonraker=use_moonraker,
                                  canbus_query=canbus_query)
        io.title("What I found")
        io.say(discover.render(found))
        return found

    def detect_menu(self) -> None:
        self.run_discovery(ask=True)
        self.io.pause()

    # ------------------------------------------------------------------ catalog
    def browse_catalog(self, chip_hint: str = "") -> "catalog.CatalogEntry | None":
        """Brand, then board (only boards with the MCU the board reports, when known)."""
        io = self.io
        entries = catalog.load_catalog(self.s.klipper_dir, self.s.catalog_file)
        usable = [e for e in entries if e.family in ("stm32", "rp2040")]
        if not usable:
            io.warn(f"The catalog is empty: no boards found in {self.s.klipper_dir / 'config'}. "
                    "Create the profile from scratch, or add boards to catalog.toml.")
            return None
        labels = dict(catalog.VENDORS)
        labels[catalog.OTHER] = "Other brands"
        core = discover.chip_core(chip_hint)
        if core:
            io.say(f"Showing only boards with the MCU this board reports ({core}).")
            usable = [e for e in usable if core in {discover.chip_core(c) for c in e.chips}] or usable
        vendors = [v for v in (*(k for k, _ in catalog.VENDORS), catalog.OTHER)
                   if any(e.vendor == v for e in usable)]
        vi = io.choose("Brand:", [f"{labels[v]}  ({sum(1 for x in usable if x.vendor == v)})"
                                  for v in vendors], cancel="My board is not here")
        if vi is None:
            return None
        pool = [x for x in usable if x.vendor == vendors[vi]]
        bi = io.choose("Board:", [f"{x.name}   [{', '.join(x.chips)}]" for x in pool],
                       cancel="My board is not here")
        return None if bi is None else pool[bi]

    # ------------------------------------------------------------------ machines
    def machines_menu(self) -> None:
        while True:
            ms = list_machines(self.s.machines_dir)
            entries = [f"{m.name}  ({len(m.boards)} boards)" for m in ms]
            k = self.io.menu("Machines", entries + ["New machine: scan what is connected"])
            if k is None:
                return
            if k == len(ms):
                UpdateFlow(self).new_machine()
            else:
                self.machine_menu(ms[k])

    def machine_menu(self, m: Machine) -> None:
        while True:
            k = self.io.menu(f"Machine: {m.name}", [
                "Update (plan, build, flash)",
                "Board status",
                "Advice and plan",
                "Build firmware",
                "Flash (uses the saved builds)",
                "Edit boards (add, remove, UUID/serial, Katapult, active test)",
            ])
            if k is None:
                return
            if k == 0:
                UpdateFlow(self).plan_and_run(m)
            elif k == 1:
                self.show_state(m)
            elif k == 2:
                self.show_plan(self.plan_for(m))
            elif k in (3, 4):
                self.run_phase(m, "build" if k == 3 else "flash")
            elif k == 5:
                self.edit_boards(m)

    def show_state(self, m: Machine) -> None:
        profs = self.profiles()
        self.io.say(f"\nMachine '{m.name}'  (CAN: {m.can_interface})")
        self.io.table(("board", "profile", "interface", "id", "Katapult", "flashed Klipper"),
                      [(b.label, b.profile_id, profs[b.profile_id].interface if b.profile_id in profs else "?",
                        (b.uuid or b.serial or b.device or "-")[:15],
                        KAT_LABEL[self.reg.katapult(m.slug, b.label)],
                        self.reg.get(m.slug, b.label).get("klipper_version", "-")) for b in m.boards])

    def run_phase(self, m: Machine, phase: str) -> bool:
        plan = self.plan_for(m)
        self.show_plan(plan)
        if plan.has_errors:
            return False
        what = "build" if phase == "build" else "flash"
        if self.runner.dry_run:
            self.io.warn("--dry-run: nothing is flashed and no service is touched (builds do run).")
        if not self.io.confirm(f"\n{what.capitalize()} now?", default=False):
            return False
        return execute_plan(plan, m, self.profiles(), self.s, self.runner, self.reg, self.flasher,
                            self.io, self.save, phase=phase)

    def edit_boards(self, m: Machine) -> None:
        io = self.io
        while True:
            self.show_state(m)
            k = io.menu("Edit boards", [
                "Add a board", "Remove a board", "Change UUID / serial / device path",
                "Set whether it has Katapult", "Active test: does it have Katapult?",
            ])
            if k is None:
                return
            if k == 0:
                self.add_board(m)
                continue
            if not m.boards:
                continue
            idx = io.choose("Which board?", [b.label for b in m.boards])
            if idx is None:
                continue
            mb = m.boards[idx]
            p = self.profiles().get(mb.profile_id)
            if k == 1:
                if io.confirm(f"Remove '{mb.label}'?"):
                    m.boards.pop(idx)
            elif k == 2:
                mb.uuid = io.ask("CAN UUID", mb.uuid).lower()
                mb.serial = io.ask("USB serial", mb.serial)
                mb.device = io.ask("Device path", mb.device)
            elif k == 3:
                v = io.choose("Does it have Katapult?", ["Yes", "No", "I don't know"])
                if v is not None:
                    self.reg.set_katapult(m.slug, mb.label, ("yes", "no", "unknown")[v])
            elif k == 4 and p:
                self.flasher.active_katapult_test(m, mb, p)
            self.save(m)

    def add_board(self, m: Machine) -> bool:
        """Manual route (the scan is the normal one): pick a saved profile, label and ids."""
        io = self.io
        profs = list(self.profiles().values())
        if not profs:
            io.warn("There are no board profiles yet. Create one first (Board profiles).")
            return False
        idx = io.choose("Which board is it?", [f"{p.name}  [{p.mcu}, {p.interface}]" for p in profs])
        if idx is None:
            return False
        p = profs[idx]
        label = slugify(io.ask("Name of this board in the machine", p.id))
        if m.board(label):
            io.warn("A board with that name already exists.")
            return False
        mb = MachineBoard(label=label, profile_id=p.id)
        if p.is_can:
            mb.uuid = io.ask("CAN UUID (12 hex; empty if you do not know yet)").lower()
        elif p.interface == "usb":
            mb.serial = io.ask("USB serial (last part of the /dev/serial/by-id name; empty if only one)")
        elif p.interface == "uart":
            mb.device = io.ask("UART device path (e.g. /dev/ttyAMA0)")
        if p.is_bridge:
            mb.serial = io.ask("USB serial of the bridge (empty if only one)")
        m.boards.append(mb)
        k = io.choose("Does it already have Katapult?", ["Yes", "No", "I don't know"], default=2)
        self.reg.set_katapult(m.slug, label, ("yes", "no", "unknown")[k if k is not None else 2])
        self.save(m)
        return True

    # ------------------------------------------------------------------ profiles
    def profiles_menu(self) -> None:
        while True:
            k = self.io.menu("Board profiles", ["List profiles", "Create a profile",
                                                "Browse Klipper's list of boards (read-only)"])
            if k is None:
                return
            if k == 0:
                ps = list_profiles(self.s.profiles_dir)
                if not ps:
                    self.io.say("\nNo profiles yet. They are created while you identify your boards, "
                                "or with option 2.")
                for p in ps:
                    self.io.say(f"\n  {p.id}: {p.name}  [{p.mcu}, {p.family}, {p.interface}]"
                                f"  first Katapult: {p.first_katapult_method}")
                    if p.notes:
                        self.io.say(f"     notes: {p.notes}")
                    for pr in p.problems():
                        self.io.warn(f"     {pr}")
            elif k == 1:
                self.create_profile()
            else:
                e = self.browse_catalog()
                if e:
                    self.io.say("\n" + e.name + f"  [{', '.join(e.chips)}]  ({e.source})")
                    for line in e.notes.splitlines():
                        self.io.say(f"  | {line}")
                    self.io.pause()

    def create_profile(self) -> "BoardProfile | None":
        io = self.io
        i = io.choose("Start from:", ["A board in Klipper's list", "Scratch (you type the name; "
                                      "everything else comes from menuconfig)"], default=0)
        if i is None:
            return None
        entry = self.browse_catalog() if i == 0 else None
        return ProfileWizard(self.s, io, self.runner).create(entry=entry)

    # ------------------------------------------------------------------ guides
    def guides_menu(self) -> None:
        keys = list(GUIDES)
        while True:
            idx = self.io.choose("Guide for which method?", keys)
            if idx is None:
                return
            self.io.say("\n" + get_guide(keys[idx]))
            self.io.pause()

    # ------------------------------------------------------------------ doctor
    def doctor(self, interactive: bool = True) -> list[tuple[str, bool, str]]:
        s = self.s
        flashtool = s.find_flashtool()
        checks: list[tuple[str, bool, str]] = [
            ("Python >= 3.8", sys.version_info >= (3, 8), sys.version.split()[0]),
            ("pyserial (python3-serial)", importlib.util.find_spec("serial") is not None,
             "needed by flashtool.py for USB/UART"),
            ("git", shutil.which("git") is not None, ""),
            ("make", shutil.which("make") is not None, ""),
            ("arm-none-eabi-gcc", shutil.which("arm-none-eabi-gcc") is not None,
             "compiler for STM32/RP2040 (see the Klipper docs)"),
            ("dfu-util", shutil.which("dfu-util") is not None, "first Katapult over DFU"),
            ("lsusb", shutil.which("lsusb") is not None, "package usbutils"),
            ("ip", shutil.which("ip") is not None, "can0 state"),
            (f"Klipper in {s.klipper_dir}", (s.klipper_dir / "Makefile").exists(), ""),
            (f"Katapult in {s.katapult_dir}", (s.katapult_dir / "Makefile").exists(),
             "git clone https://github.com/Arksine/katapult"),
            ("flashtool.py", flashtool is not None, str(flashtool) if flashtool else "not found"),
            ("canbus_query.py", s.canbus_query_script().exists(), str(s.canbus_query_script())),
            ("klippy-env python", (s.klippy_env / "bin" / "python").exists(),
             "canbus_query.py needs python-can there; if missing: sudo apt install python3-can"),
            ("sudo", shutil.which("sudo") is not None or os.geteuid() == 0,
             "stop/start Klipper and dfu-util"),
            ("dialout group", _in_group("dialout"),
             "USB serial access without root (sudo usermod -aG dialout $USER, then log in again)"),
        ]
        self.io.title("Doctor")
        for name, ok, note in checks:
            self.io.say(f"  [{'ok' if ok else '--'}] {name}" + (f"   {note}" if note else ""))
        self.io.say(f"\nData in: {s.data_dir}")
        if interactive:
            self.io.pause()
        return checks
