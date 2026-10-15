# -*- coding: utf-8 -*-
"""
Verifica los tres invariantes del backtest con FILL_SIM=True:

  1. Nunca más de CONNORS_MAX_POS posiciones abiertas simultáneamente.
  2. Cuantificación del efecto de sustitución: cuántas de las N operaciones
     son reintentos (ticker que falló fill un día previo) vs sustitutos vs
     originales sin ningún fallo previo.
  3. Sin solapamiento de posiciones para el mismo ticker (entry/exit overlap)
     y sin entrada doble el mismo día para el mismo ticker.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\validate_backtest_slots.py
    C:\\trading_env\\Scripts\\python scripts\\validate_backtest_slots.py --start 2026-06-05 --end 2026-09-26
"""

import sys
import os
import argparse
from pathlib import Path
from collections import defaultdict
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


def _parse_args():
    p = argparse.ArgumentParser(description="Valida invariantes del backtest ConnorsRSI")
    p.add_argument("--start", default=config.DEFAULT_START_DATE)
    p.add_argument("--end",   default=(config.DEFAULT_END_DATE or date.today().strftime("%Y-%m-%d")))
    p.add_argument("--fill-sim", action="store_true",
                   help="Fuerza FILL_SIM=True independientemente de config")
    p.add_argument("--no-fill-sim", action="store_true",
                   help="Fuerza FILL_SIM=False aunque config lo tenga en True")
    p.add_argument("--entry-limit", type=float, default=None,
                   help="Override de CONNORS_ENTRY_LIMIT (ej: 0.010)")
    return p.parse_args()


def _check_invariant_1(daily_pos: dict, max_pos: int) -> tuple[bool, list]:
    """
    Invariante 1: nunca más de max_pos posiciones simultáneas.
    Retorna (ok, lista de días con violación).
    """
    violaciones = []
    for d, tickers in sorted(daily_pos.items()):
        if len(tickers) > max_pos:
            violaciones.append((d, len(tickers), tickers))
    return len(violaciones) == 0, violaciones


def _check_invariant_3(trades: list) -> tuple[bool, list, list]:
    """
    Invariante 3a: sin solapamiento de fecha entry-exit para el mismo ticker.
    Invariante 3b: sin entrada doble el mismo día para el mismo ticker.
    Retorna (ok, solapamientos, duplicados_dia).
    """
    import pandas as pd

    solapamientos = []
    duplicados_dia = []

    # Agrupa por ticker
    by_ticker = defaultdict(list)
    for t in trades:
        by_ticker[t["ticker"]].append(t)

    for ticker, ops in by_ticker.items():
        ops_sorted = sorted(ops, key=lambda x: pd.Timestamp(x["entry_date"]))
        for i in range(len(ops_sorted)):
            for j in range(i + 1, len(ops_sorted)):
                a = ops_sorted[i]
                b = ops_sorted[j]
                a_entry = pd.Timestamp(a["entry_date"])
                a_exit  = pd.Timestamp(a["exit_date"])
                b_entry = pd.Timestamp(b["entry_date"])
                # Solapamiento: b empieza antes de que a cierre
                if b_entry <= a_exit:
                    solapamientos.append((ticker, a_entry.date(), a_exit.date(),
                                         b_entry.date()))
                # Entrada doble el mismo día
                if a_entry == b_entry:
                    duplicados_dia.append((ticker, a_entry.date()))

    return (len(solapamientos) == 0 and len(duplicados_dia) == 0,
            solapamientos, duplicados_dia)


