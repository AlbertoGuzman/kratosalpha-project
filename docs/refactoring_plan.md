# Plan de refactoring — COMPLETADO

Refactoring iniciado el 2026-05-29 (Bloques 1-4, hasta 2026-05-31).
Bloque 5 (mono-estrategia, fork *kratosalpha*) y Bloque 6 (modelo relacional) el 2026-06-05.
**Estado final: 201 tests passing, 10 skipped (3 fallos ambientales por datos no versionados).**

---

## Bloque 1 — Crítico (DONE 2026-05-29)

| # | Cambio | Fichero |
|---|---|---|
| 1.1 | Bug `_last_close` huérfano → función definida localmente | `paper_clenow.py` |
| 1.2 | `pyarrow>=15.0.0` instalado en `C:\trading_env` | `requirements.txt` |
| 1.3 | 6 comentarios numéricos + 2 score corregidos | `config.py` |
| 1.4 | 8 docstrings desactualizados (swing/IBEX/ADX/score) | `main.py`, `modules/__init__.py`, `signals.py`, `backtest.py` |
| 1.5 | `data.py` ahora respeta `_DISK_FMT` → caché parquet | `data.py` |

Adicional: borrados 700 CSVs viejos de `data/cache/` (~19 MB).

**−221 LOC neto** (bloques 1+2 combinados).

---

## Bloque 2 — Limpieza segura (DONE 2026-05-29)

| # | Función eliminada | Fichero | LOC |
|---|---|---|---|
| 2.1 | `_clear_universe_cache` | `data.py` | 4 |
| 2.2 | `_insert_pending_clenow` + `_invalidate_session_cache` | `paper_clenow.py` | 21 |
| 2.3 | `verify_prices` (diagnóstico opción 99) | `paper_clenow.py` | 99 |
| 2.4 | `_find_col` + `_apply_sl_tp_exits` + 7 constantes `_COL_*` | `backtest.py` | 129 |

---

## Bloque 3 — Refactor de duplicación (DONE 2026-05-30)

Extraídos dos módulos comunes. `_init_db` permanece en cada módulo (DDL específico por estrategia).

| Paso | Qué | Resultado |
|---|---|---|
| 3.1 | Creado `modules/yf_cache.py` | Detección `DISK_FMT`, 8 funciones caché disco, sesión unificada (`session_data`), `yf_download_one`. ~215 LOC |
| 3.2 | Migrado `connors_rsi.py` | −135 LOC, `import shutil` eliminado, 14 llamadas → `yf_cache.*` |
| 3.3 | Migrado `paper_clenow.py` | −130 LOC, `import shutil` eliminado, `_last_close` conservada |
| 3.4 | Migrado `data.py` | −10 LOC, usa `yf_cache.DISK_FMT / save_to_disk / load_from_disk` |
| 3.5 | Creado `modules/strategy_db.py` | 4 helpers SQLite parametrizados. `_DB_PATH` intacto en cada módulo (monkeypatch). ~104 LOC |
| 3.6 | Tests | `test_yf_cache.py` (16) + `test_strategy_db.py` (10) |

**Decisión de diseño clave**: `_DB_PATH = config.DB_PATH` se mantiene como atributo de módulo en `connors_rsi` y `paper_clenow` para que los monkeypatches de tests (`monkeypatch.setattr(cr, "_DB_PATH", tmp)`) sigan funcionando sin cambios.

---

## Bloque 4 — Borrar era swing (DONE 2026-05-31)

| Qué | Detalle |
|---|---|
| Módulos eliminados | `paper_trading.py`, `ai_analysis.py`, `visualization.py`, `report.py`, `signals.py`, `indicators.py`, `backtest.py` |
| `run_clenow_sweep` | Movida de `backtest.py` a `clenow.py` antes del borrado |
| `main.py` | 10 handlers swing eliminados (~570 LOC), imports swing eliminados, `_init_db()` de arranque eliminado |
| `config.py` | ~35 parámetros swing eliminados (`EMA_*`, `RSI_*`, `TRAILING_STOP*`, `SWAP_*`, `SWEEP_PARAMS`, `ANTHROPIC_*`, etc.). Conservados: `INITIAL_CAPITAL`, `COMMISSION` |
| Tests eliminados | 7 ficheros, 166 tests (`test_backtest`, `test_paper_trading`, `test_signals`, `test_indicators`, `test_visualization`, `test_report`, `test_ai_analysis`) |
| `conftest.py` | Fixtures `df_with_indicators` y `df_with_signals` eliminados |
| `test_config.py` | Clases `TestIndicadores` + params swing reemplazadas por `TestClenow` + `TestConnors` |

**`TRAILING_STOP_ACTIVATION = 0.7`** (bug sospechoso documentado en sesiones anteriores): resuelto por eliminación — el parámetro sólo existía en código swing.

---

## Fixes adicionales (DONE 2026-05-31)

### Backtest no determinístico — `sorted(set(...))`

`_download_universe` en `connors_rsi.py` y `paper_clenow.py` usaba `list(set(tickers + ["SPY"]))`.
Python aplica hash randomization por proceso → orden distinto en cada ejecución → `candidates.sort(key=crsi)` es sort estable → en empates de CRSI el tie-break dependía del orden aleatorio del set.

**Impacto observado**: 15 fechas con trades distintos entre develop y refactoring: 7 swaps directos (GOOG↔GOOGL en 2007/2010, WBD↔KHC, AMD↔KHC, etc.) + 8 efectos en cascada porque el portfolio divergente de días anteriores cambiaba la capacidad disponible en días siguientes.

