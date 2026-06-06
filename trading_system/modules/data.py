# -*- coding: utf-8 -*-
"""Módulo 1 — Obtención de datos de mercado históricos vía yfinance."""

import sys
import urllib.request
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import yfinance as yf
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from modules import yf_cache

console = Console()
_CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"

_SP500_HISTORY_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes.csv"
)
_SP500_HISTORY_CACHE = (
    Path(__file__).parent.parent / "data" / "sp500_historical_components.csv"
)
_SP500_HISTORY_MAX_AGE_DAYS = 7

# Caché en memoria del universo (clave = ruta absoluta del CSV)
_UNIVERSE_CACHE: dict[str, list[str]] = {}


def es_dia_habil_nyse(fecha=None) -> bool:
    """
    Devuelve True si la fecha dada (o hoy si None) es día hábil NYSE.
    Fallback conservador: True si pandas_market_calendars no está disponible
    o lanza cualquier excepción (mejor operar que perderse una señal).

    Params:
        fecha: date | str | None. Si None usa date.today().
    Returns:
        True si es día hábil de mercado, False si es festivo o fin de semana.
    """
    if fecha is None:
        fecha = date.today()
    if isinstance(fecha, str):
        fecha = date.fromisoformat(fecha)
    try:
        import pandas_market_calendars as mcal
        nyse     = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=fecha.isoformat(),
            end_date=fecha.isoformat(),
        )
        return not schedule.empty
    except Exception:
        return True


def load_universe(filepath: str | Path | None = None) -> list[str]:
    """
    Carga la lista de tickers activos desde un CSV externo.

    El CSV debe tener columnas `ticker, sector, activo`. Solo se devuelven los
    tickers con `activo == 1`. El resultado se cachea en memoria por ruta para
    no releer el fichero en cada llamada.

    Params:
        filepath: Ruta al CSV. Por defecto, `config.UNIVERSE_FILE` resuelto
                  relativo a la raíz del proyecto.

    Returns:
        Lista de tickers (str). Lista vacía si el fichero no existe o no se
        puede parsear; en ese caso se imprime una advertencia.
    """
    if filepath is None:
        path = Path(__file__).parent.parent / config.UNIVERSE_FILE
    else:
        path = Path(filepath)

    key = str(path.resolve()) if path.exists() else str(path)
    if key in _UNIVERSE_CACHE:
        return list(_UNIVERSE_CACHE[key])

    if not path.exists():
        console.print(f"[yellow]⚠ Universo no encontrado: {path}[/yellow]")
        _UNIVERSE_CACHE[key] = []
        return []

    try:
        df = pd.read_csv(path)
        for col in ("ticker", "activo"):
            if col not in df.columns:
                raise ValueError(f"columna '{col}' ausente en {path.name}")
        activos = df.loc[df["activo"] == 1, "ticker"].dropna().astype(str).tolist()
        # Sin duplicados, conservando orden
        seen: set = set()
        tickers: list = []
        for t in activos:
            if t not in seen:
                seen.add(t)
                tickers.append(t)
        _UNIVERSE_CACHE[key] = tickers
        return list(tickers)
    except Exception as exc:
        console.print(f"[red]✗ Error cargando universo {path}: {exc}[/red]")
        _UNIVERSE_CACHE[key] = []
        return []


