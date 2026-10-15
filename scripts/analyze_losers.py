# -*- coding: utf-8 -*-
"""
Análisis de patrones en operaciones perdedoras con validación IS/OOS.

Config fija: ENTRY_LIMIT=0.010, FILL_SIM=True, LOOKAHEAD_FIX=True, 1999-2026.
~7.000 operaciones. Split cronológico 70/30 para validación.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\analyze_losers.py
    C:\\trading_env\\Scripts\\python scripts\\analyze_losers.py --skip-download
"""

import sys
import os
import argparse
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

# ── Parámetros fijos de análisis ──────────────────────────────────────────────
ENTRY_LIMIT = 0.010
FILL_SIM    = True
START_DATE  = "1999-01-01"
IS_RATIO    = 0.70    # 70% cronológico → IS; 30% → OOS

# Umbrales de pérdida no realizada (%) a probar en búsqueda de patrón
DEPTH_THRESHOLDS = [-0.5, -1.0, -1.5, -2.0, -2.5, -3.0, -3.5, -4.0, -4.5]
DAY_CHECKPOINTS  = [1, 2, 3, 4]   # día de holding en que medimos (1 = día después de entrada)

REASONS = ("SL", "TSTO", "CRSI_EXIT")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="Análisis de patrones en operaciones perdedoras")
    p.add_argument("--skip-download", action="store_true",
                   help="Reusar datos en sesión (si ya se ejecutó el backtest)")
    p.add_argument("--start", default=START_DATE)
    p.add_argument("--end",   default=date.today().strftime("%Y-%m-%d"))
    return p.parse_args()


def _daily_unrealized(trade: dict, session_data: dict) -> list | None:
    """
    P&L no realizado (%) por sesión entre entry_date y exit_date inclusive.
    Usa entry_price real (fill price con FILL_SIM — puede diferir de limit_price en gap).

    Params:
        trade:        dict de trade del backtest.
        session_data: dict {ticker: DataFrame OHLCV}.
    Returns:
        Lista de floats o None si no hay datos.
    """
    ticker     = trade["ticker"]
    entry_date = pd.Timestamp(trade["entry_date"])
    exit_date  = pd.Timestamp(trade["exit_date"])
    entry_p    = trade["entry_price"]

    df = session_data.get(ticker)
    if df is None or df.empty or entry_p <= 0:
        return None

    mask   = (df.index >= entry_date) & (df.index <= exit_date)
    closes = df.loc[mask, "Close"]
    if closes.empty:
        return None

    return ((closes - entry_p) / entry_p * 100).tolist()


def _enrich(trades: list, session_data: dict) -> list:
    """
    Añade campos de análisis a cada trade: daily_pnl, max_drawdown, pnl_at_dayN, winner.

    Params:
        trades:       lista de dicts de trades del backtest.
        session_data: dict {ticker: DataFrame OHLCV}.
    Returns:
        Lista de trades enriquecidos (sin los que no tienen datos de precio).
    """
    enriched = []
    skipped  = 0
    for t in trades:
        daily = _daily_unrealized(t, session_data)
        if not daily:
            skipped += 1
            continue
        t = dict(t)
        t["daily_pnl"]       = daily
        t["n_days"]          = len(daily)
        t["max_drawdown"]    = min(daily)
        t["ever_negative"]   = any(d < 0 for d in daily)
        t["n_neg_days"]      = sum(1 for d in daily if d < 0)
        t["winner"]          = (t.get("net_pnl") or 0) > 0
        for dn in DAY_CHECKPOINTS:
            t[f"pnl_d{dn}"] = daily[dn - 1] if len(daily) >= dn else daily[-1]
        enriched.append(t)
    if skipped:
        console.print(f"[dim]  ({skipped} trades sin datos de precio — omitidos)[/dim]")
    return enriched


