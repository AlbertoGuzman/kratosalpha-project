# -*- coding: utf-8 -*-
"""
Caché yfinance usada por connors_rsi.py.

Proporciona detección de formato en disco (parquet > csv), caché de disco por
día (data/cache/YYYYMMDD/), caché de sesión en memoria (session_data), y
funciones de descarga OHLCV normalizadas.

API pública:
    DISK_FMT, MIN_CACHE_ROWS
    today_cache_dir(), cleanup_old_caches()
    safe_ticker_filename(), disk_cache_file()
    save_to_disk(), load_from_disk(), load_ticker()
    yf_download_one()
    session_data          ← dict ticker → DataFrame (acceso directo)
    get_session(t)        ← session_data.get(t)
    set_session(t, df)    ← session_data[t] = df
    clear_session()       ← limpia in-place (session_data.clear())
"""

import shutil
import sys
import time
from pathlib import Path
from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
from modules.logger import get_system_logger


def es_dia_habil_nyse(fecha=None) -> bool:
    """Wrapper lazy de data.es_dia_habil_nyse — evita importación circular."""
    from modules.data import es_dia_habil_nyse as _fn
    return _fn(fecha)

console = Console()
_slog = get_system_logger()

_DISK_CACHE_ROOT = Path(__file__).parent.parent / "data" / "cache"
MIN_CACHE_ROWS   = 200

# Detectar engine de caché en disco: parquet si hay pyarrow/fastparquet, CSV si no.
try:
    import pyarrow  # noqa: F401
    DISK_FMT = "parquet"
except ImportError:
    try:
        import fastparquet  # noqa: F401
        DISK_FMT = "parquet"
    except ImportError:
        DISK_FMT = "csv"

# Caché de sesión compartida entre estrategias: {ticker: DataFrame}
session_data: dict = {}

# Fecha de referencia para anclar la espera del cierre yfinance al día de
# lanzamiento del run (no a date.today()). El autopiloto/headless la fija al
# arrancar; si la ejecución cruza la medianoche durante los reintentos de
# yfinance, `wait_for_last_close` sigue apuntando a la sesión de ese día y no a
# la del nuevo "hoy" (que aún no ha cerrado). None → comportamiento por defecto
# (date.today()), que es lo que usa el modo interactivo.
_CLOSE_WAIT_REF = None


def set_close_wait_ref(fecha) -> None:
    """
    Fija (o limpia) la fecha de referencia para `wait_for_last_close`.

    Params:
        fecha: `datetime.date` con el día de lanzamiento del run, o None para
            restaurar el comportamiento por defecto (anclar a date.today()).
    Returns:
        None
    """
    global _CLOSE_WAIT_REF
    _CLOSE_WAIT_REF = fecha


# Si está activo, `wait_for_last_close` devuelve True de inmediato sin sondear:
# se opera con los últimos datos disponibles. Lo activa el modo interactivo
# (menú `main()`), donde hay un humano que no debe quedar bloqueado hasta 4 h
# esperando el cierre del día. El modo headless (--run, Task Scheduler) lo deja
# en False para que el cron sí espere a que yfinance publique la sesión.
_SKIP_CLOSE_WAIT = False


def set_skip_close_wait(skip: bool) -> None:
    """
    Activa/desactiva el salto de la espera del cierre en `wait_for_last_close`.

    Params:
        skip: True para no esperar (operar con los últimos datos disponibles);
            False (por defecto) para mantener el sondeo de reintentos.
    Returns:
        None
    """
    global _SKIP_CLOSE_WAIT
    _SKIP_CLOSE_WAIT = skip


# ── API de sesión ─────────────────────────────────────────────────────────────

def get_session(ticker: str) -> pd.DataFrame | None:
    """Devuelve el DataFrame del ticker en sesión, o None si no está."""
    return session_data.get(ticker)


def set_session(ticker: str, df: pd.DataFrame) -> None:
    """Almacena df en el caché de sesión."""
    session_data[ticker] = df


def clear_session() -> None:
    """Limpia el caché de sesión en memoria (in-place, preserva la referencia)."""
    session_data.clear()