def validate_universe(
    tickers: list | None = None,
    chunk_size: int = 50,
    days_back: int = 30,
) -> dict:
    """
    Verifica con yfinance que cada ticker del universo tiene datos recientes.

    Descarga en lotes (yfinance acepta múltiples tickers por llamada) y muestra
    una barra de progreso Rich. Al terminar imprime una tabla resumen con
    válidos / inválidos y una lista de los tickers sin datos para que el
    usuario pueda marcarlos como `activo=0` en el CSV.

    Params:
        tickers:    Universo a validar. Por defecto, `load_universe()`.
        chunk_size: Tickers por petición batch a yfinance (default: 50).
        days_back:  Días hacia atrás a comprobar (default: 30).

    Returns:
        dict con {"validos": list, "invalidos": list, "n_total": int}.
    """
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TextColumn, TimeElapsedColumn,
    )
    from rich.table import Table
    from rich import box

    if tickers is None:
        tickers = load_universe()
    if not tickers:
        console.print("[yellow]Universo vacío; nada que validar.[/yellow]")
        return {"validos": [], "invalidos": [], "n_total": 0}

    end_str   = date.today().strftime("%Y-%m-%d")
    from datetime import timedelta
    start_str = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    n_total   = len(tickers)
    validos:   list = []
    invalidos: list = []

    console.print(
        f"\n[bold cyan]Validando universo ({n_total} tickers) "
        f"contra yfinance — ventana {start_str} → {end_str}[/bold cyan]\n"
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Descargando", total=n_total)

        for i in range(0, n_total, chunk_size):
            chunk = tickers[i:i + chunk_size]
            progress.update(task, description=f"Lote {i // chunk_size + 1}")
            try:
                df = yf.download(
                    chunk,
                    start=start_str, end=end_str,
                    auto_adjust=True, progress=False,
                    group_by="ticker", threads=True,
                )
            except Exception:
                invalidos.extend(chunk)
                progress.advance(task, advance=len(chunk))
                continue

            if df is None or df.empty:
                invalidos.extend(chunk)
                progress.advance(task, advance=len(chunk))
                continue

            for t in chunk:
                try:
                    if isinstance(df.columns, pd.MultiIndex):
                        if t in df.columns.get_level_values(0):
                            sub = df[t].dropna(how="all")
                        else:
                            invalidos.append(t)
                            continue
                    else:
                        sub = df.dropna(how="all")
                    if not sub.empty and len(sub) >= 3:
                        validos.append(t)
                    else:
                        invalidos.append(t)
                except Exception:
                    invalidos.append(t)
            progress.advance(task, advance=len(chunk))

    # Tabla resumen
    table = Table(
        title="[bold cyan]Validación del universo[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Estado",  justify="left")
    table.add_column("N",       justify="right")
    table.add_column("%",       justify="right")
    table.add_row(
        "[green]✓ Válidos[/green]",
        str(len(validos)),
        f"{len(validos) / n_total * 100:.1f} %",
    )
    table.add_row(
        "[red]⚠ Sin datos[/red]",
        str(len(invalidos)),
        f"{len(invalidos) / n_total * 100:.1f} %",
    )
    table.add_row("[bold]Total[/bold]", str(n_total), "100.0 %")
    console.print(table)

    if invalidos:
        console.print("\n[yellow]Tickers sin datos en yfinance:[/yellow]")
        for j in range(0, len(invalidos), 10):
            console.print("  " + ", ".join(invalidos[j:j + 10]))
        console.print(
            "\n[dim]Sugerencia: marca `activo=0` en sp500_universe.csv para "
            "excluirlos del universo activo.[/dim]"
        )
    else:
        console.print("\n[green]Todos los tickers tienen datos.[/green]")

    return {"validos": validos, "invalidos": invalidos, "n_total": n_total}


def get_sp500_historical_tickers(fecha: str) -> list:
    """
    Devuelve la lista de tickers que eran componentes del SP500 en `fecha`
    (formato YYYY-MM-DD), usando el CSV histórico de fja05680/sp500.

    Estrategia:
        1. Si el caché local `data/sp500_historical_components.csv` existe y
           tiene < 7 días → se reutiliza sin descargar.
        2. Si no, intenta descargar el CSV original.
        3. Si la descarga falla y NO hay caché local → fallback a
           `load_universe()` con advertencia en amarillo.
        4. Sobre el CSV (formato `date,tickers` con tickers separados por
           comas) busca la fila más cercana ANTERIOR a `fecha`.
    """
    _SP500_HISTORY_CACHE.parent.mkdir(parents=True, exist_ok=True)

    needs_download = True
    if _SP500_HISTORY_CACHE.exists():
        age_days = (
            datetime.now() - datetime.fromtimestamp(_SP500_HISTORY_CACHE.stat().st_mtime)
        ).days
        needs_download = age_days > _SP500_HISTORY_MAX_AGE_DAYS

    if needs_download:
        try:
            console.print(
                "[cyan]↓ Descargando componentes históricos SP500 (fja05680)...[/cyan]"
            )
            urllib.request.urlretrieve(_SP500_HISTORY_URL, _SP500_HISTORY_CACHE)
            console.print(f"[green]✓ Caché actualizada: {_SP500_HISTORY_CACHE}[/green]")
        except Exception as exc:
            console.print(
                f"[yellow]⚠ No se pudo descargar histórico SP500: {exc}[/yellow]"
            )
            if not _SP500_HISTORY_CACHE.exists():
                console.print(
                    "[yellow]Usando universo actual de load_universe()[/yellow]"
                )
                return load_universe()

    try:
        df = pd.read_csv(_SP500_HISTORY_CACHE)
        date_col    = next((c for c in df.columns if "date" in c.lower()), df.columns[0])
        tickers_col = next(
            (c for c in df.columns if "ticker" in c.lower()),
            df.columns[1] if len(df.columns) > 1 else df.columns[0],
        )
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col]).sort_values(date_col)

        target = pd.Timestamp(fecha)
        sub    = df[df[date_col] <= target]
        if sub.empty:
            sub = df.head(1)
        tickers_str = str(sub.iloc[-1][tickers_col])
        tickers = [t.strip() for t in tickers_str.split(",") if t.strip()]
        if not tickers:
            raise ValueError("lista de tickers vacía tras parsing")
        return tickers
    except Exception as exc:
        console.print(
            f"[yellow]⚠ Error parseando CSV histórico SP500: {exc}[/yellow]"
        )
        console.print(
            "[yellow]Usando universo actual de load_universe()[/yellow]"
        )
        return load_universe()


