# -*- coding: utf-8 -*-
"""
Módulo 11 — Estrategia ConnorsRSI a corto plazo.

Universo: load_universe() (CSV externo). Estrategia única del sistema, con capital
y tablas SQLite propias. Datos descargados con `yf.download` directo (sin pasar por
modules/data.py).
"""

import sys
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta
from itertools import product

import numpy as np
import pandas as pd
import pandas_ta_classic as ta
import yfinance as yf
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich import box

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from modules import yf_cache, strategy_db
from modules.logger import get_system_logger, get_trading_logger
from modules.data import es_dia_habil_nyse

console = Console()
_slog = get_system_logger()
_tlog = get_trading_logger()

_DB_PATH     = config.DB_PATH
_EXPORTS_DIR = Path(__file__).parent.parent / "exports"

# ── SQLite ────────────────────────────────────────────────────────────────────

# Modelo relacional de estado único: cada operación es UNA fila en
# `connors_operaciones` que transiciona por UPDATE entre estados
# (pendiente → abierta → cerrada). Las filas NUNCA se borran, así que el
# historial completo es trazable y `connors_capital` puede relacionarse con
# la operación que originó cada movimiento vía FK `operacion_id`.
#
# Vistas lógicas (lo que antes eran tablas separadas):
#   · pendientes (orden enviada a Alpaca sin ejecutar) → estado = 'pendiente'
#   · cartera (posición abierta)                       → estado = 'abierta'
#   · trades cerrados                                  → estado = 'cerrada'
_DDL = """
CREATE TABLE IF NOT EXISTS connors_operaciones (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    estado              TEXT    NOT NULL DEFAULT 'pendiente'
                                CHECK (estado IN ('pendiente','abierta','cerrada')),
    alpaca_order_id     TEXT,
    -- entrada
    entry_date          TEXT    NOT NULL,
    entry_price         REAL    CHECK (entry_price IS NULL OR entry_price > 0),
    limit_price         REAL    CHECK (limit_price IS NULL OR limit_price > 0),
    shares              REAL    CHECK (shares IS NULL OR shares > 0),
    valor_compra        REAL    CHECK (valor_compra IS NULL OR valor_compra >= 0),
    slot_size           REAL    CHECK (slot_size IS NULL OR slot_size >= 0),
    stop_loss_price     REAL    CHECK (stop_loss_price IS NULL OR stop_loss_price > 0),
    commission_entry    REAL    DEFAULT 0.0,
    connors_rsi_entrada REAL,
    rsi3_entrada        REAL,
    streak_entrada      INTEGER,
    -- salida (NULL hasta que la operación se cierra)
    exit_date           TEXT,
    exit_price          REAL    CHECK (exit_price IS NULL OR exit_price > 0),
    commission_exit     REAL,
    gross_pnl           REAL,
    net_pnl             REAL,
    return_pct          REAL,
    dias                INTEGER,
    reason              TEXT    CHECK (reason IS NULL OR
                                reason IN ('CRSI_EXIT','SL','TSTO','MANUAL')),
    -- auditoría
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_conn_op_ticker_activa
    ON connors_operaciones(ticker) WHERE estado <> 'cerrada';
CREATE INDEX IF NOT EXISTS idx_conn_op_estado
    ON connors_operaciones(estado);
CREATE INDEX IF NOT EXISTS idx_conn_op_exit_date
    ON connors_operaciones(exit_date);
CREATE INDEX IF NOT EXISTS idx_conn_op_entry_date
    ON connors_operaciones(entry_date);

CREATE TABLE IF NOT EXISTS connors_capital (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha            TEXT    NOT NULL,
    cash             REAL    NOT NULL,
    valor_posiciones REAL    DEFAULT 0.0,
    capital_total    REAL,
    nota             TEXT,
    operacion_id     INTEGER REFERENCES connors_operaciones(id),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


def _get_connection() -> sqlite3.Connection:
    return strategy_db.get_connection(_DB_PATH)


def _init_db() -> None:
    """Crea el esquema relacional y graba el registro inicial de capital si vacío."""
    with _get_connection() as con:
        for stmt in _DDL.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                con.execute(stmt)
        row = con.execute("SELECT COUNT(*) AS n FROM connors_capital").fetchone()
        if row["n"] == 0:
            con.execute(
                "INSERT INTO connors_capital "
                "(fecha, cash, valor_posiciones, capital_total, nota) "
                "VALUES (?, ?, 0.0, ?, 'inicio')",
                (
                    date.today().isoformat(),
                    config.CONNORS_CAPITAL,
                    config.CONNORS_CAPITAL,
                ),
            )


def _get_current_cash() -> float:
    _init_db()
    return strategy_db.get_current_cash(_DB_PATH, "connors_capital", config.CONNORS_CAPITAL)


def _get_slot_size(cash: float, positions: dict) -> float:
    """
    Calcula el slot dinámico basado en el capital actual.

    El slot crece con las ganancias acumuladas pero nunca baja del mínimo
    inicial (CONNORS_CAPITAL * 0.995 / CONNORS_MAX_POS). Con pérdidas el
    max() garantiza que volvemos al slot de partida.

    Params:
        cash:      cash libre actual (resultado de _get_current_cash()).
        positions: posiciones abiertas (resultado de _get_open_positions()).
    Returns:
        Tamaño del slot en euros.
    """
    slot_minimo      = config.CONNORS_CAPITAL * 0.995 / config.CONNORS_MAX_POS
    valor_posiciones = sum(p.get("slot_size", 0.0) for p in positions.values())
    capital_actual   = cash + valor_posiciones
    return max(capital_actual * 0.995 / config.CONNORS_MAX_POS, slot_minimo)


def _append_capital(
    cash: float, valor_posiciones: float, nota: str,
    operacion_id: int | None = None,
) -> None:
    """Inserta un movimiento en el ledger de capital, opcionalmente ligado a una
    operación (FK `operacion_id` → `connors_operaciones.id`)."""
    with _get_connection() as con:
        con.execute(
            "INSERT INTO connors_capital "
            "(fecha, cash, valor_posiciones, capital_total, nota, operacion_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                date.today().isoformat(),
                cash, valor_posiciones, cash + valor_posiciones,
                nota, operacion_id,
            ),
        )


def _get_open_positions() -> dict:
    """Posiciones abiertas (estado='abierta'): {ticker: row_dict}."""
    _init_db()
    with _get_connection() as con:
        rows = con.execute(
            "SELECT * FROM connors_operaciones WHERE estado = 'abierta'"
        ).fetchall()
    return {row["ticker"]: dict(row) for row in rows}


def _get_pending_orders() -> dict:
    """Órdenes pendientes en Alpaca (estado='pendiente'): {ticker: row_dict}."""
    _init_db()
    with _get_connection() as con:
        rows = con.execute(
            "SELECT * FROM connors_operaciones WHERE estado = 'pendiente'"
        ).fetchall()
    return {row["ticker"]: dict(row) for row in rows}


def _get_pending_sells() -> set:
    """
    Consulta Alpaca por órdenes de venta abiertas sobre tickers de nuestra
    cartera. Si Alpaca no responde, devuelve set vacío (fallback conservador:
    los slots siguen contándose como ocupados).

    Returns:
        Conjunto de tickers con venta pendiente de ejecución en Alpaca.
    """
    if not getattr(config, "ALPACA_ENABLED", False):
        return set()
    try:
        from modules.alpaca_broker import get_broker
        broker = get_broker()
        open_orders = broker.get_orders(status="open")
        portfolio   = set(_get_open_positions().keys())
        return {o["ticker"] for o in open_orders
                if o.get("side") == "sell" and o["ticker"] in portfolio}
    except Exception as exc:
        _slog.warning("_get_pending_sells: no se pudo consultar Alpaca: %s", exc)
        return set()


def _count_pending_orders() -> int:
    """Número de órdenes pendientes (cuenta como slots ocupados)."""
    _init_db()
    with _get_connection() as con:
        return con.execute(
            "SELECT COUNT(*) FROM connors_operaciones WHERE estado = 'pendiente'"
        ).fetchone()[0]


def _insert_pending_order(
    ticker: str, alpaca_order_id: str, entry_price: float, slot_size: float,
    crsi: float, rsi3: float, streak: int,
) -> None:
    """
    Registra una operación enviada a Alpaca aún no ejecutada (estado='pendiente').
    `entry_price` es el precio límite objetivo (se guarda en `limit_price`); el
    precio de ejecución real se rellena al hacer fill.
    """
    shares = slot_size / entry_price if entry_price else None
    with _get_connection() as con:
        # Garantiza unicidad de ticker activo: limpia un pendiente previo del mismo
        # ticker (el índice parcial único lo prohíbe; el flujo no debería llegar aquí).
        con.execute(
            "DELETE FROM connors_operaciones "
            "WHERE ticker = ? AND estado = 'pendiente'", (ticker,)
        )
        con.execute(
            "INSERT INTO connors_operaciones "
            "(ticker, estado, alpaca_order_id, entry_date, limit_price, shares, "
            " slot_size, connors_rsi_entrada, rsi3_entrada, streak_entrada) "
            "VALUES (?, 'pendiente', ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, alpaca_order_id, date.today().isoformat(), entry_price,
             shares, slot_size, crsi, rsi3, streak),
        )


def _delete_pending_order(ticker: str) -> None:
    """Elimina una orden pendiente no ejecutada (expirada/cancelada en Alpaca).

    Solo borra filas en estado 'pendiente': una operación que llegó a abrirse o
    cerrarse nunca se elimina (su historial se conserva)."""
    with _get_connection() as con:
        con.execute(
            "DELETE FROM connors_operaciones "
            "WHERE ticker = ? AND estado = 'pendiente'", (ticker,)
        )


def _materialize_pending_as_position(
    pending: dict, fill_price: float, filled_qty: float,
) -> None:
    """
    Promueve una operación 'pendiente' (ya ejecutada en Alpaca) a 'abierta':
    transición por UPDATE (no se mueve ni se borra la fila) y descuenta el cash.
    """
    valor_compra = filled_qty * fill_price
    commis       = valor_compra * config.COMMISSION
    op_id        = pending["id"]
    with _get_connection() as con:
        con.execute(
            "UPDATE connors_operaciones SET "
            "  estado = 'abierta', entry_date = ?, entry_price = ?, shares = ?, "
            "  valor_compra = ?, stop_loss_price = ?, commission_entry = ?, "
            "  updated_at = datetime('now') "
            "WHERE id = ?",
            (
                date.today().isoformat(),
                fill_price,
                filled_qty,
                valor_compra,
                fill_price * (1.0 - config.CONNORS_STOP_LOSS),
                commis,
                op_id,
            ),
        )
    # Descuenta cash sólo cuando la orden se materializa
    cash_actual = _get_current_cash()
    cash_nuevo  = cash_actual - valor_compra - commis
    _append_capital(
        cash_nuevo,
        sum(p["shares"] * p["entry_price"] for p in _get_open_positions().values()),
        nota=f"fill {pending['ticker']}",
        operacion_id=op_id,
    )


def _check_pending_orders() -> dict:
    """
    Reconcilia las operaciones en estado 'pendiente' con Alpaca:
      · filled                  → UPDATE a estado 'abierta' + descuenta cash
      · expired / canceled      → elimina la operación pendiente (no descuenta cash)
      · new / accepted / etc.   → no hace nada (sigue pendiente)

    Returns:
        dict con conteos `filled`, `expired`, `canceled`, `pending`.
    """
    if not getattr(config, "ALPACA_ENABLED", False):
        return {"filled": 0, "expired": 0, "canceled": 0, "pending": 0}

    pendings = _get_pending_orders()
    if not pendings:
        return {"filled": 0, "expired": 0, "canceled": 0, "pending": 0}

    try:
        from modules.alpaca_broker import get_broker
        broker = get_broker()
    except Exception as exc:
        console.print(f"[yellow]⚠ No se pudo contactar con Alpaca: {exc}[/yellow]")
        return {"filled": 0, "expired": 0, "canceled": 0, "pending": len(pendings)}

    stats = {"filled": 0, "expired": 0, "canceled": 0, "pending": 0}
    for ticker, pending in pendings.items():
        try:
            order = broker.get_order(pending["alpaca_order_id"])
        except Exception as exc:
            console.print(
                f"[yellow]⚠ {ticker}: no se pudo consultar la orden — {exc}[/yellow]"
            )
            stats["pending"] += 1
            continue

        status = order.get("status", "").lower()
        if status == "filled":
            fill_price = float(order.get("filled_avg_price") or pending["limit_price"])
            filled_qty = float(order.get("filled_qty") or 0.0)
            if filled_qty <= 0:
                filled_qty = pending["slot_size"] / fill_price
            _materialize_pending_as_position(pending, fill_price, filled_qty)
            console.print(
                f"[green]✓ {ticker}: orden ejecutada (filled) "
                f"@ {fill_price:.2f}[/green]"
            )
            stats["filled"] += 1
        elif status == "expired":
            _delete_pending_order(ticker)
            console.print(f"[yellow]· {ticker}: orden expirada — descartada[/yellow]")
            stats["expired"] += 1
        elif status in ("canceled", "cancelled"):
            _delete_pending_order(ticker)
            console.print(f"[yellow]· {ticker}: orden cancelada — descartada[/yellow]")
            stats["canceled"] += 1
        else:
            stats["pending"] += 1
    _slog.info(
        "_check_pending_orders: filled=%d expired=%d canceled=%d pending=%d",
        stats["filled"], stats["expired"], stats["canceled"], stats["pending"],
    )
    return stats


# ── Descarga de datos (yfinance directo, sin data.py) ─────────────────────────

_LOOKBACK_CALENDAR_DAYS = 420   # ≈ 290 días hábiles → suficiente para SMA200 + percentil


def _download_universe(
    start:   str | None = None,
    end:     str | None = None,
    tickers: list | None = None,
) -> tuple[dict, pd.DataFrame | None]:
    """
    Descarga el universo dado + SPY. Si `start`/`end` son None usa la ventana
    de sesión (hoy-420 → hoy) y aprovecha el caché. Si se pasan explícitos,
    siempre descarga fresh con esas fechas (no usa caché). `tickers` permite
    restringir el universo (default: load_universe()).
    """
    # Limpiar cachés parquet de días anteriores antes de cualquier descarga
    yf_cache.cleanup_old_caches()

    use_session = start is None and end is None
    if use_session:
        start = (date.today() - timedelta(days=_LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
        end   = date.today().strftime("%Y-%m-%d")

    if tickers is None:
        from modules.data import load_universe
        tickers = load_universe()
    else:
        tickers = list(tickers)

    console.print(
        f"[dim]Período de descarga: {start} → {end}  "
        f"· universo: {len(tickers)} tickers + SPY[/dim]"
    )

    universe = sorted(set(tickers + ["SPY"]))

    if use_session:
        pending = [
            t for t in universe
            if t not in yf_cache.session_data
            or len(yf_cache.session_data[t]) < yf_cache.MIN_CACHE_ROWS
        ]
    else:
        pending = universe   # no usar caché en backtest

    cached_n = len(universe) - len(pending)
    if pending:
        console.print(
            f"[cyan]Cargando {len(pending)} tickers (caché parquet o yf.download) "
            f"· {cached_n} ya en sesión[/cyan]"
        )
        for t in pending:
            df = yf_cache.load_ticker(t, start, end)
            if df is None:
                continue
            if not use_session or len(df) >= yf_cache.MIN_CACHE_ROWS:
                yf_cache.set_session(t, df)

    spy = yf_cache.get_session("SPY")
    if spy is None or spy.empty:
        console.print("[red]No se pudo obtener SPY con histórico suficiente.[/red]")
        return {t: yf_cache.session_data[t] for t in tickers if t in yf_cache.session_data}, None

    if not yf_cache.wait_for_last_close():
        return {}, None

    raw_out = {t: yf_cache.session_data[t] for t in tickers if t in yf_cache.session_data}
    return raw_out, spy


# ── Cálculo ConnorsRSI ────────────────────────────────────────────────────────

def _streak(closes: pd.Series) -> pd.Series:
    """
    Serie de rachas: +N si lleva N días subiendo, -N si N días bajando, 0 si flat.
    """
    diff = closes.diff()
    out  = np.zeros(len(closes), dtype=float)
    prev = 0.0
    for i in range(1, len(closes)):
        d = diff.iloc[i]
        if pd.isna(d):
            out[i] = 0.0
            prev = 0.0
            continue
        if d > 0:
            prev = prev + 1 if prev > 0 else 1
        elif d < 0:
            prev = prev - 1 if prev < 0 else -1
        else:
            prev = 0
        out[i] = prev
    return pd.Series(out, index=closes.index)


def _percent_rank(returns: pd.Series, lookback: int) -> pd.Series:
    """% de los últimos `lookback` retornos <= retorno actual (escala 0-100)."""
    def _rank(window):
        return float((window <= window[-1]).sum()) / len(window) * 100.0
    return returns.rolling(lookback).apply(_rank, raw=True)


def compute_connors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Añade al DataFrame columnas RSI3, STREAK, RSI_STREAK, PCT_RANK,
    CONNORS_RSI y SMA200. Espera al menos `Close` en `df`.
    """
    df = df.copy()
    df["RSI3"]       = df.ta.rsi(length=config.CONNORS_RSI_PERIOD)
    df["STREAK"]     = _streak(df["Close"])
    df["RSI_STREAK"] = ta.rsi(df["STREAK"], length=config.CONNORS_STREAK_PERIOD)
    df["RET"]        = df["Close"].pct_change()
    df["PCT_RANK"]   = _percent_rank(df["RET"], config.CONNORS_RANK_PERIOD)
    df["CONNORS_RSI"] = (df["RSI3"] + df["RSI_STREAK"] + df["PCT_RANK"]) / 3.0
    df["SMA200"]     = df["Close"].rolling(200).mean()
    return df


