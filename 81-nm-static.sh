# shellcheck shell=sh
# Perfil del NetworkManager para la IP fija ingresada al arrancar.
#
# Va en el servidor, en client/stuff/60-postmount.d/81-nm-static.sh, y además
# hay que agregar `nb3_post_nm_static` a runpostmountconfigs en 90-main.sh,
# después de nb3_post_nm_wifi: el stuff solo ejecuta las funciones de esa lista.
#
# El overlay nb3-staticip.cpio del pendrive (entrada de menú "IP fija") toma
# la IP de static-ip.conf o la pregunta en la consola, configura con ella la
# red del initrd y deja los datos en $NB_RUN/static-ip. Sin este perfil, el
# fixnetworktoreconnect entrega la red al NetworkManager, que intenta DHCP en
# una sala que no lo tiene: la máquina llega al escritorio sin red (ni
# telemetría ni bloqueo de pantalla).
#
# Sin el archivo (boot por DHCP) no hace nada, salvo borrar un perfil viejo:
# las salas con DHCP siguen igual. Corre antes del nb3_limpa_run, que solo
# borra nutellaboot.conf y wifi.conf, y antes del fixnetworktoreconnect.
#
# El archivo se lee, no se ejecuta con `.`: los valores vinieron del teclado.
nb3_post_nm_static() {
    _nms_conf=${NB_RUN:-/run/nutellaboot}/static-ip
    _nms_file=${rootmnt?}/etc/NetworkManager/system-connections/nb3-static.nmconnection
    # boot por DHCP: que no quede un perfil de un boot anterior
    if [ ! -s "$_nms_conf" ]; then
        rm -f "$_nms_file"
        return 0
    fi

    _nms_dev=$(nb3_nms_val dev "$_nms_conf")
    _nms_ip=$(nb3_nms_val ip "$_nms_conf")
    _nms_mask=$(nb3_nms_val mask "$_nms_conf")
    _nms_gw=$(nb3_nms_val gw "$_nms_conf")
    _nms_dns=$(nb3_nms_val dns "$_nms_conf")

    # el nombre de la interfaz va al archivo: nada de barras ni cosas raras
    case "$_nms_dev" in '' | *[!A-Za-z0-9_.-]* | .*) _nms_dev=eth0 ;; esac
    if ! nb3_nms_ipv4 "$_nms_ip" || ! nb3_nms_ipv4 "$_nms_gw"; then
        nb_warn "static-ip: invalid address ($_nms_ip) or gateway ($_nms_gw); no static profile"
        return 0
    fi
    if ! _nms_pfx=$(nb3_nms_prefix "${_nms_mask:-255.255.255.0}"); then
        nb_warn "static-ip: invalid netmask ($_nms_mask); no static profile"
        return 0
    fi
    _nms_dnsl=""
    for _nms_d in $(printf '%s' "$_nms_dns" | tr ',' ' '); do
        nb3_nms_ipv4 "$_nms_d" && _nms_dnsl="$_nms_dnsl$_nms_d;"
    done

    mkdir -p "${_nms_file%/*}"
    {
        echo "[connection]"
        echo "id=nb3-static"
        # el uuid determinístico del 80-nm-wifi.sh; sin él, el NetworkManager
        # deriva uno de la ruta del archivo
        command -v nb3_uuid_for > /dev/null 2>&1 && echo "uuid=$(nb3_uuid_for nb3-static)"
        echo "type=ethernet"
        echo "interface-name=$_nms_dev"
        echo "autoconnect=true"
        echo "autoconnect-priority=100"
        echo
        echo "[ethernet]"
        echo
        echo "[ipv4]"
        echo "method=manual"
        echo "address1=$_nms_ip/$_nms_pfx,$_nms_gw"
        [ -n "$_nms_dnsl" ] && echo "dns=$_nms_dnsl"
        echo
        echo "[ipv6]"
        echo "method=ignore"
    } > "$_nms_file"
    chmod 600 "$_nms_file"
    nb_log "static network profile generated for NetworkManager ($_nms_ip/$_nms_pfx on $_nms_dev)"
}

# nb3_nms_val <clave> <archivo> — primer valor de clave=valor, sin comillas ni CR
nb3_nms_val() {
    sed -n "s/^$1=//p" "$2" | sed -n '1p' | tr -d "\"'\r"
}

# 4 octetos decimales de 0 a 255
nb3_nms_ipv4() {
    case "$1" in '' | *[!0-9.]* | .* | *. | *..* | 0[0-9]* | *.0[0-9]*) return 1 ;; esac
    _nmi_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$_nmi_ifs
    [ $# -eq 4 ] || return 1
    for _nmi_o; do
        [ ${#_nmi_o} -le 3 ] && [ "$_nmi_o" -le 255 ] || return 1
    done
}

# máscara punteada -> prefijo CIDR; falla si no es una máscara contigua
nb3_nms_prefix() {
    nb3_nms_ipv4 "$1" || return 1
    _nmp_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$_nmp_ifs
    _nmp_n=0
    _nmp_fim=""
    for _nmp_o; do
        if [ -n "$_nmp_fim" ]; then
            [ "$_nmp_o" -eq 0 ] || return 1
            continue
        fi
        case "$_nmp_o" in
            255) _nmp_n=$((_nmp_n + 8)); continue ;;
            254) _nmp_n=$((_nmp_n + 7)) ;;
            252) _nmp_n=$((_nmp_n + 6)) ;;
            248) _nmp_n=$((_nmp_n + 5)) ;;
            240) _nmp_n=$((_nmp_n + 4)) ;;
            224) _nmp_n=$((_nmp_n + 3)) ;;
            192) _nmp_n=$((_nmp_n + 2)) ;;
            128) _nmp_n=$((_nmp_n + 1)) ;;
            0) ;;
            *) return 1 ;;
        esac
        _nmp_fim=1
    done
    echo "$_nmp_n"
}
