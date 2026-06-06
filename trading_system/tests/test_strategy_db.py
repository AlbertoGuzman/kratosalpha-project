# -*- coding: utf-8 -*-
"""Tests para modules/strategy_db.py — helpers SQLite de ConnorsRSI."""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from modules import strategy_db


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_vacia(tmp_path) -> Path:
    """BD temporal con tabla capital y portfolio mínimos para los tests."""
    db = tmp_path / "test.db"
    con = sqlite3.connect(str(db))
    con.execute("""
        CREATE TABLE capital_test (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            cash REAL NOT NULL,
            valor_posiciones REAL NOT NULL,
            capital_total REAL NOT NULL,
            nota TEXT
        )
    """)
    con.execute("""
        CREATE TABLE portfolio_test (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            shares REAL,
            entry_price REAL
        )
    """)
    con.commit()
    con.close()
    return db


# ── get_connection ────────────────────────────────────────────────────────────

class TestGetConnection:
    def test_crea_bd_si_no_existe(self, tmp_path):
        db = tmp_path / "nueva.db"
        assert not db.exists()
        con = strategy_db.get_connection(db)
        con.close()
        assert db.exists()

    def test_crea_directorio_intermedio(self, tmp_path):
        db = tmp_path / "sub" / "dir" / "nueva.db"
        con = strategy_db.get_connection(db)
        con.close()
        assert db.exists()

    def test_row_factory_sqlite_row(self, tmp_path):
        db = tmp_path / "r.db"
        con = strategy_db.get_connection(db)
        assert con.row_factory is sqlite3.Row
        con.close()


# ── get_current_cash ──────────────────────────────────────────────────────────

class TestGetCurrentCash:
    def test_devuelve_default_si_tabla_vacia(self, db_vacia):
        resultado = strategy_db.get_current_cash(db_vacia, "capital_test", 5000.0)
        assert resultado == 5000.0

    def test_devuelve_ultimo_cash(self, db_vacia):
        con = sqlite3.connect(str(db_vacia))
        con.execute(
            "INSERT INTO capital_test (fecha, cash, valor_posiciones, capital_total, nota) "
            "VALUES ('2024-01-01', 1000.0, 0.0, 1000.0, 'inicio')"
        )
        con.execute(
            "INSERT INTO capital_test (fecha, cash, valor_posiciones, capital_total, nota) "
            "VALUES ('2024-01-02', 900.0, 100.0, 1000.0, 'compra')"
        )
        con.commit()
        con.close()
        resultado = strategy_db.get_current_cash(db_vacia, "capital_test", 5000.0)
        assert resultado == pytest.approx(900.0)


# ── append_capital ────────────────────────────────────────────────────────────

