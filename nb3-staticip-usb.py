#!/usr/bin/env python3
"""Instala en un pendrive NutellaBoot 3 la entrada de menú "IP fija", para
salas sin DHCP, sin regenerar initrd.img ni la imagen.

Uso:
  nb3-staticip-usb.py <pendrive montado> <red.toml> [--dry-run]

La red de la sala va en el TOML, que es la fuente de las claves NB_NET_* de
nutellaboot.conf (lo que el TOML no trae, sale del conf):
  [red]
  gateway = "192.168.1.1"
  mascara = "255.255.255.0"         # opcional, por defecto 255.255.255.0
  dns     = ["8.8.8.8", "1.1.1.1"]  # opcional, hasta 2
  prefijo = "192.168.1."            # opcional, ver abajo

El prefijo es el atajo para escribir solo el final de la IP al arrancar. Sin
`prefijo` se deduce del gateway y la máscara (los octetos que la máscara fija
enteros: /24 o /26 -> "192.168.1.", /16 -> "192.168."). Hace falta solo para
alargarlo en una red grande (una /16 donde la sala está toda en 172.17.68.x),
y con prefijo = "" no hay atajo.

Los archivos que van al pendrive viven en files/, junto a este script:
  files/nb3-staticip    script del initrd (sh/busybox) -> se empaqueta en
                        nb3-staticip.cpio con el ORDER del initrd
  files/static-ip.conf  tabla MAC -> IP vacía -> se copia si falta
  files/grub-block.cfg  bloque "IP fija" -> se inserta en grub.cfg

grub.cfg y nutellaboot.conf se EDITAN, no se reemplazan: cada pendrive trae
su sede, su servidor y su NB_BOOT_KEY. static-ip.conf nunca se sobrescribe:
las IPs de las máquinas se editan directo en el pendrive.

Se puede correr las veces que haga falta: lo que ya está igual no se toca, y
antes de escribir muestra los cambios y pide confirmación. Hay que volver a
correrlo cuando la autoactualización cambie initrd.img, porque el ORDER del
overlay sale del ORDER de ese initrd, y lo reemplaza.

El sistema final necesita además, en el servidor,
client/stuff/60-postmount.d/81-nm-static.sh (perfil del NetworkManager).
"""

import argparse
import difflib
import gzip
import ipaddress
import lzma
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

FILES = Path(__file__).resolve().parent / "files"
SRC_SCRIPT = FILES / "nb3-staticip"
SRC_TABLE = FILES / "static-ip.conf"
SRC_BLOCK = FILES / "grub-block.cfg"

MARK_BEGIN = "# nb3-staticip inicio"
MARK_END = "# nb3-staticip fin"
CPIO_NAME = "nb3-staticip.cpio"
TABLE_NAME = "static-ip.conf"
ORDER_PATH = "scripts/init-premount/ORDER"
SCRIPT_PATH = "scripts/init-premount/nb3-staticip"
NET_KEYS = ("NB_NET_GW", "NB_NET_MASK", "NB_NET_DNS", "NB_NET_PREFIX")
TOML_KEYS = ("gateway", "mascara", "dns", "prefijo")


class Error(Exception):
    pass


# --- colores ------------------------------------------------------------------

# Solo en una terminal y sin NO_COLOR (https://no-color.org): redirigido a un
# archivo o a un pipe queda texto plano, igual que antes.
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, CYAN = "\033[31m", "\033[32m", "\033[33m", "\033[36m"


def use_color(stream):
    return stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text, *codes, stream=sys.stdout):
    return "".join(codes) + text + RESET if codes and use_color(stream) else text


def paint_diff(diff):
    """Colorea un diff de `unified`: encabezado en negrita, @@ en cian, líneas
    agregadas en verde y quitadas en rojo."""
    out = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("--- "):
            out.append(paint(line.rstrip("\n"), BOLD) + "\n")
        elif line.startswith("@@"):
            out.append(paint(line.rstrip("\n"), CYAN) + "\n")
        elif line.startswith("+"):
            out.append(paint(line.rstrip("\n"), GREEN) + "\n")
        elif line.startswith("-"):
            out.append(paint(line.rstrip("\n"), RED) + "\n")
        else:
            out.append(line)
    return "".join(out)


def warn(msg):
    print(f"{paint('aviso:', YELLOW, BOLD, stream=sys.stderr)} {msg}", file=sys.stderr)


