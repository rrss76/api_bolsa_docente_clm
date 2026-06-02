#!/usr/bin/env python3
"""
run_pipeline.py
---------------
Script independiente que ejecuta el scraper + parsers + cargador completo.
Se lanza como proceso separado desde main.py para evitar el timeout
de Render cuando el proceso es largo.

Uso:
    python run_pipeline.py            # ejecución normal
    python run_pipeline.py --force    # fuerza re-descarga de PDFs ya procesados
"""

import sys
import re
import csv
import json
import logging
import argparse
import tempfile
import importlib
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pipeline] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("pipeline")

# Parsers y cargador
parser_disp = importlib.import_module("2_Parser_Disponibles_auto")
parser_adj  = importlib.import_module("3_Parser_Adjudicaciones_auto")
cargador    = importlib.import_module("4_Cargador_Semanal")

from scraper import (
    obtener_adjudicaciones_portada,
    extraer_pdfs_pagina,
    cargar_estado,
    guardar_estado,
    descargar_pdf_bytes,
)

def descargar_a_tmp(url: str) -> tuple | None:
    """
    Descarga un PDF a un fichero temporal en /tmp.
    Devuelve (ruta_tmp, nombre) o None si falla.
    """
    from urllib.parse import unquote
    resultado = descargar_pdf_bytes(url)
    if not resultado:
        return None
    pdf_bytes, nombre = resultado
    # Escribir en /tmp y devolver la ruta
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf", delete=False,
        dir="/tmp", prefix=nombre.replace(" ", "_") + "_"
    )
    tmp.write(pdf_bytes)
    tmp.close()
    del pdf_bytes  # liberar RAM inmediatamente
    return tmp.name, nombre

import os
import tempfile
DB_BOLSA_PATH = os.getenv("DB_BOLSA_PATH", "Base_Bolsa_Docente.db")

CAMPOS_DISP = [
    "fecha", "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
    "orden", "dni", "apellidos_nombre", "tipo_bolsa", "orden_bolsa",
    "provincias", "ingles", "frances", "aleman", "italiano",
]
CAMPOS_ADJ = [
    "fecha_publicacion", "fecha_inicio_periodo", "fecha_fin_periodo",
    "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
    "cod_centro", "nombre_centro", "localidad", "dni", "apellidos_nombre",
    "titular", "bolsa", "posicion", "tipo_jornada", "fecha_inicio", "fecha_fin",
]


def main(force: bool = False):
    log.info("=" * 50)
    log.info(f"Pipeline CLM — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info("=" * 50)

    estado = cargar_estado()
    adjudicaciones = obtener_adjudicaciones_portada()

    registros_disp = []
    registros_adj  = []
    hay_novedades  = False
    fecha_raw      = ""

    for adj in adjudicaciones:
        pdfs_por_seccion = extraer_pdfs_pagina(adj["url"])

        for seccion, pdfs in pdfs_por_seccion.items():
            for pdf in pdfs:
                clave_pdf = pdf["url"]
                if clave_pdf in estado["pdfs_descargados"] and not force:
                    continue

                descargado = descargar_a_tmp(pdf["url"])
                if not descargado:
                    continue

                ruta_tmp, nombre = descargado
                hay_novedades = True

                if clave_pdf not in estado["pdfs_descargados"]:
                    estado["pdfs_descargados"].append(clave_pdf)

                if not fecha_raw:
                    m = re.search(r'(\d{8})', nombre.replace(' ', ''))
                    if m:
                        s = m.group(1)
                        fecha_raw = f"{s[6:8]}/{s[4:6]}/{s[0:4]}"

                log.info(f"  ✓ {nombre}")

                try:
                    if seccion == "disponibles":
                        nuevos = parser_disp.parse_pdf(ruta_tmp)
                        registros_disp.extend(nuevos)
                        log.info(f"    → {len(nuevos)} registros extraídos")
                    elif seccion == "adjudicados":
                        nuevos = parser_adj.parse_pdf(ruta_tmp)
                        registros_adj.extend(nuevos)
                        log.info(f"    → {len(nuevos)} registros extraídos")
                except Exception as e:
                    log.error(f"  ✗ Error parseando {nombre}: {e}")
                finally:
                    # Borrar fichero temporal inmediatamente tras parsear
                    Path(ruta_tmp).unlink(missing_ok=True)

    guardar_estado(estado)

    if not hay_novedades:
        log.info("✓ Sin novedades esta ejecución.")
        return 0

    log.info(f"  → {len(registros_disp)} disponibles | {len(registros_adj)} adjudicaciones")

    if not fecha_raw:
        log.error("✗ No se pudo determinar la fecha.")
        return 1

    # Rellenar fecha donde falte
    for r in registros_disp:
        if not r.get("fecha"):
            r["fecha"] = fecha_raw
    for r in registros_adj:
        if not r.get("fecha_publicacion"):
            r["fecha_publicacion"] = fecha_raw

    # Guardar CSVs temporales
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.csv', delete=False, encoding='utf-8', newline=''
    ) as f_disp:
        writer = csv.DictWriter(f_disp, fieldnames=CAMPOS_DISP, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(registros_disp)
        path_disp = f_disp.name

    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.csv', delete=False, encoding='utf-8', newline=''
    ) as f_adj:
        writer = csv.DictWriter(f_adj, fieldnames=CAMPOS_ADJ, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(registros_adj)
        path_adj = f_adj.name

    try:
        log.info("▶ Cargando en base de datos...")
        cargador.procesar(Path(path_disp), Path(path_adj), Path(DB_BOLSA_PATH))
        log.info("✅ Pipeline completado correctamente.")
        return 0
    except Exception as e:
        log.error(f"✗ Error en cargador: {e}")
        return 1
    finally:
        Path(path_disp).unlink(missing_ok=True)
        Path(path_adj).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Forzar re-descarga aunque ya estén procesados")
    args = parser.parse_args()
    sys.exit(main(force=args.force))