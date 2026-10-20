# -*- coding: utf-8 -*-
"""
analisis_sl_dinamico.py — Stop Loss dinámico óptimo según días de posición.

Analiza trades ConnorsRSI (desde CSV o ejecutando un backtest limpio) y
simula políticas de SL progresivo para encontrar la configuración óptima.

Uso:
    # Usar el CSV de backtest más reciente:
    C:\\trading_env\\Scripts\\python trading_system\\analisis_sl_dinamico.py

    # Ejecutar backtest limpio con parámetros fijos (fixed_slot + SL=5%):
    C:\\trading_env\\Scripts\\python trading_system\\analisis_sl_dinamico.py --backtest

    # Usar un CSV específico:
    C:\\trading_env\\Scripts\\python trading_system\\analisis_sl_dinamico.py --csv ruta/al/archivo.csv
"""

import sys
import os
import argparse
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault('TRADING_ENV', 'dev')

import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Parámetros del backtest limpio (fijos, independientes de config.py)
# ─────────────────────────────────────────────────────────────────────────────
_BT_START      = '2010-01-01'
_BT_SL_PCT     = 0.05          # 5 % stop loss
_BT_FIXED_SLOT = True          # slot fijo (no reinvierte ganancias)
_BT_FILL_SIM   = True          # simula fills con Low del día
_BT_FILL_CLOSE = False         # no fill al cierre

_COLS_TRADES = {'exit_date', 'net_pnl', 'return_pct', 'dias', 'reason'}
INITIAL_CAPITAL = 10_000.0


# ─────────────────────────────────────────────────────────────────────────────
# 0. Argumentos
# ─────────────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description='Análisis SL dinámico ConnorsRSI')
parser.add_argument('--csv',      type=str, default=None,
                    help='Ruta al CSV de backtest existente')
parser.add_argument('--backtest', action='store_true',
                    help='Ejecutar backtest limpio con parámetros fijos (fixed_slot + SL=5%%)')
args, _ = parser.parse_known_args()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Obtención de datos
# ─────────────────────────────────────────────────────────────────────────────

