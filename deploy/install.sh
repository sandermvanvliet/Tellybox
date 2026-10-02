#!/bin/sh
# Tellybox installer (DP-6).
#   curl -fsSL https://github.com/sandermvanvliet/Tellybox/releases/latest/download/install.sh | sh
# Options: -y/--yes accepts all defaults. Variables: TELLYBOX_DIR, TZ,
# TELLYBOX_WEB_PORT, TELLYBOX_VERSION (a release tag such as v0.1.0).
set -eu

REPO_URL="https://github.com/sandermvanvliet/Tellybox/releases"
YES=0
for arg in "$@"; do
    case "$arg" in
        -y | --yes) YES=1 ;;
        -h | --help)
            echo "Usage: install.sh [-y|--yes]"
            echo "Variables: TELLYBOX_DIR, TZ, TELLYBOX_WEB_PORT, TELLYBOX_VERSION (a release tag such as v0.1.0)."
            exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ask "Question" "default": prints the answer. Uses /dev/tty so it works when
# the script is piped into sh; falls back to the default without a terminal.
ask() {
    if [ "$YES" -eq 1 ] || ! { : </dev/tty; } 2>/dev/null; then
        printf '%s\n' "$2"
        return
    fi
    printf '%s [%s]: ' "$1" "$2" >/dev/tty
    answer=""
    read -r answer </dev/tty || true
    printf '%s\n' "${answer:-$2}"
}

confirm() { # confirm "Question": yes by default
    [ "$YES" -eq 1 ] && return 0
    case "$(ask "$1 (y/n)" y)" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

check_docker() {
    [ "$(uname -s)" = "Linux" ] || die "Tellybox needs a Linux host (host networking is used for Chromecast discovery)."
    have docker || die "Docker is not installed. See https://docs.docker.com/engine/install/"
    docker compose version >/dev/null 2>&1 ||
        die "The Docker Compose plugin is missing ('docker compose version' fails). Install docker-compose-plugin; the old docker-compose v1 will not do."
    if ! info=$(docker info --format '{{.OperatingSystem}}' 2>&1); then
        case "$info" in
            *ermission*denied*) die "No permission to use Docker. Re-run with sudo, or add yourself to the docker group (sudo usermod -aG docker \$USER, then log in again)." ;;
            *) die "Cannot reach the Docker daemon: $info" ;;
        esac
    fi
    case "$info" in
        *"Docker Desktop"*) die "Docker Desktop is not supported: it runs containers in a VM, so host networking cannot reach your LAN or the Chromecast. Use Docker Engine on a Linux host." ;;
    esac
}

default_tz() {
    tz=""
    [ -r /etc/timezone ] && tz=$(head -n 1 /etc/timezone)
    if [ -z "$tz" ] && have timedatectl; then
        tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
    fi
    printf '%s\n' "${tz:-UTC}"
}

port_in_use() {
    have ss || return 1
    ss -ltn 2>/dev/null | awk -v p=":$1\$" '$4 ~ p { found = 1 } END { exit !found }'
}

prepare_dir() {
    if [ -d "$1" ] && [ -w "$1" ]; then return; fi
    if mkdir -p "$1" 2>/dev/null && [ -w "$1" ]; then return; fi
    [ "$(id -u)" -ne 0 ] || die "Cannot create $1."
    have sudo || die "Cannot write to $1 and sudo is not available. Pick another folder with TELLYBOX_DIR."
    say "Using sudo to create $1 and give it to $(id -un), because it is not writable."
    sudo mkdir -p "$1" || die "Could not create $1."
    sudo chown "$(id -u):$(id -g)" "$1" || die "Could not take ownership of $1."
}

fetch() { # fetch <file at the release> <target>
    if [ -n "${TELLYBOX_INSTALL_BASE_URL:-}" ]; then
        base="$TELLYBOX_INSTALL_BASE_URL"
    elif [ -n "${TELLYBOX_VERSION:-}" ]; then
        base="$REPO_URL/download/$TELLYBOX_VERSION"
    else
        base="$REPO_URL/latest/download"
    fi
    say "Downloading $1 ..."
    if have curl; then
        curl -fsSL "$base/$1" -o "$2" || die "Download failed: $base/$1"
    elif have wget; then
        wget -qO "$2" "$base/$1" || die "Download failed: $base/$1"
    else
        die "Neither curl nor wget is installed."
    fi
}

