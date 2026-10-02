#!/bin/bash
# Instala Cloudflare WARP (gratis, sin cuenta) en modo proxy SOCKS5 local, como ruta de salida alternativa.
# Uso (como root en un VPS Debian/Ubuntu):  sudo bash deploy/setup_warp.sh
set -euo pipefail

PORT="${WARP_PORT:-40000}"

if ! command -v warp-cli >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y curl gpg lsb-release
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" \
        > /etc/apt/sources.list.d/cloudflare-client.list
    apt-get update -y && apt-get install -y cloudflare-warp
fi

W="warp-cli --accept-tos"
$W registration new 2>/dev/null || $W register 2>/dev/null || true
# Modo proxy: SOLO el tráfico que el bot envíe al puerto local sale por WARP (SSH y Telegram no se tocan)
$W mode proxy 2>/dev/null || $W set-mode proxy
$W proxy port "$PORT" 2>/dev/null || $W set-proxy-port "$PORT"
$W connect
sleep 3

if curl -s -m 10 -x "socks5h://127.0.0.1:$PORT" https://www.cloudflare.com/cdn-cgi/trace | grep -q "warp=on"; then
    echo "✅ WARP activo en socks5h://127.0.0.1:$PORT"
    echo
    echo "👉 Añade esta línea a tu .env y reinicia el bot:"
    echo "WARP_PROXY=socks5h://127.0.0.1:$PORT"
else
    echo "❌ WARP no responde. Revisa: warp-cli status"
    exit 1
fi
