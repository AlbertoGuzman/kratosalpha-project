# -*- coding: utf-8 -*-
"""
Fixtures y configuración compartida para toda la suite de tests.

Convenciones:
    · Tests rápidos por defecto.
    · Los tests de benchmark de backtest están marcados con `@pytest.mark.slow`
      y se SALTAN por defecto. Para ejecutarlos: `pytest --run-slow`.

Fixtures disponibles:
    · ohlcv_df                                            (OHLCV sintético)
    · serie_alcista_10dias / serie_bajista_10dias         (OHLCV sintéticos)
    · db_connors_vacia                                    (BD limpia 10.000€)
    · connors_trade_ganador / _perdedor / _tsto           (trades sintéticos)
    · connors_backtest_2019_hoy (session, slow)
"""

import os
import sys
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# `config` exige TRADING_ENV en os.environ. Para la suite de tests usamos
# siempre `dev` (los tests crean BDs tmp con monkeypatch.setattr sobre _DB_PATH,
# así que esta variable solo sirve para que `import config` no aborte).
# `setdefault` respeta valores existentes por si alguien lanza tests con
# `TRADING_ENV=pre pytest` (caso poco habitual).
os.environ.setdefault("TRADING_ENV", "dev")

# Asegura que trading_system/ esté en el path para que los módulos puedan importar config
sys.path.insert(0, str(Path(__file__).parent.parent))

import config


# ── Marker `slow` y opción --run-slow ────────────────────────────────────────

