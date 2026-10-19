# -*- coding: utf-8 -*-
"""
sl_analysis_sweep.py — Barrido SL × SL_SLIP con slot FIJO, IS/OOS y diagnóstico 2026.

Combinaciones: SL in [0.05, 0.07, 0.10, None] × SL_SLIP in [0.0, 0.005, 0.010]
Periodo total: 2010-01-01 → hoy
IS  (in-sample)      : 2010-01-01 → 2019-12-31
OOS (out-of-sample)  : 2020-01-01 → hoy

Uso:
    C:\trading_env\Scripts\python scripts\sl_analysis_sweep.py
"""
import sys
import os
from pathlib import Path
from datetime import date, datetime, timedelta

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "trading_system"))
os.environ.setdefault("TRADING_ENV", "dev")

import numpy as np
import config
from modules import yf_cache, connors_rsi
from modules.data import load_universe
from modules.yf_cache import set_skip_close_wait

set_skip_close_wait(True)

# ── Parámetros del barrido ─────────────────────────────────────────────────────
START_DATE = "2010-01-01"
END_DATE   = date.today().strftime("%Y-%m-%d")
IS_END     = "2019-12-31"   # in-sample hasta fin 2019
OOS_START  = "2020-01-01"   # out-of-sample desde 2020

SL_VALUES  = [0.05, 0.07, 0.10, None]   # None = sin stop
SLIP_VALUES = [0.0, 0.005, 0.010]

OUT_DIR = REPO_ROOT / "trading_system" / "reports" / "sl_sweep2"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Descarga única ────────────────────────────────────────────────────────────
print(f"Descargando universo {START_DATE} → {END_DATE}  (única vez)...")
yf_cache.clear_session()
tickers = load_universe()
extra_days = 220 + 40
ext_start  = (
    datetime.strptime(START_DATE, "%Y-%m-%d") - timedelta(days=int(extra_days * 1.6))
).strftime("%Y-%m-%d")
raw, spy = connors_rsi._download_universe(start=ext_start, end=END_DATE, tickers=tickers)
if not raw or spy is None:
    print("ERROR: descarga fallida."); sys.exit(1)
for t, df in raw.items():
    yf_cache.session_data[t] = df
yf_cache.session_data["SPY"] = spy
print(f"  {len(raw)} tickers · SPY {spy.index[0].date()} → {spy.index[-1].date()}\n")


