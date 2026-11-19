#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# Entrypoint del contenedor (Cloud Run Jobs).
#
# Flujo:
#   1. Descarga de GCS: BD SQLite del entorno, universo y caché de yfinance.
#   2. Ejecuta `main.py --run $TRADING_RUN --env $TRADING_ENV` (sin abortar).
#   3. Sube SIEMPRE los logs a GCS.
#   4. Si la BD es válida (o el proceso salió con 0): sube la BD, guarda copia con
#      timestamp y sincroniza caché, exports y backups.
#   5. Devuelve el exit code original de Python.
#
# Variables: TRADING_ENV (pre), TRADING_RUN (exits), GCS_BUCKET_NAME (obligatoria).
# Layout en el bucket:
#   data/<env>/trading_book.db      BD viva del entorno
#   data/sp500_universe.csv         universo (no versionado en git)
#   data/cache/                     caché yfinance (compartida entre entornos)
#   exports/<env>/  backups/<env>/  logs/<env>/

set -uo pipefail   # sin -e: los fallos de GCS se toleran explícitamente

TRADING_ENV="${TRADING_ENV:-pre}"
TRADING_RUN="${TRADING_RUN:-exits}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TS_DIR="${APP_DIR}/trading_system"

log() { echo "[entrypoint] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

if [[ -z "${GCS_BUCKET_NAME:-}" ]]; then
    log "ERROR: GCS_BUCKET_NAME no está definida"
    exit 1
fi
case "${TRADING_ENV}" in dev|pre|pro) ;; *)
    log "ERROR: TRADING_ENV inválido: ${TRADING_ENV}"; exit 1 ;;
esac

BUCKET="gs://${GCS_BUCKET_NAME}"
DB_LOCAL="${TS_DIR}/data/${TRADING_ENV}/trading_book.db"
DB_REMOTE="${BUCKET}/data/${TRADING_ENV}/trading_book.db"

mkdir -p "${TS_DIR}/data/${TRADING_ENV}" "${TS_DIR}/data/cache" \
         "${TS_DIR}/logs" "${TS_DIR}/exports" "${TS_DIR}/backups"

# ── Comprobación de integridad (y checkpoint WAL) de la BD local ─────────────
db_ok() {
    [[ -f "${DB_LOCAL}" ]] || return 1
    python - "${DB_LOCAL}" <<'PY'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    if ok:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    sys.exit(0 if ok else 1)
except Exception:
    sys.exit(1)
PY
}

# ── 1. Descarga de estado ────────────────────────────────────────────────────
log "Descargando estado desde ${BUCKET} (env=${TRADING_ENV} run=${TRADING_RUN})"

if gcloud storage cp "${DB_REMOTE}" "${DB_LOCAL}" --quiet 2>/dev/null; then
    log "BD descargada: ${DB_REMOTE}"
    if ! db_ok; then
        log "ERROR: la BD descargada está corrupta; se aborta para no operar con estado dañado"
        exit 1
    fi
else
    log "AVISO: no existe ${DB_REMOTE} (primera ejecución) — se creará una BD nueva"
fi

gcloud storage cp "${BUCKET}/data/sp500_universe.csv" \
    "${TS_DIR}/data/sp500_universe.csv" --quiet 2>/dev/null \
    || log "AVISO: no se pudo descargar sp500_universe.csv"

gcloud storage rsync --recursive "${BUCKET}/data/cache" "${TS_DIR}/data/cache" \
    --quiet 2>/dev/null \
    || log "AVISO: caché vacía o no disponible (primera ejecución)"

# Logs del día ya subidos (system y trading comparten fichero por fecha entre
# la pasada 'all' y la 'exits'): se bajan para que el logger añada, no pise.
gcloud storage cp "${BUCKET}/logs/${TRADING_ENV}/*_$(date +%Y%m%d).log" \
    "${TS_DIR}/logs/" --quiet 2>/dev/null || true

# ── 2. Ejecución ─────────────────────────────────────────────────────────────
log "Lanzando main.py --run ${TRADING_RUN} --env ${TRADING_ENV}"
python "${TS_DIR}/main.py" --run "${TRADING_RUN}" --env "${TRADING_ENV}"
RC=$?
log "main.py terminó con exit code ${RC}"

# ── 3. Logs (siempre) ────────────────────────────────────────────────────────
gcloud storage rsync --recursive "${TS_DIR}/logs" \
    "${BUCKET}/logs/${TRADING_ENV}" --quiet \
    || log "AVISO: no se pudieron subir los logs"

# ── 4. Persistencia de estado ────────────────────────────────────────────────
if [[ ${RC} -eq 0 ]] || db_ok; then
    if db_ok; then
        STAMP="$(date +%Y%m%d_%H%M%S)"
        if gcloud storage cp "${DB_LOCAL}" "${DB_REMOTE}" --quiet; then
            log "BD subida: ${DB_REMOTE}"
        else
            log "ERROR: fallo subiendo la BD"
            [[ ${RC} -eq 0 ]] && RC=1   # perder el estado es un fallo aunque Python fuera OK
        fi
        gcloud storage cp "${DB_LOCAL}" \
            "${BUCKET}/backups/${TRADING_ENV}/trading_book_${TRADING_ENV}_${STAMP}.db" \
            --quiet || log "AVISO: no se pudo guardar la copia con timestamp"
        gcloud storage rsync --recursive "${TS_DIR}/backups" \
            "${BUCKET}/backups/${TRADING_ENV}" --quiet 2>/dev/null || true
    else
        log "AVISO: BD ausente o corrupta tras la ejecución — no se sube"
    fi
    gcloud storage rsync --recursive "${TS_DIR}/data/cache" "${BUCKET}/data/cache" \
        --quiet || log "AVISO: no se pudo sincronizar la caché"
    gcloud storage rsync --recursive "${TS_DIR}/exports" \
        "${BUCKET}/exports/${TRADING_ENV}" --quiet 2>/dev/null || true
else
    log "AVISO: ejecución fallida y BD no válida — no se sincroniza estado"
fi

exit ${RC}
