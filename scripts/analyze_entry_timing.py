# -*- coding: utf-8 -*-
"""
Análisis de timing de entrada: ¿conviene entrar en D, D+1 o D+2?

Para cada señal generada por las reglas de ConnorsRSI (CRSI<25, 3-day-low,
Close>SMA200, SPY>SMA200) en el periodo 2010-hoy, se simulan tres estrategias
de entrada con idéntica lógica de salida (CRSI_EXIT / SL / TSTO).

Estrategias comparadas:
  D_nofill  — entrada al limit=Close[D-1]×(1-ENTRY_LIMIT), sin filtro de fill
              (equivale a asumir 100% fill — universo completo de señales).
  D_fillsim — igual que D_nofill pero solo si Low[D] ≤ limit (FILL_SIM=True).
              Open[D]≤limit → fill al Open[D]; si no → señal perdida.
  D1_close  — entrada al Close[D+1]; señales perdidas si no hay datos D+1.
  D2_close  — entrada al Close[D+2]; señales perdidas si no hay datos D+2.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\analyze_entry_timing.py
    C:\\trading_env\\Scripts\\python scripts\\analyze_entry_timing.py --start 2010-01-01
"""

import sys
import os
import argparse
import statistics
from pathlib import Path
from datetime import date

_ROOT = Path(__file__).resolve().parent.parent / "trading_system"
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("TRADING_ENV", "dev")

import pandas as pd
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()

IS_RATIO = 0.70


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end",   default=date.today().strftime("%Y-%m-%d"))
    p.add_argument("--skip-download", action="store_true")
    return p.parse_args()


# ── Scan de señales históricas (vectorizado por ticker) ───────────────────────

def _is_3day_low_series(close_s: pd.Series) -> pd.Series:
    """
    True en la fila i si Close[i] <= Close[i-1] y Close[i] <= Close[i-2].
    Replica la lógica de _has_3_day_low del backtest.

    Params:
        close_s: Serie de cierres.
    Returns:
        Serie booleana alineada con close_s.
    """
    return (close_s <= close_s.shift(1)) & (close_s <= close_s.shift(2))


def _simulate_exit(ind: pd.DataFrame, entry_idx: int, entry_price: float,
                   sl_pct: float, exit_crsi: float, time_stop: int) -> dict:
    """
    Simula la salida dada la posición abierta al precio entry_price en entry_idx.
    Comprueba en orden: SL (usando Low), CRSI_EXIT (usando Close), TSTO.

    Params:
        ind:         DataFrame con columnas Close, Low, CONNORS_RSI.
        entry_idx:   Posición (iloc) del día de entrada.
        entry_price: Precio de entrada.
        sl_pct:      Stop loss como fracción (e.g. 0.05).
        exit_crsi:   Umbral de salida CRSI (e.g. 60).
        time_stop:   Días máximos de holding.
    Returns:
        {exit_price, reason, days_held, ret_pct}
    """
    sl_price = entry_price * (1.0 - sl_pct)
    n        = len(ind)

    for k in range(1, time_stop + 1):
        sim_idx = entry_idx + k
        if sim_idx >= n:
            # Fin de datos
            exit_p = float(ind.iloc[-1]["Close"])
            return {"exit_price": exit_p, "reason": "END", "days_held": k,
                    "ret_pct": (exit_p - entry_price) / entry_price * 100}

        row   = ind.iloc[sim_idx]
        low_  = float(row["Low"])
        cls_  = float(row["Close"])
        crsi_ = row["CONNORS_RSI"]

        # 1. Stop loss
        if low_ <= sl_price:
            return {"exit_price": sl_price, "reason": "SL", "days_held": k,
                    "ret_pct": (sl_price - entry_price) / entry_price * 100}

        # 2. CRSI_EXIT
        if not pd.isna(crsi_) and crsi_ > exit_crsi:
            return {"exit_price": cls_, "reason": "CRSI_EXIT", "days_held": k,
                    "ret_pct": (cls_ - entry_price) / entry_price * 100}

        # 3. Time stop
        if k == time_stop:
            return {"exit_price": cls_, "reason": "TSTO", "days_held": k,
                    "ret_pct": (cls_ - entry_price) / entry_price * 100}

    # No debería llegar aquí
    return {"exit_price": float(ind.iloc[-1]["Close"]), "reason": "END", "days_held": time_stop,
            "ret_pct": 0.0}


