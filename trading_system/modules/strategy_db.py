# -*- coding: utf-8 -*-
"""
Helpers SQLite del módulo connors_rsi.py.

El módulo mantiene su `_DB_PATH` como atributo de módulo (para que los tests
puedan monkeypatchearlo) y sus propias funciones wrapper `_get_connection`,
`_get_current_cash`, `_append_capital` y `_get_open_positions`; estas wrappers
delegan aquí para que la lógica viva en un único lugar.

`_init_db` NO se extrae porque el DDL y la migración son específicos del módulo.

API pública:
    get_connection(db_path)
    get_current_cash(db_path, table, default)
    append_capital(db_path, table, cash, valor_posiciones, nota)
    get_open_positions(db_path, table)
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def get_connection(db_path: Path) -> sqlite3.Connection:
    """
    Abre (o crea) la BD SQLite en `db_path`.

    Params:
        db_path: Ruta al fichero .db (se crea el directorio si no existe).
    Returns:
        Conexión con row_factory = sqlite3.Row.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")  # respeta las FK declaradas en el DDL
    return con


def get_current_cash(db_path: Path, table: str, default: float) -> float:
    """
    Devuelve el último valor de `cash` en la tabla de capital indicada.

    Params:
        db_path: Ruta al fichero .db.
        table:   Nombre de la tabla de capital (p.ej. 'connors_capital').
        default: Valor devuelto si la tabla está vacía.
    Returns:
        float con el cash más reciente, o `default` si no hay filas.
    """
    with get_connection(db_path) as con:
        row = con.execute(
            f"SELECT cash FROM {table} ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return float(row["cash"]) if row else default


def append_capital(
    db_path: Path,
    table: str,
    cash: float,
    valor_posiciones: float,
    nota: str,
) -> None:
    """
    Inserta un registro de capital en la tabla indicada.

    Params:
        db_path:           Ruta al fichero .db.
        table:             Nombre de la tabla de capital.
        cash:              Efectivo disponible.
        valor_posiciones:  Valor de mercado de posiciones abiertas.
        nota:              Texto descriptivo del movimiento.
    """
    with get_connection(db_path) as con:
        con.execute(
            f"INSERT INTO {table} "
            "(fecha, cash, valor_posiciones, capital_total, nota) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                date.today().isoformat(),
                cash,
                valor_posiciones,
                cash + valor_posiciones,
                nota,
            ),
        )


def get_open_positions(db_path: Path, table: str) -> dict:
    """
    Devuelve las posiciones abiertas de la tabla de portfolio indicada.

    Params:
        db_path: Ruta al fichero .db.
        table:   Nombre de la tabla de portfolio/posiciones.
    Returns:
        dict {ticker: row_dict} con todas las filas de la tabla.
    """
    with get_connection(db_path) as con:
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
    return {row["ticker"]: dict(row) for row in rows}


# Estados de `connors_operaciones` que cuentan como "activos" según el tipo de
# consulta. Una orden de salida (SELL) corresponde a una operación ya 'abierta',
# por eso 'orden' incluye también las abiertas (no sólo las pendientes); sin esto
# toda venta saldría etiquetada como '?'.
_ESTADOS_POR_TIPO = {
    "posicion": ("abierta",),
    "orden":    ("pendiente", "abierta"),
}

_ETIQUETA = "ConnorsRSI"


def get_estrategia_por_ticker(db_path: Path, tipo: str = "posicion") -> dict:
    """
    Construye un mapa {ticker: 'ConnorsRSI'} leyendo `connors_operaciones`, para
    etiquetar a qué estrategia pertenece cada ticker (única estrategia del
    sistema; la función se conserva porque el dashboard y los badges la usan).

    Prioridad: operaciones activas (estado según `tipo`) y, como fallback para
    tickers ausentes (a medio cerrar en Alpaca), las operaciones cerradas.

    Params:
        db_path: Ruta al fichero .db.
        tipo:    'posicion' → estado 'abierta'; 'orden' → 'pendiente' + 'abierta'.
    Returns:
        dict {ticker: 'ConnorsRSI'}. Vacío si no hay datos o la tabla no existe.
    Raises:
        ValueError si `tipo` no es 'posicion' ni 'orden'.
    """
    if tipo not in _ESTADOS_POR_TIPO:
        raise ValueError(f"tipo inválido: {tipo!r} (usa 'posicion' u 'orden')")

    estados = _ESTADOS_POR_TIPO[tipo]
    placeholders = ",".join("?" * len(estados))
    mapa: dict = {}
    with get_connection(db_path) as con:
        try:
            # Activas según el tipo.
            for r in con.execute(
                f"SELECT DISTINCT ticker FROM connors_operaciones "
                f"WHERE estado IN ({placeholders})", estados,
            ).fetchall():
                mapa.setdefault(r["ticker"], _ETIQUETA)
            # Fallback: operaciones cerradas (sólo rellena tickers no resueltos).
            for r in con.execute(
                "SELECT DISTINCT ticker FROM connors_operaciones "
                "WHERE estado = 'cerrada'"
            ).fetchall():
                mapa.setdefault(r["ticker"], _ETIQUETA)
        except sqlite3.OperationalError:
            return {}  # la tabla aún no existe
    return mapa
