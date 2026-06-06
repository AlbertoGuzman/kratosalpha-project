# -*- coding: utf-8 -*-
"""Tests del módulo operations_log.py — inspección de paper trading."""

import sqlite3

import pandas as pd
import pytest

from modules.operations_log import (
    estadisticas_por_ticker,
    exportar_filtrado,
    filtrar_trades,
    resumen_pnl_por_mes,
    ver_abiertas_unificadas,
    ver_diario_cronologico,
    ver_detalle_trade,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_with_data(tmp_path):
    """
    BD temporal con las tablas de ConnorsRSI y datos sintéticos:
      · 5 trades cerrados (2 AAPL, 1 MSFT, 2 NVDA)
      · 2 posiciones abiertas (GOOGL, TSLA)
    PnL total esperado: +100 −100 +100 +30 −50 = +80
    """
    import modules.connors_rsi as cr
    db = tmp_path / "trading_book.db"
    con = sqlite3.connect(str(db))
    # Esquema real de producción (única fuente de verdad).
    for stmt in cr._DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    cur = con.cursor()
    # 5 operaciones CERRADAS
    cur.executemany(
        "INSERT INTO connors_operaciones "
        "(ticker, estado, entry_date, exit_date, entry_price, limit_price, "
        " exit_price, shares, valor_compra, slot_size, stop_loss_price, "
        " net_pnl, return_pct, dias, reason) "
        "VALUES (?, 'cerrada', ?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("AAPL", "2024-01-15", "2024-02-20", 100.0,  99.0, 110.0, 10.0, 1000.0, 1000.0,  95.0,
              100.0,  0.10, 36, "CRSI_EXIT"),
            ("AAPL", "2024-03-01", "2024-03-15", 110.0, 108.9, 100.0, 10.0, 1100.0, 1100.0, 104.5,
             -100.0, -0.09, 14, "SL"),
            ("MSFT", "2024-04-10", "2024-05-10", 200.0, 198.0, 220.0,  5.0, 1000.0, 1000.0, 190.0,
              100.0,  0.10, 30, "CRSI_EXIT"),
            ("NVDA", "2024-02-01", "2024-02-05", 500.0, 495.0, 510.0, 2.0, 1000.0, 1000.0, 475.0,
              30.0,  0.030, 4, "CRSI_EXIT"),
            ("NVDA", "2024-03-10", "2024-03-15", 520.0, 515.0, 490.0, 2.0, 1040.0, 1040.0, 494.0,
             -50.0, -0.048, 5, "SL"),
        ],
    )
    # 2 posiciones ABIERTAS
    cur.execute(
        "INSERT INTO connors_operaciones "
        "(ticker, estado, entry_date, entry_price, limit_price, shares, valor_compra, "
        " slot_size, stop_loss_price, connors_rsi_entrada) "
        "VALUES ('GOOGL','abierta','2024-05-01',150.0,148.5,6.0,900.0,900.0,142.5,17.0)"
    )
    cur.execute(
        "INSERT INTO connors_operaciones "
        "(ticker, estado, entry_date, entry_price, limit_price, shares, valor_compra, "
        " slot_size, stop_loss_price, connors_rsi_entrada) "
        "VALUES ('TSLA','abierta','2024-05-05',180.0,178.2,5.5,980.0,980.0,168.3,15.2)"
    )
    con.commit()
    con.close()
    return db


# ── Tests de filtrar_trades ───────────────────────────────────────────────────

