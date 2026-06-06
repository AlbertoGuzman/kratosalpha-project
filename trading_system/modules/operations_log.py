# -*- coding: utf-8 -*-
"""
Operations Log · inspección detallada de paper trading.

Lee read-only las tablas SQLite que mantiene `connors_rsi.py` (sin modificar su
schema) y ofrece vistas y agregaciones que el submenú de la estrategia no
muestra:
  · Posiciones abiertas (con PnL no realizado).
  · Diario cronológico de aperturas/cierres.
  · Detalle de un trade concreto (todos los campos).
  · Estadísticas por ticker, filtros y exportación filtrada a CSV.
  · Resumen de PnL realizado por mes.
"""

import sys
import sqlite3
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich import box

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

console = Console()

_DB_PATH     = config.DB_PATH
_EXPORTS_DIR = Path(__file__).parent.parent / "exports"


# ── Helpers internos ──────────────────────────────────────────────────────────

def _get_connection(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    """Devuelve conexión con `row_factory=Row` o None si la BD no existe."""
    path = Path(db_path) if db_path else _DB_PATH
    if not path.exists():
        console.print(f"[yellow]⚠ Base de datos no encontrada: {path}[/yellow]")
        return None
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _has_table(con: sqlite3.Connection, tabla: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tabla,),
    ).fetchone()
    return row is not None


def _try_get_current_price(ticker: str) -> float | None:
    """Precio actual vía yfinance (ventana de 15 días). None si falla."""
    try:
        import yfinance as yf
        df = yf.download(
            ticker,
            start=(date.today() - timedelta(days=15)).strftime("%Y-%m-%d"),
            end=date.today().strftime("%Y-%m-%d"),
            auto_adjust=True, progress=False,
        )
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
            df = df.loc[:, ~df.columns.duplicated()]
        if "Close" not in df.columns:
            return None
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def _days_open(entry_date_str: str) -> int:
    try:
        ed = datetime.fromisoformat(str(entry_date_str)[:10]).date()
        return (date.today() - ed).days
    except Exception:
        return 0


# ── A. Posiciones abiertas ────────────────────────────────────────────────────