def scan_signals(raw: dict, spy: pd.DataFrame, start_date: str, end_date: str) -> list:
    """
    Escanea señales históricas de entrada para todos los tickers.
    La señal se computa en D-1 (LOOKAHEAD_FIX); la entrada ocurriría en D.

    Params:
        raw:        {ticker: DataFrame OHLCV}.
        spy:        DataFrame OHLCV de SPY.
        start_date: Inicio del período de análisis.
        end_date:   Fin del período de análisis.
    Returns:
        Lista de dicts con una entrada por señal detectada.
    """
    import config
    from modules.connors_rsi import compute_connors

    sl_pct    = config.CONNORS_STOP_LOSS
    exit_crsi = config.CONNORS_EXIT_CRSI
    time_stop = config.CONNORS_TIME_STOP
    entry_lim = config.CONNORS_ENTRY_LIMIT
    commission = config.COMMISSION

    start_ts = pd.Timestamp(start_date)
    end_ts   = pd.Timestamp(end_date)

    # Indicadores SPY (para filtro SMA200 de mercado)
    try:
        spy_ind = compute_connors(spy)
    except Exception:
        console.print("[red]Error al computar indicadores SPY. Abortando.[/red]")
        return []

    all_signals = []
    n_tickers   = 0

    for ticker, df in raw.items():
        if df is None or df.empty or len(df) < 220:
            continue
        try:
            ind = compute_connors(df)
        except Exception:
            continue

        # Señal vectorizada en D-1: periodo relevante
        mask_period = (ind.index >= start_ts - pd.Timedelta(days=5)) & (ind.index <= end_ts)
        ind_p = ind[mask_period].copy()
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

        # Alinear con SPY
        signal_days = ind_p.index[ind_p["signal"]]
        if signal_days.empty:
            continue

        n_tickers += 1

        for sig_day in signal_days:
            # sig_day = D-1 (día en que se computa la señal)
            if sig_day < start_ts or sig_day > end_ts:
                continue

            # Filtro SPY en sig_day
            if sig_day not in spy_ind.index:
                continue
            spy_row = spy_ind.loc[sig_day]
            if pd.isna(spy_row["SMA200"]) or spy_row["Close"] <= spy_row["SMA200"]:
                continue

            close_dm1 = float(ind.loc[sig_day, "Close"])
            limit_p   = close_dm1 * (1.0 - entry_lim)

            # Índice de D (día de entrada) en el DataFrame original
            try:
                iloc_dm1  = ind.index.get_loc(sig_day)
            except KeyError:
                continue
            entry_idx = iloc_dm1 + 1   # D
            if entry_idx >= len(ind):
                continue

            entry_day = ind.index[entry_idx]
            row_D     = ind.iloc[entry_idx]

            # Precios D a D+3
            prices = {}
            for off in range(4):
                idx_ = entry_idx + off
                if idx_ < len(ind):
                    r_ = ind.iloc[idx_]
                    prices[f"d{off}_open"]  = float(r_["Open"])
                    prices[f"d{off}_close"] = float(r_["Close"])
                    prices[f"d{off}_low"]   = float(r_["Low"])
                    prices[f"d{off}_high"]  = float(r_["High"])
                else:
                    prices[f"d{off}_open"]  = None
                    prices[f"d{off}_close"] = None
                    prices[f"d{off}_low"]   = None
                    prices[f"d{off}_high"]  = None

            # ── Estrategia D_nofill (sin fill sim) ──────────────────────────
            sim_d_no = _simulate_exit(ind, entry_idx, limit_p, sl_pct, exit_crsi, time_stop)

            # ── Estrategia D_fillsim ─────────────────────────────────────────
            d0_low  = prices.get("d0_low")
            d0_open = prices.get("d0_open")
            if d0_low is not None and d0_low <= limit_p:
                # fill
                fill_p   = d0_open if d0_open is not None and d0_open <= limit_p else limit_p
                sim_d_fs = _simulate_exit(ind, entry_idx, fill_p, sl_pct, exit_crsi, time_stop)
                d_fs_lost = False
            else:
                sim_d_fs  = None
                d_fs_lost = True

            # ── Estrategia D+1 ───────────────────────────────────────────────
            idx_d1 = entry_idx + 1
            if idx_d1 < len(ind) and prices.get("d1_close") is not None:
                ep_d1    = prices["d1_close"]
                sim_d1   = _simulate_exit(ind, idx_d1, ep_d1, sl_pct, exit_crsi, time_stop)
                d1_lost  = False
            else:
                sim_d1   = None
                d1_lost  = True

            # ── Estrategia D+2 ───────────────────────────────────────────────
            idx_d2 = entry_idx + 2
            if idx_d2 < len(ind) and prices.get("d2_close") is not None:
                ep_d2    = prices["d2_close"]
                sim_d2   = _simulate_exit(ind, idx_d2, ep_d2, sl_pct, exit_crsi, time_stop)
                d2_lost  = False
            else:
                sim_d2   = None
                d2_lost  = True

            all_signals.append({
                "ticker":      ticker,
                "signal_date": sig_day,
                "entry_date":  entry_day,
                "close_dm1":   close_dm1,
                "limit_price": limit_p,
                "crsi_dm1":    float(ind.loc[sig_day, "CONNORS_RSI"]),
                **prices,
                # Estrategia D_nofill
                "d_no_ret":    sim_d_no["ret_pct"] - 2 * commission * 100,
                "d_no_reason": sim_d_no["reason"],
                "d_no_days":   sim_d_no["days_held"],
                # Estrategia D_fillsim
                "d_fs_lost":   d_fs_lost,
                "d_fs_ret":    (sim_d_fs["ret_pct"] - 2 * commission * 100) if sim_d_fs else None,
                "d_fs_reason": sim_d_fs["reason"] if sim_d_fs else None,
                # Estrategia D+1
                "d1_lost":     d1_lost,
                "d1_entry_p":  prices.get("d1_close"),
                "d1_ret":      (sim_d1["ret_pct"] - 2 * commission * 100) if sim_d1 else None,
                "d1_reason":   sim_d1["reason"] if sim_d1 else None,
                "d1_days":     sim_d1["days_held"] if sim_d1 else None,
                # Estrategia D+2
                "d2_lost":     d2_lost,
                "d2_entry_p":  prices.get("d2_close"),
                "d2_ret":      (sim_d2["ret_pct"] - 2 * commission * 100) if sim_d2 else None,
                "d2_reason":   sim_d2["reason"] if sim_d2 else None,
                "d2_days":     sim_d2["days_held"] if sim_d2 else None,
            })

    console.print(f"  {n_tickers} tickers procesados · {len(all_signals)} señales totales")
    return all_signals


