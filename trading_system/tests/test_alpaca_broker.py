# -*- coding: utf-8 -*-
"""
Tests de modules/alpaca_broker.py — con la SDK de Alpaca completamente
mockeada (no se hace ninguna llamada de red).
"""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakeAccount:
    """Simula `alpaca.trading.models.TradeAccount`."""
    def __init__(self, equity=10000.0, cash=5000.0, buying_power=20000.0,
                 portfolio_value=10000.0):
        self.equity          = str(equity)
        self.cash            = str(cash)
        self.buying_power    = str(buying_power)
        self.portfolio_value = str(portfolio_value)


class _FakePosition:
    def __init__(self, symbol, qty=10, avg_entry_price=100.0, current_price=110.0,
                 unrealized_pl=100.0, unrealized_plpc=0.10):
        self.symbol            = symbol
        self.qty               = str(qty)
        self.avg_entry_price   = str(avg_entry_price)
        self.current_price     = str(current_price)
        self.unrealized_pl     = str(unrealized_pl)
        self.unrealized_plpc   = str(unrealized_plpc)


class _FakePortfolioHistory:
    """Simula `alpaca.trading.models.PortfolioHistory`."""
    def __init__(self, equity=None, timestamp=None):
        # capital_inicial = equity[0] = 9800; equity_actual = equity[-1] = 10000
        self.equity    = equity    if equity    is not None else [9800.0, 9900.0, 10000.0]
        self.timestamp = timestamp if timestamp is not None else [1, 2, 3]


class _FakeOrder:
    def __init__(self, order_id="abc123", status="accepted",
                 filled_avg_price=0.0):
        self.id               = order_id
        self.status           = type("S", (), {"value": status})()
        self.filled_avg_price = str(filled_avg_price) if filled_avg_price else None
        self.symbol           = "AAPL"
        self.qty              = "10"
        self.side             = type("S", (), {"value": "buy"})()
        self.order_type       = type("S", (), {"value": "market"})()
        self.created_at       = None

    def __str__(self):
        return f"FakeOrder({self.id})"


@pytest.fixture
def fake_trading_client():
    """Construye un mock que se comporta como `alpaca.trading.client.TradingClient`."""
    client = MagicMock()
    client.get_account.return_value = _FakeAccount()
    client.get_all_positions.return_value = [_FakePosition("AAPL")]
    client.get_orders.return_value = [_FakeOrder()]
    order = _FakeOrder(order_id="order-001", status="accepted")
    client.submit_order.return_value  = order
    client.close_position.return_value = _FakeOrder(order_id="close-001")
    client.get_clock.return_value = MagicMock(is_open=True)
    client.get_portfolio_history.return_value = _FakePortfolioHistory()
    return client


@pytest.fixture
def fake_data_client():
    client = MagicMock()
    # get_stock_latest_trade devuelve dict {ticker: TradeObj}
    trade = MagicMock()
    trade.price = 123.45
    client.get_stock_latest_trade.return_value = {"AAPL": trade}
    return client


@pytest.fixture
def alpaca_with_keys(monkeypatch):
    """Activa claves Alpaca para que AlpacaBroker pueda instanciarse."""
    monkeypatch.setattr(config, "ALPACA_API_KEY",    "test_key_id")
    monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "test_secret_key")
    monkeypatch.setattr(config, "ALPACA_PAPER",      True)
    # Reset del singleton entre tests
    from modules.alpaca_broker import reset_broker
    reset_broker()


@pytest.fixture
def broker(alpaca_with_keys, fake_trading_client, fake_data_client):
    """Devuelve un AlpacaBroker con clients mockeados."""
    with patch("alpaca.trading.client.TradingClient",
               return_value=fake_trading_client), \
         patch("alpaca.data.historical.StockHistoricalDataClient",
               return_value=fake_data_client):
        from modules.alpaca_broker import AlpacaBroker
        b = AlpacaBroker()
    return b


