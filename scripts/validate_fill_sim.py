# -*- coding: utf-8 -*-
"""
Valida la simulación de fill (Open/Low de yfinance) contra órdenes limit
reales de Alpaca.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\validate_fill_sim.py --csv ordenes.csv

Formato del CSV de entrada:
    ticker,fecha,limit_price,filled,fill_price_real
    AAPL,2026-06-10,192.50,1,192.48
    MSFT,2026-06-10,415.20,0,

    - filled: 1 si se llenó, 0 si no
    - fill_price_real: precio real de Alpaca (vacío si filled=0)

Lógica de fill simulado (espejo exacto del backtest con FILL_SIM=True):
    · Low[D] > limit_price  → no fill
    · Open[D] <= limit_price → fill al Open (gap de apertura)
    · Open[D] > limit_price y Low[D] <= limit_price → fill al limit_price

Salida:
    - Tabla de discrepancias
    - Métricas: accuracy, precision, recall, error de precio
    - CSV detallado en exports/fill_sim_validation_YYYYMMDD.csv
"""

import sys
import os
import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
from rich.console import Console
from rich.table import Table
from rich import box

_ROOT = Path(__file__).resolve().parent.parent / "trading_system"
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("TRADING_ENV", "dev")

console = Console()
_EXPORTS = _ROOT / "exports"
_EXPORTS.mkdir(parents=True, exist_ok=True)


def _parse_args():
    p = argparse.ArgumentParser(
        description="Valida simulación fill (Open/Low yfinance) vs órdenes reales Alpaca"
    )
    p.add_argument("--csv", required=True, help="CSV con órdenes reales")
    return p.parse_args()


def _load_orders(csv_path: str) -> pd.DataFrame:
    """
    Carga y normaliza el CSV de órdenes reales.

    Returns:
        DataFrame con columnas [ticker, fecha, limit_price, filled, fill_price_real].
    """
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"ticker", "fecha", "limit_price", "filled"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"Columnas faltantes: {missing}")
    df["fecha"]       = pd.to_datetime(df["fecha"]).dt.date
    df["limit_price"] = df["limit_price"].astype(float)
    df["filled"]      = df["filled"].astype(int)
    if "fill_price_real" not in df.columns:
        df["fill_price_real"] = float("nan")
    else:
        df["fill_price_real"] = pd.to_numeric(df["fill_price_real"], errors="coerce")
    return df


def _download_ohlcv(tickers: list[str], fechas: list[date]) -> dict[str, pd.DataFrame]:
    """
    Descarga OHLCV diario de yfinance para los tickers y rango de fechas dados.
    Un solo batch para todos los tickers — más eficiente que llamadas individuales.

    Returns:
        {ticker: DataFrame con columnas [Open, High, Low, Close, Volume]}
    """
    min_fecha = min(fechas) - timedelta(days=5)   # margen por si hay festivos
    max_fecha = max(fechas) + timedelta(days=1)

    console.print(
        f"  Descargando OHLCV yfinance: {len(tickers)} tickers "
        f"({min_fecha} → {max_fecha})..."
    )
    raw = yf.download(
        tickers, start=str(min_fecha), end=str(max_fecha),
        auto_adjust=True, progress=False,
    )
    resultado = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for ticker in tickers:
            try:
                df = raw.xs(ticker, axis=1, level=1).dropna(how="all")
                resultado[ticker] = df
            except KeyError:
                console.print(f"  [yellow]⚠ Sin datos yfinance para {ticker}[/yellow]")
    else:
        # Un solo ticker → raw ya es el DataFrame
        ticker = tickers[0]
        resultado[ticker] = raw.dropna(how="all")
    return resultado


def _simulate_fill(
    open_d: float, low_d: float, limit_price: float
) -> tuple[float | None, str]:
    """
    Simula fill con la misma lógica que el backtest (FILL_SIM=True).

    Returns:
        (fill_price, motivo): motivo es "fill", "no_fill" o "no_data".
    """
    if low_d > limit_price:
        return None, "no_fill"
    if open_d <= limit_price:
        return open_d, "fill"   # gap de apertura
    return limit_price, "fill"  # toque durante la sesión