# ── Métricas por estrategia ───────────────────────────────────────────────────

def _metrics(signals: list, ret_key: str, lost_key: str | None = None) -> dict:
    """
    Calcula métricas de retorno para una estrategia sobre un conjunto de señales.

    Params:
        signals:  lista de dicts de señales.
        ret_key:  clave del retorno en cada dict.
        lost_key: clave booleana "señal perdida"; None si no aplica.
    Returns:
        Dict de métricas.
    """
    n_total = len(signals)
    if lost_key:
        lost  = sum(1 for s in signals if s.get(lost_key))
        valid = [s for s in signals if not s.get(lost_key) and s.get(ret_key) is not None]
    else:
        lost  = 0
        valid = [s for s in signals if s.get(ret_key) is not None]

    rets  = [s[ret_key] for s in valid]
    n_win = sum(1 for r in rets if r > 0)

    return {
        "n_total":  n_total,
        "n_valid":  len(valid),
        "n_lost":   lost,
        "pct_lost": lost / n_total * 100 if n_total else 0,
        "mean":     statistics.mean(rets)   if rets else None,
        "median":   statistics.median(rets) if rets else None,
        "win_rate": n_win / len(rets) * 100 if rets else None,
    }


# ── Sección: precio en D a D+3 ───────────────────────────────────────────────

