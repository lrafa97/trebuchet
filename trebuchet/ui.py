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
        """Lista numerada 1..N e 0 para cancelar; devolve o índice (0-based) ou None."""
        print(f"\n{title}")
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}")
        print("  0) Cancelar")
        return self._pick(len(options))

    def menu(self, title: str, items: list[str], back: str | None = "Voltar") -> int | None:
        """Menu numerado 1..N, com 0 para voltar (ou sair). Devolve o índice (0-based)
        da opção escolhida, ou None para voltar/sair."""
        self.title(title)
        for i, text in enumerate(items, 1):
            print(f"  {i}) {text}")
        print(f"  0) {back or 'Voltar'}")
        return self._pick(len(items))

    def _pick(self, n: int) -> int | None:
        while True:
            val = self._input("Escolhe: ").lower()
            if val in ("0", "", "q", "b"):
                return None
            if val.isdigit() and 1 <= int(val) <= n:
                return int(val) - 1
            print(f"Opção inválida: escreve só o número da lista (1 a {n}, ou 0 para sair).")
