#!/bin/bash
# Configura la rotación de IPv6 del VPS: el bot usará una dirección distinta del /64 en cada descarga.
# Uso (como root en el VPS):  sudo bash deploy/setup_ipv6.sh [prefijo/64]
set -euo pipefail

PREFIX="${1:-}"
if [ -z "$PREFIX" ]; then
    ADDR=$(ip -6 addr show scope global | awk '/inet6/ {print $2}' | grep -v '^fd' | head -n1)
    if [ -z "$ADDR" ]; then
        echo "❌ Este VPS no tiene IPv6 global. Actívala en el panel de tu proveedor y vuelve a ejecutar."
        exit 1
    fi
    PREFIX=$(python3 -c "import ipaddress,sys; print(ipaddress.IPv6Interface(sys.argv[1]).network.supernet(new_prefix=64) if ipaddress.IPv6Interface(sys.argv[1]).network.prefixlen > 64 else ipaddress.IPv6Interface(sys.argv[1]).network)" "$ADDR")
fi
IFACE=$(ip -6 route show default | awk '{print $5; exit}')
echo "➡️  Prefijo IPv6: $PREFIX (interfaz $IFACE)"

# 1. Permitir que los procesos usen direcciones del bloque que no están asignadas a la interfaz
cat > /etc/sysctl.d/99-ipv6-rotation.conf <<SYS
net.ipv6.ip_nonlocal_bind = 1
SYS
sysctl -q -p /etc/sysctl.d/99-ipv6-rotation.conf

# 2. Ruta local para todo el /64 (persistente con systemd)
cat > /etc/systemd/system/ipv6-rotation.service <<UNIT
[Unit]
Description=Ruta local IPv6 para rotacion de direcciones del bot
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/sbin/ip -6 route replace local $PREFIX dev lo
ExecStop=/sbin/ip -6 route del local $PREFIX dev lo

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ipv6-rotation.service

# 3. Probar dos direcciones aleatorias del bloque
TEST1=$(python3 -c "import ipaddress,random,sys; n=ipaddress.IPv6Network(sys.argv[1]); print(n.network_address+random.randint(2,n.num_addresses-1))" "$PREFIX")
echo "🔎 Probando salida con $TEST1 ..."
if OUT=$(curl -6 -s -m 10 --interface "$TEST1" https://api64.ipify.org); then
    echo "✅ Salida IPv6 funcionando: $OUT"
else
    echo "⚠️  La dirección aleatoria no tiene salida."
    echo "    Tu proveedor entrega el /64 'on-link' (NDP). Instala ndppd para anunciarlo:"
    echo "      apt install -y ndppd && printf 'proxy $IFACE {\\n  rule $PREFIX {\\n    static\\n  }\\n}\\n' > /etc/ndppd.conf && systemctl restart ndppd"
    echo "    y vuelve a ejecutar este script."
    exit 1
fi

echo
echo "👉 Añade esta línea a tu .env y reinicia el bot:"
echo "IPV6_PREFIX=$PREFIX"
