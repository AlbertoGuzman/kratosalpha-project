# -*- coding: utf-8 -*-
"""Menú principal del sistema de trading (ConnorsRSI)."""

import argparse
import os
import sys
from pathlib import Path

# ── Selección de entorno (CLI --env) antes de cargar config ──────────────────
# config.py exige `TRADING_ENV` en os.environ antes de importarse. Cuando este
# fichero se ejecuta como script (no como import) parseamos --env y lo seteamos.
# Si alguien hace `import main` desde otro contexto sin TRADING_ENV definido,
# `import config` fallará abajo con un RuntimeError claro — comportamiento
# deseado: un import accidental no debe decidir el entorno.
if __name__ == "__main__":
    _parser = argparse.ArgumentParser(
        description="Sistema de trading ConnorsRSI.",
    )
    _parser.add_argument(
        "--env", choices=["dev", "pre", "pro"], required=True,
        help="Entorno de ejecución (selecciona la BD bajo data/<env>/).",
    )
    _parser.add_argument(
        "--run", choices=["all", "connors"], default=None,
        metavar="{all,connors}",
        help=(
            "Modo desatendido (sin menú): "
            "'all' = autopiloto completo, "
            "'connors' = solo ConnorsRSI."
        ),
    )
    _parsed_args = _parser.parse_args()
    os.environ["TRADING_ENV"] = _parsed_args.env
    _RUN_MODE: str | None = _parsed_args.run
else:
    _RUN_MODE = None

from datetime import date, datetime, timedelta

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.text import Text
from rich import box

sys.path.insert(0, str(Path(__file__).parent))

import config
from modules.logger import get_system_logger, get_trading_logger
from modules.data import load_universe, validate_universe, es_dia_habil_nyse
from modules.data import get_sp500_historical_tickers
from modules.operations_log import menu as operations_log_menu
from modules.connors_rsi import (
    menu as connors_menu, get_capital_summary_connors,
    run_connors_backtest,
    opcion_senales_hoy          as cr_senales,
    opcion_posiciones_abiertas  as cr_posiciones,
    opcion_registrar_entrada    as cr_entrada,
    opcion_registrar_salida     as cr_salida,
    opcion_backtest             as cr_backtest,
    opcion_historial            as cr_historial,
    opcion_resumen              as cr_resumen,
    opcion_exportar_csv         as cr_exportar,
)

# Para el autopiloto necesitamos acceso directo a los helpers privados del
# módulo de paper trading ConnorsRSI. Importamos con alias para no colisionar
# con los nombres ya importados arriba.
from modules import connors_rsi as cr
from modules import strategy_db

import sqlite3

console = Console()


# ── Opciones de menú ──────────────────────────────────────────────────────────

def _opcion_connors_submenu() -> None:
    """Opción 2 — Submenú ConnorsRSI: paper trading + backtest (A-H)."""
    _OPTS = {
        "A": ("Ver señales de entrada hoy",   cr_senales),
        "B": ("Ver posiciones abiertas",       cr_posiciones),
        "C": ("Registrar entrada",             cr_entrada),
        "D": ("Registrar salida",              cr_salida),
        "E": ("Ejecutar backtest",             cr_backtest),
        "F": ("Ver historial de operaciones",  cr_historial),
        "G": ("Ver resumen de rentabilidad",   cr_resumen),
        "H": ("Exportar a CSV",                cr_exportar),
    }
    while True:
        console.print("\n[bold cyan]ConnorsRSI — Mean Reversion[/bold cyan]")
        for k, (label, _) in _OPTS.items():
            console.print(f"  [cyan]{k}[/cyan]  {label}")
        console.print("  [cyan]0[/cyan]  Volver")
        opc = Prompt.ask("\n  Opción", default="0").strip().upper()
        if opc == "0":
            break
        entry = _OPTS.get(opc)
        if entry:
            try:
                entry[1]()
            except KeyboardInterrupt:
                console.print("\n[yellow]Operación cancelada.[/yellow]")
        else:
            console.print("[yellow]Opción no reconocida.[/yellow]")


def _capital_section(title: str, s: dict) -> Text:
    """Construye una sub-sección del panel de capital (título + 4 líneas)."""
    txt = Text()
    txt.append(f"  {title}\n", style="bold cyan")
    rent_color = "green" if s["rentabilidad_pct"] >= 0 else "red"
    cash_pct   = (
        s["cash_libre"] / s["capital_actual"] * 100
    ) if s["capital_actual"] else 0.0
    n_pos      = s["n_posiciones"]
    label_pos  = "posiciones" if n_pos != 1 else "posición"

    txt.append(
        f"    Capital inicial    : {s['capital_inicial']:>12,.2f} €\n",
        style="white",
    )
    txt.append(
        f"    Capital actual     : {s['capital_actual']:>12,.2f} €  ",
        style="white",
    )
    txt.append(
        f"({s['rentabilidad_pct']:+.1f}%)\n",
        style=f"bold {rent_color}",
    )
    txt.append(
        f"    Comprometido       : {s['capital_comprometido']:>12,.2f} €  ",
        style="white",
    )
    txt.append(f"({n_pos} {label_pos})\n", style="dim")
    txt.append(
        f"    Cash libre         : {s['cash_libre']:>12,.2f} €  ",
        style="white",
    )
    txt.append(
        f"({cash_pct:.1f}% del capital actual)\n",
        style="dim",
    )
    # Capital Alpaca cuando la integración está activa
    if "capital_alpaca" in s:
        rent_a = s.get("rentabilidad_alpaca", 0.0)
        rent_a_col = "green" if rent_a >= 0 else "red"
        txt.append(
            f"    Capital Alpaca     : ${s['capital_alpaca']:>11,.2f}  ",
            style="white",
        )
        txt.append(f"({rent_a:+.1f}%)\n", style=f"bold {rent_a_col}")
    return txt


def show_capital_panel() -> None:
    """
    Muestra panel Rich con el resumen de capital de ConnorsRSI.
    Si no se puede calcular (BD vacía, sin red…), el panel no se muestra.
    """
    try:
        connors = get_capital_summary_connors()
    except Exception as exc:
        console.print(f"[yellow]⚠ Resumen Connors no disponible: {exc}[/yellow]")
        connors = None

    if not connors:
        return

    universe = load_universe()
    body = Text()
    body.append(
        f"  Universo: {len(universe)} tickers cargados desde {config.UNIVERSE_FILE}\n\n",
        style="dim cyan",
    )
    body.append(_capital_section("CONNORS RSI", connors))

    console.print(Panel(
        body,
        title="[bold white]ESTADO DEL CAPITAL[/bold white]",
        border_style="cyan",
        box=box.DOUBLE_EDGE,
        padding=(1, 2),
    ))


def _opcion_capital() -> None:
    """Opción 3 — Resumen de capital."""
    show_capital_panel()


def _opcion_operations_log() -> None:
    """Opción 5 — Submenú Operations Log (inspección detallada)."""
    operations_log_menu()


def _opcion_validar_universo() -> None:
    """Opción 6 — Valida el universo activo contra yfinance (con progreso)."""
    validate_universe()


