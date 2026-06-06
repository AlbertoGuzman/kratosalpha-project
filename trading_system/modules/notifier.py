# -*- coding: utf-8 -*-
"""
Módulo de notificaciones Telegram para el autopiloto.

Envía UN mensaje diario al finalizar `_opcion_autopiloto()` con el resumen
completo de la ejecución. Solo activo en entorno PRO.

API pública:
    send_telegram(mensaje)          → bool
    build_daily_summary(...)        → str
"""

import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config
from modules.logger import get_system_logger

_slog = get_system_logger()

_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


# ── Envío ────────────────────────────────────────────────────────────────────

def send_telegram(mensaje: str) -> bool:
    """
    Envía un mensaje a Telegram via Bot API (MarkdownV2 desactivado — texto plano).

    Si TELEGRAM_TOKEN o TELEGRAM_CHAT_ID están vacíos, retorna True silencioso.
    Nunca lanza excepción — cualquier fallo se registra en log y retorna False.

    Params:
        mensaje: texto a enviar (UTF-8, máx ~4096 chars Telegram).
    Returns:
        True si enviado correctamente o si las credenciales no están configuradas.
        False si hubo error de red o respuesta no-OK de Telegram.
    """
    token   = os.environ.get("TELEGRAM_TOKEN", _TOKEN)
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", _CHAT_ID)

    if not token or not chat_id:
        _slog.debug("Telegram: credenciales no configuradas — notificación omitida")
        return True

    try:
        import requests
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": mensaje},
            timeout=10,
        )
        if resp.ok:
            _slog.info("Telegram: mensaje enviado correctamente")
            return True
        _slog.warning("Telegram: respuesta no-OK %s — %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        _slog.warning("Telegram: error al enviar — %s", exc)
        return False


# ── Consultas SQLite (read-only) ──────────────────────────────────────────────

def _trades_hoy() -> list[dict]:
    """Operaciones ConnorsRSI cerradas hoy (estado='cerrada', exit_date == hoy)."""
    hoy = date.today().isoformat()
    try:
        with sqlite3.connect(str(config.DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM connors_operaciones "
                "WHERE estado = 'cerrada' AND exit_date = ?", (hoy,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _positions_hoy() -> list[dict]:
    """Posiciones ConnorsRSI abiertas hoy (estado='abierta', entry_date == hoy)."""
    hoy = date.today().isoformat()
    try:
        with sqlite3.connect(str(config.DB_PATH)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM connors_operaciones "
                "WHERE estado = 'abierta' AND entry_date = ?", (hoy,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── Construcción del mensaje ──────────────────────────────────────────────────

def build_daily_summary(
    stats: dict,
    cap_connors: dict | None,
    pnl_connors_hoy: float,
    start_ts: datetime,
    entorno: str,
    alpaca_label: str,
    pnl_real: dict | None = None,
) -> str:
    """
    Construye el mensaje de resumen diario para Telegram.

    Consulta la BD para obtener el detalle de aperturas y cierres del día.
    El resultado es texto plano legible en móvil (sin tablas Rich).

    Params:
        stats:            dict de estadísticas del autopiloto.
        cap_connors:      dict de get_capital_summary_connors() o None.
        pnl_connors_hoy:  PnL realizado ConnorsRSI en el día.
        start_ts:         datetime de inicio del autopiloto.
        entorno:          "dev" | "pre" | "pro".
        alpaca_label:     "PAPER" | "REAL" | "OFF".
        pnl_real:         dict de alpaca_broker.get_pnl_real() (capital_inicial,
                          equity_actual, pnl_usd, pnl_pct) o None → muestra N/A.
    Returns:
        Mensaje formateado como string UTF-8.
    """
    sep   = "━" * 21   # ━━━━━━━━━━━━━━━━━━━━━
    lines = []

    # Cabecera
    lines.append(f"\U0001f4ca kairos · {start_ts:%d/%m/%Y · %H:%Mh}")
    lines.append(f"Entorno: {entorno.upper()} · Alpaca: {alpaca_label}")
    lines.append("")

    # ── ConnorsRSI ──────────────────────────────────────────────────────────
    lines.append(sep)
    lines.append("\U0001f504 CONNORS RSI")
    lines.append(sep)

    cierres_c  = _trades_hoy()
    aperturas_c = _positions_hoy()

    if aperturas_c:
        lines.append("Aperturas:")
        for p in aperturas_c:
            shares = p.get("shares", 0)
            precio = p.get("entry_price", 0)
            crsi   = p.get("connors_rsi_entrada") or 0
            lines.append(f"  \U0001f4c8 {p['ticker']:<6} · {shares:.1f} acc · ${precio:.2f} · CRSI={crsi:.1f}")
    else:
        lines.append("Aperturas: Sin aperturas hoy")

    lines.append("")

    if cierres_c:
        lines.append("Cierres:")
        for t in cierres_c:
            pnl    = t.get("net_pnl", 0)
            ret    = t.get("return_pct", 0) * 100
            reason = t.get("reason", "")
            icono  = "✅" if pnl >= 0 else "❌"
            lines.append(
                f"  {icono} {t['ticker']:<6} · {reason:<10} · "
                f"{'+' if pnl >= 0 else ''}{pnl:.0f}€ ({ret:+.1f}%)"
            )
    else:
        lines.append("Cierres: Sin cierres hoy")

    lines.append("")
    cap_c_act  = cap_connors["capital_actual"]  if cap_connors else 0.0
    cap_c_pct  = cap_connors["rentabilidad_pct"] if cap_connors else 0.0
    lines.append(
        f"Capital: {cap_c_act:,.0f}€ · "
        f"PnL dia: {'+' if pnl_connors_hoy >= 0 else ''}{pnl_connors_hoy:.0f}€ · "
        f"Total: {cap_c_pct:+.1f}%"
    )

    # ── Capital total real (Alpaca) ──────────────────────────────────────────
    lines.append("")
    lines.append(sep)
    lines.append("\U0001f4b0 CAPITAL TOTAL (Alpaca)")
    lines.append(sep)
    if pnl_real:
        signo = "+" if pnl_real["pnl_usd"] >= 0 else "-"
        lines.append(f"Equity actual  : ${pnl_real['equity_actual']:,.0f}")
        lines.append(f"Capital inicial: ${pnl_real['capital_inicial']:,.0f}")
        lines.append(
            f"PnL total      :   {signo}${abs(pnl_real['pnl_usd']):,.0f}  "
            f"({pnl_real['pnl_pct']:+.2f}%)"
        )
    else:
        lines.append("Equity actual  : N/A")
        lines.append("Capital inicial: N/A")
        lines.append("PnL total      : N/A")

    # ── Alertas ──────────────────────────────────────────────────────────────
    alertas: list[str] = list(stats.get("errores", []))

    sync_c = stats.get("connors_sync", {})
    if sync_c.get("expired", 0) + sync_c.get("canceled", 0) > 0:
        n = sync_c["expired"] + sync_c["canceled"]
        alertas.append(f"{n} orden(es) Connors expirada(s)/cancelada(s)")

    if alertas:
        lines.append("")
        lines.append(sep)
        lines.append("⚠️ ALERTAS")
        lines.append(sep)
        for a in alertas:
            lines.append(f"· {a}")

    return "\n".join(lines)