def _check_invariant_2(entries: list, sim_fails: list, trades: list) -> dict:
    """
    Invariante 2: cuantifica el efecto de sustitución.
    Un 'reintento' es una entrada para ticker T en día D donde T tuvo al menos
    un fallo de fill_sim en algún día anterior D' < D (dentro del mismo
    'episodio': entre la última salida de T y la entrada actual).
    Un 'sustituto' es una entrada para ticker T en día D donde T NO tuvo fallos
    previos pero OTRO ticker sí falló en D (y T llenó el slot que quedó libre).
    Todo lo demás es 'original'.
    """
    import pandas as pd

    # Índice de fallos por ticker → set de fechas de fallo
    fails_by_ticker: dict[str, set] = defaultdict(set)
    for d, ticker in sim_fails:
        fails_by_ticker[ticker].add(d)

    # Fecha de última salida por ticker (para acotar el episodio de fallos)
    last_exit: dict[str, pd.Timestamp] = {}
    for t in sorted(trades, key=lambda x: pd.Timestamp(x["exit_date"])):
        last_exit[t["ticker"]] = pd.Timestamp(t["exit_date"])

    # Fallos que ocurrieron en cada fecha (para detectar sustitutos)
    fails_by_date: dict[pd.Timestamp, set] = defaultdict(set)
    for d, ticker in sim_fails:
        fails_by_date[d].add(ticker)

    n_reintentos  = 0
    n_sustitutos  = 0
    n_originales  = 0
    ejemplos_reintentos = []
    ejemplos_sustitutos = []

    for entry_d, ticker in entries:
        # Busca fallos previos del mismo ticker antes de esta entrada
        prior_fails = {
            d for d in fails_by_ticker.get(ticker, set())
            if d < entry_d
            # Solo fallos en el mismo episodio (después de la última salida)
            and (ticker not in last_exit or d >= last_exit.get(ticker, pd.Timestamp.min))
        }

        # Busca si otro ticker falló el MISMO día de esta entrada
        otros_fallos_hoy = fails_by_date.get(entry_d, set()) - {ticker}

        if prior_fails:
            n_reintentos += 1
            if len(ejemplos_reintentos) < 5:
                ejemplos_reintentos.append((ticker, min(prior_fails).date(), entry_d.date()))
        elif otros_fallos_hoy:
            n_sustitutos += 1
            if len(ejemplos_sustitutos) < 5:
                ejemplos_sustitutos.append((ticker, entry_d.date(),
                                            list(otros_fallos_hoy)[:3]))
        else:
            n_originales += 1

    return {
        "reintentos":  n_reintentos,
        "sustitutos":  n_sustitutos,
        "originales":  n_originales,
        "total":       len(entries),
        "ej_reintentos": ejemplos_reintentos,
        "ej_sustitutos": ejemplos_sustitutos,
    }


