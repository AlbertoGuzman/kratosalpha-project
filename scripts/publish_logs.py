# -*- coding: utf-8 -*-
"""
Publica el track record de kairos en un repo público de GitHub.

Tras cada autopiloto (en DEV, PRE y PRO) se llama a `publish_track_record()`, que:
  1. Lee las operaciones cerradas (`*_trades`) y abiertas (`*_portfolio`) de la
     BD, valorando las abiertas con el precio actual de Alpaca.
  2. Construye `public_logs/<env>/RESUMEN.md` (snapshot rodante) y además un
     fichero diario `public_logs/<env>/<YYYY-MM-DD>.md` con el mismo contenido
     (histórico). Markdown legible, sin datos sensibles. Cada entorno escribe en
     su propia carpeta (`dev/`, `pre/`, `pro/`).
  3. git add + commit + push al repo público (vía subprocess).

NO se publica nunca: estrategia (qué tabla originó cada operación), claves API,
tokens ni rutas del sistema. SÍ se publica: tickers, precios, fechas y PnL%.

Configuración (.env):
    GITHUB_ENABLED=true
    GITHUB_TOKEN=ghp_xxxxxxxxxxxx
    GITHUB_REPO=usuario/kairos-track-record
"""

import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Permite `import config` y `from modules...` desde scripts/ (fuera del paquete).
_TS_DIR = Path(__file__).resolve().parent.parent / "trading_system"
if str(_TS_DIR) not in sys.path:
    sys.path.insert(0, str(_TS_DIR))

import config  # noqa: E402
from modules.logger import get_system_logger  # noqa: E402

_REPO_DIR = Path(__file__).resolve().parent.parent / "public_logs"


# ── Construcción del markdown (función pura, sin acceso a red/BD) ─────────────

def _fmt_precio(x) -> str:
    """Precio con 2 decimales, '-' si es None."""
    return f"{float(x):,.2f}" if x is not None else "-"


def _fmt_fecha(d) -> str:
    """Fecha tal cual (ya viene 'YYYY-MM-DD'), '-' si es None/vacía."""
    return str(d) if d else "-"


def _fmt_pct(x) -> str:
    """PnL% con signo, '-' si es None."""
    return f"{float(x):+.2f}%" if x is not None else "-"


def build_resumen_md(
    operaciones: list,
    capital_actual: float | None,
    capital_inicial: float | None,
    ahora: str,
) -> str:
    """
    Genera el contenido de RESUMEN.md a partir de las operaciones.

    Params:
        operaciones:     lista de dicts con f_compra, ticker, p_compra, f_venta,
                         p_venta, p_actual, pnl_pct, estado.
        capital_actual:  capital total actual (€) o None.
        capital_inicial: capital total inicial (€) o None.
        ahora:           timestamp de actualización ya formateado.
    Returns:
        str con el markdown completo. No incluye estrategias, claves ni rutas.
    """
    # Orden por fecha de compra descendente (ISO → orden lexicográfico = cronológico).
    ops = sorted(operaciones, key=lambda o: o.get("f_compra") or "", reverse=True)

    lines = [
        "# kairos — Track Record público",
        "",
        f"Actualizado: {ahora}",
        "",
        "| F. Compra | Ticker | P. Compra | F. Venta | P. Venta | P. Actual | PnL% | Estado |",
        "|-----------|--------|-----------|----------|----------|-----------|------|--------|",
    ]
    for o in ops:
        lines.append(
            f"| {_fmt_fecha(o.get('f_compra'))} "
            f"| {o.get('ticker', '-')} "
            f"| {_fmt_precio(o.get('p_compra'))} "
            f"| {_fmt_fecha(o.get('f_venta'))} "
            f"| {_fmt_precio(o.get('p_venta'))} "
            f"| {_fmt_precio(o.get('p_actual'))} "
            f"| {_fmt_pct(o.get('pnl_pct'))} "
            f"| {o.get('estado', '-')} |"
        )

    cerradas = [o for o in ops if o.get("estado") == "Cerrada"]
    n_cerradas = len(cerradas)
    n_win = sum(1 for o in cerradas if (o.get("pnl_pct") or 0) > 0)
    win_rate = (n_win / n_cerradas * 100) if n_cerradas else 0.0

    if capital_actual is not None and capital_inicial:
        pct = (capital_actual - capital_inicial) / capital_inicial * 100
        cap_txt = f"Capital: {capital_actual:,.0f}€ ({pct:+.2f}%)"
    else:
        cap_txt = "Capital: -"

    lines.append("")
    lines.append(
        f"Operaciones cerradas: {n_cerradas} · "
        f"Win rate: {win_rate:.0f}% · {cap_txt}"
    )
    lines.append("")
    return "\n".join(lines)