class TestAppendCapital:
    def test_inserta_fila(self, db_vacia):
        strategy_db.append_capital(db_vacia, "capital_test", 8000.0, 2000.0, "rebalanceo")
        con = sqlite3.connect(str(db_vacia))
        row = con.execute("SELECT * FROM capital_test ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        assert row[2] == pytest.approx(8000.0)       # cash
        assert row[3] == pytest.approx(2000.0)       # valor_posiciones
        assert row[4] == pytest.approx(10000.0)      # capital_total = 8000+2000
        assert row[5] == "rebalanceo"

    def test_capital_total_es_suma(self, db_vacia):
        strategy_db.append_capital(db_vacia, "capital_test", 3000.0, 1500.0, "test")
        con = sqlite3.connect(str(db_vacia))
        row = con.execute("SELECT capital_total FROM capital_test").fetchone()
        con.close()
        assert row[0] == pytest.approx(4500.0)

    def test_fecha_es_hoy(self, db_vacia):
        strategy_db.append_capital(db_vacia, "capital_test", 100.0, 0.0, "x")
        con = sqlite3.connect(str(db_vacia))
        row = con.execute("SELECT fecha FROM capital_test").fetchone()
        con.close()
        assert row[0] == date.today().isoformat()


# ── get_open_positions ────────────────────────────────────────────────────────

class TestGetEstrategiaPorTicker:
    @pytest.fixture
    def db_estrategias(self, tmp_path) -> Path:
        """BD con operaciones ConnorsRSI en distintos estados."""
        db = tmp_path / "estr.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones
                (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, estado TEXT,
                 exit_date TEXT);
            INSERT INTO connors_operaciones (ticker, estado) VALUES
                ('AAPL','abierta'), ('NVDA','abierta'), ('TSLA','pendiente');
        """)
        con.commit()
        con.close()
        return db

    def test_posiciones_mapea_estrategia(self, db_estrategias):
        mapa = strategy_db.get_estrategia_por_ticker(db_estrategias, "posicion")
        assert mapa == {"AAPL": "ConnorsRSI", "NVDA": "ConnorsRSI"}

    def test_ordenes_mapea_estrategia(self, db_estrategias):
        """Órdenes: estado 'pendiente' (entradas BUY) + 'abierta' (salidas SELL)."""
        mapa = strategy_db.get_estrategia_por_ticker(db_estrategias, "orden")
        assert mapa == {
            "TSLA": "ConnorsRSI",                        # pendiente (entrada)
            "AAPL": "ConnorsRSI", "NVDA": "ConnorsRSI",  # abierta (salidas)
        }

    def test_orden_venta_se_resuelve_desde_abierta(self, db_estrategias):
        """
        Regresión: una orden SELL (cierre) corresponde a una operación 'abierta',
        no 'pendiente'. NVDA está 'abierta' → debe etiquetarse 'ConnorsRSI', no '?'.
        """
        mapa = strategy_db.get_estrategia_por_ticker(db_estrategias, "orden")
        assert mapa.get("NVDA", "?") == "ConnorsRSI"

    def test_ticker_desconocido_via_get(self, db_estrategias):
        mapa = strategy_db.get_estrategia_por_ticker(db_estrategias, "posicion")
        assert mapa.get("GOOG", "?") == "?"

    def test_tabla_inexistente_devuelve_vacio(self, db_vacia):
        """BD sin connors_operaciones → mapa vacío, sin crashear."""
        assert strategy_db.get_estrategia_por_ticker(db_vacia, "posicion") == {}
        assert strategy_db.get_estrategia_por_ticker(db_vacia, "orden") == {}

    def test_tipo_invalido_lanza_valueerror(self, db_vacia):
        with pytest.raises(ValueError):
            strategy_db.get_estrategia_por_ticker(db_vacia, "xxx")

    def test_fallback_ticker_a_medio_cerrar_desde_cerradas(self, tmp_path):
        """
        CBOE ya no tiene operación activa (cerrada en SQLite) pero sigue abierto
        en Alpaca a medio vender → se resuelve desde las operaciones cerradas,
        tanto en vista de posiciones como de órdenes.
        """
        db = tmp_path / "midclose.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones
                (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, estado TEXT,
                 exit_date TEXT);
            INSERT INTO connors_operaciones (ticker, estado, exit_date)
                VALUES ('CBOE', 'cerrada', '2026-06-04');
        """)
        con.commit()
        con.close()
        assert strategy_db.get_estrategia_por_ticker(db, "posicion").get("CBOE") == "ConnorsRSI"
        assert strategy_db.get_estrategia_por_ticker(db, "orden").get("CBOE") == "ConnorsRSI"

    def test_activa_tiene_prioridad_sobre_cerrada(self, tmp_path):
        """Una operación activa (abierta) y otra cerrada del mismo ticker (re-entrada)
        resuelven igualmente a ConnorsRSI vía la rama de activas."""
        db = tmp_path / "prio.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones
                (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT, estado TEXT,
                 exit_date TEXT);
            INSERT INTO connors_operaciones (ticker, estado, exit_date)
                VALUES ('AAA', 'abierta', NULL), ('AAA', 'cerrada', '2026-06-04');
        """)
        con.commit()
        con.close()
        assert strategy_db.get_estrategia_por_ticker(db, "posicion")["AAA"] == "ConnorsRSI"


class TestGetOpenPositions:
    def test_devuelve_dict_vacio_si_no_hay_posiciones(self, db_vacia):
        resultado = strategy_db.get_open_positions(db_vacia, "portfolio_test")
        assert resultado == {}

    def test_devuelve_dict_keyed_por_ticker(self, db_vacia):
        con = sqlite3.connect(str(db_vacia))
        con.execute(
            "INSERT INTO portfolio_test (ticker, shares, entry_price) VALUES ('AAPL', 10, 150.0)"
        )
        con.execute(
            "INSERT INTO portfolio_test (ticker, shares, entry_price) VALUES ('MSFT', 5, 300.0)"
        )
        con.commit()
        con.close()
        resultado = strategy_db.get_open_positions(db_vacia, "portfolio_test")
        assert "AAPL" in resultado
        assert "MSFT" in resultado
        assert resultado["AAPL"]["shares"] == pytest.approx(10)
        assert resultado["MSFT"]["entry_price"] == pytest.approx(300.0)
