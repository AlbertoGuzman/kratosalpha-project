# -*- coding: utf-8 -*-
"""
Análisis exploratorio: ¿permite el volumen relativo elegir la profundidad
óptima de CONNORS_ENTRY_LIMIT señal a señal?

Mejoras v2:
  - Bootstrap por bloques mensuales para IC 95% en ¿Persiste?.
  - Retorno mediano por llenada junto al medio en todas las tablas.
  - Comprobación de splits yfinance (vol outliers + splits de yf.Ticker).
  - Test de monotonía del mejor ENTRY_LIMIT a través de cuartiles Q1→Q4.
  - Conteo de celdas verdes (44) vs. esperadas por azar al 95%.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\analyze_volume_limit.py
    C:\\trading_env\\Scripts\\python scripts\\analyze_volume_limit.py --start 2010-01-01
    C:\\trading_env\\Scripts\\python scripts\\analyze_volume_limit.py --n-boot 2000
"""

import sys
import os
import argparse
import statistics
import random
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent / "trading_system"
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("TRADING_ENV", "dev")

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()

_ENTRY_LIMIT_VALUES = [
    0.005, 0.008, 0.010, 0.011, 0.012,
    0.013, 0.014, 0.016, 0.017, 0.018, 0.020,
]
IS_RATIO       = 0.70
MIN_CELL_SIZE  = 30
VOL_MA_WINDOW  = 20
VOL_EXTREME_THRESHOLD_MULT = 5.0   # vol_ratio > mediana × N → sospechoso


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start",  default="2010-01-01")
    p.add_argument("--end",    default=date.today().strftime("%Y-%m-%d"))
    p.add_argument("--n-boot", type=int, default=1000,
                   help="Resamples del bootstrap por bloques (default 1000)")
    p.add_argument("--skip-download", action="store_true")
    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# Simulación de salida
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_exit(ind: pd.DataFrame, entry_idx: int, entry_price: float,
                   sl_pct: float, exit_crsi: float, time_stop: int) -> dict:
    """
    Simula la salida por CRSI_EXIT, SL o TSTO.

    Params:
        ind:         DataFrame con Close, Low, CONNORS_RSI.
        entry_idx:   Posición iloc del día de entrada.
        entry_price: Precio de entrada.
        sl_pct:      Stop loss fraccional.
        exit_crsi:   Umbral de salida CRSI.
        time_stop:   Días máximos.
    Returns:
        {ret_pct, reason}
    """
    sl_price = entry_price * (1.0 - sl_pct)
    n        = len(ind)
    for k in range(1, time_stop + 1):
        idx = entry_idx + k
        if idx >= n:
            ep = float(ind.iloc[-1]["Close"])
            return {"ret_pct": (ep - entry_price) / entry_price * 100, "reason": "END"}
        row   = ind.iloc[idx]
        low_  = float(row["Low"])
        cls_  = float(row["Close"])
        crsi_ = row["CONNORS_RSI"]
        if low_ <= sl_price:
            return {"ret_pct": (sl_price - entry_price) / entry_price * 100, "reason": "SL"}
        if not pd.isna(crsi_) and crsi_ > exit_crsi:
            return {"ret_pct": (cls_ - entry_price) / entry_price * 100, "reason": "CRSI_EXIT"}
        if k == time_stop:
            return {"ret_pct": (cls_ - entry_price) / entry_price * 100, "reason": "TSTO"}
    return {"ret_pct": 0.0, "reason": "END"}


# ══════════════════════════════════════════════════════════════════════════════
# Scan de señales con volumen relativo
# ══════════════════════════════════════════════════════════════════════════════

def _is_3day_low_series(close_s: pd.Series) -> pd.Series:
    """True si Close[i] es el mínimo de los últimos 3 cierres."""
    return (close_s <= close_s.shift(1)) & (close_s <= close_s.shift(2))


