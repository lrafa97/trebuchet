#!/usr/bin/env bash
# Instalador do Trebuchet para o Raspberry Pi (Raspberry Pi OS / Debian).
# Idempotente: podes voltar a correr. Não instala o serviço Klipper e não mexe em
# instalações de Klipper/Katapult que já existam (a não ser com --update).
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
Uso: ./install.sh [opções]
  --dry-run              mostra o que faria, sem alterar nada
  --yes, -y              não pergunta nada (apt -y)
  --no-apt               não instala pacotes do sistema (já os tens, ou não é Debian)
  --update               faz git pull ao Katapult (e ao Klipper, se foi clonado por este script)
  --with-klipper-source  se ~/klipper não existir, clona só o código-fonte do Klipper
                         (para compilar firmware; NÃO instala o serviço nem o klippy-env)
  --klipper-dir DIR      pasta do Klipper    (por defeito ~/klipper)
  --katapult-dir DIR     pasta do Katapult   (por defeito ~/katapult)
  --allow-root           permite correr como root (não recomendado)
  -h, --help             esta ajuda
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --no-apt) NO_APT=1 ;;
    --update) UPDATE=1 ;;
    --with-klipper-source) WITH_KLIPPER=1 ;;
    --klipper-dir) KLIPPER_DIR="${2:?falta o valor}"; shift ;;
    --katapult-dir) KATAPULT_DIR="${2:?falta o valor}"; shift ;;
    --allow-root) ALLOW_ROOT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opção desconhecida: $1" >&2; usage >&2; exit 2 ;;
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
  [ "$ALLOW_ROOT" = 1 ] || die "Corre como o utilizador normal que usa o Klipper (não root). O script usa sudo quando precisa. (--allow-root para ignorar)"
fi

# Corre um comando como root: direto se já és root, senão com sudo.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then run "$@"; else run sudo "$@"; fi
}

# ---------------------------------------------------------------------------
step "1/6 Python"
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 não encontrado. Instala-o: sudo apt install python3"
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  die "Precisas de Python 3.8 ou mais recente (tens $(python3 -V 2>&1)). O Raspberry Pi OS Bullseye (Python 3.9) e Bookworm servem; o Buster (3.7) já é demasiado antigo."
fi
say "ok: $(python3 -V)"

# ---------------------------------------------------------------------------
step "2/6 Pacotes do sistema"
# Obrigatórios: compilar Klipper/Katapult (stm32/rp2040), flashtool.py (pyserial), DFU, lsusb, ip.
REQUIRED_PKGS=(git make build-essential libncurses-dev
               gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi
               dfu-util usbutils iproute2 python3 python3-serial)
# Opcionais (não falham a instalação): stm32flash (1.º Katapult por UART), python3-can
# (canbus_query.py quando não há klippy-env).
OPTIONAL_PKGS=(stm32flash python3-can)

if [ "$NO_APT" = 1 ]; then
  say "saltado (--no-apt)"
elif ! command -v apt-get >/dev/null 2>&1 || ! command -v dpkg >/dev/null 2>&1; then
  warn "apt-get/dpkg não encontrados: instala à mão: ${REQUIRED_PKGS[*]}"