def _opcion_ver_logs() -> None:
    """Opción 7 — Visor de logs (system / trading)."""
    from modules.logger import get_system_logger, get_trading_logger
    _LOGS_DIR = Path(__file__).parent / "logs"

    def _log_path(prefix: str, fecha_str: str | None = None) -> Path | None:
        d = fecha_str or date.today().strftime("%Y%m%d")
        p = _LOGS_DIR / f"{prefix}_{d}.log"
        return p if p.exists() else None

    def _mostrar(path: Path, titulo: str, n_lineas: int = 50) -> None:
        try:
            lineas = path.read_text(encoding="utf-8").splitlines()
        except Exception as exc:
            console.print(f"[red]No se pudo leer {path.name}: {exc}[/red]")
            return
        if not lineas:
            console.print(f"[dim]{titulo} — vacío[/dim]")
            return
        bloque = lineas[-n_lineas:]
        console.print(Panel(
            "\n".join(bloque),
            title=f"[bold white]{titulo}[/bold white]  "
                  f"[dim]({len(lineas)} líneas · mostrando últimas {len(bloque)})[/dim]",
            border_style="cyan",
            box=box.ROUNDED,
        ))

    while True:
        console.print("\n[bold cyan]Visor de Logs y Mantenimiento[/bold cyan]")
        console.print("  [cyan]A[/cyan] Ver log trading de hoy")
        console.print("  [cyan]B[/cyan] Ver log system de hoy")
        console.print("  [cyan]C[/cyan] Listar logs disponibles")
        console.print("  [cyan]D[/cyan] Ver log trading de una fecha (YYYYMMDD)")
        console.print("  [cyan]E[/cyan] Ejecutar backup manual ahora")
        console.print("  [cyan]0[/cyan] Volver")
        opc = Prompt.ask("\n  Opción", default="0").strip().upper()

        if opc == "0":
            break
        elif opc == "A":
            p = _log_path("trading")
            if p:
                _mostrar(p, f"trading_{date.today().strftime('%Y%m%d')}.log")
            else:
                console.print("[dim]No hay log de trading para hoy.[/dim]")
        elif opc == "B":
            p = _log_path("system")
            if p:
                _mostrar(p, f"system_{date.today().strftime('%Y%m%d')}.log")
            else:
                console.print("[dim]No hay log de system para hoy.[/dim]")
        elif opc == "C":
            if not _LOGS_DIR.exists():
                console.print("[dim]Carpeta logs/ vacía.[/dim]")
                continue
            ficheros = sorted(_LOGS_DIR.glob("*.log"), reverse=True)
            if not ficheros:
                console.print("[dim]Sin ficheros .log disponibles.[/dim]")
            else:
                t = Table(box=box.SIMPLE, show_header=False)
                t.add_column("Fichero", style="cyan")
                t.add_column("Tamaño", justify="right", style="dim")
                for f in ficheros:
                    kb = f.stat().st_size / 1024
                    t.add_row(f.name, f"{kb:.1f} KB")
                console.print(t)
        elif opc == "D":
            fecha = Prompt.ask("  Fecha (YYYYMMDD)").strip()
            p = _log_path("trading", fecha)
            if p:
                _mostrar(p, f"trading_{fecha}.log")
            else:
                console.print(f"[dim]No existe trading_{fecha}.log[/dim]")
        elif opc == "E":
            from modules.backup import run_backup
            console.print("\n[cyan]Ejecutando backup manual…[/cyan]")
            res = run_backup(env=config.TRADING_ENV, force_parquet=True)
            if res["sqlite_path"]:
                console.print(f"[green]✓ SQLite  : backups/{res['sqlite_path'].name}[/green]")
            n_pq = len(res.get("parquet_files", {}))
            if n_pq:
                console.print(f"[green]✓ Parquet : {n_pq} fichero(s)[/green]")
            for e in res.get("errores", []):
                console.print(f"[yellow]⚠ {e}[/yellow]")
        else:
            console.print("[yellow]Opción no reconocida.[/yellow]")


def _usd(x: float) -> str:
    """Importe USD sin decimales: 100411 → '$100,411'."""
    return f"${x:,.0f}"


def _usd_signed(x: float) -> str:
    """Importe USD con signo: 410 → '+$410', -125 → '-$125'."""
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def _badge_estrategia(estrat: str) -> str:
    """Devuelve el badge Rich coloreado para la estrategia de un ticker."""
    if estrat == "ConnorsRSI":
        return "[magenta]ConnorsRSI[/magenta]"
    return "[dim]?[/dim]"


def _proxima_ejecucion() -> str:
    """
    Texto de la próxima ejecución del autopiloto según `AUTOPILOT_EXEC_TIME` y
    el calendario NYSE: 'hoy HH:MMh' si hoy es hábil y aún no pasó la hora;
    'mañana HH:MMh' o 'dd/mm HH:MMh' al siguiente día hábil en caso contrario.
    """
    hora_str = getattr(config, "AUTOPILOT_EXEC_TIME", "22:30")
    try:
        hh, mm = (int(x) for x in hora_str.split(":"))
    except Exception:
        hh, mm = 22, 30

    ahora    = datetime.now()
    objetivo = ahora.replace(hour=hh, minute=mm, second=0, microsecond=0)
    hoy      = ahora.date()

    if es_dia_habil_nyse(hoy) and ahora < objetivo:
        return f"hoy {hh:02d}:{mm:02d}h"

    siguiente = hoy + timedelta(days=1)
    for _ in range(10):
        if es_dia_habil_nyse(siguiente):
            break
        siguiente += timedelta(days=1)

    if siguiente == hoy + timedelta(days=1):
        return f"mañana {hh:02d}:{mm:02d}h"
    return f"{siguiente.strftime('%d/%m')} {hh:02d}:{mm:02d}h"


