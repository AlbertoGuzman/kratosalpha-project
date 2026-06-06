# -*- coding: utf-8 -*-
"""
Tests de integración Alpaca ↔ ConnorsRSI.

Verifica que:
    · Con ALPACA_ENABLED=False (default), nada cambia en el flujo existente.
    · Con ALPACA_ENABLED=True, el broker mockeado recibe las llamadas
      apropiadas (submit_order al abrir, close_position al cerrar).
    · La columna alpaca_order_id existe en el schema (con migración).
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

import config


# ── TestSchema — modelo relacional connors_operaciones ───────────────────────

class TestSchemaOperaciones:
    """El esquema relacional crea connors_operaciones con tipado y FK."""

    def test_operaciones_tiene_columnas_clave(self, tmp_path, monkeypatch):
        import modules.connors_rsi as cr
        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()
        with sqlite3.connect(str(db)) as con:
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(connors_operaciones)"
            ).fetchall()}
        for c in ("id", "ticker", "estado", "alpaca_order_id",
                  "entry_price", "exit_price", "net_pnl", "reason"):
            assert c in cols, f"falta la columna {c}"

    def test_capital_tiene_fk_operacion(self, tmp_path, monkeypatch):
        """connors_capital referencia connors_operaciones vía operacion_id (FK)."""
        import modules.connors_rsi as cr
        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()
        with sqlite3.connect(str(db)) as con:
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(connors_capital)"
            ).fetchall()}
            fks = con.execute(
                "PRAGMA foreign_key_list(connors_capital)"
            ).fetchall()
        assert "operacion_id" in cols
        # Hay una FK declarada hacia connors_operaciones.
        assert any(row[2] == "connors_operaciones" for row in fks)

    def test_estado_check_constraint(self, tmp_path, monkeypatch):
        """El CHECK de `estado` rechaza valores fuera del dominio permitido."""
        import modules.connors_rsi as cr
        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()
        with sqlite3.connect(str(db)) as con:
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    "INSERT INTO connors_operaciones (ticker, estado, entry_date) "
                    "VALUES ('AAPL', 'inventado', '2026-06-05')"
                )


# ── TestIsAlpacaEnabledFlag ──────────────────────────────────────────────────

class TestAlpacaEnabledFlag:
    """Por defecto ALPACA_ENABLED debe ser False (modo seguro)."""

    def test_alpaca_enabled_es_bool(self):
        # No fijamos el valor concreto (el usuario puede activarlo);
        # sólo confirmamos que es un bool bien tipado.
        assert isinstance(config.ALPACA_ENABLED, bool)

    def test_alpaca_paper_default_es_true(self):
        # Por seguridad, paper trading por defecto.
        assert config.ALPACA_PAPER is True

    def test_url_paper_si_paper(self):
        # Con ALPACA_PAPER=True la URL apunta al endpoint paper.
        assert "paper" in config.ALPACA_BASE_URL


# ── TestConnorsConAlpacaDeshabilitado ────────────────────────────────────────

class TestConnorsConAlpacaDeshabilitado:
    """Con ALPACA_ENABLED=False, el flujo NO debe importar ni llamar a Alpaca."""

    def test_abrir_posicion_no_llama_alpaca(self, monkeypatch, tmp_path):
        import modules.connors_rsi as cr
        # ALPACA_ENABLED=False (default) → no hay rama Alpaca
        monkeypatch.setattr(config, "ALPACA_ENABLED", False)

        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()

        sig = {
            "ticker": "AAPL", "limit_price": 100.0,
            "crsi": 18.5, "rsi3": 20.0, "streak": -4,
        }
        cash_inicial = config.CONNORS_CAPITAL
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS

        # Mock get_broker para detectar si se llama
        from modules import alpaca_broker
        get_broker_spy = MagicMock(side_effect=AssertionError(
            "get_broker NO debe llamarse con ALPACA_ENABLED=False"
        ))
        monkeypatch.setattr(alpaca_broker, "get_broker", get_broker_spy)

        new_cash, ok = cr._abrir_posicion(sig, cash_inicial, slot)
        assert ok is True
        # El spy no debe haber sido llamado
        get_broker_spy.assert_not_called()


# ── TestConnorsConAlpacaHabilitado ───────────────────────────────────────────

class TestConnorsConAlpacaHabilitado:
    """Con ALPACA_ENABLED=True, las funciones llaman al broker (mockeado)."""

    @pytest.fixture
    def setup_alpaca(self, monkeypatch, tmp_path):
        import modules.connors_rsi as cr
        from modules import alpaca_broker as ab
        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()

        # Mock del broker
        mock_broker = MagicMock()
        mock_broker.submit_order.return_value = {
            "order_id": "alp-12345",
            "status":   "accepted",
            "filled_avg_price": 0.0,
        }
        mock_broker.close_position.return_value = {
            "order_id": "alp-close-1", "status": "accepted",
        }
        monkeypatch.setattr(ab, "get_broker", lambda: mock_broker)
        return cr, ab, mock_broker, db

    def test_apertura_llama_submit_order(self, setup_alpaca):
        cr, ab, mock_broker, db = setup_alpaca
        sig = {
            "ticker": "AAPL", "limit_price": 100.0,
            "crsi": 18.5, "rsi3": 20.0, "streak": -4,
        }
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        new_cash, ok = cr._abrir_posicion(sig, config.CONNORS_CAPITAL, slot)
        assert ok is True
        mock_broker.submit_order.assert_called_once()
        # Verifica argumentos clave de la orden
        call_kwargs = mock_broker.submit_order.call_args.kwargs
        assert call_kwargs["ticker"]      == "AAPL"
        assert call_kwargs["side"]        == "buy"
        assert call_kwargs["order_type"]  == "limit"
        assert call_kwargs["limit_price"] == 100.0

    def test_apertura_va_a_pending_no_portfolio(self, setup_alpaca):
        """Con ALPACA_ENABLED=True NO se inserta en portfolio aún — va a pending."""
        cr, ab, mock_broker, db = setup_alpaca
        sig = {
            "ticker": "MSFT", "limit_price": 200.0,
            "crsi": 15.0, "rsi3": 12.0, "streak": -5,
        }
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        cr._abrir_posicion(sig, config.CONNORS_CAPITAL, slot)
        with sqlite3.connect(str(db)) as con:
            in_portfolio = con.execute(
                "SELECT ticker FROM connors_operaciones "
                "WHERE ticker = 'MSFT' AND estado = 'abierta'"
            ).fetchone()
            in_pending = con.execute(
                "SELECT ticker, alpaca_order_id, limit_price "
                "FROM connors_operaciones "
                "WHERE ticker = 'MSFT' AND estado = 'pendiente'"
            ).fetchone()
        assert in_portfolio is None  # No abierta todavía
        assert in_pending is not None
        assert in_pending[1] == "alp-12345"
        assert in_pending[2] == 200.0

    def test_apertura_no_descuenta_cash_si_pending(self, setup_alpaca):
        """Cuando va a pending, el cash NO se descuenta hasta el fill."""
        cr, ab, mock_broker, db = setup_alpaca
        sig = {
            "ticker": "GOOGL", "limit_price": 150.0,
            "crsi": 12.0, "rsi3": 14.0, "streak": -3,
        }
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        cash_inicial = config.CONNORS_CAPITAL
        new_cash, ok = cr._abrir_posicion(sig, cash_inicial, slot)
        assert ok is True
        assert new_cash == cash_inicial  # cash intacto hasta el fill

    def test_apertura_falla_si_alpaca_rechaza(self, setup_alpaca):
        """Si Alpaca rechaza la orden, la apertura se cancela (no portfolio, no pending)."""
        cr, ab, mock_broker, db = setup_alpaca
        mock_broker.submit_order.side_effect = RuntimeError("Alpaca caído")
        sig = {
            "ticker": "NVDA", "limit_price": 500.0,
            "crsi": 10.0, "rsi3": 11.0, "streak": -4,
        }
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        new_cash, ok = cr._abrir_posicion(sig, config.CONNORS_CAPITAL, slot)
        assert ok is False
        with sqlite3.connect(str(db)) as con:
            in_portfolio = con.execute(
                "SELECT ticker FROM connors_operaciones "
                "WHERE ticker = 'NVDA' AND estado = 'abierta'"
            ).fetchone()
            in_pending = con.execute(
                "SELECT ticker FROM connors_operaciones "
                "WHERE ticker = 'NVDA' AND estado = 'pendiente'"
            ).fetchone()
        assert in_portfolio is None
        assert in_pending is None


# ── TestCheckPendingOrders ───────────────────────────────────────────────────

class TestCheckPendingOrders:
    """Reconciliación de operaciones 'pendiente' contra Alpaca."""

    @pytest.fixture
    def setup_pending(self, monkeypatch, tmp_path):
        import modules.connors_rsi as cr
        from modules import alpaca_broker as ab

        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        db = tmp_path / "test.db"
        monkeypatch.setattr(cr, "_DB_PATH", db)
        cr._init_db()

        # Insertamos una orden pending sintética
        cr._insert_pending_order(
            ticker="AAPL", alpaca_order_id="ord-AAPL", entry_price=100.0,
            slot_size=2321.67, crsi=15.0, rsi3=20.0, streak=-4,
        )
        mock_broker = MagicMock()
        monkeypatch.setattr(ab, "get_broker", lambda: mock_broker)
        return cr, ab, mock_broker, db

    def test_filled_pasa_a_portfolio(self, setup_pending):
        cr, ab, mock_broker, db = setup_pending
        mock_broker.get_order.return_value = {
            "order_id":         "ord-AAPL",
            "ticker":           "AAPL",
            "qty":              23.0,
            "filled_qty":       23.0,
            "status":           "filled",
            "filled_avg_price": 99.8,
            "side":             "buy",
        }
        stats = cr._check_pending_orders()
        assert stats["filled"] == 1
        with sqlite3.connect(str(db)) as con:
            in_portfolio = con.execute(
                "SELECT entry_price, shares FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='abierta'"
            ).fetchone()
            in_pending = con.execute(
                "SELECT 1 FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='pendiente'"
            ).fetchone()
        assert in_portfolio is not None
        assert in_portfolio[0] == pytest.approx(99.8)  # entry = filled_avg_price
        assert in_pending is None  # eliminada de pendientes

    def test_expired_elimina_pendiente(self, setup_pending):
        cr, ab, mock_broker, db = setup_pending
        mock_broker.get_order.return_value = {
            "order_id": "ord-AAPL", "ticker": "AAPL", "qty": 0.0,
            "filled_qty": 0.0, "status": "expired",
            "filled_avg_price": 0.0, "side": "buy",
        }
        stats = cr._check_pending_orders()
        assert stats["expired"] == 1
        with sqlite3.connect(str(db)) as con:
            in_portfolio = con.execute(
                "SELECT 1 FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='abierta'"
            ).fetchone()
            in_pending = con.execute(
                "SELECT 1 FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='pendiente'"
            ).fetchone()
        assert in_portfolio is None
        assert in_pending is None

    def test_canceled_elimina_pendiente(self, setup_pending):
        cr, ab, mock_broker, db = setup_pending
        mock_broker.get_order.return_value = {
            "order_id": "ord-AAPL", "ticker": "AAPL", "qty": 0.0,
            "filled_qty": 0.0, "status": "canceled",
            "filled_avg_price": 0.0, "side": "buy",
        }
        stats = cr._check_pending_orders()
        assert stats["canceled"] == 1
        with sqlite3.connect(str(db)) as con:
            in_pending = con.execute(
                "SELECT 1 FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='pendiente'"
            ).fetchone()
        assert in_pending is None

    def test_new_sigue_pendiente(self, setup_pending):
        cr, ab, mock_broker, db = setup_pending
        mock_broker.get_order.return_value = {
            "order_id": "ord-AAPL", "ticker": "AAPL", "qty": 23.0,
            "filled_qty": 0.0, "status": "new",
            "filled_avg_price": 0.0, "side": "buy",
        }
        stats = cr._check_pending_orders()
        assert stats["pending"] == 1
        with sqlite3.connect(str(db)) as con:
            in_pending = con.execute(
                "SELECT 1 FROM connors_operaciones "
                "WHERE ticker='AAPL' AND estado='pendiente'"
            ).fetchone()
        assert in_pending is not None  # sigue pendiente

    def test_alpaca_desactivado_no_chequea(self, monkeypatch, tmp_path):
        """Con ALPACA_ENABLED=False, _check_pending_orders es no-op."""
        import modules.connors_rsi as cr
        monkeypatch.setattr(config, "ALPACA_ENABLED", False)
        monkeypatch.setattr(cr, "_DB_PATH", tmp_path / "x.db")
        cr._init_db()
        stats = cr._check_pending_orders()
        assert stats == {"filled": 0, "expired": 0, "canceled": 0, "pending": 0}

    def test_pending_cuenta_como_slot_ocupado(self, setup_pending):
        """`_count_pending_orders` cuenta para slots disponibles."""
        cr, ab, mock_broker, db = setup_pending
        n = cr._count_pending_orders()
        assert n == 1
        # Añade otra pending
        cr._insert_pending_order(
            "MSFT", "ord-MSFT", 200.0, 2321.67, 12.0, 14.0, -3,
        )
        assert cr._count_pending_orders() == 2


# ── TestCapitalSummaryConAlpaca ──────────────────────────────────────────────

class TestCapitalSummaryConAlpaca:
    """get_capital_summary_* añade campos `capital_alpaca` cuando está activo."""

    def test_connors_summary_sin_alpaca(self, db_connors_vacia, monkeypatch):
        import modules.connors_rsi as cr
        monkeypatch.setattr(config, "ALPACA_ENABLED", False)
        s = cr.get_capital_summary_connors()
        assert "capital_alpaca" not in s
        assert "capital_actual" in s

    def test_connors_summary_con_alpaca(self, db_connors_vacia, monkeypatch):
        import modules.connors_rsi as cr
        from modules import alpaca_broker as ab

        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        mock_broker = MagicMock()
        mock_broker.get_account.return_value = {
            "equity": 7823.45, "cash": 6000.0,
            "buying_power": 12000.0, "portfolio_value": 7823.45,
        }
        monkeypatch.setattr(ab, "get_broker", lambda: mock_broker)
        s = cr.get_capital_summary_connors()
        assert "capital_alpaca" in s
        assert s["capital_alpaca"] == pytest.approx(7823.45)
