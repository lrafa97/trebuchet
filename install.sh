#!/usr/bin/env bash
# Trebuchet installer for the Raspberry Pi (Raspberry Pi OS / Debian).
# Idempotent: you can run it again. It does not install the Klipper service and does not touch
# existing Klipper/Katapult installations (unless --update is given).
set -euo pipefail

TREB_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
KLIPPER_DIR="${HOME}/klipper"
KATAPULT_DIR="${HOME}/katapult"
BIN_DIR="${HOME}/.local/bin"
DRY=0
NO_APT=0
UPDATE=0
WITH_KLIPPER=0
ALLOW_ROOT=0
ASSUME_YES=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]
  --dry-run              show what would be done, without changing anything
  --yes, -y              do not ask anything (apt -y)
  --no-apt               do not install system packages (you already have them, or not Debian)
  --update               git pull Katapult (and Klipper, if it was cloned by this script)
  --with-klipper-source  if ~/klipper does not exist, clone only the Klipper source code
                         (to build firmware; does NOT install the service or the klippy-env)
  --klipper-dir DIR      Klipper directory   (default ~/klipper)
  --katapult-dir DIR     Katapult directory  (default ~/katapult)
  --allow-root           allow running as root (not recommended)
  -h, --help             this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-apt) NO_APT=1 ;;
    --update) UPDATE=1 ;;
    --with-klipper-source) WITH_KLIPPER=1 ;;
    --klipper-dir) KLIPPER_DIR="${2:?missing value}"; shift ;;
    --katapult-dir) KATAPULT_DIR="${2:?missing value}"; shift ;;
    --allow-root) ALLOW_ROOT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[x] %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY" = 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

if [ "$(id -u)" -eq 0 ]; then
  [ "$ALLOW_ROOT" = 1 ] || die "Run as the normal user that uses Klipper (not root). The script uses sudo when needed. (--allow-root to override)"
fi

# Run a command as root: directly if you already are root, otherwise with sudo.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then run "$@"; else run sudo "$@"; fi
}

# ---------------------------------------------------------------------------
step "1/6 Python"
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 not found. Install it: sudo apt install python3"
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  die "Python 3.8 or newer is required (you have $(python3 -V 2>&1)). Raspberry Pi OS Bullseye (Python 3.9) and Bookworm work; Buster (3.7) is too old."
fi
say "ok: $(python3 -V)"

# ---------------------------------------------------------------------------
step "2/6 System packages"
# Required: build Klipper/Katapult (stm32/rp2040), flashtool.py (pyserial), DFU, lsusb, ip.
REQUIRED_PKGS=(git make build-essential libncurses-dev
               gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi
               dfu-util usbutils iproute2 python3 python3-serial)
# Optional (do not fail the installation): stm32flash (first Katapult over UART), python3-can
# (canbus_query.py when there is no klippy-env).
OPTIONAL_PKGS=(stm32flash python3-can)

if [ "$NO_APT" = 1 ]; then
  say "skipped (--no-apt)"
elif ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
  warn "apt-get/dpkg not found: install manually: ${REQUIRED_PKGS[*]}"
else
  missing=()
  for p in "${REQUIRED_PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  APT_Y=()
  if [ "$ASSUME_YES" = 1 ]; then APT_Y=(-y); fi
  if [ ${#missing[@]} -eq 0 ]; then
    say "ok: required packages already installed"
  else
    say "installing: ${missing[*]}"
    as_root apt-get update
    as_root apt-get install ${APT_Y[@]+"${APT_Y[@]}"} "${missing[@]}"
  fi
  for p in "${OPTIONAL_PKGS[@]}"; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
      as_root apt-get install ${APT_Y[@]+"${APT_Y[@]}"} "$p" || warn "optional package '$p' not installed (continuing)"
    fi
  done
fi

# ---------------------------------------------------------------------------
step "3/6 Katapult (${KATAPULT_DIR})"
if [ -f "${KATAPULT_DIR}/Makefile" ]; then
  say "already exists: leaving it alone (use --update to update)"
  if [ "$UPDATE" = 1 ] && [ -d "${KATAPULT_DIR}/.git" ]; then
    run git -C "${KATAPULT_DIR}" pull --ff-only
  fi
else
  run git clone --depth 1 https://github.com/Arksine/katapult "${KATAPULT_DIR}"
fi

# ---------------------------------------------------------------------------
step "4/6 Klipper (${KLIPPER_DIR})"
if [ -f "${KLIPPER_DIR}/Makefile" ]; then
  say "already exists: leaving it alone"
elif [ "$WITH_KLIPPER" = 1 ]; then
  run git clone --depth 1 https://github.com/Klipper3d/klipper "${KLIPPER_DIR}"
  warn "Source code only, to build firmware. There is no service or klippy-env: if you want the full Klipper, install it with KIAUH (delete ${KLIPPER_DIR} first, so KIAUH can clone it)."
else
  warn "Klipper not found in ${KLIPPER_DIR}. Install it with KIAUH (https://github.com/dw-0/kiauh) or run again with --with-klipper-source."
fi

# ---------------------------------------------------------------------------
step "5/6 Trebuchet"
run mkdir -p "${BIN_DIR}"
run chmod +x "${TREB_DIR}/trebuchet.sh"
run ln -sf "${TREB_DIR}/trebuchet.sh" "${BIN_DIR}/treb"
run ln -sf "${TREB_DIR}/trebuchet.sh" "${BIN_DIR}/trebuchet"
say "commands 'treb' and 'trebuchet' in ${BIN_DIR}"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) warn "${BIN_DIR} is not in this session's PATH. Log out and back in, or run: export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# Non-default paths are saved for the program to use.
CFG_DIR="${TREBUCHET_DATA:-${HOME}/trebuchet_data}"
if [ "${KLIPPER_DIR}" != "${HOME}/klipper" ] || [ "${KATAPULT_DIR}" != "${HOME}/katapult" ]; then
  if [ ! -f "${CFG_DIR}/trebuchet.cfg" ]; then
    run mkdir -p "${CFG_DIR}"
    if [ "$DRY" = 1 ]; then
      say "[dry-run] write ${CFG_DIR}/trebuchet.cfg with klipper_dir=${KLIPPER_DIR} and katapult_dir=${KATAPULT_DIR}"
    else
      printf '[paths]\nklipper_dir = %s\nkatapult_dir = %s\n' "${KLIPPER_DIR}" "${KATAPULT_DIR}" > "${CFG_DIR}/trebuchet.cfg"
    fi
  else
    warn "${CFG_DIR}/trebuchet.cfg already exists: check the Klipper and Katapult paths in it."
  fi
fi

# ---------------------------------------------------------------------------
step "6/6 Permissions"
if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -qx dialout; then
  say "adding ${USER:-$(id -un)} to the dialout group (access to USB serial ports)"
  as_root usermod -aG dialout "${USER:-$(id -un)}"
  warn "Log out and back in for the group change to take effect."
else
  say "ok"
fi

say ""
say "Installed. CAN (can0) is not configured by this script: it depends on your machine."
say "Check the result with:  treb doctor    (or ${TREB_DIR}/trebuchet.sh doctor)"
if [ "$DRY" = 1 ]; then
  say "(--dry-run mode: nothing was changed)"
else
  "${TREB_DIR}/trebuchet.sh" --data-dir "${CFG_DIR}" doctor || true
fi
