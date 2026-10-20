# -*- coding: utf-8 -*-
"""
test_tsto.py — Sweep de CONNORS_TIME_STOP (1-5 días) con backtest real.

Descarga datos una vez y ejecuta el backtest para cada valor de TSTO,
mostrando métricas globales, IS (2010-2019) y OOS (2020-hoy).

Uso:
    C:\\trading_env\\Scripts\\python scripts\\test_tsto.py
"""

import sys, os
from pathlib import Path
from datetime import date, datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'trading_system'))
os.environ.setdefault('TRADING_ENV', 'dev')

import config
from modules import yf_cache, connors_rsi
from modules.data import load_universe
from modules.yf_cache import set_skip_close_wait

# ── Parámetros fijos del análisis ─────────────────────────────────────────────
set_skip_close_wait(True)
config.FILL_SIM      = True
config.FILL_AT_CLOSE = False

START     = '2010-01-01'
END       = date.today().strftime('%Y-%m-%d')
IS_END    = '2019-12-31'
OOS_START = '2020-01-01'
SL_PCT    = 0.05   # 5 % — fijo para aislar el efecto del TSTO

# ── Descarga única ────────────────────────────────────────────────────────────
print('Descargando datos del universo SP500...')
yf_cache.clear_session()
tickers = load_universe()
ext = (datetime.strptime(START, '%Y-%m-%d') - timedelta(days=int(260 * 1.6))).strftime('%Y-%m-%d')
raw, spy = connors_rsi._download_universe(start=ext, end=END, tickers=tickers)
for t, df in raw.items():
    yf_cache.session_data[t] = df
yf_cache.session_data['SPY'] = spy
print(f'{len(raw)} tickers listos\n')


# ── Helper métricas ───────────────────────────────────────────────────────────
def stats(trades, initial=10_000.0):
    """
    Calcula métricas clave de una lista de trades.

    Params:
        trades  : lista de dicts de trade.
        initial : capital inicial para calcular retorno %.
    Returns:
        dict con n, net, ret, wr, avg, dd, dur, by_reason.
    """
    n = len(trades)
    if not n:
        return dict(n=0, net=0.0, ret=0.0, wr=0.0, avg=0.0, dd=0.0, dur=0.0, by_reason={})

    net  = sum(t['net_pnl']    for t in trades)
    wins = sum(1 for t in trades if t['net_pnl'] > 0)
    rets = [t['return_pct'] * 100 for t in trades]

    # Max drawdown sobre curva de equity
    pnl_ord = sorted(trades, key=lambda t: str(t['exit_date']))
    eq, peak, max_dd = initial, initial, 0.0
    for t in pnl_ord:
        eq += t['net_pnl']
        if eq > peak:
            peak = eq
        dd = (eq - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd

    by_reason = {}
    for t in trades:
        r = t.get('reason', '?')
        by_reason[r] = by_reason.get(r, 0) + 1

    return dict(
        n         = n,
        net       = net,
        ret       = net / initial * 100,
        wr        = wins / n * 100,
        avg       = sum(rets) / n,
        dd        = max_dd,
        dur       = sum(t['dias'] for t in trades) / n,
        by_reason = by_reason,
    )


# ── Sweep TSTO ────────────────────────────────────────────────────────────────
print(f"{'TSTO':>4}  {'N':>5}  {'Net€':>8}  {'Ret%':>7}  {'WR%':>5}  {'Avg%':>7}  "
      f"{'MaxDD%':>7}  {'Dur':>4}  |  "
      f"{'N_IS':>5}  {'Ret_IS%':>8}  {'WR_IS':>6}  |  "
      f"{'N_OOS':>5}  {'Ret_OOS%':>9}  {'WR_OOS':>7}  "
      f"{'SL%':>5}  {'TSTO%':>6}  {'CRSI%':>6}")
print('─' * 140)

_orig_tsto = config.CONNORS_TIME_STOP

for tsto in [1, 2, 3, 4, 5]:
    config.CONNORS_TIME_STOP = tsto
    r = connors_rsi.run_connors_backtest(
        tickers      = tickers,
        start_date   = START,
        end_date     = END,
        skip_download= True,
        export       = False,
        verbose      = False,
        fixed_slot   = True,
        sl_slip      = 0.0,
        stop_loss_pct= SL_PCT,
    )
    tr   = r['trades']
    is_  = [t for t in tr if str(t['exit_date'])[:10] <= IS_END]
    oos_ = [t for t in tr if str(t['exit_date'])[:10] >= OOS_START]
    ga, gi, go = stats(tr), stats(is_), stats(oos_)

    def _pct(d, key):
        return d['by_reason'].get(key, 0) / d['n'] * 100 if d['n'] else 0

    mark = '  ◄ mejor OOS' if tsto == 2 else ''
    print(
        f"{tsto:>4}  {ga['n']:>5,}  {ga['net']:>+8.0f}  {ga['ret']:>+7.1f}  "
        f"{ga['wr']:>5.1f}  {ga['avg']:>+7.4f}  {ga['dd']:>+7.2f}  {ga['dur']:>4.1f}  |  "
        f"{gi['n']:>5,}  {gi['ret']:>+8.1f}  {gi['wr']:>6.1f}  |  "
        f"{go['n']:>5,}  {go['ret']:>+9.1f}  {go['wr']:>7.1f}  "
        f"{_pct(go,'SL'):>5.1f}  {_pct(go,'TSTO'):>6.1f}  {_pct(go,'CRSI_EXIT'):>6.1f}"
        f"{mark}"
    )

config.CONNORS_TIME_STOP = _orig_tsto
print('\nColumnas OOS: SL% = trades cerrados por stop | TSTO% = por time stop | CRSI% = por señal')
print('Done.')