# ── TestAlpacaBrokerInit ─────────────────────────────────────────────────────

class TestAlpacaBrokerInit:
    def test_falla_sin_keys(self, monkeypatch):
        from modules.alpaca_broker import AlpacaBroker, AlpacaNotConfigured
        monkeypatch.setattr(config, "ALPACA_API_KEY", "")
        monkeypatch.setattr(config, "ALPACA_SECRET_KEY", "")
        with pytest.raises(AlpacaNotConfigured):
            AlpacaBroker()

    def test_se_instancia_con_keys(self, broker):
        # Si la fixture lo creó sin lanzar, la inicialización funcionó.
        assert broker.trading_client is not None
        assert broker.data_client is not None

    def test_is_alpaca_enabled_lee_config(self, monkeypatch):
        from modules.alpaca_broker import is_alpaca_enabled
        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        assert is_alpaca_enabled() is True
        monkeypatch.setattr(config, "ALPACA_ENABLED", False)
        assert is_alpaca_enabled() is False


# ── TestAlpacaAccount ────────────────────────────────────────────────────────

class TestAlpacaAccount:
    def test_get_account_devuelve_dict(self, broker):
        acc = broker.get_account()
        assert isinstance(acc, dict)
        for key in ("equity", "cash", "buying_power", "portfolio_value"):
            assert key in acc
            assert isinstance(acc[key], float)

    def test_get_account_valores(self, broker):
        acc = broker.get_account()
        assert acc["equity"] == 10000.0
        assert acc["cash"]   == 5000.0


# ── TestPortfolioHistory ─────────────────────────────────────────────────────

class TestPortfolioHistory:
    def test_get_portfolio_history_dict(self, broker):
        h = broker.get_portfolio_history()
        assert h["capital_inicial"] == 9800.0
        assert h["equity_actual"]   == 10000.0
        assert h["equity"][0]       == 9800.0
        assert len(h["equity"])     == 3

    def test_get_portfolio_history_cachea(self, broker):
        """La 2ª llamada usa la caché y no vuelve a consultar a Alpaca."""
        broker.get_portfolio_history()
        broker.get_portfolio_history()
        assert broker.trading_client.get_portfolio_history.call_count == 1

    def test_get_pnl_real_calcula(self, broker, monkeypatch):
        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        from modules.alpaca_broker import get_pnl_real
        pnl = get_pnl_real(broker)
        assert pnl is not None
        assert pnl["capital_inicial"] == 9800.0
        assert pnl["equity_actual"]   == 10000.0          # de get_account()
        assert pnl["pnl_usd"]         == pytest.approx(200.0)
        assert pnl["pnl_pct"]         == pytest.approx(200.0 / 9800.0 * 100)

    def test_get_pnl_real_desactivado_none(self, broker, monkeypatch):
        """En dev / Alpaca off → None sin consultar el endpoint."""
        monkeypatch.setattr(config, "ALPACA_ENABLED", False)
        from modules.alpaca_broker import get_pnl_real
        assert get_pnl_real(broker) is None
        broker.trading_client.get_portfolio_history.assert_not_called()

    def test_get_pnl_real_endpoint_falla_none(self, broker, monkeypatch):
        """Si el endpoint lanza, devuelve None sin propagar la excepción."""
        monkeypatch.setattr(config, "ALPACA_ENABLED", True)
        broker._portfolio_history_cache = None
        broker.trading_client.get_portfolio_history.side_effect = RuntimeError("api down")
        from modules.alpaca_broker import get_pnl_real
        assert get_pnl_real(broker) is None


# ── TestAlpacaPositions ──────────────────────────────────────────────────────