def _section_price_path(signals: list) -> None:
    """
    Para el conjunto completo de señales, muestra la evolución de precios
    en D, D+1, D+2, D+3 vs Close[D-1].

    Params:
        signals: lista completa de señales.
    """
    console.print("\n[bold cyan]1 · Evolución de precio en D a D+3 vs Close[D-1][/bold cyan]")
    console.print(
        "  [dim]Precio de referencia: Close[D-1] (el usado para generar la señal).\n"
        "  Se muestran Close y Low de cada día.[/dim]\n"
    )

    DAYS = [0, 1, 2, 3]

    tbl = Table(box=box.ROUNDED, border_style="cyan")
    tbl.add_column("Día",              justify="center", style="bold")
    tbl.add_column("Close medio %",    justify="right")
    tbl.add_column("Low medio %",      justify="right")
    tbl.add_column("% señales Close<0", justify="right")
    tbl.add_column("% señales Low<0",   justify="right")
    tbl.add_column("N señales",         justify="right")

    for d in DAYS:
        ck = f"d{d}_close"
        lk = f"d{d}_low"
        ref_k = "close_dm1"
        valid = [s for s in signals if s.get(ck) and s.get(ref_k)]
        if not valid:
            continue
        closes_pct = [(s[ck] - s[ref_k]) / s[ref_k] * 100 for s in valid]
        lows_pct   = [(s[lk] - s[ref_k]) / s[ref_k] * 100 for s in valid if s.get(lk)]
        pct_close_neg = sum(1 for x in closes_pct if x < 0) / len(closes_pct) * 100
        pct_low_neg   = sum(1 for x in lows_pct   if x < 0) / len(lows_pct)   * 100 if lows_pct else 0

        tbl.add_row(
            f"D+{d}",
            f"{statistics.mean(closes_pct):+.2f}%",
            f"{statistics.mean(lows_pct):+.2f}%"   if lows_pct else "—",
            f"{pct_close_neg:.1f}%",
            f"{pct_low_neg:.1f}%",
            str(len(valid)),
        )

    console.print(tbl)

    # ¿Qué día toca el Low mínimo?
    console.print("\n  [bold]¿Qué día (D, D+1, D+2) toca el Low mínimo?[/bold]")
    counts = defaultdict(int)
    dd_vals = []
    for s in signals:
        lows = {d: s.get(f"d{d}_low") for d in [0, 1, 2]}
        lows_valid = {d: v for d, v in lows.items() if v is not None}
        if not lows_valid:
            continue
        best_d = min(lows_valid, key=lows_valid.get)
        counts[best_d] += 1
        ref = s.get("close_dm1")
        if ref:
            dd_vals.append((min(lows_valid.values()) - ref) / ref * 100)

    total_c = sum(counts.values())
    for d in [0, 1, 2]:
        c = counts.get(d, 0)
        console.print(f"    D+{d}: {c:5d}  ({c/total_c*100:.1f}%)")

    if dd_vals:
        console.print(
            f"\n  Caída adicional hasta el Low mínimo (vs Close[D-1]):\n"
            f"    Media:   {statistics.mean(dd_vals):+.2f}%\n"
            f"    Mediana: {statistics.median(dd_vals):+.2f}%"
        )


# ── Sección: comparación de estrategias ──────────────────────────────────────

STRATEGIES = [
    ("D_nofill",  "d_no_ret",  None,      "Entrada en D — limit (sin fill check)"),
    ("D_fillsim", "d_fs_ret",  "d_fs_lost","Entrada en D — limit + FILL_SIM"),
    ("D+1_close", "d1_ret",    "d1_lost",  "Entrada en D+1 — Close[D+1]"),
    ("D+2_close", "d2_ret",    "d2_lost",  "Entrada en D+2 — Close[D+2]"),
]


def _strategy_table(signals: list, title: str) -> None:
    """
    Imprime tabla de comparación de estrategias de entrada.

    Params:
        signals: lista de señales (puede ser IS, OOS o completo).
        title:   título de la tabla.
    """
    tbl = Table(
        title=title, box=box.ROUNDED, border_style="cyan",
    )
    tbl.add_column("Estrategia",       justify="left",  style="bold")
    tbl.add_column("N usadas",         justify="right")
    tbl.add_column("% perdidas",       justify="right")
    tbl.add_column("Retorno medio",    justify="right")
    tbl.add_column("Retorno mediana",  justify="right")
    tbl.add_column("Win rate",         justify="right")

    for code, ret_k, lost_k, label in STRATEGIES:
        m = _metrics(signals, ret_k, lost_k)
        c_m = "green" if m["mean"] is not None and m["mean"] > 0 else "red"
        c_l = "red"   if m["pct_lost"] > 10 else "dim"
        tbl.add_row(
            label,
            str(m["n_valid"]),
            f"[{c_l}]{m['pct_lost']:.1f}%[/{c_l}]",
            f"[{c_m}]{m['mean']:+.3f}%[/{c_m}]" if m["mean"] is not None else "—",
            f"{m['median']:+.3f}%" if m["median"] is not None else "—",
            f"{m['win_rate']:.1f}%" if m["win_rate"] is not None else "—",
        )

    console.print(tbl)