def _mostrar_panel_alpaca(broker, acc, positions, orders) -> None:
    """
    Dashboard superior de la opción 8. Con Alpaca usa equity/cash en vivo y el
    capital inicial del portfolio history; sin Alpaca (dev) o si falla, cae a
    los datos de la BD. El desglose de estrategia sale siempre de la BD.
    """
    try:
        connors = get_capital_summary_connors()
    except Exception:
        connors = None

    alpaca_ok = broker is not None and acc is not None

    if alpaca_ok:
        equity = float(acc["equity"])
        cash   = float(acc["cash"])
        # Capital inicial real del portfolio history; fallback a config.
        try:
            ci = broker.get_portfolio_history().get("capital_inicial")
            capital_inicial = float(ci) if ci else float(config.CAPITAL_JUEGO)
        except Exception:
            capital_inicial = float(config.CAPITAL_JUEGO)
        modo         = "Alpaca PAPER" if config.ALPACA_PAPER else "Alpaca REAL"
        n_abiertas   = len(positions) if positions else 0
        n_pendientes = len(orders) if orders else 0
    else:
        cash   = connors["cash_libre"]     if connors else 0.0
        equity = connors["capital_actual"] if connors else 0.0
        capital_inicial = float(config.CAPITAL_JUEGO)
        modo         = "(sin Alpaca)"
        n_abiertas   = connors["n_posiciones"] if connors else 0
        try:
            n_pendientes = len(
                strategy_db.get_estrategia_por_ticker(config.DB_PATH, "orden")
            )
        except Exception:
            n_pendientes = 0

    invertido = equity - cash
    pnl       = equity - capital_inicial
    pnl_pct   = (pnl / capital_inicial * 100) if capital_inicial else 0.0
    pnl_col   = "green" if pnl >= 0 else "red"
    fecha     = datetime.now().strftime("%d/%m/%Y %H:%Mh")

    txt = Text()
    # Fila 1-2: PORTFOLIO + PnL TOTAL
    txt.append("  PORTFOLIO", style="bold dim")
    txt.append(" " * 24)
    txt.append("PnL TOTAL\n", style="bold dim")
    txt.append(f"  {_usd(equity):<31}", style="bold white")
    txt.append(f"{_usd_signed(pnl)}  ", style=f"bold {pnl_col}")
    txt.append(f"{pnl_pct:+.2f}%\n", style=f"bold {pnl_col}")
    # Fila 3: cash / invertido / capital inicial
    txt.append(
        f"  Cash {_usd(cash)}   Invertido {_usd(invertido)}   "
        f"Capital inicial: {_usd(capital_inicial)}\n\n",
        style="white",
    )
    # Fila 4: desglose de la estrategia
    txt.append("  ")
    if connors:
        c_pnl = connors["capital_actual"] - connors["capital_inicial"]
        c_col = "green" if c_pnl >= 0 else "red"
        txt.append("CONNORSRSI  ", style="bold magenta")
        txt.append(f"{_usd(connors['capital_actual'])}  ", style="white")
        txt.append(
            f"{connors['rentabilidad_pct']:+.1f}% ({_usd_signed(c_pnl)})",
            style=c_col,
        )
    txt.append("\n")
    # Fila 5: posiciones + próxima ejecución
    plural = "" if n_pendientes == 1 else "s"
    txt.append(
        f"  Posiciones  {n_abiertas} abiertas · {n_pendientes} pendiente{plural}",
        style="white",
    )
    txt.append(f"     Próx. ejecución  {_proxima_ejecucion()}\n", style="dim")

    console.print(Panel(
        txt,
        title=f"[bold white]KAIROS TRADING SYSTEM[/bold white]  "
              f"[dim]· {fecha} · {modo}[/dim]",
        border_style="cyan", box=box.DOUBLE_EDGE, padding=(1, 2),
    ))


