# -*- coding: utf-8 -*-
"""
Sweep uniforme de CONNORS_ENTRY_LIMIT sin prompt interactivo.
Uso:
    C:\trading_env\Scripts\python scripts\run_sweep.py
    C:\trading_env\Scripts\python scripts\run_sweep.py --fill-sim
    C:\trading_env\Scripts\python scripts\run_sweep.py --start 2010-01-01 --end 2026-09-27
"""

import sys
import os
import argparse
from pathlib import Path
from datetime import date

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

_DEFAULT_VALORES = [round(0.005 + i * 0.001, 3) for i in range(26)]  # 0.005–0.030


def _parse_args():
    today = date.today().strftime("%Y-%m-%d")
    p = argparse.ArgumentParser(description="Sweep CONNORS_ENTRY_LIMIT")
    p.add_argument("--start", default=getattr(config, "DEFAULT_START_DATE", "1999-01-01"))
    p.add_argument("--end",   default=(getattr(config, "DEFAULT_END_DATE", None) or today))
    p.add_argument("--fill-sim", action="store_true")
    p.add_argument(
        "--valores",
        default=",".join(f"{v:.3f}" for v in _DEFAULT_VALORES),
    )
    return p.parse_args()


def _smooth(vals: list) -> list:
    result = []
    for i in range(len(vals)):
        nbrs = vals[max(0, i - 1): min(len(vals), i + 2)]
        result.append(sum(nbrs) / len(nbrs))
    return result


def main():
    args = _parse_args()

    if args.fill_sim:
        config.FILL_SIM = True

    try:
        valores = [float(v.strip()) for v in args.valores.split(",") if v.strip()]
    except ValueError as exc:
        console.print(f"[red]Valores inválidos: {exc}[/red]")
        return

    original_limit = config.CONNORS_ENTRY_LIMIT
    fill_tag = "[yellow][FILL_SIM=True][/yellow]" if getattr(config, "FILL_SIM", False) else "[dim][FILL_SIM=False][/dim]"

    console.print(f"\n[bold cyan]Sweep CONNORS_ENTRY_LIMIT[/bold cyan]  {fill_tag}")
    console.print(f"  Período   : {args.start} → {args.end}")
    console.print(f"  Valores   : {len(valores)} puntos ({valores[0]:.3f} – {valores[-1]:.3f}, paso 0.001)")
    console.print()

    from modules.connors_rsi import run_connors_backtest
    from modules import yf_cache as _cache
    _cache.clear_session()

    resultados = []
    first_run = True
    for val in valores:
        config.CONNORS_ENTRY_LIMIT = val
        res = run_connors_backtest(
            start_date=args.start,
            end_date=args.end,
            skip_download=(not first_run),
            export=False,
            verbose=False,
        )
        first_run = False
        cap_i  = config.CONNORS_CAPITAL
        cap_f  = res.get("capital_final", cap_i)
        trades = res.get("trades", [])
        n      = len(trades)
        pnl    = (cap_f - cap_i) / cap_i * 100
        n_win  = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
        wr     = n_win / n * 100 if n > 0 else 0.0
        resultados.append((val, pnl, cap_f, n, wr))
        color = "green" if pnl >= 0 else "red"
        console.print(
            f"  {val:.3f}  [{color}]{pnl:+.1f}%[/{color}]"
            f"  cap {cap_f:,.0f} €  {n} trades  {wr:.0f}% win"
        )

    config.CONNORS_ENTRY_LIMIT = original_limit

    # Suavizado 3 vecinos
    pnl_vals = [r[1] for r in resultados]
    smoothed = _smooth(pnl_vals)
    best_idx = smoothed.index(max(smoothed))

    # Tabla completa
    table = Table(
        title=f"[bold cyan]Sweep CONNORS_ENTRY_LIMIT ({args.start} → {args.end})[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("ENTRY_LIMIT", justify="center", style="bold")
    table.add_column("Rentab.",     justify="right")
    table.add_column("Suavizada*",  justify="right")
    table.add_column("Cap.final €", justify="right")
    table.add_column("Trades",      justify="right")
    table.add_column("Win Rate",    justify="right")

    for i, (val, pnl, cap_f, n, wr) in enumerate(resultados):
        color  = "green" if pnl >= 0 else "red"
        marker = " ◄" if i == best_idx else ""
        table.add_row(
            f"{val:.3f}",
            f"[{color}]{pnl:+.1f}%[/{color}]",
            f"{smoothed[i]:+.1f}%{marker}",
            f"{cap_f:,.0f}",
            str(n),
            f"{wr:.0f}%",
        )

    console.print()
    console.print(table)

    # Lista compacta
    lines = []
    for i, (val, pnl, _, _, _) in enumerate(resultados):
        color = "green" if pnl >= 0 else "red"
        mark  = " ◄" if i == best_idx else ""
        lines.append(f"  CONNORS_ENTRY_LIMIT({val:.3f}) = [{color}]{pnl:+.1f}%[/{color}]{mark}")
    console.print()
    console.print(Panel("\n".join(lines), title="[bold]Resultados compactos[/bold]",
                        border_style="dim", padding=(0, 2)))

    # Robustez
    best_pnl = resultados[best_idx][1]
    best_smt = smoothed[best_idx]
    delta    = best_pnl - best_smt
    peak_smt = max(smoothed)
    meseta   = [resultados[i][0] for i, s in enumerate(smoothed) if s >= 0.8 * peak_smt]
    warn = (
        "[yellow]⚠ Pico posiblemente ruidoso[/yellow]" if abs(delta) > 4
        else "[green]✓ Pico consistente[/green]"
    )
    console.print()
    console.print(Panel(
        Text.from_markup(
            f"  Óptimo (suavizado)      : ENTRY_LIMIT={resultados[best_idx][0]:.3f} → {best_smt:+.1f}%\n"
            f"  Rentab. bruta en óptimo : {best_pnl:+.1f}%\n"
            f"  Delta bruto-suavizado   : {delta:+.1f}pp  {warn}\n"
            f"  Meseta (≥80% pico smt)  : {[f'{v:.3f}' for v in meseta]}\n"
            f"\n"
            f"  * Suavizado = media(valor, vecino anterior, vecino siguiente)"
        ),
        title="[bold cyan]Análisis de robustez[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


if __name__ == "__main__":
    main()
