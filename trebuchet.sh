#!/usr/bin/env bash
# Lançador estilo KIAUH: ./trebuchet.sh   (aceita --dry-run, doctor, scan, plan <máquina>)
cd "$(dirname "$(readlink -f "$0")")" && exec python3 -m trebuchet "$@"