**Fix**: `sorted(set(tickers + ["SPY"]))` en ambos módulos. El orden alfabético es determinístico entre procesos, versiones de Python y sistemas operativos.

### Acumulación de caché plana — `cleanup_old_caches()`

`data.get_data()` (usado por el backtest Clenow) escribe ficheros planos `TICKER_YYYYMMDD_start_end.{ext}` en `data/cache/`. La función `cleanup_old_caches()` sólo limpiaba subcarpetas `YYYYMMDD/` y dejaba acumularse los ficheros planos indefinidamente.

**Fix en `yf_cache.cleanup_old_caches()`**: ahora también borra ficheros planos en `data/cache/` cuyo nombre no contenga la fecha de hoy. La limpieza ocurre automáticamente al inicio de cada descarga de universo.

---

## Bloque 5 — Eliminación total de Clenow (DONE 2026-06-05, fork *kratosalpha*)

Paso de **dos estrategias paralelas (Clenow + ConnorsRSI) a mono-estrategia (solo ConnorsRSI)**. Decisiones: borrado total, ConnorsRSI absorbe el capital (`CONNORS_CAPITAL` 7.000 → 10.000 €), menú renumerado, Parameter Sweep eliminado por completo. Sin tocar git (lo gestiona el usuario).

| Qué | Detalle |
|---|---|
| Módulos eliminados | `clenow.py`, `paper_clenow.py`, `tests/test_clenow_base.py` (+ fichero basura `=`) |
| `config.py` | `CONNORS_CAPITAL=10000`; borrados `INITIAL_CAPITAL`, `CLENOW_*`, `POSITION_SIZE_FIXED`, `CONNORS_SWEEP_PARAMS`, `CLENOW_SWEEP_PARAMS` |
| `main.py` | Menú renumerado 1-8; sin submenú Clenow; autopiloto sin pasos 3-4 (Clenow); panel/dashboard y `_run_headless` solo Connors; opción 4 backtest solo Connors; eliminados `_opcion_clenow*`, `_opcion_connors_sweep`, `_ask_market_clenow`, `_debe_rebalancear_clenow_hoy` |
| `connors_rsi.py` | Eliminada `run_connors_sweep` |
| Módulos limpiados | `operations_log.py` (vistas solo Connors), `notifier.py` (sin sección Clenow/TOTAL), `publish_logs.py`, `strategy_db.py`, `backup.py` (2 tablas parquet), `alpaca_broker.py`, `yf_cache.py` |
| Tests | Eliminado `test_integracion_base.py` (aislamiento entre estrategias, sin sentido); reescritos `test_config`, `test_capital_panel`, `test_notifier`, `test_strategy_db`, `test_operations_log`, `test_alpaca_*`, `test_nyse_calendar`, `test_connors_base`; `conftest.py` sin fixtures Clenow (`db_clenow_vacia`, `db_test`, `clenow_backtest_2019_hoy`) |

---

## Bloque 6 — Modelo de datos relacional (DONE 2026-06-05)

Sustituido el esquema de **tablas de ciclo de vida** (`connors_trades` / `connors_portfolio` / `connors_pending_orders`, donde las filas migraban y se borraban) por un **modelo relacional de estado único**: una operación es una fila en `connors_operaciones` que transiciona por `UPDATE` (`pendiente` → `abierta` → `cerrada`) y nunca se borra.

| Qué | Detalle |
|---|---|
| `connors_operaciones` | Tabla única con `estado CHECK(...)`, tipado fuerte (CHECK en precios/shares/reason), `created_at`/`updated_at`, índice **único parcial** por ticker activo + índices por estado/fechas |
| `connors_capital` | **FK real** `operacion_id → connors_operaciones(id)`; `PRAGMA foreign_keys = ON` en `strategy_db.get_connection()` |
| Capa de acceso | `_abrir_posicion`/`_materialize_pending_as_position`/`_close_position_db` ahora hacen INSERT/UPDATE de estado (mismas firmas). Lecturas (`_get_open_positions`, `_get_pending_orders`, historial/resumen/export) filtran por `estado` |
| Módulos lectores | `operations_log.py`, `publish_logs.py`, `notifier.py`, `backup.py`, `alpaca_broker.sync_alpaca_vs_sqlite`, `strategy_db.get_estrategia_por_ticker` migrados a `connors_operaciones` |
| Tests | `conftest._create_connors_schema` reutiliza el `_DDL` real; fixtures/asserts migrados a estados; nuevos tests de FK y CHECK (`test_alpaca_integracion.TestSchemaOperaciones`) |

Verificado end-to-end (abrir→cerrar): cash descontado, transición de estado, ledger enlazado por FK, `foreign_key_check` limpio y CHECK de `estado` activo.

---

## Estado final del proyecto

```
trading_system/modules/
  connors_rsi.py     — estrategia ConnorsRSI (única); esquema relacional connors_operaciones
  alpaca_broker.py   — capa Alpaca (opcional)
  operations_log.py  — log ConnorsRSI (read-only)
  notifier.py        — resumen diario Telegram
  backup.py          — backup SQLite + parquet
  data.py            — descarga yfinance + universo SP500
  yf_cache.py        — caché yfinance
  strategy_db.py     — helpers SQLite
  logger.py          — loggers system/trading

trading_system/tests/
  ~214 tests (201 passing, 10 skipped @slow, 3 fallos ambientales por datos no versionados)
  Arranque: C:\trading_env\Scripts\python.exe -m pytest trading_system\tests
```
