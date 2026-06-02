#!/usr/bin/env python3
"""
parse_aspirantes.py
-------------------
Extrae la relación de interinos disponibles para sustituciones de los PDFs
de la Junta de Comunidades de Castilla-La Mancha y los guarda en un CSV.

Estructura del PDF:
  - Cabecera: cuerpo, fecha, especialidad
  - Una fila por aspirante con: orden, DNI, apellidos/nombre,
    tipo_bolsa, orden_bolsa, provincias (códigos), e idiomas (S/N)

Columnas del CSV resultante:
  fecha, cuerpo, especialidad, orden, dni, apellidos_nombre,
  tipo_bolsa, orden_bolsa, provincias, ingles, frances, aleman, italiano

Uso:
  python parse_aspirantes.py archivo.pdf [archivo2.pdf ...]
  python parse_aspirantes.py *.pdf -o salida.csv

Dependencias:
  pip install pdfplumber
"""

import re
import csv
import sys
import argparse
import pdfplumber
from pathlib import Path


# ---------------------------------------------------------------------------
# Rangos de columnas (coordenada X, en puntos)
#
#   x ≈  20- 42   → orden
#   x ≈  43- 85   → DNI  (***XXXX**)
#   x ≈  86-305   → apellidos_nombre
#   x ≈ 300-330   → tipo_bolsa  (0 = ordinaria, 91 = CLM, etc.)
#   x ≈ 330-354   → orden_bolsa
#   x ≈ 354-432   → provincias  (códigos 2 dígitos separados por comas)
#   x ≈ 432-462   → inglés      (S si lo acredita)
#   x ≈ 462-492   → francés
#   x ≈ 492-522   → alemán
#   x ≈ 522-560   → italiano
# ---------------------------------------------------------------------------

COL_ORDEN       = (10,  43)
COL_DNI         = (43,  86)
COL_NOMBRE      = (86, 300)
COL_TIPO_BOLSA  = (295, 326)
COL_ORD_BOLSA   = (326, 354)
COL_PROVINCIAS  = (354, 432)
COL_INGLES      = (432, 462)
COL_FRANCES     = (462, 492)
COL_ALEMAN      = (492, 522)
COL_ITALIANO    = (522, 560)


# ---------------------------------------------------------------------------
# Patrones de detección en cabecera
# ---------------------------------------------------------------------------

RE_CUERPO = re.compile(
    r"CUERPO\s*-(\d{4})-\s*([A-ZÁÉÍÓÚÜÑ\s]+?)(?:$|\n)",
    re.IGNORECASE,
)
RE_ESPECIALIDAD = re.compile(
    r"Especialidad\s+(\d{3})\s+(.+)",
    re.IGNORECASE,
)
RE_FECHA = re.compile(r"FECHA PUBLICACI[ÓO]N:\s*(\d{2}/\d{2}/\d{4})")
RE_DNI   = re.compile(r"^\*{3,4}\d+\*{2,3}$")
RE_ORDEN = re.compile(r"^\d{1,4}$")

CABECERA_TOKENS = {
    "Página", "RELACIÓN", "INTERINOS", "DISPONIBLES", "PARA",
    "SUSTITUCIONES", "CUERPO", "FECHA", "PUBLICACIÓN", "PUBLICACION",
    "Especialidad", "Orden", "DNI", "Apellidos,", "Nombre",
    "Tipo", "Bolsa", "Provincia", "Inglés", "Francés",
    "Alemán", "Italiano", "PORTAL", "EDUCACIÓN", "CASTILLA",
    "MANCHA", "www.educa.jccm.es", "septiembre", "enero", "febrero",
    "marzo", "abril", "mayo", "junio", "julio", "agosto", "octubre",
    "noviembre", "diciembre",
}


def clean(text: str) -> str:
    return " ".join(text.split()).strip()


# ---------------------------------------------------------------------------
# Agrupación de palabras por fila
# ---------------------------------------------------------------------------

def agrupar_por_fila(words: list, tol: float = 4.0) -> list[list]:
    if not words:
        return []
    filas, fila_actual, y_ref = [], [words[0]], words[0]["top"]
    for w in words[1:]:
        if abs(w["top"] - y_ref) <= tol:
            fila_actual.append(w)
        else:
            filas.append(sorted(fila_actual, key=lambda x: x["x0"]))
            fila_actual, y_ref = [w], w["top"]
    filas.append(sorted(fila_actual, key=lambda x: x["x0"]))
    return filas


