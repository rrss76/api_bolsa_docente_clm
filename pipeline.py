#!/usr/bin/env python3
"""
pipeline.py
-----------
Lee el JSON generado por scraper.py, ejecuta los parsers sobre cada PDF,
genera los CSVs intermedios y llama al cargador semanal para insertar
los datos en la base de datos SQLite.

Flujo:
  1. Lee pdfs_nuevos.json  →  rutas de PDFs descargados por scraper.py
  2. Parsea PDFs de disponibles  →  disponibles_YYYYMMDD.csv
  3. Parsea PDFs de adjudicados  →  adjudicaciones_YYYYMMDD.csv
  4. Llama a 3_Cargar_semana.procesar() con ambos CSVs y la BD SQLite

Variables de entorno necesarias:
  DB_PATH   Ruta a la base de datos SQLite (por defecto: Base_Bolsa_Docente.db)

Uso:
  python pipeline.py                          # usa pdfs_nuevos.json por defecto
  python pipeline.py --input pdfs_nuevos.json
"""

import os
import sys
import json
import csv
import argparse
from pathlib import Path
from datetime import datetime

# Importar los parsers y el cargador semanal
sys.path.insert(0, str(Path(__file__).parent))
import importlib
parser_adjudicaciones = importlib.import_module("3_Parser_adjudicaciones")
parser_disponibles    = importlib.import_module("2_Parser_Disponibles")
cargador_semana       = importlib.import_module("3_Cargar_semana")


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

DB_PATH  = Path(os.environ.get("DB_PATH", "Base_Bolsa_Docente.db"))
CSV_DIR  = Path("csvs_generados")


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def extraer_fecha_pdfs(rutas: list[str]) -> str:
    """
    Intenta extraer la fecha del nombre de los PDFs.
    Los PDFs siguen el patrón: Aspirantes disponibles 0590 20260515.pdf
    Devuelve la fecha en formato DD/MM/YYYY o '' si no la encuentra.
    """
    import re
    for ruta in rutas:
        nombre = Path(ruta).stem
        m = re.search(r'(\d{8})$', nombre.replace(' ', ''))
        if m:
            s = m.group(1)  # YYYYMMDD
            return f"{s[6:8]}/{s[4:6]}/{s[0:4]}"
    return ""


def guardar_csv(registros: list[dict], campos: list[str], ruta: Path):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(registros)
    print(f"  ✓ CSV guardado: {ruta}  ({len(registros)} filas)")


# ---------------------------------------------------------------------------
# Campos CSV compatibles con 3_Cargar_semana.py
# ---------------------------------------------------------------------------

# disponibles_YYYYMMDD.csv  →  columnas que espera 3_Cargar_semana
CAMPOS_DISPONIBLES = [
    "fecha", "cod_cuerpo", "cuerpo",
    "cod_especialidad", "especialidad",
    "orden", "dni", "apellidos_nombre",
    "tipo_bolsa", "orden_bolsa", "provincias",
    "ingles", "frances", "aleman", "italiano",
]

# adjudicaciones_YYYYMMDD.csv  →  columnas que espera 3_Cargar_semana
CAMPOS_ADJUDICACIONES = [
    "fecha_publicacion", "fecha_inicio_periodo", "fecha_fin_periodo",
    "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
    "cod_centro", "nombre_centro", "localidad",
    "dni", "apellidos_nombre", "titular",
    "bolsa", "posicion", "tipo_jornada",
    "fecha_inicio", "fecha_fin",
]


# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pipeline CLM: parsea PDFs y carga en SQLite"
    )
    parser.add_argument(
        "--input", default="pdfs_nuevos.json",
        help="JSON con rutas de PDFs generado por scraper.py"
    )
    parser.add_argument(
        "--db", default=str(DB_PATH),
        help="Ruta a la base de datos SQLite"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    db_path    = Path(args.db)

    if not input_path.exists():
        print(f"✗ No existe el fichero de entrada: {input_path}")
        sys.exit(1)

    if not db_path.exists():
        print(f"✗ Base de datos no encontrada: {db_path}")
        sys.exit(1)

    pdfs_nuevos = json.loads(input_path.read_text(encoding="utf-8"))
    pdfs_disp   = pdfs_nuevos.get("disponibles", [])
    pdfs_adj    = pdfs_nuevos.get("adjudicados", [])

    if not pdfs_disp and not pdfs_adj:
        print("✓ No hay PDFs nuevos que procesar.")
        sys.exit(0)

    print("=" * 60)
    print(f"  Pipeline CLM — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"  Disponibles:    {len(pdfs_disp)} PDF(s)")
    print(f"  Adjudicaciones: {len(pdfs_adj)} PDF(s)")
    print("=" * 60)

    # Extraer fecha de los nombres de los PDFs
    fecha_raw = extraer_fecha_pdfs(pdfs_disp + pdfs_adj)
    if not fecha_raw:
        print("✗ No se pudo determinar la fecha desde los nombres de los PDFs.")
        sys.exit(1)

    fecha_fmt = fecha_raw.replace("/", "")  # DDMMYYYY para nombre de fichero
    # Convertir a YYYYMMDD para nombre consistente con 3_Cargar_semana
    d, m, a   = fecha_raw.split("/")
    fecha_yyyymmdd = f"{a}{m}{d}"

    print(f"\n  Fecha detectada: {fecha_raw}  →  {fecha_yyyymmdd}\n")

    # ── 1. Parsear PDFs de disponibles ───────────────────────────────────
    registros_disp = []
    if pdfs_disp:
        print("Parseando PDFs de disponibles...")
        for ruta in pdfs_disp:
            try:
                registros_disp.extend(parser_disponibles.parse_pdf(ruta))
            except Exception as e:
                print(f"  ✗ Error en {Path(ruta).name}: {e}")
        print(f"  → {len(registros_disp)} aspirantes disponibles extraídos\n")
    else:
        print("⚠ No hay PDFs de disponibles esta semana.\n")

    # ── 2. Parsear PDFs de adjudicaciones ────────────────────────────────
    registros_adj = []
    if pdfs_adj:
        print("Parseando PDFs de adjudicaciones...")
        for ruta in pdfs_adj:
            try:
                registros_adj.extend(parser_adjudicaciones.parse_pdf(ruta))
            except Exception as e:
                print(f"  ✗ Error en {Path(ruta).name}: {e}")
        print(f"  → {len(registros_adj)} adjudicaciones extraídas\n")
    else:
        print("⚠ No hay PDFs de adjudicaciones esta semana.\n")

    # ── 3. Guardar CSVs intermedios ───────────────────────────────────────
    csv_disp = CSV_DIR / f"disponibles_{fecha_yyyymmdd}.csv"
    csv_adj  = CSV_DIR / f"adjudicaciones_{fecha_yyyymmdd}.csv"

    print("Guardando CSVs...")

    if registros_disp:
        # Normalizar campo fecha al formato que espera 3_Cargar_semana (DD/MM/YYYY)
        for r in registros_disp:
            if not r.get("fecha"):
                r["fecha"] = fecha_raw
        guardar_csv(registros_disp, CAMPOS_DISPONIBLES, csv_disp)
    else:
        print("  ⚠ Sin registros de disponibles — CSV no generado")

    if registros_adj:
        # El parser de adjudicaciones usa fecha_publicacion; renombrar a fecha
        # para que sea compatible con el cargador
        for r in registros_adj:
            if not r.get("fecha_publicacion"):
                r["fecha_publicacion"] = fecha_raw
        guardar_csv(registros_adj, CAMPOS_ADJUDICACIONES, csv_adj)
    else:
        print("  ⚠ Sin registros de adjudicaciones — CSV no generado")

    # ── 4. Llamar al cargador semanal ─────────────────────────────────────
    if not csv_disp.exists() or not csv_adj.exists():
        print("\n⚠ Faltan uno o ambos CSVs. No se puede ejecutar el cargador.")
        sys.exit(1)

    print(f"\nEjecutando cargador semanal...")
    print(f"  Disponibles:    {csv_disp}")
    print(f"  Adjudicaciones: {csv_adj}")
    print(f"  Base de datos:  {db_path}\n")

    try:
        cargador_semana.procesar(csv_disp, csv_adj, db_path)
    except Exception as e:
        print(f"✗ Error en el cargador semanal: {e}")
        sys.exit(1)

    print("\n✅ Pipeline completado correctamente.")


if __name__ == "__main__":
    main()
