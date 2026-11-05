#!/bin/sh
# get-dm-zero.sh — saca el módulo dm-zero del kernel del pendrive. El initrd
# de NutellaBoot no lo trae y cryptsetup lo necesita para abrir BitLocker,
# así que va en un cpio extra.
#
#   ./get-dm-zero.sh /mnt/nb3            pendrive montado (usa DIR/vmlinuz)
#   ./get-dm-zero.sh ruta/al/vmlinuz     el kernel directo
#
# El módulo tiene que ser exactamente el de ese kernel (misma vermagic y
# firmado con la misma clave de build de Ubuntu), así que no se compila: se
# saca del paquete oficial linux-modules-<kver> del archivo de Ubuntu. El
# .deb se verifica contra el sha256 del índice Packages.gz de la serie.
#
# Variables:
#   SERIES=noble   serie de Ubuntu (obligatoria si no se deduce de la versión)
#   MIRROR=url     espejo (por omisión http://archive.ubuntu.com/ubuntu)
#   DEB=ruta       .deb ya descargado, para no bajarlo de nuevo (igual se
#                  verifica)
#
# Deja el módulo, descomprimido, en files/modules/<kver>/dm-zero.ko.

set -e
here=$(cd "$(dirname "$0")" && pwd)
mirror=${MIRROR:-http://archive.ubuntu.com/ubuntu}

die() { echo "$*" >&2; exit 1; }

[ $# -eq 1 ] || die "uso: $0 <vmlinuz | directorio del pendrive>"
kernel=$1
[ -d "$kernel" ] && kernel=$kernel/vmlinuz
[ -f "$kernel" ] || die "no existe $kernel"

# --- 1. Versión del kernel, leída del header del bzImage: el campo
# kernel_version (u16 little endian en 0x20E) apunta a la cadena, contando
# desde 0x200. Se lee byte a byte para no depender del endianness de od.
magic=$(dd if="$kernel" bs=1 skip=514 count=4 2> /dev/null)
[ "$magic" = HdrS ] || die "$kernel no parece un bzImage (falta HdrS)"
# shellcheck disable=SC2046  # se quiere partir: dos números
set -- $(od -An -tu1 -j526 -N2 "$kernel")
off=$(( $1 + $2 * 256 + 512 ))
[ "$off" -gt 512 ] || die "$kernel no trae kernel_version en el header"
# La cadena termina en NUL: "7.0.0-42-generic (buildd@...) #42~24.04.1-Ubuntu SMP ..."
kstr=$(dd if="$kernel" bs=1 skip="$off" count=256 2> /dev/null | tr '\0' '\n' | head -n 1)
echo "kernel: $kstr"

kver=${kstr%% *}                             # 7.0.0-42-generic
base=$(echo "$kver" | sed -n 's/^\([0-9][0-9.]*-[0-9][0-9]*\)-.*/\1/p')   # 7.0.0-42
[ -n "$base" ] || die "no entiendo la versión '$kver'"
flavour=${kver#"$base"-}                     # generic, lowlatency...
# El build: lo que va entre '#' y '-Ubuntu' ("42~24.04.1").
build=$(echo "$kstr" | sed -n 's/.* #\([0-9][^ ]*\)-Ubuntu.*/\1/p')
[ -n "$build" ] || die "no encuentro '#<build>-Ubuntu' en '$kstr'"
pkgver=$base.$build                          # 7.0.0-42.42~24.04.1
suffix=${build#"${build%%[!0-9]*}"}          # ~24.04.1, o vacío

# --- 2. Paquete fuente y serie. Con sufijo ~YY.MM.N es un kernel HWE
# (linux-hwe-<major.minor>) y la serie sale del YY.MM; sin sufijo es el
# kernel propio de la serie (linux) y la serie hay que darla.
case $suffix in
    "~"[0-9]*.[0-9]*)
        major_minor=$(echo "$base" | cut -d. -f1-2)
        src=linux-hwe-$major_minor
        yymm=$(echo "$suffix" | sed 's/^~\([0-9]*\.[0-9]*\).*/\1/')
        case $yymm in
            22.04) guess=jammy ;;
            24.04) guess=noble ;;
            26.04) guess=resolute ;;
            *) guess= ;;
        esac
        series=${SERIES:-$guess}
        [ -n "$series" ] || die "no sé qué serie es $yymm; dala con SERIES=nombre"
        ;;
    "")
        src=linux
        series=$SERIES
        [ -n "$series" ] || die "kernel sin sufijo HWE: da la serie con SERIES=nombre (p. ej. SERIES=noble)"
        ;;
    *)
        die "no entiendo el sufijo '$suffix' del build '$build'"
        ;;
