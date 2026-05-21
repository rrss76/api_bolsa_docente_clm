#!/usr/bin/env python3
"""
parse_adjudicados_semanales.py
------------------------------
Extrae la relación de aspirantes adjudicados (sustituciones semanales) de los
PDFs de la Junta de Comunidades de Castilla-La Mancha y los guarda en un CSV.

Estructura del PDF (dos líneas por registro):
  Línea 1: cod_centro | localidad | DNI-Nombre [Titular] | bolsa/posicion
  Línea 2: nombre_centro | tipo_jornada | De: fecha_inicio A: fecha_fin

Columnas del CSV resultante:
  fecha_publicacion, fecha_inicio_periodo, fecha_fin_periodo,
  cod_cuerpo, cuerpo, cod_especialidad, especialidad,
  cod_centro, nombre_centro, localidad,
  dni, apellidos_nombre, titular,
  bolsa, posicion, tipo_jornada,
  fecha_inicio, fecha_fin

Uso:
  python parse_adjudicados_semanales.py archivo.pdf [archivo2.pdf ...]
  python parse_adjudicados_semanales.py *.pdf -o salida.csv

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
#  Línea 1 (datos del centro y adjudicado):
#   x ≈  25- 140  → cod_centro (8 dígitos)
#   x ≈ 140- 285  → localidad
#   x ≈ 285- 510  → DNI-Nombre [Titular opcional]
#   x ≈ 510- 590  → titular (DNI del titular, sólo si aparece)
#   x ≈ 590- 680  → bolsa/posicion  (ej: "0-52", "91-13")
#
#  Línea 2 (datos del centro y período):
#   x ≈  25- 170  → nombre_centro
#   x ≈ 170- 320  → tipo_jornada  (ej: "Ordinario / JORNADA COMPLETA")
#   x ≈ 320- 530  → "Competencia: ... Programa: ..."  (ignorar)
#   x ≈ 530- 600  → "De:" + fecha_inicio
#   x ≈ 600- 680  → "A:" + fecha_fin
# ---------------------------------------------------------------------------

COL_COD_CENTRO  = (25,  140)
COL_LOCALIDAD   = (140, 285)
COL_DNI_NOMBRE  = (250, 515)
COL_TITULAR     = (510, 590)
COL_BOLSA_POS   = (590, 680)

COL_NOMBRE_CENT = (25,  170)
COL_JORNADA     = (170, 325)
COL_FECHA_INI   = (530, 600)
COL_FECHA_FIN   = (600, 680)


# ---------------------------------------------------------------------------
# Patrones de detección
# ---------------------------------------------------------------------------

RE_CUERPO = re.compile(r"CUERPO-?\s*([0-9]{4})-([A-Z][A-Z\xc1\xc9\xcd\xd3\xda\xdc\xd1 ]+?)(?:$|\n)", re.IGNORECASE)
RE_ESPECIALIDAD = re.compile(r"Especialidad\s+(.+)", re.IGNORECASE)
RE_FUNCION = re.compile(r"Funci[oó]n-\s*([0-9A-Z]+)\s+(.+)", re.IGNORECASE)
RE_FECHA_PUB = re.compile(r"(\d{2}/\d{2}/\d{4})\s*$")
RE_PERIODO = re.compile(r"CON COMIENZO ENTRE\s+(\d{2}/\d{2}/\d{4})\s+[Yy]\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
RE_COD_CENTRO = re.compile(r"^\d{8}$")
RE_DNI = re.compile(r"^\*{3,5}\d+\*{1,3}$")
RE_DNI_NOMBRE = re.compile(r"^(\*{3,5}\d+\*{1,3})-(.+)$")
RE_BOLSA_POS = re.compile(r"^(\d+)-(.+)$")
RE_FECHA = re.compile(r"^\d{2}/\d{2}/\d{4}$")
RE_ORDINARIO = re.compile(r"^(Ordinario|Urgente)", re.IGNORECASE)

CABECERA_TOKENS = {
    "Página", "RELACIÓN", "ASPIRANTES", "ADJUDICADOS", "CON", "COMIENZO",
    "ENTRE", "CUERPO", "Especialidad", "Centro", "Localidad", "Dni",
    "Nombre", "Adjudicado", "Titular", "Bolsa", "Posición", "PORTAL",
    "EDUCACIÓN", "CASTILLA", "MANCHA", "www.educa.jccm.es",
    "finalización", "indicada", "orientativa", "vinculante", "supeditada",
    "alta", "titular", "plaza", "Asignada", "mayor", "años", "obtenido",
    "nota", "final", "fase", "oposición", "igual", "superior", "puntos",
    "(>55):", "(+8):",
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


def is_linea1(fila: list) -> bool:
    """Línea 1: tiene cod_centro (8 dígitos) en columna izquierda."""
    cod = words_in_col(fila, COL_COD_CENTRO)
    return bool(RE_COD_CENTRO.match(cod))


def is_linea2(fila: list) -> bool:
    """Línea 2: empieza con nombre de centro (texto, no número) y tiene jornada."""
    cod = words_in_col(fila, COL_COD_CENTRO)
    jornada = words_in_col(fila, COL_JORNADA)
    return not RE_COD_CENTRO.match(cod) and bool(RE_ORDINARIO.search(jornada))


def parse_dni_nombre(texto: str) -> tuple[str, str]:
    """Separa 'DNI-Apellidos, Nombre' en (dni, nombre)."""
    m = RE_DNI_NOMBRE.match(texto)
    if m:
        return m.group(1), clean(m.group(2))
    return "", clean(texto)


def parse_fecha(texto: str, prefijo: str) -> str:
    """Extrae fecha después de 'De:' o 'A:'."""
    texto = texto.replace(prefijo, "").strip()
    m = RE_FECHA.match(texto.split()[0]) if texto else None
    return texto.split()[0] if m else texto


# ---------------------------------------------------------------------------
# Parser de una página
# ---------------------------------------------------------------------------

def _base_rec(estado: dict) -> dict:
    """Crea un registro base con los metadatos del estado actual."""
    return {
        "fecha_publicacion":    estado.get("fecha_publicacion", ""),
        "fecha_inicio_periodo": estado.get("fecha_inicio_periodo", ""),
        "fecha_fin_periodo":    estado.get("fecha_fin_periodo", ""),
        "cod_cuerpo":           estado.get("cod_cuerpo", ""),
        "cuerpo":               estado.get("cuerpo", ""),
        "cod_especialidad":     estado.get("cod_funcion", ""),
        "especialidad":         estado.get("especialidad", ""),
        "cod_centro": "", "nombre_centro": "", "localidad": "",
        "dni": "", "apellidos_nombre": "", "titular": "",
        "bolsa": "", "posicion": "", "tipo_jornada": "",
        "fecha_inicio": "", "fecha_fin": "",
    }


def parse_page(page, estado: dict) -> list[dict]:
    words = page.extract_words(keep_blank_chars=False)
    if not words:
        return []

    filas = agrupar_por_fila(words)
    registros = []

    # Metadatos de cabecera desde texto plano (más fiable para cabeceras)
    texto_plano = page.extract_text() or ""

    if not estado.get("cuerpo"):
        m = RE_CUERPO.search(texto_plano)
        if m:
            estado["cod_cuerpo"] = m.group(1)
            estado["cuerpo"] = clean(m.group(2))

    if not estado.get("fecha_publicacion"):
        # La fecha de publicación está al final de la línea del período
        m = RE_PERIODO.search(texto_plano)
        if m:
            estado["fecha_inicio_periodo"] = m.group(1)
            estado["fecha_fin_periodo"] = m.group(2)
        # Fecha de publicación: la fecha sola al final de la línea de período
        lineas = texto_plano.splitlines()
        for linea in lineas[:10]:
            if "COMIENZO ENTRE" in linea:
                m2 = RE_FECHA_PUB.search(linea)
                if m2:
                    estado["fecha_publicacion"] = m2.group(1)

    # Recorrer filas
    i = 0
    while i < len(filas):
        fila = filas[i]
        texto_fila = " ".join(w["text"] for w in fila)

        # Detectar especialidad y función
        m_esp = RE_ESPECIALIDAD.search(texto_fila)
        if m_esp and any(w["x0"] < 200 for w in fila if "Especialidad" in w["text"]):
            estado["especialidad"] = clean(m_esp.group(1))
            i += 1
            continue

        m_fun = RE_FUNCION.search(texto_fila)
        if m_fun and any(w["x0"] < 100 for w in fila if "unci" in w["text"]):
            estado["cod_funcion"] = m_fun.group(1)
            estado["funcion"] = clean(m_fun.group(2))
            i += 1
            continue

        if is_cabecera(fila):
            i += 1
            continue

        # Cada adjudicación tiene DOS filas que pueden aparecer en cualquier orden:
        #   - Fila "datos": cod_centro + localidad + DNI/nombre + bolsa  (is_linea1)
        #   - Fila "centro": nombre_centro + jornada + fechas            (is_linea2)
        # La estrategia: al encontrar cualquiera de las dos, guardar en estado["pendiente"]
        # el tipo correspondiente. Al encontrar la otra, combinarlas y emitir el registro.

        if is_linea1(fila):
            cod_centro = words_in_col(fila, COL_COD_CENTRO)
            localidad  = words_in_col(fila, COL_LOCALIDAD)
            dni_nombre = words_in_col(fila, COL_DNI_NOMBRE)
            titular    = words_in_col(fila, COL_TITULAR)
            bolsa_pos  = words_in_col(fila, COL_BOLSA_POS)

            dni, nombre = parse_dni_nombre(dni_nombre)
            # Limpiar sufijos (>55), (+8) del nombre
            import re as _re
            nombre = _re.sub(r'\s*\(>?\+?\d+\)\s*$', '', nombre).strip()
            bolsa, posicion = "", ""
            m_bp = RE_BOLSA_POS.match(bolsa_pos)
            if m_bp:
                bolsa    = m_bp.group(1)
                posicion = clean(m_bp.group(2))
            titular = titular if RE_DNI.match(titular) else ""
            if titular and titular in nombre:
                nombre = clean(nombre.replace(titular, ""))

            datos = {
                "cod_centro": cod_centro, "localidad": localidad,
                "dni": dni, "apellidos_nombre": nombre,
                "titular": titular, "bolsa": bolsa, "posicion": posicion,
            }

            if estado.get("pendiente_centro"):
                # Ya teníamos la fila "centro" esperando → completar y emitir
                rec = _base_rec(estado)
                rec.update(estado["pendiente_centro"])
                rec.update(datos)
                registros.append(rec)
                estado["pendiente_centro"] = None
            else:
                # Guardar fila "datos" y esperar la fila "centro"
                estado["pendiente_datos"] = datos
            i += 1
            continue

        if is_linea2(fila):
            nombre_centro = words_in_col(fila, COL_NOMBRE_CENT)
            tipo_jornada  = words_in_col(fila, COL_JORNADA)
            texto_fechas  = " ".join(w["text"] for w in fila if w["x0"] >= 525)
            fecha_inicio, fecha_fin = "", ""
            m_fechas = re.search(r"De:\s*(\d{2}/\d{2}/\d{4})\s+A:\s*(\d{2}/\d{2}/\d{4})", texto_fechas)
            if m_fechas:
                fecha_inicio = m_fechas.group(1)
                fecha_fin    = m_fechas.group(2)

            centro = {
                "nombre_centro": nombre_centro, "tipo_jornada": tipo_jornada,
                "fecha_inicio": fecha_inicio, "fecha_fin": fecha_fin,
            }

            if estado.get("pendiente_datos"):
                # Ya teníamos la fila "datos" esperando → completar y emitir
                rec = _base_rec(estado)
                rec.update(estado["pendiente_datos"])
                rec.update(centro)
                registros.append(rec)
                estado["pendiente_datos"] = None
            else:
                # Guardar fila "centro" y esperar la fila "datos"
                estado["pendiente_centro"] = centro
            i += 1
            continue

        i += 1

    return registros


# ---------------------------------------------------------------------------
# Procesado de un PDF completo
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Validacion de formato del PDF
# ---------------------------------------------------------------------------

VALIDACION_FMT = {
    "palabras_clave": ["RELACION", "ASPIRANTES", "ADJUDICADOS", "COMIENZO", "CUERPO"],
    "x_centro_esperada": 37,
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
    centros = [w for w in words if _re.match(r"^\d{8}$", w["text"])]
    if centros:
        x_real = centros[0]["x0"]
        x_esp = VALIDACION_FMT["x_centro_esperada"]
        if abs(x_real - x_esp) > VALIDACION_FMT["tolerancia_x"]:
            avisos.append(f"Columna cod_centro desplazada: esperada x\u2248{x_esp}, detectada x\u2248{round(x_real)}")
    texto_orig = pdf.pages[0].extract_text() or ""
    if "De:" not in texto_orig and "DE:" not in texto:
        avisos.append("No se detectan fechas 'De:' — estructura puede haber cambiado")
    total = len(pdf.pages)
    if total > 3:
        muestras = [len([w for w in pdf.pages[i].extract_words()
                         if _re.match(r"^\d{8}$", w["text"])])
                    for i in [max(0, total//4), total//2, min(total-1, 3*total//4)]]
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
        "especialidad": "", "cod_funcion": "", "funcion": "",
        "fecha_publicacion": "",
        "fecha_inicio_periodo": "", "fecha_fin_periodo": "",
        "reg": None,
        "pendiente_datos": None,
        "pendiente_centro": None,
    }
    todos = []

    with pdfplumber.open(pdf_path) as pdf:
        validar_pdf(pdf, path.name)
        total = len(pdf.pages)
        for n, page in enumerate(pdf.pages, 1):
            if n % 20 == 0 or n == total:
                print(f"    Página {n}/{total}...")
            todos.extend(parse_page(page, estado))

    print(f"    → {len(todos)} adjudicaciones extraídas")
    return todos


# ---------------------------------------------------------------------------
# Escritura del CSV
# ---------------------------------------------------------------------------

CAMPOS = [
    "fecha_publicacion", "fecha_inicio_periodo", "fecha_fin_periodo",
    "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
    "cod_centro", "nombre_centro", "localidad",
    "dni", "apellidos_nombre", "titular",
    "bolsa", "posicion", "tipo_jornada",
    "fecha_inicio", "fecha_fin",
]


# Campos obligatorios para considerar un registro completo
CAMPOS_OBLIGATORIOS = ["dni", "nombre_centro", "localidad", "fecha_inicio"]


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
    print("║   Parser de Adjudicaciones Semanales CLM            ║")
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
    salida_default = "adjudicados_semanales.csv"
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
        description="Extrae adjudicaciones semanales de PDFs de CLM a CSV"
    )
    parser.add_argument("pdfs", nargs="*", help="Uno o más archivos PDF (opcional)")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Fichero CSV de salida (por defecto: adjudicados_semanales.csv)",
    )
    args = parser.parse_args()

    if not args.pdfs:
        pdfs, salida = pedir_pdfs_interactivo()
    else:
        pdfs = args.pdfs
        salida = args.output or "adjudicados_semanales.csv"

    todos = []
    for pdf in pdfs:
        todos.extend(parse_pdf(pdf))

    if not todos:
        print("⚠ No se extrajeron registros. Revisa los PDFs.")
        sys.exit(1)

    guardar_csv(todos, salida)


if __name__ == "__main__":
    main()
