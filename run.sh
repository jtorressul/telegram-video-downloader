#!/bin/bash
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Verificar si existe .env
if [ ! -f .env ]; then
    echo "⚠️ Archivo .env no encontrado. Creando a partir de .env.example..."
    cp .env.example .env
    echo "❗ Por favor edita el archivo .env con tu TELEGRAM_BOT_TOKEN antes de continuar."
    exit 1
fi

# Detectar python virtualenv si existe
if [ -d "venv" ]; then
    PYTHON="./venv/bin/python"
elif [ -d "/home/jonparrow/Proyectos/botnew/venv" ]; then
    PYTHON="/home/jonparrow/Proyectos/botnew/venv/bin/python"
else
    PYTHON="python3"
fi

echo "🚀 Iniciando Bot Multimedia (modo anónimo)..."
exec "$PYTHON" bot.py