class TestAlpacaPositions:
    def test_get_positions_devuelve_lista(self, broker):
        positions = broker.get_positions()
        assert isinstance(positions, list)
        assert len(positions) == 1
        p = positions[0]
        for key in ("ticker", "qty", "avg_entry_price", "current_price",
                    "unrealized_pl", "unrealized_plpc"):
            assert key in p

    def test_get_positions_tipos(self, broker):
        p = broker.get_positions()[0]
        assert p["ticker"] == "AAPL"
        assert p["qty"]    == 10.0
        assert p["avg_entry_price"] == 100.0


# ── TestAlpacaOrders ─────────────────────────────────────────────────────────

class TestAlpacaOrders:
    def test_submit_order_market(self, broker):
        resp = broker.submit_order(
            ticker="AAPL", qty=5, side="buy", order_type="market",
        )
        assert "order_id" in resp
        assert "status"   in resp

    def test_submit_order_limit_requiere_precio(self, broker):
        from modules.alpaca_broker import AlpacaError
        with pytest.raises(AlpacaError):
            broker.submit_order(
                ticker="AAPL", qty=5, side="buy", order_type="limit",
            )

    def test_submit_order_limit_con_precio(self, broker):
        resp = broker.submit_order(
            ticker="AAPL", qty=5, side="buy", order_type="limit",
            limit_price=100.0,
        )
        assert "order_id" in resp

    def test_cancel_order_exito(self, broker):
        ok = broker.cancel_order("order-001")
        assert ok is True

    def test_cancel_order_fallo(self, broker):
        broker.trading_client.cancel_order_by_id.side_effect = RuntimeError("nope")
        assert broker.cancel_order("order-002") is False

    def test_close_position(self, broker):
        resp = broker.close_position("AAPL")
        assert "order_id" in resp


# ── TestAlpacaDataMarket ─────────────────────────────────────────────────────

class TestAlpacaDataMarket:
    def test_get_latest_price(self, broker):
        price = broker.get_latest_price("AAPL")
        assert price == pytest.approx(123.45)

    def test_is_market_open(self, broker):
        assert broker.is_market_open() is True


# ── TestSyncAlpacaVsSqlite ───────────────────────────────────────────────────

