# -*- coding: utf-8 -*-
"""
Comparación año a año para una lista configurable de CONNORS_ENTRY_LIMIT.

Capital compuesto real: el cierre de un año es la apertura del siguiente.
Tabla: una fila por año, una columna por valor de ENTRY_LIMIT (solo %).

Uso:
    C:\\trading_env\\Scripts\\python scripts\\yearly_comparison.py --fill-sim
    C:\\trading_env\\Scripts\\python scripts\\yearly_comparison.py --fill-sim --valores 0.010,0.017,0.027
    C:\\trading_env\\Scripts\\python scripts\\yearly_comparison.py --fill-sim \\
        --valores 0.005,0.008,0.010,0.011,0.012,0.013,0.014,0.016,0.017,0.018,0.020
"""

import sys
import os
import argparse
import statistics
from pathlib import Path
from datetime import date
from collections import defaultdict

_ROOT = Path(__file__).resolve().parent.parent / "trading_system"
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("TRADING_ENV", "dev")

import config
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

console = Console()

_DEFAULT_VALORES = "0.005,0.008,0.010,0.011,0.012,0.013,0.014,0.016,0.017,0.018,0.020"
IS_LAST   = 2011
OOS_FIRST = 2013


def _parse_args():
    today = date.today().strftime("%Y-%m-%d")
    p = argparse.ArgumentParser(description="Comparación anual por ENTRY_LIMIT")
    p.add_argument("--start",    default=getattr(config, "DEFAULT_START_DATE", "1999-01-01"))
    p.add_argument("--end",      default=(getattr(config, "DEFAULT_END_DATE", None) or today))
    p.add_argument("--fill-sim", action="store_true")
    p.add_argument("--valores",  default=_DEFAULT_VALORES,
                   help="Lista separada por comas de ENTRY_LIMIT a comparar")
    return p.parse_args()


def _yearly_stats(trades: list, cap_initial: float) -> dict:
    """
    Rentabilidad anual con capital compuesto real.
    cap_cierre(Y) = cap_inicio(Y+1).

    Params:
        trades:      lista de dicts de trades del backtest.
        cap_initial: capital inicial.
    Returns:
        {año: {pnl_pct, pnl_abs, n_trades, n_win, win_rate, cap_start, cap_end}}
    """
    import pandas as pd

    by_year = defaultdict(list)
    for t in sorted(trades, key=lambda t: pd.Timestamp(t["exit_date"])):
        by_year[pd.Timestamp(t["exit_date"]).year].append(t)

    result, running_cap = {}, cap_initial
    for yr in sorted(by_year):
        cap_start = running_cap
        ts        = by_year[yr]
        pnl_abs   = sum((t.get("net_pnl") or 0) for t in ts)
        n         = len(ts)
        n_win     = sum(1 for t in ts if (t.get("net_pnl") or 0) > 0)
        cap_end   = cap_start + pnl_abs
        result[yr] = {
            "pnl_pct":  (cap_end - cap_start) / cap_start * 100 if cap_start > 0 else 0.0,
            "pnl_abs":  pnl_abs,
            "n_trades": n,
            "n_win":    n_win,
            "win_rate": n_win / n * 100 if n > 0 else 0.0,
            "cap_start": cap_start,
            "cap_end":   cap_end,
        }
        running_cap = cap_end
    return result


