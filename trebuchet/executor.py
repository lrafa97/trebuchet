"""Execução do plano: constrói tudo primeiro, grava depois, pára à primeira falha."""
from __future__ import annotations

from typing import Callable

from .advisor import Plan, Step
from .builder import BuildError, build_firmware, load_build
from .config import Settings
from .flasher import Flasher
from .profiles import BoardProfile, Machine
from .registry import Registry
from .runner import Runner


def steps_for(plan: Plan, phase: str) -> list[Step]:
    if phase == "build":
        return [s for s in plan.steps if s.kind.startswith("build_")]
    if phase == "flash":
        return [s for s in plan.steps if not s.kind.startswith("build_")]
    return list(plan.steps)


def execute_plan(plan: Plan, machine: Machine, profiles: dict[str, BoardProfile],
                 settings: Settings, runner: Runner, registry: Registry, flasher: Flasher,
                 io, save_machine: Callable[[Machine], None], phase: str = "all") -> bool:
    steps = steps_for(plan, phase)
    stopped_service = False
    done = 0

    def fail(msg: str) -> bool:
        io.error(msg)
        remaining = steps[done:]
        if remaining:
            io.say("\nNada mais foi executado. Passos que ficaram por fazer:")
            for st in remaining:
                io.say(f"  - {st.text}")
        return False

    try:
        for step in steps:
            io.say(f"\n>>> {step.text}")
            mb = machine.board(step.board) if step.board else None
            p = profiles.get(mb.profile_id) if mb else None

            if step.kind == "stop_service":
                r = flasher.stop_service()
                stopped_service = r.ok or stopped_service
                if not r.ok:
                    return fail(r.message)

            elif step.kind in ("build_katapult", "build_klipper"):
                kind = step.kind.split("_", 1)[1]
                try:
                    res = build_firmware(settings, runner, kind=kind, profile=p,
                                         machine_slug=machine.slug, label=mb.label)
                except BuildError as exc:
                    return fail(str(exc))
                io.ok(f"{kind} construído ({res.source_version}) em {res.dest}")

            elif step.kind == "first_katapult":
                kat = load_build(settings, machine.slug, mb.label, "katapult")
                if kat is None:
                    return fail(f"não há build do Katapult para '{mb.label}'. Constrói primeiro.")
                r = flasher.first_katapult(machine, mb, p, kat)
                if not r.ok:
                    return fail(f"'{mb.label}': {r.message}")
                io.ok(r.message)

            elif step.kind == "query_uuid":
                r = flasher.query_uuid(machine, mb)
                if not r.ok:
                    return fail(f"'{mb.label}': {r.message}")
                if r.data:
                    mb.uuid = r.data
                    save_machine(machine)
                io.ok(r.message)

            elif step.kind == "flash_klipper":
                build = load_build(settings, machine.slug, mb.label, "klipper")
                if build is None:
                    return fail(f"não há build do Klipper para '{mb.label}'. Constrói primeiro.")
                r = flasher.flash_klipper(machine, mb, p, build)
                if not r.ok:
                    return fail(f"'{mb.label}': {r.message}")
                io.ok(r.message)

            elif step.kind == "start_service":
                r = flasher.start_service()
                if not r.ok:
                    return fail(r.message)
                stopped_service = False

            done += 1
        return True
    finally:
        if stopped_service:
            if io.confirm("\nO serviço Klipper ficou parado. Arrancá-lo outra vez?", default=True):
                flasher.start_service()
