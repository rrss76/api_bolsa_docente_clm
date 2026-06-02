#!/usr/bin/env python3
"""
scraper.py
----------
Detecta adjudicaciones nuevas en la web de la Junta de CLM y descarga
los PDFs correspondientes (adjudicados, disponibles).

Se apoya en un fichero de estado (estado_scraper.json) para no volver
a descargar lo que ya ha sido procesado.

Uso:
  python scraper.py                   # descarga si hay novedades
  python scraper.py --force           # fuerza re-descarga aunque ya exista

Dependencias:
  pip install requests beautifulsoup4
"""

import re
import json
import argparse
import hashlib
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

BASE_URL       = "https://educacion.castillalamancha.es"
PORTADA_URL    = BASE_URL
ESTADO_FILE    = Path("estado_scraper.json")
PDFS_DIR       = Path("pdfs_descargados")

# Patrones que identifican las secciones de PDFs que nos interesan
SECCIONES_INTERES = {
    "adjudicados":  "Aspirantes adjudicados",
    "disponibles":  "Aspirantes disponibles",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Estado persistente
# ---------------------------------------------------------------------------

def cargar_estado() -> dict:
    if ESTADO_FILE.exists():
        return json.loads(ESTADO_FILE.read_text(encoding="utf-8"))
    return {"pdfs_descargados": []}


def guardar_estado(estado: dict):
    ESTADO_FILE.write_text(
        json.dumps(estado, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Detección de adjudicaciones en portada
# ---------------------------------------------------------------------------

def obtener_adjudicaciones_portada() -> list[dict]:
    """
    Parsea la portada y devuelve lista de adjudicaciones encontradas.
    Cada elemento: {"titulo": ..., "url": ..., "fecha": ...}
    """
    print("  Consultando portada...")
    resp = requests.get(PORTADA_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    adjudicaciones = []
    patron = re.compile(
        r"adjudicaci[oó]n.{0,10}d[ií]a.{0,5}(\d{2}/\d{2}/\d{4})",
        re.IGNORECASE
    )

    for a in soup.find_all("a", href=True):
        texto = a.get_text(strip=True)
        if patron.search(texto):
            href = a["href"]
            url  = href if href.startswith("http") else BASE_URL + href
            m    = patron.search(texto)
            fecha = m.group(1) if m else ""
            adjudicaciones.append({
                "titulo": texto,
                "url":    url,
                "fecha":  fecha,
            })

    # Deduplicar por URL
    vistas = set()
    resultado = []
    for adj in adjudicaciones:
        if adj["url"] not in vistas:
            vistas.add(adj["url"])
            resultado.append(adj)

    print(f"  → {len(resultado)} adjudicación(es) encontrada(s) en portada")
    return resultado


# ---------------------------------------------------------------------------
# Extracción de PDFs de una página de adjudicación
# ---------------------------------------------------------------------------

def extraer_pdfs_pagina(url: str) -> dict[str, list[dict]]:
    """
    Visita la página de una adjudicación y devuelve los PDFs organizados
    por sección: {"adjudicados": [...], "disponibles": [...]}
    Cada PDF: {"nombre": ..., "url": ..., "seccion": ...}
    """
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    resultado = {k: [] for k in SECCIONES_INTERES}
    seccion_actual = None

    for elem in soup.find_all(["h2", "h3", "p", "a"]):
        # Detectar cambio de sección
        if elem.name in ("h2", "h3"):
            texto = elem.get_text(strip=True)
            for clave, patron in SECCIONES_INTERES.items():
                if patron.lower() in texto.lower():
                    seccion_actual = clave
                    break
            else:
                # Si el encabezado no es de ninguna sección de interés,
                # reseteamos solo si es un encabezado mayor
                if elem.name == "h2":
                    seccion_actual = None

        # Recoger enlaces PDF en la sección activa
        elif elem.name == "a" and seccion_actual:
            href = elem.get("href", "")
            if href.lower().endswith(".pdf"):
                url_pdf = href if href.startswith("http") else BASE_URL + href
                nombre  = elem.get_text(strip=True) or Path(href).name
                resultado[seccion_actual].append({
                    "nombre":  nombre,
                    "url":     url_pdf,
                    "seccion": seccion_actual,
                })

    total = sum(len(v) for v in resultado.values())
    for sec, pdfs in resultado.items():
        if pdfs:
            print(f"    {sec}: {len(pdfs)} PDF(s)")
    if total == 0:
        print("    ⚠ No se encontraron PDFs en esta página")

    return resultado


# ---------------------------------------------------------------------------
# Descarga de PDFs
# ---------------------------------------------------------------------------

def descargar_pdf(url_pdf: str, carpeta: Path) -> Path | None:
    """Descarga un PDF y lo guarda en carpeta. Devuelve la ruta o None si falla."""
    nombre = Path(url_pdf.split("?")[0]).name
    # Decodificar caracteres URL en el nombre
    from urllib.parse import unquote
    nombre = unquote(nombre)
    destino = carpeta / nombre

    if destino.exists():
        return destino  # ya descargado

    try:
        resp = requests.get(url_pdf, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()
        destino.write_bytes(resp.content)
        return destino
    except Exception as e:
        print(f"    ✗ Error descargando {nombre}: {e}")
        return None




def descargar_pdf_bytes(url_pdf: str) -> tuple | None:
    """
    Descarga un PDF y devuelve (bytes, nombre_fichero).
    No guarda nada en disco. Devuelve None si falla.
    """
    from urllib.parse import unquote
    nombre = unquote(Path(url_pdf.split("?")[0]).name)
    try:
        resp = requests.get(url_pdf, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()
        return resp.content, nombre
    except Exception as e:
        print(f"    ✗ Error descargando {nombre}: {e}")
        return None

# ---------------------------------------------------------------------------
# Proceso principal
# ---------------------------------------------------------------------------

def procesar_adjudicacion(adj: dict, estado: dict, forzar: bool = False) -> dict:
    """
    Procesa una adjudicación: extrae PDFs y los descarga.
    Devuelve un resumen de lo descargado organizado por sección.
    """
    url   = adj["url"]
    fecha = adj["fecha"].replace("/", "") if adj["fecha"] else "sin_fecha"

    # Carpeta de destino: pdfs_descargados/YYYYMMDD/
    fecha_fmt = ""
    if adj["fecha"]:
        d, m, a = adj["fecha"].split("/")
        fecha_fmt = f"{a}{m}{d}"
    carpeta = PDFS_DIR / (fecha_fmt or "sin_fecha")
    carpeta.mkdir(parents=True, exist_ok=True)

    print(f"\n  📄 {adj['titulo']}")
    print(f"     {url}")

    pdfs_por_seccion = extraer_pdfs_pagina(url)
    descargados = {"adjudicados": [], "disponibles": []}

    for seccion, pdfs in pdfs_por_seccion.items():
        for pdf in pdfs:
            clave_pdf = pdf["url"]
            if clave_pdf in estado["pdfs_descargados"] and not forzar:
                continue  # ya procesado
            ruta = descargar_pdf(pdf["url"], carpeta)
            if ruta:
                descargados[seccion].append(str(ruta))
                if clave_pdf not in estado["pdfs_descargados"]:
                    estado["pdfs_descargados"].append(clave_pdf)
                print(f"    ✓ {ruta.name}")

    return descargados


def main():
    parser = argparse.ArgumentParser(
        description="Scraper de adjudicaciones CLM"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Forzar re-descarga aunque ya existan los PDFs"
    )
    parser.add_argument(
        "--output-json", default="pdfs_nuevos.json",
        help="Fichero JSON con rutas de PDFs descargados (para pipeline.py)"
    )
    args = parser.parse_args()

    PDFS_DIR.mkdir(exist_ok=True)
    estado = cargar_estado()

    print("=" * 60)
    print(f"  Scraper CLM — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)

    adjudicaciones = obtener_adjudicaciones_portada()
    todas_nuevas   = {"adjudicados": [], "disponibles": []}
    hay_novedades  = False

    for adj in adjudicaciones:
        descargados = procesar_adjudicacion(adj, estado, forzar=args.force)

        nuevos = sum(len(v) for v in descargados.values())
        if nuevos > 0:
            hay_novedades = True
            for sec in todas_nuevas:
                todas_nuevas[sec].extend(descargados[sec])

    guardar_estado(estado)

    # Guardar rutas de PDFs nuevos para que pipeline.py las use
    Path(args.output_json).write_text(
        json.dumps(todas_nuevas, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print()
    if hay_novedades:
        total = sum(len(v) for v in todas_nuevas.values())
        print(f"✓ {total} PDF(s) nuevos descargados.")
        print(f"  Guardado resumen en: {args.output_json}")
    else:
        print("✓ Sin novedades. No hay PDFs nuevos que procesar.")

    print("=" * 60)
    return 0 if hay_novedades else 2   # 2 = sin novedades (no es error)


if __name__ == "__main__":
    import sys
    sys.exit(main())