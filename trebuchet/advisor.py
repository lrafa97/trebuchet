"""Advice and flashing plan.

`advise()` looks at the machine (boards + profiles + Katapult state) and returns
classified warnings and an ordered list of steps.

Plan order (the "bridge last" rule is our own safety reasoning, not something
the Klipper/Katapult documentation defines):
  1. stop the Klipper service
  2. build Katapult (only boards without Katapult), then Klipper (all boards),
     everything before flashing anything
  3. boards without Katapult: first Katapult (manual/ROM), then Klipper right after
  4. boards with Katapult: Klipper
  5. USB-CAN bridge always last
  6. start the Klipper service
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import kconfig
from .discover import chip_matches
from .profiles import (SUPPORTED_FAMILIES, BoardProfile, Machine, MachineBoard)

ERROR, WARN, INFO = "error", "warning", "info"


@dataclass
class Advice:
    level: str
    text: str
    board: str | None = None


@dataclass
class Step:
    kind: str            # stop_service | build_katapult | build_klipper | first_katapult |
                         # query_uuid | flash_klipper | start_service
    text: str
    board: str | None = None
    manual: bool = False


@dataclass
class Plan:
    advice: list[Advice] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(a.level == ERROR for a in self.advice)


def _can_with_katapult_ok(p: BoardProfile) -> bool:
    """Katapult documents CAN only for stm32 F-series and rp2040."""
    return p.family == "rp2040" or (p.family == "stm32" and p.mcu.lower().startswith("stm32f"))


def advise(machine: Machine, profiles: dict[str, BoardProfile],
           katapult_state: dict[str, str]) -> Plan:
    plan = Plan()
    add = lambda lvl, txt, board=None: plan.advice.append(Advice(lvl, txt, board))

    if not machine.boards:
        add(ERROR, "The machine has no boards. Add at least one.")
        return plan

    resolved: list[tuple[MachineBoard, BoardProfile]] = []
    for mb in machine.boards:
        p = profiles.get(mb.profile_id)
        if p is None:
            add(ERROR, f"profile '{mb.profile_id}' does not exist.", mb.label)
            continue
        for prob in p.problems():
            add(ERROR, f"profile '{p.id}': {prob}.", mb.label)
        resolved.append((mb, p))

    can_boards = [(mb, p) for mb, p in resolved if p.is_can]
    bridges = [(mb, p) for mb, p in resolved if p.is_bridge]

    # --- per-board checks ---
    for mb, p in resolved:
        state = katapult_state.get(mb.label, "unknown")

        if p.family not in SUPPORTED_FAMILIES:
            add(ERROR, f"family '{p.family}' is outside the scope of v1 (stm32 and rp2040 only).", mb.label)
            continue
        if mb.chip and chip_matches(p.mcu, mb.chip) is False:
            add(ERROR, f"the chip seen during discovery ({mb.chip}) does not match profile '{p.id}' "
                       f"({p.mcu}): wrong profile for this board. Fix the profile, or delete the "
                       "machine's 'chip' field if the profile text is just written differently.", mb.label)
        kcfg = kconfig.parse_config(p.klipper_config, "klipper") if p.klipper_config.exists() else None
        if p.klipper_config.exists() and kcfg is None:
            add(ERROR, "klipper.config is not a valid Klipper .config (no CONFIG_MCU). "
                       "Re-create it with menuconfig.", mb.label)
        if kcfg:
            for prob in kconfig.check_against_profile(kcfg, family=p.family, mcu=p.mcu,
                                                      interface=p.interface, can_bitrate=p.can_bitrate):
                add(ERROR, f"klipper.config: {prob}.", mb.label)
        if p.katapult_config.exists():
            tcfg = kconfig.parse_config(p.katapult_config, "katapult")
            if tcfg is None:
                add(ERROR, "katapult.config is not a valid Katapult .config. Re-create it.", mb.label)
            else:
                if kcfg and kcfg.mcu_core and tcfg.mcu_core and kcfg.mcu_core != tcfg.mcu_core:
                    add(ERROR, f"katapult.config is for {tcfg.mcu} but klipper.config is for {kcfg.mcu}.",
                        mb.label)
                off = kconfig.check_offsets(kcfg, tcfg)
                if off:
                    add(ERROR, off, mb.label)
        if p.interface == "uart" and not mb.device:
            add(ERROR, "UART board has no device path ('device' field).", mb.label)
        if p.is_can and not _can_with_katapult_ok(p):
            add(WARN, "the Katapult documentation only covers CAN for stm32 F-series and rp2040; "
                      "confirm that this MCU is supported.", mb.label)
        if p.is_can and not mb.uuid and state != "no":
            add(ERROR, "CAN UUID missing. Find it in printer.cfg (canbus_uuid), or connect only this "
                       "board to the bus and use canbus_query.", mb.label)
        if state == "unknown":
            add(ERROR, "Katapult state unknown: declare 'yes' or 'no', or run the active test "
                       "(requests the bootloader and checks what appears; asks for confirmation). "
                       "Without this there is no plan.", mb.label)
        if state == "no":
            if not p.katapult_config.exists():
                add(ERROR, "no Katapult and the profile has no katapult.config.", mb.label)
            add(INFO, f"needs the first Katapult via '{p.first_katapult_method}' "
                      "(manual step on the hardware).", mb.label)
            if p.first_katapult_method == "dfu" and p.dfu_heater_warning:
                add(WARN, "in DFU some boards may switch the heater on: disconnect heaters.", mb.label)
            if p.is_can:
                add(WARN, "new CAN board: connect only this one to the adapter while you find the UUID "
                          "('flashtool -q' is only safe with a single node).", mb.label)

    # --- machine checks ---
    if can_boards:
        rates = {p.can_bitrate for _, p in can_boards if p.can_bitrate}
        if len(rates) > 1:
            add(ERROR, f"different CAN bitrates across profiles ({sorted(rates)}): "
                       "all nodes on a bus must use the same one.")
        if len(bridges) > 1:
            add(WARN, "there is more than one USB-CAN bridge on the machine.")
        if not bridges:
            add(INFO, f"no Klipper bridge: assuming an external USB-CAN adapter with "
                      f"'{machine.can_interface}' already configured.")
    if bridges:
        add(INFO, "the bridge is flashed last; on reboot, Linux brings can0 down "
                  "(use allow-hotplug so it comes back by itself).")

    labels = {mb.label for mb, _ in resolved}
    if len(labels) != len(resolved):
        add(ERROR, "there are duplicate board labels on the machine.")

    only = [mb.label for mb, _ in resolved]
    if len(only) > 1:
        add(INFO, "update all boards on the machine together to avoid mixed versions.")

    # --- plan ---
    if plan.has_errors:
        return plan

    needs_katapult = [(mb, p) for mb, p in resolved if katapult_state.get(mb.label) == "no"]
    ordered = sorted(resolved, key=lambda t: t[1].is_bridge)   # bridge last, rest stays in order

    s = plan.steps
    s.append(Step("stop_service", "Stop the Klipper service"))
    for mb, p in needs_katapult:
        s.append(Step("build_katapult", f"Build Katapult for '{mb.label}' ({p.name})", mb.label))
    for mb, p in ordered:
        s.append(Step("build_klipper", f"Build Klipper for '{mb.label}' ({p.name})", mb.label))

    for mb, p in ordered:
        if katapult_state.get(mb.label) == "no":
            s.append(Step("first_katapult", f"Flash the first Katapult on '{mb.label}' ({p.first_katapult_method})",
                          mb.label, manual=True))
            if p.is_can and not p.is_bridge and not mb.uuid:
                s.append(Step("query_uuid", f"Find the UUID of '{mb.label}' (only this board on the bus)",
                              mb.label, manual=True))
        s.append(Step("flash_klipper", f"Flash Klipper on '{mb.label}'", mb.label))
    s.append(Step("start_service", "Start the Klipper service"))
    return plan
