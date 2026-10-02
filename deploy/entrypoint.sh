#!/bin/sh
# Las plataformas cambian a menudo: actualizar yt-dlp en cada arranque mantiene los extractores al día.
if [ "${AUTO_UPDATE_YTDLP:-true}" = "true" ]; then
    pip install --no-cache-dir -q -U "yt-dlp[default]" bgutil-ytdlp-pot-provider || echo "⚠️ No se pudo actualizar yt-dlp; se usa la versión instalada."
fi
exec python bot.py