def _split(trades: list) -> tuple:
    """
    Split cronológico 70/30 por entry_date.

    Returns:
        (is_trades, oos_trades)
    """
    s = sorted(trades, key=lambda t: t["entry_date"])
    cut = int(len(s) * IS_RATIO)
    return s[:cut], s[cut:]


def _pct(n, total):
    return f"{n/total*100:.1f}%" if total else "—"


# ── Sección 1: descriptivo por motivo de salida ───────────────────────────────

def _section_descriptivo(trades: list) -> None:
    """
    Tabla resumen: por cada motivo de salida, distribución de P&L no realizado y final.

    Params:
        trades: lista de trades enriquecidos (todos, IS+OOS).
    """
    console.print("\n[bold cyan]1 · Distribución por motivo de salida[/bold cyan]")

    tbl = Table(box=box.ROUNDED, border_style="cyan")
    tbl.add_column("Motivo",        justify="center", style="bold")
    tbl.add_column("N total",       justify="right")
    tbl.add_column("Ganadoras",     justify="right")
    tbl.add_column("WR%",           justify="right")
    tbl.add_column("Max DD medio",  justify="right")
    tbl.add_column("% con DD<-2%",  justify="right")
    tbl.add_column("% que pasan\npor pérdida", justify="right")

    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.get("reason", "?")].append(t)

    for reason in REASONS:
        ts = by_reason.get(reason, [])
        if not ts:
            tbl.add_row(reason, "0", "—", "—", "—", "—", "—")
            continue
        n      = len(ts)
        n_win  = sum(1 for t in ts if t["winner"])
        dd_med = sum(t["max_drawdown"] for t in ts) / n
        dd2    = sum(1 for t in ts if t["max_drawdown"] < -2.0)
        ev_neg = sum(1 for t in ts if t["ever_negative"])

        wr_c = "green" if n_win / n >= 0.5 else "red"
        tbl.add_row(
            reason,
            str(n),
            str(n_win),
            f"[{wr_c}]{_pct(n_win, n)}[/{wr_c}]",
            f"{dd_med:+.2f}%",
            _pct(dd2, n),
            _pct(ev_neg, n),
        )

    console.print(tbl)


# ── Sección 2: CRSI_EXIT — ¿cuántas pasan por rojo antes de girar? ───────────

