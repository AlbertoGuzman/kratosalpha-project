# -*- coding: utf-8 -*-
"""
Validación fuera de muestra (out-of-sample) de CONNORS_ENTRY_LIMIT.

Proceso:
  1. Barre ENTRY_LIMIT sobre el primer periodo (in-sample) y encuentra el óptimo.
  2. Aplica ese mismo valor óptimo al segundo periodo (out-of-sample).
  3. Compara in-sample vs out-of-sample para detectar sobreajuste.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\validate_oos.py
    C:\\trading_env\\Scripts\\python scripts\\validate_oos.py \\
        --is-start 1999-01-01 --is-end 2012-06-30 \\
        --oos-start 2012-07-01 --oos-end 2026-09-27 \\
        --fill-sim
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
    p = argparse.ArgumentParser(description="Validación OOS de CONNORS_ENTRY_LIMIT")
    p.add_argument("--is-start",  default="1999-01-01", help="Inicio in-sample")
    p.add_argument("--is-end",    default="2012-06-30", help="Fin in-sample")
    p.add_argument("--oos-start", default="2012-07-01", help="Inicio out-of-sample")
    p.add_argument("--oos-end",   default=today,        help="Fin out-of-sample")
    p.add_argument("--fill-sim",  action="store_true",  help="Activa FILL_SIM=True")
    p.add_argument(
        "--valores",
        default=",".join(f"{v:.3f}" for v in _DEFAULT_VALORES),
        help="Lista de ENTRY_LIMIT separados por coma",
    )
    return p.parse_args()


def _run_sweep(valores: list[float], start: str, end: str,
               first: bool = True) -> list[tuple]:
    """
    Ejecuta el backtest para cada valor.
    first=True → descarga datos; first=False → reutiliza caché.
    Devuelve [(val, pnl_pct, n_trades, win_rate)].
    """
    from modules.connors_rsi import run_connors_backtest

    resultados = []
    first_run = True
    for val in valores:
        config.CONNORS_ENTRY_LIMIT = val
        res = run_connors_backtest(
            start_date=start,
            end_date=end,
            skip_download=(not first_run),
            export=False,
            verbose=False,
        )
        first_run = False
        cap_i    = config.CONNORS_CAPITAL
        cap_f    = res.get("capital_final", cap_i)
        trades   = res.get("trades", [])
        n        = len(trades)
        pnl      = (cap_f - cap_i) / cap_i * 100
        n_win    = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
        wr       = n_win / n * 100 if n > 0 else 0.0
        resultados.append((val, pnl, n, wr))
        color = "green" if pnl >= 0 else "red"
        console.print(
            f"  [dim]{val:.3f}[/dim]  [{color}]{pnl:+.1f}%[/{color}]"
            f"  {n} trades  {wr:.0f}% win"
        )
    return resultados


def _smooth(vals: list[float]) -> list[float]:
    """Media de 3 vecinos (centro ± 1)."""
    result = []
    for i in range(len(vals)):
        nbrs = vals[max(0, i - 1): min(len(vals), i + 2)]
        result.append(sum(nbrs) / len(nbrs))
    return result


def _best(resultados: list[tuple], smooth: list[float]) -> tuple[float, float, int, float]:
    """Devuelve (val, pnl, n, wr) del máximo suavizado."""
    idx = smooth.index(max(smooth))
    return resultados[idx]


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
    fill_tag = "[yellow][FILL_SIM][/yellow]" if getattr(config, "FILL_SIM", False) else ""

    console.print(
        f"\n[bold cyan]Validación Out-of-Sample CONNORS_ENTRY_LIMIT[/bold cyan] {fill_tag}"
    )
    console.print(f"  In-sample      : {args.is_start} → {args.is_end}")
    console.print(f"  Out-of-sample  : {args.oos_start} → {args.oos_end}")
    console.print(f"  Valores barridos: {len(valores)}")

    # ── 1. Barrido in-sample ─────────────────────────────────────────────────
    console.print(f"\n[bold]Fase 1 — Barrido in-sample ({args.is_start} → {args.is_end})[/bold]")
    from modules import yf_cache as _cache
    _cache.clear_session()
    is_res = _run_sweep(valores, args.is_start, args.is_end, first=True)

    pnl_is   = [r[1] for r in is_res]
    smooth_is = _smooth(pnl_is)
    opt       = _best(is_res, smooth_is)
    opt_val, opt_pnl_is, opt_n_is, opt_wr_is = opt

    console.print(f"\n  → Óptimo in-sample (suavizado): ENTRY_LIMIT={opt_val:.3f}  "
                  f"({opt_pnl_is:+.1f}%  {opt_n_is} trades  {opt_wr_is:.0f}% win)")

    # ── 2. Validación out-of-sample con el valor óptimo ───────────────────────
    console.print(
        f"\n[bold]Fase 2 — Out-of-sample con ENTRY_LIMIT={opt_val:.3f} "
        f"({args.oos_start} → {args.oos_end})[/bold]"
    )
    _cache.clear_session()
    config.CONNORS_ENTRY_LIMIT = opt_val
    from modules.connors_rsi import run_connors_backtest
    res_oos = run_connors_backtest(
        start_date=args.oos_start,
        end_date=args.oos_end,
        export=False,
        verbose=False,
    )
    cap_i   = config.CONNORS_CAPITAL
    cap_f   = res_oos.get("capital_final", cap_i)
    trades  = res_oos.get("trades", [])
    n_oos   = len(trades)
    pnl_oos = (cap_f - cap_i) / cap_i * 100
    n_win   = sum(1 for t in trades if (t.get("net_pnl") or 0) > 0)
    wr_oos  = n_win / n_oos * 100 if n_oos > 0 else 0.0

    # ── 3. Benchmark OOS: valor neutro (ENTRY_LIMIT config original) ──────────
    console.print(
        f"\n[bold]Fase 3 — Benchmark OOS con ENTRY_LIMIT={original_limit:.3f} "
        f"(valor original de config)[/bold]"
    )
    config.CONNORS_ENTRY_LIMIT = original_limit
    res_bench = run_connors_backtest(
        start_date=args.oos_start,
        end_date=args.oos_end,
        skip_download=True,
        export=False,
        verbose=False,
    )
    cap_f_b  = res_bench.get("capital_final", cap_i)
    trades_b = res_bench.get("trades", [])
    n_b      = len(trades_b)
    pnl_b    = (cap_f_b - cap_i) / cap_i * 100
    n_win_b  = sum(1 for t in trades_b if (t.get("net_pnl") or 0) > 0)
    wr_b     = n_win_b / n_b * 100 if n_b > 0 else 0.0

    config.CONNORS_ENTRY_LIMIT = original_limit

    # ── Tabla comparativa ────────────────────────────────────────────────────
    table = Table(
        title="[bold cyan]Resultados Out-of-Sample[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Escenario",    justify="left",   style="bold")
    table.add_column("Período",      justify="center")
    table.add_column("ENTRY_LIMIT",  justify="center")
    table.add_column("Rentab.",      justify="right")
    table.add_column("Trades",       justify="right")
    table.add_column("Win Rate",     justify="right")

    def _row(label, period, val, pnl, n, wr):
        color = "green" if pnl >= 0 else "red"
        table.add_row(label, period, f"{val:.3f}",
                      f"[{color}]{pnl:+.1f}%[/{color}]", str(n), f"{wr:.0f}%")

    _row("In-sample (óptimo)", f"{args.is_start}→{args.is_end}",
         opt_val, opt_pnl_is, opt_n_is, opt_wr_is)
    _row("Out-of-sample (óptimo IS)", f"{args.oos_start}→{args.oos_end}",
         opt_val, pnl_oos, n_oos, wr_oos)
    _row("Out-of-sample (benchmark)", f"{args.oos_start}→{args.oos_end}",
         original_limit, pnl_b, n_b, wr_b)

    console.print()
    console.print(table)

    # ── Diagnóstico de sobreajuste ───────────────────────────────────────────
    degradacion = opt_pnl_is - pnl_oos
    mejora_vs_bench = pnl_oos - pnl_b

    if abs(degradacion) < 5 and pnl_oos >= 0:
        veredicto = (
            f"[green]✓ Sin degradación significativa fuera de muestra "
            f"(caída {degradacion:+.1f}pp). "
            f"Señal de robustez razonable.[/green]"
        )
    elif degradacion > 15:
        veredicto = (
            f"[red]✗ Degradación grave fuera de muestra ({degradacion:+.1f}pp). "
            f"El parámetro está sobreajustado al periodo in-sample. "
            f"No cambiar ENTRY_LIMIT basándose en este análisis.[/red]"
        )
    else:
        veredicto = (
            f"[yellow]⚠ Degradación moderada fuera de muestra ({degradacion:+.1f}pp). "
            f"Posible sobreajuste parcial. Ampliar el periodo o usar walk-forward "
            f"antes de adoptar el valor óptimo.[/yellow]"
        )

    console.print()
    console.print(Panel(
        Text.from_markup(
            f"  Caída IS→OOS   : {degradacion:+.1f}pp\n"
            f"  OOS vs bench   : {mejora_vs_bench:+.1f}pp "
            f"({'mejor' if mejora_vs_bench > 0 else 'peor'} que ENTRY_LIMIT original)\n\n"
            f"  {veredicto}"
        ),
        title="[bold cyan]Diagnóstico de sobreajuste[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


if __name__ == "__main__":
    main()