esac

deb=linux-modules-${kver}_${pkgver}_amd64.deb
pool=pool/main/${src%"${src#?}"}/$src/$deb
echo "kver=$kver flavour=$flavour paquete=$pkgver fuente=$src serie=$series"

t=$(mktemp -d)
trap 'rm -rf "$t"' EXIT

# --- 3. El sha256 esperado, del Packages.gz de -updates y -security; si en
# ninguno está, de la serie a secas.
sha=
for dist in "$series-updates" "$series-security" "$series"; do
    if [ "$dist" = "$series" ] && [ -n "$sha" ]; then
        break
    fi
    idx=$t/Packages-$dist.gz
    curl -fsSL -o "$idx" "$mirror/dists/$dist/main/binary-amd64/Packages.gz" \
        || die "no pude bajar el índice de $dist"
    s=$(gzip -dc "$idx" | awk -v f="Filename: $pool" '
        /^$/ { if (hit) exit; sha = "" }
        $0 == f { hit = 1 }
        /^SHA256: / { sha = $2 }
        END { if (hit) print sha }')
    if [ -n "$s" ]; then
        echo "en $dist: sha256 $s"
        [ -z "$sha" ] || [ "$sha" = "$s" ] || die "los índices no coinciden en el sha256"
        sha=$s
    fi
done
[ -n "$sha" ] || die "$deb no aparece en los índices de $series"

if [ -n "$DEB" ]; then
    [ -f "$DEB" ] || die "no existe DEB=$DEB"
    debfile=$DEB
else
    debfile=$t/$deb
    curl -fL -o "$debfile" "$mirror/$pool" || die "no pude bajar $mirror/$pool"
fi
echo "$sha  $debfile" | sha256sum -c - || die "el sha256 de $debfile no calza"

# --- 4. Sacar sólo dm-zero.ko (viene .zst, .xz o sin comprimir).
mkdir "$t/deb" "$t/x"
(cd "$t/deb" && ar x "$debfile")
data=$(find "$t/deb" -name 'data.tar*' | head -n 1)
[ -n "$data" ] || die "el .deb no trae data.tar"
case $data in
    *.zst) unpack="zstd -dc" ;;
    *.xz) unpack="xz -dc" ;;
    *.gz) unpack="gzip -dc" ;;
    *.tar) unpack="cat" ;;
    *) die "no sé abrir $data" ;;
esac
$unpack "$data" | tar -xf - -C "$t/x" --wildcards \
    "*lib/modules/$kver/kernel/drivers/md/dm-zero.ko*"
ko=$(find "$t/x" -name 'dm-zero.ko*' | head -n 1)
[ -n "$ko" ] || die "el paquete no trae dm-zero.ko"

out=$here/files/modules/$kver/dm-zero.ko
mkdir -p "$(dirname "$out")"
case $ko in
    *.zst) zstd -dcq "$ko" > "$out" ;;
    *.xz) xz -dc "$ko" > "$out" ;;
    *) cp "$ko" "$out" ;;
esac

# --- 5. Que de verdad sea para este kernel.
if command -v modinfo > /dev/null; then
    vermagic=$(modinfo -F vermagic "$out")
    sig_key=$(modinfo -F sig_key "$out")
else
    vermagic=$(tr '\0' '\n' < "$out" | sed -n 's/^vermagic=//p' | head -n 1)
    sig_key="(sin modinfo)"
fi
case $vermagic in
    "$kver "*) ;;
    *) die "vermagic '$vermagic' no es de $kver" ;;
esac

echo
echo "--- $out"
ls -l "$out"
echo "vermagic: $vermagic"
echo "sig_key:  $sig_key"