# set_env KEY VALUE FILE: replace KEY= (or a commented # KEY=) or append it.
# The sed delimiter is | so values with a slash (America/New_York) are safe.
set_env() {
    value=$(printf '%s' "$2" | sed 's/[\&|]/\\&/g')
    if grep -Eq "^#? ?$1=" "$3"; then
        sed "s|^#\{0,1\} \{0,1\}$1=.*|$1=$value|" "$3" >"$3.tmp" && mv "$3.tmp" "$3"
    else
        printf '%s=%s\n' "$1" "$2" >>"$3"
    fi
}

wait_healthy() {
    [ "${TELLYBOX_INSTALL_SKIP_HEALTH:-0}" = 1 ] && return 0
    say "Waiting for Tellybox to start (up to 90 seconds) ..."
    i=0
    while [ "$i" -lt 45 ]; do
        if have curl; then
            curl -fs "http://127.0.0.1:$1/healthz" >/dev/null 2>&1 && return 0
        else
            wget -qO /dev/null "http://127.0.0.1:$1/healthz" 2>/dev/null && return 0
        fi
        i=$((i + 1))
        sleep 2
    done
    return 1
}

lan_ip() {
    ip=""
    have ip && ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
    if [ -z "$ip" ] && have hostname; then
        ip=$(hostname -I 2>/dev/null | cut -d ' ' -f 1 || true)
    fi
    printf '%s\n' "${ip:-<server-ip>}"
}

main() {
    check_docker

    dir=$(ask "Install folder" "${TELLYBOX_DIR:-/opt/tellybox}")
    existing=0
    if [ -f "$dir/docker-compose.yml" ] || [ -f "$dir/.env" ]; then existing=1; fi
    if [ "$existing" -eq 0 ]; then
        tz=$(ask "Time zone" "${TZ:-$(default_tz)}")
        port=$(ask "Web port" "${TELLYBOX_WEB_PORT:-8080}")
        case "$port" in '' | *[!0-9]*) die "The web port must be a number." ;; esac
        if port_in_use "$port"; then
            say "Warning: something already listens on port $port. Pick another port, or stop that service first."
            confirm "Continue with port $port anyway?" || die "Aborted."
        fi
    fi

    prepare_dir "$dir"
    cd "$dir"

    if [ "$existing" -eq 1 ]; then
        say "Found an existing installation in $dir. Your .env stays as it is."
        if confirm "Update docker-compose.yml to the latest release?"; then
            fetch docker-compose.yml docker-compose.yml.new
            mv docker-compose.yml.new docker-compose.yml
        fi
        port=""
        if [ -f .env ]; then
            port=$(sed -n 's/^TELLYBOX_WEB_PORT=//p' .env | tail -n 1)
        fi
        port="${port:-8080}"
        say "Pulling the image ..."
        docker compose pull
    else
        fetch docker-compose.yml docker-compose.yml
        fetch env.example .env
        set_env TZ "$tz" .env
        set_env TELLYBOX_WEB_PORT "$port" .env
        if [ -n "${TELLYBOX_VERSION:-}" ]; then
            set_env TELLYBOX_TAG "${TELLYBOX_VERSION#v}" .env
        fi
    fi

    say "Starting Tellybox ..."
    docker compose up -d

    healthy=1
    wait_healthy "$port" || healthy=0
    code=""
    if [ "$existing" -eq 0 ]; then # an upgrade's logs may hold a code that's long been used
        code=$(docker compose logs tellybox 2>/dev/null | sed -n 's/.*admin setup code: \([A-Z0-9-]*\).*/\1/p' | tail -n 1 || true)
    fi
    ip=$(lan_ip)

    say ""
    if [ "$healthy" -eq 0 ]; then
        say "Tellybox did not answer within 90 seconds. Check: cd $dir && docker compose logs tellybox"
    else
        say "Tellybox is running."
    fi
    if [ -n "$code" ]; then
        say "  Set the admin password: http://$ip:$port/admin/setup"
        say "  Setup code:             $code"
    else
        say "  Sign in as admin:       http://$ip:$port/admin"
    fi
    say "  Kids open:              http://$ip:$port/"
    say "  Files live in:          $dir (compose file, .env, data, media, backups)"
}

main