def scan_signals_with_volume(raw: dict, spy: pd.DataFrame,
                              start_date: str, end_date: str,
                              commission: float) -> tuple:
    """
    Escanea señales históricas e incluye volumen relativo y precios de D.

    Params:
        raw:        {ticker: DataFrame OHLCV}.
        spy:        DataFrame OHLCV de SPY.
        start_date: Inicio del período.
        end_date:   Fin del período.
        commission: Comisión fraccional.
    Returns:
        (lista_señales, {ticker: ind_df})
    """
    import config
    from modules.connors_rsi import compute_connors

    sl_pct    = config.CONNORS_STOP_LOSS
    exit_crsi = config.CONNORS_EXIT_CRSI
    time_stop = config.CONNORS_TIME_STOP

    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    try:
        spy_ind = compute_connors(spy)
    except Exception as e:
        console.print(f"[red]Error indicadores SPY: {e}[/red]")
        return [], {}

    all_signals = []
    ind_cache   = {}
    n_tickers   = 0
    n_no_vol    = 0

    for ticker, df in raw.items():
        if df is None or df.empty or len(df) < 220:
            continue
        try:
            ind = compute_connors(df)
        except Exception:
            continue

        ind["VOL_MA20"] = ind["Volume"].rolling(VOL_MA_WINDOW).mean()
        ind_cache[ticker] = ind

        mask_p = (ind.index >= start_ts - pd.Timedelta(days=5)) & (ind.index <= end_ts)
        ind_p  = ind[mask_p].copy()
        if len(ind_p) < 5:
            continue

        ind_p["is_3day_low"] = _is_3day_low_series(ind_p["Close"])
        ind_p["signal"]      = (
            (ind_p["CONNORS_RSI"] < config.CONNORS_ENTRY_CRSI) &
            ind_p["is_3day_low"] &
            (ind_p["Close"] > ind_p["SMA200"]) &
            ind_p["CONNORS_RSI"].notna() &
            ind_p["SMA200"].notna()
        )

        signal_days = ind_p.index[ind_p["signal"]]
        if signal_days.empty:
            continue

        n_tickers += 1

        for sig_day in signal_days:
            if sig_day < start_ts or sig_day > end_ts:
                continue
            if sig_day not in spy_ind.index:
                continue
            spy_row = spy_ind.loc[sig_day]
            if pd.isna(spy_row["SMA200"]) or spy_row["Close"] <= spy_row["SMA200"]:
                continue

            vol_dm1    = ind.at[sig_day, "Volume"]  if sig_day in ind.index else None
            vol_ma_dm1 = ind.at[sig_day, "VOL_MA20"] if sig_day in ind.index else None
            if (vol_dm1 is None or pd.isna(vol_dm1) or float(vol_dm1) == 0 or
                    vol_ma_dm1 is None or pd.isna(vol_ma_dm1) or float(vol_ma_dm1) == 0):
                n_no_vol += 1
                continue

            vol_ratio  = float(vol_dm1) / float(vol_ma_dm1)
            close_dm1  = float(ind.at[sig_day, "Close"])

            try:
                iloc_dm1 = ind.index.get_loc(sig_day)
            except KeyError:
                continue
            entry_idx = iloc_dm1 + 1
            if entry_idx >= len(ind):
                continue

            row_D = ind.iloc[entry_idx]

            all_signals.append({
                "ticker":      ticker,
                "signal_date": sig_day,
                "close_dm1":   close_dm1,
                "vol_ratio":   vol_ratio,
                "d0_open":     float(row_D["Open"]),
                "d0_low":      float(row_D["Low"]),
                "d0_close":    float(row_D["Close"]),
                "entry_idx":   entry_idx,
                "_sl_pct":     sl_pct,
                "_exit_crsi":  exit_crsi,
                "_time_stop":  time_stop,
                "_commission": commission,
            })

    console.print(
        f"  {n_tickers} tickers · {len(all_signals):,} señales · "
        f"{n_no_vol} descartadas (volumen ausente/cero)"
    )
    return all_signals, ind_cache


# ══════════════════════════════════════════════════════════════════════════════
# Simulación fill + salida por señal × ENTRY_LIMIT
# ══════════════════════════════════════════════════════════════════════════════

def simulate_all(signals: list, ind_cache: dict) -> list:
    """
    Para cada señal y cada valor de ENTRY_LIMIT, simula fill y retorno neto.
    Añade 'results': {entry_limit: {filled, ret_net}} a cada señal.

    Params:
        signals:   lista de señales.
        ind_cache: {ticker: ind_df}.
    Returns:
        Lista de señales con 'results' añadido.
    """
    for s in signals:
        ind = ind_cache.get(s["ticker"])
        if ind is None:
            s["results"] = {}
            continue
        entry_idx  = s["entry_idx"]
        close_dm1  = s["close_dm1"]
        d0_open    = s["d0_open"]
        d0_low     = s["d0_low"]
        sl_pct     = s["_sl_pct"]
        exit_crsi  = s["_exit_crsi"]
        time_stop  = s["_time_stop"]
        commission = s["_commission"]

        results = {}
        for lim in _ENTRY_LIMIT_VALUES:
            limit_p = close_dm1 * (1.0 - lim)
            if d0_low <= limit_p:
                entry_p = d0_open if d0_open <= limit_p else limit_p
                sim     = _simulate_exit(ind, entry_idx, entry_p, sl_pct, exit_crsi, time_stop)
                results[lim] = {"filled": True,
                                "ret_net": sim["ret_pct"] - 2.0 * commission * 100.0}
            else:
                results[lim] = {"filled": False, "ret_net": None}
        s["results"] = results
    return signals


# ══════════════════════════════════════════════════════════════════════════════
# Comprobación de splits yfinance (sección 0)
# ══════════════════════════════════════════════════════════════════════════════