# ── Recopilación de operaciones desde la BD ───────────────────────────────────

def _recopilar_operaciones(db_path, precios: dict) -> list:
    """
    Lee operaciones cerradas (*_trades) y abiertas (*_portfolio) de la BD.

    Params:
        db_path: ruta a la BD SQLite.
        precios: {ticker: precio_actual} de Alpaca para valorar las abiertas.
    Returns:
        lista de dicts normalizados (sin etiqueta de estrategia).
    """
    ops: list = []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        for estado_filtro in ("cerrada",):
            try:
                rows = con.execute(
                    "SELECT ticker, entry_date, entry_price, exit_date, exit_price "
                    "FROM connors_operaciones "
                    "WHERE estado = ? AND COALESCE(exit_date, '') <> ''",
                    (estado_filtro,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                ep = float(r["entry_price"]) if r["entry_price"] else None
                xp = float(r["exit_price"]) if r["exit_price"] is not None else None
                pnl = ((xp - ep) / ep * 100) if (xp is not None and ep) else None
                ops.append({
                    "f_compra": str(r["entry_date"])[:10],
                    "ticker":   r["ticker"],
                    "p_compra": ep,
                    "f_venta":  str(r["exit_date"])[:10] if r["exit_date"] else None,
                    "p_venta":  xp,
                    "p_actual": None,
                    "pnl_pct":  pnl,
                    "estado":   "Cerrada",
                })

        for estado_filtro in ("abierta",):
            try:
                rows = con.execute(
                    "SELECT ticker, entry_date, entry_price FROM connors_operaciones "
                    "WHERE estado = ?",
                    (estado_filtro,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for r in rows:
                ep = float(r["entry_price"]) if r["entry_price"] else None
                pa = precios.get(r["ticker"])
                pa = float(pa) if pa is not None else None
                pnl = ((pa - ep) / ep * 100) if (pa is not None and ep) else None
                ops.append({
                    "f_compra": str(r["entry_date"])[:10],
                    "ticker":   r["ticker"],
                    "p_compra": ep,
                    "f_venta":  None,
                    "p_venta":  None,
                    "p_actual": pa,
                    "pnl_pct":  pnl,
                    "estado":   "Abierta",
                })
    finally:
        con.close()
    return ops


# ── Publicación git (subprocess) ──────────────────────────────────────────────

def _normalizar_repo(repo: str) -> str:
    """
    Normaliza `GITHUB_REPO` a la forma 'usuario/repo'.

    Acepta tanto 'usuario/repo' como una URL completa
    ('https://github.com/usuario/repo.git', 'git@github.com:usuario/repo.git').
    """
    r = repo.strip()
    if r.endswith(".git"):
        r = r[:-4]
    for prefijo in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if r.startswith(prefijo):
            r = r[len(prefijo):]
            break
    return r.strip("/")


def _git_publish(
    repo_dir: Path, token: str, repo: str, md_content: str, _log, env: str,
) -> bool:
    """
    Clona (si hace falta), **sincroniza con el remoto**, escribe
    `<env>/RESUMEN.md` + el fichero diario `<env>/<fecha>.md` y hace push.

    Cada entorno publica en su propia carpeta dentro del repo (`dev/`, `pre/`,
    `pro/`), que se crea si no existe.

    **Conservación del histórico**: antes de escribir se trae la rama remota y se
    fusiona en local (`merge -X ours --allow-unrelated-histories`). Los ficheros
    diarios tienen nombre único por fecha → nunca colisionan, así que la fusión
    los conserva todos (los del remoto + los locales); solo `RESUMEN.md` podría
    chocar y, como se regenera justo después, se resuelve a "ours". Esto evita el
    rechazo `fetch first` y garantiza que ningún fichero por fecha se pierda.

    El token solo se usa en la URL de clone/fetch/push y nunca se persiste como
    remoto ni se vuelca en logs (se ofusca con `_scrub`). Devuelve True si se
    publicó (o no había nada nuevo) y False ante cualquier fallo, sin propagar.
    """
    auth_url  = f"https://{token}@github.com/{repo}.git"
    clean_url = f"https://github.com/{repo}.git"
    rel_path  = f"{env}/RESUMEN.md"   # ruta relativa dentro del repo (git usa '/')

    def _scrub(s: str) -> str:
        return (s or "").replace(token, "***") if token else (s or "")

    def _run(args, check: bool = True):
        return subprocess.run(
            ["git", *args], cwd=str(repo_dir) if (repo_dir / ".git").exists() else None,
            check=check, capture_output=True, text=True,
        )

    try:
        if not (repo_dir / ".git").exists():
            repo_dir.parent.mkdir(parents=True, exist_ok=True)
            # Clon completo (no shallow): un clon shallow rompe fetch/merge y
            # provoca rechazos de push al diverger del remoto.
            subprocess.run(
                ["git", "clone", auth_url, str(repo_dir)],
                check=True, capture_output=True, text=True,
            )
            # No persistir el token: remoto sin credenciales (fetch/push usan auth_url).
            _run(["remote", "set-url", "origin", clean_url])

        # Identidad local del commit (idempotente).
        _run(["config", "user.email", "bot@kairos.local"])
        _run(["config", "user.name", "kairos-bot"])

        # ── Sincronizar con el remoto ANTES de escribir ──────────────────────
        # Trae la rama remota y fúsionala conservando el histórico de ambos
        # lados. Tolerante: si el remoto está vacío / no se puede traer, se
        # continúa (el push posterior creará o avanzará `main`).
        fetch = _run(["fetch", auth_url, "main"], check=False)
        if fetch.returncode == 0:
            merge = _run(
                ["merge", "--no-edit", "-X", "ours",
                 "--allow-unrelated-histories", "FETCH_HEAD"],
                check=False,
            )
            if merge.returncode != 0:
                _run(["merge", "--abort"], check=False)
                _log.warning(
                    "publish_logs: fusión con el remoto no limpia — se continúa "
                    "sin fusionar (%s)", _scrub(merge.stdout + merge.stderr)[:120],
                )

        # Carpeta del entorno (dev/ pre/ pro/), creada si no existe.
        dest_dir = repo_dir / env
        dest_dir.mkdir(parents=True, exist_ok=True)
        # RESUMEN.md = snapshot rodante (siempre el último).
        (dest_dir / "RESUMEN.md").write_text(md_content, encoding="utf-8")
        # Fichero diario con la fecha en el nombre = histórico verificable.
        # Re-ejecutar el mismo día sobrescribe el fichero de ese día (mismo nombre).
        fecha   = datetime.now().strftime("%Y-%m-%d")
        rel_dia = f"{env}/{fecha}.md"
        (dest_dir / f"{fecha}.md").write_text(md_content, encoding="utf-8")
        _run(["add", rel_path, rel_dia])

        commit = _run(
            ["commit", "-m",
             f"Actualiza track record {env} {datetime.now():%Y-%m-%d %H:%M}"],
            check=False,
        )
        blob = commit.stdout + commit.stderr
        if commit.returncode != 0 and "nothing to commit" not in blob:
            _log.warning("publish_logs: commit falló — %s", _scrub(blob)[:200])
            return False

        # Push SIEMPRE: sube el commit nuevo y/o el merge / commits locales
        # pendientes de subir (aunque hoy no hubiera cambios que commitear).
        push = _run(["push", auth_url, "HEAD:main"], check=False)
        if push.returncode != 0:
            _log.warning(
                "publish_logs: push falló — %s",
                _scrub(push.stdout + push.stderr)[:200],
            )
            return False

        _log.info("publish_logs: track record %s publicado en %s", env, repo)
        return True

    except subprocess.CalledProcessError as exc:
        salida = _scrub((exc.stderr or "") + (exc.stdout or "")) or str(type(exc).__name__)
        _log.warning("publish_logs: git falló — %s", salida[:200])
        return False
    except Exception as exc:  # noqa: BLE001 — nunca debe tumbar el autopiloto
        _log.warning("publish_logs: error inesperado — %s", _scrub(str(exc))[:200])
        return False


# ── Orquestador ───────────────────────────────────────────────────────────────

def publish_track_record(db_path=None, broker=None) -> bool:
    """
    Publica el track record en la carpeta del entorno (`dev/`, `pre/` o `pro/`)
    del repo. Gated: solo DEV/PRE/PRO y con GITHUB_ENABLED + credenciales. Nunca
    lanza excepción — devuelve False si se omite o falla.

    Params:
        db_path: ruta BD (default config.DB_PATH).
        broker:  AlpacaBroker opcional para precios actuales (default singleton).
    Returns:
        True si publicó (o no había cambios); False si se omitió o falló.
    """
    _log = get_system_logger()

    if config.TRADING_ENV not in ("dev", "pre", "pro"):
        _log.debug("publish_logs: omitido (entorno %s no es dev/pre/pro)", config.TRADING_ENV)
        return False
    if not getattr(config, "GITHUB_ENABLED", False):
        _log.debug("publish_logs: GITHUB_ENABLED=false — omitido")
        return False

    token = getattr(config, "GITHUB_TOKEN", "")
    repo  = _normalizar_repo(getattr(config, "GITHUB_REPO", ""))
    if not token or not repo:
        _log.warning("publish_logs: faltan GITHUB_TOKEN/GITHUB_REPO — omitido")
        return False

    db_path = db_path or config.DB_PATH

    # Precios actuales de Alpaca para valorar las posiciones abiertas.
    # Respeta ALPACA_ENABLED: en dev (Alpaca off) NO se conecta — las abiertas
    # quedan con P.Actual "-", igual que el resto del sistema en modo SQLite.
    precios: dict = {}
    try:
        if broker is None and getattr(config, "ALPACA_ENABLED", False):
            from modules.alpaca_broker import get_broker
            broker = get_broker()
        if broker is not None:
            precios = {p["ticker"]: p["current_price"] for p in broker.get_positions()}
    except Exception as exc:
        _log.warning("publish_logs: sin precios Alpaca (%s) — abiertas sin P.Actual", exc)

    operaciones = _recopilar_operaciones(db_path, precios)

    # Capital (sin desglosar por estrategia en el markdown).
    capital_actual = capital_inicial = None
    try:
        from modules.connors_rsi import get_capital_summary_connors
        cn = get_capital_summary_connors()
        capital_actual  = cn["capital_actual"]
        capital_inicial = cn["capital_inicial"]
    except Exception as exc:
        _log.warning("publish_logs: capital no disponible (%s)", exc)

    ahora = datetime.now().strftime("%Y-%m-%d %H:%Mh")
    md = build_resumen_md(operaciones, capital_actual, capital_inicial, ahora)

    return _git_publish(_REPO_DIR, token, repo, md, _log, config.TRADING_ENV)


if __name__ == "__main__":
    publish_track_record()
