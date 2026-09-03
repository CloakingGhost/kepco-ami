#!/bin/bash

set -euo pipefail

echo "========================================="
echo " Rocky Linux 9 Server Initialization"
echo "========================================="


# ============================================================
# 0. Root Permission Check
# ============================================================

if [ "${EUID}" -ne 0 ]; then
    echo "ERROR: This script must be executed as root."
    exit 1
fi

echo "[OK] Running as root."


# ============================================================
# 1. Swap
# ============================================================

echo ""
echo "[1/8] Configure 1GB Swap..."

if swapon --show | grep -q "/swapfile"; then

    echo "[SKIP] Swap is already enabled."

else

    if [ ! -f /swapfile ]; then
        if ! fallocate -l 1G /swapfile; then
            echo "[INFO] fallocate failed. Using dd..."
            dd if=/dev/zero of=/swapfile bs=1M count=1024 status=progress
        fi
    fi

    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile

fi

if ! grep -q "^/swapfile " /etc/fstab; then
    echo "/swapfile swap swap defaults 0 0" >> /etc/fstab
fi

echo "vm.swappiness=10" > /etc/sysctl.d/99-swap.conf

sysctl --system >/dev/null

echo "[OK] 1GB Swap configured."


# ============================================================
# 2. System Update
# ============================================================

echo ""
echo "[2/8] Update Rocky Linux..."

dnf update -y

echo "[OK] System updated."


# ============================================================
# 3. Basic Packages
# ============================================================

echo ""
echo "[3/8] Install basic packages..."

dnf install -y \
    git \
    curl \
    wget \
    vim \
    unzip \
    tar \
    gzip \
    ca-certificates \
    dnf-plugins-core \
    openssl

echo "[OK] Basic packages installed."


# ============================================================
# 4. Timezone
# ============================================================

echo ""
echo "[4/8] Configure timezone..."

timedatectl set-timezone Asia/Seoul

echo "[OK] Timezone: Asia/Seoul"


# ============================================================
# 5. Docker + Docker Compose
# ============================================================

echo ""
echo "[5/8] Install Docker..."

if ! command -v docker >/dev/null 2>&1; then

    dnf remove -y \
        podman \
        podman-docker \
        runc \
        buildah \
        2>/dev/null || true

    if [ ! -f /etc/yum.repos.d/docker-ce.repo ]; then

        dnf config-manager \
            --add-repo \
            https://download.docker.com/linux/rhel/docker-ce.repo

    fi

    dnf install -y \
        docker-ce \
        docker-ce-cli \
        containerd.io \
        docker-buildx-plugin \
        docker-compose-plugin

else

    echo "[SKIP] Docker is already installed."

fi

systemctl enable --now docker

echo "[OK] Docker installed."


# ============================================================
# 6. Node.js 22 + Corepack
# ============================================================

echo ""
echo "[6/8] Install Node.js 22..."

if ! command -v node >/dev/null 2>&1; then

    if [ ! -f /etc/yum.repos.d/nodesource-nodejs.repo ]; then

        curl -fsSL https://rpm.nodesource.com/setup_22.x \
            -o /tmp/nodesource_setup.sh

        bash /tmp/nodesource_setup.sh

        rm -f /tmp/nodesource_setup.sh

    fi

    dnf install -y nodejs

else

    echo "[SKIP] Node.js is already installed."

fi

echo "[INFO] Node.js: $(node --version)"

corepack enable

echo "[OK] Corepack enabled."


# ============================================================
# 7. uv
# ============================================================

echo ""
echo "[7/8] Install uv..."

if command -v uv >/dev/null 2>&1; then

    echo "[SKIP] uv is already installed."

else

    curl -LsSf https://astral.sh/uv/install.sh | sh

    export PATH="/root/.local/bin:${PATH}"

fi

echo "[OK] uv installed."


# ============================================================
# 8. Nginx
# ============================================================

echo ""
echo "[8/8] Install Nginx..."

if ! command -v nginx >/dev/null 2>&1; then

    dnf install -y nginx

else

    echo "[SKIP] Nginx is already installed."

fi

systemctl enable --now nginx

echo "[OK] Nginx installed."


# ============================================================
# Firewall
# ============================================================

echo ""
echo "[Firewall] Configure HTTP/HTTPS..."

if systemctl is-active --quiet firewalld; then

    firewall-cmd --permanent --add-service=http
    firewall-cmd --permanent --add-service=https
    firewall-cmd --reload

    echo "[OK] HTTP/HTTPS allowed."

else

    echo "[SKIP] firewalld is not active."

fi


# ============================================================
# Verification
# ============================================================

echo ""
echo "========================================="
echo " Initialization Completed"
echo "========================================="

echo ""
echo "[OS]"
grep PRETTY_NAME /etc/os-release

echo ""
echo "[Memory]"
free -h

echo ""
echo "[Swap]"
swapon --show

echo ""
echo "[Disk]"
df -h /

echo ""
echo "[Docker]"
docker --version

echo ""
echo "[Docker Compose]"
docker compose version

echo ""
echo "[Node.js]"
node --version

echo ""
echo "[npm]"
npm --version

echo ""
echo "[Corepack]"
corepack --version

echo ""
echo "[pnpm]"
corepack pnpm --version

echo ""
echo "[uv]"
export PATH="/root/.local/bin:${PATH}"
uv --version

echo ""
echo "[Nginx]"
nginx -v

echo ""
echo "[Services]"
systemctl is-active docker
systemctl is-active nginx

echo ""
echo "========================================="
echo " Server initialization finished."
echo "========================================="