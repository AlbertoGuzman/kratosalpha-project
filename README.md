# kratosalpha-project

Sistema de trading algorítmico **mono-estrategia (ConnorsRSI)** sobre el universo
S&P 500, con menú interactivo Rich, paper trading y una capa opcional de ejecución
en **Alpaca**. SQLite es la fuente de verdad.

> Fork de *kairos-project*: aquí se eliminó por completo la estrategia Clenow para
> quedar mono-estrategia. ConnorsRSI usa todo el capital del sistema.

## Estrategia

**ConnorsRSI** — mean reversion a corto plazo (1-5 días):

- Indicador `ConnorsRSI = mean(RSI3_close, RSI2_streak, percentile_rank_100d)`.
- **Entrada**: `CRSI < 25` + racha bajista ≥ 3 días + `Close > SMA200` del ticker + `SPY > SMA200`. Orden *limit* 1 % bajo el cierre.
- **Salida**: `CRSI > 60`, stop fijo 5 % o time-stop 5 días.
- Capital: `CONNORS_CAPITAL = 10.000 €`, máximo 3 posiciones simultáneas.

## Requisitos

Entorno virtual en `C:\trading_env` (el Python del sistema falla por rutas largas):

```powershell
C:\trading_env\Scripts\pip install -r trading_system/requirements.txt
```

## Uso

```powershell
# Menú interactivo (dev / pre / pro — selecciona la BD bajo data/<env>/)
C:\trading_env\Scripts\python trading_system\main.py --env dev

# Modo desatendido (cron / Task Scheduler)
C:\trading_env\Scripts\python trading_system\main.py --run all --env pro

# Tests
C:\trading_env\Scripts\python -m pytest trading_system\tests
```

`--env` es obligatorio. El menú principal:

| Opción | Descripción |
|--------|-------------|
| 1 | Autopiloto (ConnorsRSI + informe) |
| 2 | ConnorsRSI — señales, posiciones, entradas/salidas, backtest, historial, CSV |
| 3 | Resumen de capital |
| 4 | Backtesting sobre períodos históricos |
| 5 | Operations Log (inspección detallada) |
| 6 | Validar universo contra yfinance |
| 7 | Ver logs |
| 8 | Estado Alpaca (dashboard + posiciones + órdenes) |

## Documentación

Ver [CLAUDE.md](CLAUDE.md) para la arquitectura completa, parámetros, flujo Alpaca,
sistema de logs y operativa diaria.
