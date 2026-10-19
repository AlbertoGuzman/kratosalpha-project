# -*- coding: utf-8 -*-
"""
Sweep de Stop-Loss para ConnorsRSI.
Ejecuta 5 backtests (SL = 0.05, 0.04, 0.03, 0.02, 0.015) reutilizando
la descarga de datos y genera informes de texto plano + resumen CSV.
"""
import sys
import os
from pathlib import Path
from datetime import date

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(r"c:\alberto\workspace\kratosalpha-project")
sys.path.insert(0, str(REPO_ROOT / "trading_system"))
os.environ.setdefault("TRADING_ENV", "dev")

import config
from modules import yf_cache, connors_rsi
from modules.data import load_universe
from modules.yf_cache import set_skip_close_wait

# Suprimir espera de yfinance (estamos en modo interactivo)
set_skip_close_wait(True)

START_DATE  = "2010-01-01"
END_DATE    = date.today().strftime("%Y-%m-%d")
SL_VALUES   = [0.05, 0.07, 0.1]
REPORT_DIR  = Path(r"c:\alberto\workspace\kratosalpha-project\trading_system\reports\sl_sweep")
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ── Descarga única de datos ───────────────────────────────────────────────────
print(f"Descargando datos {START_DATE} → {END_DATE}  (una sola vez)...")
yf_cache.clear_session()
tickers = load_universe()

from datetime import datetime, timedelta
extra_days = 220 + 40
ext_start  = (
    datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=int(extra_days * 1.6))
).strftime("%Y-%m-%d")

raw, spy = connors_rsi._download_universe(start=ext_start, end=END_DATE, tickers=tickers)
if not raw or spy is None:
    print("ERROR: descarga fallida.")
    sys.exit(1)
# Cargar en session para skip_download
for t, df in raw.items():
    yf_cache.session_data[t] = df
yf_cache.session_data["SPY"] = spy

print(f"Datos listos: {len(raw)} tickers  |  SPY: {spy.index[0].date()} → {spy.index[-1].date()}\n")

# ── Funciones de análisis ─────────────────────────────────────────────────────
import numpy as np

def compute_max_drawdown(trades: list, initial: float) -> float:
    """MDD aproximado por curva de capital en fechas de cierre."""
    if not trades:
        return 0.0
    srt = sorted(trades, key=lambda t: t["exit_date"])
    capital = initial
    peak    = initial
    mdd     = 0.0
    for t in srt:
        capital += t["net_pnl"]
        if capital > peak:
            peak = capital
        dd = (peak - capital) / peak * 100
        if dd > mdd:
            mdd = dd
    return mdd


def yearly_stats(trades: list):
    """Devuelve list de dicts con métricas por año."""
    by_year: dict = {}
    for t in trades:
        y = t["exit_date"].year
        by_year.setdefault(y, []).append(t)
    rows = []
    for y in sorted(by_year):
        ts    = by_year[y]
        n     = len(ts)
        pnl   = sum(t["net_pnl"] for t in ts)
        wins  = sum(1 for t in ts if t["net_pnl"] > 0)
        wr    = wins / n * 100 if n else 0.0
        avg_r = sum(t["return_pct"] for t in ts) / n * 100 if n else 0.0
        n_sl  = sum(1 for t in ts if t["reason"] == "SL")
        pct_sl= n_sl / n * 100 if n else 0.0
        rows.append({
            "year": y, "n_trades": n, "net_pnl": pnl,
            "win_rate_pct": wr, "avg_return_pct": avg_r,
            "n_sl": n_sl, "pct_sl": pct_sl,
        })
    return rows


