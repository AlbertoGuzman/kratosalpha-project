# -*- coding: utf-8 -*-
"""
Tests de scripts/publish_logs.py.

Foco principal (restricción de seguridad): el RESUMEN.md publicado NO debe
contener datos sensibles — ni nombre de estrategia, ni tokens, ni claves API,
ni rutas del sistema. SÍ debe contener tickers, precios, fechas y PnL%.
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# scripts/ está fuera del paquete trading_system → añadir al path.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import publish_logs  # noqa: E402


# ── Datos de ejemplo ──────────────────────────────────────────────────────────

_OPS = [
    {"f_compra": "2026-05-01", "ticker": "AMD", "p_compra": 100.0,
     "f_venta": "2026-05-15", "p_venta": 110.0, "p_actual": None,
     "pnl_pct": 10.0, "estado": "Cerrada"},
    {"f_compra": "2026-05-20", "ticker": "MSFT", "p_compra": 200.0,
     "f_venta": None, "p_venta": None, "p_actual": 190.0,
     "pnl_pct": -5.0, "estado": "Abierta"},
    {"f_compra": "2026-04-10", "ticker": "CBOE", "p_compra": 287.75,
     "f_venta": "2026-05-02", "p_venta": 300.0, "p_actual": None,
     "pnl_pct": 4.26, "estado": "Cerrada"},
]


def _md():
    return publish_logs.build_resumen_md(_OPS, 10309.0, 10000.0, "2026-06-04 22:31h")


# ── Seguridad: no se filtran datos sensibles ──────────────────────────────────

class TestNoDatosSensibles:
    SENSIBLES = [
        "ConnorsRSI", "Clenow", "connors", "clenow",   # estrategia
        "ghp_", "GITHUB_TOKEN", "ALPACA", "secret", "SECRET",  # claves/tokens
        "C:\\", "/home/", "/Users/", "trading_system",  # rutas del sistema
        ".db", ".env", "sqlite",                        # ficheros internos
    ]

    def test_md_no_contiene_sensibles(self):
        md = _md()
        for token in self.SENSIBLES:
            assert token not in md, f"RESUMEN.md filtró dato sensible: {token!r}"

    def test_recopilar_no_filtra_estrategia(self, tmp_path):
        """El markdown generado desde la BD real no menciona la estrategia."""
        db = tmp_path / "ops.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones (
                ticker TEXT, estado TEXT, entry_date TEXT, entry_price REAL,
                exit_date TEXT, exit_price REAL);
            INSERT INTO connors_operaciones
                (ticker, estado, entry_date, entry_price, exit_date, exit_price) VALUES
                ('AAPL','cerrada','2026-01-01',150.0,'2026-02-01',165.0),
                ('NVDA','cerrada','2026-01-05',400.0,'2026-02-10',440.0),
                ('VTR','abierta','2026-05-20',79.33,NULL,NULL),
                ('MU','abierta','2026-05-22',820.46,NULL,NULL);
        """)
        con.commit()
        con.close()
        ops = publish_logs._recopilar_operaciones(db, {"VTR": 88.61, "MU": 1054.76})
        md = publish_logs.build_resumen_md(ops, None, None, "x")
        for token in ("ConnorsRSI", "Clenow", "connors", "clenow", "portfolio"):
            assert token not in md
        # pero sí los tickers
        for tk in ("AAPL", "NVDA", "VTR", "MU"):
            assert tk in md


# ── Contenido y formato ───────────────────────────────────────────────────────

