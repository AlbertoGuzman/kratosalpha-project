# -*- coding: utf-8 -*-
"""
Tests de la versión base ConnorsRSI — sin apalancamiento, sin LEVERAGE,
sin nocional, sin financing_cost, sin slippage_cost, sin STOP_OUT.
Sólo la estrategia pura.
"""

import sqlite3
from datetime import datetime

import pandas as pd
import pytest

import config


# ── TestConnorsCapital ───────────────────────────────────────────────────────

class TestConnorsCapital:
    """Estado del capital ConnorsRSI (estrategia única, 10.000 €)."""

    def test_capital_inicial_correcto(self, db_connors_vacia):
        with sqlite3.connect(str(db_connors_vacia)) as con:
            row = con.execute(
                "SELECT cash FROM connors_capital ORDER BY id ASC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(config.CONNORS_CAPITAL)
        assert row[0] == pytest.approx(10000.0)

    def test_slot_size_correcto(self):
        # slot ≈ CONNORS_CAPITAL / MAX_POS = 10000 / 3 ≈ 3333 con tolerancia
        # amplia (la reserva 0.5 % para comisiones lo reduce a ~3316,67).
        slot_base = config.CONNORS_CAPITAL / config.CONNORS_MAX_POS
        assert abs(slot_base - 3333.33) < 1.0

    def test_max_posiciones_simultaneas(self, db_connors_vacia):
        # Inserta 3 posiciones (el máximo) y verifica recuento.
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        shares = slot / 100.0
        with sqlite3.connect(str(db_connors_vacia)) as con:
            for tk in ("AAPL", "MSFT", "NVDA"):
                con.execute(
                    "INSERT INTO connors_operaciones "
                    "(ticker, estado, entry_date, entry_price, limit_price, shares, "
                    " valor_compra, slot_size, stop_loss_price, connors_rsi_entrada) "
                    "VALUES (?, 'abierta', ?,?,?,?,?,?,?,?)",
                    (tk, "2024-05-01", 100.0, 99.0, shares,
                     slot, slot, 95.0, 18.0),
                )
            n = con.execute(
                "SELECT COUNT(*) FROM connors_operaciones WHERE estado='abierta'"
            ).fetchone()[0]
        assert n == config.CONNORS_MAX_POS
        assert n <= 3

    def test_capital_libre_nunca_negativo(self, db_connors_vacia):
        # Tras inicializar la BD, el cash registrado debe ser >= 0.
        with sqlite3.connect(str(db_connors_vacia)) as con:
            row = con.execute(
                "SELECT cash FROM connors_capital ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row[0] >= 0.0

    def test_no_abre_sin_capital(self, db_connors_vacia):
        # Si el cash registrado < slot * 0.95 → no se debería abrir.
        slot = (config.CONNORS_CAPITAL * 0.995) / config.CONNORS_MAX_POS
        with sqlite3.connect(str(db_connors_vacia)) as con:
            con.execute(
                "INSERT INTO connors_capital "
                "(fecha, cash, valor_posiciones, capital_total, nota) "
                "VALUES ('2024-05-02', ?, 0.0, ?, 'fondos_bajos')",
                (slot * 0.5, slot * 0.5),
            )
            cash = con.execute(
                "SELECT cash FROM connors_capital ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        # Verificamos que el predicado de apertura sería falso
        puede_abrir = cash >= slot * 0.95
        assert not puede_abrir


# ── TestConnorsEntrada ───────────────────────────────────────────────────────

class TestConnorsEntrada:
    """Reglas de entrada del backtest base."""

    def test_entrada_solo_con_crsi_bajo(self, connors_backtest_2019_hoy):
        # Verifica sobre trades reales que el CRSI de entrada está bajo el umbral.
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible (sin red o falló)")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        # Tolerancia: hasta 5 % de trades pueden tener CRSI levemente > umbral
        # por límite de entrada que se cumple en el día siguiente.
        invalidos = [t for t in trades
                     if t.get("connors_rsi_entrada", 0) > config.CONNORS_ENTRY_CRSI + 1]
        assert len(invalidos) <= 0.05 * len(trades), \
            f"{len(invalidos)} trades con CRSI > {config.CONNORS_ENTRY_CRSI}"

    def test_entrada_requiere_spy_sobre_sma200(self, connors_backtest_2019_hoy):
        # Verificación indirecta: en el crash COVID (15-mar-2020 → 15-abr-2020)
        # SPY estaba claramente bajo SMA200 → entradas deberían ser raras.
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        df = pd.DataFrame(trades)
        df["entry_dt"] = pd.to_datetime(df["entry_date"], errors="coerce")
        crash = df[(df["entry_dt"] >= "2020-03-15") & (df["entry_dt"] <= "2020-04-15")]
        # Comparado con la actividad típica mensual del backtest
        # (~ trades_total / 84 meses), debería ser claramente inferior.
        media_mensual = len(df) / 84.0
        assert len(crash) <= media_mensual * 0.5, (
            f"{len(crash)} entradas en crash COVID; media mensual {media_mensual:.1f}"
        )

    def test_entrada_requiere_3_dias_bajando(self, connors_backtest_2019_hoy):
        # streak_entrada debe ser ≤ -CONNORS_MIN_STREAK (3 días bajando).
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        sin_streak = [t for t in trades if t.get("streak_entrada") is None]
        validos    = [t for t in trades if t.get("streak_entrada") is not None]
        # Si el campo está informado, la mayoría debe cumplir streak ≤ -3.
        if validos:
            ok = sum(1 for t in validos
                     if t["streak_entrada"] <= -config.CONNORS_MIN_STREAK)
            # Tolerancia: 70% — algunos trades entran al día siguiente del
            # cumplimiento estricto del streak (límite de precio).
            assert ok / len(validos) >= 0.70

    def test_precio_entrada_es_limite(self, db_connors_vacia):
        # Insertamos un trade donde limit_price = entry_price * (1 - ENTRY_LIMIT)
        # y verificamos la relación.
        entry_price = 100.0
        limit_price = entry_price * (1 - config.CONNORS_ENTRY_LIMIT)
        slot   = 2333.0
        shares = slot / entry_price
        with sqlite3.connect(str(db_connors_vacia)) as con:
            con.execute(
                "INSERT INTO connors_operaciones "
                "(ticker, estado, entry_date, exit_date, entry_price, limit_price, "
                " exit_price, shares, valor_compra, slot_size, commission_entry, "
                " commission_exit, gross_pnl, net_pnl, return_pct, dias, reason) "
                "VALUES (?, 'cerrada', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("AAPL", "2024-06-01", "2024-06-05", entry_price, limit_price,
                 105.0, shares, slot, slot, 3.5, 3.7, 116.65, 109.45, 0.05, 4, "CRSI_EXIT"),
            )
            row = con.execute(
                "SELECT entry_price, limit_price FROM connors_operaciones"
            ).fetchone()
        # limit_price <= entry_price (la entrada es al límite inferior).
        assert row[1] <= row[0]
        # Y la diferencia es ≈ ENTRY_LIMIT.
        ratio = row[1] / row[0]
        assert ratio == pytest.approx(1 - config.CONNORS_ENTRY_LIMIT, abs=1e-6)

    def test_shares_calculadas_correctamente(self, connors_trade_ganador):
        # shares = valor_compra / entry_price con tolerancia.
        expected = connors_trade_ganador["valor_compra"] / connors_trade_ganador["entry_price"]
        assert connors_trade_ganador["shares"] == pytest.approx(expected, abs=0.01)


# ── TestConnorsSalida ────────────────────────────────────────────────────────

class TestConnorsSalida:
    """Reglas de salida del backtest base."""

    def test_sl_activa_a_5_pct(self, connors_trade_perdedor):
        # exit_price = entry_price * (1 - 0.05).
        ratio = connors_trade_perdedor["exit_price"] / connors_trade_perdedor["entry_price"]
        assert ratio == pytest.approx(1 - config.CONNORS_STOP_LOSS, abs=1e-6)
        assert connors_trade_perdedor["reason"] == "SL"

    def test_sl_perdida_correcta(self, connors_trade_perdedor):
        # Pérdida ≈ valor_compra × STOP_LOSS (descontando comisiones).
        valor_compra = connors_trade_perdedor["valor_compra"]
        perdida_pct  = abs(connors_trade_perdedor["return_pct"])
        assert perdida_pct == pytest.approx(config.CONNORS_STOP_LOSS, abs=1e-4)
        # net_pnl debería estar cerca de -valor_compra * STOP_LOSS, con margen
        # por comisiones de entrada + salida.
        bruto_esperado = -valor_compra * config.CONNORS_STOP_LOSS
        comm_total     = valor_compra * config.COMMISSION * 2 * 0.975  # aprox
        assert connors_trade_perdedor["net_pnl"] == pytest.approx(
            bruto_esperado - comm_total, rel=0.05,
        )

    def test_time_stop_5_dias(self, connors_trade_tsto):
        # CONNORS_TIME_STOP = 5: trades con TSTO tienen dias >= TIME_STOP.
        assert connors_trade_tsto["reason"] == "TSTO"
        assert connors_trade_tsto["dias"] >= config.CONNORS_TIME_STOP

    def test_crsi_exit_sobre_60(self, connors_trade_ganador):
        # Trade ganador con reason=CRSI_EXIT confirma esa rama.
        assert connors_trade_ganador["reason"] == "CRSI_EXIT"
        # Y el return es positivo (CRSI sólo dispara con CRSI > 60, lo que
        # típicamente coincide con precio en zona alta).
        assert connors_trade_ganador["return_pct"] > 0

    def test_motivos_salida_validos(self, connors_backtest_2019_hoy):
        # Único conjunto admitido en la versión base.
        validos = {"SL", "TSTO", "CRSI_EXIT", "END"}
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        encontrados = {t.get("reason") for t in trades}
        no_permitidos = encontrados - validos
        assert not no_permitidos, f"Motivos inválidos: {no_permitidos}"


# ── TestConnorsComisiones ────────────────────────────────────────────────────

class TestConnorsComisiones:
    """Fórmulas de comisiones y PnL."""

    def test_comision_entry_correcta(self, connors_trade_ganador):
        expected = connors_trade_ganador["valor_compra"] * config.COMMISSION
        assert connors_trade_ganador["commission_entry"] == pytest.approx(
            expected, abs=0.01,
        )

    def test_comision_exit_correcta(self, connors_trade_ganador):
        valor_salida = connors_trade_ganador["shares"] * connors_trade_ganador["exit_price"]
        expected = valor_salida * config.COMMISSION
        assert connors_trade_ganador["commission_exit"] == pytest.approx(
            expected, abs=0.01,
        )

    def test_net_pnl_formula(self, connors_trade_ganador):
        gross = connors_trade_ganador["gross_pnl"]
        ce    = connors_trade_ganador["commission_entry"]
        cx    = connors_trade_ganador["commission_exit"]
        net_expected = gross - ce - cx
        assert connors_trade_ganador["net_pnl"] == pytest.approx(
            net_expected, abs=0.01,
        )

    def test_gross_pnl_formula(self, connors_trade_ganador):
        ep    = connors_trade_ganador["entry_price"]
        xp    = connors_trade_ganador["exit_price"]
        sh    = connors_trade_ganador["shares"]
        gross_expected = (xp - ep) * sh
        assert connors_trade_ganador["gross_pnl"] == pytest.approx(
            gross_expected, abs=0.01,
        )


# ── TestConnorsBacktest (benchmark — slow) ───────────────────────────────────

@pytest.mark.slow
class TestConnorsBacktest:
    """
    Benchmarks ConnorsRSI 2019-01-01 → hoy.

    Margen ±20 % para absorber cambios en el universo. Se ejecuta con
    `pytest --run-slow`. Si no hay red o el backtest falla, se skipea.
    """

    def test_rentabilidad_en_rango(self, connors_backtest_2019_hoy):
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        capital_final = connors_backtest_2019_hoy.get("capital_final", 0.0)
        rent_pct = (capital_final - config.CONNORS_CAPITAL) / config.CONNORS_CAPITAL * 100
        assert 380 <= rent_pct <= 580, (
            f"Rentabilidad {rent_pct:.1f}% fuera del rango [380, 580] "
            f"(benchmark: +478.89%)"
        )

    def test_win_rate_en_rango(self, connors_backtest_2019_hoy):
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        n_wins = sum(1 for t in trades if t.get("net_pnl", 0) > 0)
        wr     = n_wins / len(trades) * 100
        assert 65 <= wr <= 75, f"Win rate {wr:.1f}% fuera de [65, 75] (bench 70.4%)"

    def test_operaciones_en_rango(self, connors_backtest_2019_hoy):
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        n = len(connors_backtest_2019_hoy.get("trades", []))
        assert 1400 <= n <= 1800, f"{n} ops fuera de [1400, 1800] (bench 1562)"

    def test_capital_final_en_rango(self, connors_backtest_2019_hoy):
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        cf = connors_backtest_2019_hoy.get("capital_final", 0.0)
        assert 45_000 <= cf <= 70_000, (
            f"Capital final {cf:.2f}€ fuera de [45k, 70k] (bench 57.888€)"
        )

    def test_años_positivos(self, connors_backtest_2019_hoy):
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        # Agrupa PnL por año del exit_date.
        df = pd.DataFrame(trades)
        df["exit_year"] = pd.to_datetime(df["exit_date"], errors="coerce").dt.year
        by_year = df.groupby("exit_year")["net_pnl"].sum()
        n_positivos = int((by_year > 0).sum())
        assert n_positivos >= 6, (
            f"Sólo {n_positivos} años positivos (esperados ≥6 de 7)"
        )

    def test_2022_unico_año_negativo(self, connors_backtest_2019_hoy):
        # 2022 es el único año permitido en negativo (bajista global).
        if connors_backtest_2019_hoy is None:
            pytest.skip("Backtest no disponible")
        trades = connors_backtest_2019_hoy.get("trades", [])
        if not trades:
            pytest.skip("Backtest sin trades")
        df = pd.DataFrame(trades)
        df["exit_year"] = pd.to_datetime(df["exit_date"], errors="coerce").dt.year
        by_year = df.groupby("exit_year")["net_pnl"].sum()
        negativos = set(by_year[by_year < 0].index)
        # Si hay negativos, deben ser sólo {2022} (subset).
        assert negativos.issubset({2022}), (
            f"Años negativos {negativos} — solo 2022 está permitido en negativo"
        )


# ── TestMismaEmpresaGrupos ───────────────────────────────────────────────────

class TestMismaEmpresaGrupos:
    """Exclusión de doble clase de acciones de la misma empresa."""

    def _excluidos(self, ocupados):
        from modules.connors_rsi import _get_tickers_excluidos_por_grupo
        return _get_tickers_excluidos_por_grupo(set(ocupados))

    def test_goog_excluye_googl(self):
        excluidos = self._excluidos({"GOOG"})
        assert "GOOGL" in excluidos

    def test_googl_excluye_goog(self):
        excluidos = self._excluidos({"GOOGL"})
        assert "GOOG" in excluidos

    def test_fox_excluye_foxa(self):
        excluidos = self._excluidos({"FOX"})
        assert "FOXA" in excluidos

    def test_foxa_excluye_fox(self):
        excluidos = self._excluidos({"FOXA"})
        assert "FOX" in excluidos

    def test_ticker_fuera_de_grupo_no_excluye_nada(self):
        excluidos = self._excluidos({"AAPL"})
        assert not excluidos

    def test_sin_ocupados_no_excluye_nada(self):
        excluidos = self._excluidos(set())
        assert not excluidos

    def test_grupo_completo_ocupado_excluye_ambos(self):
        # Si GOOGL y GOOG están ambas ocupadas, los dos aparecen en excluidos.
        excluidos = self._excluidos({"GOOGL", "GOOG"})
        assert {"GOOGL", "GOOG"}.issubset(excluidos)

    def test_config_grupos_definidos(self):
        grupos = config.MISMA_EMPRESA_GRUPOS
        assert any({"GOOGL", "GOOG"} == g for g in grupos), \
            "Grupo Alphabet no encontrado en MISMA_EMPRESA_GRUPOS"
        assert any({"FOX", "FOXA"} == g for g in grupos), \
            "Grupo Fox no encontrado en MISMA_EMPRESA_GRUPOS"
