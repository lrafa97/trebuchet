#!/usr/bin/env bash
# Remove só os comandos 'treb' e 'trebuchet'. Não apaga os teus dados
# (~/trebuchet_data), nem o Klipper, nem o Katapult, nem pacotes do sistema.
set -euo pipefail

BIN_DIR="${HOME}/.local/bin"
TREB_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
removed=0

for name in treb trebuchet; do
  link="${BIN_DIR}/${name}"
  if [ -L "${link}" ] && [ "$(readlink -f "${link}")" = "${TREB_DIR}/trebuchet.sh" ]; then
    rm -- "${link}"
    echo "Removido ${link}"
    removed=1
  fi
done
[ "${removed}" = 1 ] || echo "Nada a remover: os comandos em ${BIN_DIR} não apontam para esta instalação."

echo
echo "Ficam intactos (apaga à mão se quiseres):"
echo "  ${TREB_DIR}            (este programa)"
echo "  ${TREBUCHET_DATA:-${HOME}/trebuchet_data}   (perfis, máquinas, builds, registo)"
echo "  ~/katapult e ~/klipper"
