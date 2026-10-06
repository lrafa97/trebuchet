"""Run external commands with dry-run support and file logging.

In dry-run only commands marked `mutating=True` are suppressed (flashing,
restarting services, copying firmware). Read-only commands (lsusb, ip link,
canbus_query) always run.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass
class Result:
    cmd: list[str]
    returncode: int
    output: str = ""
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    def __init__(self, dry_run: bool = False, log_file: Path | None = None,
                 echo: Callable[[str], None] | None = print):
        self.dry_run = dry_run
        self.log_file = log_file
        self.echo = echo

    # -- logging ---------------------------------------------------------------
    def _log(self, line: str) -> None:
        if not self.log_file:
            return
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass  # logging must never block an operation

    def _say(self, text: str) -> None:
        if self.echo:
            self.echo(text)

    # -- execution --------------------------------------------------------------
    def run(self, cmd: Sequence[str], *, cwd: Path | None = None,
            mutating: bool = False, stream: bool = False,
            timeout: float | None = None) -> Result:
        cmd = [str(c) for c in cmd]
        shown = shlex.join(cmd)

        if mutating and self.dry_run:
            self._say(f"[dry-run] $ {shown}")
            self._log(f"DRY-RUN {shown}")
            return Result(cmd, 0, "", dry_run=True)

        self._log(f"RUN {shown}" + (f" (cwd={cwd})" if cwd else ""))
        try:
            if stream:
                res = self._run_stream(cmd, cwd)
            else:
                cp = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout)
                res = Result(cmd, cp.returncode, (cp.stdout or "") + (cp.stderr or ""))
        except FileNotFoundError:
            res = Result(cmd, 127, f"command not found: {cmd[0]}")
        except subprocess.TimeoutExpired:
            res = Result(cmd, 124, f"timed out ({timeout}s): {shown}")

        self._log(f"EXIT {res.returncode} {shown}")
        if res.returncode != 0:
            tail = "\n".join(res.output.strip().splitlines()[-15:])
            self._log(f"OUTPUT-TAIL\n{tail}")
        return res

    def _run_stream(self, cmd: list[str], cwd: Path | None) -> Result:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        lines: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)
            self._say(line.rstrip("\n"))
        proc.wait()
        return Result(cmd, proc.returncode, "".join(lines))

    def call_interactive(self, cmd: Sequence[str], *, cwd: Path | None = None) -> int:
        """For menuconfig: hand the terminal directly to the process."""
        cmd = [str(c) for c in cmd]
        self._log(f"INTERACTIVE {shlex.join(cmd)}")
        try:
            return subprocess.call(cmd, cwd=cwd)
        except FileNotFoundError:
            self._say(f"command not found: {cmd[0]}")
            return 127