def generate_report(sl: float, trades: list, capital_final: float) -> str:
    """Genera el texto del informe para un valor de SL."""
    initial = config.CONNORS_CAPITAL
    n       = len(trades)
    net_pnl = capital_final - initial
    wins    = sum(1 for t in trades if t["net_pnl"] > 0)
    wr      = wins / n * 100 if n else 0.0
    avg_r   = (sum(t["return_pct"] for t in trades) / n * 100) if n else 0.0
    mdd     = compute_max_drawdown(trades, initial)

    by_reason = {"CRSI_EXIT": 0, "TSTO": 0, "SL": 0, "END": 0}
    for t in trades:
        by_reason[t.get("reason", "END")] = by_reason.get(t.get("reason", "END"), 0) + 1
    n_sl    = by_reason["SL"]
    pct_sl  = n_sl / n * 100 if n else 0.0

    returns = [t["return_pct"] * 100 for t in trades]
    arr     = np.array(returns) if returns else np.array([0.0])
    p5, p25, p50, p75, p95 = (
        np.percentile(arr, 5),
        np.percentile(arr, 25),
        np.percentile(arr, 50),
        np.percentile(arr, 75),
        np.percentile(arr, 95),
    )
    n_lt_m3  = int(np.sum(arr < -3))
    n_lt_m2  = int(np.sum(arr < -2))
    n_lt_m1  = int(np.sum(arr < -1))
    n_gt_p2  = int(np.sum(arr >  2))
    n_gt_p5  = int(np.sum(arr >  5))

    yr = yearly_stats(trades)

    lines = []
    sl_str = f"{sl:.3f}".rstrip("0").rstrip(".")
    lines.append(f"STOP_LOSS={sl:.3f} | Periodo: 2010-{END_DATE[:4]} | Total operaciones: {n}")
    lines.append("")
    lines.append("=== METRICAS GLOBALES ===")
    lines.append(f"Total net PnL           : {net_pnl:+.2f} EUR  ({net_pnl/initial*100:+.2f}%)")
    lines.append(f"Win rate                : {wr:.2f}%")
    lines.append(f"Avg return per trade    : {avg_r:+.3f}%")
    lines.append(f"Max drawdown (aprox)    : {mdd:.2f}%")
    lines.append(f"N operaciones por reason: CRSI_EXIT={by_reason['CRSI_EXIT']}  TSTO={by_reason['TSTO']}  SL={by_reason['SL']}  END={by_reason['END']}")
    lines.append(f"% operaciones con SL    : {pct_sl:.2f}%")
    lines.append("")
    lines.append("=== METRICAS POR AÑO (CSV) ===")
    lines.append("año,n_trades,net_pnl,win_rate_pct,avg_return_pct,n_sl,pct_sl")
    for row in yr:
        lines.append(
            f"{row['year']},{row['n_trades']},{row['net_pnl']:.2f},"
            f"{row['win_rate_pct']:.2f},{row['avg_return_pct']:.3f},"
            f"{row['n_sl']},{row['pct_sl']:.2f}"
        )
    lines.append("")
    lines.append("=== DISTRIBUCION DE RETURNS (%) ===")
    lines.append(f"Percentiles  p5={p5:.2f}%  p25={p25:.2f}%  p50={p50:.2f}%  p75={p75:.2f}%  p95={p95:.2f}%")
    lines.append(f"Trades < -3% : {n_lt_m3}")
    lines.append(f"Trades < -2% : {n_lt_m2}")
    lines.append(f"Trades < -1% : {n_lt_m1}")
    lines.append(f"Trades > +2% : {n_gt_p2}")
    lines.append(f"Trades > +5% : {n_gt_p5}")
    lines.append("")
    lines.append(f"END_REPORT_SL={sl:.3f}")
    return "\n".join(lines)


# ── Ejecutar sweep ─────────────────────────────────────────────────────────────
SL_NAMES = {
    0.05:  "report_sl_005.txt",
    0.04:  "report_sl_004.txt",
    0.03:  "report_sl_003.txt",
    0.02:  "report_sl_002.txt",
    0.015: "report_sl_0015.txt",
}

summary_rows = []

for sl in SL_VALUES:
    print(f"\n{'='*60}")
    print(f"  Backtest SL={sl:.3f}")
    print(f"{'='*60}")

    # Sobreescribir temporalmente el parámetro
    config.CONNORS_STOP_LOSS = sl

    result = connors_rsi.run_connors_backtest(
        tickers=tickers,
        start_date=START_DATE,
        end_date=END_DATE,
        skip_download=True,
        export=False,
        verbose=False,
    )

    trades        = result["trades"]
    capital_final = result["capital_final"]

    report = generate_report(sl, trades, capital_final)
    fname  = SL_NAMES[sl]
    fpath  = REPORT_DIR / fname
    fpath.write_text(report, encoding="utf-8")
    print(f"  Informe guardado: {fpath}")
    print(f"  Trades: {len(trades)}  |  Capital final: {capital_final:.2f} EUR")

    # Para el resumen
    initial = config.CONNORS_CAPITAL
    n       = len(trades)
    net_pnl = capital_final - initial
    wins    = sum(1 for t in trades if t["net_pnl"] > 0)
    wr      = wins / n * 100 if n else 0.0
    avg_r   = (sum(t["return_pct"] for t in trades) / n * 100) if n else 0.0
    mdd     = compute_max_drawdown(trades, initial)
    n_sl    = sum(1 for t in trades if t["reason"] == "SL")
    pct_sl  = n_sl / n * 100 if n else 0.0
    summary_rows.append({
        "stop_loss": sl, "n_trades": n,
        "net_pnl": net_pnl, "net_pnl_pct": net_pnl / initial * 100,
        "win_rate": wr, "avg_return_pct": avg_r,
        "max_drawdown_pct": mdd, "n_sl": n_sl, "pct_sl": pct_sl,
    })

# ── Resumen comparativo ────────────────────────────────────────────────────────
print("\n\nGenerando resumen comparativo...")
summary_lines = [
    "stop_loss,n_trades,net_pnl_eur,net_pnl_pct,win_rate_pct,avg_return_pct,max_drawdown_pct,n_sl,pct_sl"
]
for r in summary_rows:
    summary_lines.append(
        f"{r['stop_loss']:.3f},{r['n_trades']},{r['net_pnl']:.2f},{r['net_pnl_pct']:.2f},"
        f"{r['win_rate']:.2f},{r['avg_return_pct']:.3f},{r['max_drawdown_pct']:.2f},"
        f"{r['n_sl']},{r['pct_sl']:.2f}"
    )

summary_path = REPORT_DIR / "summary_sl_sweep.txt"
summary_path.write_text("\n".join(summary_lines), encoding="utf-8")
print(f"Resumen guardado: {summary_path}")
print("\nSweep completado.")
