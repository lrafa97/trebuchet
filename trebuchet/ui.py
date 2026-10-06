"""Interface de terminal ao estilo KIAUH: menus numerados, escreves o número e Enter."""
from __future__ import annotations

import os
import sys


class TerminalIO:
    def __init__(self, color: bool | None = None):
        if color is None:
            color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        self.color = color

    # -- saída ------------------------------------------------------------------------
    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

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

    # -- entrada ----------------------------------------------------------------------
    def _input(self, prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            return "q"

    def ask(self, text: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        val = self._input(f"{text}{suffix}: ")
        return val or default

    def confirm(self, text: str, default: bool = False) -> bool:
        hint = "S/n" if default else "s/N"
        val = self._input(f"{text} [{hint}]: ").lower()
        if not val:
            return default
        return val in ("s", "sim", "y", "yes")

    def pause(self, text: str = "Carrega Enter para continuar") -> None:
        self._input(f"{text} ")

    def choose(self, title: str, options: list[str]) -> int | None:
        """Lista numerada; devolve o índice (0-based) ou None se cancelar."""
        print(f"\n{title}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}")
        print("  B) Cancelar")
        while True:
            val = self._input("Escolhe: ").lower()
            if val in ("b", "q", ""):
                return None
            if val.isdigit() and 1 <= int(val) <= len(options):
                return int(val) - 1
            print("Opção inválida.")

    def menu(self, title: str, entries: list[tuple[str, str]],
             back: str | None = "Voltar") -> str:
        """Menu estilo KIAUH. `entries` = [(chave, texto)]. Devolve a chave escolhida
        ('b' para voltar, 'q' para sair)."""
        self.title(title)
        for key, text in entries:
            print(f"  {key}) {text}")
        if back:
            print(f"  B) {back}")
        valid = {k.lower() for k, _ in entries} | ({"b", "q"} if back else {"q"})
        while True:
            val = self._input("Escolhe uma ação: ").lower()
            if val in valid:
                return val
            print("Opção inválida.")