def words_in_col(words: list, col: tuple) -> str:
    x0, x1 = col
    return clean(" ".join(w["text"] for w in words if x0 <= w["x0"] < x1))


def is_cabecera(fila: list) -> bool:
    tokens = {w["text"] for w in fila}
    return bool(tokens & CABECERA_TOKENS)


def is_data_row(fila: list) -> bool:
    """Fila de datos: empieza con un número de orden y tiene DNI."""
    orden = words_in_col(fila, COL_ORDEN)
    dni   = words_in_col(fila, COL_DNI)
    # orden puede ser "16/1" (bisección) o número simple
    orden_ok = bool(re.match(r"^\d{1,4}(/\d+)?$", orden))
    dni_ok   = bool(RE_DNI.match(dni))
    return orden_ok and dni_ok


# ---------------------------------------------------------------------------
# Parser de una página
# ---------------------------------------------------------------------------

def parse_page(page, estado: dict) -> list[dict]:
    words = page.extract_words(keep_blank_chars=False)
    if not words:
        return []

    filas = agrupar_por_fila(words)
    registros = []

    # Extraer metadatos de cabecera (primeras ~15 filas)
    texto_cabecera = " ".join(
        " ".join(w["text"] for w in f) for f in filas[:15]
    )

    if not estado.get("cuerpo"):
        m = RE_CUERPO.search(texto_cabecera)
        if m:
            estado["cod_cuerpo"] = m.group(1)
            estado["cuerpo"]     = clean(m.group(2))
    # Fallback: extraer cuerpo del texto plano de la página (más fiable)
    if not estado.get("cuerpo"):
        texto_plano = page.extract_text() or ""
        patron = r"CUERPO -([0-9]{4})-([A-ZÁÉÍÓÚÜÑ ]+?)(?:FECHA|$)"
        m = re.search(patron, texto_plano, re.MULTILINE)
        if m:
            estado["cod_cuerpo"] = m.group(1)
            estado["cuerpo"]     = clean(m.group(2))

    if not estado.get("fecha"):
        m = RE_FECHA.search(texto_cabecera)
        if m:
            estado["fecha"] = m.group(1)

    # Recorrer filas
    for fila in filas:
        texto_fila = " ".join(w["text"] for w in fila)

        # Detectar cambio de especialidad
        m_esp = RE_ESPECIALIDAD.search(texto_fila)
        if m_esp:
            estado["cod_especialidad"] = m_esp.group(1)
            estado["especialidad"]     = clean(m_esp.group(2))
            continue

        if is_cabecera(fila):
            continue

        if not is_data_row(fila):
            continue

        # Extraer campos
        orden      = words_in_col(fila, COL_ORDEN)
        dni        = words_in_col(fila, COL_DNI)
        nombre     = words_in_col(fila, COL_NOMBRE)
        tipo_bolsa = words_in_col(fila, COL_TIPO_BOLSA)
        ord_bolsa  = words_in_col(fila, COL_ORD_BOLSA)
        provincias = words_in_col(fila, COL_PROVINCIAS).replace(" ,", ",").replace(", ", ",")
        ingles     = "S" if words_in_col(fila, COL_INGLES)  == "S" else ""
        frances    = "S" if words_in_col(fila, COL_FRANCES) == "S" else ""
        aleman     = "S" if words_in_col(fila, COL_ALEMAN)  == "S" else ""
        italiano   = "S" if words_in_col(fila, COL_ITALIANO) == "S" else ""

        # Limpiar provincias: quitar comas y espacios sobrantes
        provincias = re.sub(r"\s*,\s*", ",", provincias).strip(",")

        registros.append({
            "fecha":            estado.get("fecha", ""),
            "cod_cuerpo":       estado.get("cod_cuerpo", ""),
            "cuerpo":           estado.get("cuerpo", ""),
            "cod_especialidad": estado.get("cod_especialidad", ""),
            "especialidad":     estado.get("especialidad", ""),
            "orden":            orden,
            "dni":              dni,
            "apellidos_nombre": nombre,
            "tipo_bolsa":       tipo_bolsa,
            "orden_bolsa":      ord_bolsa,
            "provincias":       provincias,
            "ingles":           ingles,
            "frances":          frances,
            "aleman":           aleman,
            "italiano":         italiano,
        })

    return registros


# ---------------------------------------------------------------------------
# Procesado de un PDF completo
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validacion de formato del PDF
# ---------------------------------------------------------------------------

