"""Ponto de entrada: python3 -m trebuchet [--dry-run] [--data-dir DIR] [doctor|scan|plan MÁQUINA]"""
from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .menu import App
from .profiles import list_machines, slugify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="treb", description="Trebuchet: Klipper e Katapult flash helper")
    ap.add_argument("--dry-run", action="store_true",
                    help="não grava boards nem mexe em serviços (as compilações correm)")
    ap.add_argument("--data-dir", help="pasta de dados (por defeito ~/trebuchet_data ou $TREBUCHET_DATA)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="verifica ferramentas e caminhos")
    sub.add_parser("scan", help="lista dispositivos USB e estado do can0")
    dc = sub.add_parser("discover", help="descobre o que está ligado e mostra a ordem de gravação")
    dc.add_argument("--no-moonraker", action="store_true", help="não perguntar ao Moonraker")
    dc.add_argument("--canbus-query", action="store_true",
                    help="correr também o canbus_query (nós CAN sem id atribuído)")
    pl = sub.add_parser("plan", help="mostra o aconselhamento e o plano de uma máquina")
    pl.add_argument("machine")
    args = ap.parse_args(argv)

    app = App(load_settings(args.data_dir, args.dry_run))

    if args.cmd == "doctor":
        checks = app.doctor(interactive=False)
        return 0 if all(ok for _, ok, _ in checks) else 1
    if args.cmd == "scan":
        app.detect_menu()
        return 0
    if args.cmd == "discover":
        found = app.run_discovery(ask=False, canbus_query=args.canbus_query,
                                  use_moonraker=not args.no_moonraker)
        return 0 if found.devices else 1
    if args.cmd == "plan":
        wanted = slugify(args.machine)
        for m in list_machines(app.s.machines_dir):
            if m.slug == wanted or m.name == args.machine:
                plan = app.plan_for(m)
                app.show_plan(plan)
                return 1 if plan.has_errors else 0
        print(f"máquina '{args.machine}' não encontrada", file=sys.stderr)
        return 2

    try:
        app.main()
    except KeyboardInterrupt:
        print("\nInterrompido.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
