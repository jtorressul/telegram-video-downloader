#!/bin/bash
set -e

# Change directory to script folder
cd "$(dirname "$0")"

# Create venv if it does not exist
if [ ! -d "venv" ]; then
    echo "📦 Creando entorno virtual..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt
fi

# Run the bot
./venv/bin/python bot.py
