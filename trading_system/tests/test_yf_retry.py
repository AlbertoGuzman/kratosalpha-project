# -*- coding: utf-8 -*-
"""
Tests de wait_for_last_close — sondeo del cierre de sesión (modo automático).

Enfoque: el objetivo se ancla a la FECHA DE LANZAMIENTO.
  · Primeros intentos → la fecha actual (la sesión que cierra hoy).
  · Si durante la ejecución cambia el día (cruce de medianoche) → el cierre del
    día anterior (= el día de lanzamiento).
  · Sondea cada YF_RETRY_WAIT_MIN minutos (30 por defecto).

Cubre:
  · DEV (ALPACA_ENABLED=False) → True sin esperar
  · SPY ya cubre la sesión objetivo (hoy) → True sin esperar
  · _ultima_sesion_esperada salta fines de semana / festivos
  · Primer reintento exitoso → True, sleep llamado 1 vez
  · Reintentos agotados → False, sleep llamado N veces
  · Cambio de día durante la ejecución → sigue el día de lanzamiento (no el nuevo hoy)
"""

from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

import config
import modules.yf_cache as yfc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _spy_df(last_date: date) -> pd.DataFrame:
    """DataFrame mínimo con SPY con una fila en last_date."""
    idx = pd.DatetimeIndex([pd.Timestamp(last_date)])
    return pd.DataFrame({"Close": [100.0]}, index=idx)


# ── 1) DEV → no reintenta ────────────────────────────────────────────────────

def test_dev_no_reintenta(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ENABLED", False)
    monkeypatch.setattr(yfc, "session_data", {"SPY": _spy_df(date.today() - timedelta(10))})
    with patch("time.sleep") as mock_sleep:
        result = yfc.wait_for_last_close()
    assert result is True
    mock_sleep.assert_not_called()


# ── 2) SPY ya cubre la sesión objetivo (hoy) → sin espera ────────────────────

def test_sesion_objetivo_disponible_sin_espera(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ENABLED", True)
    # Día de lanzamiento hábil → objetivo = hoy; SPY ya tiene hoy.
    monkeypatch.setattr(yfc, "session_data", {"SPY": _spy_df(date.today())})
    with patch("modules.yf_cache.es_dia_habil_nyse", return_value=True), \
         patch("time.sleep") as mock_sleep:
        result = yfc.wait_for_last_close()
    assert result is True
    mock_sleep.assert_not_called()


# ── 3) _ultima_sesion_esperada salta días no hábiles ─────────────────────────

def test_ultima_sesion_salta_no_habiles(monkeypatch):
    # Hoy = lunes 2026-06-08 → la última sesión esperada es el viernes 2026-06-05
    # (sábado y domingo no son hábiles).
    lunes = date(2026, 6, 8)

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return lunes

    habiles = {date(2026, 6, 5)}  # solo el viernes es hábil en la ventana

    monkeypatch.setattr(yfc, "date", _FakeDate)
    with patch("modules.yf_cache.es_dia_habil_nyse",
               side_effect=lambda d: d in habiles):
        assert yfc._ultima_sesion_esperada() == date(2026, 6, 5)


# ── 4) Primer reintento exitoso ───────────────────────────────────────────────

def test_reintento_exitoso(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ENABLED", True)
    monkeypatch.setattr(config, "YF_RETRY_ATTEMPTS", 8)
    monkeypatch.setattr(config, "YF_RETRY_WAIT_MIN", 1)
    # SPY en sesión tiene datos de ayer; objetivo = hoy.
    ayer = date.today() - timedelta(days=1)
    monkeypatch.setattr(yfc, "session_data", {"SPY": _spy_df(ayer)})

    # load_ticker devuelve datos de hoy en el primer reintento.
    spy_hoy = _spy_df(date.today())
    with patch("modules.yf_cache.es_dia_habil_nyse", return_value=True), \
         patch("modules.yf_cache.disk_cache_file", return_value=MagicMock(exists=lambda: False)), \
         patch("modules.yf_cache.load_ticker", return_value=spy_hoy), \
         patch("time.sleep") as mock_sleep:
        result = yfc.wait_for_last_close()

    assert result is True
    assert mock_sleep.call_count == 1


# ── 5) Reintentos agotados → False ────────────────────────────────────────────

def test_reintentos_agotados(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_ENABLED", True)
    monkeypatch.setattr(config, "YF_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(config, "YF_RETRY_WAIT_MIN", 1)
    ayer = date.today() - timedelta(days=1)
    monkeypatch.setattr(yfc, "session_data", {"SPY": _spy_df(ayer)})

    # load_ticker nunca alcanza la sesión objetivo (siempre datos de ayer).
    spy_ayer = _spy_df(ayer)
    with patch("modules.yf_cache.es_dia_habil_nyse", return_value=True), \
         patch("modules.yf_cache.disk_cache_file", return_value=MagicMock(exists=lambda: False)), \
         patch("modules.yf_cache.load_ticker", return_value=spy_ayer), \
         patch("time.sleep") as mock_sleep:
        result = yfc.wait_for_last_close()

    assert result is False
    assert mock_sleep.call_count == 3  # un sleep por intento


# ── 6) Cambio de día durante la ejecución ────────────────────────────────────

def test_cambio_de_dia_sigue_dia_lanzamiento(monkeypatch):
    """
    Lanzado el día D (hábil). Tras el primer sleep cruza la medianoche (hoy=D+1).
    El objetivo debe seguir siendo la sesión de D (el día anterior), y aceptar los
    datos de D — NO exigir la sesión del nuevo hoy (D+1, que aún no ha cerrado).
    """
    monkeypatch.setattr(config, "ALPACA_ENABLED", True)
    monkeypatch.setattr(config, "YF_RETRY_ATTEMPTS", 4)
    monkeypatch.setattr(config, "YF_RETRY_WAIT_MIN", 1)

    D  = date(2026, 6, 4)   # día de lanzamiento (hábil)
    D1 = date(2026, 6, 5)   # día siguiente

    class _Clock:
        dia = D

    class _FakeDate(date):
        @classmethod
        def today(cls):
            return _Clock.dia

    monkeypatch.setattr(yfc, "date", _FakeDate)
    # Arranca con SPY de hace dos días (anterior al objetivo D).
    monkeypatch.setattr(yfc, "session_data", {"SPY": _spy_df(date(2026, 6, 2))})

    def _cruza_medianoche(_segundos):
        _Clock.dia = D1   # el primer sleep nos lleva al día siguiente

    with patch("modules.yf_cache.es_dia_habil_nyse", return_value=True), \
         patch("modules.yf_cache.disk_cache_file", return_value=MagicMock(exists=lambda: False)), \
         patch("modules.yf_cache.load_ticker", return_value=_spy_df(D)), \
         patch("time.sleep", side_effect=_cruza_medianoche) as mock_sleep:
        result = yfc.wait_for_last_close()

    # Aceptó la sesión del día de lanzamiento (D) tras el cambio de día.
    assert result is True
    assert mock_sleep.call_count == 1