# ── Detección de señales (fecha actual) ───────────────────────────────────────

def _has_3_day_low(df: pd.DataFrame, target: pd.Timestamp) -> bool:
    """True si Close[target] es el mínimo de los últimos 3 cierres (incluido hoy)."""
    sub = df.loc[:target, "Close"].tail(3)
    if len(sub) < 3:
        return False
    last = float(sub.iloc[-1])
    return last <= float(sub.iloc[0]) and last <= float(sub.iloc[1])


def _scan_entry_signals(
    raw: dict, spy: pd.DataFrame, target: pd.Timestamp | None = None,
) -> list:
    """
    Devuelve [{ticker, close, crsi, rsi3, streak, limit_price, sma200}] de los
    tickers que cumplen TODAS las condiciones de entrada en `target` (default:
    última fecha común). Ordenado por ConnorsRSI ascendente.
    """
    spy_with = spy.copy()
    if "SMA200" not in spy_with.columns:
        spy_with["SMA200"] = spy_with["Close"].rolling(200).mean()

    if target is None:
        target = max(df.index[-1] for df in raw.values() if not df.empty)

    if target not in spy_with.index:
        return []
    spy_close = float(spy_with.loc[target, "Close"])
    spy_sma   = spy_with.loc[target, "SMA200"]
    if pd.isna(spy_sma) or spy_close < float(spy_sma):
        return []

    signals = []
    for ticker, df in raw.items():
        if target not in df.index:
            continue
        try:
            ind = compute_connors(df)
        except Exception:
            continue
        if target not in ind.index:
            continue
        row    = ind.loc[target]
        close  = float(row["Close"])
        sma200 = row["SMA200"]
        crsi   = row["CONNORS_RSI"]
        if pd.isna(sma200) or pd.isna(crsi):
            continue
        if close <= float(sma200):
            continue
        if crsi >= config.CONNORS_ENTRY_CRSI:
            continue
        if not _has_3_day_low(df, target):
            continue
        signals.append({
            "ticker":      ticker,
            "close":       close,
            "crsi":        float(crsi),
            "rsi3":        float(row["RSI3"]) if not pd.isna(row["RSI3"]) else None,
            "streak":      int(row["STREAK"]) if not pd.isna(row["STREAK"]) else 0,
            "limit_price": close * (1.0 - config.CONNORS_ENTRY_LIMIT),
            "sma200":      float(sma200),
        })
    signals.sort(key=lambda s: s["crsi"])
    return signals