def _tabla_posiciones_alpaca(positions) -> None:
    """Tabla de posiciones abiertas con badge de estrategia y valor total."""
    if not positions:
        console.print("[dim]Sin posiciones abiertas en Alpaca.[/dim]")
        return

    estrat = strategy_db.get_estrategia_por_ticker(config.DB_PATH, "posicion")
    t = Table(
        title="[bold cyan]POSICIONES ABIERTAS[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    t.add_column("Ticker",    justify="left")
    t.add_column("Estrat.",   justify="center")
    t.add_column("Qty",       justify="right")
    t.add_column("Entrada",   justify="right")
    t.add_column("Actual",    justify="right")
    t.add_column("Val.total", justify="right")
    t.add_column("PnL",       justify="right")
    t.add_column("PnL%",      justify="right")

    total_pnl = total_cost = total_val = 0.0
    # Ordena por PnL% de mayor a menor.
    positions = sorted(positions, key=lambda p: p["unrealized_plpc"], reverse=True)
    for p in positions:
        col       = "green" if p["unrealized_pl"] >= 0 else "red"
        val_total = p["qty"] * p["current_price"]
        t.add_row(
            p["ticker"],
            _badge_estrategia(estrat.get(p["ticker"], "?")),
            f"{p['qty']:.4f}",
            f"${p['avg_entry_price']:,.2f}",
            f"${p['current_price']:,.2f}",
            f"${val_total:,.2f}",
            f"[{col}]${p['unrealized_pl']:+,.2f}[/{col}]",
            f"[{col}]{p['unrealized_plpc']*100:+.2f}%[/{col}]",
        )
        total_pnl  += p["unrealized_pl"]
        total_cost += p["avg_entry_price"] * p["qty"]
        total_val  += val_total

    total_pct = (total_pnl / total_cost * 100) if total_cost else 0.0
    tcol = "green" if total_pnl >= 0 else "red"
    t.add_section()
    t.add_row(
        "[bold]TOTAL[/bold]", "", "", "", "",
        f"[bold]${total_val:,.2f}[/bold]",
        f"[bold {tcol}]${total_pnl:+,.2f}[/bold {tcol}]",
        f"[bold {tcol}]{total_pct:+.2f}%[/bold {tcol}]",
    )
    console.print(t)


def _tabla_ordenes_alpaca(orders) -> None:
    """Tabla de órdenes pendientes con badge de estrategia."""
    if not orders:
        console.print("[dim]Sin órdenes pendientes en Alpaca.[/dim]")
        return

    estrat = strategy_db.get_estrategia_por_ticker(config.DB_PATH, "orden")
    t = Table(
        title="[bold cyan]ÓRDENES PENDIENTES[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    t.add_column("Ticker",  justify="left")
    t.add_column("Estrat.", justify="center")
    t.add_column("Lado",    justify="center")
    t.add_column("Qty",     justify="right")
    t.add_column("Tipo",    justify="center")
    t.add_column("Estado",  justify="center")
    for o in orders:
        t.add_row(
            o["ticker"],
            _badge_estrategia(estrat.get(o["ticker"], "?")),
            o["side"].upper(),
            f"{o['qty']:.4f}",
            o["type"], o["status"],
        )
    console.print(t)


def _opcion_alpaca_estado() -> None:
    """Opción 9 — Dashboard de estado: panel de capital + posiciones + órdenes."""
    alpaca_on = getattr(config, "ALPACA_ENABLED", False)
    broker = acc = positions = orders = None

    if alpaca_on:
        try:
            from modules.alpaca_broker import get_broker
            broker    = get_broker()
            acc       = broker.get_account()
            positions = broker.get_positions()
            orders    = broker.get_orders(status="open")
        except Exception as exc:
            console.print(
                f"[yellow]⚠ Alpaca no disponible — datos de BD ({exc})[/yellow]"
            )
            broker = acc = None  # fuerza el fallback a BD en el panel

    # Panel superior (siempre se muestra; usa BD si no hay Alpaca).
    _mostrar_panel_alpaca(broker, acc, positions, orders)

    if not alpaca_on:
        console.print(
            "\n[dim]Alpaca desactivado (dev) — panel con datos de BD. "
            "Activar ALPACA_ENABLED=True para ver posiciones y órdenes en vivo.[/dim]"
        )
        return
    if broker is None or acc is None:
        return  # ya se avisó del fallo de conexión

    _tabla_posiciones_alpaca(positions)
    _tabla_ordenes_alpaca(orders)


_MESES_ES = {
    "enero":      1,  "febrero":   2,  "marzo":     3,  "abril":   4,
    "mayo":       5,  "junio":     6,  "julio":     7,  "agosto":  8,
    "septiembre": 9,  "octubre":  10,  "noviembre": 11, "diciembre": 12,
}


def _parse_crisis_date(crisis_str: str) -> datetime | None:
    """`'Octubre 2007'` → datetime(2007, 10, 1). None si no parseable."""
    if not crisis_str:
        return None
    parts = crisis_str.strip().split()
    if len(parts) != 2:
        return None
    mes = _MESES_ES.get(parts[0].lower())
    if mes is None:
        return None
    try:
        return datetime(int(parts[1]), mes, 1)
    except Exception:
        return None


def _parse_spy_pct(spy_str: str) -> float | None:
    """`'-57%'` → -57.0. None si no parseable."""
    try:
        return float(spy_str.rstrip("%").replace("+", "").replace(",", "."))
    except Exception:
        return None


def _split_trades_by_date(trades: list, crisis_dt: datetime) -> tuple[list, list]:
    """Divide trades en (pre_crisis, durante_crisis) según `exit_date`."""
    pre, post = [], []
    for t in trades:
        ed = t.get("exit_date")
        try:
            if hasattr(ed, "to_pydatetime"):
                d = ed.to_pydatetime()
            elif isinstance(ed, datetime):
                d = ed
            elif isinstance(ed, date):
                d = datetime(ed.year, ed.month, ed.day)
            else:
                d = datetime.fromisoformat(str(ed)[:10])
        except Exception:
            pre.append(t)
            continue
        if d < crisis_dt:
            pre.append(t)
        else:
            post.append(t)
    return pre, post


def _phase_stats(trades: list, capital_inicio_fase: float) -> dict:
    """Métricas agregadas de una fase: pnl, n, wins, wr, rent, capital_final."""
    pnl    = sum(t.get("net_pnl", 0.0) for t in trades)
    n      = len(trades)
    wins   = sum(1 for t in trades if t.get("net_pnl", 0.0) > 0)
    wr     = (wins / n * 100) if n else 0.0
    rent   = (pnl / capital_inicio_fase * 100) if capital_inicio_fase else 0.0
    return {
        "pnl":           pnl,
        "n":             n,
        "wins":          wins,
        "wr":            wr,
        "rent":          rent,
        "capital_final": capital_inicio_fase + pnl,
    }


def _phase_section_text(label: str, s: dict, spy_pct: float | None = None) -> Text:
    """Genera la sub-sección Rich con la métrica de una fase para una estrategia."""
    rent_col = "green" if s["rent"] >= 0 else "red"
    diff_lbl = ""
    if spy_pct is not None:
        diff_lbl = f"  (vs SPY: {s['rent'] - spy_pct:+.1f}pp)"
    out = Text()
    out.append(f"  {label}\n", style="bold cyan")
    out.append("    Rentabilidad  : ", style="white")
    out.append(f"{s['rent']:+.2f} %{diff_lbl}\n", style=f"bold {rent_col}")
    out.append(f"    Operaciones   : {s['n']}  (WR {s['wr']:.1f} %)\n", style="white")
    out.append(f"    Capital final : {s['capital_final']:,.2f} €\n", style="white")
    return out


def _validate_tickers_for_period(tickers: list, start_date: str) -> tuple[list, list]:
    """
    Verifica con yfinance qué tickers tienen datos al inicio del período
    (con margen para los indicadores). Devuelve (válidos, excluidos).

    Usa una descarga batch con `group_by="ticker"` para minimizar latencia
    en universos grandes (~500 tickers).
    """
    import yfinance as yf
    target      = datetime.strptime(start_date, "%Y-%m-%d")
    check_start = (target - timedelta(days=120)).strftime("%Y-%m-%d")
    check_end   = (target + timedelta(days=5)).strftime("%Y-%m-%d")

    validos: list = []
    excluidos: list = []
    chunk_size = 50
    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i + chunk_size]
        try:
            df = yf.download(
                chunk, start=check_start, end=check_end,
                auto_adjust=True, progress=False, group_by="ticker",
                threads=True,
            )
        except Exception:
            excluidos.extend(chunk)
            continue
        if df is None or df.empty:
            excluidos.extend(chunk)
            continue
        for t in chunk:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if t in df.columns.get_level_values(0):
                        sub = df[t].dropna(how="all")
                    else:
                        excluidos.append(t)
                        continue
                else:
                    sub = df.dropna(how="all")
                if not sub.empty and len(sub) >= 3:
                    validos.append(t)
                else:
                    excluidos.append(t)
            except Exception:
                excluidos.append(t)
    return validos, excluidos


def _opcion_historical_backtests() -> None:
    """
    Opción 7 — Backtesting sobre períodos históricos predefinidos, usando el
    universo SP500 que realmente cotizaba en cada momento (vía fja05680).
    """
    # ── 1) Selección de período ──────────────────────────────────────────────
    table = Table(
        title="[bold cyan]Períodos históricos disponibles[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    table.add_column("Nº",          justify="center", style="bold cyan")
    table.add_column("Nombre",      justify="left")
    table.add_column("Período",     justify="center")
    table.add_column("Crisis en",   justify="center")
    table.add_column("SPY crisis",  justify="right")
    table.add_column("Descripción", justify="left")
    for k, p in config.HISTORICAL_PERIODS.items():
        table.add_row(
            k, p["nombre"],
            f"{p['inicio']} → {p['fin']}",
            p.get("crisis", "—"),
            p["spy_retorno"],
            p["descripcion"],
        )
    console.print(table)

    pid = Prompt.ask(
        "\n  Período",
        choices=list(config.HISTORICAL_PERIODS.keys()),
        default=list(config.HISTORICAL_PERIODS.keys())[-1],
    )
    periodo = config.HISTORICAL_PERIODS[pid]

    # Slug del nombre del período (se usa en los nombres de fichero exportados)
    safe_nombre = (
        periodo["nombre"].lower()
        .replace(" ", "_").replace("ó", "o").replace("í", "i")
        .replace("á", "a").replace("é", "e").replace("ú", "u")
        .replace("ñ", "n").replace("+", "_").replace("/", "_")
    )
    while "__" in safe_nombre:          # colapsa "_+_" → "___" → "_"
        safe_nombre = safe_nombre.replace("__", "_")
    safe_nombre = safe_nombre.strip("_")

    # ── 3) Universo histórico + validación ──────────────────────────────────
    console.print(
        f"\n[cyan]Obteniendo universo histórico SP500 a fecha "
        f"{periodo['inicio']}...[/cyan]"
    )
    tickers_hist = get_sp500_historical_tickers(periodo["inicio"])
    console.print(
        f"[white]Universo histórico: {len(tickers_hist)} tickers "
        f"para {periodo['inicio']}[/white]"
    )

    console.print(
        f"[cyan]Validando disponibilidad de datos en yfinance "
        f"(esto puede tardar 1-2 min para universos grandes)...[/cyan]"
    )
    validos, excluidos = _validate_tickers_for_period(
        tickers_hist, periodo["inicio"],
    )
    n_val = len(validos)
    console.print(f"  [green]Tickers válidos   : {n_val}[/green]")
    console.print(f"  [dim]Tickers excluidos : {len(excluidos)} (sin datos)[/dim]")

    if n_val < 10:
        console.print(
            "[bold red]⛔ Sólo "
            f"{n_val} tickers válidos. Resultado poco fiable.[/bold red]"
        )
        if not Confirm.ask("¿Continuar igualmente?", default=False):
            return
    elif n_val < 20:
        console.print(
            f"[yellow]⚠ Pocos tickers válidos ({n_val}). "
            f"Resultado puede ser ruidoso.[/yellow]"
        )

    # ── 4) Ejecutar backtest ConnorsRSI ──────────────────────────────────────
    resultados: dict = {}
    console.print(f"\n[bold cyan]▶ Backtest ConnorsRSI {periodo['nombre']}[/bold cyan]")
    resultados["connors"] = run_connors_backtest(
        tickers=validos,
        start_date=periodo["inicio"],
        end_date=periodo["fin"],
    )
    # `run_connors_backtest` exporta a `connors_{start}_{end}.csv`; le
    # añadimos el slug del período al nombre para distinguir entre
    # backtests históricos diferentes con el mismo rango de fechas.
    csv_path = resultados["connors"].get("csv") if resultados["connors"] else None
    if csv_path:
        csv_path = Path(csv_path)
        new_name = csv_path.name.replace(
            "connors_", f"connors_HIST_{safe_nombre}_", 1,
        )
        new_path = csv_path.with_name(new_name)
        try:
            csv_path.rename(new_path)
            resultados["connors"]["csv"] = new_path
            console.print(
                f"[green]✓ CSV ConnorsRSI renombrado: {new_path.name}[/green]"
            )
        except Exception as exc:
            console.print(
                f"[yellow]⚠ No se pudo renombrar CSV ConnorsRSI: {exc}[/yellow]"
            )

    # ── 5) Resumen comparativo con 3 fases ──────────────────────────────────
    crisis_dt = _parse_crisis_date(periodo.get("crisis", ""))

    body = Text()
    body.append(f"  PERÍODO: {periodo['nombre']}\n", style="bold cyan")
    body.append(
        f"  Simulación: {periodo['inicio']} → {periodo['fin']}\n", style="white",
    )
    if crisis_dt is not None:
        body.append(f"  Crisis estalló: {periodo['crisis']}\n", style="yellow")
    else:
        body.append(f"  Crisis: {periodo.get('crisis', '—')}\n", style="dim")
    body.append(f"  SPY durante crisis: {periodo['spy_retorno']}\n", style="dim")
    body.append(f"  Universo: {n_val} tickers históricos\n", style="white")
    body.append("\n")

    initial_by_key = {
        "connors": config.CONNORS_CAPITAL,
    }
    label_by_key = {
        "connors": "CONNORS RSI",
    }

    spy_pct = _parse_spy_pct(periodo["spy_retorno"])

    # ── FASE 1 + FASE 2 (sólo si hay fecha de crisis parseable) ────────────
    if crisis_dt is not None:
        body.append(
            f"  ── FASE 1 — Pre-crisis ({periodo['inicio']} → "
            f"{crisis_dt.strftime('%Y-%m-%d')}) ──\n",
            style="bold green",
        )
        for key, res in resultados.items():
            if not res:
                continue
            trades = res.get("trades", [])
            pre, _ = _split_trades_by_date(trades, crisis_dt)
            s = _phase_stats(pre, initial_by_key[key])
            body.append(_phase_section_text(label_by_key[key], s))
        body.append("\n")

        body.append(
            f"  ── FASE 2 — Durante crisis ({crisis_dt.strftime('%Y-%m-%d')} → "
            f"{periodo['fin']}) ──\n",
            style="bold red",
        )
        for key, res in resultados.items():
            if not res:
                continue
            trades = res.get("trades", [])
            pre, post = _split_trades_by_date(trades, crisis_dt)
            cap_inicio_fase2 = initial_by_key[key] + sum(t.get("net_pnl", 0) for t in pre)
            s = _phase_stats(post, cap_inicio_fase2)
            body.append(_phase_section_text(
                label_by_key[key], s,
                spy_pct=spy_pct,
            ))
        body.append("\n")

    # ── RESULTADO GLOBAL ────────────────────────────────────────────────────
    body.append("  ── RESULTADO GLOBAL ──\n", style="bold cyan")
    for key, res in resultados.items():
        if not res:
            continue
        trades = res.get("trades", [])
        s = _phase_stats(trades, initial_by_key[key])
        body.append(_phase_section_text(label_by_key[key], s))
    body.append(f"  SPY período completo: {periodo['spy_retorno']}\n", style="dim")

    console.print(Panel(
        body,
        title="[bold white]RESUMEN BACKTEST HISTÓRICO[/bold white]",
        border_style="cyan", box=box.DOUBLE_EDGE, padding=(1, 2),
    ))

    # ── 6) Exportar resumen a TXT (con 3 fases) ─────────────────────────────
    exports_dir = Path(__file__).parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    fpath = exports_dir / (
        f"historical_{safe_nombre}_{date.today().strftime('%Y%m%d')}.txt"
    )

    lines: list = []
    sep = "=" * 70
    lines.append(sep)
    lines.append(f"  BACKTEST HISTÓRICO · {periodo['nombre']}")
    lines.append(f"  Simulación: {periodo['inicio']}  →  {periodo['fin']}")
    lines.append(f"  Crisis: {periodo.get('crisis', '—')}")
    lines.append(f"  SPY durante crisis: {periodo['spy_retorno']}")
    lines.append(f"  Universo: {n_val} tickers (de {len(tickers_hist)})")
    lines.append(sep)
    lines.append("")

    def _phase_lines(label, s):
        return [
            f"  {label}",
            f"    Rentabilidad : {s['rent']:+.2f} %",
            f"    PnL          : {s['pnl']:+,.2f} €",
            f"    Operaciones  : {s['n']}  ({s['wins']} ganadoras)",
            f"    Win rate     : {s['wr']:.1f} %",
            f"    Capital final: {s['capital_final']:,.2f} €",
        ]

    if crisis_dt is not None:
        lines.append(
            f"— FASE 1 — Pre-crisis ({periodo['inicio']} → "
            f"{crisis_dt.strftime('%Y-%m-%d')}) —"
        )
        for key, res in resultados.items():
            if not res: continue
            trades = res.get("trades", [])
            pre, _ = _split_trades_by_date(trades, crisis_dt)
            lines.extend(_phase_lines(label_by_key[key], _phase_stats(pre, initial_by_key[key])))
            lines.append("")
        lines.append(
            f"— FASE 2 — Durante crisis ({crisis_dt.strftime('%Y-%m-%d')} → "
            f"{periodo['fin']}) —"
        )
        for key, res in resultados.items():
            if not res: continue
            trades = res.get("trades", [])
            pre, post = _split_trades_by_date(trades, crisis_dt)
            cap_inicio_fase2 = initial_by_key[key] + sum(t.get("net_pnl", 0) for t in pre)
            lines.extend(_phase_lines(label_by_key[key], _phase_stats(post, cap_inicio_fase2)))
            lines.append("")

    lines.append("— RESULTADO GLOBAL —")
    for key, res in resultados.items():
        if not res: continue
        trades = res.get("trades", [])
        lines.extend(_phase_lines(label_by_key[key], _phase_stats(trades, initial_by_key[key])))
        lines.append("")
    lines.append(f"SPY período completo: {periodo['spy_retorno']}")

    fpath.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"\n[green]✓ Resumen exportado: {fpath}[/green]")


# ── Opción 1 — Autopiloto (ConnorsRSI) ───────────────────────────────────────

def _connors_pnl_realizado_hoy() -> tuple[int, float]:
    """PnL realizado hoy en ConnorsRSI: (n_trades_hoy, suma_net_pnl)."""
    hoy = date.today().isoformat()
    try:
        with sqlite3.connect(str(config.DB_PATH)) as con:
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(net_pnl), 0) "
                "FROM connors_operaciones "
                "WHERE estado = 'cerrada' AND exit_date = ?", (hoy,),
            ).fetchone()
    except Exception:
        return (0, 0.0)
    return (int(row[0] or 0), float(row[1] or 0.0))


