#!/usr/bin/env bash
set -Eeuo pipefail

SSH_TARGET="${1:-}"
REMOTE_DIRECTORY="${2:-solarview-alarms}"
UPLOAD_MODE="${3:---upload-only}"
SSH_IDENTITY_FILE="${SSH_IDENTITY_FILE:-}"

if [[ ! "${SSH_TARGET}" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.:-]+$ ]]; then
    echo "Uso: SSH_IDENTITY_FILE=/ruta/clave.pem $0 usuario@host [directorio] [--upload-only|--bootstrap|--full]" >&2
    exit 2
fi

if [[ ! "${REMOTE_DIRECTORY}" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "El directorio remoto contiene caracteres no permitidos." >&2
    exit 2
fi

case "${UPLOAD_MODE}" in
    --upload-only|--bootstrap|--full) ;;
    *)
        echo "Modo inválido: ${UPLOAD_MODE}" >&2
        exit 2
        ;;
esac

SSH_COMMAND=(ssh -o StrictHostKeyChecking=accept-new)
if [[ -n "${SSH_IDENTITY_FILE}" ]]; then
    if [[ ! -f "${SSH_IDENTITY_FILE}" ]]; then
        echo "No existe la clave SSH indicada: ${SSH_IDENTITY_FILE}" >&2
        exit 1
    fi
    SSH_COMMAND+=(-i "${SSH_IDENTITY_FILE}")
fi

"${SSH_COMMAND[@]}" "${SSH_TARGET}" "mkdir -p -- '${REMOTE_DIRECTORY}'"

printf -v RSYNC_REMOTE_SHELL '%q ' "${SSH_COMMAND[@]}"
rsync -az --human-readable --progress \
    -e "${RSYNC_REMOTE_SHELL}" \
    --exclude='.git/' \
    --include='.env.example' \
    --include='.env.production.example' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='.venv/' \
    --exclude='**/__pycache__/' \
    --exclude='*.py[cod]' \
    --exclude='*.orig' \
    --exclude='*.egg-info/' \
    --exclude='.pytest_cache/' \
    --exclude='.ruff_cache/' \
    --exclude='staticfiles/' \
    --exclude='backups/' \
    --exclude='graphify-out/' \
    --exclude='Documentacion_API_SolarView.pdf' \
    ./ "${SSH_TARGET}:${REMOTE_DIRECTORY}/"

echo "Código sincronizado en ${SSH_TARGET}:${REMOTE_DIRECTORY}."

if [[ "${UPLOAD_MODE}" != "--upload-only" ]]; then
    "${SSH_COMMAND[@]}" "${SSH_TARGET}" \
        "cd '${REMOTE_DIRECTORY}' && ./scripts/deploy_ec2.sh '${UPLOAD_MODE}'"
fi