class TestSyncAlpacaVsSqlite:
    @pytest.fixture
    def db_con_posiciones(self, tmp_path):
        """BD temporal con posiciones en SQLite (AAPL, MSFT)."""
        db = tmp_path / "sync.db"
        with sqlite3.connect(str(db)) as con:
            con.executescript("""
                CREATE TABLE connors_operaciones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL, estado TEXT NOT NULL,
                    entry_date TEXT, entry_price REAL, limit_price REAL,
                    shares REAL, valor_compra REAL, stop_loss_price REAL
                );
            """)
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                " valor_compra, stop_loss_price) "
                "VALUES ('AAPL','abierta','2024-01-01',100.0,99.0,10.0,1000.0,95.0)",
            )
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                " valor_compra, stop_loss_price) "
                "VALUES ('MSFT','abierta','2024-01-01',200.0,199.0,5.0,1000.0,190.0)",
            )
        return db

    def test_sync_sin_diferencias(self, db_con_posiciones):
        """Alpaca y SQLite tienen las mismas posiciones."""
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = [
            {"ticker": "AAPL", "qty": 10.0, "avg_entry_price": 100.0,
             "current_price": 105.0, "unrealized_pl": 50.0,
             "unrealized_plpc": 0.05},
            {"ticker": "MSFT", "qty": 5.0, "avg_entry_price": 200.0,
             "current_price": 210.0, "unrealized_pl": 50.0,
             "unrealized_plpc": 0.05},
        ]
        mock_broker.get_orders.return_value = []
        result = sync_alpaca_vs_sqlite(db_path=db_con_posiciones, broker=mock_broker)
        assert result["only_sqlite"] == set()
        assert result["only_alpaca"] == set()

    def test_sync_solo_en_sqlite(self, db_con_posiciones):
        """AAPL en SQLite pero no en Alpaca."""
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = [
            {"ticker": "MSFT", "qty": 5.0, "avg_entry_price": 200.0,
             "current_price": 210.0, "unrealized_pl": 50.0,
             "unrealized_plpc": 0.05},
        ]
        mock_broker.get_orders.return_value = []
        result = sync_alpaca_vs_sqlite(db_path=db_con_posiciones, broker=mock_broker)
        assert "AAPL" in result["only_sqlite"]
        assert result["only_alpaca"] == set()

    def test_sync_solo_en_alpaca(self, db_con_posiciones):
        """NVDA en Alpaca pero no en SQLite."""
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = [
            {"ticker": "AAPL", "qty": 10.0, "avg_entry_price": 100.0,
             "current_price": 105.0, "unrealized_pl": 50.0,
             "unrealized_plpc": 0.05},
            {"ticker": "MSFT", "qty": 5.0, "avg_entry_price": 200.0,
             "current_price": 210.0, "unrealized_pl": 50.0,
             "unrealized_plpc": 0.05},
            {"ticker": "NVDA", "qty": 2.0, "avg_entry_price": 500.0,
             "current_price": 520.0, "unrealized_pl": 40.0,
             "unrealized_plpc": 0.04},
        ]
        mock_broker.get_orders.return_value = []
        result = sync_alpaca_vs_sqlite(db_path=db_con_posiciones, broker=mock_broker)
        assert "NVDA" in result["only_alpaca"]
        assert result["only_sqlite"] == set()

    def test_sync_db_inexistente_devuelve_vacios(self, tmp_path):
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []
        mock_broker.get_orders.return_value = []
        result = sync_alpaca_vs_sqlite(
            db_path=tmp_path / "no_existe.db", broker=mock_broker,
        )
        assert result["only_sqlite"] == set()
        assert result["only_alpaca"] == set()

    def test_sync_orden_pendiente_no_es_huerfana(self, db_con_posiciones):
        """
        Regresión: si Alpaca tiene una orden BUY abierta (accepted/new) para
        un ticker que está en SQLite pero aún no ejecutada, NO debe marcarse
        como huérfana en `only_sqlite`. Esto evitaba el falso positivo cuando
        el mercado estaba cerrado y las órdenes quedaban en `accepted`.
        """
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []  # aún sin filled
        mock_broker.get_orders.return_value = [
            {"id": "ord-1", "ticker": "AAPL", "qty": 10.0,
             "side": "buy", "type": "limit", "status": "accepted",
             "filled_avg_price": 0.0, "created_at": ""},
            {"id": "ord-2", "ticker": "MSFT", "qty": 5.0,
             "side": "buy", "type": "market", "status": "accepted",
             "filled_avg_price": 0.0, "created_at": ""},
        ]
        result = sync_alpaca_vs_sqlite(db_path=db_con_posiciones, broker=mock_broker)
        # AAPL y MSFT están en SQLite y tienen orden BUY abierta → en sync
        assert result["only_sqlite"] == set()
        assert result["only_alpaca"] == set()

    def test_sync_orden_sell_no_cuenta_como_alpaca(self, db_con_posiciones):
        """
        Una orden SELL pendiente no implica que la posición exista en Alpaca
        (es justo lo contrario: vamos a cerrarla). Sólo las órdenes BUY
        pendientes cuentan como "está en Alpaca".
        """
        from modules.alpaca_broker import sync_alpaca_vs_sqlite
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = []
        mock_broker.get_orders.return_value = [
            {"id": "ord-1", "ticker": "AAPL", "qty": 10.0,
             "side": "sell", "type": "market", "status": "accepted",
             "filled_avg_price": 0.0, "created_at": ""},
        ]
        result = sync_alpaca_vs_sqlite(db_path=db_con_posiciones, broker=mock_broker)
        # AAPL en SQLite, sólo orden SELL en Alpaca → sigue siendo huérfana
        assert "AAPL" in result["only_sqlite"]