VALIDACION_FMT = {
    "palabras_clave": ["RELACION", "INTERINOS", "DISPONIBLES", "CUERPO", "Especialidad"],
    "x_dni_esperada": 43,
    "tolerancia_x": 12,
}


def validar_pdf(pdf, path_name: str) -> bool:
    import re as _re
    avisos = []
    texto = (pdf.pages[0].extract_text() or "").upper()
    ko = [p for p in VALIDACION_FMT["palabras_clave"] if p.upper() not in texto]
    if len(ko) > 2:
        avisos.append(f"Faltan palabras clave: {', '.join(ko)}")
    words = pdf.pages[0].extract_words(keep_blank_chars=False)
    dnis = [w for w in words if _re.match(r"^\*{3,5}\d+\*{1,3}$", w["text"])]
    if dnis:
        x_real = dnis[0]["x0"]
        x_esp = VALIDACION_FMT["x_dni_esperada"]
        if abs(x_real - x_esp) > VALIDACION_FMT["tolerancia_x"]:
            avisos.append(f"Columna DNI desplazada: esperada x\u2248{x_esp}, detectada x\u2248{round(x_real)}")
    total = len(pdf.pages)
    if total > 5:
        muestras = [len([w for w in pdf.pages[i].extract_words()
                         if _re.match(r"^\*{3,5}\d+\*{1,3}$", w["text"])])
                    for i in [total//4, total//2, 3*total//4]]
        if sum(muestras) / len(muestras) < 0.3:
            avisos.append("Densidad de registros muy baja — posible cambio de estructura")
    if avisos:
        print(f"  \u26a0  ADVERTENCIA en {path_name}: el formato puede haber cambiado.")
        for a in avisos:
            print(f"     - {a}")
        print("     Se procesara igualmente, pero revisa los resultados.")
        return False
    return True


def parse_pdf(pdf_path: str) -> list[dict]:
    path = Path(pdf_path)
    print(f"  Procesando: {path.name}")

    estado = {
        "cuerpo": "", "cod_cuerpo": "",
        "especialidad": "", "cod_especialidad": "",
        "fecha": "",
    }
    todos = []

    with pdfplumber.open(pdf_path) as pdf:
        validar_pdf(pdf, path.name)
        total = len(pdf.pages)
        for n, page in enumerate(pdf.pages, 1):
            if n % 50 == 0 or n == total:
                print(f"    Página {n}/{total}...")
            todos.extend(parse_page(page, estado))

    print(f"    → {len(todos)} aspirantes extraídos")
    return todos


# ---------------------------------------------------------------------------
# Escritura del CSV
# ---------------------------------------------------------------------------

CAMPOS = [
    "fecha", "cod_cuerpo", "cuerpo",
    "cod_especialidad", "especialidad",
    "orden", "dni", "apellidos_nombre",
    "tipo_bolsa", "orden_bolsa", "provincias",
    "ingles", "frances", "aleman", "italiano",
]


# Campos obligatorios para considerar un registro completo
CAMPOS_OBLIGATORIOS = ["dni", "apellidos_nombre", "especialidad", "orden_bolsa"]



def parse_pdf_bytes(pdf_bytes: bytes, nombre: str = "archivo.pdf") -> list[dict]:
    """
    Igual que parse_pdf pero acepta bytes en lugar de ruta de fichero.
    Útil cuando el PDF se descarga en memoria sin guardarlo en disco.
    """
    import io
    estado = {
        "cuerpo": "", "cod_cuerpo": "",
        "especialidad": "", "cod_especialidad": "",
        "fecha": "",
    }
    todos = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        validar_pdf(pdf, nombre)
        total = len(pdf.pages)
        for n, page in enumerate(pdf.pages, 1):
            if n % 50 == 0 or n == total:
                print(f"    Página {n}/{total}...")
            todos.extend(parse_page(page, estado))
    print(f"    → {len(todos)} aspirantes extraídos")
    return todos


def mostrar_advertencias(registros: list[dict]):
    """Imprime por consola los registros con campos clave vacíos."""
    incompletos = []
    for i, r in enumerate(registros, 1):
        vacios = [c for c in CAMPOS_OBLIGATORIOS if not r.get(c)]
        if vacios:
            incompletos.append((i, r, vacios))

    if not incompletos:
        print("  \u2713 Todos los registros están completos.")
        return

    print(f"\n\u26a0 {len(incompletos)} registro(s) incompleto(s) — revisa manualmente:")
    col_fila = "Nº fila"
    print(f"  {col_fila:<8} {'Orden':<6} {'Especialidad':<30} {'Nombre':<35} Campos vacios")
    print(f"  {'-'*8} {'-'*6} {'-'*30} {'-'*35} {'-'*20}")
    for fila_num, r, vacios in incompletos:
        nombre   = (r.get("apellidos_nombre", "") or r.get("apellidos_nombre", ""))[:33]
        esp      = (r.get("especialidad", "") or r.get("especialidad", ""))[:28]
        orden    = r.get("orden", "") or r.get("orden_bolsa", "") or r.get("posicion", "")
        campos_v = ", ".join(vacios)
        print(f"  {fila_num:<8} {orden:<6} {esp:<30} {nombre:<35} {campos_v}")
    print()


def guardar_csv(registros: list[dict], salida: str):
    with open(salida, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(registros)
    print(f"\n\u2713 CSV guardado en: {salida}  ({len(registros)} filas)")
    print(f"  Columnas: {', '.join(CAMPOS)}")
    mostrar_advertencias(registros)


# ---------------------------------------------------------------------------
# Modo interactivo
# ---------------------------------------------------------------------------

def pedir_pdfs_interactivo() -> tuple[list[str], str]:
    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║     Parser de Aspirantes Disponibles CLM            ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()
    print("Introduce los archivos PDF a procesar.")
    print("Cuando hayas terminado, deja la línea en blanco y pulsa Enter.")
    print()

    pdfs = []        # rutas ya añadidas
    vistos = set()   # nombres de archivo para detectar duplicados
    print("Puedes introducir rutas de archivos PDF individuales o una carpeta entera.")
    print("Escribe 'exit' o 'cancelar' para salir sin procesar.")
    print()
    while True:
        prompt = f"  PDF o carpeta {len(pdfs) + 1}: " if not pdfs else f"  PDF o carpeta {len(pdfs) + 1} (Enter para terminar): "
        ruta = input(prompt).strip()

        if ruta.lower() in ("exit", "cancelar", "salir"):
            print("\n  Operación cancelada.")
            sys.exit(0)

        if ruta == "":
            if not pdfs:
                print("  ⚠ Debes introducir al menos un PDF o carpeta (o escribe 'exit' para salir).")
                continue
            break

        path = Path(ruta)
        if not path.exists():
            print(f"  ✗ Ruta no encontrada: {ruta}")
            continue

        if path.is_dir():
            encontrados = sorted(set(path.glob("*.pdf")) | set(path.glob("*.PDF")))
            if not encontrados:
                print(f"  ✗ No se encontraron PDFs en la carpeta: {ruta}")
                continue
            añadidos, duplicados = 0, 0
            for p in encontrados:
                clave = p.name.lower()
                if clave in vistos:
                    print(f"  ⚠ Duplicado ignorado: {p.name}")
                    duplicados += 1
                else:
                    vistos.add(clave)
                    pdfs.append(str(p))
                    print(f"  ✓ Añadido: {p.name}")
                    añadidos += 1
            resumen = f"  → {añadidos} PDFs añadidos desde {path.name}/"
            if duplicados:
                resumen += f"  ({duplicados} duplicados ignorados)"
            print(resumen)
            continue

        if path.suffix.lower() != ".pdf":
            print(f"  ✗ El archivo no parece un PDF: {ruta}")
            continue

        clave = path.name.lower()
        if clave in vistos:
            print(f"  ⚠ Duplicado ignorado: {path.name}")
            continue

        vistos.add(clave)
        pdfs.append(str(path))
        print(f"  ✓ Añadido: {path.name}")

    print()
    salida_default = "aspirantes.csv"
    salida_input = input(f"Nombre del CSV de salida [{salida_default}]: ").strip()
    salida = salida_input if salida_input else salida_default
    if not salida.endswith(".csv"):
        salida += ".csv"

    print()
    return pdfs, salida


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Extrae aspirantes disponibles de PDFs de CLM a CSV"
    )
    parser.add_argument("pdfs", nargs="*", help="Uno o más archivos PDF (opcional)")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Fichero CSV de salida (por defecto: aspirantes.csv)",
    )
    args = parser.parse_args()

    if not args.pdfs:
        pdfs, salida = pedir_pdfs_interactivo()
    else:
        pdfs = args.pdfs
        salida = args.output or "aspirantes.csv"

    todos = []
    for pdf in pdfs:
        todos.extend(parse_pdf(pdf))

    if not todos:
        print("⚠ No se extrajeron registros. Revisa los PDFs.")
        sys.exit(1)

    guardar_csv(todos, salida)


if __name__ == "__main__":
    main()