def main():
    """Punto de entrada del script de comparación anual."""
    args   = _parse_args()
    values = [float(v.strip()) for v in args.valores.split(",")]

    if args.fill_sim:
        config.FILL_SIM = True

    fill_tag = (" [yellow][FILL_SIM=True][/yellow]"
                if getattr(config, "FILL_SIM", False) else
                " [dim][FILL_SIM=False][/dim]")

    console.print(
        f"\n[bold cyan]Rentabilidad anual por ENTRY_LIMIT[/bold cyan]{fill_tag}\n"
        f"  Valores ({len(values)}): {', '.join(f'{v:.3f}' for v in values)}\n"
        f"  Capital inicial: {config.CONNORS_CAPITAL:,.0f} € (compuesto real)\n"
        f"  Período: {args.start} → {args.end}\n"
    )

    from modules.connors_rsi import run_connors_backtest
    from modules import yf_cache as _cache
    _cache.clear_session()

    all_results    = {}
    original_limit = config.CONNORS_ENTRY_LIMIT

    for i, val in enumerate(values):
        config.CONNORS_ENTRY_LIMIT = val
        console.print(f"  {val:.3f}...", end=" ")
        res    = run_connors_backtest(
            start_date=args.start, end_date=args.end,
            skip_download=(i > 0), export=False, verbose=False,
        )
        trades = res.get("trades", [])
        cap_f  = res.get("capital_final", config.CONNORS_CAPITAL)
        pnl_t  = (cap_f - config.CONNORS_CAPITAL) / config.CONNORS_CAPITAL * 100
        c      = "green" if pnl_t >= 0 else "red"
        console.print(f"[{c}]{pnl_t:+.1f}%[/{c}]  {len(trades)} trades")
        all_results[val] = _yearly_stats(trades, config.CONNORS_CAPITAL)

    config.CONNORS_ENTRY_LIMIT = original_limit

    all_years   = sorted(set(yr for d in all_results.values() for yr in d))
    annual_pnls = {v: [] for v in values}
    winners     = {v: 0  for v in values}

    # ── Tabla principal (% anual) ────────────────────────────────────────────
    tbl = Table(
        title="[bold cyan]% anual por ENTRY_LIMIT (capital compuesto real)[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    tbl.add_column("Año",    justify="center", style="bold", no_wrap=True)
    tbl.add_column("Bloque", justify="center", no_wrap=True)
    for v in values:
        tbl.add_column(f"{v:.3f}", justify="right", no_wrap=True)
    tbl.add_column("Gana", justify="center", no_wrap=True)

    for yr in all_years:
        block = (
            "[dim]IS[/dim]"    if yr <= IS_LAST  else
            "[cyan]SPLIT[/cyan]" if yr < OOS_FIRST else
            "[yellow]OOS[/yellow]"
        )
        pnls = {v: all_results[v].get(yr, {}).get("pnl_pct", 0.0) for v in values}
        best = max(pnls, key=pnls.get)
        winners[best] += 1
        for v in values:
            annual_pnls[v].append(pnls[v])

        cells = [str(yr), block]
        for v in values:
            p  = pnls[v]
            c  = "green" if p >= 0 else "red"
            b  = " bold" if v == best else ""
            cells.append(f"[{c}{b}]{p:+.1f}%[/{c}{b}]")
        cells.append(f"[bold]{best:.3f}[/bold]")
        tbl.add_row(*cells)

    # Fila TOTAL
    tot_cells = ["[bold]TOTAL[/bold]", ""]
    for v in values:
        yrs_v   = sorted(all_results[v])
        cap_ini = all_results[v][yrs_v[0]]["cap_start"] if yrs_v else config.CONNORS_CAPITAL
        cap_fin = all_results[v][yrs_v[-1]]["cap_end"]  if yrs_v else config.CONNORS_CAPITAL
        pct     = (cap_fin - cap_ini) / cap_ini * 100
        c = "green" if pct >= 0 else "red"
        tot_cells.append(f"[bold {c}]{pct:+.0f}%[/bold {c}]")
    tot_cells.append("")
    tbl.add_row(*tot_cells)

    console.print(tbl)

    # ── Tabla resumen estadístico ────────────────────────────────────────────
    console.print()
    n_yrs = len(all_years)

    stbl = Table(
        title="[bold cyan]Resumen estadístico (capital compuesto)[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    stbl.add_column("Métrica", justify="left", style="bold")
    for v in values:
        stbl.add_column(f"{v:.3f}", justify="right")

    def _tot(v):
        yrs_v   = sorted(all_results[v])
        cap_ini = all_results[v][yrs_v[0]]["cap_start"] if yrs_v else config.CONNORS_CAPITAL
        cap_fin = all_results[v][yrs_v[-1]]["cap_end"]  if yrs_v else config.CONNORS_CAPITAL
        pct     = (cap_fin - cap_ini) / cap_ini * 100
        c = "green" if pct >= 0 else "red"
        return f"[{c}]{pct:+.0f}%[/{c}]"

    def _shr(v):
        m = statistics.mean(annual_pnls[v])
        s = statistics.stdev(annual_pnls[v]) if len(annual_pnls[v]) > 1 else 0
        return f"{m/s:.2f}" if s > 0 else "—"

    stbl.add_row(f"Rentab. acumulada",          *[_tot(v) for v in values])
    stbl.add_row(f"Años donde gana (/{n_yrs})", *[str(winners[v]) for v in values])
    stbl.add_row("Media anual (%)",
                 *[f"{statistics.mean(annual_pnls[v]):+.1f}%" for v in values])
    stbl.add_row("Mediana anual (%)",
                 *[f"{statistics.median(annual_pnls[v]):+.1f}%" for v in values])
    stbl.add_row("Desv. estándar (pp)",
                 *[f"{statistics.stdev(annual_pnls[v]):.1f}" for v in values])
    stbl.add_row("Media / Desv. (Sharpe simple)",
                 *[_shr(v) for v in values])
    stbl.add_row("Peor año (%)",
                 *[f"{min(annual_pnls[v]):+.1f}%" for v in values])
    stbl.add_row("Mejor año (%)",
                 *[f"{max(annual_pnls[v]):+.1f}%" for v in values])
    stbl.add_row("Años negativos",
                 *[str(sum(1 for p in annual_pnls[v] if p < 0)) for v in values])

    console.print(stbl)

    # ── CSV ─────────────────────────────────────────────────────────────────
    csv_path = Path(__file__).parent / "yearly_comparison.csv"
    import csv
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        header = ["año", "bloque"] + [f"{v:.3f}" for v in values] + ["gana"]
        w = csv.writer(f)
        w.writerow(header)
        for yr in all_years:
            block_str = ("IS" if yr <= IS_LAST else "SPLIT" if yr < OOS_FIRST else "OOS")
            pnls = {v: all_results[v].get(yr, {}).get("pnl_pct", 0.0) for v in values}
            best = max(pnls, key=pnls.get)
            w.writerow([yr, block_str] + [f"{pnls[v]:.4f}" for v in values] + [f"{best:.3f}"])
        # Fila totales
        tot_row = ["TOTAL", ""]
        for v in values:
            yrs_v   = sorted(all_results[v])
            cap_ini = all_results[v][yrs_v[0]]["cap_start"] if yrs_v else config.CONNORS_CAPITAL
            cap_fin = all_results[v][yrs_v[-1]]["cap_end"]  if yrs_v else config.CONNORS_CAPITAL
            tot_row.append(f"{(cap_fin - cap_ini) / cap_ini * 100:.4f}")
        tot_row.append("")
        w.writerow(tot_row)
    console.print(f"\n  CSV guardado en [cyan]{csv_path}[/cyan]")

    # ── Panel ventaja IS vs OOS (ref = primer valor de la lista) ─────────────
    ref = values[0]
    is_yrs  = [yr for yr in all_years if yr <= IS_LAST]
    oos_yrs = [yr for yr in all_years if yr >= OOS_FIRST]
    if is_yrs and oos_yrs and len(values) > 1:
        lines = []
        for v in values[1:]:
            is_adv  = (statistics.mean([all_results[v].get(yr, {}).get("pnl_pct", 0) for yr in is_yrs])
                       - statistics.mean([all_results[ref].get(yr, {}).get("pnl_pct", 0) for yr in is_yrs]))
            oos_adv = (statistics.mean([all_results[v].get(yr, {}).get("pnl_pct", 0) for yr in oos_yrs])
                       - statistics.mean([all_results[ref].get(yr, {}).get("pnl_pct", 0) for yr in oos_yrs]))
            c_is  = "green" if is_adv  >= 0 else "red"
            c_oos = "green" if oos_adv >= 0 else "red"
            lines.append(
                f"  {v:.3f} vs {ref:.3f} →  "
                f"IS: [{c_is}]{is_adv:+.1f}pp[/{c_is}]   "
                f"OOS: [{c_oos}]{oos_adv:+.1f}pp[/{c_oos}]"
            )
        console.print(Panel(
            Text.from_markup("\n".join(lines)),
            title=f"[bold cyan]Ventaja IS vs OOS respecto a {ref:.3f}[/bold cyan]",
            border_style="cyan", padding=(0, 2),
        ))


if __name__ == "__main__":
    main()