def _autopiloto_cerrar_connors_automatico() -> int:
    """
    Cierra automáticamente posiciones ConnorsRSI que cumplen SL / TSTO /
    CRSI_EXIT. Devuelve el número de cierres ejecutados.

    A diferencia de `opcion_registrar_salida` (interactiva, una a una), aquí
    procesamos todas en bloque sin preguntar. Es el comportamiento que define
    el spec del autopiloto.
    """
    positions = cr._get_open_positions()
    if not positions:
        return 0

    # _scan_exit_signals necesita yf_cache.session_data poblado. Lo carga la
    # propia función si está vacío, descargando el universo. Para evitar pagar
    # dos veces la descarga en el mismo turno, sólo descargamos si está vacío.
    from modules import yf_cache
    if not yf_cache.session_data:
        cr._download_universe()

    exits = cr._scan_exit_signals(positions)
    n_cerradas = 0
    for e in exits:
        if not e["reason"]:
            continue
        try:
            cr._close_position_db(
                e["ticker"], e["pos"], e["cur_p"],
                e["reason"], date.today(),
            )
            n_cerradas += 1
        except Exception as exc:
            console.print(
                f"[yellow]⚠ Cierre automático {e['ticker']} falló: {exc}[/yellow]"
            )
    return n_cerradas


