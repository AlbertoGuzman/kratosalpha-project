# -*- coding: utf-8 -*-
"""
Tests del gate de calendario NYSE en el modo desatendido (--run all/connors)
y del aviso no-bloqueante del autopiloto manual (opción 1 del menú).

Cubrimos:
  · --run all en festivo → retorna 0, _opcion_autopiloto no se ejecuta.
  · --run connors en festivo → retorna 0, pasos ConnorsRSI no se ejecutan.
  · --run all en día hábil → _opcion_autopiloto sí se ejecuta.
  · autopiloto manual en festivo → muestra aviso pero no aborta.
"""

import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

# Asegura que trading_system/ esté en el path antes de importar main.
sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402  (importación después de sys.path)


# ── Fixture: mock de los pasos pesados del autopiloto ────────────────────────

@pytest.fixture()
def autopiloto_mockeado(monkeypatch):
    """
    Parchea todas las funciones pesadas que _opcion_autopiloto invoca para
    que los tests sean rápidos y no necesiten red, BD real ni Alpaca.

    Returns:
        dict con listas de llamadas registradas por cada mock.
    """
    import modules.backup as _backup
    import modules.connors_rsi as _cr

    llamadas: dict[str, list] = {
        "backup":       [],
        "check_pending": [],
        "cierres":      [],
        "aperturas":    [],
        "informe":      [],
    }

    monkeypatch.setattr(
        _backup, "run_backup",
        lambda env: (
            llamadas["backup"].append(env)
            or {"sqlite_path": None, "parquet_files": {}, "errores": []}
        ),
    )
    monkeypatch.setattr(
        _cr, "_check_pending_orders",
        lambda: llamadas["check_pending"].append(True) or {},
    )
    monkeypatch.setattr(
        main, "_autopiloto_cerrar_connors_automatico",
        lambda: llamadas["cierres"].append(True),
    )
    monkeypatch.setattr(
        _cr, "aperturas_automaticas",
        lambda **kw: (llamadas["aperturas"].append(kw) or 0),
    )
    monkeypatch.setattr(
        main, "_autopiloto_informe",
        lambda *a, **kw: llamadas["informe"].append(True),
    )
    return llamadas


# ── Tests del gate headless ───────────────────────────────────────────────────

class TestGateHeadlessFestivo:
    """--run all/connors en festivo → salida limpia sin ejecutar nada."""

    def test_run_all_festivo_retorna_0(self, monkeypatch):
        """Gate devuelve 0 y nunca llega a llamar al autopiloto."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)
        llamadas = []
        monkeypatch.setattr(
            main, "_opcion_autopiloto",
            lambda: llamadas.append("autopiloto"),
        )

        resultado = main._run_headless("all")

        assert resultado == 0
        assert llamadas == [], "El autopiloto no debe ejecutarse en festivo"

    def test_run_connors_festivo_retorna_0(self, monkeypatch):
        """Gate devuelve 0 y nunca llega al sync/cierres/aperturas ConnorsRSI."""
        import modules.connors_rsi as _cr

        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)
        llamadas = []
        monkeypatch.setattr(
            _cr, "_check_pending_orders",
            lambda: llamadas.append("sync"),
        )
        monkeypatch.setattr(
            main, "_autopiloto_cerrar_connors_automatico",
            lambda: llamadas.append("cierres"),
        )
        monkeypatch.setattr(
            _cr, "aperturas_automaticas",
            lambda **kw: llamadas.append("aperturas"),
        )

        resultado = main._run_headless("connors")

        assert resultado == 0
        assert llamadas == [], "Ningún paso ConnorsRSI debe ejecutarse en festivo"

    def test_run_all_festivo_no_invoca_autopiloto_informe(self, monkeypatch):
        """El informe (que contiene el envío a Telegram) no se alcanza en festivo."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)
        monkeypatch.setattr(main, "_opcion_autopiloto", lambda: None)  # no-op
        monkeypatch.setattr(main, "_autopiloto_informe", lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("_autopiloto_informe no debe llamarse en festivo")
        ))

        # No debe lanzar la AssertionError del mock porque el gate sale antes
        resultado = main._run_headless("all")
        assert resultado == 0


class TestGateHeadlessDiaHabil:
    """--run all en día hábil → el autopiloto sí se ejecuta."""

    def test_run_all_dia_habil_ejecuta_autopiloto(self, monkeypatch):
        """En día hábil, _opcion_autopiloto es llamado exactamente una vez."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: True)
        llamadas = []
        monkeypatch.setattr(
            main, "_opcion_autopiloto",
            lambda: llamadas.append("autopiloto"),
        )

        resultado = main._run_headless("all")

        assert resultado == 0
        assert llamadas == ["autopiloto"], "En día hábil el autopiloto debe ejecutarse"


# ── Tests del aviso y comportamiento manual en festivo ───────────────────────

class TestAutopilotoManualFestivo:
    """_opcion_autopiloto en festivo: muestra aviso y ejecuta con último cierre."""

    def test_autopiloto_festivo_imprime_aviso(self, monkeypatch, autopiloto_mockeado):
        """El aviso 'Mercado cerrado' aparece en la salida cuando es festivo."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)

        buf = io.StringIO()
        monkeypatch.setattr(main, "console", Console(file=buf, highlight=False))

        main._opcion_autopiloto()

        salida = buf.getvalue()
        assert "Mercado cerrado" in salida
        assert "no es día hábil NYSE" in salida

    def test_autopiloto_festivo_ejecuta_todos_los_pasos(self, monkeypatch, autopiloto_mockeado):
        """En modo manual (festivo), el autopiloto completa todos sus pasos."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)

        buf = io.StringIO()
        monkeypatch.setattr(main, "console", Console(file=buf, highlight=False))

        main._opcion_autopiloto()

        assert autopiloto_mockeado["backup"],  "El backup debe ejecutarse en modo manual"
        assert autopiloto_mockeado["informe"], "El informe debe generarse en modo manual"

    def test_autopiloto_festivo_aperturas_recibe_ignorar_festivo(self, monkeypatch):
        """aperturas_automaticas se llama con ignorar_festivo=True en festivo."""
        import modules.backup as _backup
        import modules.connors_rsi as _cr

        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: False)
        monkeypatch.setattr(_backup, "run_backup",
                            lambda env: {"sqlite_path": None, "parquet_files": {}, "errores": []})
        monkeypatch.setattr(main.cr, "_check_pending_orders", lambda: {})
        monkeypatch.setattr(main, "_autopiloto_cerrar_connors_automatico", lambda: None)
        monkeypatch.setattr(main, "_autopiloto_informe", lambda *a, **kw: None)

        kwargs_capturados: list[dict] = []
        monkeypatch.setattr(
            _cr, "aperturas_automaticas",
            lambda **kw: kwargs_capturados.append(kw) or 0,
        )

        buf = io.StringIO()
        monkeypatch.setattr(main, "console", Console(file=buf, highlight=False))

        main._opcion_autopiloto()

        assert kwargs_capturados, "aperturas_automaticas debe haber sido llamada"
        assert kwargs_capturados[0].get("ignorar_festivo") is True

    def test_autopiloto_dia_habil_sin_aviso(self, monkeypatch, autopiloto_mockeado):
        """En día hábil el panel de aviso no aparece."""
        monkeypatch.setattr(main, "es_dia_habil_nyse", lambda d: True)

        buf = io.StringIO()
        monkeypatch.setattr(main, "console", Console(file=buf, highlight=False))

        main._opcion_autopiloto()

        assert "Mercado cerrado" not in buf.getvalue()