# ── Caché en disco ────────────────────────────────────────────────────────────

def today_cache_dir() -> Path:
    """Carpeta de caché del día actual: data/cache/YYYYMMDD/."""
    return _DISK_CACHE_ROOT / date.today().strftime("%Y%m%d")


def cleanup_old_caches() -> None:
    """
    Elimina caché de días anteriores en data/cache/:
      - Subcarpetas YYYYMMDD/ antiguas (usadas por yf_cache.load_ticker).
      - Ficheros planos sin la fecha de hoy en el nombre (usados por data.get_data).
    """
    if not _DISK_CACHE_ROOT.exists():
        return
    today_str = date.today().strftime("%Y%m%d")
    for child in _DISK_CACHE_ROOT.iterdir():
        if child.is_dir():
            # Subcarpetas YYYYMMDD/ antiguas
            if len(child.name) == 8 and child.name.isdigit() and child.name != today_str:
                try:
                    shutil.rmtree(child)
                except Exception:
                    pass
        elif child.is_file() and today_str not in child.name:
            # Ficheros planos de días anteriores (ticker_YYYYMMDD_start_end.ext)
            try:
                child.unlink()
            except Exception:
                pass


def safe_ticker_filename(ticker: str) -> str:
    """Convierte el ticker a nombre de fichero seguro (reemplaza /, \\, :)."""
    return ticker.replace("/", "_").replace("\\", "_").replace(":", "_")


def disk_cache_file(ticker: str) -> Path:
    """Ruta completa del fichero de caché para ticker (parquet o csv)."""
    ext = "parquet" if DISK_FMT == "parquet" else "csv"
    return today_cache_dir() / f"{safe_ticker_filename(ticker)}.{ext}"


