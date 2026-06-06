# -*- coding: utf-8 -*-
"""
Backup automático de la base de datos SQLite y exportación semanal a parquet.

API pública:
    backup_sqlite(env)             → copia DB de hoy a backups/
    cleanup_old_backups(days)      → elimina backups*.db antiguos
    export_parquet(env)            → exporta tablas de trades/capital a parquet
    cleanup_old_parquets(days)     → elimina backups*.parquet antiguos
    run_backup(env, force_parquet) → orquesta los cuatro anteriores
"""

import shutil
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from modules.logger import get_system_logger

_BACKUPS_DIR = Path(__file__).parent.parent / "backups"
_slog = get_system_logger()

_TABLAS_PARQUET = {
    "connors_operaciones": "connors_operaciones_{env}_{hoy}.parquet",
    "connors_capital":     "connors_capital_{env}_{hoy}.parquet",
}


def backup_sqlite(env: str) -> Path:
    """
    Copia data/{env}/trading_book.db a backups/trading_book_{env}_YYYYMMDD.db.
    Si ya existe el backup de hoy lo sobreescribe.

    Params:
        env: entorno activo ("dev", "pre", "pro").
    Returns:
        Path del fichero de backup generado.
    """
    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    dst = _BACKUPS_DIR / f"trading_book_{env}_{date.today().strftime('%Y%m%d')}.db"
    shutil.copy2(config.DB_PATH, dst)
    _slog.info("Backup SQLite: %s", dst.name)
    return dst


def cleanup_old_backups(retention_days: int = 30) -> int:
    """
    Elimina backups/trading_book_*.db con más de retention_days días.

    Params:
        retention_days: días máximos de retención.
    Returns:
        Número de ficheros eliminados.
    """
    if not _BACKUPS_DIR.exists():
        return 0
    cutoff = date.today() - timedelta(days=retention_days)
    n = 0
    for f in _BACKUPS_DIR.glob("trading_book_*.db"):
        try:
            ds = f.stem.split("_")[-1]
            file_date = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
            if file_date < cutoff:
                f.unlink()
                n += 1
        except Exception:
            pass
    if n:
        _slog.info("Eliminados %d backups SQLite antiguos", n)
    return n


def export_parquet(env: str) -> dict:
    """
    Exporta tablas de trades y capital a parquet en backups/.
    Solo exporta tablas con al menos una fila.

    Params:
        env: entorno activo.
    Returns:
        dict {nombre_tabla: Path} con los ficheros generados.
    """
    import pandas as pd

    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    hoy = date.today().strftime("%Y%m%d")
    generados: dict = {}

    with sqlite3.connect(str(config.DB_PATH)) as con:
        for tabla, patron in _TABLAS_PARQUET.items():
            fname = patron.format(env=env, hoy=hoy)
            try:
                df = pd.read_sql_query(f"SELECT * FROM {tabla}", con)
                if df.empty:
                    continue
                dst = _BACKUPS_DIR / fname
                df.to_parquet(dst, index=False)
                generados[tabla] = dst
            except Exception as exc:
                _slog.warning("export_parquet %s: %s", tabla, exc)

    if generados:
        _slog.info("Exportados %d ficheros parquet", len(generados))
    return generados


def cleanup_old_parquets(retention_days: int = 90) -> int:
    """
    Elimina backups/*.parquet con más de retention_days días.

    Params:
        retention_days: días máximos de retención.
    Returns:
        Número de ficheros eliminados.
    """
    if not _BACKUPS_DIR.exists():
        return 0
    cutoff = date.today() - timedelta(days=retention_days)
    n = 0
    for f in _BACKUPS_DIR.glob("*.parquet"):
        try:
            ds = f.stem.split("_")[-1]
            file_date = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
            if file_date < cutoff:
                f.unlink()
                n += 1
        except Exception:
            pass
    if n:
        _slog.info("Eliminados %d parquets antiguos", n)
    return n


def run_backup(env: str, force_parquet: bool = False) -> dict:
    """
    Orquesta el backup completo: SQLite + limpieza + parquet opcional.

    El parquet se exporta solo si es lunes o force_parquet=True.
    Cualquier fallo se registra en log y en el resultado, pero nunca
    interrumpe el flujo del llamador (autopiloto u opción manual).

    Params:
        env:           entorno activo ("dev", "pre", "pro").
        force_parquet: exportar parquet aunque no sea lunes.
    Returns:
        dict con claves: sqlite_path, parquet_files, errores.
    """
    resultado: dict = {"sqlite_path": None, "parquet_files": {}, "errores": []}

    try:
        resultado["sqlite_path"] = backup_sqlite(env)
    except Exception as exc:
        _slog.error("backup_sqlite falló: %s", exc, exc_info=True)
        resultado["errores"].append(f"backup_sqlite: {exc}")

    try:
        cleanup_old_backups(config.BACKUP_RETENTION_DAYS)
    except Exception as exc:
        _slog.warning("cleanup_old_backups: %s", exc)

    if date.today().weekday() == 0 or force_parquet:
        try:
            resultado["parquet_files"] = export_parquet(env)
            cleanup_old_parquets(config.PARQUET_RETENTION_DAYS)
        except Exception as exc:
            _slog.error("export_parquet falló: %s", exc, exc_info=True)
            resultado["errores"].append(f"export_parquet: {exc}")

    return resultado
