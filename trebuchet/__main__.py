"""Entry point: python3 -m trebuchet [--dry-run] [--data-dir DIR] [update|doctor|scan|discover|plan MACHINE]"""
from __future__ import annotations

import argparse
import sys

from .config import load_settings
from .menu import App
from .profiles import list_machines, slugify


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="treb", description="Trebuchet: build and flash Klipper and Katapult")
    ap.add_argument("--dry-run", action="store_true",
                    help="nothing is flashed and no service is touched (builds still run)")
    ap.add_argument("--data-dir", help="data folder (default ~/trebuchet_data or $TREBUCHET_DATA)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("update", help="scan, identify, build and flash (same as the first menu entry)")
    sub.add_parser("doctor", help="check tools and paths")
    sub.add_parser("scan", help="list USB devices and the CAN interface state")
    dc = sub.add_parser("discover", help="detect what is connected and show the flash order")
    dc.add_argument("--no-moonraker", action="store_true", help="do not ask Moonraker")
    dc.add_argument("--canbus-query", action="store_true",
                    help="also run canbus_query (CAN nodes without an assigned id)")
    pl = sub.add_parser("plan", help="show the advice and the plan for a machine")
    pl.add_argument("machine")
    args = ap.parse_args(argv)

    app = App(load_settings(args.data_dir, args.dry_run))

    if args.cmd == "update":
        try:
            app.update()
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        return 0
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
        print(f"machine '{args.machine}' not found", file=sys.stderr)
        return 2

    try:
        app.main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