def _opcion_autopiloto() -> None:
    """
    Opción 1 — Modo autopiloto ConnorsRSI.

    Pasos:
      1. Reconcilia pending ConnorsRSI.
      2. Cierra posiciones ConnorsRSI con SL/TSTO/CRSI_EXIT y abre nuevas
         vía `opcion_registrar_entrada` (modo A automático).
      3. Genera informe Rich y lo exporta a `exports/informe_diario_*.txt`.

    Todo envuelto en try/except: si Alpaca falla en algún paso, el resto del
    flujo continúa con SQLite como fuente de verdad y se muestra un warning.
    """
    start_ts = datetime.now()
    _tlog = get_trading_logger()
    _slog = get_system_logger()
    console.rule(
        f"[bold]AUTOPILOTO  ·  {start_ts:%d/%m/%Y %H:%M:%S}[/bold]"
    )

    alpaca_on = bool(getattr(config, "ALPACA_ENABLED", False))
    alpaca_label = (
        "PAPER" if getattr(config, "ALPACA_PAPER", True) else "REAL"
    ) if alpaca_on else "OFF"

    _tlog.info("── INICIO AUTOPILOTO %s", "─" * 38)
    _tlog.info("Entorno: %s · Alpaca: %s", config.TRADING_ENV.upper(), alpaca_label)
    _slog.info("autopiloto inicio: entorno=%s alpaca=%s", config.TRADING_ENV, alpaca_label)

    stats: dict = {
        "connors_sync":          {"filled": 0, "expired": 0, "canceled": 0, "pending": 0},
        "connors_cierres":       0,
        "connors_aperturas":     0,
        "errores":               [],
    }

    # ─── BACKUP INICIAL ───────────────────────────────────────────────────────
    _backup_res: dict = {"sqlite_path": None, "parquet_files": {}, "errores": []}
    try:
        from modules.backup import run_backup
        _backup_res = run_backup(env=config.TRADING_ENV)
        if _backup_res["sqlite_path"]:
            console.print(
                f"[dim]Backup: {_backup_res['sqlite_path'].name}[/dim]"
            )
    except Exception as exc:
        console.print(f"[yellow]⚠ Backup falló: {exc}[/yellow]")
        _slog.warning("backup en autopiloto: %s", exc)

    # ─── PASO 1: Sync pending ConnorsRSI ─────────────────────────────────────
    console.print("\n[bold cyan]PASO 1 — Reconciliando órdenes pendientes ConnorsRSI…[/bold cyan]")
    if alpaca_on:
        try:
            s = cr._check_pending_orders()
            stats["connors_sync"] = s
            total = sum(s.values())
            if total == 0:
                console.print("[dim]  Sin órdenes pendientes.[/dim]")
            else:
                console.print(
                    f"  [green]✓ {s['filled']} ejecutadas[/green]  ·  "
                    f"[yellow]✗ {s['expired'] + s['canceled']} caducadas/canceladas[/yellow]  ·  "
                    f"[dim]{s['pending']} siguen pending[/dim]"
                )
        except Exception as exc:
            console.print(f"[yellow]⚠ Sync ConnorsRSI falló: {exc}[/yellow]")
            stats["errores"].append(f"sync_connors: {exc}")
            _slog.error("sync ConnorsRSI: %s", exc, exc_info=True)
    else:
        console.print("[dim]  Alpaca desactivado — paso omitido.[/dim]")
    _tlog.info("PASO 1 — Connors sync: %d filled · %d expiradas/canceladas",
               stats["connors_sync"]["filled"],
               stats["connors_sync"]["expired"] + stats["connors_sync"]["canceled"])

    # ─── PASO 2: ConnorsRSI (cierres + aperturas) ────────────────────────────
    console.print("\n[bold cyan]PASO 2 — ConnorsRSI…[/bold cyan]")

    # ── 2.1) Cierres: descarga SOLO los tickers en cartera (+ SPY auto) ──────
    # Para evaluar SL/TSTO/CRSI_EXIT no necesitamos los 349 del universo: basta
    # con los que ya tenemos abiertos. Si no hay nada abierto, saltamos.
    n_open_pre  = len(cr._get_open_positions())
    n_pend_pre  = cr._count_pending_orders()

    if n_open_pre > 0:
        tickers_cartera = list(cr._get_open_positions().keys())
        console.print(
            f"  [cyan]· Descargando {len(tickers_cartera)} ticker(s) de cartera "
            f"+ SPY para evaluar cierres…[/cyan]"
        )
        try:
            cr._download_universe(tickers=tickers_cartera)
        except Exception as exc:
            console.print(f"[yellow]⚠ Descarga parcial falló: {exc}[/yellow]")
            stats["errores"].append(f"descarga_cierres_connors: {exc}")

        try:
            stats["connors_cierres"] = _autopiloto_cerrar_connors_automatico()
            if stats["connors_cierres"]:
                console.print(
                    f"  [green]✓ Cerradas {stats['connors_cierres']} posición(es)[/green]"
                )
            else:
                console.print(
                    "[dim]  Sin cierres automáticos (ninguna posición cumple SL/TSTO/CRSI_EXIT).[/dim]"
                )
        except Exception as exc:
            console.print(f"[yellow]⚠ Cierres ConnorsRSI fallaron: {exc}[/yellow]")
            stats["errores"].append(f"cierres_connors: {exc}")
            _slog.error("cierres ConnorsRSI: %s", exc, exc_info=True)
    else:
        console.print(
            "[dim]  Sin posiciones Connors abiertas — no hay cierres que evaluar.[/dim]"
        )
        stats["connors_cierres"] = 0

    # ── 2.2) Aperturas: solo si quedan slots libres tras los cierres ─────────
    # Aquí sí descargamos el universo completo (necesario para escanear señales
    # nuevas). Los tickers de cartera ya están en yf_cache.session_data por el
    # paso anterior, así que solo se descargan los que faltan.
    n_pend_post = cr._count_pending_orders()
    n_open_post = len(cr._get_open_positions())
    slots_libres = config.CONNORS_MAX_POS - n_open_post - n_pend_post

    if slots_libres > 0:
        try:
            console.print(
                f"  [cyan]· {slots_libres} slot(s) libre(s) tras cierres — "
                f"descargando universo completo para buscar señales de entrada…[/cyan]"
            )
            # `aperturas_automaticas` llama internamente a `_download_universe()`
            # sin tickers → descarga el universo completo. El cache de sesión
            # evita re-descargar los tickers de cartera ya cargados arriba.
            cr.aperturas_automaticas(reconciliar_primero=False, confirmar=False)
            n_pend_final = cr._count_pending_orders()
            n_open_final = len(cr._get_open_positions())
            stats["connors_aperturas"] = (
                (n_pend_final - n_pend_post)
                + (n_open_final - n_open_post)
            )
        except Exception as exc:
            console.print(f"[yellow]⚠ Aperturas ConnorsRSI fallaron: {exc}[/yellow]")
            stats["errores"].append(f"aperturas_connors: {exc}")
    else:
        console.print(
            f"[dim]  Sin slots libres tras cierres "
            f"({n_open_post}/{config.CONNORS_MAX_POS} abiertas + "
            f"{n_pend_post} pendientes) — se evita descarga completa del "
            f"universo, no se buscan señales de entrada.[/dim]"
        )
        _tlog.info("[CONNORS] SIN ACCION · slots llenos (%d/%d)",
                   n_open_post + n_pend_post, config.CONNORS_MAX_POS)

    # ─── PASO 3: Informe final ───────────────────────────────────────────────
    console.print()
    _autopiloto_informe(start_ts, stats, alpaca_label, _backup_res)


