#!/usr/bin/env python3
"""
notificar.py
------------
Envía el push de "datos actualizados" SOLO cuando la API ya sirve la fecha
recién cargada. Se ejecuta tras el commit del pipeline: Render tarda unos
minutos en redesplegar con la BD nueva y avisar antes haría que la app
mostrase datos antiguos.

La fecha se toma de la tabla interinos_YYYYMMDD más reciente de la BD local.

Uso:
    python notificar.py [--db Base_Bolsa_Docente.db] [--api URL] [--timeout 1200]
"""

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime

import requests

from push_notifications import notificar_actualizacion

API_URL = "https://api-bolsa-docente-clm.onrender.com"
TITULO = "Bolsa Docente CLM actualizada"
CUERPO = "Se han publicado los nuevos datos de adjudicaciones en la app, entra y consulta tu posición !"


def ultima_fecha_local(db_path: str) -> str:
    with sqlite3.connect(db_path) as conn:
        tablas = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'interinos_[0-9]*'"
        )]
    fechas = []
    for t in tablas:
        try:
            fechas.append(datetime.strptime(t.split("_", 1)[1], "%Y%m%d"))
        except ValueError:
            pass
    return max(fechas).strftime("%Y-%m-%d")


def api_sirve_fecha(api: str, fecha: str) -> bool:
    try:
        r = requests.get(f"{api}/fechas_disponibles", timeout=30)
        return r.ok and fecha in r.json()
    except Exception:
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [notificar] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="Base_Bolsa_Docente.db")
    ap.add_argument("--api", default=API_URL)
    ap.add_argument("--timeout", type=int, default=1200, help="segundos máximos de espera")
    args = ap.parse_args()

    fecha = ultima_fecha_local(args.db)
    print(f"Esperando a que la API sirva la fecha {fecha}...")
    limite = time.time() + args.timeout
    while not api_sirve_fecha(args.api, fecha):
        if time.time() > limite:
            print(f"✗ La API no sirve {fecha} tras {args.timeout}s: no se envía la notificación.")
            return 1
        time.sleep(20)

    print("✓ La API ya sirve los datos nuevos. Enviando notificación push...")
    return 0 if notificar_actualizacion(TITULO, CUERPO) else 1


if __name__ == "__main__":
    sys.exit(main())
