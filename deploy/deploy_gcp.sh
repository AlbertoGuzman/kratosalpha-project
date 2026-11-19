#!/usr/bin/env bash
# Despliegue en Google Cloud: build en Artifact Registry, Cloud Run Jobs y
# Cloud Scheduler (zona America/New_York).
#
# Uso:
#   PROJECT_ID=mi-proyecto GCS_BUCKET_NAME=mi-bucket ./deploy/deploy_gcp.sh [pre|dev|pro]
#
# Variables opcionales: REGION (europe-west1), AR_REPO (trading),
#   SA_NAME (trading-runner), SCHED_SA_NAME (trading-scheduler).
#
# Secretos (Secret Manager) esperados por entorno, nombre kratos-<env>-<clave>:
#   alpaca-api-key, alpaca-secret-key           (obligatorios)
#   github-token, github-repo                   (opcionales: publicación track record)
#   telegram-token, telegram-chat-id            (opcionales: notificaciones)
# Se crean a mano, p. ej.:
#   printf '%s' "$VALOR" | gcloud secrets create kratos-pre-alpaca-api-key --data-file=-
# Los opcionales que no existan se omiten.

set -euo pipefail

ENV_NAME="${1:-pre}"
case "${ENV_NAME}" in dev|pre|pro) ;; *) echo "Entorno inválido: ${ENV_NAME}" >&2; exit 1 ;; esac

: "${PROJECT_ID:?Define PROJECT_ID}"
: "${GCS_BUCKET_NAME:?Define GCS_BUCKET_NAME}"
REGION="${REGION:-europe-west1}"
AR_REPO="${AR_REPO:-trading}"
SA_NAME="${SA_NAME:-trading-runner}"
SCHED_SA_NAME="${SCHED_SA_NAME:-trading-scheduler}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA_EMAIL="${SCHED_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/trading:latest"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "▶ Proyecto=${PROJECT_ID} región=${REGION} env=${ENV_NAME} bucket=${GCS_BUCKET_NAME}"
gcloud config set project "${PROJECT_ID}" >/dev/null

# ── APIs ─────────────────────────────────────────────────────────────────────
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
    cloudbuild.googleapis.com artifactregistry.googleapis.com \
    secretmanager.googleapis.com storage.googleapis.com

# ── Artifact Registry y bucket (idempotente) ─────────────────────────────────
gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1 \
    || gcloud artifacts repositories create "${AR_REPO}" \
        --repository-format=docker --location "${REGION}"

gcloud storage buckets describe "gs://${GCS_BUCKET_NAME}" >/dev/null 2>&1 \
    || gcloud storage buckets create "gs://${GCS_BUCKET_NAME}" \
        --location "${REGION}" --uniform-bucket-level-access

# ── Cuentas de servicio ──────────────────────────────────────────────────────
for sa in "${SA_NAME}" "${SCHED_SA_NAME}"; do
    gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
        || gcloud iam service-accounts create "${sa}"
done
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET_NAME}" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SA_EMAIL}" --role roles/secretmanager.secretAccessor \
    --condition=None >/dev/null

# ── Build ────────────────────────────────────────────────────────────────────
gcloud builds submit "${ROOT}" --tag "${IMAGE}"

# ── Secretos: solo los que existen ───────────────────────────────────────────
declare -A SECRET_MAP=(
    [ALPACA_API_KEY]=alpaca-api-key
    [ALPACA_SECRET_KEY]=alpaca-secret-key
    [GITHUB_TOKEN]=github-token
    [GITHUB_REPO]=github-repo
    [TELEGRAM_TOKEN]=telegram-token
    [TELEGRAM_CHAT_ID]=telegram-chat-id
)
SECRETS_ARG=""
for var in "${!SECRET_MAP[@]}"; do
    name="kratos-${ENV_NAME}-${SECRET_MAP[$var]}"
    if gcloud secrets describe "${name}" >/dev/null 2>&1; then
        SECRETS_ARG+="${var}=${name}:latest,"
    else
        echo "  · secreto ${name} no existe — se omite ${var}"
    fi
done
SECRETS_ARG="${SECRETS_ARG%,}"
[[ "${SECRETS_ARG}" == *ALPACA_API_KEY* && "${SECRETS_ARG}" == *ALPACA_SECRET_KEY* ]] \
    || { echo "Faltan los secretos de Alpaca (kratos-${ENV_NAME}-alpaca-*)" >&2; exit 1; }

GITHUB_FLAG="false"
[[ "${SECRETS_ARG}" == *GITHUB_TOKEN* && "${SECRETS_ARG}" == *GITHUB_REPO* ]] && GITHUB_FLAG="true"

# ── Jobs + Scheduler ─────────────────────────────────────────────────────────
# max-retries=0 y tasks=1: un reintento automático podría duplicar órdenes.
deploy_job() {   # $1=modo(all|exits)  $2=cron  $3=timeout
    local mode="$1" cron="$2" timeout="$3"
    local job="trading-${ENV_NAME}-${mode}"

    gcloud run jobs deploy "${job}" \
        --image "${IMAGE}" --region "${REGION}" \
        --service-account "${SA_EMAIL}" \
        --tasks 1 --parallelism 1 --max-retries 0 \
        --task-timeout "${timeout}" --cpu 1 --memory 2Gi \
        --set-env-vars "TRADING_ENV=${ENV_NAME},TRADING_RUN=${mode},GCS_BUCKET_NAME=${GCS_BUCKET_NAME},GITHUB_ENABLED=${GITHUB_FLAG}" \
        --set-secrets "${SECRETS_ARG}"

    gcloud run jobs add-iam-policy-binding "${job}" --region "${REGION}" \
        --member "serviceAccount:${SCHED_SA_EMAIL}" --role roles/run.invoker >/dev/null

    local uri="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${job}:run"
    local verb="create"
    gcloud scheduler jobs describe "${job}" --location "${REGION}" >/dev/null 2>&1 && verb="update"
    gcloud scheduler jobs "${verb}" http "${job}" \
        --location "${REGION}" \
        --schedule "${cron}" --time-zone "America/New_York" \
        --uri "${uri}" --http-method POST \
        --oauth-service-account-email "${SCHED_SA_EMAIL}"
    echo "  ✓ ${job}  (${cron} America/New_York)"
}

deploy_job all   "0 5 * * 1-5"   "30m"   # 05:00 ET
deploy_job exits "45 14 * * 1-5" "15m"   # 14:45 ET (deadline MOC 15:50 ET)

cat <<EOF

✓ Despliegue completado.
  Siguiente paso (primera vez): subir el estado inicial al bucket:
    gcloud storage cp trading_system/data/sp500_universe.csv gs://${GCS_BUCKET_NAME}/data/
    gcloud storage cp trading_system/data/${ENV_NAME}/trading_book.db gs://${GCS_BUCKET_NAME}/data/${ENV_NAME}/
    gcloud storage rsync -r trading_system/data/cache gs://${GCS_BUCKET_NAME}/data/cache   # opcional
  Prueba manual:  gcloud run jobs execute trading-${ENV_NAME}-exits --region ${REGION} --wait
EOF
