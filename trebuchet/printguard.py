"""Never stop Klipper under a running print.

Stopping the Klipper service (needed to flash) kills a print in progress. Before doing it we ask
Moonraker for `print_stats.state` (standby / printing / paused / complete / cancelled / error).

If Moonraker does not answer we do NOT assume "idle": the caller decides (the interactive flow
asks the user to confirm, with the reason).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

BUSY_STATES = ("printing", "paused")


@dataclass
class PrintState:
    state: str = ""            # "" = unknown
    filename: str = ""
    reachable: bool = False

    @property
    def busy(self) -> bool:
        return self.state in BUSY_STATES

    @property
    def label(self) -> str:
        if not self.reachable:
            return "unknown (Moonraker not reachable)"
        if self.busy:
            return f"{self.state}" + (f": {self.filename}" if self.filename else "")
        return self.state or "unknown"


def parse_print_stats(text: str) -> PrintState:
    try:
        stats = json.loads(text)["result"]["status"]["print_stats"]
        return PrintState(state=str(stats.get("state", "")), filename=str(stats.get("filename", "") or ""),
                          reachable=True)
    except (ValueError, KeyError, TypeError, AttributeError):
        return PrintState()


def print_state(url: str, timeout: float = 2.0,
                opener: Callable = urllib.request.urlopen) -> PrintState:
    try:
        with opener(f"{url.rstrip('/')}/printer/objects/query?print_stats", timeout=timeout) as resp:
            return parse_print_stats(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return PrintState()
