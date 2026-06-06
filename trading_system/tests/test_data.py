# -*- coding: utf-8 -*-
"""Tests del módulo data.py — descarga y caché de datos de mercado."""

import pandas as pd
import numpy as np
import pytest
from unittest.mock import patch


def _fake_ohlcv(n: int = 100) -> pd.DataFrame:
    """Genera un DataFrame OHLCV plano con n filas."""
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    close = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame(
        {
            "Open":   close - 0.5,
            "High":   close + 1.0,
            "Low":    close - 1.0,
            "Close":  close,
            "Volume": np.full(n, 1_000_000.0),
        },
        index=dates,
    )


class TestGetDataBasico:
    def test_devuelve_dict(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv()):
            from modules.data import get_data
            result = get_data(["AAPL"], use_cache=False)
        assert isinstance(result, dict)

    def test_ticker_presente_en_resultado(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv()):
            from modules.data import get_data
            result = get_data(["AAPL"], use_cache=False)
        assert "AAPL" in result

    def test_columnas_ohlcv_presentes(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv()):
            from modules.data import get_data
            result = get_data(["AAPL"], use_cache=False)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            assert col in result["AAPL"].columns

    def test_multiples_tickers(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv()):
            from modules.data import get_data
            result = get_data(["AAPL", "MSFT"], use_cache=False)
        assert "AAPL" in result
        assert "MSFT" in result


class TestFiltradoDatos:
    def test_omite_ticker_sin_datos(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=pd.DataFrame()):
            from modules.data import get_data
            result = get_data(["FAKE"], use_cache=False)
        assert "FAKE" not in result

    def test_omite_ticker_con_menos_de_60_dias(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv(n=30)):
            from modules.data import get_data
            result = get_data(["SMALL"], use_cache=False)
        assert "SMALL" not in result

    def test_acepta_exactamente_60_dias(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", return_value=_fake_ohlcv(n=60)):
            from modules.data import get_data
            result = get_data(["OK60"], use_cache=False)
        assert "OK60" in result


class TestManejoErrores:
    def test_continua_si_un_ticker_falla(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        call_count = {"n": 0}

        def fake_download(ticker, **kwargs):
            call_count["n"] += 1
            if ticker == "ERR":
                raise ConnectionError("Simulated network error")
            return _fake_ohlcv()

        with patch("yfinance.download", side_effect=fake_download):
            from modules.data import get_data
            result = get_data(["ERR", "AAPL"], use_cache=False)

        assert "ERR" not in result
        assert "AAPL" in result

    def test_devuelve_dict_vacio_si_todos_fallan(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        with patch("yfinance.download", side_effect=RuntimeError("fail")):
            from modules.data import get_data
            result = get_data(["ERR1", "ERR2"], use_cache=False)
        assert result == {}


class TestMultiIndex:
    def test_aplana_multiindex_columnas(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        df = _fake_ohlcv()
        # Simula el MultiIndex que devuelve yfinance >=0.2 para un ticker
        df.columns = pd.MultiIndex.from_tuples([(c, "AAPL") for c in df.columns])
        with patch("yfinance.download", return_value=df):
            from modules.data import get_data
            result = get_data(["AAPL"], use_cache=False)
        assert "AAPL" in result
        assert "Close" in result["AAPL"].columns
        assert not isinstance(result["AAPL"].columns, pd.MultiIndex)


class TestCache:
    def test_segunda_llamada_usa_cache(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        call_count = {"n": 0}

        def counting_download(*args, **kwargs):
            call_count["n"] += 1
            return _fake_ohlcv()

        with patch("yfinance.download", side_effect=counting_download):
            from modules.data import get_data
            get_data(["AAPL"], use_cache=True)
            get_data(["AAPL"], use_cache=True)

        assert call_count["n"] == 1  # solo una descarga real

    def test_use_cache_false_siempre_descarga(self, tmp_path, monkeypatch):
        import modules.data as dm
        monkeypatch.setattr(dm, "_CACHE_DIR", tmp_path)
        call_count = {"n": 0}

        def counting_download(*args, **kwargs):
            call_count["n"] += 1
            return _fake_ohlcv()

        with patch("yfinance.download", side_effect=counting_download):
            from modules.data import get_data
            get_data(["AAPL"], use_cache=False)
            get_data(["AAPL"], use_cache=False)

        assert call_count["n"] == 2
