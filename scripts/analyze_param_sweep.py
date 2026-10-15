# -*- coding: utf-8 -*-
"""
Barrido de parámetros ConnorsRSI — análisis standalone.
FILL_SIM=True, LOOKAHEAD_FIX=True. Capital fijo por año (sin reinversión).

39 configuraciones: 34 Bloque A (un parámetro × ENTRY_LIMIT=[0.010,0.017])
y 5 Bloque B (RSI/STREAK/RANK_PERIOD con ENTRY_LIMIT=0.010).

Mejoras v2:
  - Doble selección IS: (A) IS completo y (B) IS excluyendo 1999-2000.
  - Bootstrap anual + bootstrap por bloques mensuales. IC del que excluye 0.
  - Correlación de rangos Spearman IS→OOS con IC por permutación (10 000).
  - Conteo de ganadores agrupando configs vecinas del mismo parámetro.
  - Veredicto: nunca "implementar"; solo "candidata con flag reversible" si
    los tres criterios se cumplen simultáneamente.

IS: años ≤ 2012  ·  OOS: años ≥ 2013.

Uso:
    C:\\trading_env\\Scripts\\python scripts\\analyze_param_sweep.py
    C:\\trading_env\\Scripts\\python scripts\\analyze_param_sweep.py --n-boot 2000
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
from rich.table   import Table
from rich.panel   import Panel
from rich.text    import Text
from rich         import box

console = Console()

IS_MAX_YEAR  = 2012
OOS_MIN_YEAR = 2013
IS_NO_EARLY_FROM = 2001          # para selección IS sin 1999-2000
MIN_YEARS_CI = 3                 # mínimo de años/meses para CI


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start",  default="1999-01-01")
    p.add_argument("--end",    default=date.today().strftime("%Y-%m-%d"))
    p.add_argument("--n-boot", type=int, default=1000,
                   help="Resamples bootstrap (default 1000)")
    return p.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
# Definición de configuraciones (39 en total)
# ══════════════════════════════════════════════════════════════════════════════

_CURRENT = {
    "CONNORS_ENTRY_CRSI":    25,
    "CONNORS_EXIT_CRSI":     60,
    "CONNORS_STOP_LOSS":     0.05,
    "CONNORS_TIME_STOP":     5,
    "CONNORS_ENTRY_LIMIT":   0.010,
    "CONNORS_RSI_PERIOD":    3,
    "CONNORS_STREAK_PERIOD": 2,
    "CONNORS_RANK_PERIOD":   100,
}

_SWEEP_A = {
    "CONNORS_ENTRY_CRSI":  [10, 15, 20, 25, 30],
    "CONNORS_EXIT_CRSI":   [50, 55, 60, 65, 70],
    "CONNORS_STOP_LOSS":   [0.03, 0.04, 0.05, 0.07, 0.10],
    "CONNORS_TIME_STOP":   [3, 4, 5, 7, 10],
}
_ENTRY_LIMITS = [0.010, 0.017]

_SWEEP_B = {
    "CONNORS_RSI_PERIOD":    [2, 3, 4],
    "CONNORS_STREAK_PERIOD": [2, 3],
    "CONNORS_RANK_PERIOD":   [50, 100, 200],
}

_PARAM_LABELS = {
    "CONNORS_ENTRY_CRSI":    "ENTRY_CRSI",
    "CONNORS_EXIT_CRSI":     "EXIT_CRSI",
    "CONNORS_STOP_LOSS":     "STOP_LOSS",
    "CONNORS_TIME_STOP":     "TIME_STOP",
    "CONNORS_RSI_PERIOD":    "RSI_PERIOD",
    "CONNORS_STREAK_PERIOD": "STREAK_PERIOD",
    "CONNORS_RANK_PERIOD":   "RANK_PERIOD",
    "CONNORS_ENTRY_LIMIT":   "ENTRY_LIMIT",
}


def _build_configs() -> list:
    """
    Genera la lista de 39 configuraciones (Bloque A: 34, Bloque B: 5).

    Returns:
        Lista de dicts {label, params, block, vary, vary_val}.
    """
    seen    = set()
    configs = []

    def _add(label, params, block, vary, vary_val=None):
        key = tuple(sorted(params.items()))
        if key in seen:
            return
        seen.add(key)
        configs.append({"label": label, "params": dict(params),
                        "block": block, "vary": vary, "vary_val": vary_val})

    for lim in _ENTRY_LIMITS:
        cfg = {**_CURRENT, "CONNORS_ENTRY_LIMIT": lim}
        _add(f"baseline EL={lim:.3f}", cfg, "A", "baseline")

    for param, values in _SWEEP_A.items():
        for v in values:
            if v == _CURRENT[param]:
                continue
            for lim in _ENTRY_LIMITS:
                cfg = {**_CURRENT, param: v, "CONNORS_ENTRY_LIMIT": lim}
                _add(f"{_PARAM_LABELS[param]}={v} EL={lim:.3f}", cfg, "A", param, v)

    for param, values in _SWEEP_B.items():
        for v in values:
            if v == _CURRENT[param]:
                continue
            cfg = {**_CURRENT, param: v, "CONNORS_ENTRY_LIMIT": 0.010}
            _add(f"B:{_PARAM_LABELS[param]}={v}", cfg, "B", param, v)

    return configs


# ══════════════════════════════════════════════════════════════════════════════
# Métricas (base fija por año)
# ══════════════════════════════════════════════════════════════════════════════

def _yearly_stats_fixed(trades: list, cap: float, max_pos: int) -> dict:
    """
    Rentabilidad anual con capital fijo (sin reinversión).
    Normaliza a fixed_slot = cap / max_pos.

    Params:
        trades:  lista de dicts del backtest.
        cap:     capital base fijo.
        max_pos: posiciones máximas simultáneas.
    Returns:
        {año: {pnl_eur, pnl_pct, n_trades, win_rate}}
    """
    fixed_slot = cap / max_pos
    by_year    = defaultdict(list)
    for t in trades:
        if not t.get("exit_date") or not t.get("valor_compra") or t["valor_compra"] == 0:
            continue
        yr       = pd.Timestamp(t["exit_date"]).year
        ret_frac = (t.get("net_pnl") or 0) / t["valor_compra"]
        by_year[yr].append({"pnl": ret_frac * fixed_slot,
                             "winner": (t.get("net_pnl") or 0) > 0})
    result = {}
    for yr, ts in by_year.items():
        pnl_eur = sum(x["pnl"] for x in ts)
        n       = len(ts)
        n_win   = sum(1 for x in ts if x["winner"])
        result[yr] = {"pnl_eur": pnl_eur, "pnl_pct": pnl_eur / cap * 100,
                      "n_trades": n, "win_rate": n_win / n * 100 if n else 0.0}
    return result


def _is_mean(yearly: dict, from_year: int = 1900) -> float | None:
    ys = [v["pnl_pct"] for yr, v in yearly.items()
          if IS_MAX_YEAR >= yr >= from_year]
    return statistics.mean(ys) if ys else None


def _is_median(yearly: dict, from_year: int = 1900) -> float | None:
    ys = [v["pnl_pct"] for yr, v in yearly.items()
          if IS_MAX_YEAR >= yr >= from_year]
    return statistics.median(ys) if ys else None


def _oos_mean(yearly: dict) -> float | None:
    ys = [v["pnl_pct"] for yr, v in yearly.items() if yr >= OOS_MIN_YEAR]
    return statistics.mean(ys) if ys else None


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap anual
# ══════════════════════════════════════════════════════════════════════════════

def _bootstrap_annual_diff(yearly_test: dict, yearly_ref: dict,
                            n_boot: int = 1000) -> dict | None:
    """
    Bootstrap resampleando años OOS del PnL% diferencial.

    Params:
        yearly_test: {año: {pnl_pct, ...}}.
        yearly_ref:  idem, config de referencia.
        n_boot:      resamples.
    Returns:
        {obs_diff, ci_low, ci_high, n_years} o None.
    """
    years = sorted(yr for yr in yearly_test
                   if yr >= OOS_MIN_YEAR and yr in yearly_ref)
    if len(years) < MIN_YEARS_CI:
        return None
    diffs = [yearly_test[yr]["pnl_pct"] - yearly_ref[yr]["pnl_pct"] for yr in years]
    obs   = statistics.mean(diffs)
    rng   = random.Random(42)
    boot  = sorted(statistics.mean(rng.choices(diffs, k=len(diffs)))
                   for _ in range(n_boot))
    return {"obs_diff": obs, "ci_low": boot[int(n_boot * 0.025)],
            "ci_high": boot[int(n_boot * 0.975)], "n_years": len(years), "diffs": diffs}


# ══════════════════════════════════════════════════════════════════════════════
# Bootstrap mensual (bloques de un mes)
# ══════════════════════════════════════════════════════════════════════════════

def _monthly_pnl_oos(trades: list, cap: float, max_pos: int) -> dict:
    """
    PnL% mensual en OOS para bootstrap de bloques.

    Params:
        trades:  lista de trades del backtest.
        cap:     capital base fijo.
        max_pos: posiciones máximas.
    Returns:
        {(año, mes): pnl_pct}
    """
    fixed_slot = cap / max_pos
    by_month   = defaultdict(float)
    for t in trades:
        if not t.get("exit_date") or not t.get("valor_compra") or t["valor_compra"] == 0:
            continue
        ts = pd.Timestamp(t["exit_date"])
        if ts.year < OOS_MIN_YEAR:
            continue
        ret_frac = (t.get("net_pnl") or 0) / t["valor_compra"]
        by_month[(ts.year, ts.month)] += ret_frac * fixed_slot / cap * 100
    return dict(by_month)


def _bootstrap_monthly_diff(trades_test: list, trades_ref: list,
                             cap: float, max_pos: int,
                             n_boot: int = 1000) -> dict | None:
    """
    Bootstrap por bloques mensuales del diferencial PnL% OOS.
    Los meses sin trades en una config contribuyen con 0.

    Params:
        trades_test: trades de la config a evaluar.
        trades_ref:  trades de la config de referencia.
        cap:         capital base fijo.
        max_pos:     posiciones máximas.
        n_boot:      resamples.
    Returns:
        {obs_diff, ci_low, ci_high, n_months} o None.
    """
    m_test = _monthly_pnl_oos(trades_test, cap, max_pos)
    m_ref  = _monthly_pnl_oos(trades_ref,  cap, max_pos)
    months = sorted(set(list(m_test.keys()) + list(m_ref.keys())))
    if len(months) < 6:
        return None
    diffs = [m_test.get(m, 0.0) - m_ref.get(m, 0.0) for m in months]
    obs   = statistics.mean(diffs)
    rng   = random.Random(43)
    boot  = sorted(statistics.mean(rng.choices(diffs, k=len(diffs)))
                   for _ in range(n_boot))
    return {"obs_diff": obs, "ci_low": boot[int(n_boot * 0.025)],
            "ci_high": boot[int(n_boot * 0.975)], "n_months": len(months)}


# ══════════════════════════════════════════════════════════════════════════════
# Correlación de rangos Spearman IS → OOS
# ══════════════════════════════════════════════════════════════════════════════

def _rank_list(vals: list) -> list:
    """Rangos 1-based con gestión de empates (media de rango)."""
    sorted_v = sorted(vals)
    ranks    = []
    for v in vals:
        idx = [i for i, x in enumerate(sorted_v) if x == v]
        ranks.append(statistics.mean(i + 1 for i in idx))
    return ranks


def _spearman_rho(x: list, y: list) -> float:
    """
    Correlación de rangos de Spearman entre dos listas.

    Params:
        x, y: listas de valores numéricos.
    Returns:
        rho ∈ [-1, 1].
    """
    n  = len(x)
    rx = _rank_list(x)
    ry = _rank_list(y)
    mx = statistics.mean(rx)
    my = statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / n
    sx  = (sum((a - mx) ** 2 for a in rx) / n) ** 0.5
    sy  = (sum((b - my) ** 2 for b in ry) / n) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def _spearman_analysis(result_list: list, n_perm: int = 10000) -> dict:
    """
    Correlación Spearman entre ranking IS y ranking OOS de todas las configs.
    P-valor por permutación (una cola, H1: rho > 0).
    IC 95% del rho por bootstrap.

    Params:
        result_list: lista de dicts con is_mean y oos_mean.
        n_perm:      permutaciones para el p-valor.
    Returns:
        {rho, p_value, ci_low, ci_high, n_configs}
    """
    valid = [(r["is_mean"], r["oos_mean"]) for r in result_list
             if r.get("is_mean") is not None and r.get("oos_mean") is not None]
    if len(valid) < 5:
        return {}
    is_vals  = [v[0] for v in valid]
    oos_vals = [v[1] for v in valid]
    rho_obs  = _spearman_rho(is_vals, oos_vals)

    rng = random.Random(44)
    # P-valor por permutación (una cola)
    perm_rhos = [
        _spearman_rho(is_vals, rng.sample(oos_vals, len(oos_vals)))
        for _ in range(n_perm)
    ]
    p_val = sum(1 for r in perm_rhos if r >= rho_obs) / n_perm

    # IC 95% por bootstrap
    indices  = list(range(len(valid)))
    boot_rho = sorted(
        _spearman_rho(
            [is_vals[i]  for i in rng.choices(indices, k=len(indices))],
            [oos_vals[i] for i in rng.choices(indices, k=len(indices))],
        )
        for _ in range(1000)
    )
    return {
        "rho":      rho_obs,
        "p_value":  p_val,
        "ci_low":   boot_rho[int(1000 * 0.025)],
        "ci_high":  boot_rho[int(1000 * 0.975)],
        "n_configs": len(valid),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Top-K IS en el ranking OOS
# ══════════════════════════════════════════════════════════════════════════════

def _top_k_is_oos_ranks(result_list: list, best_A: dict | None,
                         best_B: dict | None, k: int = 3) -> None:
    """
    Muestra el rango OOS de las top-k configuraciones IS (selección A y B).
    Esto resuelve el problema del Spearman global: verifica directamente si
    las mejores configs en IS son también buenas en OOS.

    Params:
        result_list: lista completa de resultados.
        best_A:      mejor config por IS completo.
        best_B:      mejor config por IS sin 1999-2000.
        k:           cuántas top-IS mostrar.
    """
    n_total = len(result_list)
    top_third = n_total // 3

    # Ranking OOS de toda la lista
    oos_ranked = sorted(
        (r for r in result_list if r.get("oos_mean") is not None),
        key=lambda r: r["oos_mean"], reverse=True,
    )
    oos_rank_map = {r["label"]: i + 1 for i, r in enumerate(oos_ranked)}

    for sel_label, sort_key, from_yr in [
        ("(A) IS completo",      "is_mean",    1900),
        ("(B) IS sin 1999-2000", "is_mean_ne", IS_NO_EARLY_FROM),
    ]:
        top_k = sorted(
            (r for r in result_list if r.get(sort_key) is not None),
            key=lambda r: r[sort_key], reverse=True,
        )[:k]
        lines = []
        for rank_is, r in enumerate(top_k, 1):
            oos_r = oos_rank_map.get(r["label"])
            in_top = oos_r is not None and oos_r <= top_third
            c = "green" if in_top else "yellow"
            lines.append(
                f"    IS #{rank_is} → OOS #{oos_r}/{n_total} "
                f"[{c}]({'top tercio' if in_top else 'fuera del top tercio'})[/{c}]"
                f"  {r['label']}"
            )
        console.print(f"  {sel_label}:")
        for l in lines: console.print(l)


# ══════════════════════════════════════════════════════════════════════════════
# Spearman por parámetro (dentro de cada sweep con EL=0.010)
# ══════════════════════════════════════════════════════════════════════════════

def _per_param_spearman(all_results: dict) -> None:
    """
    Para cada parámetro del Bloque A, calcula la correlación de Spearman
    entre el rendimiento IS y el rendimiento OOS de sus 5 configuraciones
    (EL=0.010 fijo). Responde a: ¿el ranking IS dentro del parámetro predice
    el ranking OOS? Con 5 puntos no hay p-valor fiable; solo dirección.
    Informa cuántos parámetros tienen rho positivo.

    Params:
        all_results: {label: {oos_mean, ...}}.
    """
    console.print("\n[bold cyan]Spearman por parámetro (dentro del sweep, EL=0.010)[/bold cyan]")
    console.print(
        "  [dim]5 puntos por parámetro → sin p-valor fiable. Solo dirección.[/dim]"
    )

    n_positive = 0
    rows = []
    for param, sweep in _SWEEP_A.items():
        param_lbl = _PARAM_LABELS.get(param, param)
        xs, ys = [], []
        for v in sorted(sweep):
            if v == _CURRENT[param]:
                lbl = "baseline EL=0.010"
            else:
                lbl = f"{param_lbl}={v} EL=0.010"
            r   = all_results.get(lbl)
            is_ = r.get("is_mean")  if r else None
            oos = r.get("oos_mean") if r else None
            if is_ is not None and oos is not None:
                xs.append(is_)    # IS medio de esa config
                ys.append(oos)    # OOS medio de esa config
        if len(xs) < 3:
            rows.append((param_lbl, None, "sin datos"))
            continue
        # Correlación IS→OOS dentro del sweep (¿el ranking IS predice el OOS?)
        rho = _spearman_rho(xs, ys)
        if rho > 0:
            n_positive += 1
        rows.append((param_lbl, rho, None))

    n_params = len([r for r in rows if r[1] is not None])
    for param_lbl, rho, note in rows:
        if note:
            console.print(f"  {param_lbl:15s}: [dim]{note}[/dim]")
            continue
        c   = "green" if rho > 0.3 else "yellow" if rho > 0 else "red"
        dir_str = "↑ positivo" if rho > 0 else "↓ negativo"
        console.print(f"  {param_lbl:15s}: rho={rho:+.2f}  [{c}]{dir_str}[/{c}]")

    console.print(
        f"\n  Parámetros con rho positivo: {n_positive}/{n_params}\n"
        f"  [dim]Un rho positivo significa que valores más altos del parámetro "
        f"se asocian con mejor OOS (en este sweep, manteniendo el resto fijo).[/dim]"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Ejecución de una configuración
# ══════════════════════════════════════════════════════════════════════════════

def _run_config(cfg: dict, start: str, end: str,
                skip_download: bool) -> dict | None:
    """
    Ejecuta run_connors_backtest con los parámetros de cfg.
    Devuelve trades brutos y stats anuales (base fija).

    Params:
        cfg:           dict de parámetros a aplicar temporalmente.
        start/end:     período del backtest.
        skip_download: True para reutilizar session_data.
    Returns:
        {trades, yearly, cap, max_pos, n_trades} o None.
    """
    import config
    from modules.connors_rsi import run_connors_backtest

    saved = {}
    for k, v in cfg.items():
        saved[k] = getattr(config, k, None)
        setattr(config, k, v)
    try:
        res    = run_connors_backtest(start_date=start, end_date=end,
                                      skip_download=skip_download,
                                      export=False, verbose=False)
        trades = res.get("trades", [])
        cap    = config.CONNORS_CAPITAL
        mp     = config.CONNORS_MAX_POS
        yearly = _yearly_stats_fixed(trades, cap, mp)
        return {"trades": trades, "yearly": yearly,
                "cap": cap, "max_pos": mp, "n_trades": len(trades)}
    except Exception as e:
        console.print(f"  [red]Error: {e}[/red]")
        return None
    finally:
        for k, v in saved.items():
            if v is None:
                try: delattr(config, k)
                except AttributeError: pass
            else:
                setattr(config, k, v)


# ══════════════════════════════════════════════════════════════════════════════
# Tablas
# ══════════════════════════════════════════════════════════════════════════════

def _ranking_table(results: list, title: str, sort_key: str = "is_mean",
                   highlight_label: str | None = None) -> None:
    """
    Tabla de ranking con IS (media + mediana) y OOS (media).

    Params:
        results:         [{label, is_mean, is_median, oos_mean, ...}].
        title:           Título.
        sort_key:        Clave de ordenación.
        highlight_label: Label de la config actual.
    """
    ranked = sorted(
        (r for r in results if r.get(sort_key) is not None),
        key=lambda r: r[sort_key], reverse=True,
    )
    tbl = Table(title=title, box=box.ROUNDED, border_style="cyan")
    tbl.add_column("#",           justify="right",  no_wrap=True)
    tbl.add_column("Config",      justify="left",   style="bold", no_wrap=True)
    tbl.add_column("IS μ%",       justify="right",  no_wrap=True)
    tbl.add_column("IS med%",     justify="right",  no_wrap=True)
    tbl.add_column("OOS μ%",      justify="right",  no_wrap=True)
    tbl.add_column("N trd IS",    justify="right",  no_wrap=True)
    tbl.add_column("Bl.",         justify="center", no_wrap=True)

    for i, r in enumerate(ranked, 1):
        is_m  = r.get("is_mean")
        is_d  = r.get("is_median")
        oos_m = r.get("oos_mean")
        hi    = r["label"] == highlight_label

        def _c(v): return "green" if v is not None and v >= 0 else "red"
        def _b(v): return f" bold" if hi else ""
        def _fmt(v, c=None):
            if v is None: return "—"
            col = (c or _c(v)) + _b(v)
            return f"[{col}]{v:+.2f}%[/{col}]"

        tbl.add_row(
            str(i), r["label"],
            _fmt(is_m), _fmt(is_d), _fmt(oos_m),
            str(r.get("n_trades_is", "—")), r.get("block", "—"),
        )
    console.print(tbl)


def _yearly_table(yearly_test: dict, yearly_ref: dict,
                  label_test: str, label_ref: str) -> None:
    """Tabla OOS año a año comparando dos configuraciones."""
    years = sorted(yr for yr in yearly_test if yr >= OOS_MIN_YEAR)
    if not years:
        return
    tbl = Table(
        title=f"[bold cyan]OOS año a año: {label_test[:22]} vs {label_ref[:22]}[/bold cyan]",
        box=box.ROUNDED, border_style="cyan",
    )
    tbl.add_column("Año",     justify="center", style="bold")
    tbl.add_column("Test",    justify="right")
    tbl.add_column("Ref",     justify="right")
    tbl.add_column("Δ (pp)",  justify="right")
    tbl.add_column("N trd",   justify="right")
    for yr in years:
        t_m = (yearly_test.get(yr) or {}).get("pnl_pct")
        r_m = (yearly_ref.get(yr)  or {}).get("pnl_pct")
        dlt = (t_m - r_m) if t_m is not None and r_m is not None else None
        n_t = (yearly_test.get(yr) or {}).get("n_trades", "—")
        def _fmt(v):
            if v is None: return "—"
            c = "green" if v >= 0 else "red"
            return f"[{c}]{v:+.2f}%[/{c}]"
        c_d = "green" if dlt is not None and dlt > 0 else "red"
        tbl.add_row(str(yr), _fmt(t_m), _fmt(r_m),
                    f"[{c_d}]{dlt:+.2f}[/{c_d}]" if dlt is not None else "—",
                    str(n_t))
    console.print(tbl)


def _ci_panel(ci_ann: dict | None, ci_mon: dict | None,
              label_test: str, label_ref: str) -> None:
    """
    Panel con IC anual + IC mensual y valoración de cuál excluye el cero.

    Params:
        ci_ann: resultado de _bootstrap_annual_diff.
        ci_mon: resultado de _bootstrap_monthly_diff.
        label_test/ref: etiquetas.
    """
    lines = []
    ann_excl_0 = ci_ann is not None and ci_ann["ci_low"] > 0
    mon_excl_0 = ci_mon is not None and ci_mon["ci_low"] > 0

    if ci_ann:
        c = "green" if ci_ann["obs_diff"] > 0 else "red"
        excl = " [green]← excluye 0[/green]" if ann_excl_0 else ""
        lines.append(
            f"  Bootstrap [bold]anual[/bold]   Δ [{c}]{ci_ann['obs_diff']:+.3f}pp[/{c}]  "
            f"IC 95%: [{ci_ann['ci_low']:+.3f}%, {ci_ann['ci_high']:+.3f}%]  "
            f"({ci_ann['n_years']} años){excl}"
        )
    else:
        lines.append("  Bootstrap anual:  N/A")

    if ci_mon:
        c = "green" if ci_mon["obs_diff"] > 0 else "red"
        excl = " [green]← excluye 0[/green]" if mon_excl_0 else ""
        lines.append(
            f"  Bootstrap [bold]mensual[/bold]  Δ [{c}]{ci_mon['obs_diff']:+.3f}pp[/{c}]  "
            f"IC 95%: [{ci_mon['ci_low']:+.3f}%, {ci_mon['ci_high']:+.3f}%]  "
            f"({ci_mon['n_months']} meses){excl}"
        )
    else:
        lines.append("  Bootstrap mensual: N/A")

    console.print(Panel(
        Text.from_markup("\n".join(lines)),
        title=f"[bold cyan]IC 95%: '{label_test[:30]}' vs '{label_ref[:30]}'[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))


# ══════════════════════════════════════════════════════════════════════════════
# Conteo de ganadores con agrupación de configs vecinas
# ══════════════════════════════════════════════════════════════════════════════

def _count_winner_groups(all_results: dict, ref_oos: float | None) -> dict:
    """
    Para cada parámetro del Bloque A y del B, agrupa los valores
    contiguos que superan al baseline en OOS. Cada grupo de valores
    contiguos = un hallazgo (las configs están correlacionadas dentro
    del mismo barrido de parámetro).

    Params:
        all_results: {label: {oos_mean, ...}}.
        ref_oos:     OOS media de la config actual (referencia).
    Returns:
        {param: [lista de grupos ganadores]}
    """
    if ref_oos is None:
        return {}

    groups_by_param = {}

    for param, sweep in {**_SWEEP_A, **_SWEEP_B}.items():
        winner_vals = []
        for v in sweep:
            if v == _CURRENT[param]:
                continue
            # Buscar el label que corresponde (EL=0.010 siempre existe)
            if param in _SWEEP_A:
                lbl = f"{_PARAM_LABELS[param]}={v} EL=0.010"
            else:
                lbl = f"B:{_PARAM_LABELS[param]}={v}"
            r = all_results.get(lbl)
            if r and r.get("oos_mean") is not None and r["oos_mean"] > ref_oos:
                winner_vals.append(v)

        if not winner_vals:
            groups_by_param[param] = []
            continue

        # Agrupar valores contiguos en el sweep
        sorted_sweep   = sorted(sweep)
        sorted_winners = sorted(winner_vals)
        groups = []
        cur_group = [sorted_winners[0]]
        for w in sorted_winners[1:]:
            prev_idx = sorted_sweep.index(cur_group[-1])
            cur_idx  = sorted_sweep.index(w)
            if cur_idx == prev_idx + 1:
                cur_group.append(w)
            else:
                groups.append(cur_group)
                cur_group = [w]
        groups.append(cur_group)
        groups_by_param[param] = groups

    return groups_by_param


def _winner_count_section(all_results: dict, ref_oos: float | None,
                           n_configs_other: int) -> None:
    """
    Muestra el conteo de ganadores agrupados y contrasta con el azar.

    Params:
        all_results:     {label: {oos_mean, ...}}.
        ref_oos:         OOS media de la config actual.
        n_configs_other: configs distintas de la actual (38).
    """
    console.print("\n[bold cyan]Conteo de ganadores (configs vecinas agrupadas)[/bold cyan]")
    groups = _count_winner_groups(all_results, ref_oos)
    n_groups = sum(len(g) for g in groups.values())
    n_simple = sum(
        1 for r in all_results.values()
        if r.get("oos_mean") is not None and ref_oos is not None
        and r["oos_mean"] > ref_oos
    )
    # Excluir la actual de n_simple
    n_simple_exc = max(0, n_simple - (1 if (ref_oos or 0) > (ref_oos or 0) else 0))

    for param, groups_p in groups.items():
        if not groups_p:
            continue
        lbl_param = _PARAM_LABELS.get(param, param)
        for grp in groups_p:
            console.print(f"  {lbl_param}: valores {grp} → un grupo ganador")

    console.print(
        f"\n  Configs individuales que superan a la actual en OOS: {n_simple_exc}/{n_configs_other}\n"
        f"  Grupos de parámetros contiguos ganadores: [bold]{n_groups}[/bold]\n"
        f"  [dim]Nota: los valores contiguos del mismo parámetro están correlacionados "
        f"(sus trades se superponen), por lo que contar grupos en lugar de configs "
        f"individuales evita inflar el número de hallazgos.[/dim]\n"
        f"  Esperado por azar (aprox. N_grupos = N_parámetros × p_azar):\n"
        f"  [dim]Con {len(groups)} parámetros, α=0.05 (una cola): ~{len(groups) * 0.05:.1f} grupos.[/dim]"
    )
    if n_groups == 0:
        console.print("  → [dim]0 grupos ganadores. Sin evidencia de mejora.[/dim]")
    elif n_groups <= len(groups) * 0.10:
        console.print(f"  → [{_gc(n_groups, len(groups))}]{n_groups} grupo(s) — compatible con ruido.[/]")
    else:
        console.print(f"  → [yellow]{n_groups} grupos — supera lo esperado por azar. Requiere validación adicional.[/yellow]")


def _gc(n, total): return "dim" if n <= total * 0.10 else "yellow"


# ══════════════════════════════════════════════════════════════════════════════
# Interacción ENTRY_LIMIT × parámetro
# ══════════════════════════════════════════════════════════════════════════════

def _interaction_table(all_results: dict) -> None:
    """
    Para cada parámetro del Bloque A y cada valor, compara la diferencia
    OOS (ENTRY_LIMIT=0.017 − 0.010). ¿Cambia de signo o magnitud?

    Params:
        all_results: {label: {yearly, oos_mean, ...}}.
    """
    console.print("\n[bold cyan]Interacción OOS: Δ(EL=0.017 − EL=0.010) por valor de parámetro[/bold cyan]")
    r010_base = all_results.get("baseline EL=0.010")
    r017_base = all_results.get("baseline EL=0.017")
    if r010_base is None or r017_base is None:
        console.print("[red]Sin baseline. Saltando sección.[/red]")
        return

    baseline_diff = (r017_base.get("oos_mean") or 0) - (r010_base.get("oos_mean") or 0)

    tbl = Table(box=box.ROUNDED, border_style="cyan",
                title="[bold cyan]Δ OOS (EL=0.017 − EL=0.010) vs baseline[/bold cyan]")
    tbl.add_column("Parámetro",  justify="left",  style="bold")
    tbl.add_column("Valor",      justify="right")
    tbl.add_column("OOS 0.010",  justify="right")
    tbl.add_column("OOS 0.017",  justify="right")
    tbl.add_column("Δ (pp)",     justify="right")
    tbl.add_column("Δ vs base",  justify="right")

    # Baseline
    b010 = r010_base.get("oos_mean")
    b017 = r017_base.get("oos_mean")
    tbl.add_row("[dim]baseline[/dim]", "[dim]—[/dim]",
                f"{b010:+.2f}%" if b010 is not None else "—",
                f"{b017:+.2f}%" if b017 is not None else "—",
                f"[{'green' if baseline_diff>0 else 'red'}]{baseline_diff:+.2f}pp[/]",
                "[dim]0.00 pp[/dim]")

    for param in _SWEEP_A:
        for v in _SWEEP_A[param]:
            if v == _CURRENT[param]:
                continue
            l010 = f"{_PARAM_LABELS[param]}={v} EL=0.010"
            l017 = f"{_PARAM_LABELS[param]}={v} EL=0.017"
            r010 = all_results.get(l010)
            r017 = all_results.get(l017)
            if not r010 or not r017:
                continue
            m010  = r010.get("oos_mean")
            m017  = r017.get("oos_mean")
            delta = (m017 or 0) - (m010 or 0)
            vs_b  = delta - baseline_diff
            c_d   = "green" if delta > 0 else "red"
            c_vb  = "green" if vs_b  > 0 else "red"
            tbl.add_row(_PARAM_LABELS[param], str(v),
                        f"{m010:+.2f}%" if m010 is not None else "—",
                        f"{m017:+.2f}%" if m017 is not None else "—",
                        f"[{c_d}]{delta:+.2f}pp[/{c_d}]",
                        f"[{c_vb}]{vs_b:+.2f}pp[/{c_vb}]")
    console.print(tbl)
    console.print(
        "  [dim]'Δ vs base' negativo → EL=0.017 pierde ventaja (o empeora) cuando ese "
        "parámetro se aleja del actual. Signo cambiante = interacción.[/dim]"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Monotonía OOS por parámetro
# ══════════════════════════════════════════════════════════════════════════════

def _monotonicity_by_param(all_results: dict) -> None:
    """OOS monótono al mover cada parámetro (EL=0.010)."""
    console.print("\n[bold cyan]Monotonía OOS por parámetro (EL=0.010)[/bold cyan]")
    for param, sweep in {**_SWEEP_A, **_SWEEP_B}.items():
        vals = []
        for v in sorted(sweep):
            if v == _CURRENT[param]:
                lbl = "baseline EL=0.010"
            elif param in _SWEEP_A:
                lbl = f"{_PARAM_LABELS[param]}={v} EL=0.010"
            else:
                lbl = f"B:{_PARAM_LABELS[param]}={v}"
            r   = all_results.get(lbl)
            oos = r.get("oos_mean") if r else None
            vals.append((v, oos))
        seq = [oos for _, oos in vals if oos is not None]
        if len(seq) < 3:
            continue
        up   = all(a <= b for a, b in zip(seq, seq[1:]))
        down = all(a >= b for a, b in zip(seq, seq[1:]))
        mono = ("[green]↑ creciente[/green]" if (up and not down) else
                "[green]↓ decreciente[/green]" if (down and not up) else
                "[red]no monótono[/red]")
        vals_str = "  ".join(f"{v}→{(o or 0):+.2f}%" for v, o in vals if o is not None)
        console.print(f"  {_PARAM_LABELS.get(param, param):15s}: {vals_str}  → {mono}")


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """Punto de entrada del barrido de parámetros ConnorsRSI."""
    args    = _parse_args()
    configs = _build_configs()

    import config
    from modules.connors_rsi import _download_universe
    from modules import yf_cache

    n_total      = len(configs)
    current_label = "baseline EL=0.010"

    console.print(
        f"\n[bold cyan]Barrido de parámetros ConnorsRSI (v2)[/bold cyan]\n"
        f"  Configuraciones: [bold]{n_total}[/bold]  (34 Bloque A + 5 Bloque B)\n"
        f"  Victorias esperadas por azar al 95%: [yellow]~{n_total*0.05:.1f}[/yellow]\n"
        f"  IS ≤{IS_MAX_YEAR}  ·  OOS ≥{OOS_MIN_YEAR}  ·  "
        f"FILL_SIM=True · LOOKAHEAD_FIX=True\n"
        f"  Bootstrap: {args.n_boot} resamples (anual + mensual)  ·  "
        f"Spearman p-valor: 10 000 permutaciones.\n"
    )

    # Activar flags
    config.FILL_SIM = True
    if not getattr(config, "LOOKAHEAD_FIX", True):
        config.LOOKAHEAD_FIX = True

    # Descarga única
    yf_cache.clear_session()
    console.print("[cyan]Descargando datos...[/cyan]")
    ext_start = (datetime.strptime(args.start, "%Y-%m-%d") - timedelta(days=420)).strftime("%Y-%m-%d")
    raw, spy  = _download_universe(start=ext_start, end=args.end)
    if not raw or spy is None:
        console.print("[red]Sin datos. Abortando.[/red]")
        return

    # ── Ejecución de las 39 configuraciones ──────────────────────────────────
    all_results = {}
    for i, cfg_entry in enumerate(configs):
        lbl    = cfg_entry["label"]
        params = cfg_entry["params"]
        console.print(f"  [{i+1:2d}/{n_total}] {lbl}...", end=" ")
        result = _run_config(params, args.start, args.end, skip_download=(i > 0))
        if result:
            yearly   = result["yearly"]
            is_m     = _is_mean(yearly)
            is_d     = _is_median(yearly)
            is_m_ne  = _is_mean(yearly, from_year=IS_NO_EARLY_FROM)
            oos_m    = _oos_mean(yearly)
            n_is     = sum(v["n_trades"] for yr, v in yearly.items()
                           if yr <= IS_MAX_YEAR)
            c  = "green" if oos_m is not None and oos_m >= 0 else "red"
            console.print(
                f"IS {is_m:+.2f}%  OOS [{c}]{oos_m:+.2f}%[/{c}]  "
                f"{result['n_trades']} trades"
            ) if is_m is not None and oos_m is not None else console.print("—")
            all_results[lbl] = {
                **result,
                "label":      lbl,
                "block":      cfg_entry["block"],
                "is_mean":    is_m,
                "is_median":  is_d,
                "is_mean_ne": is_m_ne,   # sin 1999-2000
                "oos_mean":   oos_m,
                "n_trades_is": n_is,
            }
        else:
            console.print("[red]error[/red]")

    if not all_results:
        console.print("[red]Sin resultados. Abortando.[/red]")
        return

    result_list  = list(all_results.values())
    ref_res      = all_results.get(current_label)
    cap          = (ref_res or {}).get("cap", config.CONNORS_CAPITAL)
    max_pos      = (ref_res or {}).get("max_pos", config.CONNORS_MAX_POS)
    ref_oos      = ref_res.get("oos_mean") if ref_res else None

    # ── Rankings IS y OOS ─────────────────────────────────────────────────────
    console.print()
    _ranking_table(result_list,
                   title=f"[bold cyan]Ranking IS (≤{IS_MAX_YEAR}, capital fijo)[/bold cyan]",
                   sort_key="is_mean", highlight_label=current_label)

    ranked_by_is = sorted(result_list, key=lambda r: r.get("is_mean") or -999, reverse=True)
    console.print()
    _ranking_table(ranked_by_is,
                   title=f"[bold cyan]Ranking OOS (≥{OOS_MIN_YEAR}, orden = IS)[/bold cyan]",
                   sort_key="oos_mean", highlight_label=current_label)

    if ref_res:
        is_rank  = sorted(result_list, key=lambda r: r.get("is_mean")  or -999, reverse=True)
        oos_rank = sorted(result_list, key=lambda r: r.get("oos_mean") or -999, reverse=True)
        is_pos   = next((i+1 for i, r in enumerate(is_rank)  if r["label"] == current_label), "—")
        oos_pos  = next((i+1 for i, r in enumerate(oos_rank) if r["label"] == current_label), "—")
        console.print(f"\n  Config actual ({current_label}): "
                      f"IS #{is_pos}/{len(result_list)}  ·  OOS #{oos_pos}/{len(result_list)}")

    # ── Correlación Spearman IS → OOS ─────────────────────────────────────────
    console.print("\n[bold cyan]Correlación de rangos Spearman IS → OOS[/bold cyan]")
    console.print(
        "  [dim]Nota: el Spearman global puede ser positivo trivialmente porque las configs "
        "claramente malas (CRSI=10, SL=3%) lo son en IS y en OOS. "
        "Se complementa con el rank OOS de las mejores IS y el Spearman por parámetro.[/dim]"
    )
    sp = _spearman_analysis(result_list, n_perm=10000)
    if sp:
        rho  = sp["rho"]
        p    = sp["p_value"]
        c_r  = "green" if rho > 0.3 else "yellow" if rho > 0 else "red"
        sig  = "[green]sí, p<0.05[/green]" if p < 0.05 else "[red]no significativo[/red]"
        console.print(
            f"  ρ global = [{c_r}]{rho:+.3f}[/{c_r}]  "
            f"IC 95%: [{sp['ci_low']:+.3f}, {sp['ci_high']:+.3f}]  "
            f"p-valor permutación (una cola) = {p:.3f}  → {sig}"
        )
    else:
        sp = {}
        console.print("  [dim]Insuficientes datos para Spearman global.[/dim]")

    # ── Rank OOS de las top-3 IS (selecciones A y B) ──────────────────────────
    console.print("\n[bold cyan]Rank OOS de las top-3 configuraciones IS[/bold cyan]")
    console.print(
        "  [dim]Pregunta clave: ¿las mejores de IS están también en el tercio superior de OOS?[/dim]"
    )

    def _best_by(key):
        return max((r for r in result_list if r.get(key) is not None),
                   key=lambda r: r[key], default=None)

    best_A = _best_by("is_mean")
    best_B = _best_by("is_mean_ne")

    _top_k_is_oos_ranks(result_list, best_A, best_B, k=3)

    # ── Spearman por parámetro ────────────────────────────────────────────────
    _per_param_spearman(all_results)

    # ── Doble selección IS: (A) completo vs (B) sin 1999-2000 ────────────────
    console.print("\n[bold cyan]Doble selección IS y evaluación en OOS[/bold cyan]")

    for sel_label, best_r, desc in [
        ("(A) IS completo",            best_A, "mejor por IS mean 1999-2012"),
        ("(B) IS sin 1999-2000",       best_B, "mejor por IS mean 2001-2012"),
    ]:
        if best_r is None or ref_res is None:
            continue
        lbl = best_r["label"]
        console.print(f"\n  Selección {sel_label}: [bold]'{lbl}'[/bold]  ({desc})")

        ci_ann = _bootstrap_annual_diff(best_r["yearly"], ref_res["yearly"], args.n_boot)
        ci_mon = _bootstrap_monthly_diff(
            best_r["trades"], ref_res["trades"], cap, max_pos, args.n_boot
        )
        _ci_panel(ci_ann, ci_mon, lbl, current_label)
        _yearly_table(best_r["yearly"], ref_res["yearly"], lbl[:22], current_label[:22])

        if lbl == best_A.get("label") if best_A else None:
            same = best_B is not None and best_A["label"] == best_B["label"]
            if same:
                console.print("  → [green]Coincide con selección (B). Mayor robustez.[/green]")
            else:
                console.print(
                    f"  → [yellow]Selección (A) y (B) difieren → la ventaja "
                    f"puede depender de 1999-2000.[/yellow]"
                )

    # ── Monotonía ─────────────────────────────────────────────────────────────
    _monotonicity_by_param(all_results)

    # ── Interacción ──────────────────────────────────────────────────────────
    _interaction_table(all_results)

    # ── Conteo de ganadores agrupados ─────────────────────────────────────────
    _winner_count_section(all_results, ref_oos, n_configs_other=len(result_list) - 1)

    # ── Veredicto final ───────────────────────────────────────────────────────
    console.print("\n" + "─" * 70)
    # Condiciones para "candidata":
    # 1. IC mensual excluye 0
    # 2. Selección A y B coinciden (robusto a 1999-2000)
    # 3. Mejor IS está en el tercio superior de OOS (más preciso que Spearman global)

    if best_A is None or ref_res is None:
        console.print("[dim]Sin datos suficientes para veredicto.[/dim]")
        return

    best_lbl   = best_A["label"]
    best_B_lbl = best_B["label"] if best_B else None

    a_is_current = (best_lbl == current_label)
    b_is_current = (best_B_lbl == current_label) if best_B_lbl is not None else True

    # ── Caso 1: ambas selecciones apuntan a la config actual ────────────────
    if a_is_current and b_is_current:
        console.print(Panel(
            Text.from_markup(
                "  La configuración actual es la mejor de IS tanto en la selección A\n"
                "  (IS completo 1999-2012) como en la B (IS sin 1999-2000).\n"
                "  No existe ninguna alternativa que supere a la actual en ninguna de las dos.\n"
                "  [bold]Mantener configuración actual.[/bold]"
            ),
            title="[bold cyan]Veredicto[/bold cyan]",
            border_style="cyan", padding=(0, 2),
        ))
        console.print("\n[bold green]Barrido completado.[/bold green]\n")
        return

    # ── Determinar candidata e indicador de robustez ─────────────────────────
    # Condición de robustez: A y B coinciden en el mismo ganador, distinto de la actual
    a_eq_b    = (best_lbl == best_B_lbl) if best_B_lbl is not None else False
    is_robust = (not a_is_current and not b_is_current and a_eq_b)

    if a_is_current and not b_is_current:
        # Caso 2: A = actual, B ≠ actual → el ranking cambia al excluir 1999-2000
        console.print(
            f"\n  [dim]La selección A (IS completo) coincide con la actual; "
            f"la selección B señala '{best_B_lbl}'. "
            f"El ranking no es robusto a 1999-2000.[/dim]"
        )
        candidate = best_B   # evaluar B a título informativo
    elif not a_is_current and b_is_current:
        # Caso 3: A ≠ actual, B = actual → la ganadora IS depende de 1999-2000
        console.print(
            f"\n  [dim]La mejor de IS completo ('{best_lbl}') depende de 1999-2000: "
            f"al excluir esos años la actual vuelve a ser la mejor (selección B). "
            f"Se evalúa '{best_lbl}' solo a título informativo.[/dim]"
        )
        candidate = best_A   # evaluar A a título informativo
    elif not a_is_current and not b_is_current and not a_eq_b:
        # Caso 4: A ≠ B, ninguna es la actual → ranking inestable entre selecciones
        console.print(
            f"\n  [dim]Selección A señala '{best_lbl}', selección B señala '{best_B_lbl}': "
            f"el ranking cambia al excluir 1999-2000. No robusto.[/dim]"
        )
        candidate = best_A   # evaluar A a título informativo
    else:
        # Caso 5: A == B ≠ actual → ganadora estable → candidata potencial
        candidate = best_A

    ci_mon_final = _bootstrap_monthly_diff(
        candidate["trades"], ref_res["trades"], cap, max_pos, args.n_boot
    )
    cond_ci_mon  = ci_mon_final is not None and ci_mon_final["ci_low"] > 0
    cond_robust  = is_robust

    # Tercio superior OOS
    n_configs          = len(result_list)
    top_third          = n_configs // 3
    oos_sorted         = sorted(result_list, key=lambda r: r.get("oos_mean") or -999, reverse=True)
    candidate_oos_rank = next(
        (i + 1 for i, r in enumerate(oos_sorted) if r["label"] == candidate["label"]), n_configs
    )
    cond_top_third = candidate_oos_rank <= top_third

    conditions = [
        (cond_ci_mon,    "IC mensual excluye 0"),
        (cond_robust,    "Selecciones A y B coinciden (robusto a 1999-2000)"),
        (cond_top_third, f"Candidata en tercio superior OOS "
                         f"(rank #{candidate_oos_rank}/{n_configs}, "
                         f"umbral ≤{top_third})"),
    ]
    n_ok = sum(1 for c, _ in conditions if c)

    lines_v = []
    for ok, txt in conditions:
        sym = "[green]✓[/green]" if ok else "[red]✗[/red]"
        lines_v.append(f"  {sym} {txt}")

    if n_ok == 3:
        delta_eur = (ci_mon_final["obs_diff"] / 100) * cap
        lines_v.append(
            f"\n  → '[bold]{candidate['label']}[/bold]' es [yellow bold]candidata para probar "
            f"en cartera con flag reversible[/yellow bold].\n"
            f"    Mejora media mensual obs.: {ci_mon_final['obs_diff']:+.3f} pp  "
            f"≈ {delta_eur:+,.0f} €/mes base fija {cap:,.0f} €.\n"
            f"    [dim]Antes de activar: validar con datos posteriores al OOS "
            f"y no eliminar el flag de reversión.[/dim]"
        )
    elif not is_robust:
        lines_v.append(
            f"\n  → [red bold]No candidata: el ranking IS depende de 1999-2000.[/red bold]\n"
            f"    [dim]Resultado de '{candidate['label']}' mostrado a título informativo. "
            f"Mantener configuración actual.[/dim]"
        )
    else:
        lines_v.append(
            f"\n  → Ninguna configuración cumple los tres criterios de forma simultánea.\n"
            f"    [dim]Las diferencias observadas en OOS son compatibles con "
            f"ruido de selección múltiple. Mantener configuración actual.[/dim]"
        )

    console.print(Panel(
        Text.from_markup("\n".join(lines_v)),
        title="[bold cyan]Veredicto[/bold cyan]",
        border_style="cyan", padding=(0, 2),
    ))

    console.print("\n[bold green]Barrido completado.[/bold green]\n")


if __name__ == "__main__":
    main()
