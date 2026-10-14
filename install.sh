#!/usr/bin/env bash
# sms-gateway installer.
#
#   curl -fsSL https://raw.githubusercontent.com/xyc0677/sms-gateway/main/install.sh | sudo bash
#   # or, from a clone:  sudo ./install.sh
#
# Asks a few questions, writes .env (random secrets included), prepares the
# modem (udev rules, USB audio) and starts everything with Docker Compose.
# Safe to run again: an existing .env and its secrets are kept.
#
# Options:
#   -y, --yes      do not ask; take answers from the environment or defaults
#                  (SMS_DOMAIN, SIP_DOMAIN, LAN_IP, TZ, BACKUP_DIR, LAN_DNS=1 ...)
#   --dir=PATH     where the project lives (default /opt/sms-gateway)
#   --dry-run      only write .env; change nothing on the system, start nothing
set -euo pipefail

REPO_URL=${SMSGW_REPO:-https://github.com/xyc0677/sms-gateway.git}
DIR=${SMSGW_DIR:-/opt/sms-gateway}
ASSUME_YES=0
DRY_RUN=0
SECRETS="AMI_SECRET SESSION_SECRET SIP_PASSWORD FLEXIAPI_ROUTER_KEY INTERNAL_KEY BARK_PUSH_PASSWORD WEB_SIP_PASSWORD PBX_TOKEN"

case "${LC_ALL:-}${LC_MESSAGES:-}${LANG:-}" in *zh*) ZH=1 ;; *) ZH=0 ;; esac
t() { if [ "$ZH" = 1 ]; then printf '%s' "$1"; else printf '%s' "$2"; fi; }
say() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$(t "$1" "$2")"; }
info() { printf '    %s\n' "$(t "$1" "$2")"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$(t "$1" "$2")"; }
die() { printf '\033[1;31m  x %s\033[0m\n' "$(t "$1" "$2")" >&2; exit 1; }
run() { if [ "$DRY_RUN" = 1 ]; then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --dir=*) DIR=${arg#*=} ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "不认识的参数：$arg" "unknown option: $arg" ;;
    esac
done

# Questions come from the terminal even when the script itself arrives on
# stdin (curl ... | sudo bash).
if { : </dev/tty; } 2>/dev/null; then exec 3</dev/tty; else exec 3<&0; fi

# ask VAR "中文问题" "English question" default
ask() {
    local var=$1 zh=$2 en=$3 def=${4:-} ans
    if [ -n "${!var:-}" ]; then return; fi
    if [ "$ASSUME_YES" = 1 ]; then printf -v "$var" '%s' "$def"; return; fi
    printf '\033[1;32m  ?\033[0m %s' "$(t "$zh" "$en")"
    [ -n "$def" ] && printf ' [%s]' "$def"
    printf ': '
    IFS= read -r ans <&3 || ans=""
    printf -v "$var" '%s' "${ans:-$def}"
}
# confirm "中文" "English" y|n  -> exit status
confirm() {
    local def=${3:-y} ans
    if [ "$ASSUME_YES" = 1 ]; then [ "$def" = y ]; return; fi
    printf '\033[1;32m  ?\033[0m %s [%s]: ' "$(t "$1" "$2")" "$([ "$def" = y ] && echo Y/n || echo y/N)"
    IFS= read -r ans <&3 || ans=""
    ans=${ans:-$def}
    case "$ans" in y|Y|yes|Yes|是|好) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------------ checks --
say "检查环境" "Checking the system"
[ "$(uname -s)" = Linux ] || die "只支持 Linux" "Linux only"
if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "请用 root 运行：sudo $0" "please run as root: sudo $0"
fi
for c in git openssl ip ss sed awk; do
    command -v "$c" >/dev/null || die "缺少命令：$c，请先安装" "missing command: $c, please install it first"
done
if ! docker compose version >/dev/null 2>&1; then
    warn "没有找到 Docker 和 Docker Compose v2" "Docker with Compose v2 was not found"
    if [ "$DRY_RUN" = 0 ] && confirm "用 Docker 官方脚本（get.docker.com）安装吗？" "Install it with Docker's script (get.docker.com)?" y; then
        curl -fsSL https://get.docker.com | sh
        docker compose version >/dev/null 2>&1 || die "Docker 安装失败" "Docker installation failed"
    elif [ "$DRY_RUN" = 0 ]; then
        die "请先装好 Docker：https://docs.docker.com/engine/install/" "install Docker first: https://docs.docker.com/engine/install/"
    fi
fi
info "环境没问题" "looks good"

# -------------------------------------------------------------------- code --
say "准备代码" "Getting the code"
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/compose.yaml" ] && [ "${SMSGW_DIR:-}" = "" ] && [[ "$*" != *--dir=* ]]; then
    DIR=$SELF_DIR
fi
if [ -f "$DIR/compose.yaml" ]; then
    info "使用 $DIR" "using $DIR"
else
    run git clone --depth 1 "$REPO_URL" "$DIR"
fi
cd "$DIR" 2>/dev/null || { [ "$DRY_RUN" = 1 ] && die "dry-run 需要一个已有的目录（--dir）" "dry-run needs an existing checkout (--dir)"; }
ENVF=$DIR/.env

# ------------------------------------------------------------------- modem --
say "检查 4G 模块" "Checking the 4G modem"
modem_found=0
for d in /sys/bus/usb/devices/*; do
    if [ "$(cat "$d/idVendor" 2>/dev/null)" = 2c7c ] && [ "$(cat "$d/idProduct" 2>/dev/null)" = 0125 ]; then modem_found=1; fi
done
if [ "$modem_found" = 1 ]; then
    info "找到 Quectel EC20/EC25（2c7c:0125）" "found Quectel EC20/EC25 (2c7c:0125)"
else
    warn "没找到 Quectel EC20/EC25（2c7c:0125）。别的型号要先改 host/77-quectel-sms-gateway.rules，见 docs/environments.md" \
         "no Quectel EC20/EC25 (2c7c:0125) found. Other models need host/77-quectel-sms-gateway.rules adapted, see docs/en/environments.md"
    confirm "还是继续安装吗？" "Continue anyway?" n || exit 1
fi

info "安装 udev 规则（固定串口名、不让 ModemManager 碰模块）" "installing udev rules (stable port names, keep ModemManager away)"
run install -m 644 host/77-quectel-sms-gateway.rules /etc/udev/rules.d/
if [ -d /etc/NetworkManager/conf.d ]; then
    run install -m 644 host/99-no-wwan-data.conf /etc/NetworkManager/conf.d/
fi
run udevadm control --reload
run udevadm trigger
if command -v systemctl >/dev/null && systemctl is-active --quiet ModemManager 2>/dev/null; then
    warn "ModemManager 正在运行，它可能会抢模块的串口" "ModemManager is running and may grab the modem's serial ports"
    if confirm "停用 ModemManager 吗？（其它设备要用它的话选 n）" "Disable ModemManager? (answer n if other devices need it)" y; then
        run systemctl disable --now ModemManager
    fi
fi

if [ "$modem_found" = 1 ] && [ "$DRY_RUN" = 0 ]; then
    for _ in $(seq 10); do [ -e /dev/quectel-at ] && break; sleep 1; done
    if ! grep -q Quectel /proc/asound/cards 2>/dev/null; then
        warn "模块的 USB 声卡还没打开，不打开就打不了电话" "the modem's USB audio is off; calls need it"
        if confirm "现在打开吗？（会写入模块设置并让模块重启一次）" "Turn it on now? (writes the modem setting and restarts it once)" y; then
            [ -e /dev/quectel-at ] || die "找不到 /dev/quectel-at，请检查 udev 规则" "/dev/quectel-at is missing, check the udev rules"
            stty -F /dev/quectel-at 115200 raw -echo 2>/dev/null || true
            exec 4<>/dev/quectel-at
            printf 'AT+QCFG="usbcfg",0x2C7C,0x0125,1,1,1,1,1,0,1\r' >&4; sleep 1
            printf 'AT+CFUN=1,1\r' >&4; sleep 1
            exec 4>&-
            info "等模块重启……" "waiting for the modem to restart..."
            for _ in $(seq 60); do grep -q Quectel /proc/asound/cards 2>/dev/null && break; sleep 1; done
            if grep -q Quectel /proc/asound/cards 2>/dev/null; then info "声卡已经打开" "USB audio is on"
            else warn "还是没看到声卡，可能内核没有 snd-usb-audio，见 docs/troubleshooting.md" "still no sound card; the kernel may lack snd-usb-audio, see docs/en/troubleshooting.md"; fi
        fi
    else
        info "模块声卡已经打开" "modem USB audio is already on"
    fi
fi

# ------------------------------------------------------------------ config --
say "填写配置" "Configuration"
if [ -f "$ENVF" ]; then
    info "已有 .env，保留里面的设置和密钥，只补上缺的" "keeping the existing .env and its secrets, only filling in what is missing"
else
    cp .env.example "$ENVF"
fi
chmod 600 "$ENVF"
get_env() { sed -n -E "s/^$1=(.*)$/\1/p" "$ENVF" | tail -n 1; }
set_env() {
    if grep -qE "^#?[[:space:]]*$1=" "$ENVF"; then
        sed -i -E "s|^#?[[:space:]]*$1=.*|$1=$2|" "$ENVF"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENVF"
    fi
}
cur() { local v; v=$(get_env "$1"); case "$v" in *example.com|192.168.1.10|192.168.1.1|'') printf '%s' "${2:-}" ;; *) printf '%s' "$v" ;; esac; }

ask SMS_DOMAIN "网页用的域名（比如 sms.example.com）" "domain for the web page (e.g. sms.example.com)" "$(cur SMS_DOMAIN)"
[ -n "$SMS_DOMAIN" ] || die "域名不能为空" "a domain is required"
ask SIP_DOMAIN "Linphone 用的域名" "domain for Linphone (SIP)" "$(cur SIP_DOMAIN "sip.${SMS_DOMAIN#*.}")"
[ "$SIP_DOMAIN" != "$SMS_DOMAIN" ] || die "两个域名不能一样" "the two domains must differ"
detected_ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
ask LAN_IP "这台机器的局域网 IP" "this machine's LAN IP" "$(cur LAN_IP "$detected_ip")"
detected_tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || true)
ask TZ "时区" "time zone" "$(cur TZ "${detected_tz:-Asia/Shanghai}")"
ask BACKUP_DIR "每日备份放在哪（最好是另一块硬盘）" "where daily backups go (ideally another disk)" "$(cur BACKUP_DIR ./data/backup)"

if ss -ltnH '( sport = :80 or sport = :443 )' 2>/dev/null | grep -q . && [ -z "$(get_env CADDY_HTTPS_PORT)" ]; then
    warn "80 或 443 端口已经被别的程序占用（比如 nginx、1Panel）" "port 80 or 443 is already taken (nginx, a panel, ...)"
    info "Caddy 会改用 18081/18444 并只监听本机，你需要让现有的代理把两个域名转过来，见 README「特殊情况」" \
         "Caddy will use 18081/18444 on loopback; point your existing proxy at it, see the README section on existing proxies"
    # shellcheck disable=SC2034  # read back through ${!k} below
    CADDY_HTTP_PORT=18081 CADDY_HTTPS_PORT=18444 CADDY_BIND=127.0.0.1
fi
for p in 5061 18300; do
    ss -ltnH "( sport = :$p )" 2>/dev/null | grep -q . && ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q sms-gateway \
        && warn "TCP $p 已被占用，启动时会冲突" "TCP $p is already in use and will conflict"
done
ss -lunH '( sport = :5060 )' 2>/dev/null | grep -q . && ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q sms-gateway \
    && warn "UDP 5060 已被占用（别的 SIP 软件？）" "UDP 5060 is already in use (another SIP server?)"

if [ -z "${LAN_DNS:-}" ] && [ "$(get_env COMPOSE_PROFILES)" != lan-dns ]; then
    if confirm "你的路由器在家里访问不到自己的公网 IP 吗？（不确定就选 n，以后可以再开）" \
               "Does your router lack NAT loopback (home devices can't reach your own public IP)? (say n if unsure)" n; then LAN_DNS=1; fi
fi
if [ "${LAN_DNS:-0}" = 1 ]; then
    gw=$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}')
    ask LAN_DNS_UPSTREAM "路由器 IP（其它域名转发给它）" "router IP (other names are forwarded to it)" "$(cur LAN_DNS_UPSTREAM "$gw")"
    set_env COMPOSE_PROFILES lan-dns
    set_env LAN_DNS_UPSTREAM "$LAN_DNS_UPSTREAM"
fi

for k in SMS_DOMAIN SIP_DOMAIN LAN_IP TZ BACKUP_DIR; do set_env "$k" "${!k}"; done
for k in CADDY_HTTP_PORT CADDY_HTTPS_PORT CADDY_BIND; do [ -n "${!k:-}" ] && set_env "$k" "${!k}"; done
new=0
for k in $SECRETS; do
    if [ -z "$(get_env "$k")" ]; then set_env "$k" "$(openssl rand -hex 24)"; new=$((new + 1)); fi
done
info "生成了 $new 个新密钥，配置写在 $ENVF" "generated $new new secrets; settings are in $ENVF"

case "$BACKUP_DIR" in /*) bdir=$BACKUP_DIR ;; *) bdir=$DIR/${BACKUP_DIR#./} ;; esac
run mkdir -p "$bdir"
run touch "$bdir/.sms-gateway-backup"

if [ "$DRY_RUN" = 1 ]; then
    say "dry-run 结束，系统没有被改动" "Dry run finished; nothing on the system was changed"
    exit 0
fi

# ------------------------------------------------------------------- start --
say "构建并启动（第一次要二三十分钟，主要在编译 Flexisip）" "Building and starting (the first build takes 20-40 minutes, mostly Flexisip)"
cores=$(nproc)
mem_gb=$(awk '/MemTotal/ { print int($2 / 1048576) }' /proc/meminfo)
jobs=$(( cores > mem_gb ? mem_gb : cores )); [ "$jobs" -ge 1 ] || jobs=1
docker compose build --build-arg JOBS="$jobs"
docker compose up -d

say "检查运行状态" "Checking that everything runs"
modem_state=""
for _ in $(seq 45); do
    modem_state=$(docker compose exec -T asterisk asterisk -rx "quectel show devices" 2>/dev/null | awk 'NR == 2 { print $3 }') || true
    [ "$modem_state" = Free ] && break
    sleep 2
done
docker compose ps --format '    {{.Service}}: {{.Status}}'
if [ "$modem_state" = Free ]; then info "模块已连上，可以收发短信了" "the modem is up and ready"
else warn "模块还没就绪（状态：${modem_state:-未知}），看 docker compose logs asterisk" "the modem is not ready yet (state: ${modem_state:-unknown}); see docker compose logs asterisk"; fi

# ------------------------------------------------------------------- done --
SIP_PASSWORD=$(get_env SIP_PASSWORD)
say "装好了，还剩这几步要你自己做" "Installed. A few things only you can do"
# shellcheck disable=SC1111  # Chinese quotation marks in the message text
if [ "$ZH" = 1 ]; then cat <<MSG
    1. 域名：给 $SMS_DOMAIN 和 $SIP_DOMAIN 加 A 记录，指向你家的公网 IP。
       用 Cloudflare 的话，$SIP_DOMAIN 只能选“仅 DNS”。
    2. 路由器：把 TCP 80、443、5061 和 UDP 10000-10099 转发到 $LAN_IP。
       18300 和 5060 不要转发出去。
    3. 网页：在家里打开 http://$LAN_IP:18300 设登录密码，之后用 https://$SMS_DOMAIN 登录。
    4. Bark：在 App 里添加服务器 https://$SMS_DOMAIN/bark，把测试链接填到网页「设置 → Bark 推送设备」。
    5. Linphone：网页「设置」里填 linphone.org 账号；手机上的 Linphone 用
         用户名 iphone   密码 $SIP_PASSWORD   域名 $SIP_DOMAIN   传输 TLS
       媒体加密选 SRTP。

    详细说明：$DIR/README.md
MSG
else cat <<MSG
    1. DNS: add A records for $SMS_DOMAIN and $SIP_DOMAIN pointing to your public IP.
       On Cloudflare, $SIP_DOMAIN must be "DNS only".
    2. Router: forward TCP 80, 443, 5061 and UDP 10000-10099 to $LAN_IP.
       Do not forward 18300 or 5060.
    3. Web page: at home, open http://$LAN_IP:18300 to set the password; afterwards use https://$SMS_DOMAIN.
    4. Bark: add the server https://$SMS_DOMAIN/bark in the app and paste its test URL under Settings -> Bark devices.
    5. Linphone: enter your linphone.org account under Settings; in Linphone on the phone use
         username iphone   password $SIP_PASSWORD   domain $SIP_DOMAIN   transport TLS
       and set media encryption to SRTP.

    Details: $DIR/README.en.md
MSG
fi
