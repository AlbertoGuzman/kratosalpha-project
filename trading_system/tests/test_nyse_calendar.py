# -*- coding: utf-8 -*-
"""
Tests del calendario NYSE y su integración en ConnorsRSI.

Cubre:
  · es_dia_habil_nyse: día normal, festivo, sábado, martes post-festivo
  · aperturas_automaticas: devuelve 0 en festivo sin descargar nada
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import config
import modules.connors_rsi as cr
from modules.data import es_dia_habil_nyse

# ── Fechas conocidas ──────────────────────────────────────────────────────────
_LUNES_NORMAL    = date(2026, 6, 1)   # Lunes hábil
_MARTES_NORMAL   = date(2026, 6, 2)   # Martes tras lunes hábil
_MEMORIAL_DAY    = date(2026, 5, 25)  # Lunes festivo NYSE (Memorial Day)
_MARTES_POSTFEST = date(2026, 5, 26)  # Martes tras Memorial Day
_SABADO          = date(2026, 5, 30)  # Sábado


# ── es_dia_habil_nyse ─────────────────────────────────────────────────────────

def test_dia_habil_lunes_normal():
    assert es_dia_habil_nyse(_LUNES_NORMAL) is True

def test_festivo_memorial_day():
    assert es_dia_habil_nyse(_MEMORIAL_DAY) is False

def test_sabado_no_es_habil():
    assert es_dia_habil_nyse(_SABADO) is False

def test_martes_post_festivo_es_habil():
    assert es_dia_habil_nyse(_MARTES_POSTFEST) is True

def test_acepta_string():
    assert es_dia_habil_nyse("2026-06-01") is True

def test_fallback_devuelve_true(monkeypatch):
    """Si pandas_market_calendars no está disponible → fallback True."""
    import sys
    original = sys.modules.get("pandas_market_calendars")
    sys.modules["pandas_market_calendars"] = None  # type: ignore
    try:
        resultado = es_dia_habil_nyse(_LUNES_NORMAL)
        assert resultado is True
    finally:
        if original is None:
            sys.modules.pop("pandas_market_calendars", None)
        else:
            sys.modules["pandas_market_calendars"] = original


# ── aperturas_automaticas en festivo ─────────────────────────────────────────

def test_aperturas_automaticas_festivo_devuelve_cero(
    db_connors_vacia, monkeypatch,
):
    """En festivo NYSE, aperturas_automaticas devuelve 0 sin descargar nada."""
    monkeypatch.setattr(cr, "es_dia_habil_nyse", lambda d=None: False)
    # _download_universe no debe llamarse — si se llama lanzaría excepción
    monkeypatch.setattr(cr, "_download_universe",
                        lambda: (_ for _ in ()).throw(AssertionError("no debe llamarse")))
    n = cr.aperturas_automaticas(confirmar=False)
    assert n == 0