def error(msg):
    print(f"{paint('error:', RED, BOLD, stream=sys.stderr)} {msg}", file=sys.stderr)


# --- validaciones -----------------------------------------------------------


def ipv4_ok(s):
    try:
        ipaddress.IPv4Address(s)
        return True
    except ValueError:
        return False


def mask_prefix(mask):
    """Máscara punteada -> prefijo CIDR; None si no es una máscara contigua."""
    if not ipv4_ok(mask):
        return None
    try:
        net = ipaddress.IPv4Network(f"0.0.0.0/{mask}")
    except ValueError:
        return None
    # 0.0.0.255 lo acepta como hostmask: tiene que ser la máscara misma
    return net.prefixlen if str(net.netmask) == mask else None


def same_net(ip, gw, mask):
    return ipaddress.IPv4Address(ip) in ipaddress.IPv4Network(f"{gw}/{mask}", strict=False)


def derived_prefix(gw, mask):
    """Los octetos que la máscara fija enteros, que toda IP de la red comparte
    con el gateway: 172.17.68.65/26 -> "172.17.68.", 10.0.115.1/23 -> "10.0."."""
    k = min(mask_prefix(mask) // 8, 3)
    return "".join(o + "." for o in gw.split(".")[:k])


def prefix_ok(prefix):
    """De 1 a 3 octetos, terminado en punto: "192.168.1."."""
    if not prefix.endswith("."):
        return False
    octs = prefix[:-1].split(".")
    return 1 <= len(octs) <= 3 and all(o.isdigit() and len(o) <= 3 and int(o) <= 255 for o in octs)


def norm_mac(mac):
    m = mac.lower().replace("-", ":")
    return m if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", m) else None


# --- la red, del TOML ---------------------------------------------------------


def read_net(path):
    """[red] del TOML -> (gateway, máscara, dns "a,b", prefijo). Una clave
    opcional ausente queda en None (se conserva la de nutellaboot.conf); vacía
    ("" o []) queda en "" (se borra del conf)."""
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise Error(f"no existe {path}")
    except tomllib.TOMLDecodeError as e:
        raise Error(f"{path}: {e}")
    extra = sorted(set(data) - {"red"})
    if extra:
        raise Error(f"{path}: sección desconocida: {', '.join(extra)} (solo se admite [red])")
    red = data.get("red")
    if not isinstance(red, dict):
        raise Error(f"{path}: falta la sección [red]")
    extra = sorted(set(red) - set(TOML_KEYS))
    if extra:
        raise Error(f"{path}: clave desconocida en [red]: {', '.join(extra)} "
                    f"(se admiten {', '.join(TOML_KEYS)})")

    def text(key):
        v = red.get(key)
        if v is not None and not isinstance(v, str):
            raise Error(f'{path}: [red] {key} tiene que ir entre comillas, ej.: {key} = "..."')
        return v.strip() if v is not None else None

    gw, mask, prefix = text("gateway"), text("mascara"), text("prefijo")
    dns = red.get("dns")
    if isinstance(dns, str):
        dns = [dns]
    if dns is not None:
        if not isinstance(dns, list) or not all(isinstance(d, str) for d in dns):
            raise Error(f'{path}: [red] dns tiene que ser una lista, ej.: dns = ["8.8.8.8", "1.1.1.1"]')
        dns = [d.strip() for d in dns]
        if len(dns) > 2 or not all(ipv4_ok(d) for d in dns):
            raise Error(f"{path}: [red] dns inválido: {dns} (hasta dos IPs)")
        dns = ",".join(dns)
    if not gw:
        raise Error(f'{path}: falta [red] gateway, ej.: gateway = "192.168.1.1"')
    if not ipv4_ok(gw):
        raise Error(f"{path}: [red] gateway inválido: {gw}")
    if mask and mask_prefix(mask) is None:
        raise Error(f"{path}: [red] mascara inválida: {mask}")
    if prefix and not prefix_ok(prefix):
        raise Error(f"{path}: [red] prefijo inválido: {prefix} (por ejemplo 192.168.1.)")
    return gw, mask, dns, prefix


# --- nutellaboot.conf -------------------------------------------------------


def conf_line_re(key):
    return re.compile(rf"^(?:set )?{re.escape(key)}=(.*)$", re.M)


def conf_get(text, key):
    """La misma lectura que el nb_conf_value del initrd: `set` opcional, sin
    comillas ni CR, primera ocurrencia."""
    m = conf_line_re(key).search(text)
    return m.group(1).translate({ord(c): None for c in "\"'\r"}) if m else None


# --- cpio newc --------------------------------------------------------------

CPIO_MAGICS = (b"070701", b"070702")


def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            raise Error("initrd/cpio truncado")
        buf += chunk
    return bytes(buf)


def skip(f, n, seekable):
    if seekable:
        f.seek(n, os.SEEK_CUR)
    else:
        while n > 0:
            n -= len(read_exact(f, min(n, 1 << 20)))


def cpio_read(f, want=None, seekable=False):
    """Recorre un cpio newc hasta el TRAILER. Devuelve {nombre: (modo, datos)};
    con `want`, solo guarda los datos de esos nombres (los demás se saltan)."""
    entries = {}
    while True:
        hdr = read_exact(f, 110)
        if hdr[:6] not in CPIO_MAGICS:
            raise Error(f"cabecera cpio inválida ({hdr[:6]!r})")
        fields = [int(hdr[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        mode, filesize, namesize = fields[1], fields[6], fields[11]
        name = read_exact(f, namesize)[:-1].decode("utf-8", "replace")
        read_exact(f, (-(110 + namesize)) % 4)
        if name == "TRAILER!!!":
            return entries
        name = name[2:] if name.startswith("./") else name
        if want is None or name in want:
            entries[name] = (mode, read_exact(f, filesize))
        else:
            skip(f, filesize, seekable)
        read_exact(f, (-filesize) % 4)


def cpio_build(entries):
    """entries: [(nombre, modo, datos)] -> bytes. Todo de root, fecha 0 e
    inodos en secuencia: el mismo contenido da siempre los mismos bytes."""
    out = bytearray()
    for ino, (name, mode, data) in enumerate(entries + [("TRAILER!!!", 0, b"")], 1):
        nb = name.encode() + b"\0"
        nlink = 2 if mode & 0o040000 else 1
        vals = [ino, mode, 0, 0, nlink, 0, len(data), 0, 0, 0, 0, len(nb), 0]
        out += b"070701" + "".join(f"{v:08X}" for v in vals).encode() + nb
        out += b"\0" * ((-len(out)) % 4)
        out += data
        out += b"\0" * ((-len(out)) % 4)
    return bytes(out)


# --- initrd.img: el ORDER de init-premount ------------------------------------


def decompressor(f, magic, path, offset):
    """Stream descomprimido del segmento que empieza en `offset`."""
    if magic.startswith(b"\x1f\x8b"):
        return gzip.GzipFile(fileobj=f), None
    if magic.startswith(b"\xfd7zXZ\x00") or magic.startswith(b"\x5d\x00\x00"):
        return lzma.LZMAFile(f), None
    tools = {
        b"\x28\xb5\x2f\xfd": ["zstd", "-dc"],
        b"\x02\x21\x4c\x18": ["lz4", "-dc"],
        b"\x89\x4c\x5a\x4f": ["lzop", "-dc"],
    }
    if magic[:4] == b"\x28\xb5\x2f\xfd":
        try:
            from compression import zstd  # Python 3.14+

            return zstd.ZstdFile(f), None
        except ImportError:
            pass
    cmd = tools.get(magic[:4])
    if cmd is None:
        raise Error(f"formato desconocido en initrd.img, byte {offset} ({magic.hex()})")
    if not shutil.which(cmd[0]):
        raise Error(f"initrd.img viene comprimido y falta {cmd[0]} para leerlo")
    raw = open(path, "rb", buffering=0)
    raw.seek(offset)
    proc = subprocess.Popen(cmd, stdin=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    raw.close()
    return proc.stdout, proc


def initrd_order(path):
    """initrd.img son varios cpio pegados (microcode, firmware...) y el último,
    el principal, comprimido. Devuelve el ORDER de init-premount del último
    segmento que lo trae, o None."""
    order = None
    size = path.stat().st_size
    with open(path, "rb") as f:
        while f.tell() < size:
            pos = f.tell()
            magic = f.read(6)
            f.seek(pos)
            if magic[:4] == b"\0\0\0\0":
                # relleno entre segmentos (los cpio van alineados a 4)
                f.seek(4, os.SEEK_CUR)
                continue
            if magic in CPIO_MAGICS:
                got = cpio_read(f, {ORDER_PATH}, seekable=True)
                if ORDER_PATH in got:
                    order = got[ORDER_PATH][1]
                continue
            # el comprimido es siempre el último segmento
            stream, proc = decompressor(f, magic, path, pos)
            try:
                got = cpio_read(stream, {ORDER_PATH})
            finally:
                if proc:
                    proc.stdout.close()
                    proc.kill()
                    proc.wait()
            if ORDER_PATH in got:
                order = got[ORDER_PATH][1]
            break
    return order


# --- grub.cfg ---------------------------------------------------------------


def first_entry(lines):
    """Título, línea linux e initrd de la primera entrada que no es la nuestra."""
    title = linux = initrd = None
    inside = False
    for line in lines:
        if line.startswith(MARK_BEGIN):
            inside = True
            continue
        if inside:
            inside = not line.startswith(MARK_END)
            continue
        if title is None:
            m = re.match(r"""^\s*menuentry\s+(["'])(.*?)\1""", line)
            if m:
                title = m.group(2)
            continue
        m = re.match(r"^\s*linux\s+(.*?)\s*$", line)
        if m and linux is None:
            linux = m.group(1)
            continue
        m = re.match(r"^\s*initrd\s+(.*?)\s*$", line)
        if m and initrd is None:
            initrd = m.group(1)
        if linux and initrd:
            break
    return title, linux, initrd


def grub_with_block(text, block):
    lines = text.splitlines(keepends=True)
    out = []
    done = False
    inside = False
    for line in lines:
        if line.startswith(MARK_BEGIN):
            inside = True
            if not done:
                out.append(block)
                done = True
            continue
        if inside:
            inside = not line.startswith(MARK_END)
            continue
        if not done and re.match(r"^\s*menuentry\s", line):
            out.append(block + "\n")
            done = True
        out.append(line)
    return "".join(out)


# --- el plan ------------------------------------------------------------------


def unified(old, new, name):
    diff = difflib.unified_diff(old.splitlines(True), new.splitlines(True), n=3)
    body = [d if d.endswith("\n") else d + "\n" for d in list(diff)[2:]]
    return f"--- {name}\n" + "".join(body)


def read_sources():
    for p in (SRC_SCRIPT, SRC_TABLE, SRC_BLOCK):
        if not p.is_file():
            raise Error(f"falta {p}")
    script = SRC_SCRIPT.read_bytes()
    if not script.startswith(b"#!/bin/sh"):
        raise Error(f"{SRC_SCRIPT} no empieza con #!/bin/sh")
    sh = shutil.which("sh")
    if sh and subprocess.run([sh, "-n", str(SRC_SCRIPT)]).returncode != 0:
        raise Error(f"{SRC_SCRIPT} tiene errores de sintaxis")
    block = SRC_BLOCK.read_text()
    blines = block.rstrip("\n").splitlines()
    if not (blines and blines[0].startswith(MARK_BEGIN) and blines[-1].startswith(MARK_END)):
        raise Error(f"{SRC_BLOCK} tiene que empezar con '{MARK_BEGIN}' y terminar con '{MARK_END}'")
    for ph in ("@TITLE@", "@LINUX@", "@INITRD@"):
        if ph not in block:
            raise Error(f"{SRC_BLOCK} no tiene {ph}")
    return script, SRC_TABLE.read_bytes(), block.rstrip("\n") + "\n"


def plan(args):
    dest = Path(args.dest)
    if not dest.is_dir():
        raise Error(f"{dest} no es un directorio")
    for f in ("grub.cfg", "nutellaboot.conf", "initrd.img", "vmlinuz"):
        if not (dest / f).is_file():
            raise Error(f"{dest / f} no existe: ¿es la partición NB3CFG montada?")
    if not args.dry_run and not os.access(dest, os.W_OK):
        raise Error(f"{dest} no se puede escribir (¿montado como solo lectura?)")
    if shutil.disk_usage(dest).free < 256 * 1024:
        raise Error(f"no queda espacio libre en {dest}")

    script, table_tpl, block_tpl = read_sources()
    conf_path, grub_path = dest / "nutellaboot.conf", dest / "grub.cfg"
    cpio_path, table_path = dest / CPIO_NAME, dest / TABLE_NAME
    conf = conf_path.read_text(encoding="utf-8", errors="surrogateescape")
    grub = grub_path.read_text(encoding="utf-8", errors="surrogateescape")

    # valores: TOML > nutellaboot.conf > predeterminados
    cur = {k: conf_get(conf, k) for k in NET_KEYS}
    t_gw, t_mask, t_dns, t_prefix = read_net(args.toml)
    gw = t_gw
    mask = (cur["NB_NET_MASK"] if t_mask is None else t_mask) or "255.255.255.0"
    dns = (cur["NB_NET_DNS"] if t_dns is None else t_dns) or ""
    # lo que viene del conf también se valida: puede haberse editado a mano
    if mask_prefix(mask) is None:
        raise Error(f"máscara inválida en nutellaboot.conf: {mask}")
    if dns and not (len(dns.split(",")) <= 2 and all(ipv4_ok(d) for d in dns.split(","))):
        raise Error(f"DNS inválido en nutellaboot.conf: {dns}")
    # sin prefijo en el TOML, se deduce; con prefijo = "", no hay atajo
    prefix = derived_prefix(gw, mask) if t_prefix is None else t_prefix
    if t_prefix:
        # el prefijo y la red del gateway tienen que solaparse: "172.17.68." es
        # 172.17.68.0/24, que contiene a 172.17.68.64/26 aunque el .0 no esté
        octs = prefix[:-1].split(".")
        pnet = ipaddress.IPv4Network(f"{'.'.join(octs + ['0'] * (4 - len(octs)))}/{8 * len(octs)}")
        if not pnet.overlaps(ipaddress.IPv4Network(f"{gw}/{mask}", strict=False)):
            raise Error(f"el prefijo {prefix} no está en la red de {gw}/{mask}")
    if not dns:
        warn("sin dns en el TOML: el sistema final no va a resolver nombres (a menos que el servidor esté en NB_HOSTS)")
    if "\r" in conf:
        warn("nutellaboot.conf tiene fin de línea de Windows (CRLF): el GRUB podría leer mal los valores")

    p = {"dest": dest, "net": (gw, mask, dns, prefix), "prefix_derived": t_prefix is None,
         "writes": [], "report": [], "diffs": []}

    # 1. nutellaboot.conf: solo las líneas NB_NET_*; lo demás no se toca
    new_conf = conf if conf.endswith("\n") or not conf else conf + "\n"
    touched = []
    for key, val in zip(NET_KEYS, (gw, mask, dns, prefix)):
        old = cur[key]
        if val:
            if old == val:
                continue
            line = f'set {key}="{val}"'
            if old is None:
                new_conf += line + "\n"
                touched.append(f"+{key}")
            else:
                new_conf = conf_line_re(key).sub(line.replace("\\", "\\\\"), new_conf)
                touched.append(f"{key} {old} -> {val}")
        elif old is not None:
            new_conf = re.sub(rf"^(?:set )?{re.escape(key)}=.*(?:\n|$)", "", new_conf, flags=re.M)
            touched.append(f"-{key}")
    if new_conf == conf:
        p["report"].append(("nutellaboot.conf", "ya estaba"))
    else:
        p["report"].append(("nutellaboot.conf", "se actualiza: " + ", ".join(touched)))
        p["writes"].append(("nutellaboot.conf", new_conf.encode("utf-8", "surrogateescape"), True))
        p["diffs"].append(unified(conf, new_conf, "nutellaboot.conf"))

    # 2. grub.cfg
    lines = grub.splitlines(keepends=True)
    begins = sum(ln.startswith(MARK_BEGIN) for ln in lines)
    ends = sum(ln.startswith(MARK_END) for ln in lines)
    if begins != ends or begins > 1:
        raise Error(f"grub.cfg tiene los marcadores '{MARK_BEGIN}'/'{MARK_END}' desparejos; revísalo a mano")
    title, linux, initrd = first_entry(lines)
    if not (title and linux and initrd):
        raise Error("no encontré en grub.cfg una entrada con menuentry, linux e initrd para copiar")
    if re.search(r"(^|\s)ip=", linux):
        raise Error("la primera entrada de grub.cfg ya trae ip=; revisa el grub.cfg a mano")
    block = block_tpl.replace("@TITLE@", title).replace("@LINUX@", linux).replace("@INITRD@", initrd)
    new_grub = grub_with_block(grub, block)
    if new_grub == grub:
        p["report"].append(("grub.cfg", "ya estaba"))
    else:
        p["report"].append(("grub.cfg", "se actualiza el bloque IP fija" if begins else "se agrega el bloque IP fija"))
        p["writes"].append(("grub.cfg", new_grub.encode("utf-8", "surrogateescape"), True))
        p["diffs"].append(unified(grub, new_grub, "grub.cfg"))

    # 3. nb3-staticip.cpio: el script de files/ + el ORDER del initrd
    base = initrd_order(dest / "initrd.img")
    if base and b"nb3-staticip" in base:
        raise Error("el initrd.img ya trae nb3-staticip en su ORDER: no hace falta el overlay")
    entries = [
        ("scripts", 0o040755, b""),
        ("scripts/init-premount", 0o040755, b""),
    ]
    if base:
        order = base.rstrip(b"\n") + b'\n/scripts/init-premount/nb3-staticip "$@"\n' \
            b"[ -e /conf/param.conf ] && . /conf/param.conf\n"
        entries.append((ORDER_PATH, 0o100644, order))
    # sin ORDER en el initrd, el initramfs-tools ordena solo por los prereqs
    entries.append((SCRIPT_PATH, 0o100755, script))
    want = {n: (m, d) for n, m, d in entries}
    have = None
    if cpio_path.is_file():
        try:
            with open(cpio_path, "rb") as f:
                have = cpio_read(f)
            have.pop(".", None)
            # en los directorios solo importa que existan
            have = {n: (m, b"" if m & 0o040000 else d) for n, (m, d) in have.items()}
        except Error:
            have = {}
    if have == want:
        p["report"].append((CPIO_NAME, "ya estaba"))
    else:
        p["report"].append((CPIO_NAME, "se crea" if have is None else "se actualiza (script u ORDER distintos)"))
        p["writes"].append((CPIO_NAME, cpio_build(entries), False))
        nscripts = sum(ln.startswith(b"/scripts") for ln in (base or b"").splitlines())
        p["diffs"].append(
            f"--- {CPIO_NAME} (contenido)\n  {ORDER_PATH}\n  {SCRIPT_PATH}\n"
            + (f"  ORDER = el de initrd.img ({nscripts} scripts) + nb3-staticip\n" if base else "")
        )
    p["want_cpio"] = want

    # 4. static-ip.conf: se revisa, nunca se reescribe
    if table_path.exists():
        p["report"].append((TABLE_NAME, check_table(table_path, gw, mask)))
    else:
        p["report"].append((TABLE_NAME, "se crea (vacía, con instrucciones)"))
        p["writes"].append((TABLE_NAME, table_tpl, False))
    return p


def check_table(path, gw, mask):
    n = bad = 0
    macs, ips = {}, {}
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    for ln, line in enumerate(text.splitlines(), 1):
        f = line.split()
        if not f or f[0].startswith("#"):
            continue
        n += 1
        mac = norm_mac(f[0])
        if mac is None:
            warn(f'static-ip.conf línea {ln}: MAC inválida "{f[0]}"')
        elif len(f) < 2 or not ipv4_ok(f[1]):
            warn(f'static-ip.conf línea {ln}: IP inválida "{f[1] if len(f) > 1 else ""}"')
        elif not same_net(f[1], gw, mask):
            warn(f"static-ip.conf línea {ln}: {f[1]} no está en la red {gw}/{mask}")
        else:
            macs.setdefault(mac, []).append(ln)
            ips.setdefault(f[1], []).append(ln)
            continue
        bad += 1
    for mac, lns in macs.items():
        if len(lns) > 1:
            warn(f"static-ip.conf: la MAC {mac} aparece más de una vez (líneas {', '.join(map(str, lns))})")
    for ip, lns in ips.items():
        if len(lns) > 1:
            warn(f"static-ip.conf: la IP {ip} está asignada a más de una MAC (líneas {', '.join(map(str, lns))})")
    msg = f"{n} máquinas" + (f", {bad} líneas con problemas" if bad else "")
    return f"ya estaba ({msg}); no se toca"


# --- escritura y verificación -------------------------------------------------


def install(dest, name, data):
    """Escribe a un temporal en la misma FAT y renombra: nunca un archivo a medias."""
    tmp = dest / f".nb3tmp-{name}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest / name)


def apply(p):
    dest = p["dest"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for name, data, backup in p["writes"]:
        target = dest / name
        if name == TABLE_NAME and target.exists():
            continue  # alguien la creó mientras se confirmaba
        if backup:
            shutil.copyfile(target, dest / f"{name}.bak-{stamp}")
        install(dest, name, data)
    os.sync()
    return stamp


def verify(p):
    dest = p["dest"]
    fails = []
    for name, data, _ in p["writes"]:
        if name != TABLE_NAME and (dest / name).read_bytes() != data:
            fails.append(f"{name} no quedó como se escribió")
    conf = (dest / "nutellaboot.conf").read_text(encoding="utf-8", errors="replace")
    if conf_get(conf, "NB_NET_GW") != p["net"][0]:
        fails.append("NB_NET_GW en nutellaboot.conf")
    if "--id staticip" not in (dest / "grub.cfg").read_text(encoding="utf-8", errors="replace"):
        fails.append("el bloque IP fija en grub.cfg")
    with open(dest / CPIO_NAME, "rb") as f:
        got = cpio_read(f)
    for n, (m, d) in p["want_cpio"].items():
        if n not in got or got[n][0] != m or (not m & 0o040000 and got[n][1] != d):
            fails.append(f"{n} dentro de {CPIO_NAME}")
    if not (dest / TABLE_NAME).is_file():
        fails.append(TABLE_NAME)
    if shutil.which("grub-script-check"):
        if subprocess.run(["grub-script-check", str(dest / "grub.cfg")]).returncode != 0:
            fails.append("grub-script-check grub.cfg")
    return fails


def main():
    ap = argparse.ArgumentParser(
        description='Instala la entrada "IP fija" (salas sin DHCP) en un pendrive NutellaBoot 3.')
    ap.add_argument("dest", help="la partición NB3CFG del pendrive, montada (ej.: /mnt/nb3)")
    ap.add_argument("toml", help="la red de la sala: sección [red] con gateway, mascara, dns, prefijo")
    ap.add_argument("--dry-run", action="store_true", help="mostrar los cambios sin escribir nada")
    args = ap.parse_args()

    try:
        p = plan(args)
    except (Error, OSError) as e:
        error(e)
        return 1

    gw, mask, dns, prefix = p["net"]
    print(f"Pendrive: {p['dest']}")
    print(f"Red ({args.toml}): gateway {gw}, máscara {mask}" + (f", DNS {dns}" if dns else "")
          + (f", prefijo {prefix}" + (" (deducido)" if p["prefix_derived"] else "") if prefix else ""))
    print()
    for name, status in p["report"]:
        print(f"  {name:<18} " + paint(status, DIM if status.startswith("ya estaba") else YELLOW))
    if not p["writes"]:
        print("\n" + paint("Todo ya está en el pendrive: no hay nada que cambiar.", GREEN))
        return 0
    print()
    for d in p["diffs"]:
        print(paint_diff(d))
    if args.dry_run:
        print(paint("(--dry-run: no se escribió nada)", CYAN, BOLD))
        return 0

    try:
        ans = input(f"¿Aplicar estos cambios en {p['dest']}? [s/N] ")
    except EOFError:
        ans = ""
    if ans.strip().lower() not in ("s", "si", "sí", "y", "yes"):
        print("Cancelado: no se escribió nada.")
        return 1

    try:
        stamp = apply(p)
        fails = verify(p)
    except (Error, OSError) as e:
        error(e)
        return 1
    if fails:
        for f in fails:
            print(f"  {paint('FALLÓ:', RED, BOLD, stream=sys.stderr)} {f}", file=sys.stderr)
        error(f"la verificación falló; las copias están en *.bak-{stamp}")
        return 1

    print("\n" + paint("Listo.", GREEN, BOLD) + f" Copias de lo anterior: *.bak-{stamp}")
    print("Siguientes pasos:")
    print("  - En el servidor, client/stuff/60-postmount.d/81-nm-static.sh (sin él, el")
    print("    escritorio queda sin red).")
    print('  - Probar en una máquina: el menú debe arrancar en "IP fija" y pedir la IP.')
    print("  - Volver a correr este script cuando la autoactualización cambie initrd.img.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