def pytest_addoption(parser):
    """Añade el flag --run-slow para ejecutar tests marcados como lentos."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="ejecuta tests marcados con @pytest.mark.slow",
    )


def pytest_configure(config):  # noqa: A002  (shadowing intencional, hookspec)
    """Registra el marker `slow` para que pytest no avise."""
    config.addinivalue_line(
        "markers",
        "slow: tests lentos (e.g. backtests con yfinance). Skip por defecto.",
    )


def pytest_collection_modifyitems(config, items):  # noqa: A002
    """Salta tests `@pytest.mark.slow` salvo que se pase --run-slow."""
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="usa --run-slow para ejecutarlo")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


# ── OHLCV sintéticos (originales + nuevos) ───────────────────────────────────

@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    """
    DataFrame OHLCV sintético con 150 días hábiles.
    Suficiente para calcular EMA_21, RSI_14, ADX_14 y VOLUME_MA20 sin NaN en la zona final.
    """
    np.random.seed(42)
    n = 150
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    close = 100.0 * np.cumprod(1 + np.random.normal(0.0005, 0.015, n))
    high = close * (1 + np.abs(np.random.normal(0, 0.005, n)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, n)))
    open_ = close * (1 + np.random.normal(0, 0.003, n))
    volume = np.random.randint(1_000_000, 10_000_000, n).astype(float)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume},
        index=dates,
    )


def _build_serie(daily_return: float, n: int = 10) -> pd.DataFrame:
    """Helper para crear serie OHLCV con retorno diario constante."""
    dates = pd.date_range("2024-01-02", periods=n, freq="B")
    close = 100.0 * np.cumprod(np.full(n, 1.0 + daily_return))
    open_ = close * (1 + np.random.normal(0, 0.001, n))
    high  = close * (1 + 0.003)
    low   = close * (1 - 0.003)
    vol   = np.full(n, 5_000_000.0)
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=dates,
    )


@pytest.fixture
def serie_alcista_10dias() -> pd.DataFrame:
    """OHLCV sintético — 10 días subiendo 1 % cada uno."""
    np.random.seed(7)
    return _build_serie(daily_return=+0.01, n=10)


@pytest.fixture
def serie_bajista_10dias() -> pd.DataFrame:
    """OHLCV sintético — 10 días bajando 1 % cada uno."""
    np.random.seed(7)
    return _build_serie(daily_return=-0.01, n=10)


# ── BD SQLite limpia (Connors) ────────────────────────────────────────────────

def _create_connors_schema(con: sqlite3.Connection) -> None:
    """Crea el esquema relacional real (connors_operaciones + connors_capital).

    Usa el DDL de producción (`connors_rsi._DDL`) como única fuente de verdad,
    para que los tests no diverjan del esquema real."""
    import modules.connors_rsi as cr
    for stmt in cr._DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)


CAPITAL_CONNORS_BASE = config.CONNORS_CAPITAL  # 10.000€


@pytest.fixture
def db_connors_vacia(tmp_path, monkeypatch) -> Path:
    """
    BD temporal inicializada con el esquema real y `connors_capital` = 10.000€.
    Apunta `modules.connors_rsi._DB_PATH` al tmp y delega en `cr._init_db()`
    (crea connors_operaciones + connors_capital + registro de capital inicial).
    """
    import modules.connors_rsi as cr
    db_path = tmp_path / "test_connors.db"
    monkeypatch.setattr(cr, "_DB_PATH", db_path)
    cr._init_db()
    return db_path


# ── Trades sintéticos de ConnorsRSI ──────────────────────────────────────────

def _slot_connors() -> float:
    """Slot estándar Connors con reserva 0.5 % para comisiones."""
    return (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS


def _insert_connors_trade(
    db_path: Path,
    ticker: str,
    entry_price: float,
    exit_price: float,
    dias: int,
    reason: str,
    entry_date: str = "2024-01-15",
    exit_date: str  = "2024-01-18",
) -> dict:
    """
    Inserta una operación CERRADA (estado='cerrada') en connors_operaciones con
    campos calculados (gross/net/return). Devuelve un dict con los valores para
    que el test pueda compararlos.
    """
    slot   = _slot_connors()
    shares = slot / entry_price
    valor_compra      = shares * entry_price
    valor_salida      = shares * exit_price
    commission_entry  = valor_compra * config.COMMISSION
    commission_exit   = valor_salida * config.COMMISSION
    gross_pnl         = (exit_price - entry_price) * shares
    net_pnl           = gross_pnl - commission_entry - commission_exit
    return_pct        = (exit_price - entry_price) / entry_price

    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO connors_operaciones "
            "(ticker, estado, entry_date, exit_date, entry_price, limit_price, "
            " exit_price, shares, valor_compra, slot_size, stop_loss_price, "
            " commission_entry, commission_exit, gross_pnl, net_pnl, return_pct, "
            " dias, reason, connors_rsi_entrada, rsi3_entrada, streak_entrada) "
            "VALUES (?,'cerrada',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticker, entry_date, exit_date,
                entry_price, entry_price * (1 - config.CONNORS_ENTRY_LIMIT),
                exit_price, shares, valor_compra, slot,
                entry_price * (1 - config.CONNORS_STOP_LOSS),
                commission_entry, commission_exit, gross_pnl, net_pnl, return_pct,
                dias, reason, 18.5, 20.0, -4,
            ),
        )
    return {
        "ticker": ticker, "entry_price": entry_price, "exit_price": exit_price,
        "shares": shares, "valor_compra": valor_compra,
        "commission_entry": commission_entry, "commission_exit": commission_exit,
        "gross_pnl": gross_pnl, "net_pnl": net_pnl, "return_pct": return_pct,
        "dias": dias, "reason": reason,
    }


@pytest.fixture
def connors_trade_ganador(db_connors_vacia) -> dict:
    """
    Trade sintético ganador: entry=100, exit=107, días=3, reason=CRSI_EXIT.
    PnL esperado: +7 % × slot - comisiones.
    """
    return _insert_connors_trade(
        db_connors_vacia, "AAPL", 100.0, 107.0, 3, "CRSI_EXIT",
        entry_date="2024-02-01", exit_date="2024-02-06",
    )


@pytest.fixture
def connors_trade_perdedor(db_connors_vacia) -> dict:
    """
    Trade sintético perdedor: entry=100, exit=95, días=2, reason=SL.
    PnL esperado: -5 % × slot - comisiones.
    """
    return _insert_connors_trade(
        db_connors_vacia, "MSFT", 100.0, 95.0, 2, "SL",
        entry_date="2024-03-01", exit_date="2024-03-04",
    )


@pytest.fixture
def connors_trade_tsto(db_connors_vacia) -> dict:
    """
    Trade sintético salida por time stop: entry=100, exit=101, días=5, reason=TSTO.
    PnL esperado: +1 % × slot - comisiones.
    """
    return _insert_connors_trade(
        db_connors_vacia, "NVDA", 100.0, 101.0, 5, "TSTO",
        entry_date="2024-04-01", exit_date="2024-04-08",
    )


# ── Backtests reales (session-scope, marcados slow indirectamente) ───────────

# El rango 2019-01-01 → hoy cubre los 7 años de los benchmarks indicados.
_BENCH_START = "2019-01-01"
_BENCH_END   = date.today().strftime("%Y-%m-%d")


@pytest.fixture(scope="session")
def connors_backtest_2019_hoy(request):
    """
    Ejecuta `run_connors_backtest` una vez por sesión sobre el universo actual
    y el rango 2019-01-01 → hoy. Devuelve None si no se pasa `--run-slow`
    (los tests que dependen del fixture se skipean automáticamente).
    """
    if not request.config.getoption("--run-slow"):
        return None
    try:
        from modules.connors_rsi import run_connors_backtest
        return run_connors_backtest(
            start_date=_BENCH_START, end_date=_BENCH_END, export=False,
        )
    except Exception:
        return None