def main():
    args = _parse_args()

    if args.fill_sim:
        config.FILL_SIM = True
    if args.no_fill_sim:
        config.FILL_SIM = False
    if args.entry_limit is not None:
        config.CONNORS_ENTRY_LIMIT = args.entry_limit
    fill_sim_activo = getattr(config, "FILL_SIM", False)

    console.print(f"\n[bold cyan]Validación de invariantes del backtest[/bold cyan]")
    console.print(f"  Período  : {args.start} → {args.end}")
    console.print(f"  FILL_SIM : {'[green]True[/green]' if fill_sim_activo else '[yellow]False[/yellow]'}")
    console.print(f"  MAX_POS  : {config.CONNORS_MAX_POS}")
    console.print()

    from modules.connors_rsi import run_connors_backtest

    resultado = run_connors_backtest(
        start_date=args.start,
        end_date=args.end,
        export=False,
        verbose=False,
        collect_debug=True,
    )

    trades    = resultado["trades"]
    daily_pos = resultado["_dbg_daily_pos"]
    sim_fails = resultado["_dbg_sim_fails"]
    entries   = resultado["_dbg_entries"]

    console.print(f"  Operaciones cerradas : {len(trades)}")
    console.print(f"  Entradas registradas : {len(entries)}")
    console.print(f"  Fallos fill_sim      : {len(sim_fails)}")
    console.print()

    # ── Invariante 1 ─────────────────────────────────────────────────────────
    ok1, violaciones = _check_invariant_1(daily_pos, config.CONNORS_MAX_POS)
    if ok1:
        # Muestra distribución de máximos
        max_conc = max((len(v) for v in daily_pos.values()), default=0)
        dias_en_max = sum(1 for v in daily_pos.values() if len(v) == max_conc)
        console.print(Panel(
            Text.from_markup(
                f"  [green]✓ INVARIANTE 1 CUMPLIDA[/green]\n"
                f"  Máximo simultáneo     : {max_conc} (límite: {config.CONNORS_MAX_POS})\n"
                f"  Días en máximo ({max_conc} pos): {dias_en_max}\n"
                f"  Días con 0 posiciones : "
                f"{sum(1 for v in daily_pos.values() if len(v)==0)}\n"
                f"  Días con 1 posición   : "
                f"{sum(1 for v in daily_pos.values() if len(v)==1)}\n"
                f"  Días con 2 posiciones : "
                f"{sum(1 for v in daily_pos.values() if len(v)==2)}\n"
                f"  Días con 3 posiciones : "
                f"{sum(1 for v in daily_pos.values() if len(v)==3)}"
            ),
            title="[bold]Invariante 1 — slots[/bold]",
            border_style="green", padding=(0, 2),
        ))
    else:
        txt = Text.from_markup("[red]✗ INVARIANTE 1 VIOLADA — días con más de "
                               f"{config.CONNORS_MAX_POS} posiciones:[/red]\n")
        for d, cnt, tickers in violaciones[:10]:
            txt.append(f"  {d.date()}  {cnt} posiciones: {tickers}\n", style="red")
        console.print(Panel(txt, title="[bold red]Invariante 1 — FALLO[/bold red]",
                            border_style="red", padding=(0, 2)))

    # ── Invariante 3 ─────────────────────────────────────────────────────────
    ok3, solapamientos, duplicados = _check_invariant_3(trades)
    if ok3:
        console.print(Panel(
            "[green]✓ INVARIANTE 3 CUMPLIDA[/green]\n"
            "  Sin solapamientos de fechas entry/exit para el mismo ticker.\n"
            "  Sin entradas dobles el mismo día para el mismo ticker.",
            title="[bold]Invariante 3 — solapamientos[/bold]",
            border_style="green", padding=(0, 2),
        ))
    else:
        txt = Text()
        if solapamientos:
            txt.append("[red]✗ Solapamientos encontrados:[/red]\n", style="red")
            for ticker, ae, ax, be in solapamientos[:10]:
                txt.append(f"  {ticker}: pos A {ae}→{ax} solapa con pos B entrada {be}\n",
                            style="red")
        if duplicados:
            txt.append("[red]✗ Entradas dobles el mismo día:[/red]\n", style="red")
            for ticker, d in duplicados[:10]:
                txt.append(f"  {ticker} en {d}\n", style="red")
        console.print(Panel(txt, title="[bold red]Invariante 3 — FALLO[/bold red]",
                            border_style="red", padding=(0, 2)))

    # ── Invariante 2 ─────────────────────────────────────────────────────────
    if fill_sim_activo and sim_fails:
        inv2 = _check_invariant_2(entries, sim_fails, trades)
        total = inv2["total"]
        arith_ok = (inv2["originales"] + inv2["reintentos"] + inv2["sustitutos"]) == total

        txt = Text.from_markup(
            f"  Total entradas            : {total}\n"
            f"  Originales (sin fallo previo, slot sin fallo el mismo día): "
            f"[white]{inv2['originales']}[/white]  "
            f"({inv2['originales']/total*100:.1f}%)\n"
            f"  Reintentos (ticker falló fill en día anterior del episodio): "
            f"[yellow]{inv2['reintentos']}[/yellow]  "
            f"({inv2['reintentos']/total*100:.1f}%)\n"
            f"  Sustitutos (otro ticker falló fill ese mismo día): "
            f"[cyan]{inv2['sustitutos']}[/cyan]  "
            f"({inv2['sustitutos']/total*100:.1f}%)\n"
            f"  Suma {inv2['originales']}+{inv2['reintentos']}+{inv2['sustitutos']}"
            f" = {inv2['originales']+inv2['reintentos']+inv2['sustitutos']} "
            + ("[green]✓[/green]\n" if arith_ok else "[red]✗ NO CUADRA[/red]\n")
        )
        if inv2["ej_reintentos"]:
            txt.append("\n  Ejemplos de reintentos:\n", style="dim")
            for ticker, fallo_d, entry_d in inv2["ej_reintentos"]:
                txt.append(f"    {ticker}: fallo {fallo_d} → entrada {entry_d}\n",
                            style="dim yellow")
        if inv2["ej_sustitutos"]:
            txt.append("\n  Ejemplos de sustitutos:\n", style="dim")
            for ticker, entry_d, otros in inv2["ej_sustitutos"]:
                txt.append(
                    f"    {ticker} entró en {entry_d}; fallaron ese día: "
                    f"{[str(o)[:10] for o in otros]}\n",
                    style="dim cyan",
                )
        console.print(Panel(
            txt,
            title="[bold]Invariante 2 — efecto de sustitución[/bold]",
            border_style="cyan", padding=(0, 2),
        ))
    else:
        console.print(Panel(
            "[dim]FILL_SIM inactivo o sin fallos — invariante 2 no aplica.[/dim]",
            title="[bold]Invariante 2[/bold]",
            border_style="dim", padding=(0, 2),
        ))

    console.print()
    if ok1 and ok3:
        console.print("[bold green]✓ Todos los invariantes verificados.[/bold green]")
    else:
        console.print("[bold red]✗ Hay invariantes violados — revisar el backtest.[/bold red]")


if __name__ == "__main__":
    main()
