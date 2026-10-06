"""Build Klipper and Katapult from stored .config files.

Uses the project's own build system (`make KCONFIG_CONFIG=<file> ...`) on a
COPY of the profile's .config, so `olddefconfig` never modifies the stored
file. Artifacts are copied to
<data>/builds/<machine>/<board>/<klipper|katapult>/ with a build.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .profiles import BoardProfile
from .runner import Runner


class BuildError(Exception):
    pass


@dataclass
class BuildResult:
    kind: str
    dest: Path
    artifacts: dict[str, Path] = field(default_factory=dict)
    source_version: str = "unknown"

    @property
    def bin(self) -> Path | None:
        return self.artifacts.get("bin")

    @property
    def uf2(self) -> Path | None:
        return self.artifacts.get("uf2")

    @property
    def deployer(self) -> Path | None:
        return self.artifacts.get("deployer")


def _repo_for(settings: Settings, kind: str) -> Path:
    return settings.klipper_dir if kind == "klipper" else settings.katapult_dir


def _config_for(profile: BoardProfile, kind: str) -> Path:
    return profile.klipper_config if kind == "klipper" else profile.katapult_config


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_dest(settings: Settings, machine_slug: str, label: str, kind: str) -> Path:
    return settings.builds_dir / machine_slug / label / kind


def load_build(settings: Settings, machine_slug: str, label: str, kind: str) -> BuildResult | None:
    """Return the last stored build, if it exists and is complete."""
    dest = build_dest(settings, machine_slug, label, kind)
    meta = dest / "build.json"
    if not meta.exists():
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    arts = {k: dest / name for k, name in info.get("artifacts", {}).items()
            if (dest / name).exists()}
    if not arts:
        return None
    return BuildResult(kind, dest, arts, info.get("source_version", "unknown"))


def build_firmware(settings: Settings, runner: Runner, *, kind: str,
                   profile: BoardProfile, machine_slug: str, label: str) -> BuildResult:
    if kind not in ("klipper", "katapult"):
        raise BuildError(f"unknown firmware type: {kind}")

    repo = _repo_for(settings, kind)
    if not (repo / "Makefile").exists():
        raise BuildError(f"cannot find {kind} in {repo} (Makefile missing)")

    src_cfg = _config_for(profile, kind)
    if not src_cfg.exists():
        raise BuildError(f"profile '{profile.id}' has no {src_cfg.name}")

    if (settings.check_toolchain and profile.family in ("stm32", "rp2040")
            and shutil.which("arm-none-eabi-gcc") is None):
        raise BuildError("the arm-none-eabi-gcc compiler is missing. Install it with:\n"
                         "  sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi "
                         "libnewlib-arm-none-eabi\n(or run install.sh)")

    work = settings.builds_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    cfg = work / f"{machine_slug}-{label}-{kind}.config"
    shutil.copyfile(src_cfg, cfg)

    base = ["make", f"KCONFIG_CONFIG={cfg}"]
    for step, extra in (("clean", ["clean"]), ("olddefconfig", ["olddefconfig"]),
                        ("compile", [f"-j{os.cpu_count() or 2}"])):
        res = runner.run(base + extra, cwd=repo, stream=False)
        if not res.ok:
            tail = "\n".join(res.output.strip().splitlines()[-15:])
            raise BuildError(f"'{step}' failed for {kind} on {label}:\n{tail}")

    out = repo / "out"
    names = {
        "bin": f"{kind}.bin",
        "uf2": f"{kind}.uf2",
        "deployer": "deployer.bin" if kind == "katapult" else None,
    }
    found = {k: out / n for k, n in names.items() if n and (out / n).exists()}
    if kind == "klipper" and "bin" not in found:
        raise BuildError(f"the Klipper build for {label} did not produce out/klipper.bin")
    if not found:
        raise BuildError(f"the {kind} build for {label} produced no artifacts in {out}")

    dest = build_dest(settings, machine_slug, label, kind)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    saved: dict[str, Path] = {}
    for k, src in found.items():
        shutil.copyfile(src, dest / src.name)
        saved[k] = dest / src.name
    shutil.copyfile(cfg, dest / "used.config")

    ver = runner.run(["git", "-C", str(repo), "describe", "--always", "--dirty"])
    version = ver.output.strip() if ver.ok and ver.output.strip() else "unknown"

    (dest / "build.json").write_text(json.dumps({
        "kind": kind, "board": label, "profile": profile.id,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_version": version,
        "artifacts": {k: p.name for k, p in saved.items()},
        "sha256": {p.name: _sha256(p) for p in saved.values()},
    }, indent=2), encoding="utf-8")

    return BuildResult(kind, dest, saved, version)


def edit_config(settings: Settings, runner: Runner, kind: str, target: Path) -> bool:
    """Open the project's menuconfig on `target` (created if missing)."""
    repo = _repo_for(settings, kind)
    if not (repo / "Makefile").exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    rc = runner.call_interactive(["make", f"KCONFIG_CONFIG={target}", "menuconfig"], cwd=repo)
    return rc == 0 and target.exists()
