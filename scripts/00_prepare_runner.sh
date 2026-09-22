#!/usr/bin/env bash
# ==============================================================================
#  00_prepare_runner.sh — turn a stock GitHub runner into an AOSP-10 workstation
#  * reclaims ~25 GB of disk (Android SDK, dotnet, docker images, toolcache)
#  * adds an 8 GB swap file (soong/kati/lld RAM spikes on a 16 GB box)
#  * installs Ubuntu 20.04-era toolchain bits on 22.04 (openjdk-8, ncurses5)
# ==============================================================================
source "$(dirname "$0")/lib.sh"

_log "Reclaiming runner disk (safe: node20 runtime lives in the runner agent, not toolcache)..."
sudo rm -rf /usr/local/lib/android \
            /usr/share/dotnet \
            /usr/local/share/boost \
            /opt/ghc \
            /opt/az \
            /usr/local/julia \
            /usr/local/graalvm \
            /usr/local/.ghcup \
            /usr/share/swift \
            /opt/hostedtoolcache \
            2>/dev/null || true
sudo docker system prune -af >/dev/null 2>&1 || true
sudo rm -rf /usr/lib/jvm/temurin-17-jdk-amd64 /usr/lib/jvm/temurin-11-jdk-amd64 2>/dev/null || true

command -v gh >/dev/null || _die "gh CLI vanished after cleanup — this should never happen"
command -v node >/dev/null 2>&1 || _warn "node missing from PATH (actions runtime uses its own — fine)"

_log "Installing build toolchain (Ubuntu 22.04 hosting an Android-10-era build)..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
  git git-lfs ccache zstd curl wget rsync zip unzip tar \
  build-essential openjdk-8-jdk \
  python-is-python3 python3-distutils zlib1g-dev \
  libncurses5-dev libncursesw5-dev \
  >/dev/null

# Android-10 host tools want ncurses5/tinfo5 sonames; jammy ships only v6.
for lib in libncurses.so.5 libtinfo.so.5 libncursesw.so.5; do
  if [ ! -e "/usr/lib/x86_64-linux-gnu/${lib}" ]; then
    base="${lib%.so.*}"
    if compgen -G "/usr/lib/x86_64-linux-gnu/${base}.so.6*" >/dev/null; then
      sudo ln -sf "/usr/lib/x86_64-linux-gnu/${base}.so.6" "/usr/lib/x86_64-linux-gnu/${lib}"
      _ok "compat symlink: ${lib} -> ${base}.so.6"
    else
      _warn "cannot create ${lib} compat symlink (base lib missing)"
    fi
  fi
done

_log "Configuring swap (RAM spike absorber)..."
if [ "$(swapon --show | wc -l)" -eq 0 ]; then
  free_now="$(disk_free_gb)"
  need_swap_gb="${SWAP_SIZE%G}"
  if [ "$free_now" -gt $((need_swap_gb + 25)) ]; then
    sudo fallocate -l "${SWAP_SIZE}" /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    _ok "swap on: ${SWAP_SIZE}"
  else
    _warn "only ${free_now} GB free — skipping swap creation to protect disk budget"
  fi
else
  _ok "swap already present"
fi

_log "git identity (repo tool requires it)..."
git config --global user.name  "${GIT_AUTHOR_NAME:-Zoro-15}"
git config --global user.email "${GIT_AUTHOR_EMAIL:-zoro15@users.noreply.github.com}"
git config --global http.postBuffer 524288000

_hr
_ok "Runner prepared."
printf '  CPU cores : %s\n' "$(nproc)"
free -h | sed 's/^/  /'
disk_report
java -version 2>&1 | head -1 | sed 's/^/  java: /'