def _encontrar_csv():
    """Devuelve el CSV de trades ConnorsRSI más reciente en exports/."""
    exports = Path(__file__).parent / 'exports'
    if not exports.exists():
        return None
    candidatos = sorted(exports.glob('connors_*.csv'),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for p in candidatos:
        try:
            cab = pd.read_csv(p, nrows=0, encoding='utf-8-sig')
            if _COLS_TRADES.issubset(set(cab.columns)):
                return p
        except Exception:
            continue
    return None


def _ejecutar_backtest_limpio() -> pd.DataFrame:
    """
    Ejecuta run_connors_backtest con parámetros limpios fijos y devuelve
    un DataFrame de trades listo para analizar.

    Parámetros fijos:
        start_date  = 2010-01-01
        fixed_slot  = True   (slot constante, sin reinversión)
        stop_loss   = 5 %
        FILL_SIM    = True   (simula fill con Low del día)
        FILL_AT_CLOSE = False
    """
    import config
    from modules import connors_rsi
    from modules.yf_cache import set_skip_close_wait

    set_skip_close_wait(True)

    end_dt = date.today().strftime('%Y-%m-%d')

    print(f"\n{'─'*60}")
    print("  Backtest limpio — parámetros fijos")
    print(f"{'─'*60}")
    print(f"  Periodo    : {_BT_START} → {end_dt}")
    print(f"  Slot       : fijo ({INITIAL_CAPITAL:,.0f} € / {config.CONNORS_MAX_POS} pos)")
    print(f"  Stop Loss  : {_BT_SL_PCT*100:.0f}%")
    print(f"  FILL_SIM   : {_BT_FILL_SIM}")
    print(f"  Descargando datos del universo SP500 (~349 tickers)...")
    print(f"  (puede tardar varios minutos)\n")

    # Sobrescribir temporalmente flags de fill en config
    _orig_fill_sim   = config.FILL_SIM
    _orig_fill_close = config.FILL_AT_CLOSE
    config.FILL_SIM    = _BT_FILL_SIM
    config.FILL_AT_CLOSE = _BT_FILL_CLOSE

    try:
        resultado = connors_rsi.run_connors_backtest(
            tickers      = None,       # usa load_universe()
            start_date   = _BT_START,
            end_date     = end_dt,
            skip_download= False,
            export       = True,       # guarda CSV en exports/ para reutilizar
            verbose      = False,
            fixed_slot   = _BT_FIXED_SLOT,
            sl_slip      = 0.0,
            stop_loss_pct= _BT_SL_PCT,
        )
    finally:
        config.FILL_SIM    = _orig_fill_sim
        config.FILL_AT_CLOSE = _orig_fill_close

    trades = resultado.get('trades', [])
    if not trades:
        print("ERROR: el backtest no generó trades.")
        input("\nPulsa Enter para salir...")
        sys.exit(1)

    df_bt = pd.DataFrame(trades)
    df_bt['entry_date'] = pd.to_datetime(df_bt['entry_date'])
    df_bt['exit_date']  = pd.to_datetime(df_bt['exit_date'])
    df_bt['ganador']    = df_bt['net_pnl'] > 0
    return df_bt


def _cargar_csv(path: Path) -> pd.DataFrame:
    """Carga y valida un CSV de trades."""
    df_c = pd.read_csv(path, encoding='utf-8-sig')
    faltantes = _COLS_TRADES | {'entry_date'} - set(df_c.columns)
    if faltantes:
        print(f"\nERROR: CSV con columnas incorrectas. Faltan: {sorted(faltantes)}")
        print(f"Tiene: {sorted(df_c.columns.tolist())}")
        print("\nGenera un CSV válido con: --backtest")
        input("\nPulsa Enter para salir...")
        sys.exit(1)
    df_c['entry_date'] = pd.to_datetime(df_c['entry_date'])
    df_c['exit_date']  = pd.to_datetime(df_c['exit_date'])
    df_c['ganador']    = df_c['net_pnl'] > 0
    return df_c


# ── Decidir fuente de datos ───────────────────────────────────────────────────

fuente = "backtest_limpio"

if args.csv:
    df = _cargar_csv(Path(args.csv))
    fuente = Path(args.csv).name
elif args.backtest:
    df = _ejecutar_backtest_limpio()
else:
    csv_path = _encontrar_csv()
    if csv_path is None:
        print("\nNo se encontró ningún CSV de trades en exports/.")
        resp = input("¿Ejecutar backtest limpio ahora? (s/n): ").strip().lower()
        if resp == 's':
            df = _ejecutar_backtest_limpio()
        else:
            print("\nEjecuta con --backtest o genera un CSV desde el menú (2→E).")
            input("\nPulsa Enter para salir...")
            sys.exit(0)
    else:
        df = _cargar_csv(csv_path)
        fuente = csv_path.name

SLOT_DEFAULT = df['valor_compra'].mean() if 'valor_compra' in df.columns else INITIAL_CAPITAL * 0.995 / 3

print(f"\n{'='*70}")
print(f"  ANÁLISIS STOP LOSS DINÁMICO — ConnorsRSI")
print(f"{'='*70}")
print(f"  Fuente : {fuente}")
print(f"  Trades : {len(df):,}  |  {df['entry_date'].min().date()} → {df['exit_date'].max().date()}")
print(f"  Capital: {INITIAL_CAPITAL:,.0f} €  |  Slot medio: {SLOT_DEFAULT:,.0f} €")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def max_drawdown(trades_df, initial=INITIAL_CAPITAL):
    """Max drawdown (%) sobre curva de equity ordenada por fecha de salida."""
    if trades_df.empty:
        return 0.0
    eq     = initial + trades_df.sort_values('exit_date')['net_pnl'].cumsum().values
    peak   = np.maximum.accumulate(eq)
    dd_pct = (eq - peak) / peak * 100
    return float(dd_pct.min())


def resumen(trades_df, label, initial=INITIAL_CAPITAL):
    """Métricas clave de un conjunto de trades."""
    n = len(trades_df)
    if n == 0:
        return dict(label=label, n=0, net=0.0, ret=0.0, wr=0.0, avg=0.0, dd=0.0, dur=0.0)
    net = trades_df['net_pnl'].sum()
    return dict(
        label = label,
        n     = n,
        net   = net,
        ret   = net / initial * 100,
        wr    = trades_df['ganador'].mean() * 100,
        avg   = trades_df['return_pct'].mean() * 100,
        dd    = max_drawdown(trades_df, initial),
        dur   = trades_df['dias'].mean(),
    )


def print_tabla(rows, title):
    """Imprime tabla comparativa de métricas."""
    sep = '─' * 92
    print(f"\n\n{'━'*70}")
    print(f"  {title}")
    print(f"{'━'*70}")
    print(f"{'Política':<38} {'N':>5}  {'Net PnL€':>9}  {'Ret%':>7}  {'WR%':>5}  "
          f"{'Avg%':>7}  {'MaxDD%':>7}  {'Dur':>4}")
    print(sep)
    base = rows[0]
    for r in rows:
        delta = f"({r['ret']-base['ret']:+.1f})" if r is not base else ""
        print(f"{r['label']:<38} {r['n']:>5}  {r['net']:>+9.0f}  "
              f"{r['ret']:>+6.1f}{delta:<9}  {r['wr']:>5.1f}  "
              f"{r['avg']:>+7.4f}  {r['dd']:>+7.2f}  {r['dur']:>4.1f}")
    print('━' * 70)


# ─────────────────────────────────────────────────────────────────────────────
# PARTE 1 — Descriptivo: comportamiento por días de posición
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n\n{'━'*70}")
print("  PARTE 1 — COMPORTAMIENTO POR DÍAS DE POSICIÓN")
print(f"{'━'*70}")

print(f"\n{'Días':>4}  {'N':>5}  {'WR%':>6}  {'Avg ret%':>9}  "
      f"{'Avg loss€':>10}  {'PnL acum€':>11}  {'% del total':>12}")
print('─' * 65)

total_pnl = df['net_pnl'].sum()
for d in sorted(df['dias'].unique()):
    sub      = df[df['dias'] == d]
    perdedor = sub[~sub['ganador']]
    avg_loss = perdedor['net_pnl'].mean() if len(perdedor) else 0.0
    pnl_d    = sub['net_pnl'].sum()
    pct_pnl  = pnl_d / total_pnl * 100 if total_pnl != 0 else 0
    print(f"{int(d):>4}  {len(sub):>5,}  {sub['ganador'].mean()*100:>6.1f}"
          f"  {sub['return_pct'].mean()*100:>+9.4f}  {avg_loss:>+10.2f}"
          f"  {pnl_d:>+11.2f}  {pct_pnl:>+11.1f}%")

print(f"\n--- Supervivencia condicional: si sigues abierto al día D, ¿cuál es tu WR final? ---")
print(f"{'Día D':>6}  {'N restantes':>12}  {'WR eventual':>13}  {'Avg ret eventual':>18}")
print('─' * 55)
for d in sorted(df['dias'].unique()):
    sub = df[df['dias'] >= d]
    print(f"{int(d):>6}  {len(sub):>12,}  {sub['ganador'].mean()*100:>12.1f}%"
          f"  {sub['return_pct'].mean()*100:>+17.4f}%")

print(f"\n--- Pérdidas graves (return_pct < -5%) por duración ---")
print(f"{'Días':>4}  {'N total':>8}  {'N <-5%':>8}  {'% del grupo':>12}  {'Peor pérdida':>13}")
print('─' * 52)
for d in sorted(df['dias'].unique()):
    sub  = df[df['dias'] == d]
    n_5  = (sub['return_pct'] < -0.05).sum()
    peor = sub['net_pnl'].min()
    pct  = n_5 / len(sub) * 100 if len(sub) else 0
    print(f"{int(d):>4}  {len(sub):>8,}  {n_5:>8,}  {pct:>11.1f}%  {peor:>+13.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# PARTE 2 — Política A: Time Stop anticipado
# ─────────────────────────────────────────────────────────────────────────────

def simular_time_stop(df_in, max_dias):
    """
    Simula cerrar posiciones al día max_dias en lugar de esperar al TSTO=5.
    Para trades con dias > max_dias, estima retorno por interpolación lineal.

    Params:
        df_in    : DataFrame de trades.
        max_dias : día máximo de apertura simulado.
    Returns:
        DataFrame con trades simulados.
    """
    rows = []
    for _, t in df_in.iterrows():
        d = t.to_dict()
        if t['dias'] <= max_dias:
            rows.append(d)
        else:
            factor  = max_dias / t['dias']
            slot    = t.get('valor_compra', SLOT_DEFAULT)
            new_ret = t['return_pct'] * factor
            new_net = slot * new_ret
            d2 = d.copy()
            d2['return_pct'] = new_ret
            d2['net_pnl']    = new_net
            d2['dias']       = max_dias
            d2['ganador']    = new_net > 0
            rows.append(d2)
    return pd.DataFrame(rows)


resultados_A = [resumen(df, 'Base (TSTO=5 días, sin SL)')]
for n in [1, 2, 3, 4]:
    sim = simular_time_stop(df, n)
    resultados_A.append(resumen(sim, f'Time Stop anticipado = {n} día(s)'))

print_tabla(resultados_A, "POLÍTICA A — Time Stop anticipado (aprox. lineal)")


# ─────────────────────────────────────────────────────────────────────────────
# PARTE 3 — Política B: Stop Loss progresivo por días
# ─────────────────────────────────────────────────────────────────────────────

def simular_sl_progresivo(df_in, schedule):
    """
    Aplica SL variable según el día de la posición (interpolación lineal).

    Params:
        df_in    : DataFrame de trades.
        schedule : dict {dia: umbral_sl (negativo)} — None = sin SL ese día.
    Returns:
        DataFrame con trades simulados.
    """
    rows = []
    for _, t in df_in.iterrows():
        dias_t    = int(t['dias'])
        ret_final = t['return_pct']
        slot      = t.get('valor_compra', SLOT_DEFAULT)
        d_orig    = t.to_dict()

        disparado = False
        for dia in range(1, dias_t + 1):
            umbral = schedule.get(dia)
            if umbral is None:
                continue
            ret_dia = ret_final * (dia / dias_t) if dias_t > 0 else ret_final
            if ret_dia <= umbral:
                d2 = d_orig.copy()
                d2['return_pct'] = umbral
                d2['net_pnl']    = slot * umbral
                d2['dias']       = dia
                d2['ganador']    = False
                rows.append(d2)
                disparado = True
                break

        if not disparado:
            rows.append(d_orig)

    return pd.DataFrame(rows)


POLITICAS_B = {
    'Base (sin SL)':            {d: None for d in range(1, 6)},
    'SL fijo -5% desde día 1':  {d: -0.05 for d in range(1, 6)},
    'SL fijo -7% desde día 1':  {d: -0.07 for d in range(1, 6)},
    'SL fijo -10% desde día 1': {d: -0.10 for d in range(1, 6)},
    'SL progresivo -10→-5%':    {1: -0.10, 2: -0.08, 3: -0.07, 4: -0.06, 5: -0.05},
    'SL progresivo -8→-5%':     {1: -0.08, 2: -0.07, 3: -0.06, 4: -0.055, 5: -0.05},
    'Libre d1, SL -5% d2+':     {1: None, 2: -0.05, 3: -0.05, 4: -0.05, 5: -0.05},
    'Libre d1-2, SL -5% d3+':   {1: None, 2: None, 3: -0.05, 4: -0.05, 5: -0.05},
    'Libre d1-3, SL -5% d4+':   {1: None, 2: None, 3: None, 4: -0.05, 5: -0.05},
    'Libre d1-2, SL -8% d3+':   {1: None, 2: None, 3: -0.08, 4: -0.07, 5: -0.06},
    'Libre d1-2, SL -10% d3+':  {1: None, 2: None, 3: -0.10, 4: -0.08, 5: -0.06},
}

resultados_B = []
for nombre, schedule in POLITICAS_B.items():
    sim = df.copy() if all(v is None for v in schedule.values()) \
          else simular_sl_progresivo(df, schedule)
    resultados_B.append(resumen(sim, nombre))

print_tabla(resultados_B, "POLÍTICA B — Stop Loss progresivo por días (aprox. lineal)")


# ─────────────────────────────────────────────────────────────────────────────
# PARTE 4 — Trades perdedores tardíos (dias >= 3)
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n\n{'━'*70}")
print("  PARTE 4 — TRADES PERDEDORES TARDÍOS (dias >= 3 y net_pnl < 0)")
print(f"{'━'*70}")

tardios = df[(df['dias'] >= 3) & (~df['ganador'])]
print(f"\nTotal : {len(tardios):,}  ({len(tardios)/len(df)*100:.1f}% de todos los trades)")
if len(tardios):
    print(f"PnL   : {tardios['net_pnl'].sum():+,.2f} €  |  "
          f"Avg {tardios['net_pnl'].mean():+.2f} €  |  Peor {tardios['net_pnl'].min():+.2f} €")

    print(f"\n{'Días':>4}  {'N perdedores':>13}  {'PnL total€':>11}  {'Avg loss€':>10}  {'Peor€':>9}")
    print('─' * 55)
    for d in sorted(tardios['dias'].unique()):
        sub = tardios[tardios['dias'] == d]
        print(f"{int(d):>4}  {len(sub):>13,}  {sub['net_pnl'].sum():>+11.2f}"
              f"  {sub['net_pnl'].mean():>+10.2f}  {sub['net_pnl'].min():>+9.2f}")

    print(f"\nRazón de salida:")
    for reason, cnt in tardios['reason'].value_counts().items():
        print(f"  {reason}: {cnt:,} ({cnt/len(tardios)*100:.1f}%)")

print(f"\n{'━'*70}")
print("  NOTA METODOLÓGICA")
print(f"{'━'*70}")
print("""
  Las Políticas A y B usan interpolación lineal del retorno final para
  estimar la posición del trade en días intermedios (aproximación).

  Para simulaciones exactas usa el backtest real:
      python trading_system\\analisis_sl_dinamico.py --backtest
  que llama a run_connors_backtest() con precios reales día a día.
""")

input("\nPulsa Enter para salir...")
