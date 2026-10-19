# -*- coding: utf-8 -*-
"""
reset_db.py — Borra todos los datos operativos de trading_book.db y deja
la BD en estado inicial: sin operaciones ni movimientos de capital, con el
registro 'inicio' que fija el capital de arranque.

Uso:
    C:\trading_env\Scripts\python scripts\reset_db.py --env dev
    C:\trading_env\Scripts\python scripts\reset_db.py --env pre
    C:\trading_env\Scripts\python scripts\reset_db.py --env pro

Siempre pide confirmación antes de borrar. Hace backup automático de la BD
antes de cualquier modificación.

ADVERTENCIA: esta operación es irreversible salvo que tengas el backup.
"""
import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "trading_system"))

import os
parser = argparse.ArgumentParser(description="Reiniciar la BD de trading")
parser.add_argument("--env", required=True, choices=["dev", "pre", "pro"],
                    help="Entorno cuya BD se va a reiniciar")
parser.add_argument("--yes", action="store_true",
                    help="Salta la confirmación interactiva (uso en scripts)")
args = parser.parse_args()

os.environ["TRADING_ENV"] = args.env
import config  # noqa: E402 — debe importarse después de fijar TRADING_ENV

DB_PATH     = config.DB_PATH
BACKUP_DIR  = REPO_ROOT / "trading_system" / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_PATH = BACKUP_DIR / f"trading_book_{args.env}_RESET_BACKUP_{date.today().isoformat()}.db"


def main() -> None:
    print(f"\n{'='*60}")
    print(f"  REINICIO DE BD — entorno: {args.env.upper()}")
    print(f"  BD:     {DB_PATH}")
    print(f"  Capital inicial: {config.CONNORS_CAPITAL:,.0f} €")
    print(f"{'='*60}\n")

    if not DB_PATH.exists():
        print("La BD no existe. No hay nada que borrar.")
        print("Al lanzar la app se creará automáticamente con el estado inicial.")
        sys.exit(0)

    # ── Confirmación ──────────────────────────────────────────────────────────
    if not args.yes:
        print("ATENCIÓN: esta operación borrará TODAS las operaciones y")
        print("movimientos de capital. La BD quedará como si nunca se hubiera")
        print("operado.\n")
        resp = input(f"  Escriba 'RESET {args.env.upper()}' para confirmar: ").strip()
        if resp != f"RESET {args.env.upper()}":
            print("\nOperación cancelada.")
            sys.exit(0)

    # ── Backup previo ─────────────────────────────────────────────────────────
    shutil.copy2(DB_PATH, BACKUP_PATH)
    print(f"\n✓ Backup guardado en: {BACKUP_PATH}")

    # ── Reset ─────────────────────────────────────────────────────────────────
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")

    with con:
        # Borrar en orden para respetar FK: primero capital, luego operaciones
        n_capital   = con.execute("SELECT COUNT(*) FROM connors_capital").fetchone()[0]
        n_ops       = con.execute("SELECT COUNT(*) FROM connors_operaciones").fetchone()[0]

        con.execute("DELETE FROM connors_capital")
        con.execute("DELETE FROM connors_operaciones")

        # Reiniciar secuencias de autoincrement
        con.execute("DELETE FROM sqlite_sequence WHERE name IN ('connors_capital','connors_operaciones')")

        # Registro inicial de capital
        con.execute(
            "INSERT INTO connors_capital "
            "(fecha, cash, valor_posiciones, capital_total, nota) "
            "VALUES (?, ?, 0.0, ?, 'inicio')",
            (date.today().isoformat(), config.CONNORS_CAPITAL, config.CONNORS_CAPITAL),
        )

    con.execute("VACUUM")
    con.close()

    print(f"✓ Eliminadas {n_ops} operaciones y {n_capital} registros de capital")
    print(f"✓ Capital inicial restaurado: {config.CONNORS_CAPITAL:,.0f} €")
    print(f"\nBD reiniciada correctamente. Entorno '{args.env}' listo para operar.\n")


if __name__ == "__main__":
    main()
