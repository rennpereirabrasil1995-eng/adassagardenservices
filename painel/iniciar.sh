#!/usr/bin/env bash
# Liga o painel. Uso: ./iniciar.sh [porta]
cd "$(dirname "$0")" || exit 1
exec python3 server.py --porta "${1:-8080}"
