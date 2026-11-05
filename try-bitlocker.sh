#!/bin/sh
# try-bitlocker.sh — corre files/nb3-bitlocker en esta terminal con discos,
# cryptsetup, blkid, udev y mknod de mentira, y muestra lo que haría. Usa el
# sh del busybox, que es el del initrd (dnf/apt install busybox).
#
#   ./try-bitlocker.sh            todos los casos
#   ./try-bitlocker.sh clear      solo uno: none clear tpm nocs off twice mixed
#
# Cada caso arma un directorio temporal: dev/ con los discos (archivos
# vacíos), sys/block/ con los dm que "abre" el cryptsetup falso, y calls con
# las órdenes que el script dio.

here=$(cd "$(dirname "$0")" && pwd)
command -v busybox > /dev/null || { echo "falta busybox" >&2; exit 1; }

# stubs <dir>: los programas falsos. El blkid lee el tipo de types/<disco>; el
# cryptsetup abre si bitlk/<disco> dice "clear" y falla si dice "tpm".
stubs() {
    mkdir -p "$1/bin" "$1/nb3" "$1/types" "$1/bitlk" "$1/sys/block" "$1/dev" "$1/cfg"
    cat > "$1/bin/blkid" << 'EOF'
#!/bin/sh
for a; do d=$a; done
cat "$T/types/${d##*/}" 2> /dev/null
EOF
    cat > "$1/nb3/cryptsetup" << 'EOF'
#!/bin/sh
echo "cryptsetup $*" >> "$T/calls"
case "$1" in
    open)
        for a; do :; done
        name=$a
        for a; do case "$a" in "$T"/dev/*) dev=$a ;; esac; done
        b=${dev##*/}
        key=$(cat)
        [ -n "$key" ] || { echo "no key" >&2; exit 1; }
        if [ "$(cat "$T/bitlk/$b")" != clear ]; then
            echo "No usable keyslot is available." >&2
            exit 1
        fi
        k=0
        while [ -e "$T/sys/block/dm-$k" ]; do k=$((k + 1)); done
        mkdir -p "$T/sys/block/dm-$k/dm" "$T/sys/block/dm-$k/slaves/$b"
        echo "$name" > "$T/sys/block/dm-$k/dm/name"
        echo "253:$k" > "$T/sys/block/dm-$k/dev"
        ;;
    bitlkDump)
        b=${2##*/}
        echo "Info for BITLK device $2."
        echo "Keyslots:"
        if [ "$(cat "$T/bitlk/$b")" = clear ]; then
            echo " 0: VMK  Protection: VMK protected with clear key"
        else
            echo " 0: VMK  Protection: VMK protected with TPM"
        fi
        ;;
esac
EOF
    # shellcheck disable=SC2016 # el $* es del stub
    printf '#!/bin/sh\necho "modprobe $*" >> "$T/calls"\n' > "$1/bin/modprobe"
    # shellcheck disable=SC2016
    printf '#!/bin/sh\necho "udevadm $*" >> "$T/calls"\n' > "$1/bin/udevadm"
    # shellcheck disable=SC2016
    printf '#!/bin/sh\necho "dmsetup $*" >> "$T/calls"\nexit 1\n' > "$1/bin/dmsetup"
    # el nodo de bloque es un archivo con "b major minor"; el ls falso lo
    # muestra como lo haría con un nodo de verdad
    cat > "$1/bin/mknod" << 'EOF'
#!/bin/sh
echo "mknod $*" >> "$T/calls"
[ "$1" = -m ] && shift 2
echo "$2 $3 $4" > "$1"
EOF
    cat > "$1/bin/ls" << 'EOF'
#!/bin/sh
for f; do :; done
if read -r t ma mi < "$f" 2> /dev/null && [ "$t" = b ]; then
    echo "brw-rw---- 1 0 6 $ma, $mi Jan  1 00:00 $f"
else
    exec /bin/ls "$@"
fi
EOF
    chmod +x "$1"/bin/* "$1/nb3/cryptsetup"
}

# disk <dir> <nombre> <tipo> [clear|tpm]
disk() {
    : > "$1/dev/$2"
    echo "$3" > "$1/types/$2"
    [ -n "$4" ] && echo "$4" > "$1/bitlk/$2"
}

run() {
    T=$1 PATH=$1/bin:$PATH NB_RUN=$1/run NBB_CMDLINE=$1/cmdline \
        NBB_CONSOLE=/dev/stdout NBB_CRYPTSETUP=$1/nb3/cryptsetup \
        NBB_BLKID=$1/bin/blkid NBB_DEV=$1/dev NBB_SYS_BLOCK=$1/sys/block \
        NBB_CFGDIR=$1/cfg NBB_PAUSE=0 NBB_TIMEOUT=1 busybox sh "$here/files/nb3-bitlocker"
    echo "(exit $?)"
}

show() {
    echo "  -- calls:"
    sed 's/^/     /' "$1/calls" 2> /dev/null | grep -v -e '^     modprobe' -e '^     udevadm'
    echo "  -- nodes:"
    for n in "$1"/dev/sd?0; do
        [ -e "$n" ] && echo "     ${n##*/} = $(cat "$n")"
    done
    echo "  -- $1/run/bitlocker:"
    sed 's/^/     /' "$1/run/bitlocker" 2> /dev/null
}

case_() {
    echo
    echo "=== $1: $2"
    t=$(mktemp -d)
    stubs "$t"
    echo "BOOT_IMAGE=/vmlinuz boot=nutellaboot quiet" > "$t/cmdline"
    case "$1" in
        none)
            disk "$t" nvme0n1p1 vfat
            disk "$t" nvme0n1p3 ntfs
            run "$t"
            ;;
        clear)
            disk "$t" nvme0n1p1 vfat
            disk "$t" nvme0n1p3 BitLocker clear
            run "$t"
            ;;
        tpm)
            disk "$t" sda3 BitLocker tpm
            run "$t"
            ;;
        nocs)
            disk "$t" nvme0n1p3 BitLocker clear
            rm "$t/nb3/cryptsetup"
            run "$t"
            ;;
        off)
            disk "$t" nvme0n1p3 BitLocker clear
            echo "BOOT_IMAGE=/vmlinuz boot=nutellaboot nb3bitlocker=n" > "$t/cmdline"
            run "$t"
            ;;
        twice)
            disk "$t" nvme0n1p3 BitLocker clear
            run "$t"
            echo "  -- second run:"
            run "$t"
            ;;
        mixed)
            # un /dev/sdz0 ajeno (no debería existir nunca, pero no se pisa)
            echo "b 8 999" > "$t/dev/sdz0"
            disk "$t" nvme0n1p3 BitLocker clear
            disk "$t" sda1 BitLocker tpm
            disk "$t" sdb2 BitLocker clear
            run "$t"
            ;;
    esac
    show "$t"
    rm -rf "$t"
}

desc() {
    case "$1" in
        none) echo "no BitLocker (vfat + ntfs)" ;;
        clear) echo "BitLocker with protection suspended (clear key)" ;;
        tpm) echo "BitLocker protected by TPM" ;;
        nocs) echo "no /nb3/cryptsetup in the initramfs" ;;
        off) echo "nb3bitlocker=n on the kernel line" ;;
        twice) echo "clear key, script run twice (idempotent)" ;;
        mixed) echo "two clear-key volumes, one TPM, foreign /dev/sdz0" ;;
    esac
}

for c in ${1:-none clear tpm nocs off twice mixed}; do
    case_ "$c" "$(desc "$c")"
done
