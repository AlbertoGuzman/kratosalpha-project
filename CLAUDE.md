# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Entorno Python (CRÍTICO)

Usar **siempre** el entorno virtual en `C:\trading_env`. El Python del sistema y el de Microsoft Store causan errores de instalación por rutas largas (>260 chars).

- Activar: `C:\trading_env\Scripts\activate`
- pip:     `C:\trading_env\Scripts\pip install -r trading_system/requirements.txt`
- python:  `C:\trading_env\Scripts\python`

## Comandos habituales

Todo el código vive bajo [trading_system/](trading_system/). Ejecutar desde la raíz del repo (cada módulo hace `sys.path.insert` para resolver `import config`).

```powershell
C:\trading_env\Scripts\python trading_system\main.py --env dev   # menú Rich (BD: data/dev/)
C:\trading_env\Scripts\python trading_system\main.py --env pre   # BD: data/pre/
C:\trading_env\Scripts\python trading_system\main.py --env pro   # BD: data/pro/
C:\trading_env\Scripts\python -m pytest trading_system\tests     # suite rápida (~15s)
C:\trading_env\Scripts\python -m pytest trading_system\tests --run-slow   # incluye backtests reales
C:\trading_env\Scripts\python -m pytest trading_system\tests\test_connors_base.py
C:\trading_env\Scripts\python -m pytest trading_system\tests\test_connors_base.py::TestConnorsCapital -v

# Modos headless (Task Scheduler / cron):
C:\trading_env\Scripts\python trading_system\main.py --run all   --env pre  # autopiloto completo (~15:20 local)
C:\trading_env\Scripts\python trading_system\main.py --run exits --env pre  # solo salidas con precio Alpaca (~21:30 local / 15:30 ET)
```

`--env` es **obligatorio** al lanzar `main.py` — sin él, `argparse` aborta con exit code 2. Cada entorno tiene su propio `trading_book.db` aislado bajo `trading_system/data/<env>/`. La caché de yfinance (`data/cache/`) es **compartida** entre entornos. El formato en disco lo autodetecta `_DISK_FMT`: **parquet** si `pyarrow` está instalado (lo está, `requirements.txt`), si no `csv`. El panel de arranque marca el entorno activo en color (verde=dev, amarillo=pre, rojo=pro). Para los tests, `conftest.py` setea `TRADING_ENV=dev` por defecto.

`pytest.ini` ya añade `-v --tb=short`. La suite son **~213 tests** (200 passing, 10 skipped; 3 fallos ambientales si faltan los ficheros de datos no versionados — ver más abajo). Los tests marcados `@pytest.mark.slow` (backtest ConnorsRSI 2019-2026) se omiten por defecto y requieren `--run-slow`. No hay linter ni type-checker configurado.