def get_data(
    tickers: list,
    start_date: str = None,
    end_date: str = None,
    use_cache: bool = True,
) -> dict:
    """
    Descarga datos OHLCV para una lista de tickers usando yfinance.

    Params:
        tickers:    Lista de símbolos bursátiles (p. ej. ['SAN.MC', 'AAPL']).
        start_date: Fecha inicio 'YYYY-MM-DD'. Default: config.DEFAULT_START_DATE.
        end_date:   Fecha fin 'YYYY-MM-DD'. Default: hoy.
        use_cache:  Si True, guarda/lee caché local (parquet si pyarrow está
                    instalado; CSV en caso contrario) para evitar re-descargas
                    el mismo día.

    Returns:
        dict {ticker: pd.DataFrame} con columnas Open, High, Low, Close, Volume.
    """
    if start_date is None:
        start_date = config.DEFAULT_START_DATE
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    today_str = date.today().strftime("%Y%m%d")
    safe_start = start_date.replace("-", "")
    safe_end = end_date.replace("-", "")

    results: dict = {}

    ext = "parquet" if yf_cache.DISK_FMT == "parquet" else "csv"

    for ticker in tickers:
        try:
            safe_ticker = ticker.replace(".", "_")
            cache_file = _CACHE_DIR / (
                f"{safe_ticker}_{today_str}_{safe_start}_{safe_end}.{ext}"
            )

            if use_cache and cache_file.exists():
                console.print(f"  [dim]↩ {ticker} desde caché[/dim]")
                df = yf_cache.load_from_disk(cache_file)
            else:
                console.print(f"  [cyan]↓ Descargando {ticker}...[/cyan]")
                raw = yf.download(
                    ticker,
                    start=start_date,
                    end=end_date,
                    progress=False,
                    auto_adjust=True,
                )

                if raw.empty:
                    console.print(f"  [yellow]⚠ {ticker}: sin datos disponibles[/yellow]")
                    continue

                # Aplanar MultiIndex (yfinance >=0.2.x devuelve MultiIndex para un ticker)
                if isinstance(raw.columns, pd.MultiIndex):
                    raw.columns = raw.columns.get_level_values(0)
                    raw = raw.loc[:, ~raw.columns.duplicated()]

                needed = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
                if len(needed) < 5:
                    console.print(f"  [yellow]⚠ {ticker}: columnas inesperadas {raw.columns.tolist()}[/yellow]")
                    continue

                df = raw[["Open", "High", "Low", "Close", "Volume"]].copy()

                if use_cache:
                    yf_cache.save_to_disk(df, cache_file)

            if len(df) < 60:
                console.print(
                    f"  [yellow]⚠ {ticker}: datos insuficientes ({len(df)} días, mínimo 60)[/yellow]"
                )
                continue

            results[ticker] = df
            console.print(
                f"  [green]✓ {ticker}: {len(df)} registros "
                f"({df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')})[/green]"
            )

        except Exception as exc:
            console.print(f"  [red]✗ {ticker}: error al obtener datos — {exc}[/red]")

    return results