def save_to_disk(df: pd.DataFrame, path: Path) -> None:
    """
    Guarda df en disco en el formato detectado (DISK_FMT).

    Params:
        df:   DataFrame OHLCV a guardar.
        path: Ruta destino (se crea el directorio si no existe).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if DISK_FMT == "parquet":
        df.to_parquet(path)
    else:
        df.to_csv(path)


def load_from_disk(path: Path) -> pd.DataFrame:
    """
    Lee un fichero de caché de disco en el formato detectado (DISK_FMT).

    Params:
        path: Ruta al fichero (parquet o csv).
    Returns:
        DataFrame leído.
    """
    if DISK_FMT == "parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, index_col=0, parse_dates=True)


def load_ticker(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """
    Carga un ticker con caché en disco por día (data/cache/YYYYMMDD/).

    Si el caché existe pero no cubre el rango [start, end] pedido (p.ej. al
    pasar de una ventana corta a una histórica amplia), lo invalida y redescarga.
    Tolera 14 días de desfase por festivos y fines de semana.

    Params:
        ticker: Símbolo del activo.
        start:  Fecha inicio YYYY-MM-DD.
        end:    Fecha fin YYYY-MM-DD.
    Returns:
        DataFrame OHLCV o None si no hay datos descargables.
    """
    cache_file = disk_cache_file(ticker)
    start_ts   = pd.Timestamp(start)
    end_ts     = pd.Timestamp(end)
    tol        = pd.Timedelta(days=14)

    if cache_file.exists():
        try:
            df = load_from_disk(cache_file)
            if not df.empty:
                covers = (df.index[0] <= start_ts + tol) and (df.index[-1] >= end_ts - tol)
                if covers:
                    console.print(
                        f"  [green]✓ {ticker}: desde caché disco ({len(df)} registros)[/green]"
                    )
                    _slog.debug("caché disco: %s  filas=%d", ticker, len(df))
                    return df
                console.print(
                    f"  [yellow]⟳ {ticker}: caché no cubre [{start}…{end}] "
                    f"(disco va {df.index[0].date()}…{df.index[-1].date()}), "
                    f"redescargando[/yellow]"
                )
        except Exception as exc:
            console.print(
                f"  [yellow]⚠ {ticker}: caché disco corrupta ({exc}), redescargando[/yellow]"
            )

    console.print(f"  [cyan]↓ {ticker}: descargando {start}…{end}...[/cyan]", end="")
    df = yf_download_one(ticker, start, end)
    if df is None or df.empty:
        console.print("  [red]✗ sin datos[/red]")
        _slog.warning("yfinance sin datos: %s [%s → %s]", ticker, start, end)
        return None
    try:
        save_to_disk(df, cache_file)
    except Exception as exc:
        console.print(f"  [yellow]⚠ caché no guardada: {exc}[/yellow]")
        _slog.warning("caché no guardada %s: %s", ticker, exc)
    console.print(f"  [green]✓ ({len(df)} registros)[/green]")
    _slog.debug("descarga yfinance: %s  filas=%d  [%s → %s]", ticker, len(df), start, end)
    return df


def _ultima_sesion_esperada() -> date:
    """
    Fecha de la última sesión NYSE que ya debería estar cerrada y publicada.

    El autopiloto se lanza tras el cierre (noche/madrugada en España), así que la
    sesión más reciente es la del último día hábil NYSE **anterior a hoy**.
    yfinance suele publicar esa sesión con retardo (a veces aún no está pasada la
    medianoche), por eso es la que exigimos como mínimo — NO la de "hoy", cuyo
    cierre puede no existir todavía.

    Returns:
        date de la última sesión hábil NYSE estrictamente anterior a hoy.
    """
    d = date.today() - timedelta(days=1)
    # Retrocede hasta el día hábil NYSE más reciente (tope de seguridad: 10 días).
    for _ in range(10):
        if es_dia_habil_nyse(d):
            return d
        d -= timedelta(days=1)
    return d


def wait_for_last_close() -> bool:
    """
    Espera (sondeando) a que SPY tenga el cierre de la sesión NYSE objetivo,
    pensado para el modo automático que se lanza por la noche:

      · **Primeros intentos**: se comprueba con la **fecha actual** (la sesión
        que cierra hoy), porque el autopiloto arranca tras el cierre NYSE.
      · **Si durante la ejecución cambia el día** (se cruza la medianoche), se
        pasa a comprobar el cierre del **día anterior** — que es justamente la
        sesión del día en que se lanzó. El objetivo, por tanto, queda anclado a
        la fecha de lanzamiento.
      · Sondea cada `YF_RETRY_WAIT_MIN` minutos (30 por defecto) hasta
        `YF_RETRY_ATTEMPTS` veces, re-descargando SPY (invalida caché en disco).

    El ancla de la fecha de lanzamiento se toma de `_CLOSE_WAIT_REF` si está
    fijada (vía `set_close_wait_ref`, lo hacen el autopiloto y el modo headless);
    si es None se usa `date.today()`. Esto evita que, cuando el autopiloto se
    lanza de noche y la espera cruza la medianoche, `fecha_inicio` capture ya el
    día siguiente y pasemos a exigir una sesión que aún no ha cerrado.

    Solo reintenta si ALPACA_ENABLED=True (descarta DEV y backtests); en DEV
    devuelve True sin esperar para no bloquear el flujo. Si `_SKIP_CLOSE_WAIT`
    está activo (lo enciende el modo interactivo vía `set_skip_close_wait`),
    devuelve True de inmediato y se opera con los últimos datos disponibles.

    Returns:
        True si SPY ya cubre la sesión objetivo (o no procede reintentar);
        False si se agotaron los reintentos sin obtenerla.
    """
    import config

    # Modo interactivo: no bloquear esperando el cierre, operar con lo disponible.
    if _SKIP_CLOSE_WAIT:
        _slog.info("wait_for_last_close: espera omitida (_SKIP_CLOSE_WAIT)")
        return True

    # No reintentar en DEV/backtest (ALPACA_ENABLED=False)
    if not getattr(config, "ALPACA_ENABLED", False):
        return True

    fecha_inicio = _CLOSE_WAIT_REF or date.today()

    def _objetivo() -> date:
        """Sesión NYSE cuyo cierre esperamos, anclada a la fecha de lanzamiento.

        Mientras seguimos en el día de lanzamiento → la fecha actual (hoy). Si la
        ejecución cruzó la medianoche → el último día hábil anterior, que coincide
        con el día de lanzamiento. (Si se lanzó en festivo/finde, cae al último
        hábil.)"""
        hoy = date.today()
        if hoy == fecha_inicio and es_dia_habil_nyse(hoy):
            return hoy
        return _ultima_sesion_esperada()

    objetivo = _objetivo()

    # ¿SPY en sesión ya cubre la sesión objetivo?
    spy = session_data.get("SPY")
    if spy is not None and not spy.empty and spy.index[-1].date() >= objetivo:
        return True

    attempts = getattr(config, "YF_RETRY_ATTEMPTS", 8)
    wait_min = getattr(config, "YF_RETRY_WAIT_MIN", 30)

    last_date = spy.index[-1].date() if spy is not None and not spy.empty else None

    for intento in range(1, attempts + 1):
        last_str = str(last_date) if last_date else "desconocida"
        _slog.warning(
            "yfinance SPY: datos hasta %s · esperando cierre de la sesión %s "
            "-- reintentando en %d min (%d/%d)",
            last_str, objetivo, wait_min, intento, attempts,
        )
        console.print(
            f"[yellow]yfinance: datos hasta {last_str} · esperando cierre de la "
            f"sesión {objetivo} — reintentando en {wait_min} min "
            f"({intento}/{attempts})[/yellow]"
        )
        time.sleep(wait_min * 60)

        # Recalcular el objetivo: si la ejecución cruzó la medianoche, pasamos a
        # comprobar el cierre del día anterior (el de lanzamiento).
        nuevo_objetivo = _objetivo()
        if nuevo_objetivo != objetivo:
            _slog.info(
                "Cambio de día detectado durante la ejecución: ahora se comprueba "
                "el cierre del día anterior (%s)", nuevo_objetivo,
            )
            console.print(
                f"[cyan]Cambio de día detectado — comprobando el cierre del día "
                f"anterior ({nuevo_objetivo})[/cyan]"
            )
            objetivo = nuevo_objetivo

        # Ventana de descarga recalculada con la fecha actual
        start = (date.today() - timedelta(days=15)).strftime("%Y-%m-%d")
        end   = date.today().strftime("%Y-%m-%d")

        # Invalidar caché de SPY en disco y sesión para forzar re-descarga
        spy_file = disk_cache_file("SPY")
        if spy_file.exists():
            try:
                spy_file.unlink()
            except Exception:
                pass
        session_data.pop("SPY", None)

        df = load_ticker("SPY", start, end)
        if df is not None and not df.empty:
            set_session("SPY", df)
            if df.index[-1].date() >= objetivo:
                _slog.info("yfinance SPY: datos hasta %s -- OK", df.index[-1].date())
                console.print(
                    f"[green]yfinance: datos hasta {df.index[-1].date()} — OK[/green]"
                )
                return True
            last_date = df.index[-1].date()

    _slog.error(
        "yfinance SPY: sin la sesión %s tras %d reintentos -- abortando",
        objetivo, attempts,
    )
    console.print(
        f"[red]yfinance: sin la sesión {objetivo} tras {attempts} reintentos "
        f"— abortando.[/red]"
    )
    return False


def yf_download_one(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """
    Descarga un único ticker con yf.download, aplana MultiIndex y devuelve
    sólo las columnas OHLCV. None si no hay datos utilizables.

    Params:
        ticker: Símbolo del activo.
        start:  Fecha inicio YYYY-MM-DD.
        end:    Fecha fin YYYY-MM-DD.
    Returns:
        DataFrame con columnas [Open, High, Low, Close, Volume] o None.
    """
    try:
        df = yf.download(
            ticker, start=start, end=end,
            auto_adjust=True, progress=False,
        )
    except Exception as exc:
        console.print(f"  [red]✗ {ticker}: error de yfinance — {exc}[/red]")
        _slog.error("error yfinance %s: %s", ticker, exc, exc_info=True)
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
        df = df.loc[:, ~df.columns.duplicated()]
    needed = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        console.print(
            f"  [yellow]⚠ {ticker}: columnas inesperadas {df.columns.tolist()}[/yellow]"
        )
        return None
    return df[needed].dropna()