[config.py](trading_system/config.py) hace `load_dotenv(.env)` al arrancar ([config.py:12](trading_system/config.py#L12)) — un `.env` en la raíz del repo (gitignored) alimenta las API keys. Variables relevantes (todas opcionales):
- `ANTHROPIC_API_KEY` — análisis IA (módulo `ai_analysis.py`, hoy fuera del menú).
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` — broker. Se leen **solo** de entorno/`.env` ([config.py:208-209](trading_system/config.py#L208-L209)); ya **no** hay keys hardcodeadas (movidas a `.env` en commit b314161). Si faltan, quedan vacías y Alpaca falla al conectar. `ALPACA_ENABLED=True` y `ALPACA_PAPER=True` por defecto.
- `GITHUB_ENABLED` / `GITHUB_TOKEN` / `GITHUB_REPO` — publicación del track record público (ver sección [Publicación de track record en GitHub](#publicación-de-track-record-en-github-scriptspublish_logspy)). `GITHUB_REPO` acepta `usuario/repo` o URL completa.

## Arquitectura

El sistema nació como swing-trading (score EMA/RSI/Vol/ADX) y, tras varias iteraciones, opera **una única estrategia**:

- **ConnorsRSI** — mean reversion a corto plazo (1-5 días) sobre el universo SP500.

> **Nota histórica:** este proyecto deriva de *kairos-project*, que corría dos estrategias en paralelo (Clenow "Stocks on the Move" + ConnorsRSI). En *kratosalpha* **Clenow se eliminó por completo** (módulos, menú, capital, tests y docs) para quedar mono-estrategia. ConnorsRSI absorbió todo el capital del sistema (`CONNORS_CAPITAL = 10.000 €`). No reintroducir Clenow sin petición explícita.

ConnorsRSI usa la BD SQLite ([data/trading_book.db](trading_system/data/trading_book.db)) como fuente de verdad. Una capa opcional de **Alpaca** ejecuta órdenes paper/real.

### Menú actual ([main.py](trading_system/main.py))

Las opciones de swing trading (señales/backtest/gráficos/paper trading/IA/CSV) se retiraron en su día. El menú vivo es:

| Opción | Función | Módulo |
|--------|---------|--------|
| 1 | Autopiloto (ConnorsRSI + informe) | `main._opcion_autopiloto` |
| 2 | ConnorsRSI — submenú A-H | `main._opcion_connors_submenu` |
| 2→A | Ver señales de entrada hoy | `connors_rsi.opcion_senales_hoy` |
| 2→B | Ver posiciones abiertas | `connors_rsi.opcion_posiciones_abiertas` |
| 2→C | Registrar entrada | `connors_rsi.opcion_registrar_entrada` |
| 2→D | Registrar salida | `connors_rsi.opcion_registrar_salida` |
| 2→E | Ejecutar backtest | `connors_rsi.opcion_backtest` |
| 2→F | Ver historial | `connors_rsi.opcion_historial` |
| 2→G | Ver resumen de rentabilidad | `connors_rsi.opcion_resumen` |
| 2→H | Exportar a CSV | `connors_rsi.opcion_exportar_csv` |
| 3 | Resumen de capital (ConnorsRSI) | `main.show_capital_panel` |
| 4 | Operations Log — inspección detallada (A-H) | `operations_log.menu` |
| 5 | Validar universo contra yfinance | `data.validate_universe` |
| 6 | Ver logs (system / trading) | `main._opcion_ver_logs` |
| 7 | Estado Alpaca — dashboard + posiciones + órdenes | `main._opcion_alpaca_estado` |

El **Parameter Sweep** (barrido de parámetros) se eliminó junto con Clenow: ni el submenú de ConnorsRSI ni config contienen ya `*_SWEEP_PARAMS`.

`main()` arranca con `show_capital_panel()` y, si `ALPACA_ENABLED`, llama a `sync_alpaca_vs_sqlite()` para reportar (no corregir) divergencias.

### Opción 8 — dashboard de estado ([main.py](trading_system/main.py))

`_opcion_alpaca_estado()` muestra (en este orden):
1. **Panel "KAIROS TRADING SYSTEM"** (`_mostrar_panel_alpaca`): equity/cash/invertido en vivo de Alpaca, `PnL total` (equity − capital inicial del portfolio history, con fallback a `config.CAPITAL_JUEGO`), capital ConnorsRSI desde BD, nº posiciones/pendientes y próxima ejecución (`_proxima_ejecucion`, vía `AUTOPILOT_EXEC_TIME` + calendario NYSE). En dev / sin Alpaca cae a datos de BD con título `(sin Alpaca)`.
2. **Tabla de posiciones** (`_tabla_posiciones_alpaca`): columnas `Ticker · Estrat. · Qty · Entrada · Actual · Val.total · PnL · PnL%`, ordenada por **PnL% descendente**, con fila TOTAL. Importes en `$` con comas.
3. **Tabla de órdenes** (`_tabla_ordenes_alpaca`): añade columna `Estrat.`.

El badge `Estrat.` (ConnorsRSI/`?`) lo resuelve `strategy_db.get_estrategia_por_ticker(db, tipo)` — ver [Alpaca](#alpaca-alpaca_brokerpy).

### Universo

No hay constantes `TICKERS["IBEX"] / TICKERS["SP500"]`. El universo activo se carga desde un **CSV externo** vía [data.load_universe()](trading_system/modules/data.py#L32):

- Ruta en `config.UNIVERSE_FILE` (default `data/sp500_universe.csv`, ~349 tickers SP500 con 11 sectores GICS).
- Formato `ticker,sector,activo` — solo se cargan filas con `activo=1`.
- Solo símbolos NYSE/NASDAQ, sin sufijo de mercado.
- IBEX se eliminó del universo (activos laterales, incompatibles con la estrategia momentum/mean-reversion).

Para backtests históricos, [data.get_sp500_historical_tickers(fecha)](trading_system/modules/data.py#L220) obtiene los componentes SP500 reales a esa fecha desde el dataset fja05680/sp500 (caché en `data/sp500_historical_components.csv`, refresh cada 7 días, fallback a `load_universe()` si falla la descarga).

**Convención yfinance**: todas las descargas usan `auto_adjust=True` (precios ajustados por splits/dividendos). Mantener en módulos nuevos.

### Pipeline ConnorsRSI ([connors_rsi.py](trading_system/modules/connors_rsi.py))

- Indicador `ConnorsRSI = mean(RSI3_close, RSI2_streak, percentile_rank_100d)`.
- **Entrada (3 condiciones AND)**: `CRSI < CONNORS_ENTRY_CRSI` (25) + streak bajista ≥ `CONNORS_MIN_STREAK` (3 días) + `Close > SMA200` del ticker + SPY > SMA200. Se envía orden **limit a `Close × (1 − CONNORS_ENTRY_LIMIT)`** (1 % bajo el cierre).
- **Salida**: `CRSI > CONNORS_EXIT_CRSI` (60), stop fijo `CONNORS_STOP_LOSS` (5 %) o time-stop `CONNORS_TIME_STOP` (5 días). Con `EXIT_MOC=True` la orden de cierre se envía como MOC (Market on Close, `TimeInForce.CLS`) → fill al cierre oficial de NYSE, igual que el backtest.
- Capital `CONNORS_CAPITAL` (default 10 000 € — todo el capital del sistema tras eliminar Clenow).
- Datos descargados con `yf.download` directo, **sin pasar por** `data.get_data()`.

#### Alineación backtest ↔ operativa real

El gap principal entre backtest (+41 %) y real (~10-25 %) tiene dos causas:

1. **Fill rate de entradas**: el backtest asume 100 % de fill con descuento `CONNORS_ENTRY_LIMIT=0.01`. En real, el fill rate es ~50 % porque muchos días el precio no baja hasta el límite. Esto es estructural y no elimina el sesgo (las que no se llenan habrían sido buenas). No corregir el backtest; aceptar que real ≈ backtest × fill_rate.

2. **Timing de salidas (RESUELTO)**: el backtest calcula CRSI[D] con el cierre del día D para decidir si salir ese mismo día. En real, si se ejecuta a las 09:20, solo se dispone de CRSI[D-1] → salida un día tarde. **Solución**: dos pasadas Task Scheduler:
   - **~15:20 local** (`--run all`): reconcilia pendientes, entradas del día, informe.
   - **~21:30 local / ~15:30 ET** (`--run exits`): obtiene `current_price` de Alpaca por posición → `_append_today_price` añade esa fila de hoy → `compute_connors` produce CRSI[D] real → envía MOC si procede (deadline Alpaca 15:50 ET).

#### `_append_today_price(df, precio) -> DataFrame`

Helper en `connors_rsi.py`. Recibe un DataFrame histórico y el precio actual (obtenido de `alpaca_broker.get_positions()['current_price']`). Añade (o reemplaza) una fila con la fecha de hoy en el índice, con `Open=High=Low=Close=precio` y `Volume=0`. Permite que `compute_connors` calcule CRSI[D] sin depender de yfinance intradía (poco fiable).

#### `_scan_exit_signals(positions, precios_alpaca=None) -> list`

Acepta un dict opcional `{ticker: current_price}`. Cuando está presente:
- `cur_p` (precio para stop-loss) usa el precio Alpaca en lugar del último cierre de caché.
- Llama a `_append_today_price(df, cur_p)` antes de `compute_connors` → CRSI[D] en lugar de CRSI[D-1].
- El backtest **no se modifica**: sigue usando CRSI[D] y Close[D] para exits (comportamiento original preservado).

### Modelo de datos relacional ([connors_rsi.py](trading_system/modules/connors_rsi.py) `_DDL`)

Desde el Bloque 6 (2026-06-05) el esquema es **relacional de estado único**, no las antiguas tablas de ciclo de vida (`connors_trades` / `connors_portfolio` / `connors_pending_orders`, **eliminadas**). El DDL vive en `connors_rsi._DDL` (única fuente de verdad; los tests lo reutilizan):

- **`connors_operaciones`** — una operación es **una fila** que transiciona por `UPDATE` entre estados, **nunca se borra** (salvo una pendiente que expira/cancela sin llegar a ejecutarse):
  - `estado TEXT CHECK (estado IN ('pendiente','abierta','cerrada'))` — el ciclo de vida.
  - Vistas lógicas: `pendiente` = orden enviada a Alpaca sin fill · `abierta` = posición en cartera · `cerrada` = trade cerrado.
  - Tipado fuerte con `CHECK` (`entry_price>0`, `shares>0`, `valor_compra>=0`, `stop_loss_price>0`, `reason IN ('CRSI_EXIT','SL','TSTO','MANUAL')`). Columnas de salida (`exit_*`, `net_pnl`, `dias`…) son NULL hasta cerrar. `created_at`/`updated_at` de auditoría.
  - **Índice único parcial** `idx_conn_op_ticker_activa ON (ticker) WHERE estado <> 'cerrada'`: un ticker no puede tener dos operaciones vivas a la vez (sí permite re-entrada tras cerrar). Índices por `estado`, `entry_date`, `exit_date`.
- **`connors_capital`** — ledger de capital con **FK real** `operacion_id REFERENCES connors_operaciones(id)` (NULL para el registro `inicio`). `PRAGMA foreign_keys = ON` se activa en `strategy_db.get_connection()` (factory central).

**Flujo ConnorsRSI con Alpaca**: `_abrir_posicion` envía la orden limit y crea la operación en estado `pendiente` (**no descuenta cash**). `_check_pending_orders` (inicio de la opción 2 / autopiloto) reconcilia: `filled` → `_materialize_pending_as_position` hace `UPDATE estado='abierta'` (precio real `filled_avg_price`) + descuenta cash; `expired`/`cancelled` → borra la pendiente sin tocar cash; `new` → sigue pendiente. El cierre (`_close_position_db`) hace `UPDATE estado='cerrada'` rellenando los campos de salida. Con `ALPACA_ENABLED=False`, `_abrir_posicion` inserta directamente en estado `abierta` (sin paso `pendiente`).

`_get_open_positions` = `WHERE estado='abierta'`; `_get_pending_orders` = `WHERE estado='pendiente'`; historial/resumen/export = `WHERE estado='cerrada'`. Todas las funciones internas conservan nombre y firma.

[operations_log.py](trading_system/modules/operations_log.py) es read-only sobre `connors_operaciones` (filtrando por `estado`) y ofrece posiciones abiertas, diario cronológico y estadísticas por ticker.

### Alpaca ([alpaca_broker.py](trading_system/modules/alpaca_broker.py))

Capa de ejecución opcional sobre `alpaca-py` (import lazy). SQLite sigue siendo la fuente de verdad; Alpaca solo envía órdenes y consulta estado.

- Activación: `ALPACA_ENABLED=True` en config.
- Paper vs real: `ALPACA_PAPER` controla `ALPACA_BASE_URL` automáticamente.
- La API devuelve **dicts con campos estables** (no objetos SDK) para mantener acoplamiento bajo.
- `limit_price` debe pasarse a Alpaca con **2 decimales** (`round(..., 2)`); bug histórico ya cerrado.
- `connors_operaciones` guarda `alpaca_order_id` para correlacionar con SQLite.
- Si `ALPACA_ENABLED=False` el comportamiento de todos los módulos cae al "sólo SQLite" sin cambios visibles.
- `get_account()` devuelve equity/cash/buying_power/portfolio_value. `get_positions()` devuelve también `current_price`/`unrealized_pl`/`unrealized_plpc`.
- `get_portfolio_history(period="all", timeframe="1D")` → dict `{equity[], timestamp[], capital_inicial, equity_actual}`, **cacheado** en la instancia (el capital inicial no cambia). `capital_inicial = equity[0]`.
- `get_pnl_real(broker=None)` (función de módulo) → `{capital_inicial, equity_actual, pnl_usd, pnl_pct}` o `None` si Alpaca off / falla. Reutilizado por la opción 9, el panel y la sección de capital de Telegram (no duplicar el cálculo).

`strategy_db.get_estrategia_por_ticker(db_path, tipo)` (en [strategy_db.py](trading_system/modules/strategy_db.py)) mapea `{ticker: 'ConnorsRSI'}` leyendo las tablas SQLite. `tipo="posicion"` → `connors_portfolio`; `tipo="orden"` → `connors_pending_orders` + `connors_portfolio` (las ventas SELL ya no están en pending). **Fallback de trades**: para tickers a medio cerrar (borrados de portfolio, aún abiertos en Alpaca) usa el trade cerrado más reciente (`connors_trades`). Las tablas activas tienen prioridad sobre los trades. (Con una sola estrategia el mapa es trivial, pero la función se conserva porque la consumen el dashboard y los badges.)

### Indicadores ([indicators.py](trading_system/modules/indicators.py))

`calculate_indicators()` añade EMA_9, EMA_21, RSI_14, VOLUME_MA20, VOLUME_RATIO, ADX_14, SMA_200 y MACD usando el accesor `df.ta` de **pandas-ta-classic** (la fork mantenida, **no** `pandas_ta`).

### Señales legacy ([signals.py](trading_system/modules/signals.py))

`generate_signals()` aplica un score de -4 a +4 (con MACD) — fuera del menú pero todavía cubierto por tests:
- `SIGNAL_EMA` ±1 si hubo cruce 9/21 en las últimas 3 velas
- `SIGNAL_RSI` ±1 según umbrales `RSI_BULL` / `RSI_BEAR`
- `SIGNAL_VOL` ±1 si volumen > `VOLUME_MULTIPLIER` × media y la dirección concuerda con EMA
- `SIGNAL_MACD` ±1
- **Filtro ADX**: si `ADX_14 < ADX_THRESHOLD` (default 20) el `SCORE` final se fuerza a 0.

### Parámetros centralizados ([config.py](trading_system/config.py))

Lo más editado y los valores actuales (sospechar de regresión si cambian sin intención):

```
COMMISSION = 0.0015

CONNORS_CAPITAL = 10000   CONNORS_MAX_POS = 3
CONNORS_ENTRY_CRSI = 25  CONNORS_EXIT_CRSI = 60
CONNORS_STOP_LOSS = 0.05  CONNORS_TIME_STOP = 5  CONNORS_ENTRY_LIMIT = 0.01

CAPITAL_JUEGO = 10_000          # base fija del PnL% del panel / fallback capital inicial
AUTOPILOT_EXEC_TIME = "22:30"   # hora (informativa) que muestra el panel opción 8

ALPACA_ENABLED = True   ALPACA_PAPER = True   (sobreescritos por config/{env}.properties)

# Publicación track record (leídas del .env; ver sección dedicada)
GITHUB_ENABLED = False  GITHUB_TOKEN = ""  GITHUB_REPO = ""

YF_RETRY_ATTEMPTS = 8    # reintentos esperando el cierre de la sesión objetivo
YF_RETRY_WAIT_MIN = 30   # minutos entre sondeos (8 × 30 = 4 h; cruza medianoche)

BACKUP_RETENTION_DAYS = 30    LOG_SYSTEM_RETENTION_DAYS = 30    (dev: 7)
PARQUET_RETENTION_DAYS = 90   LOG_TRADING_RETENTION_DAYS = 90   (pre: 180)

# Parámetros de alineación backtest ↔ real (ver sección Pipeline ConnorsRSI):
LOOKAHEAD_FIX = True    # señales de entrada usan datos hasta Close[D-1] (sin look-ahead)
FILL_AT_CLOSE = False   # False = fill al límite descuento; True = fill al Close[D] (MOC backtest)
EXIT_MOC = True         # salidas reales vía orden MOC (fill al cierre NYSE), igual que backtest
```

Los parámetros swing (`STOP_LOSS`, `TRAILING_STOP`, `POSITION_SIZE`, `SIGNAL_BUY/SELL`, etc.) se eliminaron en el Bloque 4. Los de Clenow (`CLENOW_*`, `POSITION_SIZE_FIXED`, `INITIAL_CAPITAL`) y los `*_SWEEP_PARAMS` se eliminaron al pasar a mono-estrategia (Bloque 5). No reintroducirlos.

### Overrides por entorno ([config/](trading_system/config/))

Tras cargar los defaults, `config.py` lee `trading_system/config/{env}.properties` y sobreescribe variables escalares del módulo. Solo afecta a variables ya definidas; claves desconocidas y tipos complejos (dicts) se ignoran. `ALPACA_BASE_URL` se recalcula siempre después de los overrides porque depende de `ALPACA_PAPER`.

Ficheros actuales:
- [config/dev.properties](trading_system/config/dev.properties) — `ALPACA_ENABLED=False`, `LOG_SYSTEM_RETENTION_DAYS=7`
- [config/pre.properties](trading_system/config/pre.properties) — `ALPACA_ENABLED=True`, `ALPACA_PAPER=True`, `LOG_TRADING_RETENTION_DAYS=180`
- `pro.properties` — no existe todavía; usa defaults de `config.py`

### Sistema de logs ([modules/logger.py](trading_system/modules/logger.py))

Dos loggers singleton inicializados una sola vez por proceso:

| Logger | Fichero | Nivel | Retención |
|--------|---------|-------|-----------|
| `kairos.system` | `logs/system_YYYYMMDD.log` | DEBUG | 30 días (dev: 7) |
| `kairos.trading` | `logs/trading_YYYYMMDD.log` | INFO/WARNING | 90 días (pre: 180) |

API: `get_system_logger()` y `get_trading_logger()` — importar desde `modules.logger`. Los ficheros de log se crean en `trading_system/logs/` (en `.gitignore`). Si el directorio no puede escribirse el sistema sigue funcionando (warning Rich, no excepción).

Módulos que emiten logs:
- `yf_cache` — DEBUG por cada ticker (caché hit / descarga yfinance / error); WARNING en reintentos por datos desactualizados
- `alpaca_broker` — INFO conexión, órdenes, fills; ERROR en fallos
- `connors_rsi` — INFO/WARNING aperturas, cierres, capital insuficiente (bloqueado), festivos NYSE
- `main` (autopiloto) — INFO paso a paso + resumen capital + backup al final

### Patrón sys.path

Cada módulo bajo `modules/` empieza con:
```python
sys.path.insert(0, str(Path(__file__).parent.parent))
import config
```
Esto permite `import config` desde cualquier punto sin instalar el proyecto. Mantener este patrón al crear módulos nuevos.

### Calendar NYSE ([modules/data.py](trading_system/modules/data.py))

`es_dia_habil_nyse(fecha=None) -> bool` usa `pandas_market_calendars` para verificar si una fecha es día hábil NYSE. Fallback conservador: `True` si la librería falla. Importada a nivel de módulo en `connors_rsi.py`, `main.py` y `yf_cache.py` para que sea parcheble en tests.

Casos especiales: cuando July 4 cae en sábado, NYSE observa el festivo el **viernes** anterior — el calendario lo maneja correctamente.

### Reintentos yfinance ([modules/yf_cache.py](trading_system/modules/yf_cache.py))

`wait_for_last_close() -> bool` se llama en `_download_universe` tras cargar SPY. Pensado para el **modo automático** (autopiloto lanzado de noche): la **sesión objetivo se ancla a la fecha de lanzamiento**.
- **Ancla explícita `_CLOSE_WAIT_REF`** (módulo `yf_cache`, default `None`): `set_close_wait_ref(fecha)` la fija. El autopiloto (`_opcion_autopiloto`) la setea a `start_ts.date()` y `_run_headless` a `date.today()` capturada antes de cualquier descarga. `wait_for_last_close` toma `fecha_inicio = _CLOSE_WAIT_REF or date.today()`, de modo que si la espera de yfinance **cruza la medianoche** el `fecha_inicio` no salta al día siguiente. `None` = comportamiento por defecto (modo interactivo, sin tocar).
- **Salto en modo interactivo `_SKIP_CLOSE_WAIT`** (default `False`): `set_skip_close_wait(True)` hace que `wait_for_last_close` devuelva `True` de inmediato (opera con los últimos datos disponibles, sin sondear). Lo activa `main()` al entrar al menú interactivo — hay un humano que no debe quedar bloqueado hasta 4 h esperando el cierre del día. El modo headless (`--run`, Task Scheduler) **no** lo activa: el cron sí espera a que yfinance publique la sesión. La guarda es lo primero que comprueba `wait_for_last_close`, antes incluso del check `ALPACA_ENABLED`.
- **Primeros intentos** → comprueba con la **fecha de lanzamiento** (la sesión que cierra ese día; el autopiloto arranca tras el cierre NYSE).
- **Si durante la ejecución cambia el día** (cruce de medianoche) → pasa a comprobar el cierre del **día anterior** (= el día de lanzamiento). En la práctica el objetivo es invariante = fecha de lanzamiento; lo calcula `_objetivo()` apoyándose en `_ultima_sesion_esperada` (salta finde/festivos, tope 10 días).
- **Sondeo cada `YF_RETRY_WAIT_MIN` min (30 por defecto)**, hasta `YF_RETRY_ATTEMPTS` (8 → 4 h, cruza medianoche), re-descargando SPY (invalida caché disco).
- En DEV (`ALPACA_ENABLED=False`) → devuelve `True` sin esperar. Si SPY ya cubre la sesión objetivo → `True` de inmediato. Si agota intentos → `False` → `_download_universe` retorna `({}, None)` → operativa abortada.

### Backup automático ([modules/backup.py](trading_system/modules/backup.py))

`run_backup(env, force_parquet=False)` se ejecuta al inicio del autopiloto:
1. Copia `trading_book.db` a `backups/trading_book_{env}_YYYYMMDD.db`
2. Limpia backups > `BACKUP_RETENTION_DAYS` días
3. Los lunes (o si `force_parquet=True`): exporta las 2 tablas ConnorsRSI (`connors_operaciones`, `connors_capital`) a parquet en `backups/`

### Slot dinámico ConnorsRSI

`_get_slot_size(cash, positions)` en `connors_rsi.py` calcula el slot como `max(capital_actual × 0.995 / MAX_POS, slot_mínimo_inicial)`. El slot crece con ganancias acumuladas pero nunca baja del mínimo inicial. Usado en `aperturas_automaticas` y `opcion_registrar_entrada`.

### Publicación de track record en GitHub ([scripts/publish_logs.py](scripts/publish_logs.py))

`publish_track_record()` se llama al final del autopiloto (en **dev, pre y pro**) para publicar el historial en un repo público y que el track record sea verificable. Gating: solo si `GITHUB_ENABLED=True` + `GITHUB_TOKEN`/`GITHUB_REPO` presentes. **Nunca lanza excepción** (devuelve `False` si se omite/falla).

- Lee operaciones cerradas (`connors_trades`) y abiertas (`connors_portfolio`); valora las abiertas con el precio actual de Alpaca **solo si `ALPACA_ENABLED`** (en dev no se conecta → `P.Actual = "-"`).
- `build_resumen_md()` (función pura, testeable) genera markdown con columnas `F.Compra · Ticker · P.Compra · F.Venta · P.Venta · P.Actual · PnL% · Estado`, ordenado por F.Compra desc, con pie `Operaciones cerradas · Win rate · Capital`. **NO publica estrategia, claves ni rutas**; SÍ tickers/precios/fechas/PnL%.
- Escribe dos ficheros por entorno: `public_logs/<env>/RESUMEN.md` (snapshot rodante) y `public_logs/<env>/<YYYY-MM-DD>.md` (histórico diario). Cada entorno (`dev/`, `pre/`, `pro/`) tiene su carpeta.
- `_git_publish()` clona el repo (clon **completo**, no shallow) en `public_logs/` (gitignored), **sincroniza con el remoto antes de escribir** (`fetch` + `merge -X ours --allow-unrelated-histories`) para no perder ficheros ni que el push sea rechazado por divergencia, escribe, `add`+`commit`+`push` vía `subprocess`. Los **ficheros diarios `<fecha>.md` tienen nombre único → la fusión los conserva todos** (los del remoto + los locales); solo `RESUMEN.md` podría chocar y se resuelve a "ours" (se regenera en cada run). El push se hace **siempre** (sube también commits/merge pendientes). El token solo va en la URL de fetch/push (no se persiste como remoto ni se loguea — se ofusca con `_scrub`). `GITHUB_REPO` se normaliza con `_normalizar_repo` (acepta URL o `usuario/repo`).
- **Nota de seguridad**: el push es una acción saliente; debe lanzarlo el usuario o el Task Scheduler (no se puede ejecutar desde el asistente por bloqueo del clasificador).

### Tests

Ficheros de test activos (13):

| Fichero | Cubre |
|---------|-------|
| `test_config.py` | Parámetros config (incl. ausencia de `CLENOW_*` y `*_SWEEP_PARAMS`) |
| `test_data.py` | get_data, caché, universo |
| `test_yf_cache.py` | Sesión yfinance |
| `test_yf_retry.py` | wait_for_last_close / última sesión esperada (5 tests) |
| `test_connors_base.py` | Lógica ConnorsRSI |
| `test_aperturas_automaticas.py` | aperturas_automaticas (11 tests) |
| `test_alpaca_broker.py` | AlpacaBroker |
| `test_alpaca_integracion.py` | Flujo pending Alpaca |
| `test_capital_panel.py` | Panel de capital |
| `test_operations_log.py` | Operations Log |
| `test_strategy_db.py` | Helpers SQLite + `get_estrategia_por_ticker` (incl. fallback trades) |
| `test_backup.py` | backup.py (7 tests) |
| `test_nyse_calendar.py` | Calendar NYSE |
| `test_notifier.py` | Mensaje diario Telegram |
| `test_publish_logs.py` | Publicación track record (seguridad/no-fuga, formato, carpeta por entorno, fichero diario) |

Fixtures compartidos en [tests/conftest.py](trading_system/tests/conftest.py): `ohlcv_df`, `serie_alcista_10dias`/`serie_bajista_10dias`, `db_connors_vacia` (10.000 €), trades sintéticos Connors y `connors_backtest_2019_hoy` (slow).

**Fallos ambientales conocidos**: 3 tests fallan en una copia limpia del repo por faltar ficheros **no versionados**: `data/sp500_universe.csv` (universo, lo usa `test_config::test_universe_file_apunta_a_csv_existente`) y la BD `data/<env>/trading_book.db` (la usan los 2 tests de `test_backup`). No son regresiones; aparecen al no haber arrancado nunca la app en este entorno.

## Idioma y convenciones

Código, comentarios, docstrings, mensajes Rich y commits en **español**. Al añadir código:
- Rutas con `pathlib.Path`, nunca strings con `/`.
- `try/except` con mensaje descriptivo en español.
- `rich.console.Console`, nunca `print()` plano.
- API keys vía `os.environ.get()` / `.env`, nunca hardcoded.
- Cabecera `# -*- coding: utf-8 -*-` en cada `.py`.
- Docstring en cada función con `Params:` y `Returns:`.

## Operativa diaria

### Esquema de dos pasadas (Task Scheduler recomendado)

Para alinear las salidas con el backtest (CRSI[D] en lugar de CRSI[D-1]) se usan **dos tareas** en el Task Scheduler:

| Hora local | Comando | Qué hace |
|------------|---------|----------|
| ~15:20 | `--run all --env pre` | Backup + reconcilia pendientes + **entradas** del día (señales D-1) + informe + Telegram + GitHub |
| ~21:30 | `--run exits --env pre` | Obtiene `current_price` de Alpaca, calcula CRSI[D], envía **MOC** si procede (deadline 15:50 ET → margen 20 min) |

`--run exits` descarga datos **solo de los tickers en cartera** (no el universo completo de 349), evitando superar el deadline MOC. Saltea el wait de yfinance (`set_skip_close_wait(True)`), ya que el cierre del día está confirmado por Alpaca. Aplica el guard de festivos NYSE.

Opcionalmente se puede añadir una tercera tarea a las ~19:00 para capturar salidas tempranas (si el CRSI sube mucho antes del cierre), aunque no es imprescindible.

- **Alternativa manual**: opción **2** → `D` (cierres) → `C` (aperturas).
- **Modo CLI desatendido**: exit code 0 = OK, 1 = error.

Festivos NYSE bloquean las aperturas ConnorsRSI (`es_dia_habil_nyse`). Los cierres (stop loss, time stop) no tienen guard y pueden ejecutarse siempre.

## Refactoring en curso

[docs/refactoring_plan.md](docs/refactoring_plan.md) recoge el estado del refactoring iniciado 2026-05-29 (leerlo antes de retomar). **Bloques 1 y 2 hechos** (−221 LOC: bug `_last_close`, `data.py` respeta `_DISK_FMT`, funciones muertas borradas, docstrings/comentarios corregidos). Pendientes:

- **Bloque 3 DONE** (2026-05-30) — Creados [yf_cache.py](trading_system/modules/yf_cache.py) y [strategy_db.py](trading_system/modules/strategy_db.py). [data.py](trading_system/modules/data.py), [connors_rsi.py](trading_system/modules/connors_rsi.py) y [paper_clenow.py](trading_system/modules/paper_clenow.py) ya no contienen código de caché ni helpers SQLite duplicados. **No añadir más duplicación entre estos módulos.**
- **Bloque 4 DONE** (2026-05-31) — Eliminados ~1.900 LOC swing: `paper_trading`, `ai_analysis`, `visualization`, `report`, `signals`, `indicators`, `backtest` y 10 handlers de `main.py`. Parámetros swing purgados de [config.py](trading_system/config.py). 166 tests eliminados.
- **Bloque 5 DONE** (2026-06-05) — **Eliminación total de Clenow** (paso de dos estrategias a mono-estrategia ConnorsRSI, fork *kratosalpha*). Borrados `clenow.py`, `paper_clenow.py`, `test_clenow_base.py`. Limpieza transversal en `main.py` (menú renumerado 1-8, autopiloto sin pasos Clenow, panel/dashboard solo Connors, headless sin modo `clenow`, opción 4 backtest solo Connors), `operations_log.py`, `notifier.py`, `publish_logs.py`, `strategy_db.py`, `backup.py`, `alpaca_broker.py`, `yf_cache.py`. **Parameter Sweep eliminado** (config `*_SWEEP_PARAMS` + `run_connors_sweep`). `CONNORS_CAPITAL` 7.000 → 10.000 €; `INITIAL_CAPITAL`/`CLENOW_*`/`POSITION_SIZE_FIXED` borrados. Tests adaptados (eliminado `test_integracion_base.py`, reescritos los acoplados a Clenow).
- **Bloque 6 DONE** (2026-09-21) — **Alineación backtest ↔ operativa real** (salidas MOC + `--run exits`). Añadido `EXIT_MOC=True` (config): cierres reales vía MOC, fill al cierre NYSE igual que backtest. Añadido `--run exits` (argparse + `_run_headless`): pasada desatendida a las 21:30 local / 15:30 ET que obtiene `current_price` de Alpaca, lo añade a la serie histórica con `_append_today_price` y recalcula CRSI[D] antes del deadline MOC (15:50 ET). `_scan_exit_signals` acepta `precios_alpaca` dict para usar precio Alpaca en lugar de caché yfinance. Fix yfinance: se filtran filas con fecha futura en `yf_download_one` (artifacts de `auto_adjust` con eventos corporativos). Operativa pasa a dos pasadas Task Scheduler: `--run all` a las 15:20 + `--run exits` a las 21:30.

## Bugs conocidos pendientes

Sin bugs conocidos activos.

## Fuera de alcance

No implementar sin petición explícita: reintroducir Clenow u otra segunda estrategia, sentiment MT Newswires, ejecución real Alpaca con `ALPACA_PAPER=False`, intradía Polygon.io + VWAP, apalancamiento, dashboard web.
