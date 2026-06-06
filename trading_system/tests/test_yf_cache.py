# -*- coding: utf-8 -*-
"""Tests para modules/yf_cache.py — API pública de caché yfinance."""

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from modules import yf_cache


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def limpia_sesion():
    """Cada test arranca con la sesión limpia y la deja limpia al salir."""
    yf_cache.clear_session()
    yield
    yf_cache.clear_session()


def _df_sintetico(n: int = 10) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 100},
        index=idx,
    )


# ── API de sesión ─────────────────────────────────────────────────────────────

class TestSesionAPI:
    def test_sesion_empieza_vacia(self):
        assert yf_cache.session_data == {}

    def test_get_session_devuelve_none_si_no_hay(self):
        assert yf_cache.get_session("AAPL") is None

    def test_set_y_get_session(self):
        df = _df_sintetico()
        yf_cache.set_session("AAPL", df)
        resultado = yf_cache.get_session("AAPL")
        assert resultado is df

    def test_clear_session_vacia_el_cache(self):
        yf_cache.set_session("AAPL", _df_sintetico())
        yf_cache.set_session("SPY", _df_sintetico())
        yf_cache.clear_session()
        assert yf_cache.session_data == {}

    def test_clear_preserva_referencia_al_dict(self):
        ref = yf_cache.session_data
        yf_cache.set_session("X", _df_sintetico())
        yf_cache.clear_session()
        assert yf_cache.session_data is ref  # misma identidad, no reemplazado

    def test_varios_tickers_independientes(self):
        df_a = _df_sintetico(5)
        df_b = _df_sintetico(8)
        yf_cache.set_session("AAPL", df_a)
        yf_cache.set_session("MSFT", df_b)
        assert len(yf_cache.session_data) == 2
        assert yf_cache.get_session("AAPL") is df_a
        assert yf_cache.get_session("MSFT") is df_b


# ── Constantes ────────────────────────────────────────────────────────────────

class TestConstantes:
    def test_min_cache_rows_es_200(self):
        assert yf_cache.MIN_CACHE_ROWS == 200

    def test_disk_fmt_es_parquet_o_csv(self):
        assert yf_cache.DISK_FMT in ("parquet", "csv")


# ── Caché en disco ────────────────────────────────────────────────────────────

class TestCacheEnDisco:
    def test_save_y_load_parquet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yf_cache, "DISK_FMT", "parquet")
        df = _df_sintetico()
        path = tmp_path / "AAPL.parquet"
        yf_cache.save_to_disk(df, path)
        assert path.exists()
        recuperado = yf_cache.load_from_disk(path)
        assert list(recuperado.columns) == list(df.columns)
        assert len(recuperado) == len(df)

    def test_save_y_load_csv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yf_cache, "DISK_FMT", "csv")
        df = _df_sintetico()
        path = tmp_path / "AAPL.csv"
        yf_cache.save_to_disk(df, path)
        assert path.exists()
        recuperado = yf_cache.load_from_disk(path)
        assert "Close" in recuperado.columns

    def test_safe_ticker_filename_reemplaza_caracteres(self):
        assert yf_cache.safe_ticker_filename("BRK/B") == "BRK_B"
        assert yf_cache.safe_ticker_filename("A:B") == "A_B"
        assert yf_cache.safe_ticker_filename("AAPL") == "AAPL"

    def test_disk_cache_file_usa_hoy(self):
        from datetime import date
        hoy = date.today().strftime("%Y%m%d")
        p = yf_cache.disk_cache_file("AAPL")
        assert hoy in str(p)
        assert "AAPL" in str(p)

    def test_cleanup_no_falla_si_no_existe_directorio(self, monkeypatch, tmp_path):
        monkeypatch.setattr(yf_cache, "_DISK_CACHE_ROOT", tmp_path / "no_existe")
        yf_cache.cleanup_old_caches()  # no debe lanzar excepción


# ── yf_download_one (unit, sin red) ──────────────────────────────────────────

class TestYfDownloadOne:
    def test_devuelve_none_si_yfinance_lanza_excepcion(self):
        with patch("yfinance.download", side_effect=RuntimeError("red caída")):
            resultado = yf_cache.yf_download_one("AAPL", "2024-01-01", "2024-01-10")
        assert resultado is None

    def test_devuelve_none_si_df_vacio(self):
        df_vacio = pd.DataFrame()
        with patch("yfinance.download", return_value=df_vacio):
            resultado = yf_cache.yf_download_one("AAPL", "2024-01-01", "2024-01-10")
        assert resultado is None

    def test_columnas_ohlcv_presentes(self):
        df_mock = _df_sintetico()
        with patch("yfinance.download", return_value=df_mock):
            resultado = yf_cache.yf_download_one("AAPL", "2024-01-01", "2024-01-10")
        assert resultado is not None
        assert set(resultado.columns) == {"Open", "High", "Low", "Close", "Volume"}
