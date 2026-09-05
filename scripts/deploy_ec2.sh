#!/usr/bin/env bash
set -Eeuo pipefail

DEPLOY_ENV_FILE="${DEPLOY_ENV_FILE:-.env.production}"
DEPLOY_MODE="${1:---full}"
DEPLOY_PROJECT_NAME="${DEPLOY_PROJECT_NAME:-solarview-alarms}"
BACKUP_DIRECTORY="${BACKUP_DIRECTORY:-backups}"
export DEPLOY_ENV_FILE

if [[ ! -f "${DEPLOY_ENV_FILE}" ]]; then
    echo "No existe ${DEPLOY_ENV_FILE}. Copia .env.production.example y completa los valores." >&2
    exit 1
fi

if grep -Eq '^(SECRET_KEY|POSTGRES_PASSWORD)=($|reemplazar-)' "${DEPLOY_ENV_FILE}"; then
    echo "SECRET_KEY y POSTGRES_PASSWORD deben tener valores reales antes de desplegar." >&2
    exit 1
fi

if grep -Eq '^PUBLIC_HOST=(|alarmas\.example\.com)$' "${DEPLOY_ENV_FILE}"; then
    echo "PUBLIC_HOST debe contener el dominio real de la instancia." >&2
    exit 1
fi

case "${DEPLOY_MODE}" in
    --bootstrap|--full) ;;
    *)
        echo "Uso: $0 [--bootstrap|--full]" >&2
        exit 2
        ;;
esac

COMPOSE=(docker compose -p "${DEPLOY_PROJECT_NAME}" --env-file "${DEPLOY_ENV_FILE}" -f docker-compose.prod.yml)

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d --wait postgres redis
"${COMPOSE[@]}" stop beat || true

mkdir -p "${BACKUP_DIRECTORY}"
BACKUP_FILE="${BACKUP_DIRECTORY}/solarview_alarms-$(date +%F-%H%M%S).sql.gz"
BACKUP_TEMP="${BACKUP_FILE}.tmp"
if "${COMPOSE[@]}" exec -T postgres sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "${BACKUP_TEMP}"; then
    mv "${BACKUP_TEMP}" "${BACKUP_FILE}"
    echo "Backup previo guardado en ${BACKUP_FILE}."
else
    rm -f "${BACKUP_TEMP}"
    echo "Falló el backup previo; se cancela el despliegue." >&2
    exit 1
fi

"${COMPOSE[@]}" run --rm web python manage.py migrate --noinput
"${COMPOSE[@]}" run --rm web python manage.py collectstatic --noinput
"${COMPOSE[@]}" up -d web worker caddy

if [[ "${DEPLOY_MODE}" == "--full" ]]; then
    "${COMPOSE[@]}" up -d beat
    echo "Despliegue completo: Celery Beat está activo."
else
    echo "Bootstrap listo: Beat permanece apagado hasta terminar la configuración de Discord."
    echo "Actívalo con: DEPLOY_ENV_FILE=${DEPLOY_ENV_FILE} $0 --full"
fi

"${COMPOSE[@]}" ps