class TestContenido:
    def test_cabecera_y_columnas(self):
        md = _md()
        assert "# kairos — Track Record público" in md
        assert "Actualizado: 2026-06-04 22:31h" in md
        assert "F. Compra | Ticker | P. Compra | F. Venta | P. Venta | P. Actual | PnL% | Estado" in md

    def test_cerrada_sin_precio_actual(self):
        md = _md()
        fila = [l for l in md.splitlines() if l.startswith("| 2026-05-01")][0]
        # F.Venta y P.Venta rellenos, P.Actual "-"
        assert "2026-05-15" in fila and "110.00" in fila
        assert "Cerrada" in fila
        # P.Actual es el hueco "-" entre P.Venta y PnL%
        assert "| - |" in fila

    def test_abierta_sin_fecha_venta(self):
        md = _md()
        fila = [l for l in md.splitlines() if "MSFT" in l][0]
        assert "Abierta" in fila
        assert "190.00" in fila          # P.Actual
        assert "-5.00%" in fila          # PnL%
        # F.Venta y P.Venta vacías
        assert fila.count("| - |") >= 1

    def test_orden_descendente_por_fecha(self):
        md = _md()
        filas = [l for l in md.splitlines() if l.startswith("| 2026")]
        fechas = [l.split("|")[1].strip() for l in filas]
        assert fechas == sorted(fechas, reverse=True)
        assert fechas[0] == "2026-05-20"   # la más reciente primero

    def test_pie_estadisticas(self):
        md = _md()
        # 2 cerradas (AMD +10%, CBOE +4.26%) → win rate 100%
        assert "Operaciones cerradas: 2" in md
        assert "Win rate: 100%" in md
        assert "Capital: 10,309€ (+3.09%)" in md

    def test_win_rate_con_perdedora(self):
        ops = [
            {"f_compra": "2026-01-01", "ticker": "A", "p_compra": 100.0,
             "f_venta": "2026-01-10", "p_venta": 110.0, "p_actual": None,
             "pnl_pct": 10.0, "estado": "Cerrada"},
            {"f_compra": "2026-01-02", "ticker": "B", "p_compra": 100.0,
             "f_venta": "2026-01-11", "p_venta": 90.0, "p_actual": None,
             "pnl_pct": -10.0, "estado": "Cerrada"},
        ]
        md = publish_logs.build_resumen_md(ops, None, None, "x")
        assert "Operaciones cerradas: 2" in md
        assert "Win rate: 50%" in md
        assert "Capital: -" in md

    def test_capital_none_muestra_guion(self):
        md = publish_logs.build_resumen_md(_OPS, None, None, "x")
        assert "Capital: -" in md


# ── Recopilación desde BD ─────────────────────────────────────────────────────