else
  missing=()
  for p in "${REQUIRED_PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  APT_Y=()
  if [ "$ASSUME_YES" = 1 ]; then APT_Y=(-y); fi
  if [ ${#missing[@]} -eq 0 ]; then
    say "ok: pacotes obrigatórios já instalados"
  else
    say "a instalar: ${missing[*]}"
    as_root apt-get update
    as_root apt-get install ${APT_Y[@]+"${APT_Y[@]}"} "${missing[@]}"
  fi
  for p in "${OPTIONAL_PKGS[@]}"; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
      as_root apt-get install ${APT_Y[@]+"${APT_Y[@]}"} "$p" || warn "pacote opcional '$p' não instalado (continua)"
    fi
  done
fi

# ---------------------------------------------------------------------------
step "3/6 Katapult (${KATAPULT_DIR})"
if [ -f "${KATAPULT_DIR}/Makefile" ]; then
  say "já existe: não toco (usa --update para atualizar)"
  if [ "$UPDATE" = 1 ] && [ -d "${KATAPULT_DIR}/.git" ]; then
    run git -C "${KATAPULT_DIR}" pull --ff-only
  fi
else
  run git clone --depth 1 https://github.com/Arksine/katapult "${KATAPULT_DIR}"
fi

# ---------------------------------------------------------------------------
step "4/6 Klipper (${KLIPPER_DIR})"
if [ -f "${KLIPPER_DIR}/Makefile" ]; then
  say "já existe: não toco"
elif [ "$WITH_KLIPPER" = 1 ]; then
  run git clone --depth 1 https://github.com/Klipper3d/klipper "${KLIPPER_DIR}"
  warn "Só o código-fonte, para compilar firmware. Não há serviço nem klippy-env: se quiseres o Klipper completo, instala-o com o KIAUH (apaga ${KLIPPER_DIR} antes, para o KIAUH poder clonar)."
else
  warn "Klipper não encontrado em ${KLIPPER_DIR}. Instala-o com o KIAUH (https://github.com/dw-0/kiauh) ou volta a correr com --with-klipper-source."
fi

# ---------------------------------------------------------------------------
step "5/6 Trebuchet"
run mkdir -p "${BIN_DIR}"
run chmod +x "${TREB_DIR}/trebuchet.sh"
run ln -sf "${TREB_DIR}/trebuchet.sh" "${BIN_DIR}/treb"
run ln -sf "${TREB_DIR}/trebuchet.sh" "${BIN_DIR}/trebuchet"
say "comandos 'treb' e 'trebuchet' em ${BIN_DIR}"
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *) warn "${BIN_DIR} não está no PATH desta sessão. Volta a entrar (logout/login) ou corre: export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac

# Caminhos não-padrão ficam guardados para o programa os usar.
CFG_DIR="${TREBUCHET_DATA:-${HOME}/trebuchet_data}"
if [ "${KLIPPER_DIR}" != "${HOME}/klipper" ] || [ "${KATAPULT_DIR}" != "${HOME}/katapult" ]; then
  if [ ! -f "${CFG_DIR}/trebuchet.cfg" ]; then
    run mkdir -p "${CFG_DIR}"
    if [ "$DRY" = 1 ]; then
      say "[dry-run] escrever ${CFG_DIR}/trebuchet.cfg com klipper_dir=${KLIPPER_DIR} e katapult_dir=${KATAPULT_DIR}"
    else
      printf '[paths]\nklipper_dir = %s\nkatapult_dir = %s\n' "${KLIPPER_DIR}" "${KATAPULT_DIR}" > "${CFG_DIR}/trebuchet.cfg"
    fi
  else
    warn "${CFG_DIR}/trebuchet.cfg já existe: confirma lá os caminhos do Klipper e do Katapult."
  fi
fi

# ---------------------------------------------------------------------------
step "6/6 Permissões"
if [ "$(id -u)" -ne 0 ] && ! id -nG | tr ' ' '\n' | grep -qx dialout; then
  say "a adicionar ${USER:-$(id -un)} ao grupo dialout (acesso às portas série USB)"
  as_root usermod -aG dialout "${USER:-$(id -un)}"
  warn "Sai e volta a entrar na sessão para o grupo ter efeito."
else
  say "ok"
fi

say ""
say "Instalado. O CAN (can0) não é configurado por este script: depende da tua máquina."
say "Verifica o resultado com:  treb doctor    (ou ${TREB_DIR}/trebuchet.sh doctor)"
if [ "$DRY" = 1 ]; then
  say "(modo --dry-run: nada foi alterado)"
else
  "${TREB_DIR}/trebuchet.sh" --data-dir "${CFG_DIR}" doctor || true
fi
