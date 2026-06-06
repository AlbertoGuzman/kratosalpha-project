# -*- coding: utf-8 -*-
"""
Módulo de integración con Alpaca Markets — paper trading (modo simulado por
defecto) o real. SQLite es la fuente de verdad del sistema; este módulo
solamente envía órdenes y consulta el estado de la cuenta.

Convenciones:
    · `alpaca-py` se importa LAZY dentro de los métodos de la clase. Si la
      librería no está instalada y `ALPACA_ENABLED=False`, el módulo se puede
      importar sin problema (no se ejecuta el import lazy).
    · Las llamadas externas devuelven dicts con campos estables, no objetos de
      la SDK, para mantener acoplamiento bajo.
    · Si una llamada falla → se propaga la excepción para que el caller decida
      qué hacer (típicamente: log de warning + seguir con SQLite).
"""

import sqlite3
import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from modules.logger import get_system_logger

console = Console()
_slog = get_system_logger()


# ── Excepciones específicas ──────────────────────────────────────────────────

class AlpacaError(Exception):
    """Error en una llamada a Alpaca."""


class AlpacaNotConfigured(AlpacaError):
    """Faltan credenciales o `ALPACA_ENABLED=False`."""


# ── Clase principal ──────────────────────────────────────────────────────────

class AlpacaBroker:
    """
    Cliente fino sobre `alpaca-py`. Una instancia gestiona una sesión con la
    API de Alpaca. Use `get_broker()` para obtener un singleton compartido.
    """

    def __init__(self) -> None:
        """
        Conecta a Alpaca con las credenciales de `config`. Lanza
        `AlpacaNotConfigured` si faltan API keys.
        """
        if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
            raise AlpacaNotConfigured(
                "Faltan ALPACA_API_KEY / ALPACA_SECRET_KEY en config.py o "
                "variables de entorno."
            )

        # Import lazy — sólo si realmente vamos a usar Alpaca.
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise AlpacaError(
                "alpaca-py no instalado. Ejecuta: pip install alpaca-py"
            ) from exc

        self._TradingClient = TradingClient
        self._DataClient    = StockHistoricalDataClient

        self.trading_client = TradingClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
            paper      = config.ALPACA_PAPER,
        )
        self.data_client = StockHistoricalDataClient(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_SECRET_KEY,
        )

        # Caché del portfolio history (el capital inicial no cambia, así que
        # solo se consulta a Alpaca una vez por sesión).
        self._portfolio_history_cache: dict | None = None

        # Verificación inicial — falla rápido si las credenciales no son válidas.
        try:
            account = self.trading_client.get_account()
            modo = "PAPER" if config.ALPACA_PAPER else "REAL"
            console.print(
                f"[green]✓ Alpaca conectado — modo {modo}  "
                f"(equity: ${float(account.equity):,.2f})[/green]"
            )
            _slog.info("Alpaca conectado · modo %s · equity $%.2f", modo, float(account.equity))
        except Exception as exc:
            _slog.error("Alpaca conexión fallida: %s", exc, exc_info=True)
            raise AlpacaError(f"No se pudo conectar a Alpaca: {exc}") from exc

    # ── Cuenta ───────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        """Resumen de la cuenta: equity, cash, buying_power, portfolio_value."""
        acc = self.trading_client.get_account()
        return {
            "equity":          float(acc.equity),
            "cash":            float(acc.cash),
            "buying_power":    float(acc.buying_power),
            "portfolio_value": float(acc.portfolio_value),
        }

    def get_portfolio_history(
        self, period: str = "all", timeframe: str = "1D",
    ) -> dict:
        """
        Historial del valor de la cartera en Alpaca. El primer valor de
        `equity` es el capital inicial real de la cuenta.

        El resultado se cachea en la instancia: la primera llamada consulta a
        Alpaca y las siguientes devuelven la copia cacheada (el capital inicial
        no varía durante la sesión).

        Params:
            period:    rango temporal ("all", "1M", "1A"…). Default "all".
            timeframe: granularidad ("1D", "1H"…). Default "1D".

        Returns:
            dict con:
                equity          : lista de equity (float) por punto temporal.
                timestamp       : lista de epochs (int) correspondientes.
                capital_inicial : primer equity no nulo (float) o None.
                equity_actual   : último equity no nulo (float) o None.
        """
        if self._portfolio_history_cache is not None:
            return self._portfolio_history_cache

        from alpaca.trading.requests import GetPortfolioHistoryRequest

        req  = GetPortfolioHistoryRequest(period=period, timeframe=timeframe)
        hist = self.trading_client.get_portfolio_history(history_filter=req)

        equity    = [float(e) for e in (hist.equity or []) if e is not None]
        timestamp = list(hist.timestamp or [])
        capital_inicial = equity[0]  if equity else None
        equity_actual   = equity[-1] if equity else None

        result = {
            "equity":          equity,
            "timestamp":       timestamp,
            "capital_inicial": capital_inicial,
            "equity_actual":   equity_actual,
        }
        self._portfolio_history_cache = result
        _slog.info(
            "get_portfolio_history: %d puntos · inicial $%.2f · actual $%.2f",
            len(equity), capital_inicial or 0.0, equity_actual or 0.0,
        )
        return result

    # ── Posiciones ───────────────────────────────────────────────────────────

    def get_positions(self) -> list:
        """Posiciones abiertas en Alpaca (lista de dicts homogéneos)."""
        positions = self.trading_client.get_all_positions()
        out: list = []
        for p in positions:
            out.append({
                "ticker":            p.symbol,
                "qty":               float(p.qty),
                "avg_entry_price":   float(p.avg_entry_price),
                "current_price":     float(p.current_price) if p.current_price else 0.0,
                "unrealized_pl":     float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                "unrealized_plpc":   float(p.unrealized_plpc) if p.unrealized_plpc else 0.0,
            })
        return out

    # ── Órdenes ──────────────────────────────────────────────────────────────

    def get_orders(self, status: str = "all") -> list:
        """Lista de órdenes: status ∈ {"open", "closed", "all"}."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        status_map = {
            "open":   QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all":    QueryOrderStatus.ALL,
        }
        req = GetOrdersRequest(status=status_map.get(status, QueryOrderStatus.ALL))
        orders = self.trading_client.get_orders(filter=req)
        out: list = []
        for o in orders:
            out.append({
                "id":                 str(o.id),
                "ticker":             o.symbol,
                "qty":                float(o.qty) if o.qty else 0.0,
                "side":               str(o.side).lower().split(".")[-1],
                "type":               str(o.order_type).lower().split(".")[-1],
                "status":             str(o.status).lower().split(".")[-1],
                "filled_avg_price":   float(o.filled_avg_price) if o.filled_avg_price else 0.0,
                "created_at":         o.created_at.isoformat() if o.created_at else "",
            })
        return out

    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> dict:
        """
        Envía una orden a Alpaca.

        Params:
            ticker:      símbolo (e.g. "AAPL").
            qty:         número de acciones (puede ser fraccional).
            side:        "buy" o "sell".
            order_type:  "market" o "limit".
            limit_price: precio límite (obligatorio si `order_type=="limit"`).

        Returns:
            dict con `order_id`, `status`, `filled_avg_price`.
        """
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        if order_type.lower() == "limit":
            if limit_price is None:
                raise AlpacaError("limit_price es obligatorio para órdenes 'limit'")
            # Alpaca exige máximo 2 decimales para acciones > 1$
            # (y 4 decimales para penny stocks < 1$).
            if limit_price is not None:
                limit_price = round(limit_price, 2)
            req = LimitOrderRequest(
                symbol=ticker, qty=qty, side=side_enum,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=ticker, qty=qty, side=side_enum,
                time_in_force=TimeInForce.DAY,
            )

        order = self.trading_client.submit_order(order_data=req)
        result = {
            "order_id":         str(order.id),
            "status":           str(order.status).lower().split(".")[-1],
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else 0.0,
        }
        _slog.info(
            "submit_order: %s  side=%s  qty=%.4f  price=%s  order_id=%s",
            ticker, side, qty,
            f"{limit_price:.2f}" if limit_price else "market",
            result["order_id"],
        )
        return result

    def get_order(self, order_id: str) -> dict:
        """
        Consulta el estado de una orden por id. Devuelve un dict homogéneo con
        `status` ya normalizado (e.g. "new", "filled", "expired", "canceled",
        "accepted", "pending_new", "partially_filled", "rejected").
        """
        order = self.trading_client.get_order_by_id(order_id)
        status_raw = str(order.status).lower().split(".")[-1]
        result = {
            "order_id":         str(order.id),
            "ticker":           order.symbol,
            "qty":              float(order.qty) if order.qty else 0.0,
            "filled_qty":       float(order.filled_qty) if order.filled_qty else 0.0,
            "status":           status_raw,
            "filled_avg_price": float(order.filled_avg_price) if order.filled_avg_price else 0.0,
            "side":             str(order.side).lower().split(".")[-1],
        }
        _slog.debug("get_order: %s  ticker=%s  status=%s", order_id, result["ticker"], status_raw)
        return result

    def cancel_order(self, order_id: str) -> bool:
        """Cancela una orden pendiente. Devuelve True si tuvo éxito."""
        try:
            self.trading_client.cancel_order_by_id(order_id)
            return True
        except Exception:
            return False

    def close_position(self, ticker: str) -> dict:
        """Cierra la posición completa de un ticker (orden de mercado)."""
        try:
            order = self.trading_client.close_position(ticker)
            result = {
                "order_id": str(order.id),
                "status":   str(order.status).lower().split(".")[-1],
            }
            _slog.info("close_position: %s  order_id=%s", ticker, result["order_id"])
            return result
        except Exception as exc:
            _slog.error("close_position %s falló: %s", ticker, exc, exc_info=True)
            raise AlpacaError(f"close_position {ticker}: {exc}") from exc

    # ── Datos de mercado ─────────────────────────────────────────────────────

    def get_latest_price(self, ticker: str) -> float:
        """Último precio (trade) del activo."""
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=ticker)
        latest = self.data_client.get_stock_latest_trade(req)
        trade = latest.get(ticker)
        return float(trade.price) if trade else 0.0

    def is_market_open(self) -> bool:
        """True si la sesión bursátil está abierta ahora mismo."""
        clock = self.trading_client.get_clock()
        return bool(clock.is_open)


# ── Singleton + helpers ──────────────────────────────────────────────────────

_broker_instance: AlpacaBroker | None = None


def get_broker() -> AlpacaBroker:
    """
    Devuelve la instancia compartida de `AlpacaBroker`. La crea en la primera
    llamada y la reutiliza después. Lanza `AlpacaNotConfigured` si faltan keys.
    """
    global _broker_instance
    if _broker_instance is None:
        _broker_instance = AlpacaBroker()
    return _broker_instance


def reset_broker() -> None:
    """Resetea la instancia (útil en tests para forzar recreación)."""
    global _broker_instance
    _broker_instance = None


def is_alpaca_enabled() -> bool:
    """`True` si la integración con Alpaca está activada en config."""
    return bool(getattr(config, "ALPACA_ENABLED", False))


def get_pnl_real(broker: "AlpacaBroker | None" = None) -> dict | None:
    """
    PnL real de la cuenta Alpaca: equity actual frente al capital inicial
    (primer equity del portfolio history).

    Devuelve None si Alpaca está desactivado (p.ej. en dev) o si la consulta
    falla — en ese caso el caller debe mostrar "N/A" sin crashear. Reutiliza
    `get_portfolio_history()`, así que opción 9 y Telegram comparten lógica.

    Params:
        broker: instancia opcional; si no se pasa se usa el singleton.

    Returns:
        dict con `capital_inicial`, `equity_actual`, `pnl_usd`, `pnl_pct`
        (floats), o None si no disponible.
    """
    if not is_alpaca_enabled():
        return None
    try:
        broker          = broker or get_broker()
        equity_actual   = broker.get_account()["equity"]
        capital_inicial = broker.get_portfolio_history().get("capital_inicial")
        if not capital_inicial:
            return None
        pnl_usd = equity_actual - capital_inicial
        pnl_pct = pnl_usd / capital_inicial * 100
        return {
            "capital_inicial": capital_inicial,
            "equity_actual":   equity_actual,
            "pnl_usd":         pnl_usd,
            "pnl_pct":         pnl_pct,
        }
    except Exception as exc:
        _slog.warning("get_pnl_real falló: %s", exc)
        return None


# ── Sincronización SQLite ↔ Alpaca ───────────────────────────────────────────

def _sqlite_tickers(db_path: Path, estados: tuple[str, ...]) -> set:
    """Lee los tickers de `connors_operaciones` en los estados indicados."""
    if not db_path.exists():
        return set()
    placeholders = ",".join("?" * len(estados))
    try:
        with sqlite3.connect(str(db_path)) as con:
            rows = con.execute(
                f"SELECT ticker FROM connors_operaciones "
                f"WHERE estado IN ({placeholders})", estados,
            ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


def sync_alpaca_vs_sqlite(db_path: Path | None = None, broker: AlpacaBroker | None = None) -> dict:
    """
    Compara las posiciones abiertas en Alpaca frente a las operaciones vivas en
    SQLite (`connors_operaciones` en estado 'abierta' o 'pendiente'). Solo
    informa, nunca corrige automáticamente.

    Returns:
        dict con conjuntos `only_sqlite` y `only_alpaca` (tickers).
    """
    if db_path is None:
        db_path = config.DB_PATH

    sqlite_set = _sqlite_tickers(db_path, ("abierta", "pendiente"))

    try:
        broker = broker or get_broker()
        alpaca_positions = broker.get_positions()
        # También consideramos tickers con órdenes pendientes (accepted/new/
        # pending_new/partially_filled) como "está en Alpaca" — la posición se
        # materializará cuando se ejecuten. Si sólo miráramos `get_positions()`
        # marcaríamos como huérfanas las aperturas mandadas con mercado cerrado.
        try:
            open_orders = broker.get_orders(status="open")
        except Exception:
            open_orders = []
    except AlpacaError as exc:
        console.print(f"[yellow]⚠ Sync Alpaca: {exc}[/yellow]")
        return {"only_sqlite": set(), "only_alpaca": set()}

    alpaca_set = (
        {p["ticker"] for p in alpaca_positions}
        | {o["ticker"] for o in open_orders if o.get("side") == "buy"}
    )

    only_sqlite = sqlite_set - alpaca_set
    only_alpaca = alpaca_set - sqlite_set

    if not only_sqlite and not only_alpaca:
        console.print("[green]✓ Posiciones SQLite ↔ Alpaca en sincronía.[/green]")
    else:
        for t in sorted(only_sqlite):
            console.print(
                f"[yellow]⚠ Posición {t} en SQLite pero NO en Alpaca[/yellow]"
            )
        for t in sorted(only_alpaca):
            console.print(
                f"[yellow]⚠ Posición {t} en Alpaca pero NO en SQLite[/yellow]"
            )

    return {"only_sqlite": only_sqlite, "only_alpaca": only_alpaca}
