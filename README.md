# Trebuchet

**Build and flash Klipper and Katapult on your boards, in the right order, without typing commands.**

A numbered, KIAUH-style terminal menu for the Raspberry Pi. It looks at what is connected, helps you
say which board each one is, builds Katapult only where it is missing, builds Klipper, and then
flashes (CAN bridge last), checking after every flash.

**Status: v0.2. It has never run against real hardware.** It is tested with parsers, simulated
repositories, scripted sessions and `--dry-run`. Before touching a client's machine, do the first
real run on a bench board that is not a client's, with `treb --dry-run` first.

## Install (Raspberry Pi 3, 4, 5; Bullseye or newer)

```bash
git clone https://github.com/lrafa97/trebuchet.git ~/trebuchet
cd ~/trebuchet
./install.sh --dry-run     # shows what it would do, changes nothing
./install.sh               # installs
treb doctor                # checks tools and paths
treb                       # opens the menu
```

Run it as the normal user that runs Klipper (not root); the script uses `sudo` when needed.
Update Trebuchet with `cd ~/trebuchet && git pull && ./install.sh`. Remove it with `./uninstall.sh`
(your data, Klipper and Katapult are left alone). Commands: `treb` and `trebuchet` are the same program.

Needs Python 3.8 or newer (Bullseye has 3.9). No pip packages. `install.sh` installs the ARM
compiler, `dfu-util`, `usbutils`, `iproute2`, `python3-serial`, and clones Katapult to `~/katapult`
if missing. It does not install Klipper, set up `can0`, or touch existing installs.
Options: `--dry-run --yes --no-apt --update --with-klipper-source --klipper-dir DIR --katapult-dir DIR`.
Other paths go in `~/trebuchet_data/trebuchet.cfg`:

```ini
[paths]
klipper_dir = /home/pi/klipper
katapult_dir = /home/pi/katapult
klippy_env = /home/pi/klippy-env
[can]
interface = can0
```

## Using it

```
  Klipper    v0.12.0-...        Katapult   v0.0.1-...
  Printer    standby            can0       up, 500000

  1) Update a machine            scan, identify, plan, build, flash
  2) Machines
  3) Board profiles
  4) Detect connected devices    read-only
  5) First-Katapult guides
  6) Doctor
  0) Exit
```

Rules every screen follows: options are numbered, `0` goes back, Enter accepts the recommended
option. Text is typed only when the program cannot know the answer.

**Update a machine** is the normal path:

1. It scans: USB (`/dev/serial/by-id`), DFU/BOOTSEL, `can0`, Moonraker (read-only), and
   `canbus_query` if you allow it. A `printer.cfg` is optional.
2. For each board found you pick which board it is. Suggestions come first: boards recognised from
   last time (by CAN UUID or USB serial), saved profiles with the same MCU and interface, then boards
   from Klipper's own list ranked by how many pins match your `printer.cfg`. If none fits, browse
   by brand (only boards with that MCU) or create a profile from scratch.
3. Creating a profile opens Klipper's own menuconfig. MCU, interface and CAN speed are **read from
   the resulting `.config`**, not typed again, and checked against what the board reports.
   Typed input: the machine name, and the board name only when starting from scratch.
4. Boards without Katapult get a Katapult menuconfig; Trebuchet checks that Katapult's application
   offset equals Klipper's.
5. It shows the plan and warnings, then builds everything first, and only then asks to flash.
   It stops at the first failure.

Other commands: `treb update`, `treb doctor`, `treb scan`, `treb discover [--canbus-query] [--no-moonraker]`,
`treb plan <machine>`. `--dry-run` never flashes and never touches services (builds still run).

## Safety rules

- **Never stops Klipper during a print.** It asks Moonraker for `print_stats.state`; `printing` or
  `paused` blocks the flash. If Moonraker does not answer it does not assume idle: it asks you.
- Katapult is built and flashed only on boards that lack it. State "unknown" blocks the plan until
  you answer or run the active test.
- `flashtool.py -q` only with one CAN node connected (Katapult's own warning); it asks first.
- Every flash shows board, interface and file and asks for confirmation.
- The CAN bridge is flashed last. That is our own reasoning, not documented.
- Profiles that contradict their `.config` (MCU, interface, CAN speed), or a chip that does not match
  what was seen, block the plan.
- A DFU heater warning is one non-blocking line; disable it per profile with `dfu_heater_warning = false`.

## Scope

STM32 and RP2040. Interfaces: USB, CAN, USB-CAN bridge, UART. AVR, SAM, LPC, RP2350 are out of scope.

## Board list

The catalog is read from the header comment of every `~/klipper/config/generic-*.cfg` and
`sample-*.cfg`, where Klipper says which MCU, bootloader and crystal to compile for. It always
matches your Klipper version. Missing boards: add them to `~/trebuchet_data/catalog.toml`
(see `examples/catalog.toml`). Pin matching only **suggests**: board revisions with the same pins tie.

The MCU shown is what the **running firmware reports** (USB name, Moonraker), not read from the
silicon. A blank board in DFU/BOOTSEL does not report one.

## Files

```
~/trebuchet_data/
  profiles/<id>/profile.toml, klipper.config, katapult.config
  machines/<name>.toml
  builds/<machine>/<board>/<klipper|katapult>/    (+ build.json with version and sha256)
  registry.json        Katapult yes/no, last flash, remembered board identities
  logs/trebuchet.log   every command that was run
```

## Verified and not verified

Verified against real code: Kconfig symbols (`CONFIG_MCU`, `USBSERIAL`, `CANSERIAL`, `USBCANBUS`,
`CANBUS_FREQUENCY`, `FLASH_APPLICATION_ADDRESS`, `LAUNCH_APP_ADDRESS`) using `.config` files produced
by Klipper's and Katapult's own build; `make KCONFIG_CONFIG=... olddefconfig`; the `flashtool.py`
flags used; `install.sh` against real clones.

**Not verified. Confirm before relying on it:**
- Nothing has run on real boards, real CAN, or a full build-and-flash.
- Moonraker response formats (`print_stats`, `mcu_constants.MCU`, `mcu_version`, `CANBUS_BRIDGE`)
  are assumptions tested only with invented responses. If a field is missing the chip shows "?".
- That the `can0` driver of a Klipper USB-CAN bridge is `gs_usb` (read from `/sys/class/net`).
- apt package names; that `dfu-util` needs `sudo`; the `Programming Complete` message and the
  `-q` / `canbus_query.py` output formats.
- That a freshly flashed CAN node shows up in `canbus_query.py` (the post-flash check assumes so).
- USB IDs `0483:df11` (STM32 DFU), `2e8a:0003` (RP2040 BOOTSEL); Katapult can customise them.
- Where `flashtool.py` lives inside Klipper (searched in 3 places, `~/katapult` first).
- Pin-based suggestions were only tested with Klipper's sample configs, not a real `printer.cfg`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

GitHub CI (`.github/workflows/tests.yml`) runs Python 3.8, 3.9, 3.11, 3.13 and shellcheck.

## Licence

Not chosen yet; without one the code is "all rights reserved". Choose before making it public. No
KIAUH code was copied; KIAUH and Katapult are GPL-3.0, so copying code from them would make the
result GPL-3.0.
