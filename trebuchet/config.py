"""Settings and paths.

Precedence, lowest to highest: defaults < trebuchet.cfg file < environment variables
< command-line arguments.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    return Path(os.path.expanduser("~"))


@dataclass
class Settings:
    klipper_dir: Path = field(default_factory=lambda: _home() / "klipper")
    katapult_dir: Path = field(default_factory=lambda: _home() / "katapult")
    klippy_env: Path = field(default_factory=lambda: _home() / "klippy-env")
    data_dir: Path = field(default_factory=lambda: _home() / "trebuchet_data")
    can_interface: str = "can0"
    klipper_service: str = "klipper"
    printer_cfg: str = ""            # empty = look in the usual places
    moonraker_url: str = "http://127.0.0.1:7125"
    dry_run: bool = False
    check_toolchain: bool = True      # tests turn this off

    # --- derived directories -------------------------------------------------
    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def machines_dir(self) -> Path:
        return self.data_dir / "machines"

    @property
    def catalog_file(self) -> Path:
        return self.data_dir / "catalog.toml"

    @property
    def builds_dir(self) -> Path:
        return self.data_dir / "builds"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def registry_file(self) -> Path:
        return self.data_dir / "registry.json"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "trebuchet.log"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.profiles_dir, self.machines_dir,
                  self.builds_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --- tools ---------------------------------------------------------
    def klippy_python(self) -> str:
        """Python from klippy-env (has python-can); falls back to python3 if missing."""
        cand = self.klippy_env / "bin" / "python"
        return str(cand) if cand.exists() else "python3"

    def flashtool_candidates(self) -> list[Path]:
        # The paths inside Klipper's lib/katapult are guesses: confirm on a
        # real installation (see README).
        return [
            self.katapult_dir / "scripts" / "flashtool.py",
            self.klipper_dir / "lib" / "katapult" / "scripts" / "flashtool.py",
            self.klipper_dir / "lib" / "katapult" / "flashtool.py",
        ]

    def find_flashtool(self) -> Path | None:
        for cand in self.flashtool_candidates():
            if cand.exists():
                return cand
        return None

    def canbus_query_script(self) -> Path:
        return self.klipper_dir / "scripts" / "canbus_query.py"


def load_settings(data_dir: str | None = None, dry_run: bool = False) -> Settings:
    s = Settings()
    env_data = os.environ.get("TREBUCHET_DATA")
    if env_data:
        s.data_dir = Path(env_data).expanduser()
    if data_dir:
        s.data_dir = Path(data_dir).expanduser()

    cfg_path = s.data_dir / "trebuchet.cfg"
    if cfg_path.exists():
        cp = configparser.ConfigParser()
        cp.read(cfg_path)
        if cp.has_section("paths"):
            sec = cp["paths"]
            if "klipper_dir" in sec:
                s.klipper_dir = Path(sec["klipper_dir"]).expanduser()
            if "katapult_dir" in sec:
                s.katapult_dir = Path(sec["katapult_dir"]).expanduser()
            if "printer_cfg" in sec:
                s.printer_cfg = str(Path(sec["printer_cfg"]).expanduser())
            if "klippy_env" in sec:
                s.klippy_env = Path(sec["klippy_env"]).expanduser()
        if cp.has_section("can") and "interface" in cp["can"]:
            s.can_interface = cp["can"]["interface"]
        if cp.has_section("services"):
            if "klipper" in cp["services"]:
                s.klipper_service = cp["services"]["klipper"]
            if "moonraker_url" in cp["services"]:
                s.moonraker_url = cp["services"]["moonraker_url"]

    s.dry_run = dry_run
    return s