def _run_validation(orders: pd.DataFrame, ohlcv: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Ejecuta la simulación sobre todas las órdenes y recoge resultados."""
    filas = []
    for _, row in orders.iterrows():
        ticker      = str(row["ticker"]).strip().upper()
        fecha       = row["fecha"]
        limit_price = float(row["limit_price"])
        filled_real = int(row["filled"])
        fill_p_real = row["fill_price_real"]

        df_t = ohlcv.get(ticker)
        if df_t is None or df_t.empty:
            sim_price, motivo = None, "no_data"
        else:
            ts = pd.Timestamp(fecha)
            if ts not in df_t.index:
                sim_price, motivo = None, "no_data"
            else:
                open_d = float(df_t.loc[ts, "Open"])
                low_d  = float(df_t.loc[ts, "Low"])
                sim_price, motivo = _simulate_fill(open_d, low_d, limit_price)

        sim_fill   = 1 if motivo == "fill" else 0
        match      = (sim_fill == filled_real)
        price_err  = (
            abs(sim_price - fill_p_real)
            if (sim_fill == 1 and filled_real == 1
                and sim_price is not None
                and not pd.isna(fill_p_real))
            else float("nan")
        )

        color = "green" if match else "red"
        console.print(
            f"  {ticker} {fecha} limit={limit_price:.2f}  "
            f"[{color}]sim={motivo}  real={'fill' if filled_real else 'no_fill'}  "
            f"{'✓' if match else '✗'}[/{color}]"
        )
        filas.append({
            "ticker":          ticker,
            "fecha":           fecha,
            "limit_price":     limit_price,
            "filled_real":     filled_real,
            "fill_price_real": fill_p_real,
            "sim_fill":        sim_fill,
            "sim_price":       sim_price if sim_price is not None else float("nan"),
            "fill_reason":     motivo,
            "match":           match,
            "price_error":     price_err,
        })
    return pd.DataFrame(filas)


def _print_report(df: pd.DataFrame) -> None:
    """Imprime métricas de validación."""
    n          = len(df)
    n_fill     = df["filled_real"].sum()
    n_sim_fill = df["sim_fill"].sum()

    real_fills   = df[df["filled_real"] == 1]
    tp = real_fills["sim_fill"].sum()
    fn = len(real_fills) - tp

    real_nofills = df[df["filled_real"] == 0]
    tn = (real_nofills["sim_fill"] == 0).sum()
    fp = len(real_nofills) - tn

    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    accuracy  = (tp + tn) / n if n > 0 else float("nan")

    price_df  = df[df["price_error"].notna() & ~df["price_error"].isna()]
    n_price   = len(price_df)

    # Tabla de discrepancias
    errors = df[~df["match"]]
    if not errors.empty:
        console.print("\n[bold red]Discrepancias (sim ≠ real):[/bold red]")
        t = Table(box=box.SIMPLE)
        for col in ["Ticker", "Fecha", "Limit", "Sim", "Real", "Motivo"]:
            t.add_column(col)
        for _, r in errors.iterrows():
            t.add_row(
                r["ticker"], str(r["fecha"]), f"{r['limit_price']:.2f}",
                "fill" if r["sim_fill"] else "no_fill",
                "fill" if r["filled_real"] else "no_fill",
                r["fill_reason"],
            )
        console.print(t)

    console.print("\n[bold cyan]RESULTADOS VALIDACIÓN FILL SIM (Open/Low yfinance)[/bold cyan]")
    console.print(f"  Órdenes totales      : {n}")
    console.print(f"  Fill rate real       : {n_fill}/{n} = {n_fill/n*100:.1f}%")
    console.print(f"  Fill rate simulado   : {n_sim_fill}/{n} = {n_sim_fill/n*100:.1f}%")
    console.print(f"  Accuracy             : {accuracy*100:.1f}%  ({tp+tn}/{n} correctas)")
    console.print(f"  Precision            : {precision*100:.1f}%")
    console.print(f"  Recall               : {recall*100:.1f}%")
    console.print(f"  TP={tp}  FP={fp}  TN={tn}  FN={fn}")
    if n_price > 0:
        console.print(
            f"  Error precio fill    : media={price_df['price_error'].mean():.3f}$  "
            f"máx={price_df['price_error'].max():.3f}$  (n={n_price})"
        )


def _export(df: pd.DataFrame) -> Path:
    path = _EXPORTS / f"fill_sim_validation_{date.today().strftime('%Y%m%d')}.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    console.print(f"\n[green]✓ Resultados exportados: {path}[/green]")
    return path


def main():
    args   = _parse_args()
    orders = _load_orders(args.csv)
    console.print(
        f"\n[bold]Órdenes cargadas:[/bold] {len(orders)}  "
        f"({orders['filled'].sum()} fills · {(orders['filled']==0).sum()} no fills)\n"
    )
    tickers = orders["ticker"].str.strip().str.upper().unique().tolist()
    fechas  = orders["fecha"].tolist()
    ohlcv   = _download_ohlcv(tickers, fechas)
    console.print()
    results = _run_validation(orders, ohlcv)
    _print_report(results)
    _export(results)


if __name__ == "__main__":
    main()