def _autopiloto_informe(
    start_ts: datetime, stats: dict, alpaca_label: str,
    backup_res: dict | None = None,
) -> None:
    """Imprime el panel Rich del informe y lo exporta a TXT en exports/."""
    end_ts = datetime.now()

    # ── Recolectar métricas para los bloques del informe ──
    try:
        cap_connors = get_capital_summary_connors()
    except Exception:
        cap_connors = None

    n_pnl_real_connors, pnl_real_connors = _connors_pnl_realizado_hoy()

    n_open_connors = len(cr._get_open_positions())
    n_pend_connors = cr._count_pending_orders()

    pnl_unreal_connors = (
        cap_connors["capital_actual"] - cap_connors["capital_inicial"]
        - pnl_real_connors
    ) if cap_connors else 0.0

    cap_connors_act = cap_connors["capital_actual"]   if cap_connors else 0.0
    cap_connors_pct = cap_connors["rentabilidad_pct"] if cap_connors else 0.0

    # ── Construir cuerpo del informe (texto plano para TXT + Rich) ──
    lines: list[str] = []
    sep = "=" * 60
    lines.append(sep)
    lines.append(
        f"  INFORME DIARIO — {start_ts:%d/%m/%Y %H:%M:%S}"
    )
    lines.append(
        f"  Entorno: {config.TRADING_ENV.upper()}  ·  Alpaca: {alpaca_label}"
    )
    lines.append(sep)
    lines.append("")
    lines.append("  CONNORS RSI")
    lines.append(f"  Cierres hoy     : {stats['connors_cierres']}")
    lines.append(f"  Aperturas hoy   : {stats['connors_aperturas']}")
    lines.append(
        f"  Posiciones      : {n_open_connors}/{config.CONNORS_MAX_POS} "
        f"slots ocupados  (+{n_pend_connors} pendientes Alpaca)"
    )
    lines.append(
        f"  PnL realizado   : {pnl_real_connors:+,.2f} €  "
        f"({n_pnl_real_connors} op.)"
    )
    lines.append(f"  PnL no realiz.  : {pnl_unreal_connors:+,.2f} €")
    lines.append("")
    lines.append("  CAPITAL")
    lines.append(
        f"  ConnorsRSI      : {cap_connors_act:>10,.2f} €  ({cap_connors_pct:+.2f}%)"
    )
    lines.append(
        f"  PnL día         : {pnl_real_connors + pnl_unreal_connors:+,.2f} €"
    )
    lines.append("")
    if backup_res:
        lines.append("  BACKUP")
        sqlite_name = backup_res["sqlite_path"].name if backup_res.get("sqlite_path") else "—"
        lines.append(f"  SQLite  : backups/{sqlite_name}")
        n_pq = len(backup_res.get("parquet_files", {}))
        lines.append(f"  Parquet : {n_pq} fichero(s) (solo lunes)")
        if backup_res.get("errores"):
            for e in backup_res["errores"]:
                lines.append(f"  ⚠ {e}")
        lines.append("")

    lines.append("  PRÓXIMAS ACCIONES")
    lines.append("  Mañana          : ConnorsRSI ~22:00 h (tras cierre NYSE)")
    if stats["errores"]:
        lines.append("")
        lines.append("  AVISOS")
        for err in stats["errores"]:
            lines.append(f"  · {err}")
    lines.append("")
    lines.append(sep)
    lines.append(f"  Duración: {(end_ts - start_ts).total_seconds():.1f} s")
    lines.append(sep)

    contenido = "\n".join(lines)
    console.print(Panel(
        Text(contenido, style="white"),
        title=f"[bold white]INFORME DIARIO[/bold white]",
        border_style="cyan", box=box.DOUBLE_EDGE, padding=(1, 2),
    ))

    # Exportar a fichero
    exports_dir = Path(__file__).parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    fpath = exports_dir / f"informe_diario_{start_ts:%Y%m%d_%H%M%S}.txt"
    try:
        fpath.write_text(contenido, encoding="utf-8")
        console.print(f"\n[green]✓ Informe exportado: {fpath}[/green]")
    except Exception as exc:
        console.print(f"[yellow]⚠ No se pudo exportar el informe: {exc}[/yellow]")

    # ── Trading log: resumen de capital ──────────────────────────────────────
    _tlog = get_trading_logger()
    _tlog.info("Capital ConnorsRSI : %.0f€ (%+.1f%%)", cap_connors_act, cap_connors_pct)
    _tlog.info("── FIN AUTOPILOTO %s", "─" * 40)

    # ── Notificación Telegram (solo en PRO) ──────────────────────────────────
    if config.TRADING_ENV in ("dev", "pre", "pro"):
        try:
            from modules.notifier import send_telegram, build_daily_summary
            from modules.alpaca_broker import get_pnl_real
            pnl_real_alpaca = get_pnl_real()  # None en dev / si Alpaca falla
            msg = build_daily_summary(
                stats=stats,
                cap_connors=cap_connors,
                pnl_connors_hoy=pnl_real_connors,
                start_ts=start_ts,
                entorno=config.TRADING_ENV,
                alpaca_label=alpaca_label,
                pnl_real=pnl_real_alpaca,
            )
            send_telegram(msg)
        except Exception as exc:
            get_system_logger().warning("Telegram: error inesperado — %s", exc)

    # ── Publicar track record en GitHub (DEV, PRE y PRO) ─────────────────────
    if config.TRADING_ENV in ("dev", "pre", "pro"):
        try:
            _scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
            if str(_scripts_dir) not in sys.path:
                sys.path.insert(0, str(_scripts_dir))
            from publish_logs import publish_track_record
            publish_track_record()
        except Exception as exc:
            get_system_logger().warning("publish_logs: error inesperado — %s", exc)