class TestFiltrarTrades:
    def test_sin_filtros_devuelve_todos(self, db_with_data):
        r = filtrar_trades(db_path=db_with_data)
        assert len(r) == 5

    def test_estrategia_se_etiqueta(self, db_with_data):
        r = filtrar_trades(db_path=db_with_data)
        estr = {t["estrategia"] for t in r}
        assert estr == {"CONNORS"}

    def test_filtro_estrategia_connors(self, db_with_data):
        r = filtrar_trades(estrategia="CONNORS", db_path=db_with_data)
        assert len(r) == 5
        assert all(t["estrategia"] == "CONNORS" for t in r)

    def test_filtro_ticker_case_insensitive(self, db_with_data):
        r = filtrar_trades(ticker="aapl", db_path=db_with_data)
        assert len(r) == 2
        assert all(t["ticker"] == "AAPL" for t in r)

    def test_filtro_solo_ganadoras(self, db_with_data):
        r = filtrar_trades(solo_ganadoras=True, db_path=db_with_data)
        # ganadoras: AAPL +100, MSFT +100, NVDA +30 = 3
        assert len(r) == 3
        assert all(t["net_pnl"] > 0 for t in r)

    def test_filtro_solo_perdedoras(self, db_with_data):
        r = filtrar_trades(solo_ganadoras=False, db_path=db_with_data)
        # perdedoras: AAPL -100, NVDA -50 = 2
        assert len(r) == 2
        assert all(t["net_pnl"] <= 0 for t in r)

    def test_filtro_motivo(self, db_with_data):
        r = filtrar_trades(motivo="SL", db_path=db_with_data)
        # 2 cierres por SL: AAPL y NVDA
        assert len(r) == 2
        assert all(t["reason"] == "SL" for t in r)

    def test_filtro_fecha_desde(self, db_with_data):
        r = filtrar_trades(fecha_desde="2024-04-01", db_path=db_with_data)
        # sólo MSFT cierra después de 2024-04-01
        assert len(r) == 1
        assert r[0]["ticker"] == "MSFT"

    def test_filtro_fecha_hasta(self, db_with_data):
        r = filtrar_trades(fecha_hasta="2024-02-28", db_path=db_with_data)
        # cierran antes del 28-feb: AAPL (20/02) y NVDA (05/02)
        assert len(r) == 2

    def test_db_inexistente_devuelve_vacio(self, tmp_path):
        assert filtrar_trades(db_path=tmp_path / "no_existe.db") == []


# ── Tests de estadisticas_por_ticker ──────────────────────────────────────────

class TestEstadisticasPorTicker:
    def test_pnl_total_coherente_con_filtrar(self, db_with_data):
        # Suma de net_pnl en TODOS los trades = 80
        total = sum(
            t["net_pnl"] for t in filtrar_trades(db_path=db_with_data)
        )
        assert total == pytest.approx(80.0)

    def test_no_lanza_excepcion_con_datos(self, db_with_data):
        # La función imprime con Rich; sólo verificamos que no rompa.
        estadisticas_por_ticker(db_path=db_with_data)

    def test_no_lanza_excepcion_sin_datos(self, tmp_path):
        # BD sin tablas
        empty_db = tmp_path / "empty.db"
        sqlite3.connect(str(empty_db)).close()
        estadisticas_por_ticker(db_path=empty_db)


# ── Tests de exportar_filtrado ────────────────────────────────────────────────

class TestExportarFiltrado:
    def test_crea_csv_con_columnas_esperadas(self, db_with_data, tmp_path):
        path = tmp_path / "out.csv"
        result = exportar_filtrado(
            {"ticker": "AAPL"}, filename=path, db_path=db_with_data,
        )
        assert result == path
        assert path.exists()
        df = pd.read_csv(path)
        assert len(df) == 2
        for col in ("ticker", "entry_date", "exit_date",
                    "net_pnl", "return_pct", "reason", "estrategia"):
            assert col in df.columns

    def test_sin_resultados_devuelve_none(self, db_with_data):
        # ticker inexistente
        result = exportar_filtrado(
            {"ticker": "ZZZZ"}, db_path=db_with_data,
        )
        assert result is None

    def test_filtro_combinado(self, db_with_data, tmp_path):
        path = tmp_path / "out.csv"
        result = exportar_filtrado(
            {"ticker": "NVDA", "solo_ganadoras": True},
            filename=path, db_path=db_with_data,
        )
        assert result == path
        df = pd.read_csv(path)
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "NVDA"
        assert df.iloc[0]["net_pnl"] > 0


# ── Tests de las funciones de impresión (smoke) ───────────────────────────────

class TestSmokePrints:
    """Funciones que sólo imprimen no deben lanzar excepciones."""
    def test_ver_abiertas_unificadas_no_crashea(self, db_with_data, monkeypatch):
        # Evitamos llamar a yfinance: parchamos para que devuelva None y use entry_price
        import modules.operations_log as ol
        monkeypatch.setattr(ol, "_try_get_current_price", lambda t: None)
        ver_abiertas_unificadas(db_path=db_with_data)

    def test_ver_diario_cronologico_no_crashea(self, db_with_data):
        # 30 días por defecto; los trades sintéticos son de 2024-02-2024-05,
        # probablemente fuera del cutoff de "últimos 30 días desde hoy".
        # Pasamos un cutoff amplio para asegurar que entra al render.
        ver_diario_cronologico(dias=10_000, db_path=db_with_data)

    def test_ver_detalle_trade_existente(self, db_with_data):
        ver_detalle_trade(1, "CONNORS", db_path=db_with_data)

    def test_ver_detalle_trade_inexistente(self, db_with_data):
        ver_detalle_trade(9999, "CONNORS", db_path=db_with_data)

    def test_resumen_pnl_por_mes(self, db_with_data):
        resumen_pnl_por_mes(db_path=db_with_data)
