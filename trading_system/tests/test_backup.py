# -*- coding: utf-8 -*-
"""
Tests del módulo modules/backup.py.

Cubre:
  · backup_sqlite crea el fichero esperado
  · backup_sqlite sobreescribe si ya existe backup del día
  · cleanup_old_backups elimina ficheros más antiguos que retention_days
  · export_parquet no exporta tablas vacías
  · export_parquet exporta tablas con datos
  · run_backup con force_parquet=True siempre exporta parquet
  · run_backup sin force_parquet respeta el día de la semana
"""

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import config
import modules.backup as bk


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_backups(tmp_path, monkeypatch):
    """Redirige _BACKUPS_DIR a tmp y silencia el logger para todos los tests."""
    monkeypatch.setattr(bk, "_BACKUPS_DIR", tmp_path)
    monkeypatch.setattr(bk, "_slog", logging.getLogger("kairos.test.backup.null"))
    return tmp_path


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """BD SQLite vacía (solo schema, sin filas) y config.DB_PATH apuntando a ella."""
    db = tmp_path / "empty.db"
    with sqlite3.connect(str(db)) as con:
        for tabla in bk._TABLAS_PARQUET:
            con.execute(f"CREATE TABLE IF NOT EXISTS {tabla} (id INTEGER PRIMARY KEY)")
    monkeypatch.setattr(config, "DB_PATH", db)
    return db


# ── 1) backup_sqlite crea el fichero ─────────────────────────────────────────

def test_backup_sqlite_crea_fichero():
    path = bk.backup_sqlite(config.TRADING_ENV)
    assert path.exists()
    assert path.suffix == ".db"
    assert config.TRADING_ENV in path.name
    assert date.today().strftime("%Y%m%d") in path.name


# ── 2) backup_sqlite sobreescribe el mismo día ───────────────────────────────

def test_backup_sqlite_sobreescribe_mismo_dia():
    p1 = bk.backup_sqlite(config.TRADING_ENV)
    p2 = bk.backup_sqlite(config.TRADING_ENV)
    assert p1 == p2
    assert p2.exists()


# ── 3) cleanup elimina ficheros más antiguos que retention ───────────────────

def test_cleanup_elimina_antiguos(tmp_path):
    hoy = date.today()
    ficheros = {
        0:  f"trading_book_dev_{hoy.strftime('%Y%m%d')}.db",
        10: f"trading_book_dev_{(hoy - timedelta(days=10)).strftime('%Y%m%d')}.db",
        40: f"trading_book_dev_{(hoy - timedelta(days=40)).strftime('%Y%m%d')}.db",
    }
    for nombre in ficheros.values():
        (tmp_path / nombre).write_bytes(b"x")

    n = bk.cleanup_old_backups(retention_days=30)

    assert n == 1
    assert (tmp_path / ficheros[0]).exists()
    assert (tmp_path / ficheros[10]).exists()
    assert not (tmp_path / ficheros[40]).exists()


# ── 4) export_parquet con tablas vacías no genera ficheros ───────────────────

def test_export_parquet_tablas_vacias_no_exporta(empty_db):
    generados = bk.export_parquet(config.TRADING_ENV)
    assert generados == {}


# ── 5) export_parquet con datos genera fichero ───────────────────────────────

def test_export_parquet_con_datos(tmp_path, monkeypatch):
    # Crear BD temporal con un registro en connors_capital
    db = tmp_path / "con_datos.db"
    with sqlite3.connect(str(db)) as con:
        for tabla in bk._TABLAS_PARQUET:
            con.execute(f"CREATE TABLE IF NOT EXISTS {tabla} (id INTEGER PRIMARY KEY)")
        con.execute(
            "INSERT INTO connors_capital (id) VALUES (9999)"
        )
    monkeypatch.setattr(config, "DB_PATH", db)

    generados = bk.export_parquet(config.TRADING_ENV)

    assert "connors_capital" in generados
    assert generados["connors_capital"].exists()
    assert generados["connors_capital"].suffix == ".parquet"
    # Tablas sin datos no aparecen
    assert "connors_operaciones" not in generados


# ── 6) run_backup con force_parquet=True siempre exporta parquet ─────────────

def test_run_backup_force_parquet_exporta(monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(bk, "export_parquet",    lambda env: (llamadas.append(env), {})[1])
    monkeypatch.setattr(bk, "cleanup_old_parquets", lambda *a, **kw: 0)

    bk.run_backup(env=config.TRADING_ENV, force_parquet=True)

    assert len(llamadas) == 1


# ── 7) run_backup sin force respeta el día de la semana ──────────────────────

def test_run_backup_no_lunes_no_exporta_parquet(monkeypatch):
    """
    Sin force_parquet, export_parquet solo se llama si es lunes.
    El test se adapta al día real de ejecución.
    """
    llamadas: list = []
    monkeypatch.setattr(bk, "export_parquet",    lambda env: (llamadas.append(env), {})[1])
    monkeypatch.setattr(bk, "cleanup_old_parquets", lambda *a, **kw: 0)

    es_lunes = date.today().weekday() == 0
    bk.run_backup(env=config.TRADING_ENV, force_parquet=False)

    if es_lunes:
        assert len(llamadas) == 1
    else:
        assert len(llamadas) == 0
