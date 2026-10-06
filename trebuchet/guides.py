"""First-Katapult guides, by method.

The first Katapult can never get onto a board through Katapult itself: it has to be
flashed by a ROM method or by a programmer. The commands below follow the patterns
in Klipper's docs (Bootloaders) and the Katapult README; the ones that depend on
your hardware are marked.
"""
from __future__ import annotations

COMMON = """\
Before flashing Katapult:
 - The Katapult README recommends fully erasing the chip first. That way
   Katapult detects that there is no application and enters the bootloader by itself.
 - Check that katapult.config has the right crystal, interface (USB/CAN/UART),
   CAN bitrate and application offset for THIS board. A wrong offset or
   clock leaves the board unusable without a programmer.
 - Klipper must be compiled with a bootloader offset equal to Katapult's
   "application offset".
"""

GUIDES: dict[str, str] = {
    "dfu": """\
Method: ROM DFU (STM32)
 1. Put the board in DFU (BOOT0 jumper/BOOT button and reset, depending on your board).
    On some boards DFU can turn the heater on: disconnect heaters.
 2. The program checks that the DFU device appears (0483:df11 in lsusb) and runs:
      dfu-util -d 0483:df11 -a 0 -R -D <katapult.bin> -s 0x08000000:leave
    (pattern from Klipper's docs; confirm the flash start address of your
     chip.)
 3. Remove the BOOT0 jumper and reset if the board doesn't start by itself.
 The Katapult README only documents DFU for STM32F042/F072; on other chips check
 ST's AN2606 that ROM DFU exists and that the board exposes it.
""",
    "bootsel": """\
Method: BOOTSEL (RP2040)
 1. Hold BOOT/BOOTSEL and connect the board by USB (or reset it).
 2. A USB drive appears. The program copies katapult.uf2 to it if it finds it
    mounted under /media, /run/media or /mnt; otherwise copy the file yourself.
    Documented alternative: 'make flash' in the Katapult folder with the board in BOOTSEL.
 3. WARNING: flashing Katapult erases Klipper. Klipper has to be flashed
    again through Katapult (it is the next step of the plan).
""",
    "stm32flash": """\
Method: ROM UART (STM32F103)
 1. Connect PA10 (MCU Rx) and PA9 (MCU Tx) to a 3.3 V UART adapter, BOOT0 high,
    BOOT1 low, and reset.
 2. Flash (pattern from Klipper's docs, swapping the file):
      stm32flash -w <katapult.bin> -v -g 0 /dev/ttyAMA0
    On a Raspberry Pi the UART must be the full one (the mini UART doesn't support the
    parity that stm32flash uses).
 3. Set BOOT0 and BOOT1 back to low.
 This step is manual: the program doesn't run stm32flash for you.
""",
    "stlink": """\
Method: ST-Link programmer
 1. Build katapult.bin and flash it with STM32CubeProgrammer (or your OpenOCD
    workflow) at the flash start address (0x08000000 on STM32).
 2. Do a full chip erase before flashing.
 This step is manual: the program doesn't control the ST-Link.
""",
    "deployer": """\
Method: Katapult deployer (replaces an existing bootloader without a programmer)
 1. In Katapult's menuconfig, set 'Build Katapult deployment application' with the
    offset of the EXISTING bootloader. This produces out/deployer.bin besides katapult.bin.
 2. Flash deployer.bin using the current bootloader (SD card, HID, old Katapult).
    At the end the deployer resets the board into Katapult.
 WARNING: a wrong configuration leaves the board unusable and only a programmer
 can recover it. If you are coming from a factory bootloader, back it up first.
 This step is manual.
""",
    "other": """\
Method: other
 Flash Katapult with the method you use on this board and, once the board is in
 Katapult, confirm in the program.
""",
}


def get_guide(method: str) -> str:
    return GUIDES.get(method, GUIDES["other"]) + "\n" + COMMON
