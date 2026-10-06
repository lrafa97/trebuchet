#!/usr/bin/env bash
# Removes only the 'treb' and 'trebuchet' commands. Does not delete your data
# (~/trebuchet_data), nor Klipper, nor Katapult, nor system packages.
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
TREB_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
removed=0

for name in treb trebuchet; do
  link="${BIN_DIR}/${name}"
  if [ -L "${link}" ] && [ "$(readlink -f "${link}")" = "${TREB_DIR}/trebuchet.sh" ]; then
    rm -- "${link}"
    echo "Removed ${link}"
    removed=1
  fi
done
[ "${removed}" = 1 ] || echo "Nothing to remove: the commands in ${BIN_DIR} do not point to this installation."

echo
echo "Left untouched (delete by hand if you want):"
echo "  ${TREB_DIR}            (this program)"
echo "  ${TREBUCHET_DATA:-${HOME}/trebuchet_data}   (profiles, machines, builds, log)"
echo "  ~/katapult and ~/klipper"