def _section_estrategias(signals: list) -> None:
    """
    Compara las cuatro estrategias en IS y OOS, con tabla por bloque.

    Params:
        signals: lista completa de señales.
    """
    console.print("\n[bold cyan]2 · Comparación de estrategias de entrada (IS / OOS)[/bold cyan]")

    sorted_s = sorted(signals, key=lambda s: s["signal_date"])
    cut      = int(len(sorted_s) * IS_RATIO)
    is_s     = sorted_s[:cut]
    oos_s    = sorted_s[cut:]

    cut_date = oos_s[0]["signal_date"] if oos_s else "—"
    console.print(
        f"  IS:  {len(is_s):,} señales  ·  "
        f"OOS: {len(oos_s):,} señales (desde {str(cut_date)[:10]})\n"
    )

    _strategy_table(is_s,  "[bold cyan]IS (entrenamiento 70%)[/bold cyan]")
    console.print()
    _strategy_table(oos_s, "[bold cyan]OOS (validación 30%)[/bold cyan]")

    # Panel de valoración: ¿el patrón sobrevive?
    def _avg(sigs, ret_k, lost_k):
        m = _metrics(sigs, ret_k, lost_k)
        return m["mean"]

    lines = []
    ref_is  = _avg(is_s,  "d_no_ret", None)
    ref_oos = _avg(oos_s, "d_no_ret", None)

    for code, ret_k, lost_k, label in STRATEGIES[1:]:
        is_m  = _avg(is_s,  ret_k, lost_k)
        oos_m = _avg(oos_s, ret_k, lost_k)
        if is_m is None or ref_is is None:
            continue
        adv_is  = (is_m  - ref_is)  if is_m  is not None else None
        adv_oos = (oos_m - ref_oos) if oos_m is not None else None
        c_is  = "green" if adv_is  is not None and adv_is  > 0 else "red"
        c_oos = "green" if adv_oos is not None and adv_oos > 0 else "red"

        sobrevive = (adv_is is not None and adv_is > 0 and
                     adv_oos is not None and adv_oos > 0)
        verdict = "[green]patrón real[/green]" if sobrevive else "[red]sobreajuste o ruido[/red]"

        lines.append(
            f"  {label:40s}  IS: [{c_is}]{adv_is:+.3f}pp[/{c_is}]   "
            f"OOS: [{c_oos}]{adv_oos:+.3f}pp[/{c_oos}]   → {verdict}"
        )

    if lines:
        console.print()
        console.print(Panel(
            Text.from_markup("\n".join(lines)),
            title="[bold cyan]¿La ventaja IS persiste en OOS? (vs D_nofill)[/bold cyan]",
            border_style="cyan", padding=(0, 2),
        ))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """Punto de entrada del análisis de timing de entrada."""
    args = _parse_args()

    import config
    from modules.connors_rsi import _download_universe
    from modules import yf_cache

    console.print(
        f"\n[bold cyan]Análisis de timing de entrada (D vs D+1 vs D+2)[/bold cyan]\n"
        f"  Período señales: {args.start} → {args.end}\n"
        f"  ENTRY_LIMIT={config.CONNORS_ENTRY_LIMIT} · SL={config.CONNORS_STOP_LOSS} "
        f"· TIME_STOP={config.CONNORS_TIME_STOP} · EXIT_CRSI={config.CONNORS_EXIT_CRSI}\n"
        f"  Estrategia D base: sin FILL_SIM (universo completo de señales)\n"
    )

    if not args.skip_download:
        yf_cache.clear_session()

    # Descarga: necesitamos historial desde ~1997 para SMA200 (incluso si análisis desde 2010)
    # ext_start: start - ~420 días de calendario para warmup indicadores
    from datetime import datetime, timedelta
    ext_start = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")

    if args.skip_download and not yf_cache.session_data:
        console.print("[red]--skip-download activo pero session_data vacío. Descargando de todas formas.[/red]")
        args.skip_download = False

    if not args.skip_download:
        console.print("[cyan]Descargando datos...[/cyan]")
        raw, spy = _download_universe(start=ext_start, end=args.end)
    else:
        raw = {t: df for t, df in yf_cache.session_data.items() if t != "SPY"}
        spy = yf_cache.get_session("SPY")

    if not raw or spy is None:
        console.print("[red]Sin datos. Abortando.[/red]")
        return

    console.print("[cyan]Escaneando señales históricas...[/cyan]")
    signals = scan_signals(raw, spy, args.start, args.end)

    if not signals:
        console.print("[red]Sin señales detectadas. Abortando.[/red]")
        return

    console.print(f"  Total señales: {len(signals):,}\n")

    _section_price_path(signals)
    _section_estrategias(signals)

    console.print("\n[bold green]Análisis completado.[/bold green]\n")


if __name__ == "__main__":
    main()