# ── Modo desatendido (--run) ──────────────────────────────────────────────────

def _run_headless(mode: str) -> int:
    """
    Ejecuta el sistema sin menú interactivo. Diseñado para cron / CI.

    Params:
        mode: "all" | "connors"
    Returns:
        0 si todo fue bien, 1 si hubo algún error.
    """
    _slog = get_system_logger()
    _tlog = get_trading_logger()
    _slog.info("Modo headless --run %s --env %s", mode, config.TRADING_ENV)

    try:
        if mode == "all":
            _opcion_autopiloto()

        elif mode == "connors":
            alpaca_on = getattr(config, "ALPACA_ENABLED", False)
            if alpaca_on:
                try:
                    cr._check_pending_orders()
                except Exception as exc:
                    console.print(f"[yellow]⚠ Sync ConnorsRSI falló: {exc}[/yellow]")
                    _slog.error("headless sync ConnorsRSI: %s", exc, exc_info=True)
            _autopiloto_cerrar_connors_automatico()
            slots = (
                config.CONNORS_MAX_POS
                - len(cr._get_open_positions())
                - cr._count_pending_orders()
            )
            if slots > 0:
                cr.aperturas_automaticas(reconciliar_primero=False, confirmar=False)

        _tlog.info("headless --run %s completado OK", mode)
        return 0

    except Exception as exc:
        _slog.error("Error en headless --run %s: %s", mode, exc, exc_info=True)
        console.print(f"[red]✗ Error en --run {mode}: {exc}[/red]")
        return 1


# ── Menú principal ────────────────────────────────────────────────────────────

def _print_menu() -> None:
    """Imprime el menú principal con Rich."""
    menu = Text(justify="left")
    menu.append("  1", style="bold magenta")
    menu.append(". Autopiloto (sistema completo)\n", style="white")
    menu.append("  2", style="bold cyan")
    menu.append(". ConnorsRSI — Mean Reversion\n", style="white")
    menu.append("  3", style="bold cyan")
    menu.append(". Resumen de capital\n", style="white")
    menu.append("  4", style="bold cyan")
    menu.append(". Backtesting períodos históricos\n", style="white")
    menu.append("  5", style="bold cyan")
    menu.append(". Operations Log\n", style="white")
    menu.append("  6", style="bold cyan")
    menu.append(". Validar universo (yfinance)\n", style="white")
    menu.append("  7", style="bold cyan")
    menu.append(". Ver logs\n", style="white")
    menu.append("  8", style="bold cyan")
    menu.append(". Estado Alpaca\n", style="white")
    menu.append("  0", style="bold red")
    menu.append(". Salir\n", style="white")

    console.print(
        Panel(
            menu,
            title="[bold white]SISTEMA DE TRADING[/bold white]",
            subtitle="[dim]ConnorsRSI · S&P 500[/dim]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 4),
        )
    )


_OPTIONS = {
    "1": _opcion_autopiloto,
    "2": _opcion_connors_submenu,
    "3": _opcion_capital,
    "4": _opcion_historical_backtests,
    "5": _opcion_operations_log,
    "6": _opcion_validar_universo,
    "7": _opcion_ver_logs,
    "8": _opcion_alpaca_estado,
}


def main() -> None:
    """Punto de entrada principal: muestra el menú y orquesta los módulos."""
    # Inicializar loggers al arrancar (crea los ficheros del día si no existen)
    _slog = get_system_logger()
    get_trading_logger()
    _slog.info("Arrancando --env %s", config.TRADING_ENV)

    console.clear()
    console.print()

    _inicio = datetime.now()
    _env_style = {"dev": "green", "pre": "yellow", "pro": "bold red"}.get(
        config.TRADING_ENV, "white"
    )
    console.print(Panel.fit(
        f"ENTORNO: [{_env_style}]{config.TRADING_ENV.upper()}[/{_env_style}]"
        f"   ·   BD: [dim]{config.DB_PATH}[/dim]"
        f"   ·   Inicio: [dim]{_inicio:%d/%m/%Y %H:%M:%S}[/dim]",
        border_style=_env_style.split()[-1],
    ))

    # Reconciliar órdenes ConnorsRSI pending con Alpaca ANTES de pintar el
    # panel de capital: si alguna orden límite se ejecutó desde la última
    # sesión, _check_pending_orders promueve la operación de 'pendiente' a
    # 'abierta' y descuenta cash, dejando el panel al día.
    if getattr(config, "ALPACA_ENABLED", False):
        try:
            from modules.connors_rsi import _check_pending_orders
            _check_pending_orders()
        except Exception as exc:
            console.print(
                f"[yellow]⚠ Reconciliación pending ConnorsRSI falló: {exc}[/yellow]"
            )

    # Mostrar estado del capital al arrancar (antes del primer render del menú).
    # Envuelto en try/except por si los módulos de paper trading aún no se
    # pueden inicializar (BD bloqueada, sin red para obtener precios actuales…).
    try:
        show_capital_panel()
    except Exception as exc:
        console.print(
            f"[yellow]⚠ No se pudo mostrar el resumen de capital: {exc}[/yellow]"
        )

    # Sincronización SQLite ↔ Alpaca al arrancar (sólo informa, no corrige).
    if getattr(config, "ALPACA_ENABLED", False):
        try:
            from modules.alpaca_broker import sync_alpaca_vs_sqlite
            sync_alpaca_vs_sqlite()
        except Exception as exc:
            console.print(f"[yellow]⚠ Sync Alpaca falló: {exc}[/yellow]")

    while True:
        _print_menu()
        choice = Prompt.ask(
            "[bold cyan]Opción[/bold cyan]",
            choices=list(_OPTIONS.keys()) + ["0"],
            default="0",
        )

        if choice == "0":
            console.print("\n[bold cyan]Hasta la próxima. ¡Buenas inversiones![/bold cyan]\n")
            break

        handler = _OPTIONS.get(choice)
        if handler:
            try:
                handler()
            except KeyboardInterrupt:
                console.print("\n[yellow]Operación cancelada por el usuario.[/yellow]")
            except Exception as exc:
                console.print(f"\n[red]✗ Error inesperado: {exc}[/red]")

        console.print()
        Prompt.ask("[dim]Pulsa Enter para continuar[/dim]", default="")
        console.clear()


if __name__ == "__main__":
    if _RUN_MODE is not None:
        sys.exit(_run_headless(_RUN_MODE))
    else:
        main()
