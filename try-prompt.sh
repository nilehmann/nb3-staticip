#!/bin/sh
# try-prompt.sh — corre files/nb3-staticip en esta terminal, con una placa de
# red y un pendrive de mentira, para probar la pregunta de la IP sin bootear.
# Usa el sh del busybox, que es el del initrd (dnf/apt install busybox).
#
#   ./try-prompt.sh                 MAC sin IP en la tabla: pregunta
#   ./try-prompt.sh 172.17.68.70    MAC con esa IP en la tabla: la ofrece
#
# Gateway, máscara y prefijo se cambian con GW=, MASK= y NET= (NET= vacío:
# sin prefijo). Deja todo en un directorio temporal y lo muestra al final.

here=$(cd "$(dirname "$0")" && pwd)
command -v busybox > /dev/null || { echo "falta busybox" >&2; exit 1; }

t=$(mktemp -d)
mkdir -p "$t/net/eth0/device" "$t/cfg" "$t/run"
mac=aa:bb:cc:dd:ee:01
echo "$mac" > "$t/net/eth0/address"
echo 1 > "$t/net/eth0/carrier"
echo "BOOT_IMAGE=x nbgw=${GW-172.17.68.65} nbmask=${MASK-255.255.255.192} nbnet=${NET-172.17.68.}" > "$t/cmdline"
[ -n "$1" ] && printf '%s\t%s\n' "$mac" "$1" > "$t/cfg/static-ip.conf"

NB_RUN=$t/run NBS_CMDLINE=$t/cmdline NBS_PARAM=$t/param NBS_CONSOLE=/dev/tty \
    NBS_SYS_NET=$t/net NBS_CFGDIR=$t/cfg busybox sh "$here/files/nb3-staticip"

echo
echo "--- $t/param (lo que lee el initramfs)"
cat "$t/param" 2> /dev/null
echo "--- $t/cfg/static-ip.conf"
cat "$t/cfg/static-ip.conf" 2> /dev/null
