#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
mkdir -p secrets backups

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "Created .env from .env.example; set LAN_BIND_IP and review transport settings."
fi

if [ ! -s secrets/app_secret_key.txt ]; then
    if [ "$(id -u)" -eq 0 ]; then
        umask 077
        openssl rand -hex 32 > secrets/app_secret_key.txt
    elif command -v sudo >/dev/null 2>&1; then
        openssl rand -hex 32 | sudo tee secrets/app_secret_key.txt >/dev/null
    else
        echo "Run as root or install sudo to create the application secret." >&2
        exit 1
    fi
fi

# Optional credentials get empty placeholders so Compose can mount the secret
# files. Configure GitHub App values/private key before enabling publication.
for secret in github_app_private_key.pem github_webhook_secret.txt github_pat.txt tls_certificate.pem tls_private_key.pem; do
    if [ ! -e "secrets/$secret" ]; then
        if [ "$(id -u)" -eq 0 ]; then
            touch "secrets/$secret"
        elif command -v sudo >/dev/null 2>&1; then
            sudo touch "secrets/$secret"
        else
            touch "secrets/$secret"
        fi
    fi
done
if [ "$(id -u)" -eq 0 ]; then
    chown 10001:10001 secrets backups
    chown 10001:10001 secrets/app_secret_key.txt secrets/github_app_private_key.pem \
        secrets/github_webhook_secret.txt secrets/github_pat.txt
    chown 101:101 secrets/tls_certificate.pem secrets/tls_private_key.pem
    chmod 755 secrets
    chmod 400 secrets/app_secret_key.txt secrets/github_app_private_key.pem \
        secrets/github_webhook_secret.txt secrets/github_pat.txt \
        secrets/tls_certificate.pem secrets/tls_private_key.pem
    chmod 770 backups
elif command -v sudo >/dev/null 2>&1; then
    sudo chown 10001:10001 secrets backups
    sudo chown 10001:10001 secrets/app_secret_key.txt secrets/github_app_private_key.pem \
        secrets/github_webhook_secret.txt secrets/github_pat.txt
    sudo chown 101:101 secrets/tls_certificate.pem secrets/tls_private_key.pem
    sudo chmod 755 secrets
    sudo chmod 400 secrets/app_secret_key.txt secrets/github_app_private_key.pem \
        secrets/github_webhook_secret.txt secrets/github_pat.txt \
        secrets/tls_certificate.pem secrets/tls_private_key.pem
    sudo chmod 770 backups
else
    echo "Run this script as root or install sudo so UID 10001 can read secrets and write backups." >&2
    exit 1
fi

echo "Backend secrets are owned by UID 10001; TLS files are owned by UID 101 (the unprivileged Nginx user)."
echo "Use sudo install with the matching UID/GID and mode 0400 when replacing a secret file."
echo "Add the GitHub App key (or PAT), then start the stack."
