#!/bin/sh
# sow installer — makes the host capable, then runs ``sow init``.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/VictorBusque/sow/main/install.sh | sh
#
# Installs missing system dependencies where possible (requires sudo for
# packages). Privilege escalation is requested only when needed.
# sow itself runs rootless — only the one-time dep install may use sudo.
set -e

# ── style ─────────────────────────────────────────────────────────────────
BOLD=$(printf '\033[1m')
GREEN=$(printf '\033[32m')
RED=$(printf '\033[31m')
YELLOW=$(printf '\033[33m')
CYAN=$(printf '\033[36m')
DIM=$(printf '\033[2m')
RESET=$(printf '\033[0m')

ok()   { printf "  ${GREEN}✓${RESET} %s\n" "$1"; }
fail() { printf "  ${RED}✗${RESET} %s\n" "$1"; }
warn() { printf "  ${YELLOW}!${RESET} %s\n" "$1"; }
info() { printf "  ${CYAN}→${RESET} %s\n" "$1"; }
step() { printf "\n${BOLD}[%d/%d]${RESET} %s\n" "$1" "$2" "$3"; }
die()  { printf "\n${RED}${BOLD}fatal:${RESET} %s\n\n" "$1"; exit 1; }

# ── header ────────────────────────────────────────────────────────────────
clear 2>/dev/null || true
printf "${BOLD}"
printf "  ╭──────────────────────────────────────╮\n"
printf "  │                                      │\n"
printf "  │   🌱  sow installer                  │\n"
printf "  │   Linux micro-platform control plane │\n"
printf "  │                                      │\n"
printf "  ╰──────────────────────────────────────╯\n"
printf "${RESET}\n"

# ── platform detection ────────────────────────────────────────────────────
detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "apt"
    elif command -v dnf >/dev/null 2>&1; then
        echo "dnf"
    elif command -v yum >/dev/null 2>&1; then
        echo "yum"
    elif command -v pacman >/dev/null 2>&1; then
        echo "pacman"
    elif command -v apk >/dev/null 2>&1; then
        echo "apk"
    elif command -v brew >/dev/null 2>&1; then
        echo "brew"
    elif command -v zypper >/dev/null 2>&1; then
        echo "zypper"
    else
        echo ""
    fi
}

sudo_cmd() {
    if command -v sudo >/dev/null 2>&1 && [ "$(id -u)" -ne 0 ]; then
        printf "${DIM}(sudo may prompt for your password)${RESET}\n"
        sudo "$@"
    else
        "$@"
    fi
}

install_pkg() {
    pkg="$1" manager="$2"
    case "$manager" in
        apt)    sudo_cmd apt-get install -y "$pkg" ;;
        dnf)    sudo_cmd dnf install -y "$pkg" ;;
        yum)    sudo_cmd yum install -y "$pkg" ;;
        pacman) sudo_cmd pacman -S --noconfirm "$pkg" ;;
        apk)    sudo_cmd apk add "$pkg" ;;
        brew)   brew install "$pkg" ;;
        zypper) sudo_cmd zypper install -y "$pkg" ;;
        *)      return 1 ;;
    esac
}

PKG_MANAGER=$(detect_pkg_manager)
if [ -n "$PKG_MANAGER" ]; then
    ok "detected package manager: ${BOLD}$PKG_MANAGER${RESET}"
else
    warn "no package manager detected — can only check, not install"
fi

TOTAL_STEPS=4

# ── step 1: check and install system dependencies ─────────────────────────
step 1 $TOTAL_STEPS "System dependencies"

NEED_RESTART=0

ensure_cmd() {
    cmd="$1" pkg="$2"
    label="${3:-$1}"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$label found"
        return 0
    fi
    fail "$label not found"
    if [ -z "$PKG_MANAGER" ]; then
        die "install $label manually and re-run"
    fi
    info "installing ${BOLD}$pkg${RESET} via $PKG_MANAGER..."
    if install_pkg "$pkg" "$PKG_MANAGER"; then
        ok "$label installed"
        return 0
    fi
    die "could not install $label — install manually and re-run"
}

ensure_cmd git    "git"            "git"
ensure_cmd nginx  "nginx"          "nginx"
# cloudflared needs its own repo on apt/deb — check and install separately
get_cloudflared() {
    if command -v cloudflared >/dev/null 2>&1; then
        ok "cloudflared found"
        return 0
    fi
    fail "cloudflared not found"
    if [ -z "$PKG_MANAGER" ]; then
        die "install cloudflared manually and re-run"
    fi
    info "installing ${BOLD}cloudflared${RESET} via $PKG_MANAGER..."
    case "$PKG_MANAGER" in
        apt)
            sudo_cmd mkdir -p -m 0755 /usr/share/keyrings
            curl -fsSLo /tmp/cloudflare-main.gpg https://pkg.cloudflare.com/cloudflare-main.gpg
            sudo_cmd cp /tmp/cloudflare-main.gpg /usr/share/keyrings/cloudflare-main.gpg
            echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /tmp/cloudflared.list
            sudo_cmd cp /tmp/cloudflared.list /etc/apt/sources.list.d/cloudflared.list
            sudo_cmd apt-get update -qq
            sudo_cmd apt-get install -y cloudflared
            ;;
        *)
            install_pkg cloudflared "$PKG_MANAGER" || die "could not install cloudflared via $PKG_MANAGER — install manually and re-run"
            ;;
    esac
    ok "cloudflared installed"
}
get_cloudflared

# systemd user check — not a package, a service check
if systemctl --user --version >/dev/null 2>&1; then
    ok "systemd user mode available"
else
    fail "systemd user mode unavailable"
    NEED_RESTART=1
fi

# ── step 2: enable linger ─────────────────────────────────────────────────
step 2 $TOTAL_STEPS "User session persistence"

if loginctl enable-linger >/dev/null 2>&1; then
    ok "linger enabled (services survive logout)"
else
    warn "could not enable linger — services may stop on logout"
    warn "run manually: ${BOLD}loginctl enable-linger${RESET}"
fi

# ── step 3: install sow ───────────────────────────────────────────────
step 3 $TOTAL_STEPS "Installing sow CLI"

if command -v uv >/dev/null 2>&1; then
    info "installing via uv from PyPI..."
    uv tool install sow-cli --force
elif command -v pipx >/dev/null 2>&1; then
    info "installing via pipx from PyPI..."
    pipx install sow-cli
else
    info "installing via pip from PyPI..."
    pip3 install --user sow-cli
fi

ok "sow installed"

# ── step 4: bootstrap ─────────────────────────────────────────────────────
step 4 $TOTAL_STEPS "Bootstrapping platform"

sow init

# ── done ──────────────────────────────────────────────────────────────────
printf "\n${BOLD}"
printf "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}\n"
printf "${BOLD}"
printf "  sow is ready! ${RESET}🌱\n"
printf "\n"
printf "  Next:\n"
printf "    ${CYAN}1.${RESET} Edit your config at ${YELLOW}~/.config/sow/sow.yaml${RESET}\n"
printf "    ${CYAN}2.${RESET} Run ${BOLD}sow apply${RESET} to deploy\n"
printf "    ${CYAN}3.${RESET} Run ${BOLD}sow status${RESET} to see your stack\n"
printf "\n"
