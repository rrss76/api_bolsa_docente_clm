#!/usr/bin/env python3
"""
run_pipeline.py
---------------
Script independiente que ejecuta el scraper + parsers + cargador completo.
Se lanza como proceso separado desde main.py para evitar el timeout
de Render cuando el proceso es largo.

Uso:
    python run_pipeline.py                                  # ejecución normal
    python run_pipeline.py --force                          # fuerza re-descarga de PDFs ya procesados
    python run_pipeline.py --db Base_Bolsa_Docente_TEST.db   # prueba contra una BD alternativa
                                                              # (no envía notificación push)
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
from push_notifications import notificar_actualizacion

import os
DB_BOLSA_PATH = os.getenv("DB_BOLSA_PATH", "Base_Bolsa_Docente.db")
CUERPOS_ESPERADOS = {f"0{n}" for n in range(590, 599)}

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


def main(force: bool = False, db_path: str | None = None):
    log.info("=" * 50)
    log.info(f"Pipeline CLM — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info("=" * 50)

    estado = cargar_estado()
    adjudicaciones = obtener_adjudicaciones_portada()

    from zoneinfo import ZoneInfo
    ahora = datetime.now(ZoneInfo("Europe/Madrid"))
    if (ahora.weekday() == 4 and ahora.hour >= 13
            and not any(a["fecha"] == ahora.strftime("%d/%m/%Y") for a in adjudicaciones)):
        log.error("✗ Viernes pasadas las 13:00 y no hay ninguna adjudicación con la fecha de hoy en portada.")
        return 3

    registros_disp = []
    registros_adj  = []
    hay_novedades  = False
    fecha_raw      = ""
    problemas      = []

    for adj in adjudicaciones:
        pdfs_por_seccion = extraer_pdfs_pagina(adj["url"])

        for seccion, pdfs in pdfs_por_seccion.items():
            for pdf in pdfs:
                clave_pdf = pdf["url"]
                if clave_pdf in estado["pdfs_descargados"] and not force:
                    continue

                resultado = descargar_pdf_bytes(pdf["url"])
                if not resultado:
                    problemas.append(f"no se pudo descargar {pdf['url']}")
                    continue

                pdf_bytes, nombre = resultado
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
                    nuevos = []
                    if seccion == "disponibles":
                        nuevos = parser_disp.parse_pdf_bytes(pdf_bytes, nombre)
                        registros_disp.extend(nuevos)
                        log.info(f"    → {len(nuevos)} registros extraídos")
                    elif seccion == "adjudicados":
                        nuevos = parser_adj.parse_pdf_bytes(pdf_bytes, nombre)
                        registros_adj.extend(nuevos)
                        log.info(f"    → {len(nuevos)} registros extraídos")
                    if not nuevos:
                        problemas.append(f"{nombre} no contiene registros")
                except Exception as e:
                    log.error(f"  ✗ Error parseando {nombre}: {e}")
                    problemas.append(f"error parseando {nombre}: {e}")
                finally:
                    # Liberar memoria del PDF inmediatamente tras parsear
                    del pdf_bytes
                    import gc; gc.collect()

    if not hay_novedades:
        log.info("✓ Sin novedades esta ejecución.")
        return 0

    log.info(f"  → {len(registros_disp)} disponibles | {len(registros_adj)} adjudicaciones")

    # Validación antes de tocar la BD. Si algo falta no se marca ningún PDF como
    # procesado, así que el siguiente disparo reintenta el conjunto completo.
    cuerpos_disp = {str(r.get("cod_cuerpo", "")).strip().zfill(4) for r in registros_disp}
    faltan = sorted(CUERPOS_ESPERADOS - cuerpos_disp)
    if faltan:
        problemas.append(f"faltan disponibles de las bolsas {', '.join(faltan)}")
    if not registros_adj:
        problemas.append("no hay ninguna adjudicación")
    if problemas:
        log.error("✗ Datos incompletos o con errores; no se carga nada (se reintentará en el siguiente disparo):")
        for p in problemas:
            log.error(f"    - {p}")
        return 4

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

    destino_db = Path(db_path) if db_path else Path(DB_BOLSA_PATH)
    try:
        log.info(f"▶ Cargando en base de datos... ({destino_db})")
        cargador.procesar(Path(path_disp), Path(path_adj), destino_db)
        guardar_estado(estado)
        log.info("✅ Pipeline completado correctamente.")
        if db_path:
            log.info("→ Base de datos de prueba: notificación push omitida.")
        else:
            notificar_actualizacion(
                "Bolsa Docente CLM actualizada",
                "Se han publicado nuevos datos de disponibles o adjudicaciones.",
            )
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
    parser.add_argument("--db", default=None,
                        help="Ruta a una base de datos alternativa (p.ej. de pruebas). "
                             "Si se indica, no se envía la notificación push.")
    args = parser.parse_args()
    sys.exit(main(force=args.force, db_path=args.db))