class TestRecopilar:
    def test_cerradas_y_abiertas(self, tmp_path):
        db = tmp_path / "r.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones (
                ticker TEXT, estado TEXT, entry_date TEXT, entry_price REAL,
                exit_date TEXT, exit_price REAL);
            INSERT INTO connors_operaciones
                (ticker, estado, entry_date, entry_price, exit_date, exit_price) VALUES
                ('AAPL','cerrada','2026-01-01',100.0,'2026-02-01',120.0),
                ('MSFT','abierta','2026-05-20',200.0,NULL,NULL);
        """)
        con.commit()
        con.close()
        ops = publish_logs._recopilar_operaciones(db, {"MSFT": 220.0})
        por_ticker = {o["ticker"]: o for o in ops}

        aapl = por_ticker["AAPL"]
        assert aapl["estado"] == "Cerrada"
        assert aapl["p_actual"] is None
        assert aapl["pnl_pct"] == pytest.approx(20.0)

        msft = por_ticker["MSFT"]
        assert msft["estado"] == "Abierta"
        assert msft["f_venta"] is None and msft["p_venta"] is None
        assert msft["p_actual"] == pytest.approx(220.0)
        assert msft["pnl_pct"] == pytest.approx(10.0)

    def test_tablas_inexistentes_no_crashea(self, tmp_path):
        db = tmp_path / "vacia.db"
        sqlite3.connect(str(db)).close()
        assert publish_logs._recopilar_operaciones(db, {}) == []

    def test_abierta_sin_precio_pnl_none(self, tmp_path):
        db = tmp_path / "np.db"
        con = sqlite3.connect(str(db))
        con.executescript("""
            CREATE TABLE connors_operaciones (
                ticker TEXT, estado TEXT, entry_date TEXT, entry_price REAL,
                exit_date TEXT, exit_price REAL);
            INSERT INTO connors_operaciones
                (ticker, estado, entry_date, entry_price) VALUES
                ('XYZ','abierta','2026-05-20',50.0);
        """)
        con.commit()
        con.close()
        ops = publish_logs._recopilar_operaciones(db, {})  # sin precio
        assert ops[0]["p_actual"] is None
        assert ops[0]["pnl_pct"] is None


# ── Publicación por entorno (pre/ vs pro/) ────────────────────────────────────

def _setup_git_publish(tmp_path, env, monkeypatch):
    """
    Prepara un repo_dir con .git simulado y mockea subprocess.run para no tocar
    red ni git real. Devuelve (ok, repo_dir, mock_run).
    """
    repo_dir = tmp_path / "public_logs"
    (repo_dir / ".git").mkdir(parents=True)   # simula clon ya existente → no clona
    run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(publish_logs.subprocess, "run", run)
    ok = publish_logs._git_publish(
        repo_dir, "ghp_secreto", "user/repo", "# md", MagicMock(), env,
    )
    return ok, repo_dir, run


class TestNormalizarRepo:
    def test_acepta_usuario_repo(self):
        assert publish_logs._normalizar_repo("user/repo") == "user/repo"

    def test_acepta_url_https_con_git(self):
        assert publish_logs._normalizar_repo(
            "https://github.com/AlbertoGuzman/kairos-track-record.git"
        ) == "AlbertoGuzman/kairos-track-record"

    def test_acepta_url_sin_git(self):
        assert publish_logs._normalizar_repo(
            "https://github.com/user/repo"
        ) == "user/repo"

    def test_acepta_ssh(self):
        assert publish_logs._normalizar_repo(
            "git@github.com:user/repo.git"
        ) == "user/repo"


class TestGitPublishPorEntorno:
    def test_escribe_en_carpeta_del_entorno(self, tmp_path, monkeypatch):
        ok, repo_dir, _ = _setup_git_publish(tmp_path, "pre", monkeypatch)
        assert ok is True
        # RESUMEN.md va a public_logs/pre/RESUMEN.md, no a la raíz
        assert (repo_dir / "pre" / "RESUMEN.md").exists()
        assert not (repo_dir / "RESUMEN.md").exists()

    def test_crea_carpeta_pro_independiente(self, tmp_path, monkeypatch):
        _, repo_dir, _ = _setup_git_publish(tmp_path, "pro", monkeypatch)
        assert (repo_dir / "pro" / "RESUMEN.md").exists()
        assert not (repo_dir / "pre").exists()

    def test_carpeta_dev(self, tmp_path, monkeypatch):
        ok, repo_dir, _ = _setup_git_publish(tmp_path, "dev", monkeypatch)
        assert ok is True
        assert (repo_dir / "dev" / "RESUMEN.md").exists()

    def test_git_add_usa_ruta_del_entorno(self, tmp_path, monkeypatch):
        _, _, run = _setup_git_publish(tmp_path, "pro", monkeypatch)
        adds = [c.args[0] for c in run.call_args_list
                if c.args and "add" in c.args[0]]
        assert any("pro/RESUMEN.md" in args for args in adds)

    def test_genera_fichero_diario_con_fecha(self, tmp_path, monkeypatch):
        from datetime import datetime
        _, repo_dir, run = _setup_git_publish(tmp_path, "pre", monkeypatch)
        fecha = datetime.now().strftime("%Y-%m-%d")
        # Existen ambos: snapshot rodante y fichero del día.
        assert (repo_dir / "pre" / "RESUMEN.md").exists()
        assert (repo_dir / "pre" / f"{fecha}.md").exists()
        # Mismo contenido en ambos.
        assert (repo_dir / "pre" / "RESUMEN.md").read_text(encoding="utf-8") == \
               (repo_dir / "pre" / f"{fecha}.md").read_text(encoding="utf-8")
        # git add incluye el fichero diario.
        adds = [c.args[0] for c in run.call_args_list
                if c.args and "add" in c.args[0]]
        assert any(f"pre/{fecha}.md" in args for args in adds)

    def test_push_usa_token_pero_no_se_loguea(self, tmp_path, monkeypatch):
        _, _, run = _setup_git_publish(tmp_path, "pre", monkeypatch)
        pushes = [c.args[0] for c in run.call_args_list
                  if c.args and "push" in c.args[0]]
        assert pushes, "debe haber hecho push"
        assert any("ghp_secreto@github.com" in " ".join(a) for a in pushes)
