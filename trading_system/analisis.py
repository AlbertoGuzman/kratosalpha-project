"""
analisis.py — Búsqueda de filtros para evitar operaciones perdedoras.

Enriquece los trades del CSV con contexto de mercado (SPY) en la fecha
de entrada y analiza qué condiciones separan ganadores de perdedores.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd
import numpy as np
import yfinance as yf
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.model_selection import StratifiedKFold, cross_val_score

# ── 1. Cargar trades ──────────────────────────────────────────────────────────
CSV = r'C:\alberto\workspace\kratosalpha-project\trading_system\exports\connors_20100101_20261019_nolookahead_fill_sim.csv'
df = pd.read_csv(CSV)
df = df.dropna(subset=['net_pnl', 'connors_rsi_entrada', 'rsi3_entrada', 'streak_entrada'])
df['entry_date'] = pd.to_datetime(df['entry_date'])
df['exit_date']  = pd.to_datetime(df['exit_date'])
df['target']     = (df['net_pnl'] > 0).astype(int)
print(f"Trades: {len(df)}  WR base: {df['target'].mean()*100:.2f}%\n")

# ── 2. Descargar SPY para contexto de mercado ─────────────────────────────────
print("Descargando SPY...")
start_dl = df['entry_date'].min() - pd.Timedelta(days=300)
end_dl   = df['entry_date'].max() + pd.Timedelta(days=5)
spy_raw  = yf.download("SPY", start=start_dl.strftime("%Y-%m-%d"),
                        end=end_dl.strftime("%Y-%m-%d"),
                        auto_adjust=True, progress=False)
spy = spy_raw[["Close"]].copy()
spy.index = pd.to_datetime(spy.index)
spy.columns = ["spy_close"]
spy["spy_sma200"]  = spy["spy_close"].rolling(200).mean()
spy["spy_sma50"]   = spy["spy_close"].rolling(50).mean()
spy["spy_ret5"]    = spy["spy_close"].pct_change(5)    # retorno SPY últimos 5 días
spy["spy_ret20"]   = spy["spy_close"].pct_change(20)   # retorno SPY últimos 20 días
spy["spy_atr20"]   = (spy["spy_close"].pct_change().abs()
                       .rolling(20).mean())             # volatilidad proxy (ATR diario)
spy["spy_margin"]  = (spy["spy_close"] / spy["spy_sma200"] - 1)  # distancia al SMA200
print(f"SPY: {spy.index[0].date()} → {spy.index[-1].date()}\n")

# ── 3. Unir contexto de mercado a cada trade ──────────────────────────────────
# Buscar la fila de SPY más cercana a la fecha de entrada (hacia atrás)
spy_sorted = spy.sort_index()

def _get_spy_row(entry_dt):
    idx = spy_sorted.index.searchsorted(entry_dt, side="right") - 1
    if idx < 0:
        return pd.Series(dtype=float)
    return spy_sorted.iloc[idx]

spy_ctx = pd.DataFrame(
    [_get_spy_row(d) for d in df["entry_date"]],
    index=df.index
)
df = pd.concat([df, spy_ctx], axis=1)
df = df.dropna(subset=["spy_ret5", "spy_ret20", "spy_atr20", "spy_margin"])
print(f"Trades tras merge SPY: {len(df)}\n")

# ── 4. Split temporal IS / OOS ────────────────────────────────────────────────
mask_is  = df['exit_date'] < '2020-01-01'
mask_oos = df['exit_date'] >= '2020-01-01'
print(f"IS  2010-2019: {mask_is.sum()} trades  WR={df.loc[mask_is,'target'].mean()*100:.2f}%")
print(f"OOS 2020-2026: {mask_oos.sum()} trades  WR={df.loc[mask_oos,'target'].mean()*100:.2f}%\n")

# ── 5. Árbol con features enriquecidas ────────────────────────────────────────
features_enrich = [
    'connors_rsi_entrada', 'rsi3_entrada', 'streak_entrada',
    'spy_ret5',    # momentum SPY últimos 5 días
    'spy_ret20',   # momentum SPY últimos 20 días
    'spy_atr20',   # volatilidad de mercado
    'spy_margin',  # distancia SPY a su SMA200
]

X_is  = df.loc[mask_is,  features_enrich]
y_is  = df.loc[mask_is,  'target']
X_oos = df.loc[mask_oos, features_enrich]
y_oos = df.loc[mask_oos, 'target']

print("=== CROSS-VALIDATION (K=5, IS) con features enriquecidas ===")
for depth in [2, 3, 4, 5]:
    m  = DecisionTreeClassifier(max_depth=depth, random_state=42)
    cv = StratifiedKFold(n_splits=5, shuffle=False)
    sc = cross_val_score(m, X_is, y_is, cv=cv, scoring='accuracy')
    print(f"  depth={depth}: {sc.mean()*100:.2f}% ± {sc.std()*100:.2f}%")

best_tree = DecisionTreeClassifier(max_depth=3, random_state=42)
best_tree.fit(X_is, y_is)
from sklearn.metrics import accuracy_score
print(f"\n  OOS accuracy (depth=3): {accuracy_score(y_oos, best_tree.predict(X_oos))*100:.2f}%")
print(f"  WR base OOS           : {y_oos.mean()*100:.2f}%\n")

print("=== REGLAS (depth=3, entrenado en IS) ===")
print(export_text(best_tree, feature_names=features_enrich))

print("=== IMPORTANCIA DE VARIABLES ===")
imp = pd.DataFrame({'Variable': features_enrich,
                    'Importancia': best_tree.feature_importances_})
print(imp.sort_values('Importancia', ascending=False).to_string(index=False))
print()

# ── 6. Análisis directo por condición de mercado (sin modelo) ─────────────────
# La pregunta útil: ¿bajo qué condiciones de SPY pierden más operaciones?
print("=== WIN RATE POR RÉGIMEN DE MERCADO EN ENTRADA (OOS) ===")
d = df[mask_oos].copy()

# 6a. Volatilidad alta vs baja
q75_atr = d['spy_atr20'].quantile(0.75)
q25_atr = d['spy_atr20'].quantile(0.25)
for label, mask in [
    (f"Vol baja  (ATR20 <= {q25_atr*100:.2f}%)", d['spy_atr20'] <= q25_atr),
    (f"Vol media",                                (d['spy_atr20'] > q25_atr) & (d['spy_atr20'] <= q75_atr)),
    (f"Vol alta  (ATR20 >= {q75_atr*100:.2f}%)", d['spy_atr20'] >= q75_atr),
]:
    sub = d[mask]
    print(f"  {label}: n={len(sub):4d}  WR={sub['target'].mean()*100:.2f}%  "
          f"avg_ret={sub['return_pct'].mean()*100:+.4f}%")

print()

# 6b. Momentum SPY últimos 5 días
print("=== WIN RATE POR MOMENTUM SPY-5d EN ENTRADA (OOS) ===")
bins   = [-np.inf, -0.03, -0.01, 0.01, 0.03, np.inf]
labels = ['SPY<-3%', '-3%→-1%', '-1%→+1%', '+1%→+3%', 'SPY>+3%']
d['spy_ret5_bin'] = pd.cut(d['spy_ret5'], bins=bins, labels=labels)
g = d.groupby('spy_ret5_bin', observed=True).agg(
    n=('target','count'), wr=('target','mean'), avg_ret=('return_pct','mean')
)
g['wr_pct']  = g['wr']  * 100
g['ret_pct'] = g['avg_ret'] * 100
print(g[['n','wr_pct','ret_pct']].to_string())
print()

# 6c. Distancia SPY a SMA200
print("=== WIN RATE POR DISTANCIA SPY A SMA200 EN ENTRADA (OOS) ===")
bins2   = [-np.inf, 0.01, 0.03, 0.06, 0.10, np.inf]
labels2 = ['SPY<+1%', '+1%→+3%', '+3%→+6%', '+6%→+10%', 'SPY>+10%']
d['spy_margin_bin'] = pd.cut(d['spy_margin'], bins=bins2, labels=labels2)
g2 = d.groupby('spy_margin_bin', observed=True).agg(
    n=('target','count'), wr=('target','mean'), avg_ret=('return_pct','mean')
)
g2['wr_pct']  = g2['wr']  * 100
g2['ret_pct'] = g2['avg_ret'] * 100
print(g2[['n','wr_pct','ret_pct']].to_string())
print()

# ── 7. Filtro concreto y su impacto ───────────────────────────────────────────
print("=== IMPACTO DE FILTROS CONCRETOS (OOS) ===")
d_all = df[mask_oos].copy()

filtros = {
    "Sin filtro (base)":
        pd.Series([True]*len(d_all), index=d_all.index),
    "Excluir vol alta (ATR20 > p75)":
        d_all['spy_atr20'] <= d_all['spy_atr20'].quantile(0.75),
    "Solo SPY-5d > -1 %":
        d_all['spy_ret5'] > -0.01,
    "Excluir CRSI < 5":
        d_all['connors_rsi_entrada'] >= 5,
    "Excluir CRSI < 5 + vol alta":
        (d_all['connors_rsi_entrada'] >= 5) &
        (d_all['spy_atr20'] <= d_all['spy_atr20'].quantile(0.75)),
    "Excluir CRSI < 5 + SPY-5d > -1 %":
        (d_all['connors_rsi_entrada'] >= 5) &
        (d_all['spy_ret5'] > -0.01),
}

for nombre, m in filtros.items():
    sub = d_all[m]
    if len(sub) == 0:
        continue
    wr  = sub['target'].mean() * 100
    ret = sub['return_pct'].mean() * 100
    pct_desc = (1 - len(sub)/len(d_all)) * 100
    print(f"  {nombre:<42s}: n={len(sub):4d} ({pct_desc:4.1f}% desc.)  "
          f"WR={wr:.2f}%  avg_ret={ret:+.4f}%")