def ver_abiertas_unificadas(db_path: Path | str | None = None) -> None:
    """Muestra todas las posiciones abiertas de ConnorsRSI en una tabla."""
    con = _get_connection(db_path)
    if con is None:
        return
    abiertas: list = []
    if _has_table(con, "connors_operaciones"):
        for r in con.execute(
            "SELECT * FROM connors_operaciones WHERE estado = 'abierta'"
        ).fetchall():
            abiertas.append(dict(r))
    con.close()

    if not abiertas:
        console.print("[dim]No hay posiciones abiertas.[/dim]")
        return

    # Precios actuales (deduplicados por ticker)
    tickers_unicos = sorted({pos["ticker"] for pos in abiertas})
    console.print(
        f"[cyan]Obteniendo precios actuales de {len(tickers_unicos)} tickers...[/cyan]"
    )
    precios = {t: _try_get_current_price(t) for t in tickers_unicos}

    table = Table(
        title="[bold cyan]Posiciones abiertas · ConnorsRSI[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    for c in ("Ticker", "Fecha ent.", "Días",
              "P.entrada", "P.actual", "Valor compra",
              "PnL no real.", "Ret %", "Stop loss"):
        table.add_column(c, justify="center")

    pnl_total = 0.0

    for pos in sorted(abiertas, key=lambda p: p["ticker"]):
        ticker  = pos["ticker"]
        entry_p = float(pos["entry_price"])
        shares  = float(pos["shares"])
        cur_p   = precios.get(ticker)
        if cur_p is None:
            cur_p = entry_p
        pnl     = shares * (cur_p - entry_p)
        ret     = (cur_p - entry_p) / entry_p * 100 if entry_p else 0.0
        dias    = _days_open(pos["entry_date"])
        sl_p    = pos.get("stop_loss_price")
        sl_str  = f"{float(sl_p):.2f}" if sl_p else "—"
        pc      = "green" if pnl >= 0 else "red"

        table.add_row(
            ticker, str(pos["entry_date"])[:10], str(dias),
            f"{entry_p:.2f}", f"{cur_p:.2f}",
            f"{float(pos['valor_compra']):,.2f}",
            f"[{pc}]{pnl:>+9,.2f} €[/{pc}]",
            f"[{pc}]{ret:+5.1f} %[/{pc}]",
            sl_str,
        )
        pnl_total += pnl

    console.print(table)

    tot_col = "green" if pnl_total >= 0 else "red"
    txt = Text()
    txt.append("  PnL no realizado : ", style="bold white")
    txt.append(f"{pnl_total:>+10,.2f} €\n", style=f"bold {tot_col}")
    console.print(Panel(
        txt, title="[bold cyan]Resumen[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


# ── B. Diario cronológico ─────────────────────────────────────────────────────

def ver_diario_cronologico(
    dias: int = 30, db_path: Path | str | None = None,
) -> None:
    """Timeline de aperturas/cierres de los últimos N días (ConnorsRSI)."""
    con = _get_connection(db_path)
    if con is None:
        return
    cutoff = (date.today() - timedelta(days=dias)).isoformat()

    eventos: list = []  # (fecha, estr, accion, ticker, precio, pnl)
    estr = "CONNORS"

    if _has_table(con, "connors_operaciones"):
        # Operaciones materializadas (abierta/cerrada): generan APERTURA por
        # entry_date y, si están cerradas, CIERRE por exit_date. Las pendientes
        # (sin fill) no producen eventos.
        for r in con.execute(
            "SELECT * FROM connors_operaciones WHERE estado IN ('abierta','cerrada')"
        ).fetchall():
            d = dict(r)
            if d.get("entry_date") and str(d["entry_date"])[:10] >= cutoff:
                eventos.append((
                    str(d["entry_date"])[:10], estr, "APERTURA",
                    d["ticker"], d.get("entry_price"), None,
                ))
            if d.get("exit_date") and str(d["exit_date"])[:10] >= cutoff:
                eventos.append((
                    str(d["exit_date"])[:10], estr, "CIERRE",
                    d["ticker"], d.get("exit_price"), d.get("net_pnl"),
                ))
    con.close()

    if not eventos:
        console.print(f"[dim]Sin eventos en los últimos {dias} días.[/dim]")
        return

    eventos.sort(key=lambda e: e[0], reverse=True)

    table = Table(
        title=f"[bold cyan]Diario cronológico · últimos {dias} días[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    for c in ("Fecha", "Estrategia", "Acción", "Ticker", "Precio", "PnL €"):
        table.add_column(c, justify="center")
    for fecha, estr, accion, ticker, precio, pnl in eventos:
        acc_col = "green" if accion == "APERTURA" else "red"
        if pnl is None:
            pnl_str = "—"
        else:
            pc = "green" if pnl >= 0 else "red"
            pnl_str = f"[{pc}]{pnl:>+9,.2f}[/{pc}]"
        table.add_row(
            fecha, estr,
            f"[{acc_col}]{accion}[/{acc_col}]",
            ticker,
            f"{float(precio):.2f}" if precio is not None else "—",
            pnl_str,
        )
    console.print(table)


# ── C. Detalle de un trade ────────────────────────────────────────────────────

def ver_detalle_trade(
    trade_id: int, estrategia: str, db_path: Path | str | None = None,
) -> None:
    """Imprime todos los campos del trade `trade_id` en la tabla de `estrategia`."""
    con = _get_connection(db_path)
    if con is None:
        return
    tabla = "connors_operaciones"
    if not _has_table(con, tabla):
        console.print(f"[yellow]⚠ Tabla {tabla} no existe.[/yellow]")
        con.close()
        return
    row = con.execute(f"SELECT * FROM {tabla} WHERE id = ?", (trade_id,)).fetchone()
    con.close()
    if row is None:
        console.print(
            f"[yellow]Trade id={trade_id} no encontrado en {tabla}.[/yellow]"
        )
        return

    txt = Text()
    for key in row.keys():
        v = row[key]
        if isinstance(v, float):
            txt.append(f"  {key:<24s}: {v:,.6f}\n", style="white")
        else:
            txt.append(f"  {key:<24s}: {v}\n", style="white")
    console.print(Panel(
        txt,
        title=f"[bold cyan]Trade #{trade_id} · {estrategia.upper()}[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


# ── D. Estadísticas por ticker ────────────────────────────────────────────────

def estadisticas_por_ticker(
    estrategia: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Tabla Rich con ops / WR / PnL acumulado / mejor / peor / ret medio por ticker."""
    con = _get_connection(db_path)
    if con is None:
        return
    todos: list = []
    if _has_table(con, "connors_operaciones"):
        for r in con.execute(
            "SELECT * FROM connors_operaciones WHERE estado = 'cerrada'"
        ).fetchall():
            d = dict(r)
            d["estrategia"] = "CONNORS"
            todos.append(d)
    con.close()

    if not todos:
        console.print("[dim]No hay operaciones cerradas todavía.[/dim]")
        return

    agg: dict = {}
    for t in todos:
        tk = t["ticker"]
        if tk not in agg:
            agg[tk] = {"n": 0, "wins": 0, "pnl": 0.0,
                       "best": float("-inf"), "worst": float("inf"),
                       "rets": []}
        pnl = float(t.get("net_pnl") or 0.0)
        agg[tk]["n"]   += 1
        agg[tk]["pnl"] += pnl
        if pnl > 0:
            agg[tk]["wins"] += 1
        if pnl > agg[tk]["best"]:
            agg[tk]["best"]  = pnl
        if pnl < agg[tk]["worst"]:
            agg[tk]["worst"] = pnl
        ret = t.get("return_pct")
        if ret is not None:
            agg[tk]["rets"].append(float(ret))

    rows = sorted(agg.items(), key=lambda x: -x[1]["pnl"])

    title = "Estadísticas por ticker"
    if estrategia:
        title += f" · {estrategia.upper()}"
    table = Table(
        title=f"[bold cyan]{title}[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    for c in ("Ticker", "Ops", "Win rate",
              "PnL acumulado", "Mejor", "Peor", "Ret medio"):
        table.add_column(c, justify="center")

    for tk, d in rows:
        wr      = (d["wins"] / d["n"] * 100) if d["n"] else 0.0
        ret_med = (sum(d["rets"]) / len(d["rets"]) * 100) if d["rets"] else 0.0
        pc      = "green" if d["pnl"] >= 0 else "red"
        table.add_row(
            tk, str(d["n"]), f"{wr:.0f} %",
            f"[{pc}]{d['pnl']:>+9,.2f} €[/{pc}]",
            f"{d['best']:>+8,.2f}",
            f"{d['worst']:>+8,.2f}",
            f"{ret_med:+5.1f} %",
        )
    console.print(table)


# ── E. Filtrar trades ─────────────────────────────────────────────────────────

def filtrar_trades(
    estrategia: str | None = None,
    ticker: str | None = None,
    fecha_desde: str | None = None,
    fecha_hasta: str | None = None,
    motivo: str | None = None,
    solo_ganadoras: bool | None = None,
    db_path: Path | str | None = None,
) -> list:
    """Devuelve los trades cerrados que cumplen TODOS los filtros activos."""
    con = _get_connection(db_path)
    if con is None:
        return []

    resultado: list = []
    ticker_u   = ticker.upper() if ticker else None
    if _has_table(con, "connors_operaciones"):
        for r in con.execute(
            "SELECT * FROM connors_operaciones WHERE estado = 'cerrada'"
        ).fetchall():
            d = dict(r)
            d["estrategia"] = "CONNORS"
            if ticker_u and str(d.get("ticker", "")).upper() != ticker_u:
                continue
            if motivo and d.get("reason") != motivo:
                continue
            ed = str(d.get("exit_date") or "")[:10]
            if fecha_desde and ed and ed < fecha_desde:
                continue
            if fecha_hasta and ed and ed > fecha_hasta:
                continue
            pnl = float(d.get("net_pnl") or 0.0)
            if solo_ganadoras is True and pnl <= 0:
                continue
            if solo_ganadoras is False and pnl > 0:
                continue
            resultado.append(d)
    con.close()
    return resultado


def exportar_filtrado(
    filtros_dict: dict,
    filename: Path | str | None = None,
    db_path: Path | str | None = None,
) -> Path | None:
    """Llama a `filtrar_trades(**filtros_dict)` y exporta el resultado a CSV."""
    trades = filtrar_trades(db_path=db_path, **filtros_dict)
    if not trades:
        console.print("[yellow]Sin trades que cumplan los filtros.[/yellow]")
        return None
    if filename is None:
        _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = _EXPORTS_DIR / f"trades_filtrados_{date.today().strftime('%Y%m%d')}.csv"
    else:
        filename = Path(filename)
        filename.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(trades).to_csv(filename, index=False, encoding="utf-8-sig")
    console.print(f"[green]✓ Exportado: {filename} ({len(trades)} trades)[/green]")
    return filename


# ── F. Resumen PnL por mes ────────────────────────────────────────────────────

def resumen_pnl_por_mes(db_path: Path | str | None = None) -> None:
    """Tabla año-mes con nº de trades cerrados y PnL realizado ConnorsRSI."""
    con = _get_connection(db_path)
    if con is None:
        return
    by_month: dict = {}
    if _has_table(con, "connors_operaciones"):
        for r in con.execute(
            "SELECT * FROM connors_operaciones "
            "WHERE estado = 'cerrada' AND exit_date IS NOT NULL"
        ).fetchall():
            d = dict(r)
            ym = str(d.get("exit_date") or "")[:7]   # YYYY-MM
            if len(ym) != 7:
                continue
            if ym not in by_month:
                by_month[ym] = {"cnt": 0, "pnl": 0.0}
            by_month[ym]["cnt"] += 1
            by_month[ym]["pnl"] += float(d.get("net_pnl") or 0.0)
    con.close()

    if not by_month:
        console.print("[dim]Sin operaciones cerradas todavía.[/dim]")
        return

    table = Table(
        title="[bold cyan]PnL realizado por mes · ConnorsRSI[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    for c in ("Año-Mes", "Nº trades", "PnL"):
        table.add_column(c, justify="center")
    for ym in sorted(by_month.keys()):
        d   = by_month[ym]
        t_c = "green" if d["pnl"] >= 0 else "red"
        table.add_row(
            ym, str(d["cnt"]),
            f"[bold {t_c}]{d['pnl']:>+9,.2f}[/bold {t_c}]",
        )
    console.print(table)


def evolucion_capital(db_path: Path | str | None = None) -> None:
    """
    Tabla de evolucion diaria del capital ConnorsRSI (ultimo snapshot por dia),
    con PnL y PnL% respecto al inicio.
    """
    con = _get_connection(db_path)
    if con is None:
        return

    def _snapshots(tabla: str) -> list[dict]:
        if not _has_table(con, tabla):
            return []
        rows = con.execute(
            f"SELECT fecha, capital_total FROM {tabla} "
            f"WHERE id IN (SELECT MAX(id) FROM {tabla} GROUP BY fecha) "
            f"ORDER BY fecha"
        ).fetchall()
        return [dict(r) for r in rows]

    snaps_c = _snapshots("connors_capital")

    def _initial_cash(tabla: str) -> float:
        """Cash de la primera fila (antes de comisiones ni rebalanceos)."""
        if not _has_table(con, tabla):
            return 0.0
        row = con.execute(
            f"SELECT cash FROM {tabla} ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return float(row["cash"]) if row else 0.0

    ini_c = _initial_cash("connors_capital")
    con.close()

    if not snaps_c:
        console.print("[dim]Sin datos de capital todavía.[/dim]")
        return

    by_c: dict = {r["fecha"]: float(r["capital_total"]) for r in snaps_c}

    # Añadir/reemplazar el valor de hoy con capital vivo (precios actuales).
    # Se desactiva Alpaca temporalmente para usar yfinance cache en vez del broker.
    hoy = date.today().isoformat()
    try:
        from modules.connors_rsi import get_capital_summary_connors
        _alpaca_prev = config.ALPACA_ENABLED
        config.ALPACA_ENABLED = False
        try:
            live_c = get_capital_summary_connors()
            if live_c:
                by_c[hoy] = live_c["capital_actual"]
        finally:
            config.ALPACA_ENABLED = _alpaca_prev
    except Exception:
        pass

    all_dates = sorted(by_c)

    def _pct(v: float | None, ini: float) -> str:
        if v is None or ini == 0:
            return "[dim]---[/dim]"
        p = (v - ini) / ini * 100
        c = "green" if p >= 0 else "red"
        return f"[{c}]{p:>+6.2f}%[/{c}]"

    def _val(v: float | None) -> str:
        return "[dim]---[/dim]" if v is None else f"{v:>9,.2f}"

    table = Table(
        title="[bold cyan]Evolucion de capital -- ultimo snapshot por dia[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    table.add_column("Fecha",   justify="center", style="white")
    table.add_column("Connors", justify="right", style="bold")
    table.add_column("Conn %",  justify="right", style="bold")

    for d in all_dates:
        cap_c = by_c.get(d)
        table.add_row(d, _val(cap_c), _pct(cap_c, ini_c))

    last_c    = by_c[max(by_c)] if by_c else ini_c
    pnl_fin   = last_c - ini_c
    pct_fin   = pnl_fin / ini_c * 100 if ini_c else 0.0
    fc        = "green" if pct_fin >= 0 else "red"
    table.add_section()
    table.add_row(
        "[bold]ACUMULADO[/bold]",
        f"[bold {fc}]{pnl_fin:>+9,.2f}[/bold {fc}]",
        f"[bold {fc}]{pct_fin:>+6.2f}%[/bold {fc}]",
    )

    console.print(table)


# ── Submenú A-G ───────────────────────────────────────────────────────────────

def _ask_filtros() -> dict:
    """Diálogo para construir un dict de filtros para `filtrar_trades`."""
    estr   = "CONNORS"
    ticker = Prompt.ask("  Ticker (Enter=todos)", default="").strip().upper() or None
    desde  = Prompt.ask("  Fecha desde YYYY-MM-DD (Enter=sin límite)", default="").strip() or None
    hasta  = Prompt.ask("  Fecha hasta YYYY-MM-DD (Enter=sin límite)", default="").strip() or None
    motivo = Prompt.ask("  Motivo (Enter=todos)", default="").strip() or None
    g      = Prompt.ask("  Solo ganadoras (y/n/Enter=todas)", default="").strip().lower()
    solo_g = True if g == "y" else (False if g == "n" else None)
    return {
        "estrategia": estr, "ticker": ticker,
        "fecha_desde": desde, "fecha_hasta": hasta,
        "motivo": motivo, "solo_ganadoras": solo_g,
    }


def _interactivo_detalle() -> None:
    estr = "CONNORS"
    tid_raw = Prompt.ask("  ID del trade", default="").strip()
    try:
        tid = int(tid_raw)
    except ValueError:
        console.print("[red]ID inválido.[/red]")
        return
    ver_detalle_trade(tid, estr)


def _interactivo_stats() -> None:
    estadisticas_por_ticker(estrategia="CONNORS")


def _interactivo_filtrar() -> None:
    filtros = _ask_filtros()
    trades  = filtrar_trades(**filtros)
    if not trades:
        console.print("[yellow]Sin resultados.[/yellow]")
        return
    table = Table(
        title=f"[bold cyan]Resultados del filtro ({len(trades)} trades)[/bold cyan]",
        box=box.ROUNDED, border_style="cyan", show_header=True,
    )
    for c in ("Estrategia", "Ticker", "Entrada", "Salida",
              "PnL €", "Ret %", "Motivo"):
        table.add_column(c, justify="center")
    for t in trades[:50]:
        pnl = float(t.get("net_pnl") or 0.0)
        ret = float(t.get("return_pct") or 0.0) * 100
        pc  = "green" if pnl >= 0 else "red"
        table.add_row(
            t.get("estrategia") or "—",
            t.get("ticker") or "—",
            str(t.get("entry_date"))[:10] if t.get("entry_date") else "—",
            str(t.get("exit_date"))[:10]  if t.get("exit_date")  else "—",
            f"[{pc}]{pnl:>+9,.2f}[/{pc}]",
            f"[{pc}]{ret:+5.1f} %[/{pc}]",
            t.get("reason") or "—",
        )
    console.print(table)
    if len(trades) > 50:
        console.print(f"[dim]Mostrando 50 de {len(trades)}.[/dim]")


def _interactivo_exportar() -> None:
    filtros = _ask_filtros()
    exportar_filtrado(filtros)


_OPTIONS = {
    "A": ("Ver posiciones abiertas (ConnorsRSI)",
          lambda: ver_abiertas_unificadas()),
    "B": ("Diario cronológico (últimos 30 días)",
          lambda: ver_diario_cronologico(30)),
    "C": ("Detalle de un trade concreto",
          _interactivo_detalle),
    "D": ("Estadísticas por ticker",
          _interactivo_stats),
    "E": ("Filtrar trades (interactivo)",
          _interactivo_filtrar),
    "F": ("Resumen PnL realizado por mes",
          lambda: resumen_pnl_por_mes()),
    "G": ("Exportar trades filtrados a CSV",
          _interactivo_exportar),
    "H": ("Evolución de capital por día",
          lambda: evolucion_capital()),
}


def menu() -> None:
    """Submenú principal A-G + 0 para volver."""
    while True:
        body = Text()
        for k, (label, _) in _OPTIONS.items():
            body.append(f"  {k}", style="bold cyan")
            body.append(f". {label}\n", style="white")
        body.append("  0", style="bold red")
        body.append(". Volver al menú principal\n", style="white")
        console.print()
        console.print(Panel(
            body,
            title="[bold white]OPERATIONS LOG · INSPECCIÓN DETALLADA[/bold white]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
            padding=(1, 4),
        ))
        raw_choice = Prompt.ask(
            "[bold cyan]Opción[/bold cyan]", default="0",
        ).strip().upper()
        if raw_choice == "0":
            break
        handler = _OPTIONS.get(raw_choice)
        if handler is None:
            console.print("[red]Opción inválida.[/red]")
            continue
        try:
            handler[1]()
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelado por el usuario.[/yellow]")
        except Exception as exc:
            console.print(f"\n[red]✗ Error inesperado: {exc}[/red]")
        console.print()
        Prompt.ask("[dim]Pulsa Enter para continuar[/dim]", default="")