# ── Opción A — Señales de entrada hoy ────────────────────────────────────────

def opcion_senales_hoy() -> None:
    raw, spy = _download_universe()
    if not raw or spy is None:
        return
    signals = _scan_entry_signals(raw, spy)
    if not signals:
        console.print(
            "\n[yellow]No hay señales activas hoy "
            "(o el mercado está en tendencia bajista).[/yellow]"
        )
        return

    table = Table(
        title=f"[bold cyan]Señales ConnorsRSI · ConnorsRSI < {config.CONNORS_ENTRY_CRSI}"
              "[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Ticker",    justify="center", style="bold cyan")
    table.add_column("Close",     justify="right")
    table.add_column("ConnorsRSI", justify="right")
    table.add_column("RSI3",      justify="right")
    table.add_column("Streak",    justify="center")
    table.add_column("Lím. entrada", justify="right")
    for s in signals[:30]:
        table.add_row(
            s["ticker"],
            f"{s['close']:.2f}",
            f"[bold]{s['crsi']:5.1f}[/bold]",
            f"{s['rsi3']:.1f}" if s["rsi3"] is not None else "—",
            f"{s['streak']:+d}",
            f"{s['limit_price']:.2f}",
        )
    console.print(table)
    console.print(
        f"[dim]Mostradas las {min(len(signals), 30)} mejores (de "
        f"{len(signals)} en total).[/dim]"
    )


# ── Opción B — Posiciones abiertas ────────────────────────────────────────────

