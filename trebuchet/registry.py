"""State the program remembers (JSON), written by the program.

  machines/<machine>/<label>   what we did to each board: Katapult yes/no/unknown, last build
  boards/<uuid:..|serial:..>   which profile and label a physical board was identified as, so the
                               next run recognises it without asking again

Not a source of truth about the hardware: it can be out of date if someone flashed a board
outside this program.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .profiles import KATAPULT_STATES


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"machines": {}, "boards": {}}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {"machines": {}, "boards": {}}
        self._data.setdefault("machines", {})
        self._data.setdefault("boards", {})

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
            raise ValueError(f"invalid state: {state}")
        self.update(machine, label, katapult=state)

    def forget_machine(self, machine: str) -> None:
        self._data["machines"].pop(machine, None)
        self.save()

    # -- identities of physical boards -----------------------------------------------
    @staticmethod
    def board_key(uuid: str = "", serial: str = "") -> str:
        """uuid for CAN nodes, USB serial for USB boards. '' when neither is known."""
        if uuid:
            return f"uuid:{uuid.lower()}"
        if serial:
            return f"serial:{serial}"
        return ""

    def remember_board(self, key: str, profile_id: str, label: str) -> None:
        if key:
            self._data["boards"][key] = {"profile": profile_id, "label": label,
                                         "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.save()

    def recall_board(self, key: str) -> dict:
        return dict(self._data["boards"].get(key, {})) if key else {}

    def forget_board(self, key: str) -> None:
        self._data["boards"].pop(key, None)
        self.save()
