# -*- coding: utf-8 -*-
"""
Tests de modules/notifier.py.

Cubre:
  · build_daily_summary: apertura+cierre, solo cierres, sin actividad, con alertas
  · send_telegram: sin credenciales → True silencioso; error de red → False
"""

import copy
from datetime import date, datetime

import pytest

import modules.notifier as notifier


# ── Fixtures ─────────────────────────────────────────────────────────────────

_HOY = date.today().isoformat()

# Aislamos build_daily_summary de la BD real mockeando las helpers de consulta
@pytest.fixture(autouse=True)
def _bd_vacia(monkeypatch):
    """Por defecto, los helpers de BD devuelven listas vacías."""
    monkeypatch.setattr(notifier, "_trades_hoy",    lambda: [])
    monkeypatch.setattr(notifier, "_positions_hoy", lambda: [])


_STATS_BASE: dict = {
    "connors_sync":        {"filled": 0, "expired": 0, "canceled": 0, "pending": 0},
    "connors_cierres":     0,
    "connors_aperturas":   0,
    "errores":             [],
}

_CAP_CONNORS = {
    "capital_inicial": 10000.0,
    "capital_actual":  9850.0,
    "rentabilidad_pct": -1.5,
}

_START_TS = datetime(2026, 6, 3, 22, 31, 0)


def _call_summary(stats=None, cap_c=None, pnl=0.0,
                  entorno="pro", alpaca="PAPER"):
    return notifier.build_daily_summary(
        stats=stats or copy.deepcopy(_STATS_BASE),
        cap_connors=cap_c or dict(_CAP_CONNORS),
        pnl_connors_hoy=pnl,
        start_ts=_START_TS,
        entorno=entorno,
        alpaca_label=alpaca,
    )


# ── 1) Sin actividad ─────────────────────────────────────────────────────────

def test_sin_actividad_contiene_cabecera():
    msg = _call_summary()
    assert "kairos" in msg
    assert "CONNORS RSI" in msg


def test_sin_actividad_no_aperturas():
    msg = _call_summary()
    assert "Sin aperturas hoy" in msg
    assert "Sin cierres hoy" in msg


def test_sin_clenow_en_mensaje():
    # Tras retirar Clenow, su sección no debe aparecer.
    msg = _call_summary()
    assert "CLENOW" not in msg
    assert "TOTAL SISTEMA" not in msg


# ── 2) Con apertura ConnorsRSI ────────────────────────────────────────────────

def test_apertura_connors(monkeypatch):
    monkeypatch.setattr(notifier, "_positions_hoy", lambda: (
        [{"ticker": "AAPL", "entry_date": _HOY, "entry_price": 185.20,
          "shares": 12.0, "connors_rsi_entrada": 18.4}]
    ))
    msg = _call_summary()
    assert "AAPL" in msg
    assert "Sin aperturas hoy" not in msg


# ── 3) Con cierre ConnorsRSI ganador y perdedor ───────────────────────────────

def test_cierres_connors(monkeypatch):
    monkeypatch.setattr(notifier, "_trades_hoy", lambda: (
        [
            {"ticker": "DELL", "net_pnl": 146.0, "return_pct": 0.425, "reason": "CRSI_EXIT"},
            {"ticker": "INTC", "net_pnl": -38.0, "return_pct": -0.113, "reason": "SL"},
        ]
    ))
    msg = _call_summary(pnl=108.0)
    assert "DELL" in msg
    assert "INTC" in msg
    assert "CRSI_EXIT" in msg
    assert "SL" in msg
    assert "Sin cierres hoy" not in msg


# ── 4) Con alertas ────────────────────────────────────────────────────────────

def test_alertas_aparecen():
    stats = copy.deepcopy(_STATS_BASE)
    stats["errores"] = ["Mercado bajista — SPY < SMA200"]
    stats["connors_sync"]["expired"] = 1
    msg = _call_summary(stats=stats)
    assert "ALERTAS" in msg
    assert "Mercado bajista" in msg
    assert "expirada" in msg


def test_sin_alertas_no_seccion():
    msg = _call_summary()   # stats base tiene errores=[] y syncs=0
    assert "ALERTAS" not in msg


# ── 5) Capital None (robustez) ────────────────────────────────────────────────

def test_capital_none_no_explota():
    msg = notifier.build_daily_summary(
        stats=dict(_STATS_BASE),
        cap_connors=None,
        pnl_connors_hoy=0.0,
        start_ts=_START_TS,
        entorno="pro",
        alpaca_label="OFF",
    )
    assert "CONNORS RSI" in msg


# ── 6) send_telegram sin credenciales ────────────────────────────────────────

def test_send_telegram_sin_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    result = notifier.send_telegram("test")
    assert result is True


def test_send_telegram_error_red(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "fake:token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    from unittest.mock import patch
    with patch("requests.post", side_effect=Exception("timeout")):
        result = notifier.send_telegram("test")
    assert result is False
