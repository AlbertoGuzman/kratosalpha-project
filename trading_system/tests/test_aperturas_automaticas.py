# -*- coding: utf-8 -*-
"""
Tests de `connors_rsi.aperturas_automaticas` — la función pública
no-interactiva usada por el autopiloto (opción 12) y por el modo A de
`opcion_registrar_entrada`.

Cubrimos:
  · sin slots libres → 0, sin tocar la BD
  · cash insuficiente → 0, sin tocar la BD
  · señales válidas + confirmar=False → abre N posiciones (= slots libres)
  · confirmar=True + usuario dice "n" → 0, sin tocar la BD
  · filtrado de tickers ya en portfolio o en pending
  · reconciliar_primero=True llama a _check_pending_orders
  · ALPACA_ENABLED=False usa el camino SQLite directo (cash descontado)

`_download_universe` y `_scan_entry_signals` se mockean con monkeypatch
para no depender de yfinance ni de red. Las señales se construyen como
dicts con los campos que consume `_abrir_posicion`: ticker, limit_price,
close, crsi, rsi3, streak.
"""

import sqlite3

import pytest

import config
import modules.connors_rsi as cr


# ── Helpers de test ──────────────────────────────────────────────────────────

def _signal(ticker: str, crsi: float = 10.0, limit_price: float = 50.0) -> dict:
    """Construye una señal de entrada sintética con los campos mínimos."""
    return {
        "ticker":      ticker,
        "close":       limit_price * 1.01,
        "crsi":        crsi,
        "rsi3":        crsi,
        "streak":      -3,
        "limit_price": limit_price,
    }


@pytest.fixture
def stub_descarga_y_senales(monkeypatch):
    """
    Reemplaza `_download_universe` y `_scan_entry_signals` por funciones
    que no tocan red. El test concreto sobrescribe la lista de señales
    devolviendo lo que necesite vía `monkeypatch.setattr` o vía la variable
    de captura que devuelve este fixture.
    """
    estado = {"signals": []}

    monkeypatch.setattr(cr, "_download_universe", lambda: ({"DUMMY": None}, "spy"))
    monkeypatch.setattr(cr, "_scan_entry_signals", lambda raw, spy: estado["signals"])
    # Día hábil NYSE forzado: aísla la lógica de apertura del calendario real
    # (si no, estos tests fallarían los fines de semana / festivos por el guard).
    monkeypatch.setattr(cr, "es_dia_habil_nyse", lambda *a, **kw: True)
    # Confirm.ask por defecto: si el test no lo sobrescribe, asumimos NO.
    monkeypatch.setattr(cr.Confirm, "ask", lambda *a, **kw: False)
    return estado


@pytest.fixture
def alpaca_off(monkeypatch):
    """Fuerza ALPACA_ENABLED=False para que `_abrir_posicion` use el flujo SQLite."""
    monkeypatch.setattr(config, "ALPACA_ENABLED", False)


# ── 1) Sin slots libres ──────────────────────────────────────────────────────

def test_sin_slots_libres_devuelve_cero(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """
    Con MAX_POS posiciones ya abiertas, la función debe devolver 0 sin
    tocar la BD ni preguntar confirmación.
    """
    # Llenar el portfolio con CONNORS_MAX_POS posiciones ficticias.
    with sqlite3.connect(str(db_connors_vacia)) as con:
        for i in range(config.CONNORS_MAX_POS):
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
                "VALUES (?, 'abierta', ?,?,?,?,?,?,?,?)",
                (f"TKR{i}", "2026-05-01", 100.0, 99.0, 10.0,
                 1000.0, 1000.0, 95.0, 18.0),
            )

    stub_descarga_y_senales["signals"] = [_signal("OVV")]
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == 0


# ── 2) Cash insuficiente ─────────────────────────────────────────────────────