def _last_close(ticker: str) -> float | None:
    df = yf_cache.get_session(ticker)
    if df is None or df.empty:
        today_str   = date.today().strftime("%Y-%m-%d")
        short_start = (date.today() - timedelta(days=15)).strftime("%Y-%m-%d")
        df = yf_cache.yf_download_one(ticker, short_start, today_str)
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def opcion_posiciones_abiertas() -> None:
    positions = _get_open_positions()
    if not positions:
        console.print("\n[yellow]No hay posiciones ConnorsRSI abiertas.[/yellow]")
        return

    # Asegurarse de tener datos actuales
    if not yf_cache.session_data:
        _download_universe()

    table = Table(
        title="[bold cyan]Cartera ConnorsRSI[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Ticker",     justify="center", style="bold cyan")
    table.add_column("Entrada",    justify="center")
    table.add_column("P. ent.",    justify="right")
    table.add_column("P. actual",  justify="right")
    table.add_column("SL",         justify="right")
    table.add_column("PnL",        justify="right")
    table.add_column("Ret %",      justify="right")
    table.add_column("Días",       justify="right")

    total_inv = 0.0
    total_val = 0.0
    today_d   = date.today()

    for ticker, pos in positions.items():
        entry_p = pos["entry_price"]
        shares  = pos["shares"]
        sl_p    = pos["stop_loss_price"]
        cur_p   = _last_close(ticker) or entry_p
        pnl     = shares * (cur_p - entry_p)
        ret     = (cur_p - entry_p) / entry_p * 100 if entry_p else 0.0
        try:
            entry_d = datetime.fromisoformat(pos["entry_date"]).date()
            dias    = (today_d - entry_d).days
        except Exception:
            dias = 0
        pc = "green" if pnl >= 0 else "red"
        table.add_row(
            ticker,
            pos["entry_date"],
            f"{entry_p:.2f}",
            f"{cur_p:.2f}",
            f"{sl_p:.2f}",
            f"[{pc}]{pnl:>+8,.2f} €[/{pc}]",
            f"[{pc}]{ret:+5.1f} %[/{pc}]",
            str(dias),
        )
        total_inv += pos["valor_compra"]
        total_val += shares * cur_p

    console.print(table)

    cash      = _get_current_cash()
    total_cap = cash + total_val
    unreal    = total_val - total_inv
    pc        = "green" if unreal >= 0 else "red"
    txt = Text()
    txt.append(f"  Cash libre             : {cash:>10,.2f} €\n", style="white")
    txt.append(f"  Valor coste            : {total_inv:>10,.2f} €\n", style="white")
    txt.append(f"  Valor mercado          : {total_val:>10,.2f} €\n", style="white")
    txt.append(f"  PnL no realizado       : ", style="white")
    txt.append(f"{unreal:+,.2f} €\n", style=f"bold {pc}")
    txt.append(f"  Capital total          : {total_cap:>10,.2f} €\n", style="bold white")
    console.print(Panel(
        txt, title="[bold cyan]Resumen[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


# ── Opción C — Registrar entrada ─────────────────────────────────────────────

def _abrir_posicion(sig: dict, cash: float, slot_size: float) -> tuple[float, bool]:
    """
    Abre una posición en `connors_operaciones` y devuelve `(cash_actualizado, ok)`.
    `slot_size` es el objetivo; si el cash disponible no llega al slot completo
    se recorta proporcionalmente (con margen para la comisión). Si cae por
    debajo del 95 % del slot, no abre y devuelve `(cash, False)`.
    """
    entry_p   = sig["limit_price"]
    cost_full = slot_size * (1 + config.COMMISSION)
    if cash >= cost_full:
        effective_slot = slot_size
    else:
        effective_slot = cash / (1 + config.COMMISSION)
    if effective_slot < slot_size * 0.95:
        console.print(
            f"  [red]✗ {sig['ticker']}: capital insuficiente "
            f"(menos del 95 % del slot)[/red]"
        )
        _slog.warning("apertura bloqueada %s: capital insuficiente (cash=%.0f€ slot=%.0f€)",
                      sig["ticker"], cash, slot_size)
        _tlog.warning("[CONNORS] BLOQUEADO %-6s · capital insuficiente (cash=%.0f€ < slot=%.0f€)",
                      sig["ticker"], cash, slot_size)
        return cash, False
    commis = effective_slot * config.COMMISSION
    shares = effective_slot / entry_p

    # ── Flujo con Alpaca activo ──────────────────────────────────────────────
    # Cuando ALPACA_ENABLED=True la operación NO se abre todavía: primero se
    # envía la orden a Alpaca y se registra con estado 'pendiente'.
    # `_check_pending_orders()` la promueve a 'abierta' (UPDATE) cuando la orden
    # se ejecute. El cash NO se descuenta hasta que esto ocurra.
    if getattr(config, "ALPACA_ENABLED", False):
        try:
            from modules.alpaca_broker import get_broker
            broker = get_broker()
            resp = broker.submit_order(
                ticker=sig["ticker"], qty=shares, side="buy",
                order_type="limit", limit_price=entry_p,
            )
            _insert_pending_order(
                sig["ticker"], resp["order_id"], entry_p, effective_slot,
                sig["crsi"], sig["rsi3"], sig["streak"],
            )
            console.print(
                f"  [cyan]📤 {sig['ticker']}: orden enviada a Alpaca "
                f"({resp['order_id']}) — pendiente de ejecución[/cyan]"
            )
            _slog.info("apertura pending Alpaca: %s  entry=%.2f  slot=%.0f€  CRSI=%.1f",
                       sig["ticker"], entry_p, effective_slot, sig["crsi"])
            _tlog.info("[CONNORS] APERTURA  %-6s @ %.2f$  · slot %.0f€  · CRSI=%.1f",
                       sig["ticker"], entry_p, effective_slot, sig["crsi"])
            # No descontamos cash; lo hará `_materialize_pending_as_position`
            return cash, True
        except Exception as exc:
            console.print(
                f"  [red]✗ {sig['ticker']}: Alpaca rechazó la orden — {exc}[/red]"
            )
            _slog.error("apertura Alpaca rechazada %s: %s", sig["ticker"], exc, exc_info=True)
            return cash, False

    # ── Flujo sin Alpaca (SQLite es la fuente de verdad) ─────────────────────
    # Se inserta directamente como operación 'abierta' (sin paso intermedio
    # 'pendiente', porque no hay orden externa que reconciliar).
    with _get_connection() as con:
        con.execute(
            """INSERT INTO connors_operaciones
               (ticker, estado, entry_date, entry_price, limit_price, shares,
                valor_compra, slot_size, stop_loss_price, commission_entry,
                connors_rsi_entrada, rsi3_entrada, streak_entrada)
               VALUES (?, 'abierta', ?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sig["ticker"],
                date.today().isoformat(),
                entry_p,
                sig["limit_price"],
                shares,
                effective_slot,
                effective_slot,
                entry_p * (1.0 - config.CONNORS_STOP_LOSS),
                commis,
                sig["crsi"],
                sig["rsi3"],
                sig["streak"],
            ),
        )
    cash_new = cash - effective_slot - commis
    console.print(
        f"  [green]✓ {sig['ticker']}: {shares:.4f} acciones @ {entry_p:.2f} "
        f"(CRSI {sig['crsi']:.1f})[/green]"
    )
    _slog.info("apertura SQLite: %s  entry=%.2f  slot=%.0f€  CRSI=%.1f",
               sig["ticker"], entry_p, effective_slot, sig["crsi"])
    _tlog.info("[CONNORS] APERTURA  %-6s @ %.2f$  · slot %.0f€  · CRSI=%.1f",
               sig["ticker"], entry_p, effective_slot, sig["crsi"])
    return cash_new, True


def _post_apertura_actualizar_capital(cash_final: float, n_abiertas: int) -> None:
    """Registra el nuevo registro de capital tras apertura(s)."""
    if n_abiertas == 0:
        return
    positions_after = _get_open_positions()
    val_pos = sum(
        p["shares"] * (_last_close(t) or p["entry_price"])
        for t, p in positions_after.items()
    )
    plural = "es" if n_abiertas != 1 else ""
    _append_capital(
        cash_final, val_pos,
        nota=f"apertura(s) ({n_abiertas} posición{plural})",
    )


def aperturas_automaticas(
    reconciliar_primero: bool = False,
    confirmar: bool = True,
) -> int:
    """
    Apertura ConnorsRSI no-interactiva en modo A.

    Selecciona automáticamente las N señales de menor CRSI donde
    N = slots_libres, filtrando las que ya tienen una operación viva (estado
    'abierta' o 'pendiente'), y envía las órdenes en bloque tras (opcional)
    una única confirmación.

    Diseñada como punto de entrada del autopiloto (opción 12) y como
    implementación del modo A dentro de `opcion_registrar_entrada` —ambos
    rutas comparten lógica y comportamiento.

    Params:
        reconciliar_primero: si True, llama a `_check_pending_orders()` antes
            de calcular slots libres. Útil cuando se invoca de forma
            standalone; ponerlo a False si quien llama ya reconcilió en este
            mismo turno (evita doble llamada al broker).
        confirmar: si True, pide UN `Confirm.ask("¿Confirmar apertura?")`
            antes de enviar las órdenes. Si False, envía sin preguntar
            (ejecución totalmente desatendida — útil en tests y en flujos
            programáticos que ya han confirmado aguas arriba).

    Returns:
        Nº de órdenes enviadas correctamente. 0 si no hay slots, no hay
        señales aprovechables, falta capital o el usuario canceló la
        confirmación.
    """
    if not es_dia_habil_nyse():
        _slog.info("Festivo NYSE -- sin operativa ConnorsRSI hoy")
        console.print("[dim]Festivo NYSE — sin operativa ConnorsRSI hoy.[/dim]")
        return 0

    if reconciliar_primero:
        _check_pending_orders()

    raw, spy = _download_universe()
    if not raw or spy is None:
        return 0
    signals = _scan_entry_signals(raw, spy)
    if not signals:
        console.print("[yellow]No hay señales activas hoy.[/yellow]")
        return 0

    positions     = _get_open_positions()
    pendings      = _get_pending_orders()
    pending_sells = _get_pending_sells()
    n_open        = len(positions)
    n_pending     = len(pendings)
    n_selling     = len(pending_sells)
    cash          = _get_current_cash()

    slot_size    = _get_slot_size(cash, positions)
    slots_libres = config.CONNORS_MAX_POS - n_open - n_pending + n_selling
    if slots_libres <= 0:
        console.print(
            f"[yellow]No hay slots disponibles "
            f"({n_open} abiertas + {n_pending} pendientes / "
            f"{config.CONNORS_MAX_POS}). Cierra o espera ejecución.[/yellow]"
        )
        return 0
    if cash < slot_size * 0.95:
        console.print(
            f"[yellow]Cash insuficiente ({cash:,.2f} € libre, mínimo "
            f"{slot_size * 0.95:,.2f} € = 95 % del slot).[/yellow]"
        )
        return 0

    # Tickers en venta pendiente se excluyen de 'ocupados' (slot liberado)
    # pero no se vuelven candidatos de re-entrada en el mismo ciclo.
    ocupados  = (set(positions) - pending_sells) | set(pendings)
    available = [s for s in signals if s["ticker"] not in ocupados]
    if not available:
        console.print("[yellow]Todas las señales activas ya están en cartera.[/yellow]")
        return 0

    a_abrir = available[:slots_libres]
    plural  = "es" if len(a_abrir) != 1 else ""
    console.print(
        f"\n[bold]Abriendo automáticamente {len(a_abrir)} "
        f"posición{plural}:[/bold]"
    )
    for i, s in enumerate(a_abrir, 1):
        console.print(
            f"  [cyan]{i}[/cyan]. {s['ticker']:<6}  "
            f"CRSI={s['crsi']:5.1f}  límite={s['limit_price']:.2f}"
        )

    if confirmar:
        if not Confirm.ask("\n¿Confirmar apertura?", default=False):
            console.print("[yellow]Apertura cancelada.[/yellow]")
            return 0

    n_ok = 0
    for s in a_abrir:
        cash, ok = _abrir_posicion(s, cash, slot_size)
        if ok:
            n_ok += 1
    _post_apertura_actualizar_capital(cash, n_ok)
    return n_ok


def opcion_registrar_entrada() -> None:
    """
    Confirma señal(es) y registra la apertura. Soporta dos modos:
      · Automático (A): delega en `aperturas_automaticas()` (única confirmación).
      · Manual (M): el usuario elige una señal numerada de la lista (top 10).

    Si ALPACA_ENABLED=True, antes de listar señales reconcilia las órdenes
    pendientes en Alpaca (filled→portfolio, expired/canceled→descartadas).
    """
    if not es_dia_habil_nyse():
        _slog.info("Festivo NYSE -- sin operativa ConnorsRSI hoy")
        console.print("[dim]Festivo NYSE — sin operativa ConnorsRSI hoy.[/dim]")
        return

    # Reconcilia órdenes pendientes en Alpaca antes de calcular slots libres.
    _check_pending_orders()

    raw, spy = _download_universe()
    if not raw or spy is None:
        return
    signals = _scan_entry_signals(raw, spy)
    if not signals:
        console.print("[yellow]No hay señales activas hoy.[/yellow]")
        return

    positions     = _get_open_positions()
    pendings      = _get_pending_orders()
    pending_sells = _get_pending_sells()
    n_open        = len(positions)
    n_pending     = len(pendings)
    n_selling     = len(pending_sells)
    cash          = _get_current_cash()

    slot_size = _get_slot_size(cash, positions)

    # ── Slots libres (descuenta posiciones abiertas Y pendientes Alpaca) ─────
    slots_libres = config.CONNORS_MAX_POS - n_open - n_pending + n_selling
    if slots_libres <= 0:
        console.print(
            f"[yellow]No hay slots disponibles "
            f"({n_open} abiertas + {n_pending} pendientes / "
            f"{config.CONNORS_MAX_POS}). Cierra o espera ejecución.[/yellow]"
        )
        return
    if cash < slot_size * 0.95:
        console.print(
            f"[yellow]Cash insuficiente ({cash:,.2f} € libre, mínimo "
            f"{slot_size * 0.95:,.2f} € = 95 % del slot).[/yellow]"
        )
        return

    # Tickers en venta pendiente liberan slot pero no son re-candidatos
    ocupados  = (set(positions) - pending_sells) | set(pendings)
    available = [s for s in signals if s["ticker"] not in ocupados]
    if not available:
        console.print("[yellow]Todas las señales activas ya están en cartera.[/yellow]")
        return

    console.print(
        f"\n  [bold]Slots libres:[/bold] {slots_libres} / {config.CONNORS_MAX_POS}"
    )
    modo = Prompt.ask(
        "  ¿Apertura automática (mejores señales) o manual?",
        choices=["A", "M", "a", "m"], default="A",
    ).strip().upper()

    # ── Modo AUTOMÁTICO — delega en la función pública ───────────────────────
    if modo == "A":
        # Ya reconciliamos arriba, no repetir. El caché `yf_cache.session_data`
        # hace que la segunda llamada a `_download_universe` sea un no-op.
        aperturas_automaticas(reconciliar_primero=False, confirmar=True)
        return

    # ── Modo MANUAL (flujo clásico) ──────────────────────────────────────────
    console.print(
        f"\n[bold]Señales disponibles (top {min(len(available), 10)}):[/bold]"
    )
    for i, s in enumerate(available[:10], 1):
        console.print(
            f"  [cyan]{i:>2}[/cyan]  {s['ticker']:<6}  "
            f"close={s['close']:.2f}  CRSI={s['crsi']:5.1f}  "
            f"límite={s['limit_price']:.2f}"
        )
    raw_choice = Prompt.ask(
        "\n  Nº de señal a registrar (Enter para cancelar)",
        default="",
    ).strip()
    if not raw_choice:
        return
    try:
        idx = int(raw_choice) - 1
        sig = available[idx]
    except (ValueError, IndexError):
        console.print("[red]Selección inválida.[/red]")
        return
    if not Confirm.ask(
        f"\n¿Registrar entrada en {sig['ticker']} a precio límite "
        f"{sig['limit_price']:.2f}?",
        default=False,
    ):
        return
    cash, ok = _abrir_posicion(sig, cash, slot_size)
    _post_apertura_actualizar_capital(cash, 1 if ok else 0)


# ── Opción D — Registrar salida ──────────────────────────────────────────────

def _scan_exit_signals(positions: dict) -> list:
    """Para cada posición abierta, decide si toca salida y por qué motivo."""
    out: list = []
    today_d = date.today()
    for ticker, pos in positions.items():
        cur_p   = _last_close(ticker)
        if cur_p is None:
            continue
        entry_p = pos["entry_price"]
        sl_p    = pos["stop_loss_price"]
        try:
            entry_d = datetime.fromisoformat(pos["entry_date"]).date()
            dias    = (today_d - entry_d).days
        except Exception:
            dias = 0

        reason: str | None = None
        if cur_p <= sl_p:
            reason = "SL"
        elif dias >= config.CONNORS_TIME_STOP:
            reason = "TSTO"
        else:
            df = yf_cache.get_session(ticker)
            if df is not None and not df.empty:
                try:
                    ind = compute_connors(df)
                    crsi_now = float(ind["CONNORS_RSI"].dropna().iloc[-1])
                    if crsi_now > config.CONNORS_EXIT_CRSI:
                        reason = "CRSI_EXIT"
                except Exception:
                    pass
        out.append({
            "ticker": ticker, "pos": pos, "cur_p": cur_p,
            "dias": dias, "reason": reason,
        })
    return out


def opcion_registrar_salida() -> None:
    positions = _get_open_positions()
    if not positions:
        console.print("\n[yellow]No hay posiciones abiertas.[/yellow]")
        return

    if not yf_cache.session_data:
        _download_universe()

    info = _scan_exit_signals(positions)

    table = Table(
        title="[bold cyan]Estado de salida[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Nº",       justify="center")
    table.add_column("Ticker",   justify="center", style="bold cyan")
    table.add_column("Días",     justify="right")
    table.add_column("Precio",   justify="right")
    table.add_column("Entrada",  justify="right")
    table.add_column("Motivo",   justify="center")
    for i, e in enumerate(info, 1):
        reason_color = {
            "SL":        "red",
            "TSTO":      "yellow",
            "CRSI_EXIT": "blue",
        }.get(e["reason"] or "", "dim")
        reason_str = (
            f"[{reason_color}]{e['reason']}[/{reason_color}]"
            if e["reason"] else "[dim]—[/dim]"
        )
        table.add_row(
            str(i),
            e["ticker"],
            str(e["dias"]),
            f"{e['cur_p']:.2f}",
            f"{e['pos']['entry_price']:.2f}",
            reason_str,
        )
    console.print(table)

    raw_choice = Prompt.ask(
        "\n  Nº de posición a cerrar (Enter para cancelar)",
        default="",
    ).strip()
    if not raw_choice:
        return
    try:
        idx = int(raw_choice) - 1
        e   = info[idx]
    except (ValueError, IndexError):
        console.print("[red]Selección inválida.[/red]")
        return

    if not Confirm.ask(
        f"\n¿Cerrar {e['ticker']} a {e['cur_p']:.2f}?", default=True,
    ):
        return

    _close_position_db(
        e["ticker"], e["pos"], e["cur_p"], e["reason"] or "MANUAL", date.today(),
    )


def _close_position_db(
    ticker: str, pos: dict, exit_price: float, reason: str, exit_d: date,
) -> None:
    """Cierra una posición: UPDATE de la operación a estado 'cerrada'."""
    shares           = pos["shares"]
    entry_p          = pos["entry_price"]
    val_compra       = pos["valor_compra"]
    commission_entry = pos.get("commission_entry") or val_compra * config.COMMISSION
    commission_exit  = shares * exit_price * config.COMMISSION
    gross            = shares * (exit_price - entry_p)
    net              = gross - commission_entry - commission_exit
    ret_pct          = (exit_price - entry_p) / entry_p if entry_p else 0.0
    try:
        entry_d = datetime.fromisoformat(pos["entry_date"]).date()
        dias    = (exit_d - entry_d).days
    except Exception:
        dias    = 0

    # Transición 'abierta' → 'cerrada' por UPDATE de la misma fila (la operación
    # conserva todo su historial de entrada; sólo se rellenan los campos de salida).
    op_id = pos.get("id")
    with _get_connection() as con:
        if op_id is not None:
            con.execute(
                "UPDATE connors_operaciones SET "
                "  estado = 'cerrada', exit_date = ?, exit_price = ?, "
                "  commission_entry = COALESCE(commission_entry, ?), "
                "  commission_exit = ?, gross_pnl = ?, net_pnl = ?, "
                "  return_pct = ?, dias = ?, reason = ?, "
                "  updated_at = datetime('now') "
                "WHERE id = ?",
                (
                    exit_d.isoformat(), exit_price, commission_entry,
                    commission_exit, gross, net, ret_pct, dias, reason, op_id,
                ),
            )
        else:
            # Fallback (pos sin id): cerrar por ticker la operación abierta.
            con.execute(
                "UPDATE connors_operaciones SET "
                "  estado = 'cerrada', exit_date = ?, exit_price = ?, "
                "  commission_exit = ?, gross_pnl = ?, net_pnl = ?, "
                "  return_pct = ?, dias = ?, reason = ?, "
                "  updated_at = datetime('now') "
                "WHERE ticker = ? AND estado = 'abierta'",
                (
                    exit_d.isoformat(), exit_price, commission_exit,
                    gross, net, ret_pct, dias, reason, ticker,
                ),
            )

    cash     = _get_current_cash() + shares * exit_price - commission_exit
    val_pos  = sum(
        p["shares"] * (_last_close(t) or p["entry_price"])
        for t, p in _get_open_positions().items()
    )
    _append_capital(cash, val_pos, nota=f"salida {ticker} {reason}", operacion_id=op_id)
    pc = "green" if net >= 0 else "red"
    console.print(
        f"[{pc}]✓ Cerrado {ticker} ({reason}): PnL {net:+,.2f} € "
        f"({ret_pct*100:+.1f}%)[/{pc}]"
    )
    _slog.info("cierre: %s  reason=%s  pnl=%.2f€  dias=%d", ticker, reason, net, dias)
    _tlog.info("[CONNORS] CIERRE    %-6s · %-10s · PnL %+.2f€  · %dd",
               ticker, reason, net, dias)

    # ── Integración Alpaca (opcional) ────────────────────────────────────────
    if getattr(config, "ALPACA_ENABLED", False):
        try:
            from modules.alpaca_broker import get_broker
            broker = get_broker()
            broker.close_position(ticker)
            console.print(f"[cyan]📤 Posición cerrada en Alpaca: {ticker}[/cyan]")
        except Exception as exc:
            console.print(f"[yellow]⚠ Alpaca: {exc}[/yellow]")
            _slog.error("close_position Alpaca %s: %s", ticker, exc, exc_info=True)


# ── Opción E — Backtest ───────────────────────────────────────────────────────

def run_connors_backtest(
    tickers:       list | None = None,
    start_date:    str | None  = None,
    end_date:      str | None  = None,
    skip_download: bool        = False,
    export:        bool        = True,
) -> dict:
    """
    Backtest ConnorsRSI sobre un rango histórico.

    Params:
        skip_download: si True, reutiliza el contenido actual de
                       `yf_cache.session_data` (útil para el sweep que descarga
                       UNA VEZ al inicio y reúsa los datos en 324 combinaciones).
        export:        si False, no escribe el CSV de operaciones (útil
                       cuando el sweep ejecuta múltiples backtests seguidos).
    """
    from modules.data import load_universe
    tickers    = list(tickers) if tickers is not None else load_universe()
    start_date = start_date or config.DEFAULT_START_DATE
    end_date   = end_date   or date.today().strftime("%Y-%m-%d")

    # Descarga con margen extra para tener 200 cierres antes del start
    extra_days = 220 + 40
    ext_start  = (
        datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=int(extra_days * 1.6))
    ).strftime("%Y-%m-%d")

    if skip_download:
        # Reusar datos ya cargados en yf_cache.session_data (caso sweep)
        raw = {t: yf_cache.session_data[t] for t in tickers if t in yf_cache.session_data}
        spy = yf_cache.get_session("SPY")
        if not raw or spy is None or spy.empty:
            console.print(
                "[red]skip_download=True pero no hay datos en yf_cache.session_data.[/red]"
            )
            return {"trades": [], "capital_final": config.CONNORS_CAPITAL, "csv": None}
    else:
        # Limpiar caché y descargar (con el universo del backtest)
        yf_cache.clear_session()
        raw, spy = _download_universe(start=ext_start, end=end_date, tickers=tickers)
        if not raw or spy is None:
            return {"trades": [], "capital_final": config.CONNORS_CAPITAL, "csv": None}

    spy = spy.copy()
    spy["SMA200"] = spy["Close"].rolling(200).mean()

    # Precalcular indicadores
    console.print(
        f"[cyan]Calculando ConnorsRSI para {len(raw)} tickers...[/cyan]"
    )
    indicators: dict = {}
    for t, df in raw.items():
        try:
            ind = compute_connors(df)
        except Exception as exc:
            console.print(f"  [red]✗ {t}: indicador fallido — {exc}[/red]")
            continue
        valid = int(ind["CONNORS_RSI"].notna().sum())
        console.print(
            f"  [dim]{t}: {len(ind)} filas · "
            f"{valid} con CRSI válido · "
            f"rango CRSI [{ind['CONNORS_RSI'].min():.1f}, "
            f"{ind['CONNORS_RSI'].max():.1f}][/dim]"
        )
        indicators[t] = ind

    start_ts  = pd.Timestamp(start_date)
    end_ts    = pd.Timestamp(end_date)
    all_dates = spy.index[(spy.index >= start_ts) & (spy.index <= end_ts)]
    if len(all_dates) == 0:
        console.print(
            f"[red]Rango vacío de fechas.[/red]\n"
            f"[red]  pedido        : {start_ts.date()} → {end_ts.date()}[/red]\n"
            f"[red]  SPY disponible: {spy.index[0].date()} → {spy.index[-1].date()}[/red]"
        )
        return {"trades": [], "capital_final": config.CONNORS_CAPITAL, "csv": None}

    initial_capital = config.CONNORS_CAPITAL
    n_target        = config.CONNORS_MAX_POS
    # Reserva del 0.5 % para comisiones (ver `opcion_registrar_entrada`)
    slot_size       = (initial_capital * 0.995) / n_target
    cash            = initial_capital
    positions: dict = {}
    closed: list    = []

    console.print(
        f"[bold cyan]Backtest ConnorsRSI {start_date} → {end_date}  "
        f"({len(all_dates)} sesiones · capital {initial_capital:.0f} € · "
        f"{slot_size:.0f} €/slot)[/bold cyan]"
    )

    # Contadores de diagnóstico
    n_market_off    = 0
    n_signals_total = 0
    n_entries       = 0
    n_exits_sl      = 0
    n_exits_tsto    = 0
    n_exits_crsi    = 0
    rejected = {"sma": 0, "crsi": 0, "low3": 0}

    for d in all_dates:
        # ── Salidas ──────────────────────────────────────────────────────────
        for ticker in list(positions.keys()):
            pos = positions[ticker]
            ind = indicators.get(ticker)
            if ind is None or d not in ind.index:
                continue
            row     = ind.loc[d]
            close   = float(row["Close"])
            crsi    = row["CONNORS_RSI"]
            days    = (d - pos["entry_date"]).days

            reason: str | None = None
            exit_p             = close
            if close <= pos["stop_loss_price"]:
                reason = "SL"
                exit_p = pos["stop_loss_price"]
            elif not pd.isna(crsi) and float(crsi) > config.CONNORS_EXIT_CRSI:
                reason = "CRSI_EXIT"
            elif days >= config.CONNORS_TIME_STOP:
                reason = "TSTO"

            if reason is None:
                continue
            if   reason == "SL":        n_exits_sl   += 1
            elif reason == "TSTO":      n_exits_tsto += 1
            elif reason == "CRSI_EXIT": n_exits_crsi += 1
            shares           = pos["shares"]
            val_compra       = pos["valor_compra"]
            commission_exit  = shares * exit_p * config.COMMISSION
            gross            = shares * (exit_p - pos["entry_price"])
            net              = gross - pos["commission_entry"] - commission_exit
            ret_pct          = (exit_p - pos["entry_price"]) / pos["entry_price"]
            cash            += shares * exit_p - commission_exit
            closed.append({
                "ticker":           ticker,
                "entry_date":       pos["entry_date"],
                "exit_date":        d,
                "entry_price":      pos["entry_price"],
                "limit_price":      pos["limit_price"],
                "exit_price":       exit_p,
                "shares":           shares,
                "valor_compra":     val_compra,
                "commission_entry": pos["commission_entry"],
                "commission_exit":  commission_exit,
                "gross_pnl":        gross,
                "net_pnl":          net,
                "return_pct":       ret_pct,
                "dias":             days,
                "reason":           reason,
                "connors_rsi_entrada": pos["connors_rsi_entrada"],
                "rsi3_entrada":     pos["rsi3_entrada"],
                "streak_entrada":   pos["streak_entrada"],
            })
            del positions[ticker]

        # ── Filtro de mercado ────────────────────────────────────────────────
        spy_close = float(spy.loc[d, "Close"])
        spy_sma   = spy.loc[d, "SMA200"]
        market_ok = (not pd.isna(spy_sma)) and (spy_close >= float(spy_sma))
        if not market_ok:
            n_market_off += 1
            continue
        if len(positions) >= n_target:
            continue

        # ── Entradas ─────────────────────────────────────────────────────────
        candidates = []
        for ticker, ind in indicators.items():
            if ticker in positions:
                continue
            if d not in ind.index:
                continue
            row    = ind.loc[d]
            close  = float(row["Close"])
            sma200 = row["SMA200"]
            crsi   = row["CONNORS_RSI"]
            if pd.isna(sma200) or pd.isna(crsi):
                continue
            if close <= float(sma200):
                rejected["sma"] += 1
                continue
            if float(crsi) >= config.CONNORS_ENTRY_CRSI:
                rejected["crsi"] += 1
                continue
            if not _has_3_day_low(ind, d):
                rejected["low3"] += 1
                continue
            candidates.append({
                "ticker": ticker,
                "close":  close,
                "crsi":   float(crsi),
                "rsi3":   float(row["RSI3"]) if not pd.isna(row["RSI3"]) else None,
                "streak": int(row["STREAK"]) if not pd.isna(row["STREAK"]) else 0,
            })
        n_signals_total += len(candidates)
        candidates.sort(key=lambda c: c["crsi"])

        for c in candidates:
            if len(positions) >= n_target:
                break
            limit_p = c["close"] * (1.0 - config.CONNORS_ENTRY_LIMIT)
            # Tamaño efectivo: slot completo si hay cash; si no, recortamos a
            # lo disponible (con margen para comisión). Aceptamos sólo si la
            # apertura cubre ≥ 95 % del slot objetivo.
            cost_full = slot_size * (1 + config.COMMISSION)
            if cash >= cost_full:
                effective_slot = slot_size
            else:
                effective_slot = cash / (1 + config.COMMISSION)
            if effective_slot < slot_size * 0.95:
                continue
            commis = effective_slot * config.COMMISSION
            shares = effective_slot / limit_p
            cash  -= effective_slot + commis
            n_entries += 1
            positions[c["ticker"]] = {
                "entry_date":       d,
                "entry_price":      limit_p,
                "limit_price":      limit_p,
                "shares":           shares,
                "valor_compra":     effective_slot,
                "commission_entry": commis,
                "stop_loss_price":  limit_p * (1.0 - config.CONNORS_STOP_LOSS),
                "connors_rsi_entrada": c["crsi"],
                "rsi3_entrada":     c["rsi3"],
                "streak_entrada":   c["streak"],
            }

    # Cierre final
    final_d = all_dates[-1]
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        ind = indicators.get(ticker)
        exit_p = (
            float(ind.loc[final_d, "Close"])
            if ind is not None and final_d in ind.index
            else pos["entry_price"]
        )
        shares           = pos["shares"]
        commission_exit  = shares * exit_p * config.COMMISSION
        gross            = shares * (exit_p - pos["entry_price"])
        net              = gross - pos["commission_entry"] - commission_exit
        ret_pct          = (exit_p - pos["entry_price"]) / pos["entry_price"]
        days             = (final_d - pos["entry_date"]).days
        cash            += shares * exit_p - commission_exit
        closed.append({
            "ticker":           ticker,
            "entry_date":       pos["entry_date"],
            "exit_date":        final_d,
            "entry_price":      pos["entry_price"],
            "limit_price":      pos["limit_price"],
            "exit_price":       exit_p,
            "shares":           shares,
            "valor_compra":     pos["valor_compra"],
            "commission_entry": pos["commission_entry"],
            "commission_exit":  commission_exit,
            "gross_pnl":        gross,
            "net_pnl":          net,
            "return_pct":       ret_pct,
            "dias":             days,
            "reason":           "END",
            "connors_rsi_entrada": pos["connors_rsi_entrada"],
            "rsi3_entrada":     pos["rsi3_entrada"],
            "streak_entrada":   pos["streak_entrada"],
        })
        del positions[ticker]

    capital_final = cash

    # ── Diagnóstico del backtest ────────────────────────────────────────────
    console.print()
    diag = Text()
    diag.append(f"  Sesiones procesadas        : {len(all_dates)}\n", style="white")
    diag.append(f"  Sesiones con mercado bajista (SPY<SMA200): {n_market_off}\n", style="white")
    diag.append(
        f"  Señales generadas (suma)   : {n_signals_total}\n", style="white"
    )
    diag.append(
        f"  Entradas ejecutadas        : {n_entries}\n", style="bold green"
    )
    diag.append(
        f"  Salidas SL / TSTO / CRSI   : {n_exits_sl} / {n_exits_tsto} / {n_exits_crsi}\n",
        style="white",
    )
    diag.append(f"  Total trades cerrados      : {len(closed)}\n", style="bold white")
    diag.append("\n  Rechazos en bucle de entrada (totales acumulados):\n", style="dim")
    diag.append(
        f"    · Close ≤ SMA200          : {rejected['sma']:>8}\n", style="dim",
    )
    diag.append(
        f"    · CRSI ≥ {config.CONNORS_ENTRY_CRSI:<3}              : "
        f"{rejected['crsi']:>8}\n",
        style="dim",
    )
    diag.append(
        f"    · No es mínimo de 3 días  : {rejected['low3']:>8}\n", style="dim",
    )
    console.print(Panel(
        diag,
        title="[bold cyan]Diagnóstico backtest[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))

    _print_backtest_report(
        start_date, end_date, initial_capital, capital_final, closed, len(all_dates),
    )
    csv = _export_backtest_csv(closed, start_date, end_date) if export else None
    return {"trades": closed, "capital_final": capital_final, "csv": csv}


def get_capital_summary_connors() -> dict:
    """
    Resumen actual del capital ConnorsRSI.

    Returns:
        dict con capital_inicial / capital_actual / capital_comprometido /
        cash_libre / n_posiciones / rentabilidad_pct.
    """
    _init_db()
    positions = _get_open_positions()

    with _get_connection() as con:
        row = con.execute(
            "SELECT cash FROM connors_capital ORDER BY id ASC LIMIT 1"
        ).fetchone()
    capital_inicial = float(row["cash"]) if row else float(config.CONNORS_CAPITAL)

    valor_actual         = 0.0
    capital_comprometido = 0.0
    for ticker, pos in positions.items():
        try:
            cur_p = _last_close(ticker) or pos["entry_price"]
        except Exception:
            cur_p = pos["entry_price"]
        valor_actual         += pos["shares"] * cur_p
        capital_comprometido += pos["valor_compra"]

    cash_sqlite     = _get_current_cash()
    capital_actual  = cash_sqlite + valor_actual
    cash_libre      = capital_actual - capital_comprometido
    rent_pct        = (
        (capital_actual - capital_inicial) / capital_inicial * 100
    ) if capital_inicial else 0.0

    summary = {
        "capital_inicial":      capital_inicial,
        "capital_actual":       capital_actual,
        "capital_comprometido": capital_comprometido,
        "cash_libre":           cash_libre,
        "n_posiciones":         len(positions),
        "rentabilidad_pct":     rent_pct,
    }

    # Capital adicional de Alpaca si la integración está activa
    if getattr(config, "ALPACA_ENABLED", False):
        try:
            from modules.alpaca_broker import get_broker
            broker = get_broker()
            acc = broker.get_account()
            summary["capital_alpaca"]      = acc["equity"]
            summary["rentabilidad_alpaca"] = (
                (acc["equity"] - capital_inicial) / capital_inicial * 100
                if capital_inicial else 0.0
            )
        except Exception as exc:
            console.print(f"[yellow]⚠ No se pudo leer Alpaca: {exc}[/yellow]")

    return summary



def _print_backtest_report(
    start_date: str, end_date: str,
    initial: float, final: float, trades: list, total_days: int,
) -> None:
    rent      = (final - initial) / initial * 100 if initial else 0.0
    pnl_total = sum(t["net_pnl"] for t in trades)
    n_ops     = len(trades)
    n_wins    = sum(1 for t in trades if t["net_pnl"] > 0)
    wr        = n_wins / n_ops * 100 if n_ops else 0.0
    best      = max(trades, key=lambda t: t["net_pnl"]) if trades else None
    worst     = min(trades, key=lambda t: t["net_pnl"]) if trades else None
    avg_dias  = (sum(t["dias"] for t in trades) / n_ops) if n_ops else 0.0

    rent_color = "green" if rent      >= 0 else "red"
    pnl_color  = "green" if pnl_total >= 0 else "red"

    txt = Text()
    txt.append(f"  Período               : {start_date}  →  {end_date}\n", style="dim")
    txt.append(f"  Capital inicial       : {initial:>12,.2f} €\n", style="white")
    txt.append(f"  Capital final         : {final:>12,.2f} €\n", style="white")
    txt.append(f"  Rentabilidad          : ", style="white")
    txt.append(f"{rent:+.2f} %\n", style=f"bold {rent_color}")
    txt.append(f"  PnL total             : ", style="white")
    txt.append(f"{pnl_total:+,.2f} €\n", style=f"bold {pnl_color}")
    txt.append(f"  Operaciones           : {n_ops}  ({n_wins} ganadoras)\n", style="white")
    txt.append(f"  Win Rate              : {wr:.1f} %\n", style="white")
    txt.append(f"  Días medios abierta   : {avg_dias:.1f}\n", style="white")
    if best:
        txt.append(
            f"  Mejor operación       : {best['ticker']}  {best['net_pnl']:+,.2f} €\n",
            style="green",
        )
    if worst:
        txt.append(
            f"  Peor operación        : {worst['ticker']}  {worst['net_pnl']:+,.2f} €\n",
            style="red",
        )
    console.print()
    console.print(Panel(
        txt,
        title="[bold cyan]RESUMEN BACKTEST CONNORSRSI[/bold cyan]",
        border_style="cyan", padding=(1, 2),
    ))


_TRADE_COLUMNS = [
    "ticker", "entry_date", "exit_date", "entry_price", "limit_price",
    "exit_price", "shares", "valor_compra", "commission_entry",
    "commission_exit", "gross_pnl", "net_pnl", "return_pct", "dias", "reason",
    "connors_rsi_entrada", "rsi3_entrada", "streak_entrada",
]


def _export_backtest_csv(trades: list, start_date: str, end_date: str) -> Path | None:
    """
    Exporta SIEMPRE un CSV (incluso con 0 trades), para que el usuario sepa
    que el backtest se ejecutó. Si no hay operaciones, escribe sólo la cabecera.
    """
    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_s = start_date.replace("-", "")
    safe_e = end_date.replace("-", "")
    path   = _EXPORTS_DIR / f"connors_{safe_s}_{safe_e}.csv"

    if not trades:
        pd.DataFrame(columns=_TRADE_COLUMNS).to_csv(
            path, index=False, encoding="utf-8-sig",
        )
        console.print(
            f"\n[yellow]⚠ Backtest sin operaciones · CSV vacío exportado: "
            f"{path}[/yellow]"
        )
        return path

    pd.DataFrame(trades).to_csv(path, index=False, encoding="utf-8-sig")
    console.print(f"\n[green]✓ Operaciones ConnorsRSI exportadas: {path}[/green]")
    return path


def opcion_backtest() -> None:
    end = config.DEFAULT_END_DATE or date.today().strftime("%Y-%m-%d")
    start = Prompt.ask(
        "  Fecha inicio (YYYY-MM-DD)", default=config.DEFAULT_START_DATE,
    ).strip()
    end_ask = Prompt.ask(
        "  Fecha fin    (YYYY-MM-DD)", default=end,
    ).strip()
    try:
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end_ask, "%Y-%m-%d")
    except ValueError:
        console.print("[red]Formato de fecha inválido.[/red]")
        return
    run_connors_backtest(start_date=start, end_date=end_ask)


# ── Opción F — Historial ─────────────────────────────────────────────────────

def opcion_historial() -> None:
    _init_db()
    with _get_connection() as con:
        df = pd.read_sql_query(
            "SELECT * FROM connors_operaciones WHERE estado = 'cerrada' "
            "ORDER BY id DESC LIMIT 50", con,
        )
    if df.empty:
        console.print("[yellow]Sin operaciones registradas todavía.[/yellow]")
        return
    table = Table(
        title=f"[bold cyan]Historial ConnorsRSI · últimas {len(df)} operaciones"
              "[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    for col in (
        "Ticker", "Entrada", "Salida", "P.Ent", "P.Sal",
        "PnL €", "Ret %", "Días", "CRSI ent.", "Motivo",
    ):
        table.add_column(col, justify="center")
    for _, r in df.iterrows():
        pnl = r.get("net_pnl")
        ret = r.get("return_pct")
        pc  = "green" if (pnl or 0) >= 0 else "red"
        table.add_row(
            r["ticker"],
            str(r["entry_date"])[:10],
            str(r["exit_date"])[:10] if pd.notna(r.get("exit_date")) else "—",
            f"{r['entry_price']:.2f}",
            f"{r['exit_price']:.2f}" if pd.notna(r.get("exit_price")) else "—",
            f"[{pc}]{pnl:+,.2f}[/{pc}]" if pd.notna(pnl) else "—",
            f"[{pc}]{ret*100:+.1f}%[/{pc}]" if pd.notna(ret) else "—",
            str(int(r["dias"])) if pd.notna(r.get("dias")) else "—",
            f"{r['connors_rsi_entrada']:.1f}" if pd.notna(r.get("connors_rsi_entrada")) else "—",
            r["reason"] or "—",
        )
    console.print(table)


# ── Opción G — Resumen ────────────────────────────────────────────────────────

def opcion_resumen() -> None:
    _init_db()
    with _get_connection() as con:
        trades_df = pd.read_sql_query(
            "SELECT * FROM connors_operaciones WHERE estado = 'cerrada'", con,
        )
        first_cap = con.execute(
            "SELECT cash, fecha FROM connors_capital ORDER BY id ASC LIMIT 1"
        ).fetchone()
    initial_cap = float(first_cap["cash"]) if first_cap else config.CONNORS_CAPITAL
    start_date  = first_cap["fecha"] if first_cap else date.today().isoformat()

    positions = _get_open_positions()
    cash      = _get_current_cash()

    if not yf_cache.session_data and positions:
        _download_universe()
    val_pos = sum(
        p["shares"] * (_last_close(t) or p["entry_price"])
        for t, p in positions.items()
    )
    total_cap = cash + val_pos
    rent_pct  = ((total_cap - initial_cap) / initial_cap * 100) if initial_cap else 0.0

    pnl_real = float(trades_df["net_pnl"].sum()) if not trades_df.empty else 0.0
    n_ops    = len(trades_df)
    n_wins   = int((trades_df["net_pnl"] > 0).sum()) if not trades_df.empty else 0
    wr       = n_wins / n_ops * 100 if n_ops else 0.0
    rent_col = "green" if rent_pct >= 0 else "red"

    txt = Text()
    txt.append(f"  Fecha inicio          : {start_date}\n", style="dim")
    txt.append(f"  Capital inicial       : {initial_cap:>12,.2f} €\n", style="white")
    txt.append(f"  Cash libre            : {cash:>12,.2f} €\n", style="white")
    txt.append(f"  Valor posiciones      : {val_pos:>12,.2f} €\n", style="white")
    txt.append(f"  Capital total actual  : {total_cap:>12,.2f} €\n", style="bold white")
    txt.append(f"  Rentabilidad          : ", style="white")
    txt.append(f"{rent_pct:+.2f} %\n", style=f"bold {rent_col}")
    txt.append(f"  PnL realizado         : {pnl_real:+,.2f} €\n", style="white")
    txt.append(f"  Operaciones cerradas  : {n_ops}  ({n_wins} ganadoras)\n", style="white")
    txt.append(f"  Win Rate              : {wr:.1f} %\n", style="white")
    txt.append(f"  Posiciones abiertas   : {len(positions)} / {config.CONNORS_MAX_POS}\n", style="white")
    console.print(Panel(
        txt,
        title="[bold cyan]Resumen ConnorsRSI[/bold cyan]",
        border_style="cyan", padding=(1, 2),
    ))


# ── Opción H — Exportar CSV ───────────────────────────────────────────────────

def opcion_exportar_csv() -> None:
    _init_db()
    _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    sources = [
        ("connors_operaciones", f"connors_operaciones_{today_str}.csv"),
        ("connors_capital",     f"connors_capital_{today_str}.csv"),
    ]
    exported = 0
    with _get_connection() as con:
        for tbl, fname in sources:
            df = pd.read_sql_query(f"SELECT * FROM {tbl}", con)
            if df.empty:
                continue
            path = _EXPORTS_DIR / fname
            df.to_csv(path, index=False, encoding="utf-8-sig")
            console.print(f"[green]✓ Exportado: {path}[/green]")
            exported += 1
    if exported == 0:
        console.print("[yellow]No hay datos para exportar.[/yellow]")


# ── Submenú A-H ───────────────────────────────────────────────────────────────

_OPTIONS = {
    "A": ("Ver señales de entrada hoy",          opcion_senales_hoy),
    "B": ("Ver posiciones abiertas",             opcion_posiciones_abiertas),
    "C": ("Registrar entrada (confirmar señal)", opcion_registrar_entrada),
    "D": ("Registrar salida",                    opcion_registrar_salida),
    "E": ("Ejecutar backtest",                   opcion_backtest),
    "F": ("Ver historial de operaciones",        opcion_historial),
    "G": ("Ver resumen de rentabilidad",         opcion_resumen),
    "H": ("Exportar a CSV",                      opcion_exportar_csv),
}


def menu() -> None:
    """Submenú principal ConnorsRSI (A-H + 0 para salir)."""
    yf_cache.clear_session()
    _init_db()
    while True:
        body = Text()
        for k, (label, _) in _OPTIONS.items():
            body.append(f"  {k}", style="bold cyan")
            body.append(f". {label}\n", style="white")
        body.append("  0", style="bold red")
        body.append(". Volver al menú principal\n", style="white")
        console.print()
        console.print(Panel(
            body,
            title="[bold white]CONNORSRSI · CORTO PLAZO[/bold white]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 4),
        ))
        raw_choice = Prompt.ask(
            "[bold cyan]Opción[/bold cyan]", default="0",
        ).strip().upper()
        if raw_choice == "0":
            break
        handler = _OPTIONS.get(raw_choice)
        if handler is None:
            console.print("[red]Opción inválida.[/red]")
            continue
        try:
            handler[1]()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelado por el usuario.[/yellow]")
        except Exception as exc:
            console.print(f"\n[red]✗ Error inesperado: {exc}[/red]")
        console.print()
        Prompt.ask("[dim]Pulsa Enter para continuar[/dim]", default="")

# TODO v2: simulación de apalancamiento con margin calls
# reales, capital running y slots dinámicos correctos.
