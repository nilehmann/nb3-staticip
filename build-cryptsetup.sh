#!/bin/sh
# build-cryptsetup.sh — compila un cryptsetup estático (x86_64, musl) para
# meterlo en el initramfs. El de Ubuntu 24.04 es 2.7.0 y no abre volúmenes
# BitLocker con la protección suspendida ("clear key"); eso llegó en 2.8.2.
#
#   ./build-cryptsetup.sh              compila en un directorio temporal
#   ./build-cryptsetup.sh DIR          compila en DIR (se puede reusar)
#
# Compila dentro de un contenedor Alpine (podman) y deja el binario, ya
# stripped, en files/cryptsetup.static. Baja el tarball oficial de kernel.org
# y verifica su sha256 (sacado de sha256sums.asc del mismo directorio).
#
# Sin podman (o donde podman no corre): ROOTFS=dir apunta a un minirootfs
# de Alpine ya descomprimido y se usa bwrap en vez del contenedor. Ojo, se
# le instalan paquetes encima.

set -e
here=$(cd "$(dirname "$0")" && pwd)

ver=2.8.8
sha=3acfa685f2dd7fcc832e0b77bc7093aa7da554a51ce8dafbb4138eaa854eee35
tarball=cryptsetup-$ver.tar.xz
url=https://www.kernel.org/pub/linux/utils/cryptsetup/v${ver%.*}/$tarball
image=docker.io/library/alpine:3.24.3

dir=${1:-$(mktemp -d)}
mkdir -p "$dir"
dir=$(cd "$dir" && pwd)

# El tarball, verificado. Si ya está (DIR reusado) no se baja de nuevo.
[ -f "$dir/$tarball" ] || curl -fL -o "$dir/$tarball" "$url"
echo "$sha  $dir/$tarball" | sha256sum -c - || { echo "sha256 no calza" >&2; exit 1; }

# Lo que corre adentro de Alpine. /build es $dir.
cat > "$dir/inner.sh" << 'EOF'
set -e
# Compilador y las bibliotecas, con sus .a:
#  - device-mapper-static: libdevmapper.a (de lvm2)
#  - json-c-dev, popt-static, util-linux-static (libblkid.a, libuuid.a)
#  - openssl-libs-static: backend de crypto; OpenSSL >= 3.2 trae argon2,
#    y si no cryptsetup usa su argon2 interno (no hace falta libargon2)
# Con bwrap apk no puede cambiar dueños de archivos (unix_chkpwd) y termina
# con error aunque instaló todo; ahí se ignora.
apk add --no-cache build-base pkgconf linux-headers xz \
    lvm2-dev device-mapper-static json-c-dev popt-dev popt-static \
    util-linux-dev util-linux-static openssl-dev openssl-libs-static \
    || [ -n "$EN_BWRAP" ]

cd /build
rm -rf src && mkdir src
tar xJf cryptsetup-*.tar.xz -C src --strip-components=1
cd src

# Sólo cryptsetup, estático. Fuera: udev, tokens externos y ssh, manpages
# (asciidoc), traducciones, selinux, chequeo de calidad de contraseñas,
# y los otros binarios (veritysetup, integritysetup).
./configure \
    --disable-shared --enable-static --enable-static-cryptsetup \
    --with-crypto_backend=openssl \
    --disable-udev --disable-ssh-token --disable-external-tokens \
    --disable-asciidoc --disable-nls --disable-selinux \
    --disable-pwquality --disable-passwdqc \
    --disable-veritysetup --disable-integritysetup
make -j"$(nproc)" cryptsetup.static

mkdir -p /build/out
strip -o /build/out/cryptsetup.static cryptsetup.static
/build/out/cryptsetup.static --version
EOF

if [ -n "$ROOTFS" ]; then
    bwrap --unshare-user --uid 0 --gid 0 --bind "$ROOTFS" / \
        --ro-bind /etc/resolv.conf /etc/resolv.conf \
        --proc /proc --dev /dev --tmpfs /tmp --bind "$dir" /build \
        --setenv PATH /usr/sbin:/usr/bin:/sbin:/bin --setenv EN_BWRAP 1 \
        /bin/sh /build/inner.sh
else
    podman run --rm -v "$dir:/build:Z" "$image" sh /build/inner.sh
fi

cp "$dir/out/cryptsetup.static" "$here/files/cryptsetup.static"
echo
echo "--- $here/files/cryptsetup.static (compilado en $dir)"
ls -l "$here/files/cryptsetup.static"
file "$here/files/cryptsetup.static" 2> /dev/null || true