def _section_crsi(trades: list) -> None:
    """
    Para operaciones CRSI_EXIT: desglose de las que pasan por pérdida no realizada
    antes de cerrar en positivo.

    Params:
        trades: lista de trades enriquecidos (todos, IS+OOS).
    """
    console.print("\n[bold cyan]2 · CRSI_EXIT — evolución interna[/bold cyan]")

    ts = [t for t in trades if t.get("reason") == "CRSI_EXIT"]
    if not ts:
        console.print("  Sin datos CRSI_EXIT.")
        return

    winners  = [t for t in ts if     t["winner"]]
    losers   = [t for t in ts if not t["winner"]]

    # De las ganadoras: ¿cuántas estuvieron en rojo durante el holding?
    win_went_red = sum(1 for t in winners if t["ever_negative"])
    # De las perdedoras: ¿cuántas alcanzaron DD > 2%?
    loss_deep    = sum(1 for t in losers  if t["max_drawdown"] < -2.0)

    lines = [
        f"  Total CRSI_EXIT: {len(ts)}  |  Ganadoras: {len(winners)} ({_pct(len(winners), len(ts))})  "
        f"|  Perdedoras: {len(losers)} ({_pct(len(losers), len(ts))})",
        f"  Ganadoras que pasaron por pérdida no realizada: "
        f"[yellow]{win_went_red}[/yellow] ({_pct(win_went_red, len(winners))})"
        f" — éstas habrían sido sacrificadas por una regla de corte temprano.",
        f"  Perdedoras con DD > 2%: [red]{loss_deep}[/red] ({_pct(loss_deep, len(losers))})",
    ]
    console.print(Panel(
        Text.from_markup("\n".join(lines)),
        title="[bold cyan]CRSI_EXIT[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))

    # P&L en cada checkpoint para ganadoras vs perdedoras
    tbl = Table(box=box.SIMPLE, border_style="dim")
    tbl.add_column("Día",      justify="center")
    tbl.add_column("Ganad. media %", justify="right")
    tbl.add_column("Perd. media %",  justify="right")
    tbl.add_column("Diferencia",     justify="right")

    for dn in DAY_CHECKPOINTS:
        key  = f"pnl_d{dn}"
        w_ts = [t for t in winners if t["n_days"] >= dn]
        l_ts = [t for t in losers  if t["n_days"] >= dn]
        if not w_ts or not l_ts:
            continue
        w_avg = sum(t[key] for t in w_ts) / len(w_ts)
        l_avg = sum(t[key] for t in l_ts) / len(l_ts)
        diff  = w_avg - l_avg
        tbl.add_row(
            f"D+{dn}",
            f"[green]{w_avg:+.2f}%[/green]",
            f"[red]{l_avg:+.2f}%[/red]",
            f"[{'green' if diff >= 0 else 'red'}]{diff:+.2f}pp[/{'green' if diff >= 0 else 'red'}]",
        )

    console.print(tbl)


# ── Sección 3: regla de corte temprano sobre universo completo ────────────────

def _evaluate_rule_global(trades_all: list, n_universe: int, day_n: int, threshold: float) -> dict:
    """
    Evalúa "si pnl_dN < threshold → salir anticipado" sobre el universo completo de trades.
    El denominador del impacto económico es n_universe (todas las operaciones), no solo
    las flaggeadas, para que la pp/trade sea comparable entre reglas.

    Params:
        trades_all:  todas las trades enriquecidas (IS o OOS, sin filtrar por reason).
        n_universe:  total de trades del bloque (IS o OOS) — denominador del impacto.
        day_n:       día de holding en que se aplica la regla.
        threshold:   umbral de pérdida no realizada (%).
    Returns:
        Dict con métricas de impacto.
    """
    key      = f"pnl_d{day_n}"
    eligible = [t for t in trades_all if t["n_days"] >= day_n]
    if not eligible:
        return {}

    flagged = [t for t in eligible if t[key] < threshold]
    if not flagged:
        return {}

    # Desglose de las flaggeadas por motivo × resultado
    breakdown = defaultdict(int)
    for t in flagged:
        reason = t.get("reason", "?")
        outcome = "gan" if t["winner"] else "per"
        breakdown[f"{reason}_{outcome}"] += 1

    # Impacto económico: si saliéramos en day_n en lugar de esperar al cierre real
    # net_trade = pnl_dN - final_pnl
    #   > 0 para perdedoras: salimos antes de que profundice (ahorro)
    #   < 0 para ganadoras: cortamos la recuperación (coste)
    net_by_trade = [t[key] - t["daily_pnl"][-1] for t in flagged]
    saved_abs    = sum(x for x in net_by_trade if x > 0)   # ahorro en perdedoras
    cost_abs     = sum(-x for x in net_by_trade if x < 0)  # coste en ganadoras

    n_losers  = sum(1 for t in flagged if not t["winner"])
    n_winners = sum(1 for t in flagged if     t["winner"])

    return {
        "day_n":      day_n,
        "threshold":  threshold,
        "n_eligible": len(eligible),
        "n_flagged":  len(flagged),
        "n_losers_flagged":  n_losers,
        "n_winners_flagged": n_winners,
        "breakdown":  dict(breakdown),
        # Impacto por trade sobre el universo completo del bloque
        "saved_pp":   saved_abs  / n_universe,
        "cost_pp":    cost_abs   / n_universe,
        "net_pp":     (saved_abs - cost_abs) / n_universe,
    }


def _section_regla_global(trades: list) -> None:
    """
    Busca el mejor umbral de corte temprano sobre las ~7000 operaciones (SL+TSTO+CRSI_EXIT).
    Mide el coste real en CRSI_EXIT ganadoras sacrificadas y el ahorro real en perdedoras
    de todos los motivos. Valida IS/OOS.

    Params:
        trades: lista de trades enriquecidos (todos, IS+OOS).
    """
    console.print(
        "\n[bold cyan]3 · Regla de corte temprano — universo completo (~7000 trades)[/bold cyan]"
    )

    if not trades:
        console.print("  Sin trades.")
        return

    is_trades, oos_trades = _split(trades)
    n_is  = len(is_trades)
    n_oos = len(oos_trades)

    n_is_win  = sum(1 for t in is_trades  if     t["winner"])
    n_oos_win = sum(1 for t in oos_trades if     t["winner"])

    console.print(
        f"  IS:  {n_is} trades · ganadoras {n_is_win} ({_pct(n_is_win, n_is)}) · "
        f"perdedoras {n_is - n_is_win} ({_pct(n_is - n_is_win, n_is)})\n"
        f"  OOS: {n_oos} trades · ganadoras {n_oos_win} ({_pct(n_oos_win, n_oos)}) · "
        f"perdedoras {n_oos - n_oos_win} ({_pct(n_oos - n_oos_win, n_oos)})"
    )

    # ── Barrido en IS: criterio = maximizar net_pp con n_flagged ≥ 30 ───────
    best_is = None
    all_is_results = []
    for dn in DAY_CHECKPOINTS:
        for thr in DEPTH_THRESHOLDS:
            m = _evaluate_rule_global(is_trades, n_is, dn, thr)
            if not m or m.get("n_flagged", 0) < 30:
                continue
            all_is_results.append(m)
            if best_is is None or m["net_pp"] > best_is["net_pp"]:
                best_is = m

    if best_is is None:
        console.print("  [yellow]Ninguna regla con N suficiente en IS.[/yellow]")
        return

    dn_best  = best_is["day_n"]
    thr_best = best_is["threshold"]

    console.print(
        f"\n  Mejor regla IS: pnl_D+{dn_best} < [bold]{thr_best:.1f}%[/bold]  →  salir anticipado"
    )

    # ── Validación OOS con la misma regla ────────────────────────────────────
    m_oos = _evaluate_rule_global(oos_trades, n_oos, dn_best, thr_best)

    # Tabla principal IS vs OOS
    tbl = Table(
        box=box.ROUNDED, border_style="cyan",
        title=f"[bold cyan]Regla: pnl_D+{dn_best} < {thr_best:.1f}% — universo completo[/bold cyan]",
    )
    tbl.add_column("Métrica",      justify="left",  style="bold")
    tbl.add_column("IS (train)",   justify="right")
    tbl.add_column("OOS (valid.)", justify="right")
    tbl.add_column("Valoración",   justify="left")

    def _vrd(is_v, oos_v, higher_better=True):
        d = (oos_v - is_v) if higher_better else (is_v - oos_v)
        c = "green" if d >= -0.002 else ("yellow" if d >= -0.005 else "red")
        lbl = "ok" if d >= -0.002 else ("↓" if d >= -0.005 else "↓↓")
        return f"[{c}]{lbl}[/{c}]"

    tbl.add_row("N universo (denominador)",   str(n_is),  str(n_oos),  "—")
    tbl.add_row("N elegibles (n_days≥D+N)",
                str(best_is["n_eligible"]), str(m_oos.get("n_eligible", 0)), "—")
    tbl.add_row("N flagged (regla activa)",
                str(best_is["n_flagged"]),  str(m_oos.get("n_flagged", 0)),  "—")
    tbl.add_row("  · perdedoras flagged",
                str(best_is["n_losers_flagged"]),
                str(m_oos.get("n_losers_flagged", 0)), "—")
    tbl.add_row("  · ganadoras flagged (coste)",
                str(best_is["n_winners_flagged"]),
                str(m_oos.get("n_winners_flagged", 0)), "—")

    sv_is  = best_is["saved_pp"];  sv_oos = m_oos.get("saved_pp", 0)
    ct_is  = best_is["cost_pp"];   ct_oos = m_oos.get("cost_pp", 0)
    nt_is  = best_is["net_pp"];    nt_oos = m_oos.get("net_pp", 0)

    tbl.add_row("Ahorro (pp/trade universo)",
                f"[green]+{sv_is:.4f}[/green]", f"[green]+{sv_oos:.4f}[/green]",
                _vrd(sv_is, sv_oos))
    tbl.add_row("Coste (pp/trade universo)",
                f"[red]-{ct_is:.4f}[/red]",    f"[red]-{ct_oos:.4f}[/red]",
                _vrd(-ct_is, -ct_oos))

    c_n_is  = "green" if nt_is  > 0 else "red"
    c_n_oos = "green" if nt_oos > 0 else "red"
    tbl.add_row(
        "[bold]Impacto neto (pp/trade)[/bold]",
        f"[{c_n_is}]{nt_is:+.4f}[/{c_n_is}]",
        f"[{c_n_oos}]{nt_oos:+.4f}[/{c_n_oos}]",
        _vrd(nt_is, nt_oos),
    )

    console.print(tbl)

    # Desglose de las flagged (OOS) por motivo × resultado
    brkd = m_oos.get("breakdown", {})
    if brkd:
        console.print("\n  [dim]Desglose flagged OOS por motivo:[/dim]")
        for key_b, cnt in sorted(brkd.items(), key=lambda x: -x[1]):
            reason, outcome = key_b.rsplit("_", 1)
            c = "green" if outcome == "gan" else "red"
            console.print(f"    {reason:12s} [{c}]{outcome}[/{c}]: {cnt}")

    # ── Tabla barrido IS completo (top 12 por net_pp) ────────────────────────
    console.print("\n  [dim]Barrido IS completo (top 12 por impacto neto sobre universo):[/dim]")
    btbl = Table(box=box.SIMPLE, border_style="dim")
    for col in ["Día", "Umbral", "N flag.", "N per.", "N gan.", "Ahorro", "Coste", "Neto (pp)"]:
        btbl.add_column(col, justify="right")

    all_is_results.sort(key=lambda x: x["net_pp"], reverse=True)
    for m in all_is_results[:12]:
        is_best_row = m["day_n"] == dn_best and m["threshold"] == thr_best
        s = "bold" if is_best_row else ""
        def _c(v): return f"[{s}]{v}[/{s}]" if s else str(v)
        btbl.add_row(
            _c(f"D+{m['day_n']}"), _c(f"{m['threshold']:.1f}%"),
            _c(str(m["n_flagged"])),
            _c(str(m["n_losers_flagged"])),
            _c(str(m["n_winners_flagged"])),
            _c(f"+{m['saved_pp']:.4f}"),
            _c(f"-{m['cost_pp']:.4f}"),
            _c(f"{m['net_pp']:+.4f}"),
        )
    console.print(btbl)


# ── Sección 4: indicadores de entrada como predictores ───────────────────────

def _section_indicadores(trades: list) -> None:
    """
    Valida si connors_rsi_entrada, rsi3_entrada o streak_entrada predicen el desenlace
    en IS y en OOS.

    Params:
        trades: lista de trades enriquecidos (todos, IS+OOS).
    """
    console.print("\n[bold cyan]4 · Indicadores de entrada como predictor[/bold cyan]")

    losers  = [t for t in trades if not t["winner"]]
    winners = [t for t in trades if     t["winner"]]

    indicadores = [
        ("connors_rsi_entrada", "CRSI entrada"),
        ("rsi3_entrada",        "RSI3 entrada"),
        ("streak_entrada",      "Streak entrada"),
    ]

    is_trades, oos_trades = _split(trades)
    is_los  = [t for t in is_trades  if not t["winner"]]
    oos_los = [t for t in oos_trades if not t["winner"]]

    tbl = Table(box=box.ROUNDED, border_style="cyan",
                title="[bold cyan]Indicadores de entrada — media ganadora vs perdedora[/bold cyan]")
    tbl.add_column("Indicador",      justify="left",  style="bold")
    tbl.add_column("Media ganadora", justify="right")
    tbl.add_column("Media perdedora",justify="right")
    tbl.add_column("Δ",              justify="right")
    tbl.add_column("IS AUC aprox.",  justify="right")
    tbl.add_column("OOS AUC aprox.", justify="right")
    tbl.add_column("Valoración",     justify="left")

    for field, label in indicadores:
        w_vals = [t[field] for t in winners if t.get(field) is not None]
        l_vals = [t[field] for t in losers  if t.get(field) is not None]
        if not w_vals or not l_vals:
            tbl.add_row(label, "—", "—", "—", "—", "—", "sin datos")
            continue

        w_avg = sum(w_vals) / len(w_vals)
        l_avg = sum(l_vals) / len(l_vals)
        delta = l_avg - w_avg

        # AUC aproximada: P(indicador de perdedora > indicador de ganadora) si mayor = peor
        # o P(indicador de perdedora < indicador de ganadora) si menor = peor
        # Simplificado: usamos threshold = media global, calculamos precision de clasificación
        all_vals = w_vals + l_vals
        mean_all = sum(all_vals) / len(all_vals)

        def _auc_approx(t_list_is, t_list_oos, fld, positive_if="lower"):
            """AUC aproximada por threshold óptimo en IS, evaluado en OOS."""
            # Barrido de thresholds con percentiles del IS
            is_vals_ = [t[fld] for t in t_list_is if t.get(fld) is not None]
            if not is_vals_:
                return 0.5, 0.5
            sorted_v = sorted(set(is_vals_))
            n_steps  = min(20, len(sorted_v))
            step     = max(1, len(sorted_v) // n_steps)
            candidates = sorted_v[::step]

            best_thr, best_acc = None, 0.0
            is_los_  = [t for t in t_list_is if not t["winner"] and t.get(fld) is not None]
            is_win_  = [t for t in t_list_is if     t["winner"] and t.get(fld) is not None]
            for thr_ in candidates:
                if positive_if == "lower":
                    tp = sum(1 for t in is_los_ if t[fld] <= thr_)
                    tn = sum(1 for t in is_win_ if t[fld] >  thr_)
                else:
                    tp = sum(1 for t in is_los_ if t[fld] >= thr_)
                    tn = sum(1 for t in is_win_ if t[fld] <  thr_)
                acc = (tp + tn) / (len(is_los_) + len(is_win_)) if (is_los_ or is_win_) else 0
                if acc > best_acc:
                    best_acc, best_thr = acc, thr_

            # Evaluar en OOS con el mismo threshold
            oos_los_ = [t for t in t_list_oos if not t["winner"] and t.get(fld) is not None]
            oos_win_ = [t for t in t_list_oos if     t["winner"] and t.get(fld) is not None]
            if best_thr is None or not oos_los_:
                return best_acc, 0.5
            if positive_if == "lower":
                tp_o = sum(1 for t in oos_los_ if t[fld] <= best_thr)
                tn_o = sum(1 for t in oos_win_ if t[fld] >  best_thr)
            else:
                tp_o = sum(1 for t in oos_los_ if t[fld] >= best_thr)
                tn_o = sum(1 for t in oos_win_ if t[fld] <  best_thr)
            acc_o = (tp_o + tn_o) / (len(oos_los_) + len(oos_win_)) if (oos_los_ or oos_win_) else 0
            return best_acc, acc_o

        # CRSI y RSI3 altos → se espera recuperación (entry criteria es CRSI < 25)
        # Pérdidas podrían asociarse a valores aún más altos (señal más débil)
        pos_if   = "lower"   # menor CRSI/RSI3 = más sobrevendido = debería recuperar mejor
        auc_is, auc_oos = _auc_approx(is_trades, oos_trades, field, pos_if)

        degr = auc_oos - auc_is
        baseline_acc = 0.5
        useful_is  = auc_is  > baseline_acc + 0.03
        useful_oos = auc_oos > baseline_acc + 0.03

        if useful_is and useful_oos:
            verdict = "[green]señal real[/green]"
        elif useful_is and not useful_oos:
            verdict = "[red]sobreajuste IS[/red]"
        else:
            verdict = "[dim]sin señal[/dim]"

        tbl.add_row(
            label,
            f"{w_avg:.2f}",
            f"{l_avg:.2f}",
            f"{delta:+.2f}",
            f"{auc_is*100:.1f}%",
            f"[{'green' if useful_oos else 'red'}]{auc_oos*100:.1f}%[/{'green' if useful_oos else 'red'}]",
            verdict,
        )

    console.print(tbl)
    console.print(
        "  [dim]AUC aprox.: accuracy del mejor threshold IS aplicado en OOS. "
        "Baseline = 50%. Señal real si ambos bloques > 53%.[/dim]"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    """Punto de entrada principal del análisis de patrones en operaciones perdedoras."""
    args = _parse_args()

    import config
    config.FILL_SIM            = FILL_SIM
    config.CONNORS_ENTRY_LIMIT = ENTRY_LIMIT

    console.print(
        f"\n[bold cyan]Análisis de patrones en operaciones perdedoras[/bold cyan]\n"
        f"  ENTRY_LIMIT={ENTRY_LIMIT} · FILL_SIM={FILL_SIM} · LOOKAHEAD_FIX={config.LOOKAHEAD_FIX}\n"
        f"  Período: {args.start} → {args.end}\n"
        f"  Split IS/OOS: {int(IS_RATIO*100)}% / {int((1-IS_RATIO)*100)}% (cronológico)\n"
    )

    from modules.connors_rsi import run_connors_backtest
    from modules import yf_cache

    console.print("[cyan]Corriendo backtest completo...[/cyan]")
    res = run_connors_backtest(
        start_date=args.start,
        end_date=args.end,
        skip_download=args.skip_download,
        export=False,
        verbose=False,
    )

    trades = res.get("trades", [])
    cap_f  = res.get("capital_final", config.CONNORS_CAPITAL)
    pnl_t  = (cap_f - config.CONNORS_CAPITAL) / config.CONNORS_CAPITAL * 100

    console.print(
        f"  Backtest completo: [green]{pnl_t:+.1f}%[/green]  ·  {len(trades)} trades cerradas"
    )

    if not trades:
        console.print("[red]Sin trades. Abortando.[/red]")
        return

    # Enriquecer con datos de precio diario
    console.print("[cyan]Reconstruyendo P&L diario por operación...[/cyan]")
    enriched = _enrich(trades, yf_cache.session_data)
    console.print(f"  {len(enriched)} trades enriquecidas\n")

    # Estadísticas generales
    is_tr, oos_tr = _split(enriched)
    cut_date = oos_tr[0]["entry_date"] if oos_tr else "—"
    console.print(
        f"  IS: {len(is_tr)} trades  (hasta {cut_date[:10] if isinstance(cut_date, str) else str(cut_date)[:10]})\n"
        f"  OOS: {len(oos_tr)} trades\n"
    )

    _section_descriptivo(enriched)
    _section_crsi(enriched)
    _section_regla_global(enriched)
    _section_indicadores(enriched)

    console.print("\n[bold green]Análisis completado.[/bold green]\n")


if __name__ == "__main__":
    main()
