# -*- coding: utf-8 -*-
"""
Dos loggers singleton para el sistema de trading.

LOGGER TÉCNICO  (system)  — logs/system_YYYYMMDD.log
    Nivel DEBUG. Captura todo lo técnico: conexiones, descargas,
    errores con traceback. Retención 30 días.
    Formato: {timestamp} {nivel:<8} {módulo:<20} {mensaje}

LOGGER OPERATIVO (trading) — logs/trading_YYYYMMDD.log
    Nivel INFO. Solo eventos de negocio: aperturas, cierres,
    rebalanceos, capital. Retención 90 días.
    Formato: {timestamp} · {mensaje}

API pública:
    get_system_logger()  → logging.Logger  (singleton)
    get_trading_logger() → logging.Logger  (singleton)
"""

import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

_LOGS_DIR = Path(__file__).parent.parent / "logs"

_FMT_SYSTEM  = "%(asctime)s %(levelname)-8s %(module)-20s %(message)s"
_FMT_TRADING = "%(asctime)s · %(message)s"
_DATEFMT     = "%Y-%m-%d %H:%M:%S"

_system_logger:  logging.Logger | None = None
_trading_logger: logging.Logger | None = None


def _cleanup_old(prefix: str, keep_days: int) -> None:
    """Elimina ficheros {prefix}_YYYYMMDD.log más antiguos que keep_days."""
    cutoff = date.today() - timedelta(days=keep_days)
    pat = re.compile(rf"^{prefix}_(\d{{8}})\.log$")
    for f in _LOGS_DIR.glob(f"{prefix}_*.log"):
        m = pat.match(f.name)
        if not m:
            continue
        try:
            ds = m.group(1)
            file_date = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
            if file_date < cutoff:
                f.unlink()
        except Exception:
            pass


def _make_logger(
    name: str, prefix: str, level: int, fmt: str, keep_days: int
) -> logging.Logger:
    """
    Crea (o devuelve ya creado) el logger con un FileHandler
    apuntando a logs/{prefix}_YYYYMMDD.log.

    Si el directorio no existe se crea. Si falla la escritura en disco
    se imprime un warning en consola pero el sistema sigue funcionando.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # ya inicializado en este proceso

    logger.setLevel(level)
    logger.propagate = False

    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_old(prefix, keep_days)
        log_path = _LOGS_DIR / f"{prefix}_{date.today().strftime('%Y%m%d')}.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt, datefmt=_DATEFMT))
        logger.addHandler(handler)
    except Exception as exc:
        # No bloqueamos el arranque si el log no puede escribirse
        try:
            from rich.console import Console
            Console().print(f"[yellow]⚠ No se pudo inicializar log '{prefix}': {exc}[/yellow]")
        except Exception:
            pass

    return logger


def get_system_logger() -> logging.Logger:
    """
    Devuelve el logger técnico (DEBUG+). Singleton — misma instancia en todo el proceso.

    Returns:
        logging.Logger configurado para logs/system_YYYYMMDD.log.
    """
    global _system_logger
    if _system_logger is None:
        _system_logger = _make_logger(
            "kairos.system", "system", logging.DEBUG,
            _FMT_SYSTEM, config.LOG_SYSTEM_RETENTION_DAYS,
        )
    return _system_logger


def get_trading_logger() -> logging.Logger:
    """
    Devuelve el logger operativo (INFO+). Singleton — misma instancia en todo el proceso.

    Returns:
        logging.Logger configurado para logs/trading_YYYYMMDD.log.
    """
    global _trading_logger
    if _trading_logger is None:
        _trading_logger = _make_logger(
            "kairos.trading", "trading", logging.INFO,
            _FMT_TRADING, config.LOG_TRADING_RETENTION_DAYS,
        )
    return _trading_logger