# ── Helpers de métricas ───────────────────────────────────────────────────────
def _stats(trades: list, initial: float) -> dict:
    """Métricas globales sobre una lista de trades."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "net_pnl": 0, "ret_pct": 0, "wr": 0,
                "avg_ret": 0, "mdd": 0, "n_sl": 0, "pct_sl": 0}
    net     = sum(t["net_pnl"] for t in trades)
    wins    = sum(1 for t in trades if t["net_pnl"] > 0)
    n_sl    = sum(1 for t in trades if t["reason"] == "SL")
    returns = np.array([t["return_pct"] * 100 for t in trades])
    # MDD por curva de capital (fechas de salida)
    srt = sorted(trades, key=lambda t: t["exit_date"])
    cap, peak, mdd = initial, initial, 0.0
    for t in srt:
        cap += t["net_pnl"]
        if cap > peak: peak = cap
        dd = (peak - cap) / peak * 100
        if dd > mdd: mdd = dd
    return {
        "n": n, "net_pnl": net, "ret_pct": net / initial * 100,
        "wr": wins / n * 100, "avg_ret": float(returns.mean()),
        "mdd": mdd, "n_sl": n_sl, "pct_sl": n_sl / n * 100,
    }


def _yearly(trades: list, initial: float) -> list:
    by_year: dict = {}
    for t in trades:
        by_year.setdefault(t["exit_date"].year, []).append(t)
    rows = []
    for y in sorted(by_year):
        ts   = by_year[y]
        s    = _stats(ts, initial)
        n_crsi = sum(1 for t in ts if t["reason"] == "CRSI_EXIT")
        n_tsto = sum(1 for t in ts if t["reason"] == "TSTO")
        n_end  = sum(1 for t in ts if t["reason"] == "END")
        rows.append({"year": y, **s,
                     "n_crsi": n_crsi, "n_tsto": n_tsto, "n_end": n_end})
    return rows


def _label(sl, slip):
    sl_s = f"{int(sl*100)}pct" if sl else "nostop"
    slip_s = f"slip{int(slip*1000)}bp" if slip else "slip0"
    return f"SL={sl_s} {slip_s}"


# ── Bucle principal ───────────────────────────────────────────────────────────
initial = config.CONNORS_CAPITAL
summary_rows = []

for sl in SL_VALUES:
    for slip in SLIP_VALUES:
        lbl = _label(sl, slip)
        print(f"{'─'*55}\n  {lbl}")

        result = connors_rsi.run_connors_backtest(
            tickers=tickers, start_date=START_DATE, end_date=END_DATE,
            skip_download=True, export=False, verbose=False,
            fixed_slot=True, sl_slip=slip, stop_loss_pct=sl,
        )
        trades = result["trades"]
        is_trades  = [t for t in trades if str(t["exit_date"])[:10] <= IS_END]
        oos_trades = [t for t in trades if str(t["exit_date"])[:10] >= OOS_START]

        g_all = _stats(trades,     initial)
        g_is  = _stats(is_trades,  initial)
        g_oos = _stats(oos_trades, initial)
        yr    = _yearly(trades, initial)

        print(f"  Total: {g_all['n']:4d} ops · net={g_all['net_pnl']:+8.0f}€"
              f" ({g_all['ret_pct']:+.1f}%) · WR={g_all['wr']:.1f}%"
              f" · SL={g_all['pct_sl']:.1f}% · MDD={g_all['mdd']:.1f}%")
        print(f"  IS  : {g_is['n']:4d} ops · net={g_is['net_pnl']:+8.0f}€"
              f" ({g_is['ret_pct']:+.1f}%)")
        print(f"  OOS : {g_oos['n']:4d} ops · net={g_oos['net_pnl']:+8.0f}€"
              f" ({g_oos['ret_pct']:+.1f}%)")

        # ── Informe de texto ──────────────────────────────────────────────────
        sl_tag   = f"{sl:.2f}" if sl else "None"
        slip_tag = f"{slip:.3f}"
        lines = [
            f"SL={sl_tag} | SL_SLIP={slip_tag} | SLOT=FIJO | {START_DATE}→{END_DATE}",
            f"IS={START_DATE}→{IS_END}  OOS={OOS_START}→{END_DATE}",
            "",
            "=== GLOBAL ===",
            f"Ops: {g_all['n']}  WR: {g_all['wr']:.2f}%  avg_ret: {g_all['avg_ret']:+.3f}%"
            f"  MDD: {g_all['mdd']:.2f}%",
            f"Net PnL: {g_all['net_pnl']:+.2f}€  ({g_all['ret_pct']:+.2f}%)",
            f"SL exits: {g_all['n_sl']} ({g_all['pct_sl']:.2f}%)",
            "",
            "=== IS (2010-2019) ===",
            f"Ops: {g_is['n']}  WR: {g_is['wr']:.2f}%  avg_ret: {g_is['avg_ret']:+.3f}%"
            f"  MDD: {g_is['mdd']:.2f}%",
            f"Net PnL: {g_is['net_pnl']:+.2f}€  ({g_is['ret_pct']:+.2f}%)",
            "",
            "=== OOS (2020-2026) ===",
            f"Ops: {g_oos['n']}  WR: {g_oos['wr']:.2f}%  avg_ret: {g_oos['avg_ret']:+.3f}%"
            f"  MDD: {g_oos['mdd']:.2f}%",
            f"Net PnL: {g_oos['net_pnl']:+.2f}€  ({g_oos['ret_pct']:+.2f}%)",
            "",
            "=== POR AÑO (CSV) ===",
            "año,n,net_pnl,ret_pct,wr,avg_ret,mdd,n_sl,pct_sl,n_crsi,n_tsto,n_end",
        ]
        for r in yr:
            lines.append(
                f"{r['year']},{r['n']},{r['net_pnl']:.2f},{r['ret_pct']:.3f},"
                f"{r['wr']:.2f},{r['avg_ret']:.4f},{r['mdd']:.2f},"
                f"{r['n_sl']},{r['pct_sl']:.2f},{r['n_crsi']},{r['n_tsto']},{r['n_end']}"
            )
        lines += ["", f"END_REPORT SL={sl_tag} SLIP={slip_tag}"]

        fname = f"sl{sl_tag.replace('.','')}_slip{slip_tag.replace('.','')}.txt"
        (OUT_DIR / fname).write_text("\n".join(lines), encoding="utf-8")

        summary_rows.append({
            "sl": sl_tag, "slip": slip_tag,
            "n_total": g_all["n"], "net_pnl": g_all["net_pnl"],
            "ret_pct": g_all["ret_pct"], "wr": g_all["wr"],
            "avg_ret": g_all["avg_ret"], "mdd": g_all["mdd"],
            "pct_sl": g_all["pct_sl"],
            "n_is": g_is["n"],  "net_is": g_is["net_pnl"],  "ret_is": g_is["ret_pct"],
            "n_oos": g_oos["n"], "net_oos": g_oos["net_pnl"], "ret_oos": g_oos["ret_pct"],
        })

# ── Resumen comparativo ────────────────────────────────────────────────────────
hdr = ("sl,slip,n_total,net_pnl,ret_pct,wr_pct,avg_ret_pct,mdd_pct,pct_sl,"
       "n_is,net_is,ret_is,n_oos,net_oos,ret_oos")
rows_csv = [hdr]
for r in summary_rows:
    rows_csv.append(
        f"{r['sl']},{r['slip']},{r['n_total']},{r['net_pnl']:.2f},{r['ret_pct']:.2f},"
        f"{r['wr']:.2f},{r['avg_ret']:.4f},{r['mdd']:.2f},{r['pct_sl']:.2f},"
        f"{r['n_is']},{r['net_is']:.2f},{r['ret_is']:.2f},"
        f"{r['n_oos']},{r['net_oos']:.2f},{r['ret_oos']:.2f}"
    )
summary_path = OUT_DIR / "summary_sl_slip_sweep.csv"
summary_path.write_text("\n".join(rows_csv), encoding="utf-8")
print(f"\n✓ Resumen: {summary_path}")


# ── Diagnóstico 2026 (punto 4) ────────────────────────────────────────────────
print("\n" + "="*60)
print("  DIAGNÓSTICO 2026 (SL=5%, slip=0, slot fijo)")
print("="*60)

result_ref = connors_rsi.run_connors_backtest(
    tickers=tickers, start_date="2026-01-01", end_date=END_DATE,
    skip_download=True, export=False, verbose=False,
    fixed_slot=True, sl_slip=0.0, stop_loss_pct=0.05,
)
t26 = result_ref["trades"]
result_prev = connors_rsi.run_connors_backtest(
    tickers=tickers, start_date="2023-01-01", end_date="2025-12-31",
    skip_download=True, export=False, verbose=False,
    fixed_slot=True, sl_slip=0.0, stop_loss_pct=0.05,
)
t_prev = result_prev["trades"]

def _diag(trades, label):
    if not trades:
        print(f"  {label}: sin datos"); return
    s = _stats(trades, initial)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    returns = np.array([t["return_pct"]*100 for t in trades])
    dias    = np.array([t["dias"] for t in trades])
    print(f"\n  {label}")
    print(f"    Ops={s['n']}  WR={s['wr']:.1f}%  avg_ret={s['avg_ret']:+.3f}%  MDD={s['mdd']:.1f}%")
    print(f"    Net PnL={s['net_pnl']:+.0f}€ ({s['ret_pct']:+.1f}%)")
    print(f"    Reasons: {reasons}")
    print(f"    Returns p5={np.percentile(returns,5):.2f}%  p25={np.percentile(returns,25):.2f}%"
          f"  p50={np.percentile(returns,50):.2f}%  p95={np.percentile(returns,95):.2f}%")
    print(f"    Días medio={dias.mean():.1f}  n_TSTO(ratio)={reasons.get('TSTO',0)/s['n']*100:.1f}%")

_diag(t26,   "2026 (ene–hoy)")
_diag(t_prev,"2023-2025 (referencia)")

# SPY en 2026
spy_26 = spy[spy.index >= "2026-01-01"]
if len(spy_26) > 1:
    spy_ret = (spy_26["Close"].iloc[-1] / spy_26["Close"].iloc[0] - 1) * 100
    spy_vol = spy_26["Close"].pct_change().std() * (252 ** 0.5) * 100
    print(f"\n  SPY 2026: ret={spy_ret:+.1f}%  vol_anual={spy_vol:.1f}%")

# Distribución mensual de señales en 2026
all_2026 = [t for t in result["trades"]
            if str(t["exit_date"])[:4] == "2026"]  # usando último result del sweep
if all_2026:
    by_month: dict = {}
    for t in all_2026:
        m = str(t["exit_date"])[:7]
        by_month.setdefault(m, []).append(t["return_pct"]*100)
    print("\n  2026 por mes (ops, avg_ret%):")
    for m in sorted(by_month):
        rs = by_month[m]
        print(f"    {m}: n={len(rs):3d}  avg={np.mean(rs):+.2f}%"
              f"  pct_neg={sum(1 for r in rs if r<0)/len(rs)*100:.0f}%")

diag_path = OUT_DIR / "diag_2026.txt"
# redirigir a fichero (reescribe con open)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    _diag(t26,   "2026 (ene–hoy)")
    _diag(t_prev,"2023-2025 (referencia)")
    if len(spy_26) > 1:
        print(f"\n  SPY 2026: ret={spy_ret:+.1f}%  vol_anual={spy_vol:.1f}%")
diag_path.write_text(buf.getvalue(), encoding="utf-8")
print(f"\n✓ Diagnóstico 2026: {diag_path}")
print("\nSweep completado.")
