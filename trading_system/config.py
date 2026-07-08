# -*- coding: utf-8 -*-
"""Configuración centralizada del sistema de trading — ConnorsRSI."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Carga las variables del fichero .env en la raíz del proyecto (un nivel por
# encima de trading_system/). Si el fichero no existe, load_dotenv() no falla;
# las API keys quedan vacías y el sistema cae al modo "sólo SQLite" sin Alpaca.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Entorno de ejecución (dev / pre / pro) ────────────────────────────────────
# Determina qué BD SQLite usa el sistema. Cada entorno tiene su propio fichero
# bajo `trading_system/data/<env>/trading_book.db`, así dev/pre/pro no se
# mezclan. Se establece vía variable de entorno TRADING_ENV (típicamente por
# argparse en main.py: `python main.py --env dev`). La caché de yfinance
# (`data/cache/`) es compartida entre entornos.
_VALID_ENVS = {"dev", "pre", "pro"}
TRADING_ENV = os.environ.get("TRADING_ENV", "").strip().lower()
if TRADING_ENV not in _VALID_ENVS:
    raise RuntimeError(
        f"TRADING_ENV no establecido o invalido (recibido: {TRADING_ENV!r}). "
        f"Lanza main.py con `--env {{dev|pre|pro}}` o exporta TRADING_ENV "
        f"antes de importar config."
    )

_BASE_DIR = Path(__file__).resolve().parent
DATA_DIR  = _BASE_DIR / "data" / TRADING_ENV
DB_PATH   = DATA_DIR / "trading_book.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Universo de tickers ──────────────────────────────────────────────────────
# Definido en un CSV externo (ticker, sector, activo). Cargado vía
# `modules.data.load_universe()`. Solo NYSE/NASDAQ — sin sufijos de mercado.
UNIVERSE_FILE = "data/sp500_universe.csv"

# ── Simulación ───────────────────────────────────────────────────────────────
COMMISSION = 0.0015           # 0.15 % por operación

# Capital "de juego" real con el que operamos, usado como base fija para el
# PnL% global del panel de capital. Aunque la cuenta Alpaca tenga más fondos
# (p.ej. 100.000 €), la rentabilidad global se mide sobre este importe.
CAPITAL_JUEGO = 10_000.0

# Hora (HH:MM, hora local) a la que el Task Scheduler lanza el autopiloto tras
# el cierre NYSE. Solo informativo: el panel de la opción 9 la usa para mostrar
# la próxima ejecución prevista (respetando el calendario NYSE).
AUTOPILOT_EXEC_TIME = "22:30"

# ── ConnorsRSI — estrategia única ─────────────────────────────────────────────
CONNORS_RSI_PERIOD    = 3        # RSI del precio
CONNORS_STREAK_PERIOD = 2        # RSI del streak
CONNORS_RANK_PERIOD   = 100      # períodos para percentil
CONNORS_ENTRY_CRSI    = 25       # umbral de entrada (CRSI < 25)
CONNORS_EXIT_CRSI     = 60       # umbral de salida (CRSI > 60)
CONNORS_STOP_LOSS     = 0.05     # 5 %
CONNORS_TIME_STOP     = 5        # días máximos abierta
CONNORS_MIN_STREAK    = 3        # nº mínimo de días consecutivos bajando para entrar
CONNORS_ENTRY_LIMIT   = 0.01     # 1 % bajo el cierre
CONNORS_CAPITAL       = 10000.0  # capital total dedicado a la estrategia
CONNORS_MAX_POS       = 3        # posiciones simultáneas

# Tickers de la misma empresa que no deben comprarse simultáneamente.
# Si cualquier ticker de un grupo está ocupado (abierto o pendiente),
# el resto del grupo queda excluido de los candidatos ese día.
MISMA_EMPRESA_GRUPOS: list[set] = [
    {"GOOGL", "GOOG"},   # Alphabet — correlación ~0.99
    {"FOX",   "FOXA"},   # Fox Corporation — correlación ~0.98
]

# ── Backtesting ───────────────────────────────────────────────────────────────
DEFAULT_START_DATE = "1999-01-01"
DEFAULT_END_DATE = None   # None = hoy

# ── Períodos históricos para backtesting de estrés ────────────────────────────
# Cada período empieza ANTES de que estalle la crisis para evaluar cómo se
# comporta el sistema en la fase alcista previa Y luego durante la caída.
HISTORICAL_PERIODS = {
    "1": {
        "nombre": "Pre-Puntocom",
        "inicio": "1999-01-01",
        "fin":    "2002-10-31",
        "crisis": "Marzo 2000",
        "descripcion": "Sistema arranca 14 meses antes del crash tecnológico",
        "spy_retorno": "-49%",
    },
    "2": {
        "nombre": "Pre-Crisis Financiera Global",
        "inicio": "2006-01-01",
        "fin":    "2009-03-31",
        "crisis": "Octubre 2007",
        "descripcion": "Sistema arranca 21 meses antes de Lehman Brothers",
        "spy_retorno": "-57%",
    },
    "3": {
        "nombre": "Pre-Corrección China + Brexit",
        "inicio": "2014-01-01",
        "fin":    "2016-06-30",
        "crisis": "Junio 2015",
        "descripcion": "Sistema arranca 18 meses antes del desplome Yuan",
        "spy_retorno": "-15%",
    },
    "4": {
        "nombre": "Pre-Corrección Q4 2018",
        "inicio": "2017-01-01",
        "fin":    "2018-12-31",
        "crisis": "Octubre 2018",
        "descripcion": "Sistema arranca 21 meses antes del crash de fin de año",
        "spy_retorno": "-20%",
    },
    "5": {
        "nombre": "Pre-COVID",
        "inicio": "2019-06-01",
        "fin":    "2020-12-31",
        "crisis": "Febrero 2020",
        "descripcion": "Sistema arranca 8 meses antes del crash más rápido",
        "spy_retorno": "-34%",
    },
    "6": {
        "nombre": "Pre-Bajista 2022",
        "inicio": "2021-01-01",
        "fin":    "2022-12-31",
        "crisis": "Enero 2022",
        "descripcion": "Sistema arranca 12 meses antes de inflación y tipos",
        "spy_retorno": "-20%",
    },
    "7": {
        "nombre": "Out-of-sample 2010-2019",
        "inicio": "2010-01-01",
        "fin":    "2019-12-31",
        "crisis": "Varias correcciones",
        "descripcion": "Período no usado en optimización — 3 correcciones incluidas",
        "spy_retorno": "+257%",
    },
    "8": {
        "nombre": "Histórico completo 1999-2026",
        "inicio": "1999-01-01",
        "fin":    "2026-05-23",
        "crisis": "Todas",
        "descripcion": "Todo el histórico disponible — todas las crisis",
        "spy_retorno": "+450%",
    },
}

# ── Alpaca Markets ────────────────────────────────────────────────────────────
# Integración con Alpaca para enviar órdenes simuladas o reales. SQLite sigue
# siendo la fuente de verdad; Alpaca actúa como capa de ejecución opcional.
ALPACA_ENABLED    = True                                # True para activar
ALPACA_PAPER      = True                                 # True = paper, False = real
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# URL automática según modo paper/real.
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER else
    "https://api.alpaca.markets"
)

# ── Publicación de track record en GitHub ─────────────────────────────────────
# Sube public_logs/<env>/RESUMEN.md a un repo público tras el autopiloto, en
# DEV, PRE y PRO (cada entorno a su carpeta). Las credenciales se leen del .env,
# nunca se hardcodean ni se publican.
GITHUB_ENABLED = os.environ.get("GITHUB_ENABLED", "false").strip().lower() == "true"
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO    = os.environ.get("GITHUB_REPO", "")   # formato 'usuario/repo'

# ── Reintentos yfinance ───────────────────────────────────────────────────────
YF_RETRY_ATTEMPTS = 8    # máximo de reintentos esperando el cierre de la sesión
YF_RETRY_WAIT_MIN = 30   # minutos entre reintentos (8 × 30 = 4 h; cruza medianoche)

# ── Backup ────────────────────────────────────────────────────────────────────
BACKUP_RETENTION_DAYS  = 30   # días retención backups SQLite
PARQUET_RETENTION_DAYS = 90   # días retención exports parquet

# ── Logs ──────────────────────────────────────────────────────────────────────
LOG_SYSTEM_RETENTION_DAYS  = 30    # días retención log técnico (system_YYYYMMDD.log)
LOG_TRADING_RETENTION_DAYS = 90    # días retención log operativo (trading_YYYYMMDD.log)


# ── Overrides por entorno ─────────────────────────────────────────────────────
# Lee trading_system/config/{env}.properties y sobreescribe variables escalares
# (bool, int, float, str) de este módulo. Permite ajustar parámetros por entorno
# sin tocar este fichero. Las claves desconocidas y los tipos complejos (dicts)
# se ignoran silenciosamente.
def _load_env_overrides(env: str) -> None:
    """
    Aplica los overrides de config/{env}.properties sobre el namespace del módulo.

    Params:
        env: nombre del entorno activo ("dev", "pre", "pro").
    """
    props = _BASE_DIR / "config" / f"{env}.properties"
    if not props.exists():
        return
    ns = globals()
    for linea in props.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        if "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        clave = clave.strip()
        valor = valor.strip()
        if clave not in ns or clave.startswith("_"):
            continue
        actual = ns[clave]
        if not isinstance(actual, (bool, int, float, str)):
            continue
        # bool antes que int: isinstance(True, int) == True en Python
        if isinstance(actual, bool):
            ns[clave] = valor.lower() in ("true", "1", "yes")
        elif isinstance(actual, int):
            ns[clave] = int(valor)
        elif isinstance(actual, float):
            ns[clave] = float(valor)
        else:
            ns[clave] = valor


_load_env_overrides(TRADING_ENV)

# ALPACA_BASE_URL se deriva de ALPACA_PAPER — recalcular tras overrides.
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER else
    "https://api.alpaca.markets"
)
