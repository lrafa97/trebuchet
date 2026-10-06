"""Create a board profile with as little typing as possible.

What the program knows is read, not asked:
  - MCU, communication interface and CAN speed come from the `.config` that menuconfig produces;
  - the name comes from the board catalog (typed only when creating from scratch);
  - the MCU/interface the board reports while running are compared with the `.config`, so a
    firmware built for the wrong chip or the wrong interface is caught before it is flashed;
  - Katapult's application offset is compared with Klipper's.
Typing is the last resort.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from . import catalog, discover, kconfig
from .builder import edit_config
from .profiles import BoardProfile, save_profile, slugify

RECOMMENDED_METHOD = {"stm32": "dfu", "rp2040": "bootsel"}


class ProfileWizard:
    def __init__(self, settings, io, runner):
        self.s = settings
        self.io = io
        self.run = runner

    # -- public ----------------------------------------------------------------------
    def create(self, entry: Optional[catalog.CatalogEntry] = None,
               device: Optional[discover.Device] = None) -> Optional[BoardProfile]:
        pid_holder: list = []
        try:
            return self._create(entry, device, pid_holder)
        finally:                               # a cancelled attempt must not leave a half-made profile
            for pid in pid_holder:
                d = self.s.profiles_dir / pid
                if d.is_dir() and not (d / "profile.toml").exists():
                    shutil.rmtree(d, ignore_errors=True)

    def _create(self, entry, device, pid_holder) -> Optional[BoardProfile]:
        io = self.io
        name = entry.name if entry else io.ask("Board name (the only thing you have to type)")
        if not name:
            return None
        pid = self._unique_id(slugify(name))
        pid_holder.append(pid)

        want_chip = discover.chip_core(device.chip) if device else ""
        want_iface = discover.profile_interface_for(device) if device else ""
        if entry and len(entry.chips) > 1 and not want_chip:
            i = io.choose("This board comes with different chips. Which one is yours?", list(entry.chips))
            if i is None:
                return None
            want_chip = discover.chip_core(entry.chips[i])
        elif entry and entry.chip and not want_chip:
            want_chip = discover.chip_core(entry.chip)

        if entry:
            io.say("\nWhat Klipper says about this board "
                   f"({Path(entry.source).name}), to guide the menuconfig:")
            for line in entry.notes.splitlines():
                io.say(f"  | {line}")

        target_dir = self.s.profiles_dir / pid
        kcfg_path = target_dir / "klipper.config"
        cfg = self._firmware_config(kcfg_path, "klipper", want_chip, want_iface)
        if cfg is None:
            return None
        if cfg.family not in ("stm32", "rp2040"):
            io.error(f"{cfg.mcu} is outside the scope of this version (only STM32 and RP2040).")
            return None

        method = self._first_katapult_method(cfg.family)
        if method is None:
            return None
        profile = BoardProfile(
            id=pid, name=name, mcu=cfg.mcu_core, family=cfg.family, interface=cfg.interface,
            first_katapult_method=method, can_bitrate=cfg.can_bitrate,
            notes=f"From Klipper's list: {Path(entry.source).name}" if entry else "",
            catalog_id=entry.id if entry else "")
        save_profile(self.s.profiles_dir, profile)
        io.ok(f"Profile '{name}' saved: {cfg.mcu_core}, {cfg.interface}"
              + (f", CAN {cfg.can_bitrate}" if cfg.can_bitrate else ""))
        return profile

    def ensure_katapult_config(self, profile: BoardProfile) -> bool:
        """Needed only for boards that still have no Katapult. True if profile.katapult_config is usable."""
        if profile.katapult_config.exists() and kconfig.parse_config(profile.katapult_config, "katapult"):
            return True
        klip = kconfig.parse_config(profile.klipper_config, "klipper")
        self.io.say(f"\n'{profile.name}' has no Katapult yet, so it needs a Katapult build too.")
        if klip:
            self.io.say(f"In the Katapult menuconfig use the same MCU ({klip.mcu}), the same interface "
                        f"({klip.interface}) and the same application offset as Klipper "
                        f"({klip.app_address:#x}); Trebuchet checks it for you afterwards."
                        if klip.app_address is not None else
                        f"In the Katapult menuconfig use the same MCU ({klip.mcu}) and interface.")
        while True:
            cfg = self._firmware_config(profile.katapult_config, "katapult", profile.mcu, "")
            if cfg is None:
                return False
            off = kconfig.check_offsets(klip, cfg)
            if not off:
                return True
            self.io.error(off)
            profile.katapult_config.unlink()

    # -- internals -------------------------------------------------------------------
    def _unique_id(self, pid: str) -> str:
        if not (self.s.profiles_dir / pid).exists():
            return pid
        n = 2
        while (self.s.profiles_dir / f"{pid}-{n}").exists():
            n += 1
        return f"{pid}-{n}"

    def _firmware_config(self, target: Path, kind: str, want_chip: str,
                         want_iface: str) -> Optional[kconfig.FirmwareConfig]:
        """Produce `target` (menuconfig / current .config / import), read it and check it matches
        what the board reports. Loops until it is consistent or the user cancels."""
        io = self.io
        repo = self.s.klipper_dir if kind == "klipper" else self.s.katapult_dir
        current = repo / ".config"
        while True:
            opts = [f"Open the {kind} menuconfig (recommended)",
                    f"Use the current .config of {repo}" + ("" if current.exists() else " (does not exist)"),
                    "Import a .config file that already works (type its path)"]
            idx = io.choose(f"{kind.capitalize()} firmware settings:", opts, default=0)
            if idx is None:
                return None
            if idx == 0:
                if not edit_config(self.s, self.run, kind, target):
                    io.warn(f"menuconfig did not produce a file (is {repo} installed?).")
                    continue
            elif idx == 1:
                if not current.exists():
                    io.warn("There is no .config there yet.")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(current, target)
            else:
                src = Path(io.ask("Path of the .config").strip()).expanduser()
                if not src.is_file():
                    io.warn("File not found.")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)

            cfg = kconfig.parse_config(target, kind)
            if cfg is None:
                io.warn("That is not a valid Klipper/Katapult .config (no CONFIG_MCU). Try again.")
                continue
            problems = []
            if want_chip and cfg.mcu_core and cfg.mcu_core != want_chip:
                problems.append(f"it is built for {cfg.mcu} but the board reports {want_chip}")
            if kind == "klipper" and want_iface and cfg.interface != want_iface:
                problems.append(f"it makes the board talk over '{cfg.interface or 'nothing'}' but this "
                                f"board is connected as '{want_iface}'")
            if problems:
                for p in problems:
                    io.error(f"This .config does not fit: {p}.")
                io.say("Fix it in menuconfig (Micro-controller Architecture, Processor model, "
                       "Communication interface) and try again.")
                continue
            return cfg

    def _first_katapult_method(self, family: str) -> Optional[str]:
        from .profiles import FIRST_KATAPULT_METHODS
        rec = RECOMMENDED_METHOD.get(family, "other")
        labels = {"dfu": "dfu (STM32 ROM bootloader, USB)", "bootsel": "bootsel (RP2040 mass storage)",
                  "stm32flash": "stm32flash (serial ROM bootloader)", "stlink": "stlink (SWD programmer)",
                  "deployer": "deployer (flash Katapult through Klipper's own bootloader)",
                  "other": "other (manual, see the guides)"}
        opts = [labels[m] for m in FIRST_KATAPULT_METHODS]
        i = self.io.choose("How is the first Katapult flashed on this board?", opts,
                           default=FIRST_KATAPULT_METHODS.index(rec))
        return None if i is None else FIRST_KATAPULT_METHODS[i]