def check_volume_splits(signals: list) -> dict:
    """
    Comprueba si los outliers de vol_ratio coinciden con splits de yfinance.
    Solo descarga splits para los tickers con más señales outlier.

    Con yfinance auto_adjust=True, el volumen se divide por el split_ratio
    hacia atrás, por lo que vol_ratio no debería mostrar saltos en la fecha
    del split. Esta función verifica ese supuesto.

    Params:
        signals: lista completa de señales con vol_ratio.
    Returns:
        Dict con estadísticas del check.
    """
    import yfinance as yf

    vols = sorted(s["vol_ratio"] for s in signals)
    n    = len(vols)
    if n == 0:
        return {}

    p50  = vols[n // 2]
    p95  = vols[int(n * 0.95)]
    p99  = vols[int(n * 0.99)]
    # Umbral "extremo": > 99th percentile
    extreme_thresh = p99

    extreme_sigs = [s for s in signals if s["vol_ratio"] > extreme_thresh]
    n_extreme    = len(extreme_sigs)

    # Agrupar outliers por ticker (elegir los 20 más frecuentes)
    by_ticker = defaultdict(list)
    for s in extreme_sigs:
        by_ticker[s["ticker"]].append(s)
    top_tickers = sorted(by_ticker, key=lambda t: -len(by_ticker[t]))[:20]

    n_near_split = 0
    n_checked    = 0

    for ticker in top_tickers:
        try:
            splits = yf.Ticker(ticker).splits
        except Exception:
            continue
        if splits is None or len(splits) == 0:
            n_checked += 1
            continue
        n_checked += 1
        # Quitar timezone para poder comparar con sig_date (tz-naive)
        split_dates = set(pd.Timestamp(d).tz_localize(None).normalize() for d in splits.index)

        for s in by_ticker[ticker]:
            sig_date = pd.Timestamp(s["signal_date"]).normalize()
            # Dentro de la ventana VOL_MA20: si un split cae en [sig_date-20, sig_date]
            for sd in split_dates:
                delta = (sig_date - sd).days
                if 0 <= delta <= VOL_MA_WINDOW:
                    n_near_split += 1
                    break

    return {
        "n_total":        n,
        "p50":            p50,
        "p95":            p95,
        "p99":            p99,
        "extreme_thresh": extreme_thresh,
        "n_extreme":      n_extreme,
        "pct_extreme":    n_extreme / n * 100,
        "n_tickers_checked": n_checked,
        "n_near_split":   n_near_split,
        "pct_near_split_of_extreme": n_near_split / n_extreme * 100 if n_extreme else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap por bloques (IC 95%)
# ══════════════════════════════════════════════════════════════════════════════

def _block_bootstrap_diff(sigs_q: list, lim_test: float, lim_sys: float,
                           n_boot: int = 1000) -> dict | None:
    """
    Bootstrap por bloques mensuales del retorno esperado por señal:
    ret_exp(lim_test) - ret_exp(lim_sys), señal a señal.
    Las señales del mismo mes van en el mismo bloque para respetar correlación.

    Params:
        sigs_q:   señales del cuartil (solo OOS).
        lim_test: valor de ENTRY_LIMIT a contrastar.
        lim_sys:  valor de referencia (sistema actual).
        n_boot:   número de resamples.
    Returns:
        {obs_diff, ci_low, ci_high, n_months, n_signals} o None si no hay datos.
    """
    if not sigs_q:
        return None

    # Por señal: diferencia de retornos esperados (señales no llenadas = 0)
    by_month = defaultdict(list)
    for s in sigs_q:
        r_test = (s["results"].get(lim_test) or {})
        r_sys  = (s["results"].get(lim_sys)  or {})
        v_test = r_test.get("ret_net", 0) if r_test.get("filled") else 0.0
        v_sys  = r_sys.get("ret_net",  0) if r_sys.get("filled")  else 0.0
        month  = s["signal_date"].to_period("M")
        by_month[month].append(v_test - v_sys)

    months = list(by_month.keys())
    n_months = len(months)
    if n_months < 3:
        return None

    all_diffs   = [d for month in months for d in by_month[month]]
    obs_diff    = statistics.mean(all_diffs)

    rng         = random.Random(42)
    boot_means  = []
    for _ in range(n_boot):
        sample = []
        for _ in range(n_months):
            m = rng.choice(months)
            sample.extend(by_month[m])
        boot_means.append(statistics.mean(sample))
    boot_means.sort()

    ci_low  = boot_means[int(n_boot * 0.025)]
    ci_high = boot_means[int(n_boot * 0.975)]

    return {
        "obs_diff":  obs_diff,
        "ci_low":    ci_low,
        "ci_high":   ci_high,
        "n_months":  n_months,
        "n_signals": len(all_diffs),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Métricas por celda y cuartiles
# ══════════════════════════════════════════════════════════════════════════════

def _cell_metrics(sigs: list, lim: float) -> dict:
    """
    Calcula fill_rate, ret_mean, ret_median, ret_expected para una celda.

    Params:
        sigs: señales del subgrupo.
        lim:  valor de ENTRY_LIMIT.
    Returns:
        {n, n_filled, fill_rate, ret_mean, ret_median, ret_expected}
    """
    n_total = len(sigs)
    filled  = [s for s in sigs if (s["results"].get(lim) or {}).get("filled")]
    rets    = [s["results"][lim]["ret_net"] for s in filled]

    fill_rate = len(filled) / n_total * 100 if n_total else 0.0
    ret_mean  = statistics.mean(rets)   if rets else None
    ret_med   = statistics.median(rets) if rets else None
    ret_exp   = (fill_rate / 100.0) * ret_mean if ret_mean is not None else None

    return {
        "n": n_total, "n_filled": len(filled),
        "fill_rate": fill_rate,
        "ret_mean": ret_mean, "ret_median": ret_med,
        "ret_expected": ret_exp,
    }


def _assign_quartile(vol_ratio: float, thresholds: list) -> int:
    q25, q50, q75 = thresholds
    if vol_ratio <= q25:
        return 1
    if vol_ratio <= q50:
        return 2
    if vol_ratio <= q75:
        return 3
    return 4


def _q_label(q: int, thresholds: list) -> str:
    q25, q50, q75 = thresholds
    labels = {1: f"≤{q25:.2f}", 2: f"{q25:.2f}–{q50:.2f}",
              3: f"{q50:.2f}–{q75:.2f}", 4: f">{q75:.2f}"}
    return labels[q]


# ══════════════════════════════════════════════════════════════════════════════
# Test de monotonía
# ══════════════════════════════════════════════════════════════════════════════

def monotonicity_test(is_by_q: dict, thresholds: list) -> dict:
    """
    Comprueba si el mejor ENTRY_LIMIT por cuartil sigue una pauta monótona.
    Un patrón no monótono sugiere ruido aunque alguna celda individual sea verde.

    Params:
        is_by_q:    {q: [señales IS]}.
        thresholds: [q25, q50, q75].
    Returns:
        {best_by_q: [lim_Q1, lim_Q2, lim_Q3, lim_Q4], monotone: bool, direction: str}
    """
    best_by_q = []
    for q in [1, 2, 3, 4]:
        sigs_q = is_by_q.get(q, [])
        if len(sigs_q) < MIN_CELL_SIZE:
            best_by_q.append(None)
            continue

        def _key(lim):
            m = _cell_metrics(sigs_q, lim)
            if m["n"] < MIN_CELL_SIZE:
                return -999.0
            return m["ret_expected"] if m["ret_expected"] is not None else -999.0

        best_by_q.append(max(_ENTRY_LIMIT_VALUES, key=_key))

    valid = [(i, v) for i, v in enumerate(best_by_q) if v is not None]
    if len(valid) < 3:
        return {"best_by_q": best_by_q, "monotone": None, "direction": "sin datos suficientes"}

    vals = [v for _, v in valid]
    up   = all(a <= b for a, b in zip(vals, vals[1:]))
    down = all(a >= b for a, b in zip(vals, vals[1:]))

    if up and not down:
        return {"best_by_q": best_by_q, "monotone": True,  "direction": "creciente (más profundo con más volumen)"}
    if down and not up:
        return {"best_by_q": best_by_q, "monotone": True,  "direction": "decreciente (menos profundo con más volumen)"}
    return     {"best_by_q": best_by_q, "monotone": False, "direction": "no monótono — patrón no robusto"}


# ══════════════════════════════════════════════════════════════════════════════
# Conteo de celdas verdes vs. azar (las 44)
# ══════════════════════════════════════════════════════════════════════════════

def count_green_cells(oos_by_q: dict, system_limit: float,
                      is_by_q: dict, n_boot: int = 1000) -> dict:
    """
    Para cada una de las 44 celdas (4 cuartiles × 11 ENTRY_LIMIT),
    aplica bootstrap por bloques en OOS: IC 95% de (lim − system_limit).
    Cuenta cuántas celdas tienen IC > 0 con el mismo signo que en IS.
    Compara con las esperadas por azar al 95% (44 × 0.05 ≈ 2.2).

    Params:
        oos_by_q:     {q: [señales OOS]}.
        system_limit: CONNORS_ENTRY_LIMIT del sistema.
        is_by_q:      {q: [señales IS]} para verificar signo IS.
        n_boot:       resamples bootstrap.
    Returns:
        {n_green, n_expected_chance, details}
    """
    n_total    = 4 * len(_ENTRY_LIMIT_VALUES)  # 44
    n_expected = n_total * 0.05
    n_green    = 0
    details    = []

    for q in [1, 2, 3, 4]:
        sigs_oos = oos_by_q.get(q, [])
        sigs_is  = is_by_q.get(q, [])
        for lim in _ENTRY_LIMIT_VALUES:
            if lim == system_limit:
                details.append({"q": q, "lim": lim, "green": False, "note": "=sistema"})
                continue

            is_m   = _cell_metrics(sigs_is, lim)
            is_ref = _cell_metrics(sigs_is, system_limit)
            is_adv = ((is_m["ret_expected"] or 0) - (is_ref["ret_expected"] or 0))

            if len(sigs_oos) < MIN_CELL_SIZE:
                details.append({"q": q, "lim": lim, "green": False, "note": "N insuf."})
                continue

            ci = _block_bootstrap_diff(sigs_oos, lim, system_limit, n_boot)
            if ci is None:
                details.append({"q": q, "lim": lim, "green": False, "note": "sin CI"})
                continue

            is_green = (ci["ci_low"] > 0 and is_adv > 0)
            if is_green:
                n_green += 1
            details.append({
                "q": q, "lim": lim, "green": is_green,
                "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
                "obs_diff": ci["obs_diff"],
            })

    return {
        "n_total":          n_total,
        "n_green":          n_green,
        "n_expected_chance": n_expected,
        "details":          details,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Tablas de output
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_ret(v, n, bold=False):
    if n < MIN_CELL_SIZE:
        return "[dim]¿?[/dim]"
    if v is None:
        return "—"
    c = "green" if v >= 0 else "red"
    b = " bold" if bold else ""
    return f"[{c}{b}]{v:+.3f}%[/{c}{b}]"


def _fmt_fill(v, n):
    if n < MIN_CELL_SIZE:
        return "[dim]¿?[/dim]"
    c = "green" if v >= 50 else "yellow"
    return f"[{c}]{v:.1f}%[/{c}]"


def _best_lim(sigs_q: list, metric: str = "ret_expected") -> float:
    """Devuelve el ENTRY_LIMIT con mejor métrica en sigs_q."""
    def _key(lim):
        m = _cell_metrics(sigs_q, lim)
        if m["n"] < MIN_CELL_SIZE:
            return -999.0
        return m.get(metric) or -999.0
    return max(_ENTRY_LIMIT_VALUES, key=_key)


def _print_metric_table(signals_by_q: dict, thresholds: list,
                         block_name: str, metric: str) -> None:
    """
    Tabla con filas = cuartiles y columnas = ENTRY_LIMIT.
    Para ret_mean y ret_expected incluye también la mediana.

    Params:
        signals_by_q: {q: [señales]}.
        thresholds:   [q25, q50, q75].
        block_name:   "IS" o "OOS".
        metric:       "fill_rate", "ret_mean" o "ret_expected".
    """
    titles = {
        "fill_rate":    "Fill rate",
        "ret_mean":     "Retorno medio / mediano por llenada",
        "ret_expected": "Retorno esperado por señal (fill_rate × ret_medio)",
    }
    tbl = Table(
        title=f"[bold cyan]{block_name} — {titles[metric]}[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    tbl.add_column("Cuartil vol.", justify="left",  style="bold", no_wrap=True)
    tbl.add_column("N señales",   justify="right",  no_wrap=True)
    for lim in _ENTRY_LIMIT_VALUES:
        tbl.add_column(f"{lim:.3f}", justify="right", no_wrap=True)
    tbl.add_column("Mejor", justify="center", no_wrap=True)

    for q in [1, 2, 3, 4]:
        sigs_q = signals_by_q.get(q, [])
        n_q    = len(sigs_q)
        cells  = {lim: _cell_metrics(sigs_q, lim) for lim in _ENTRY_LIMIT_VALUES}
        bl     = _best_lim(sigs_q, metric)

        row = [f"Q{q} (×{_q_label(q, thresholds)})", str(n_q)]
        for lim in _ENTRY_LIMIT_VALUES:
            m       = cells[lim]
            n_cell  = m["n"]
            is_best = (lim == bl and n_cell >= MIN_CELL_SIZE)

            if metric == "fill_rate":
                row.append(_fmt_fill(m["fill_rate"], n_cell))
            elif metric == "ret_mean":
                mean_ = _fmt_ret(m["ret_mean"],   n_cell, bold=is_best)
                med_  = _fmt_ret(m["ret_median"], n_cell)
                row.append(f"{mean_}\n{med_}")
            else:  # ret_expected
                row.append(_fmt_ret(m["ret_expected"], n_cell, bold=is_best))

        row.append(f"[bold]{bl:.3f}[/bold]" if n_q >= MIN_CELL_SIZE else "¿?")
        tbl.add_row(*row)

    console.print(tbl)


def _print_summary_with_ci(is_by_q: dict, oos_by_q: dict, thresholds: list,
                            system_limit: float, n_boot: int) -> None:
    """
    Tabla resumen con IC 95% bootstrap por bloques para ¿Persiste?.
    Verde solo si IC excluye el cero Y el signo coincide con IS.

    Params:
        is_by_q:      {q: [señales IS]}.
        oos_by_q:     {q: [señales OOS]}.
        thresholds:   [q25, q50, q75].
        system_limit: ENTRY_LIMIT actual del sistema.
        n_boot:       resamples bootstrap.
    """
    tbl = Table(
        title="[bold cyan]Resumen: mejor IS → IC 95% bootstrap en OOS[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    tbl.add_column("Cuartil",        justify="left",   style="bold")
    tbl.add_column("N IS / OOS",     justify="right")
    tbl.add_column("Mejor IS",       justify="center")
    tbl.add_column("Ret.exp IS\n(mejor)",        justify="right")
    tbl.add_column(f"Ret.exp IS\n({system_limit:.3f})", justify="right")
    tbl.add_column("Δ OOS observado",             justify="right")
    tbl.add_column("IC 95% Δ OOS",                justify="right")
    tbl.add_column("¿Persiste?",                  justify="center")

    for q in [1, 2, 3, 4]:
        sigs_is  = is_by_q.get(q, [])
        sigs_oos = oos_by_q.get(q, [])
        n_is, n_oos = len(sigs_is), len(sigs_oos)

        bl       = _best_lim(sigs_is)
        m_is_bl  = _cell_metrics(sigs_is,  bl)
        m_is_sys = _cell_metrics(sigs_is,  system_limit)
        is_adv   = ((m_is_bl["ret_expected"] or 0) - (m_is_sys["ret_expected"] or 0))

        ci = None
        if n_oos >= MIN_CELL_SIZE and bl != system_limit:
            ci = _block_bootstrap_diff(sigs_oos, bl, system_limit, n_boot)

        def _fre(v, n):
            if n < MIN_CELL_SIZE or v is None:
                return "¿?"
            c = "green" if v >= 0 else "red"
            return f"[{c}]{v:+.4f}%[/{c}]"

        if ci is None:
            ci_str    = "[dim]N/A[/dim]"
            obs_str   = "[dim]N/A[/dim]"
            persiste  = "[dim]N insuf.[/dim]" if n_oos < MIN_CELL_SIZE else "[dim]=sistema[/dim]"
        else:
            obs_str   = f"[{'green' if ci['obs_diff']>0 else 'red'}]{ci['obs_diff']:+.4f}%[/]"
            ci_str    = f"[{ci['ci_low']:+.4f}%,  {ci['ci_high']:+.4f}%]"
            ci_excl_0 = ci["ci_low"] > 0 or ci["ci_high"] < 0
            same_sign = (ci["obs_diff"] > 0 and is_adv > 0) or (ci["obs_diff"] < 0 and is_adv < 0)
            if ci["ci_low"] > 0 and is_adv > 0:
                persiste = "[green bold]Sí (IC>0, mismo signo)[/green bold]"
            elif ci["ci_high"] < 0:
                persiste = "[red]Invierte signo[/red]"
            elif ci_excl_0 and not same_sign:
                persiste = "[red]IC excluye 0, signo cambia[/red]"
            else:
                persiste = "[dim]Neutro (IC cruza 0)[/dim]"

        tbl.add_row(
            f"Q{q} (×{_q_label(q, thresholds)})",
            f"{n_is:,} / {n_oos:,}",
            f"[bold]{bl:.3f}[/bold]",
            _fre(m_is_bl.get("ret_expected"),  n_is),
            _fre(m_is_sys.get("ret_expected"), n_is),
            obs_str,
            ci_str,
            persiste,
        )

    console.print(tbl)


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Punto de entrada del análisis volumen relativo × CONNORS_ENTRY_LIMIT."""
    args = _parse_args()

    import config
    from modules.connors_rsi import _download_universe
    from modules import yf_cache

    system_limit = config.CONNORS_ENTRY_LIMIT

    console.print(
        f"\n[bold cyan]Análisis: volumen relativo × CONNORS_ENTRY_LIMIT (v2)[/bold cyan]\n"
        f"  Período: {args.start} → {args.end}\n"
        f"  Matriz ENTRY_LIMIT ({len(_ENTRY_LIMIT_VALUES)} valores): "
        f"{', '.join(f'{v:.3f}' for v in _ENTRY_LIMIT_VALUES)}\n"
        f"  ENTRY_LIMIT actual del sistema: {system_limit:.3f}\n"
        f"  Vol. relativo = Volume[D-1] / media({VOL_MA_WINDOW} sesiones hasta D-1)\n"
        f"  [dim]yfinance auto_adjust=True: precios y volumen ajustados por splits "
        f"(Volume ÷ split_ratio acumulado → escala consistente).[/dim]\n"
        f"  Bootstrap: {args.n_boot} resamples, bloques mensuales.\n"
    )

    if args.skip_download and not yf_cache.session_data:
        console.print("[yellow]--skip-download sin session_data. Descargando.[/yellow]")
        args.skip_download = False

    ext_start = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")

    if not args.skip_download:
        yf_cache.clear_session()
        console.print("[cyan]Descargando datos...[/cyan]")
        raw, spy = _download_universe(start=ext_start, end=args.end)
    else:
        raw = {t: df for t, df in yf_cache.session_data.items() if t != "SPY"}
        spy = yf_cache.get_session("SPY")

    if not raw or spy is None:
        console.print("[red]Sin datos. Abortando.[/red]")
        return

    console.print("[cyan]Escaneando señales con volumen...[/cyan]")
    signals, ind_cache = scan_signals_with_volume(
        raw, spy, args.start, args.end, commission=config.COMMISSION
    )
    if not signals:
        console.print("[red]Sin señales. Abortando.[/red]")
        return

    console.print(f"[cyan]Simulando fill + salida × {len(_ENTRY_LIMIT_VALUES)} valores...[/cyan]")
    signals = simulate_all(signals, ind_cache)

    # ── IS / OOS split ────────────────────────────────────────────────────────
    sorted_s = sorted(signals, key=lambda s: s["signal_date"])
    cut       = int(len(sorted_s) * IS_RATIO)
    is_sigs   = sorted_s[:cut]
    oos_sigs  = sorted_s[cut:]
    oos_start = str(oos_sigs[0]["signal_date"])[:10] if oos_sigs else "—"
    console.print(
        f"\n  IS: {len(is_sigs):,} señales  ·  OOS: {len(oos_sigs):,} señales (desde {oos_start})\n"
    )

    # ── Cuartiles (solo IS) ───────────────────────────────────────────────────
    is_vols    = sorted(s["vol_ratio"] for s in is_sigs)
    n_is       = len(is_vols)
    q25 = is_vols[int(n_is * 0.25)]
    q50 = is_vols[int(n_is * 0.50)]
    q75 = is_vols[int(n_is * 0.75)]
    thresholds = [q25, q50, q75]
    console.print(
        f"  Umbrales cuartil IS (fijos, se aplican a OOS):\n"
        f"    Q25={q25:.3f}  Q50={q50:.3f}  Q75={q75:.3f}\n"
    )

    def _group(sigs):
        d = defaultdict(list)
        for s in sigs:
            d[_assign_quartile(s["vol_ratio"], thresholds)].append(s)
        return d

    is_by_q  = _group(is_sigs)
    oos_by_q = _group(oos_sigs)

    # ══ Sección 0: comprobación splits ════════════════════════════════════════
    console.print(Panel(
        "[bold]0 · Comprobación de volumen ajustado por splits[/bold]",
        border_style="cyan", padding=(0, 1)
    ))
    console.print("[cyan]  Comprobando outliers y splits yfinance (top 20 tickers)...[/cyan]")
    sv = check_volume_splits(signals)

    vols_all = [s["vol_ratio"] for s in signals]
    vols_all.sort()
    n_v = len(vols_all)
    dist_str = (
        f"    Distribución vol_ratio:  "
        f"min={vols_all[0]:.2f}  P5={vols_all[int(n_v*.05)]:.2f}  "
        f"P25={vols_all[int(n_v*.25)]:.2f}  P50={vols_all[int(n_v*.50)]:.2f}  "
        f"P75={vols_all[int(n_v*.75)]:.2f}  P95={vols_all[int(n_v*.95)]:.2f}  "
        f"max={vols_all[-1]:.2f}"
    )
    console.print(dist_str)
    console.print(
        f"    Umbral 'extremo' (P99): {sv['extreme_thresh']:.3f}  → "
        f"{sv['n_extreme']} señales ({sv['pct_extreme']:.1f}%)\n"
        f"    Tickers chequeados con yf.splits: {sv['n_tickers_checked']}\n"
        f"    Señales outlier dentro de ventana VOL_MA20 de un split: "
        f"{sv['n_near_split']} ({sv['pct_near_split_of_extreme']:.1f}% de los extremos)\n"
    )
    if sv["pct_near_split_of_extreme"] < 20:
        console.print(
            "  [green]OK[/green] — Menos del 20% de los outliers coinciden con splits. "
            "El ajuste de yfinance parece consistente; no se excluyen señales.\n"
        )
    else:
        console.print(
            "  [yellow]AVISO[/yellow] — Más del 20% de los outliers están dentro de la ventana "
            "de un split. Revisar manualmente si el ajuste es correcto para esos tickers.\n"
        )

    # ══ Secciones 1-3: tablas de métricas ════════════════════════════════════
    console.print("─" * 70)
    console.print(Panel("[bold]1 · Fill rate[/bold]", border_style="cyan", padding=(0, 1)))
    _print_metric_table(is_by_q,  thresholds, "IS",  "fill_rate")
    console.print()
    _print_metric_table(oos_by_q, thresholds, "OOS", "fill_rate")

    console.print("\n" + "─" * 70)
    console.print(Panel("[bold]2 · Retorno medio y mediano por llenada[/bold]",
                         border_style="cyan", padding=(0, 1)))
    _print_metric_table(is_by_q,  thresholds, "IS",  "ret_mean")
    console.print()
    _print_metric_table(oos_by_q, thresholds, "OOS", "ret_mean")

    console.print("\n" + "─" * 70)
    console.print(Panel("[bold]3 · Retorno esperado por señal[/bold]",
                         border_style="cyan", padding=(0, 1)))
    _print_metric_table(is_by_q,  thresholds, "IS",  "ret_expected")
    console.print()
    _print_metric_table(oos_by_q, thresholds, "OOS", "ret_expected")

    # ══ Sección 4: test de monotonía ══════════════════════════════════════════
    console.print("\n" + "─" * 70)
    console.print(Panel("[bold]4 · Test de monotonía[/bold]", border_style="cyan", padding=(0, 1)))
    mono = monotonicity_test(is_by_q, thresholds)
    best_seq = " → ".join(
        f"Q{i+1}:{v:.3f}" if v is not None else f"Q{i+1}:¿?"
        for i, v in enumerate(mono["best_by_q"])
    )
    console.print(f"  Mejor ENTRY_LIMIT por cuartil IS:  {best_seq}")
    if mono["monotone"] is None:
        console.print("  [dim]Datos insuficientes para test de monotonía.[/dim]")
    elif mono["monotone"]:
        console.print(f"  [green]Monótono — {mono['direction']}[/green]")
        console.print(
            "  [dim]Un patrón monótono sugiere que el efecto del volumen tiene "
            "una dirección consistente (más creíble).[/dim]"
        )
    else:
        console.print(
            f"  [red]No monótono — {mono['direction']}[/red]\n"
            "  [dim]Un patrón irregular (ej. Q1=0.020, Q2=0.010, Q3=0.017, Q4=0.008) "
            "indica que las diferencias entre cuartiles son probablemente ruido, "
            "independientemente de cuántas celdas individuales parezcan positivas.[/dim]"
        )

    # ══ Sección 5: resumen con IC bootstrap ════════════════════════════════════
    console.print("\n" + "─" * 70)
    console.print(Panel(
        f"[bold]5 · Resumen por cuartil — IC 95% bootstrap por bloques[/bold]\n"
        f"[dim]({args.n_boot} resamples mensuales; referencia = {system_limit:.3f} actual del sistema)[/dim]",
        border_style="cyan", padding=(0, 1)
    ))
    _print_summary_with_ci(is_by_q, oos_by_q, thresholds, system_limit, args.n_boot)

    # ══ Sección 6: conteo de celdas verdes ════════════════════════════════════
    console.print("\n" + "─" * 70)
    console.print(Panel("[bold]6 · Conteo de celdas verdes vs. azar[/bold]",
                         border_style="cyan", padding=(0, 1)))
    console.print(f"  [cyan]Calculando bootstrap para las {4 * len(_ENTRY_LIMIT_VALUES)} celdas...[/cyan]")
    gc = count_green_cells(oos_by_q, system_limit, is_by_q, n_boot=args.n_boot)

    n_g   = gc["n_green"]
    n_exp = gc["n_expected_chance"]
    n_tot = gc["n_total"]
    c_g   = "green" if n_g > n_exp * 2 else "yellow" if n_g > n_exp else "dim"

    console.print(
        f"\n  Celdas verdes observadas (IC>0, mismo signo IS): [{c_g}]{n_g} / {n_tot}[/{c_g}]\n"
        f"  Celdas verdes esperadas solo por azar al 95%:    {n_exp:.1f} (= {n_tot} × 0.05)\n"
    )
    if n_g <= n_exp * 1.5:
        console.print(
            "  [dim]Conclusión: el número de celdas verdes está dentro del rango esperable "
            "por azar. No hay evidencia de que el volumen relativo permita mejorar "
            "sistemáticamente el ENTRY_LIMIT.[/dim]\n"
        )
    elif not mono["monotone"]:
        console.print(
            "  [yellow]Conclusión: hay más celdas verdes que las esperadas por azar, "
            "pero el patrón no es monótono → el resultado es muy probablemente ruido "
            "de selección múltiple (p-hacking implícito al comparar 44 celdas).[/yellow]\n"
        )
    else:
        console.print(
            "  [green]Conclusión: más celdas verdes que las esperadas por azar Y patrón "
            "monótono. El volumen relativo podría tener capacidad predictiva sobre "
            "la profundidad óptima de ENTRY_LIMIT, pero conviene validar con datos frescos "
            "antes de implementar.[/green]\n"
        )

    console.print("[bold green]Análisis completado.[/bold green]\n")


if __name__ == "__main__":
    main()
