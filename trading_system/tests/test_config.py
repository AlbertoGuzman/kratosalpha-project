# -*- coding: utf-8 -*-
"""Tests del módulo config.py — validación de parámetros centralizados."""

from pathlib import Path

import config


class TestUniverse:
    """El universo de tickers vive en un CSV externo (`UNIVERSE_FILE`)."""

    def test_universe_file_definido(self):
        assert hasattr(config, "UNIVERSE_FILE"), "config.UNIVERSE_FILE no existe"
        assert isinstance(config.UNIVERSE_FILE, str) and config.UNIVERSE_FILE.strip() != ""

    def test_universe_file_apunta_a_csv_existente(self):
        path = Path(__file__).parent.parent / config.UNIVERSE_FILE
        assert path.exists(), f"Universo no encontrado: {path}"
        assert path.suffix.lower() == ".csv"

    def test_tickers_ya_no_existe(self):
        # El antiguo dict TICKERS (IBEX/SP500) ha sido eliminado.
        assert not hasattr(config, "TICKERS"), (
            "config.TICKERS debería haberse eliminado; el universo se gestiona "
            "ahora desde el CSV externo via load_universe()."
        )


class TestSimulacion:
    def test_capital_juego_positivo(self):
        assert config.CAPITAL_JUEGO > 0

    def test_commission_fraccion_pequeña(self):
        assert 0 < config.COMMISSION < 0.05


class TestClenowEliminado:
    """Clenow se retiró: ni sus parámetros ni el Parameter Sweep deben existir."""

    def test_sin_parametros_clenow(self):
        for attr in (
            "CLENOW_TOP_N", "CLENOW_LOOKBACK", "CLENOW_MIN_R2",
            "CLENOW_REBAL_DAY", "CLENOW_SWEEP_PARAMS", "INITIAL_CAPITAL",
            "POSITION_SIZE_FIXED",
        ):
            assert not hasattr(config, attr), f"config.{attr} debería haberse eliminado"

    def test_sin_sweep_connors(self):
        assert not hasattr(config, "CONNORS_SWEEP_PARAMS"), (
            "El Parameter Sweep se retiró; CONNORS_SWEEP_PARAMS no debe existir."
        )


class TestConnors:
    def test_capital_positivo(self):
        assert config.CONNORS_CAPITAL > 0

    def test_entry_crsi_menor_que_exit(self):
        assert config.CONNORS_ENTRY_CRSI < config.CONNORS_EXIT_CRSI

    def test_stop_loss_fraccion_valida(self):
        assert 0 < config.CONNORS_STOP_LOSS < 1

    def test_max_pos_positivo(self):
        assert config.CONNORS_MAX_POS > 0
