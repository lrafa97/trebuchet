"""Compilação de Klipper e Katapult a partir de ficheiros .config guardados.

Usa o sistema de build do próprio projeto (`make KCONFIG_CONFIG=<ficheiro> ...`)
sobre uma CÓPIA do .config do perfil, para o `olddefconfig` nunca alterar o
ficheiro guardado. Os artefactos são copiados para
<data>/builds/<máquina>/<board>/<klipper|katapult>/ com um build.json.
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
    source_version: str = "desconhecida"

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
    """Devolve a última build guardada, se existir e estiver completa."""
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
    return BuildResult(kind, dest, arts, info.get("source_version", "desconhecida"))


def build_firmware(settings: Settings, runner: Runner, *, kind: str,
                   profile: BoardProfile, machine_slug: str, label: str) -> BuildResult:
    if kind not in ("klipper", "katapult"):
        raise BuildError(f"tipo de firmware desconhecido: {kind}")

    repo = _repo_for(settings, kind)
    if not (repo / "Makefile").exists():
        raise BuildError(f"não encontro o {kind} em {repo} (falta Makefile)")

    src_cfg = _config_for(profile, kind)
    if not src_cfg.exists():
        raise BuildError(f"o perfil '{profile.id}' não tem {src_cfg.name}")

    if (settings.check_toolchain and profile.family in ("stm32", "rp2040")
            and shutil.which("arm-none-eabi-gcc") is None):
        raise BuildError("falta o compilador arm-none-eabi-gcc. Instala com:\n"
                         "  sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi "
                         "libnewlib-arm-none-eabi\n(ou corre o install.sh)")

    work = settings.builds_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    cfg = work / f"{machine_slug}-{label}-{kind}.config"
    shutil.copyfile(src_cfg, cfg)

    base = ["make", f"KCONFIG_CONFIG={cfg}"]
    for step, extra in (("clean", ["clean"]), ("olddefconfig", ["olddefconfig"]),
                        ("compilar", [f"-j{os.cpu_count() or 2}"])):
        res = runner.run(base + extra, cwd=repo, stream=False)
        if not res.ok:
            tail = "\n".join(res.output.strip().splitlines()[-15:])
            raise BuildError(f"falhou '{step}' do {kind} para {label}:\n{tail}")

    out = repo / "out"
    names = {
        "bin": f"{kind}.bin",
        "uf2": f"{kind}.uf2",
        "deployer": "deployer.bin" if kind == "katapult" else None,
    }
    found = {k: out / n for k, n in names.items() if n and (out / n).exists()}
    if kind == "klipper" and "bin" not in found:
        raise BuildError(f"a build do Klipper para {label} não produziu out/klipper.bin")
    if not found:
        raise BuildError(f"a build do {kind} para {label} não produziu nenhum artefacto em {out}")

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
    version = ver.output.strip() if ver.ok and ver.output.strip() else "desconhecida"

    (dest / "build.json").write_text(json.dumps({
        "kind": kind, "board": label, "profile": profile.id,
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_version": version,
        "artifacts": {k: p.name for k, p in saved.items()},
        "sha256": {p.name: _sha256(p) for p in saved.values()},
    }, indent=2), encoding="utf-8")

    return BuildResult(kind, dest, saved, version)


def edit_config(settings: Settings, runner: Runner, kind: str, target: Path) -> bool:
    """Abre o menuconfig do projeto sobre `target` (criado se não existir)."""
    repo = _repo_for(settings, kind)
    if not (repo / "Makefile").exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    rc = runner.call_interactive(["make", f"KCONFIG_CONFIG={target}", "menuconfig"], cwd=repo)
    return rc == 0 and target.exists()