def test_cash_insuficiente_devuelve_cero(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """Si el cash registrado es < 95 % del slot → no abre y devuelve 0."""
    slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
    with sqlite3.connect(str(db_connors_vacia)) as con:
        con.execute(
            "INSERT INTO connors_capital "
            "(fecha, cash, valor_posiciones, capital_total, nota) "
            "VALUES ('2026-05-29', ?, 0.0, ?, 'fondos_bajos')",
            (slot * 0.5, slot * 0.5),
        )
    stub_descarga_y_senales["signals"] = [_signal("OVV")]
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == 0


# ── 3) Camino feliz: abre hasta slots_libres ─────────────────────────────────

def test_abre_hasta_slots_libres(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """
    Con cartera vacía y 3 señales válidas, `aperturas_automaticas(confirmar=False)`
    debe abrir CONNORS_MAX_POS posiciones (3) y devolver 3.
    """
    stub_descarga_y_senales["signals"] = [
        _signal("OVV", crsi=7.1,  limit_price=55.91),
        _signal("COP", crsi=8.6,  limit_price=115.40),
        _signal("XOM", crsi=9.3,  limit_price=148.31),
        _signal("DVN", crsi=14.9, limit_price=44.69),  # extra: no debe abrirse
    ]
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == config.CONNORS_MAX_POS  # 3

    with sqlite3.connect(str(db_connors_vacia)) as con:
        tickers = {r[0] for r in con.execute(
            "SELECT ticker FROM connors_operaciones WHERE estado='abierta'"
        )}
    assert tickers == {"OVV", "COP", "XOM"}  # los de menor CRSI, DVN excluido


# ── 4) Usuario cancela en el Confirm ─────────────────────────────────────────

def test_confirmar_true_y_usuario_cancela(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off, monkeypatch,
):
    """Si confirmar=True y Confirm.ask devuelve False → no abre y devuelve 0."""
    stub_descarga_y_senales["signals"] = [_signal("OVV")]
    # El stub ya pone Confirm.ask → False por defecto.
    n = cr.aperturas_automaticas(confirmar=True)
    assert n == 0
    with sqlite3.connect(str(db_connors_vacia)) as con:
        n_rows = con.execute("SELECT COUNT(*) FROM connors_operaciones WHERE estado='abierta'").fetchone()[0]
    assert n_rows == 0


# ── 5) Filtrado de tickers ya en cartera ─────────────────────────────────────

def test_filtra_tickers_en_portfolio(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """
    Si una señal corresponde a un ticker ya abierto, debe excluirse del
    listado de candidatos y la apertura se hace con el siguiente.
    """
    # Pre-existe OVV en portfolio.
    with sqlite3.connect(str(db_connors_vacia)) as con:
        con.execute(
            "INSERT INTO connors_operaciones "
            "(ticker, estado, entry_date, entry_price, limit_price, shares, "
            " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
            "VALUES (?, 'abierta', ?,?,?,?,?,?,?,?)",
            ("OVV", "2026-05-25", 55.0, 54.0, 30.0, 1650.0, 1650.0, 52.25, 7.1),
        )

    stub_descarga_y_senales["signals"] = [
        _signal("OVV", crsi=7.1),   # ya en cartera — debe ignorarse
        _signal("COP", crsi=8.6),
        _signal("XOM", crsi=9.3),
    ]
    n = cr.aperturas_automaticas(confirmar=False)
    # Slots libres = MAX_POS - 1 (OVV ya cuenta) = 2
    assert n == config.CONNORS_MAX_POS - 1

    with sqlite3.connect(str(db_connors_vacia)) as con:
        tickers = {r[0] for r in con.execute(
            "SELECT ticker FROM connors_operaciones WHERE estado='abierta'"
        )}
    assert tickers == {"OVV", "COP", "XOM"}


# ── 6) Filtrado de tickers en pending ────────────────────────────────────────

def test_filtra_tickers_en_pending(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """
    Tickers con orden ya enviada (operación en estado 'pendiente') cuentan como
    slot ocupado y se excluyen del nuevo top.
    """
    # Una operación 'pendiente' ocupa slot igual que una abierta.
    cr._init_db()
    with sqlite3.connect(str(db_connors_vacia)) as con:
        con.execute(
            "INSERT INTO connors_operaciones "
            "(ticker, estado, alpaca_order_id, entry_date, limit_price, shares, "
            " slot_size, connors_rsi_entrada, rsi3_entrada, streak_entrada) "
            "VALUES ('TSN', 'pendiente', 'fake-id', '2026-05-26', 64.40, 36.05, "
            " 2321.67, 15.3, 17.2, -2)"
        )

    stub_descarga_y_senales["signals"] = [
        _signal("TSN", crsi=5.0),   # ya en pending — debe ignorarse
        _signal("OVV", crsi=7.1),
        _signal("COP", crsi=8.6),
    ]
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == config.CONNORS_MAX_POS - 1   # 2 (TSN ocupa 1 slot)

    with sqlite3.connect(str(db_connors_vacia)) as con:
        portfolio_tk = {r[0] for r in con.execute(
            "SELECT ticker FROM connors_operaciones WHERE estado='abierta'"
        )}
    assert portfolio_tk == {"OVV", "COP"}    # TSN sigue en pending, no en portfolio


# ── 7) reconciliar_primero=True dispara _check_pending_orders ────────────────

def test_reconciliar_primero_invoca_check_pending(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off, monkeypatch,
):
    """
    Con `reconciliar_primero=True` la función debe llamar a
    `_check_pending_orders` antes de calcular slots; con False, NO debe
    llamarla. Comprobamos el comportamiento con un mock que cuenta llamadas.
    """
    llamadas = {"n": 0}

    def fake_check():
        llamadas["n"] += 1
        return {"filled": 0, "expired": 0, "canceled": 0, "pending": 0}

    monkeypatch.setattr(cr, "_check_pending_orders", fake_check)

    stub_descarga_y_senales["signals"] = []   # da igual, no llegará a abrir
    cr.aperturas_automaticas(reconciliar_primero=False)
    assert llamadas["n"] == 0

    cr.aperturas_automaticas(reconciliar_primero=True)
    assert llamadas["n"] == 1


# ── 8) Sin señales activas → 0 ───────────────────────────────────────────────

def test_sin_senales_devuelve_cero(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    stub_descarga_y_senales["signals"] = []
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == 0


# ── 9) Slot liberado por venta pendiente ─────────────────────────────────────

def test_pending_sell_libera_slot(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off, monkeypatch,
):
    """
    Con MAX_POS posiciones abiertas pero UNA con venta pendiente en Alpaca,
    debe haber 1 slot libre y la función abre 1 nueva posición.
    """
    # Llenar todos los slots
    with sqlite3.connect(str(db_connors_vacia)) as con:
        for i in range(config.CONNORS_MAX_POS):
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
                "VALUES (?, 'abierta', ?,?,?,?,?,?,?,?)",
                (f"TKR{i}", "2026-05-01", 100.0, 99.0, 10.0,
                 1000.0, 1000.0, 95.0, 18.0),
            )

    # TKR0 tiene una orden de venta pendiente → su slot se libera
    monkeypatch.setattr(cr, "_get_pending_sells", lambda: {"TKR0"})

    stub_descarga_y_senales["signals"] = [_signal("AMD")]
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == 1

    with sqlite3.connect(str(db_connors_vacia)) as con:
        tickers = {r[0] for r in con.execute("SELECT ticker FROM connors_operaciones WHERE estado='abierta'")}
    assert "AMD" in tickers


# ── 10) Devuelve el contrato esperado (int >= 0) ────────────────────────────

def test_retorna_int_no_negativo(
    db_connors_vacia, stub_descarga_y_senales, alpaca_off,
):
    """Sanidad de tipo y dominio: el retorno siempre es int en [0, MAX_POS]."""
    stub_descarga_y_senales["signals"] = [_signal("OVV")]
    n = cr.aperturas_automaticas(confirmar=False)
    assert isinstance(n, int)
    assert 0 <= n <= config.CONNORS_MAX_POS
