"""Registo de estado por board (JSON), escrito pelo programa.

Guarda o que o programa sabe depois de ter feito algo: se a board tem Katapult,
que build foi gravada e quando. Não é a fonte de verdade sobre o hardware:
o que está aqui pode estar desatualizado se alguém gravou a board por fora.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .profiles import KATAPULT_STATES


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"machines": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {"machines": {}}
        self._data.setdefault("machines", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    def get(self, machine: str, label: str) -> dict:
        return dict(self._data["machines"].get(machine, {}).get(label, {}))

    def update(self, machine: str, label: str, **fields) -> None:
        entry = self._data["machines"].setdefault(machine, {}).setdefault(label, {})
        entry.update({k: v for k, v in fields.items() if v is not None})
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.save()

    def katapult(self, machine: str, label: str) -> str:
        v = self.get(machine, label).get("katapult", "unknown")
        return v if v in KATAPULT_STATES else "unknown"

    def set_katapult(self, machine: str, label: str, state: str) -> None:
        if state not in KATAPULT_STATES:
            raise ValueError(f"estado inválido: {state}")
        self.update(machine, label, katapult=state)

    def forget_machine(self, machine: str) -> None:
        self._data["machines"].pop(machine, None)
        self.save()
