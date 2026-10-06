"""Terminal interface: numbered menus, Enter accepts the recommended option, 0 goes back.

Rules every screen follows (so the program feels the same everywhere):
  - options are numbered 1..N, never letters;
  - 0 always means back / cancel / exit;
  - when the program has a recommendation, it is marked and Enter accepts it;
  - typing text is the last resort, used only when the program cannot know the answer.
"""
from __future__ import annotations

import os
import sys

WIDTH = 72

BANNER = r"""
  _____ ____  _____ ____  _   _  ____ _   _ _____ _____
 |_   _|  _ \| ____| __ )| | | |/ ___| | | | ____|_   _|
   | | | |_) |  _| |  _ \| | | | |   | |_| |  _|   | |
   | | |  _ <| |___| |_) | |_| | |___|  _  | |___  | |
   |_| |_| \_\_____|____/ \___/ \____|_| |_|_____| |_|
"""


class TerminalIO:
    def __init__(self, color: bool | None = None):
        if color is None:
            color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.color = color

    # -- output -------------------------------------------------------------------
    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._c("1", text)

    def dim(self, text: str) -> str:
        return self._c("2", text)

    def good(self, text: str) -> str:
        return self._c("32", text)

    def bad(self, text: str) -> str:
        return self._c("31", text)

    def say(self, text: str = "") -> None:
        print(text)

    def warn(self, text: str) -> None:
        print(self._c("33", "[!] ") + text)

    def error(self, text: str) -> None:
        print(self._c("31", "[x] ") + text)

    def ok(self, text: str) -> None:
        print(self._c("32", "[ok] ") + text)

    def title(self, text: str) -> None:
        bar = "=" * max(len(text) + 4, 40)
        print("\n" + self._c("36", bar))
        print(self._c("36", f"  {text}"))
        print(self._c("36", bar))

    def banner(self, subtitle: str = "") -> None:
        print(self._c("36", BANNER))
        if subtitle:
            print("  " + self.dim(subtitle))

    def rule(self) -> None:
        print(self.dim("  " + "-" * (WIDTH - 2)))

    def table(self, header: tuple, rows: list) -> None:
        cols = [header] + [tuple(str(c) for c in r) for r in rows]
        widths = [max(len(str(r[i])) for r in cols) for i in range(len(header))]
        fmt = "  " + "  ".join(f"{{:<{w}}}" for w in widths)
        print(self.bold(fmt.format(*header)))
        print(self.dim(fmt.format(*("-" * w for w in widths))))
        for r in cols[1:]:
            print(fmt.format(*r))

    # -- input --------------------------------------------------------------------
    eof = False

    def _input(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            self.eof = True
            return "0"

    def ask(self, text: str, default: str = "") -> str:
        """Free text. Use only when the program cannot know the answer."""
        suffix = f" [{default}]" if default else ""
        val = self._input(f"{text}{suffix}: ")
        if self.eof:                      # input ended (Ctrl+D / pipe): never invent an answer
            return default
        return val or default

    def confirm(self, text: str, default: bool = False) -> bool:
        hint = "Y/n" if default else "y/N"
        val = self._input(f"{text} [{hint}]: ").lower()
        if not val:
            return default
        return val in ("y", "yes")

    def pause(self, text: str = "Press Enter to continue") -> None:
        self._input(f"{text} ")

    def choose(self, title: str, options: list, default: int | None = None,
               cancel: str = "Cancel") -> int | None:
        """Numbered list 1..N and 0 to cancel. Returns the 0-based index, or None.
        `default` (0-based) is marked as recommended and chosen by pressing Enter."""
        print(f"\n{title}")
        for i, opt in enumerate(options, 1):
            tag = self.good("  <- recommended") if default == i - 1 else ""
            print(f"  {i}) {opt}{tag}")
        print(f"  0) {cancel}")
        return self._pick(len(options), default)

    def menu(self, title: str, items: list, back: str | None = "Back") -> int | None:
        """Titled menu. Returns the 0-based index of the chosen item, or None for back/exit."""
        self.title(title)
        for i, text in enumerate(items, 1):
            print(f"  {i}) {text}")
        print(f"  0) {back or 'Back'}")
        return self._pick(len(items), None)

    def _pick(self, n: int, default: int | None) -> int | None:
        hint = f"Choose [Enter = {default + 1}]: " if default is not None else "Choose: "
        while True:
            val = self._input(hint).lower()
            if val == "" and default is not None:
                return default
            if val in ("0", "", "q", "b"):
                return None
            if val.isdigit() and 1 <= int(val) <= n:
                return int(val) - 1
            print(f"Invalid option: type just the number of the list (1 to {n}, or 0 to go back).")
