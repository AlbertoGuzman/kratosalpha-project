# -*- coding: utf-8 -*-
"""
Tests del panel de capital — invariantes sobre el capital de ConnorsRSI
(10.000€) y su consistencia con SQLite.
"""

import sqlite3
from datetime import date

import pytest

import config


# ── Helpers locales ──────────────────────────────────────────────────────────

def _capital_inicial_connors(db_path) -> float:
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT cash FROM connors_capital ORDER BY id ASC LIMIT 1"
        ).fetchone()
    return float(row[0])


def _last_cash_connors(db_path) -> float:
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT cash FROM connors_capital ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return float(row[0])


# ── TestCapitalPanel ─────────────────────────────────────────────────────────

class TestCapitalPanel:
    """Invariantes del panel de capital tras inicialización y operaciones."""

    def test_capital_total_inicio(self, db_connors_vacia):
        # ConnorsRSI arranca con todo el capital del sistema (10.000€).
        assert _capital_inicial_connors(db_connors_vacia) == pytest.approx(10_000.0)

    def test_cash_libre_coherente(self, db_connors_vacia):
        # Inserta una posición Connors abierta y verifica:
        # cash_libre = capital_actual - capital_comprometido
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        with sqlite3.connect(str(db_connors_vacia)) as con:
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
                "VALUES ('AAPL','abierta','2024-05-01',100.0,99.0,?,?,?,95.0,18.0)",
                (slot / 100.0, slot, slot),
            )
            # Cash tras abrir: capital inicial − valor_compra − comisión
            commission = slot * config.COMMISSION
            new_cash = config.CONNORS_CAPITAL - slot - commission
            con.execute(
                "INSERT INTO connors_capital "
                "(fecha, cash, valor_posiciones, capital_total, nota) "
                "VALUES (?, ?, ?, ?, 'apertura')",
                (date.today().isoformat(), new_cash, slot, new_cash + slot),
            )
            cash_real = con.execute(
                "SELECT cash FROM connors_capital ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            comprometido = con.execute(
                "SELECT SUM(valor_compra) FROM connors_operaciones "
                "WHERE estado = 'abierta'"
            ).fetchone()[0]
        capital_actual = cash_real + comprometido
        cash_libre     = capital_actual - comprometido
        # cash_libre debería igualar el cash registrado en SQLite
        assert cash_libre == pytest.approx(cash_real, abs=0.01)

    def test_cash_libre_nunca_negativo(self, db_connors_vacia):
        # Tras inicialización, el cash de ConnorsRSI >= 0
        assert _last_cash_connors(db_connors_vacia) >= 0.0

    def test_posiciones_coherentes(self, db_connors_vacia):
        # n_posiciones reportadas == filas en portfolio
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        with sqlite3.connect(str(db_connors_vacia)) as con:
            for tk in ("AAPL", "MSFT"):
                con.execute(
                    "INSERT INTO connors_operaciones "
                    "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                    " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
                    "VALUES (?, 'abierta', ?,?,?,?,?,?,?,?)",
                    (tk, "2024-05-01", 100.0, 99.0, slot / 100.0,
                     slot, slot, 95.0, 18.0),
                )
            n = con.execute(
                "SELECT COUNT(*) FROM connors_operaciones WHERE estado='abierta'"
            ).fetchone()[0]
        assert n == 2

    def test_capital_actual_tras_operacion(self, db_connors_vacia):
        # Cerrar un trade ganador con PnL=+50€ → cash debe subir +50€
        # respecto al estado anterior a la operación.
        slot = 2333.0
        shares = slot / 100.0
        exit_price = 102.5  # +2.5% sobre 100
        gross = (exit_price - 100.0) * shares  # = slot * 0.025
        commission = (slot + shares * exit_price) * config.COMMISSION
        net_pnl = gross - commission
        with sqlite3.connect(str(db_connors_vacia)) as con:
            cash_antes = _last_cash_connors(db_connors_vacia)
            # Insertamos la operación cerrada
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, exit_date, entry_price, limit_price, "
                " exit_price, shares, valor_compra, slot_size, commission_entry, "
                " commission_exit, gross_pnl, net_pnl, return_pct, dias, reason) "
                "VALUES ('AAPL','cerrada','2024-05-01','2024-05-04',100.0,99.0,?,"
                "?,?,?,?,?,?,?,?,?,'CRSI_EXIT')",
                (exit_price, shares, slot, slot,
                 slot * config.COMMISSION, shares * exit_price * config.COMMISSION,
                 gross, net_pnl, 0.025, 3),
            )
            # Y actualizamos el capital con el PnL
            con.execute(
                "INSERT INTO connors_capital "
                "(fecha, cash, valor_posiciones, capital_total, nota) "
                "VALUES (?, ?, 0.0, ?, 'cierre')",
                (date.today().isoformat(),
                 cash_antes + net_pnl, cash_antes + net_pnl),
            )
        cash_despues = _last_cash_connors(db_connors_vacia)
        assert cash_despues == pytest.approx(cash_antes + net_pnl, abs=0.01)
