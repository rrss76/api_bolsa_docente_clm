from fastapi import FastAPI, HTTPException, Query, APIRouter, BackgroundTasks, Header
import sqlite3
import pandas as pd
import unicodedata
from typing import Optional
import numpy as np
from datetime import datetime, date, timedelta
import re
from fastapi.middleware.cors import CORSMiddleware
from typing import Tuple
import os
import requests as _requests
import subprocess
import sys
import threading
import logging

app = FastAPI(title="API Interinos CLM")

DB_PATH = "Base_Bolsa_Docente.db"

# Año de convocatoria activo (se usa para detectar tablas de bolsa)
ANIO_BOLSA = "2026"

# Tabla de disponibles semanales
TABLA_DISPONIBLES_SEMANALES = "disponibles_semanales_2026_2027"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === APP META ===
def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip() or default

def get_app_meta_dict():
    return {
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "android": {
            "latest": _env("APP_ANDROID_LATEST", "3.0.8+38"),
            "min":    _env("APP_ANDROID_MIN",    "3.0.7+37"),
            "force":  _env("APP_ANDROID_FORCE",  "false").lower() == "true",
            "store_url": _env(
                "APP_ANDROID_STORE_URL",
                "https://play.google.com/store/apps/details?id=com.roberto.bolsadocenteclm"
            ),
            "changelog": [
                x for x in _env("APP_ANDROID_CHANGELOG", "Nueva actualización;Correcciones de la app y actualización de datos").split(";") if x.strip()
            ],
        },
        "ios": {
            "latest": _env("APP_IOS_LATEST", "3.0.8"),
            "min":    _env("APP_IOS_MIN",    "3.0.7"),
            "force":  _env("APP_IOS_FORCE",  "false").lower() == "true",
            "store_url": _env(
                "APP_IOS_STORE_URL",
                "https://apps.apple.com/app/id6749509491"
            ),
            "changelog": [
                x for x in _env("APP_IOS_CHANGELOG", "Nuevo actualización;Correcciones de la app y actualización de datos").split(";") if x.strip()
            ],
        }
    }

@app.get("/app_meta")
def app_meta():
    return get_app_meta_dict()
# === /APP META ===


# ─────────────────────────────────────────────
# HELPERS GENERALES
# ─────────────────────────────────────────────

def normalizar_nombre(nombre):
    """Elimina tildes y pasa a mayúsculas."""
    if not nombre:
        return ""
    nfkd = unicodedata.normalize('NFKD', nombre)
    sin_tildes = ''.join([c for c in nfkd if not unicodedata.combining(c)])
    return sin_tildes.upper()


PROV_MAP = {
    "02": "Albacete",
    "13": "Ciudad Real",
    "16": "Cuenca",
    "19": "Guadalajara",
    "45": "Toledo",
}
ALLOWED_PROV = set(PROV_MAP.keys())


def _split_especialidades(s: str):
    if pd.isna(s) or not str(s).strip():
        return []
    return re.findall(r"\d{3}", str(s))


def _normalizar_especialidades_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Construye 'especialidades_norm' con códigos de 3 dígitos siempre:
    - Bolsa inicial (597): usa 'especialidades' (ej: '036,031,038')
    - Tablas semanales: usa 'codigo_especialidad' con zfill(3) (ej: '1' → '001')
    """
    df = df.copy()

    tiene_esps  = "especialidades" in df.columns
    tiene_cod   = "codigo_especialidad" in df.columns

    if tiene_esps:
        base = df["especialidades"].fillna("").str.strip()
    else:
        base = pd.Series([""] * len(df), index=df.index)

    # Donde 'especialidades' está vacío, usar codigo_especialidad con padding
    if tiene_cod:
        mask_vacio = base == ""
        def _padear(val):
            try:
                return str(int(float(str(val)))).zfill(3)
            except Exception:
                return str(val).strip().zfill(3) if str(val).strip().isdigit() else ""
        base = base.copy()
        base[mask_vacio] = df.loc[mask_vacio, "codigo_especialidad"].apply(_padear)

    df["especialidades_norm"] = base
    return df


def _split_especialidades_norm(s: str):
    """
    Divide 'especialidades_norm' en lista de códigos de 3 dígitos.
    Siempre son códigos numéricos (la normalización ya los convierte).
    """
    if pd.isna(s) or not str(s).strip():
        return []
    return re.findall(r"\d{3}", str(s))


def _split_provincias(s: str):
    if pd.isna(s) or not str(s).strip():
        return []
    s = re.sub(r"[;/\s]+", ",", str(s))
    out, seen = [], set()
    for p in s.split(","):
        p = re.sub(r"\D", "", p)
        if len(p) == 1: p = p.zfill(2)
        elif len(p) > 2: p = p[-2:]
        if p in ALLOWED_PROV and p not in seen:
            seen.add(p); out.append(p)
    return out


def _posicion_en(df_sorted, nombre_norm: str):
    tmp = df_sorted.reset_index(drop=True)
    ix = tmp.index[tmp["nombre_normalizado"] == nombre_norm]
    return int(ix[0]) + 1 if len(ix) else None


def _es_si(series):
    return series.astype(str).str.strip().str.upper().isin(["S", "1", "TRUE", "SI", "YES"])


def _add_nombre_normalizado(df: pd.DataFrame) -> pd.DataFrame:
    """Añade columna nombre_normalizado si no existe (tablas semanales/adjudicaciones)."""
    if "nombre_normalizado" not in df.columns:
        df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    return df


def _nombre_tabla_interinos(fecha_str: str) -> str:
    """
    Dado 'YYYY-MM-DD' devuelve el nombre de tabla interinos_YYYYMMDD.
    Ejemplo: '2025-09-05' -> 'interinos_20250905'
    """
    try:
        dt = datetime.strptime(fecha_str, "%Y-%m-%d")
        return f"interinos_{dt.strftime('%Y%m%d')}"
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")


def _tablas_interinos_disponibles(conn) -> list:
    """Devuelve todos los nombres de tabla que siguen el patrón interinos_YYYYMMDD."""
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(r"^interinos_(\d{8})$")
    resultado = []
    for nombre in tablas["name"]:
        m = patron.match(nombre)
        if m:
            resultado.append((nombre, m.group(1)))  # (tabla, YYYYMMDD)
    return resultado


def _tablas_adjudicaciones(conn) -> list:
    """Devuelve todos los nombres de tabla que siguen el patrón adjudicaciones_YYYY_YYYY."""
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(r"^adjudicaciones_\d{4}_\d{4}$")
    return [t for t in tablas["name"] if patron.match(t)]


def _tablas_bolsas(conn) -> list:
    """Devuelve todas las tablas bolsas_YYYY_CCC del año activo, ordenadas por cuerpo."""
    return _tablas_bolsas_anio(conn, ANIO_BOLSA)


def _tablas_bolsas_anio(conn, anio: str) -> list:
    """Devuelve las tablas bolsas_{anio}_CCC para el año indicado, ordenadas por cuerpo."""
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(rf"^bolsas_{re.escape(anio)}_(\d{{3}})$")
    resultado = []
    for nombre in tablas["name"]:
        m = patron.match(nombre)
        if m:
            resultado.append((nombre, m.group(1)))  # (tabla, cuerpo)
    return sorted(resultado, key=lambda x: x[1])


def _union_bolsas_all_anios(conn) -> str:
    """UNION ALL de todas las tablas bolsas_YYYY_CCC (todos los años disponibles)."""
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(r"^bolsas_\d{4}_\d{3}$")
    partes = [f"SELECT * FROM {nombre}" for nombre in tablas["name"] if patron.match(nombre)]
    return " UNION ALL ".join(partes) if partes else None


def _anios_bolsa_disponibles(conn) -> list:
    """Devuelve los años de convocatoria disponibles en la BD, ordenados descendentemente."""
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(r"^bolsas_(\d{4})_\d{3}$")
    anios = set()
    for nombre in tablas["name"]:
        m = patron.match(nombre)
        if m:
            anios.add(m.group(1))
    return sorted(anios, reverse=True)


def _fecha_skipping_verano(fecha_inicio: date, semanas: float) -> date:
    """
    Añade 'semanas' semanas activas a fecha_inicio saltando julio y agosto
    (meses sin adjudicaciones). Si en algún momento caemos en julio,
    saltamos directamente al 1 de septiembre.
    """
    if semanas is None or semanas <= 0:
        return fecha_inicio
    fecha = fecha_inicio
    if fecha.month in (7, 8):
        fecha = date(fecha.year, 9, 1)
    semanas_restantes = float(semanas)
    while semanas_restantes > 0.01:
        fecha += timedelta(weeks=1)
        if fecha.month == 7:
            fecha = date(fecha.year, 9, 1)
        semanas_restantes -= 1
    return fecha


def _union_bolsas(conn) -> str:
    """UNION ALL de todas las bolsas del año activo, todas tienen las mismas columnas."""
    tablas = _tablas_bolsas(conn)
    if not tablas:
        return None
    partes = [f"SELECT * FROM {t}" for t, _ in tablas]
    return " UNION ALL ".join(partes)


def _union_adjudicaciones(conn) -> str:
    """
    Construye un UNION ALL de todas las tablas de adjudicaciones
    usando únicamente las columnas comunes a todas ellas.
    Evita el error 'SELECTs do not have the same number of result columns'.
    """
    tablas = _tablas_adjudicaciones(conn)
    if not tablas:
        return None, []

    # Obtener columnas de cada tabla
    cols_por_tabla = {}
    for t in tablas:
        cursor = conn.execute(f"PRAGMA table_info({t})")
        cols_por_tabla[t] = [row[1] for row in cursor.fetchall()]

    # Columnas comunes a todas las tablas (preservando orden de la primera)
    comunes = [c for c in cols_por_tabla[tablas[0]] if all(c in cols_por_tabla[t] for t in tablas)]

    cols_sql = ", ".join(comunes)
    partes = [f"SELECT {cols_sql} FROM {t}" for t in tablas]
    union_query = " UNION ALL ".join(partes)
    return union_query, comunes


# ─────────────────────────────────────────────
# ENDPOINTS BÁSICOS
# ─────────────────────────────────────────────

@app.get("/")
def read_root():
    return {"mensaje": "La API está viva!"}


@app.get("/interinos")
def get_nombres_normalizados():
    """Lista de nombres distintos de la bolsa inicial (597)."""
    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas(conn)
        df = pd.read_sql_query(f"SELECT DISTINCT nombre FROM ({union})", conn)
    nombres = df["nombre"].dropna().sort_values().tolist()
    return [{"nombre": n} for n in nombres]


@app.get("/adjudicaciones")
def obtener_adjudicaciones(nombre: str, dni: str = Query(None, description="DNI ofuscado para identificar unívocamente al interino")):
    """Devuelve todas las adjudicaciones que contienen el nombre indicado (todos los cursos)."""
    with sqlite3.connect(DB_PATH) as conn:
        union_query, _ = _union_adjudicaciones(conn)
        if not union_query:
            raise HTTPException(status_code=404, detail="No se encontraron tablas de adjudicaciones.")
        df = pd.read_sql_query(f"SELECT * FROM ({union_query})", conn)

    df = _add_nombre_normalizado(df)
    nombre_norm = normalizar_nombre(nombre)
    # Primero buscar coincidencia exacta; si no hay, usar contains como fallback
    df_filtrado = df[df["nombre_normalizado"] == nombre_norm]
    if df_filtrado.empty:
        df_filtrado = df[df["nombre_normalizado"].str.contains(nombre_norm, na=False)]
    # Filtrar por DNI si se proporciona (identificador único)
    if dni and not df_filtrado.empty and "dni_ofuscado" in df_filtrado.columns:
        filtrado_dni = df_filtrado[df_filtrado["dni_ofuscado"].astype(str) == str(dni)]
        if not filtrado_dni.empty:
            df_filtrado = filtrado_dni

    if df_filtrado.empty:
        return {"adjudicaciones": []}

    return {"adjudicaciones": df_filtrado.drop(columns=["nombre_normalizado"]).to_dict(orient="records")}


@app.get("/buscar_nombre")
def buscar_nombre(query: str = Query(...)):
    """Búsqueda de nombre con orden_bolsa y cuerpo para autocompletado (todas las bolsas)."""
    qnorm = normalizar_nombre(query)

    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas(conn)
        df = pd.read_sql_query(
            f"SELECT nombre, orden_bolsa, cuerpo, dni_ofuscado FROM ({union});",
            conn
        )

    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    mask = df["nombre_normalizado"].str.contains(qnorm, case=False, na=False)
    df = df[mask].copy()

    df["orden_bolsa"] = pd.to_numeric(df["orden_bolsa"], errors="coerce")
    df = (df
          .sort_values(["nombre_normalizado", "cuerpo", "orden_bolsa"])
          .drop_duplicates(subset=["nombre_normalizado", "cuerpo"], keep="first")
          .reset_index(drop=True))

    def mk_display(row):
        ob = row["orden_bolsa"]
        cuerpo = row.get("cuerpo", "")
        base = f"{row['nombre']} — #{int(ob)}" if pd.notna(ob) else row["nombre"]
        return f"{base} (Cuerpo {cuerpo})" if cuerpo else base

    df["display"] = df.apply(mk_display, axis=1)
    return df[["nombre", "orden_bolsa", "cuerpo", "dni_ofuscado", "display"]].to_dict(orient="records")


@app.get("/fechas_disponibles")
def fechas_disponibles():
    """
    Devuelve las fechas YYYY-MM-DD de las tablas interinos_YYYYMMDD, ordenadas.
    La bolsa inicial ('inicio') se consulta directamente con /posicion_en_fecha?fecha=inicio.
    """
    with sqlite3.connect(DB_PATH) as conn:
        tablas_interinos = _tablas_interinos_disponibles(conn)

    fechas = []
    for _, yyyymmdd in tablas_interinos:
        try:
            dt = datetime.strptime(yyyymmdd, "%Y%m%d")
            fechas.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    return sorted(fechas)


@app.get("/cursos_disponibles")
def cursos_disponibles():
    """Devuelve los años de convocatoria disponibles en la BD."""
    with sqlite3.connect(DB_PATH) as conn:
        anios = _anios_bolsa_disponibles(conn)
    return {"cursos": anios}


@app.get("/datos_interino")
def datos_interino(nombre: str = Query(..., description="Nombre completo o parcial del interino"), dni: str = Query(None, description="DNI ofuscado para identificar unívocamente al interino")):
    """Datos de puntuación e idiomas de un interino en la bolsa inicial (todos los años)."""
    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas_all_anios(conn)
        df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)

    nombre_busqueda = normalizar_nombre(nombre)
    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    # Primero buscar coincidencia exacta; si no hay, usar contains como fallback
    coincidencias = df[df["nombre_normalizado"] == nombre_busqueda]
    if coincidencias.empty:
        coincidencias = df[df["nombre_normalizado"].str.contains(nombre_busqueda, case=False, na=False)]
    # Filtrar por DNI si se proporciona (identificador único)
    if dni and not coincidencias.empty and "dni_ofuscado" in coincidencias.columns:
        filtrado_dni = coincidencias[coincidencias["dni_ofuscado"].astype(str) == str(dni)]
        if not filtrado_dni.empty:
            coincidencias = filtrado_dni

    if coincidencias.empty:
        return {"mensaje": "No se encontraron coincidencias."}

    columnas_deseadas = [
        "nombre", "cuerpo", "anio_convocatoria", "puntos_total", "puntos_apd1", "puntos_apd2", "puntos_apd3",
        "especialidad", "especialidades", "aleman", "frances", "ingles", "italiano", "leng_signos"
    ]
    # Filtrar solo las que existen en el df (por si alguna falta)
    columnas_deseadas = [c for c in columnas_deseadas if c in coincidencias.columns]
    datos_filtrados = coincidencias[columnas_deseadas].fillna("")
    return {
        "resultados": datos_filtrados.to_dict(orient="records"),
        "total": len(datos_filtrados)
    }


@app.get("/ceses_previstos")
def ceses_previstos(desde: str = Query(...), hasta: str = Query(...)):
    """
    Adjudicaciones cuya fecha_fin cae en el rango indicado (todos los cursos).
    Parámetros en formato YYYY-MM-DD. fecha_fin está en formato YYYY-MM-DD en la BD.
    """
    try:
        datetime.strptime(desde, "%Y-%m-%d")
        datetime.strptime(hasta, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")

    with sqlite3.connect(DB_PATH) as conn:
        union_query, _ = _union_adjudicaciones(conn)
        if not union_query:
            raise HTTPException(status_code=404, detail="No se encontraron tablas de adjudicaciones.")
        # fecha_fin está en YYYY-MM-DD → BETWEEN funciona directamente
        df = pd.read_sql_query(
            f"""
            SELECT * FROM ({union_query})
            WHERE fecha_fin BETWEEN ? AND ?
            """,
            conn,
            params=(desde, hasta)
        )
    return {
        "total": len(df),
        "desde": desde,
        "hasta": hasta,
        "ceses": df.to_dict(orient="records")
    }


# ─────────────────────────────────────────────
# POSICIÓN INICIAL (bolsa de inicio de curso)
# ─────────────────────────────────────────────

@app.get("/posicion_inicial")
def posicion_inicial(
    nombre: str = Query(..., description="Parte del nombre del interino")
):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            union = _union_bolsas(conn)
            df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)

        df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
        nombre_busqueda = normalizar_nombre(nombre)

        df_nombre = df[df["nombre_normalizado"].str.contains(nombre_busqueda, na=False)]

        if df_nombre.empty:
            return {"mensaje": "No se encontraron interinos con ese nombre."}

        df_bolsa_ordenada = df.sort_values(by="orden_bolsa").reset_index(drop=True)

        resultados = []

        for _, fila in df_nombre.iterrows():
            nombre_actual = fila["nombre"]
            nombre_normalizado = fila["nombre_normalizado"]
            orden = fila["orden_bolsa"]

            pos_general = df_bolsa_ordenada[df_bolsa_ordenada["nombre_normalizado"] == nombre_normalizado].index
            posicion_general = int(pos_general[0] + 1) if not pos_general.empty else None

            especialidades_str = str(fila.get("especialidades", "") or "")
            especialidades = _split_especialidades(especialidades_str)

            posiciones_especialidad = []

            for esp in especialidades:
                df_esp = df[
                    df["especialidades"].fillna("").str.split(",").apply(lambda x: esp in x)
                ].sort_values(by="orden_bolsa").reset_index(drop=True)

                pos_esp = df_esp[df_esp["nombre_normalizado"] == nombre_normalizado].index
                if not pos_esp.empty:
                    pos_idx = pos_esp[0]
                    personas_antes = df_esp.iloc[:pos_idx]

                    idiomas = ["aleman", "frances", "ingles", "italiano", "leng_signos"]
                    personas_con_idiomas = {}
                    for idioma in idiomas:
                        col = idioma
                        if col in personas_antes.columns:
                            personas_con_idiomas[idioma] = int(personas_antes[personas_antes[col] == "S"].shape[0])
                        else:
                            personas_con_idiomas[idioma] = 0

                    posiciones_especialidad.append({
                        "especialidad": esp,
                        "posicion": int(pos_idx + 1),
                        "total_en_especialidad": df_esp.shape[0],
                        "personas_por_delante_con_idiomas": personas_con_idiomas
                    })

            resultados.append({
                "nombre": nombre_actual,
                "orden": orden,
                "posicion_bolsa_general": posicion_general,
                "posiciones_por_especialidad": posiciones_especialidad
            })

        return {"resultados": resultados}

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────
# POSICIÓN EN DISPONIBLES (semana concreta)
# ─────────────────────────────────────────────

@app.get("/posicion_disponibles")
def posicion_disponibles(nombre: str = Query(..., description="Nombre del interino")):
    """Posición del interino en la bolsa inicial, desglosada por provincia."""
    nombre = normalizar_nombre(nombre)

    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas(conn)
        df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)

    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    df = df[["orden_bolsa", "nombre", "nombre_normalizado", "especialidades", "provincias"]].copy()
    df = df.sort_values(by="orden_bolsa").reset_index(drop=True)

    df_filtrado = df[df["nombre_normalizado"].str.contains(nombre, case=False, na=False)].copy()

    if df_filtrado.empty:
        return {"mensaje": "No se encontraron interinos con ese nombre."}

    resultados = []

    for _, row in df_filtrado.iterrows():
        nombre_interino = row["nombre"]
        nombre_normalizado = row["nombre_normalizado"]

        especialidades = str(row["especialidades"]).split(",")
        provincias = _split_provincias(str(row.get("provincias", "") or ""))

        posicion_general = df[df["nombre_normalizado"] == nombre_normalizado].index[0] + 1

        posiciones_por_provincia = []
        for provincia in provincias:
            df_prov = df[df["provincias"].fillna("").str.contains(provincia, na=False)]
            df_prov = df_prov.sort_values(by="orden_bolsa").reset_index(drop=True)

            if nombre_normalizado in df_prov["nombre_normalizado"].values:
                pos = df_prov[df_prov["nombre_normalizado"] == nombre_normalizado].index[0] + 1
                posiciones_por_provincia.append({
                    "provincia": provincia,
                    "posicion": int(pos),
                    "total_en_provincia": int(len(df_prov))
                })

        resultados.append({
            "nombre": nombre_interino,
            "especialidades": especialidades,
            "provincias": provincias,
            "posicion_general_disponibles": int(posicion_general),
            "posiciones_por_provincia": posiciones_por_provincia
        })

    return {"resultados": resultados}


@app.get("/posicion_disponibles_especialidad")
def posicion_disponibles_especialidad(
    nombre: str = Query(..., description="Nombre del interino"),
    especialidad: str = Query(..., description="Código de especialidad a filtrar (ej. 031)")
):
    nombre = normalizar_nombre(nombre)

    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas(conn)
        df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)

    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    df = df[["orden_bolsa", "nombre", "nombre_normalizado", "especialidades", "provincias"]].copy()
    df = df.sort_values(by="orden_bolsa").reset_index(drop=True)

    df_filtrado = df[df["nombre_normalizado"].str.contains(nombre, case=False, na=False)].copy()

    if df_filtrado.empty:
        return {"mensaje": "No se encontraron interinos con ese nombre."}

    resultados = []

    for _, row in df_filtrado.iterrows():
        nombre_interino = row["nombre"]
        nombre_normalizado = row["nombre_normalizado"]
        especialidades = _split_especialidades(str(row.get("especialidades", "") or ""))
        provincias = _split_provincias(str(row.get("provincias", "") or ""))

        if especialidad not in especialidades:
            continue

        posicion_general = df[df["nombre_normalizado"] == nombre_normalizado].index[0] + 1

        df_esp = df[df["especialidades"].fillna("").str.contains(especialidad, na=False)]
        df_esp = df_esp.sort_values(by="orden_bolsa").reset_index(drop=True)

        posicion_en_especialidad = (
            df_esp[df_esp["nombre_normalizado"] == nombre_normalizado].index[0] + 1
            if nombre_normalizado in df_esp["nombre_normalizado"].values else None
        )

        posiciones_por_provincia = []
        for provincia in provincias:
            df_prov = df_esp[df_esp["provincias"].fillna("").str.contains(provincia, na=False)]
            df_prov = df_prov.sort_values(by="orden_bolsa").reset_index(drop=True)

            if nombre_normalizado in df_prov["nombre_normalizado"].values:
                pos = df_prov[df_prov["nombre_normalizado"] == nombre_normalizado].index[0] + 1
                posiciones_por_provincia.append({
                    "provincia": provincia,
                    "posicion": int(pos),
                    "total_en_provincia": int(len(df_prov))
                })

        resultados.append({
            "nombre": nombre_interino,
            "especialidad_filtrada": especialidad,
            "especialidades": especialidades,
            "provincias": provincias,
            "posicion_general_disponibles": int(posicion_general),
            "posicion_en_especialidad": int(posicion_en_especialidad) if posicion_en_especialidad else None,
            "posiciones_por_provincia": posiciones_por_provincia
        })

    if not resultados:
        return {"mensaje": "El interino no tiene la especialidad indicada."}

    return {"resultados": resultados}


# ─────────────────────────────────────────────
# POSICIÓN EN FECHA  (tablas interinos_YYYYMMDD)
# ─────────────────────────────────────────────

@app.get("/posicion_en_fecha")
def posicion_en_fecha(nombre: str = Query(...), fecha: str = Query(...), anio: str = Query(None)):
    """
    Devuelve la posición de un interino en una fecha concreta.

    - fecha='inicio'  → usa la bolsa inicial del año indicado por 'anio' (o ANIO_BOLSA si no se indica)
    - fecha='YYYY-MM-DD' → busca la tabla interinos_YYYYMMDD
    """
    try:
        anio_bolsa = anio if anio else ANIO_BOLSA
        if fecha.lower() == "inicio":
            es_tabla_bolsa = True
            tabla = "bolsa_inicial"
        else:
            tabla = _nombre_tabla_interinos(fecha)
            es_tabla_bolsa = False

        with sqlite3.connect(DB_PATH) as conn:
            if es_tabla_bolsa:
                # Buscar la tabla específica del cuerpo del interino (no mezclar cuerpos)
                tablas_bolsa = [t for t, _ in _tablas_bolsas_anio(conn, anio_bolsa)]
                if not tablas_bolsa:
                    raise HTTPException(status_code=404, detail="No se encontraron tablas de bolsa.")
                nombre_norm = normalizar_nombre(nombre)
                tabla_persona = None
                for t in tablas_bolsa:
                    row = conn.execute(
                        f"SELECT 1 FROM {t} WHERE nombre_normalizado LIKE ?",
                        (f"%{nombre_norm}%",)
                    ).fetchone()
                    if row:
                        tabla_persona = t
                        break
                if tabla_persona is None:
                    raise HTTPException(status_code=404, detail="Interino no encontrado en la bolsa inicial.")
                df = pd.read_sql_query(f"SELECT * FROM {tabla_persona}", conn)
            else:
                chk = pd.read_sql_query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    conn, params=[tabla]
                )
                if chk.empty:
                    raise HTTPException(status_code=404, detail=f"No existe datos para la fecha '{fecha}'.")
                df = pd.read_sql_query(f"SELECT * FROM {tabla}", conn)

        if df.empty:
            raise HTTPException(status_code=404, detail="No se encontraron datos para la fecha indicada.")

        if "orden_bolsa" not in df.columns:
            raise HTTPException(status_code=400, detail="Los datos no contienen la columna 'orden_bolsa'.")

        df = _add_nombre_normalizado(df)
        df["orden_bolsa"] = pd.to_numeric(df["orden_bolsa"], errors="coerce")

        # Unificar especialidades en columna normalizada (código 3 dígitos)
        df = _normalizar_especialidades_df(df)

        # Idiomas
        idiomas_cols = ["aleman", "frances", "ingles", "italiano", "leng_signos"]
        for col in idiomas_cols:
            if col not in df.columns:
                df[col] = ""

        has_provincias = "provincias" in df.columns
        if has_provincias:
            df["provincias"] = df["provincias"].fillna("")
        else:
            df["provincias"] = ""

        # ── tipo_bolsa: 0=ordinaria, 91=reserva. Si no existe, asumir 0. ──
        if "tipo_bolsa" in df.columns:
            df["tipo_bolsa"] = pd.to_numeric(df["tipo_bolsa"], errors="coerce").fillna(0).astype(int)
        else:
            df["tipo_bolsa"] = 0

        # ── Agrupar por persona: en tablas semanales cada especialidad es una fila ──
        # Consolidamos todas las especialidades de la misma persona en una sola fila
        agg_dict = {
            "orden_bolsa": "first",
            "tipo_bolsa":  "first",  # mismo tipo para todas las filas del mismo interino
            "especialidades_norm": lambda x: ",".join(sorted(set(x.dropna().astype(str)))),
            "provincias": "first",
        }
        for col in idiomas_cols:
            agg_dict[col] = "first"

        df = (df
              .groupby("nombre_normalizado", sort=False)
              .agg({"nombre": "first", **agg_dict})
              .reset_index())

        df["provincias_list"] = df["provincias"].apply(_split_provincias)
        df["especialidades_list"] = df["especialidades_norm"].apply(_split_especialidades_norm)
        df["especialidades_list_full"] = df["especialidades_list"]

        # Ordenar: primero bolsa ordinaria (0), luego reserva (91+), dentro de cada una por orden_bolsa
        df = df.sort_values(by=["tipo_bolsa", "orden_bolsa"]).reset_index(drop=True)
        nombre_norm = normalizar_nombre(nombre)
        coincidencias = df[df["nombre_normalizado"].str.contains(nombre_norm, na=False)]

        if coincidencias.empty:
            raise HTTPException(status_code=404, detail="Interino no encontrado en esa fecha.")

        cols_keep = [
            "nombre", "nombre_normalizado", "orden_bolsa", "tipo_bolsa",
            "especialidades_list", "especialidades_list_full",
            "provincias_list", "aleman", "frances", "ingles", "italiano", "leng_signos"
        ]
        for c in cols_keep:
            if c not in df.columns:
                df[c] = ""

        df_exp = df[cols_keep].explode("especialidades_list", ignore_index=True)
        df_exp = df_exp.rename(columns={"especialidades_list": "esp"})
        df_exp = df_exp[df_exp["esp"].notna()]

        resultados = []

        for _, interino in coincidencias.iterrows():
            nom_norm_i = interino["nombre_normalizado"]
            pos_general = _posicion_en(df, nom_norm_i)

            esp_list = _split_especialidades_norm(interino.get("especialidades_norm", ""))
            prov_list_interino = interino["provincias_list"] if has_provincias else []

            posiciones_especialidad = []

            for esp in esp_list:
                df_esp = df_exp[df_exp["esp"] == esp].sort_values(["tipo_bolsa", "orden_bolsa"])
                pos_esp = _posicion_en(df_esp, nom_norm_i)
                total_esp = int(len(df_esp))

                personas_antes = df_esp.reset_index(drop=True).iloc[:max((pos_esp or 1) - 1, 0)]

                personas_con_idiomas = {
                    idioma: int(personas_antes[_es_si(personas_antes[idioma])].shape[0])
                    if idioma in personas_antes.columns else 0
                    for idioma in idiomas_cols
                }

                def tiene_otras_especialidades(lst):
                    if not isinstance(lst, list):
                        return False
                    s = set(lst)
                    return (len(s) > 1) or (esp not in s)

                personas_con_otras_especialidades = int(
                    personas_antes["especialidades_list_full"].apply(tiene_otras_especialidades).sum()
                ) if "especialidades_list_full" in personas_antes.columns else 0

                por_provincia = []
                if has_provincias and prov_list_interino:
                    for cod in prov_list_interino:
                        df_esp_prov = df_esp[
                            df_esp["provincias_list"].apply(lambda lst: isinstance(lst, list) and cod in lst)
                        ].sort_values(["tipo_bolsa", "orden_bolsa"])

                        pos_esp_prov = _posicion_en(df_esp_prov, nom_norm_i)
                        total_esp_prov = int(len(df_esp_prov))

                        personas_antes_prov = (
                            df_esp_prov.reset_index(drop=True).iloc[:pos_esp_prov - 1]
                            if pos_esp_prov and pos_esp_prov > 1
                            else df_esp_prov.iloc[0:0]
                        )

                        def solo_esta_prov(lst):
                            return isinstance(lst, list) and len(lst) == 1 and lst[0] == cod

                        por_delante_solo_esta_provincia = int(
                            personas_antes_prov["provincias_list"].apply(solo_esta_prov).sum()
                        ) if "provincias_list" in personas_antes_prov.columns else 0

                        personas_con_idiomas_en_prov = {
                            idioma: int(personas_antes_prov[_es_si(personas_antes_prov[idioma])].shape[0])
                            if (idioma in personas_antes_prov.columns and len(personas_antes_prov) > 0)
                            else 0
                            for idioma in idiomas_cols
                        }

                        por_provincia.append({
                            "codigo": cod,
                            "provincia": PROV_MAP.get(cod, cod),
                            "posicion": pos_esp_prov,
                            "total_en_provincia": total_esp_prov,
                            "personas_por_delante": (pos_esp_prov - 1) if pos_esp_prov else None,
                            "personas_por_delante_solo_esta_provincia": por_delante_solo_esta_provincia,
                            "personas_por_delante_con_idiomas": personas_con_idiomas_en_prov
                        })

                posiciones_especialidad.append({
                    "especialidad": esp,
                    "posicion": pos_esp,
                    "total_en_especialidad": total_esp,
                    "personas_por_delante_con_idiomas": personas_con_idiomas,
                    "personas_por_delante_con_otras_especialidades": personas_con_otras_especialidades,
                    "por_provincia": por_provincia
                })

            resultados.append({
                "nombre": interino.get("nombre", ""),
                "nombre_normalizado": nom_norm_i,
                "posicion_general": pos_general,
                "posiciones_por_especialidad": posiciones_especialidad
            })

        return {"fecha": fecha, "tabla_usada": tabla, "interinos": resultados}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# NO DISPONIBLES POR DELANTE
# ─────────────────────────────────────────────

@app.get("/no_disponibles_adelante")
def no_disponibles_adelante(
    nombre: str = Query(..., description="Nombre del aspirante (se normaliza internamente)"),
    fecha: str = Query(..., description="Fecha semanal 'YYYY-MM-DD'")
):
    """
    Devuelve los 'No Disponibles' por delante del aspirante en la fecha indicada.
    Criterio: en bolsa del MISMO cuerpo, NO disponibles esa semana, NO adjudicados este curso.

    Mejoras v3 (enfoque híbrido, ~10x más rápido que v2):
      - Solo se consulta la tabla de bolsa del cuerpo del aspirante (no unión de todos).
      - Sets Python para disponibles y adjudicados (sin función SQLite custom).
      - Disponibles filtrados por cuerpo usando el índice (cuerpo, semana).
      - Solo adjudicaciones del curso activo (ANIO_BOLSA / ANIO_BOLSA+1).
      - explode() para resumen por especialidad (vectorizado).
      - Desglose por provincia cuando hay datos.
    """
    nombre_norm = normalizar_nombre(nombre)

    try:
        dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_bd = dt.strftime("%d/%m/%Y")
        # Semana ISO para usar el índice (cuerpo, semana)
        semana_iso = dt.strftime("%G-W%V")
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")

    tabla_adj_activa = f"adjudicaciones_{ANIO_BOLSA}_{int(ANIO_BOLSA) + 1}"

    with sqlite3.connect(DB_PATH) as conn:

        # ── 0) Verificar que existe datos para esa semana ─────────────────
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM {TABLA_DISPONIBLES_SEMANALES} WHERE semana=?",
            (semana_iso,)
        ).fetchone()[0]
        if cnt == 0:
            raise HTTPException(status_code=404,
                detail=f"No hay datos de disponibles para la fecha '{fecha}'.")

        # ── 1) Localizar al usuario en SU tabla de bolsa ──────────────────
        #    Buscamos en cada tabla de bolsa hasta encontrarlo.
        tablas_bolsa = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ?",
                (f"bolsas_{ANIO_BOLSA}_%",)
            ).fetchall()
        )
        if not tablas_bolsa:
            raise HTTPException(status_code=404, detail="La bolsa inicial está vacía.")

        user_fila = None
        user_tabla = None
        user_cuerpo = None
        for t in tablas_bolsa:
            row = conn.execute(
                f"SELECT nombre_normalizado, nombre, CAST(orden_bolsa AS INTEGER), "
                f"especialidades, provincias, cuerpo "
                f"FROM {t} WHERE nombre_normalizado LIKE ? ORDER BY orden_bolsa ASC LIMIT 1",
                (f"%{nombre_norm}%",)
            ).fetchone()
            if row:
                user_fila = row
                user_tabla = t
                user_cuerpo = str(row[5])
                break

        if user_fila is None:
            raise HTTPException(status_code=404,
                detail="No se encontró el aspirante en la bolsa inicial.")

        user_nom_norm    = str(user_fila[0])
        user_nom_display = str(user_fila[1])
        user_orden       = int(user_fila[2])
        user_esps        = str(user_fila[3] or "")
        user_provs       = set(_split_provincias(str(user_fila[4] or "")))

        # ── 2) Set de disponibles (solo mismo cuerpo, vía índice semana+cuerpo) ──
        nombres_disp = {
            normalizar_nombre(r[0]) for r in conn.execute(
                f"SELECT nombre FROM {TABLA_DISPONIBLES_SEMANALES} WHERE semana=? AND cuerpo=?",
                (semana_iso, user_cuerpo)
            ).fetchall()
        }

        # ── 3) Set de adjudicados (solo curso activo) ─────────────────────
        tablas_existentes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if tabla_adj_activa in tablas_existentes:
            nombres_adj = {
                normalizar_nombre(r[0]) for r in conn.execute(
                    f"SELECT DISTINCT nombre FROM {tabla_adj_activa}"
                ).fetchall()
            }
        else:
            nombres_adj = set()

        # ── 4) Filas de bolsa por delante (mismo cuerpo) ──────────────────
        rows_ahead = conn.execute(
            f"SELECT nombre_normalizado, especialidades, provincias, CAST(orden_bolsa AS INTEGER) "
            f"FROM {user_tabla} WHERE CAST(orden_bolsa AS INTEGER) < ? ORDER BY orden_bolsa ASC",
            (user_orden,)
        ).fetchall()

        # ── 5) Filtrar no-disponibles con sets Python ─────────────────────
        no_disp_rows = [
            (nn, esp, prov, ord_b)
            for nn, esp, prov, ord_b in rows_ahead
            if nn not in nombres_disp and nn not in nombres_adj
        ]

        # ── 6) Posición del usuario en disponibles de esa semana ──────────
        en_disp = user_nom_norm in nombres_disp
        if en_disp:
            pos_en_disp = conn.execute(
                f"SELECT COUNT(*) FROM {TABLA_DISPONIBLES_SEMANALES} "
                f"WHERE semana=? AND cuerpo=? AND CAST(orden_bolsa AS INTEGER) < ("
                f"  SELECT CAST(orden_bolsa AS INTEGER) FROM {TABLA_DISPONIBLES_SEMANALES} "
                f"  WHERE semana=? AND cuerpo=? AND nombre LIKE ? LIMIT 1"
                f")",
                (semana_iso, user_cuerpo, semana_iso, user_cuerpo, f"%{nombre_norm[-10:]}%")
            ).fetchone()[0]
            posicion_semana = int(pos_en_disp) + 1
        else:
            posicion_semana = None

        # ── 7) Resumen por especialidad con explode ───────────────────────
        if no_disp_rows:
            import pandas as _pd
            df_no = _pd.DataFrame(no_disp_rows, columns=["nn", "especialidades", "provincias", "orden_bolsa"])
            df_no["esp_list"] = df_no["especialidades"].fillna("").apply(_split_especialidades)
            exploded = df_no.explode("esp_list")
            exploded = exploded[exploded["esp_list"].notna() & (exploded["esp_list"] != "")]
            if not exploded.empty:
                vc = exploded["esp_list"].value_counts()
                resumen_especialidad = [
                    {"especialidad": esp, "count": int(cnt)}
                    for esp, cnt in vc.items()
                ]
            else:
                resumen_especialidad = []

            # ── 8) Desglose por provincia ──────────────────────────────────
            df_no["prov_list"] = df_no["provincias"].fillna("").apply(_split_provincias)
            resumen_provincia = {
                prov: int(df_no["prov_list"].apply(lambda lst: prov in lst).sum())
                for prov in ALLOWED_PROV
            }
        else:
            resumen_especialidad = []
            resumen_provincia = {prov: 0 for prov in ALLOWED_PROV}

        return {
            "fecha": fecha,
            "fecha_bd": fecha_bd,
            "usuario": {
                "nombre": user_nom_display,
                "nombre_normalizado": user_nom_norm,
                "orden_bolsa": user_orden,
                "cuerpo": user_cuerpo,
                "tabla_bolsa": user_tabla,
                "posicion_en_lista_semana": posicion_semana,
                "especialidades": user_esps,
                "provincias": list(user_provs),
            },
            "resumen": {
                "total_no_disponibles_por_delante": len(no_disp_rows),
                "por_especialidad": resumen_especialidad,
                "por_provincia": resumen_provincia,
            },
        }


# ─────────────────────────────────────────────
# ESTIMACIÓN DE ADJUDICACIÓN
# ─────────────────────────────────────────────
# POSICIÓN BÁSICA (versión rápida para historial)
# ─────────────────────────────────────────────

@app.get("/posicion_basica")
def posicion_basica(nombre: str = Query(...), fecha: str = Query(...), anio: str = Query(None)):
    """
    Versión ligera de posicion_en_fecha: solo posición general y por especialidad.
    Usa SQL COUNT en lugar de cargar toda la tabla. ~10x más rápido.
    Diseñado para el historial de posición (34 fechas en paralelo).
    Parámetro opcional 'anio': año de la convocatoria para fecha='inicio' (ej: '2025', '2026').
    Si no se indica, usa ANIO_BOLSA.
    """
    try:
        nombre_norm = normalizar_nombre(nombre)
        anio_bolsa = anio if anio else ANIO_BOLSA

        def _parse_provs(prov_str: str) -> list:
            """Normaliza una cadena de provincias a lista de códigos de 2 dígitos."""
            out, seen = [], set()
            for p in re.sub(r"[;/\s]+", ",", prov_str or "").split(","):
                p = re.sub(r"\D", "", p)
                if len(p) == 1: p = p.zfill(2)
                elif len(p) > 2: p = p[-2:]
                if p in ALLOWED_PROV and p not in seen:
                    seen.add(p); out.append(p)
            return out

        if fecha.lower() == "inicio":
            # ── Bolsa inicial: una entrada por cuerpo donde aparezca el interino ──
            #
            # Dos estructuras posibles:
            #   A) cuerpo 590/591/...: una fila por persona por especialidad,
            #      columnas `codigo_especialidad` + `especialidad`, orden_bolsa = rank dentro de la esp.
            #   B) cuerpo 597/...:     una fila por persona, columna `especialidades` (varios códigos),
            #      orden_bolsa = posición global dentro del cuerpo.
            with sqlite3.connect(DB_PATH) as conn:
                tablas_bolsa = _tablas_bolsas_anio(conn, anio_bolsa)
                if not tablas_bolsa:
                    raise HTTPException(status_code=404, detail="No se encontraron tablas de bolsa.")

                # 1) Encontrar todas las tablas donde aparece el interino
                tablas_persona = []
                for t, cuerpo_code in tablas_bolsa:
                    exists = conn.execute(
                        f"SELECT 1 FROM {t} WHERE nombre_normalizado LIKE ? LIMIT 1",
                        (f"%{nombre_norm}%",)
                    ).fetchone()
                    if exists:
                        tablas_persona.append((t, cuerpo_code))

                if not tablas_persona:
                    raise HTTPException(status_code=404, detail="Interino no encontrado en esa fecha.")

                # 2) Calcular posición dentro de cada cuerpo por separado
                interinos_result = []
                for t, cuerpo_code in tablas_persona:
                    # Obtener TODAS las filas del interino (puede estar en varias especialidades)
                    rows = conn.execute(
                        f"SELECT nombre, nombre_normalizado, orden_bolsa, "
                        f"COALESCE(especialidades,'') AS especialidades_multi, "
                        f"COALESCE(provincias,'') AS provincias, "
                        f"codigo_especialidad, COALESCE(especialidad,'') AS especialidad_nombre, "
                        f"COALESCE(tipo_bolsa_fuente,'ORDINARIA') AS tipo_bolsa "
                        f"FROM {t} WHERE nombre_normalizado LIKE ?",
                        (f"%{nombre_norm}%",)
                    ).fetchall()
                    # índices: 0=nombre, 1=nombre_norm, 2=orden_bolsa, 3=especialidades_multi,
                    #          4=provincias, 5=codigo_especialidad, 6=especialidad_nombre, 7=tipo_bolsa

                    if not rows:
                        continue

                    nombre_found = rows[0][0]
                    mis_provincias = _parse_provs(rows[0][4] or "")

                    # Detectar estructura: ¿tiene codigo_especialidad? → tipo A (590/591)
                    usa_esp_por_fila = rows[0][5] is not None

                    posiciones_esp = []

                    def _cnt_adelante_tipo_a(conn, t, cod_esp, orden_b, tipo, prov=None):
                        """Cuenta personas por delante respetando prioridad ORDINARIA > RESERVA."""
                        prov_filter = f"AND ',' || COALESCE(provincias,'') || ',' LIKE '%,{prov},%'" if prov else ""
                        if tipo == 'ORDINARIA':
                            return conn.execute(
                                f"SELECT COUNT(*) FROM {t} "
                                f"WHERE codigo_especialidad=? AND tipo_bolsa_fuente='ORDINARIA' "
                                f"{prov_filter} AND CAST(orden_bolsa AS INTEGER) < ?",
                                (cod_esp, orden_b)
                            ).fetchone()[0]
                        else:  # RESERVA u otro
                            total_ord = conn.execute(
                                f"SELECT COUNT(*) FROM {t} "
                                f"WHERE codigo_especialidad=? AND tipo_bolsa_fuente='ORDINARIA' {prov_filter}",
                                (cod_esp,)
                            ).fetchone()[0]
                            cnt_res = conn.execute(
                                f"SELECT COUNT(*) FROM {t} "
                                f"WHERE codigo_especialidad=? AND tipo_bolsa_fuente=? "
                                f"{prov_filter} AND CAST(orden_bolsa AS INTEGER) < ?",
                                (cod_esp, tipo, orden_b)
                            ).fetchone()[0]
                            return total_ord + cnt_res

                    def _cnt_adelante_tipo_b(conn, t, orden_b, tipo, esp=None, prov=None):
                        """Cuenta personas por delante en tablas tipo 597 (especialidades en columna)."""
                        esp_filter = f"AND ',' || COALESCE(especialidades,'') || ',' LIKE '%,{esp},%'" if esp else ""
                        prov_filter = f"AND ',' || COALESCE(provincias,'') || ',' LIKE '%,{prov},%'" if prov else ""
                        if tipo == 'ORDINARIA':
                            return conn.execute(
                                f"SELECT COUNT(DISTINCT nombre_normalizado) FROM {t} "
                                f"WHERE tipo_bolsa_fuente='ORDINARIA' {esp_filter} {prov_filter} "
                                f"AND CAST(orden_bolsa AS INTEGER) < ?",
                                (orden_b,)
                            ).fetchone()[0]
                        else:
                            total_ord = conn.execute(
                                f"SELECT COUNT(DISTINCT nombre_normalizado) FROM {t} "
                                f"WHERE tipo_bolsa_fuente='ORDINARIA' {esp_filter} {prov_filter}",
                                ()
                            ).fetchone()[0]
                            cnt_res = conn.execute(
                                f"SELECT COUNT(DISTINCT nombre_normalizado) FROM {t} "
                                f"WHERE tipo_bolsa_fuente=? {esp_filter} {prov_filter} "
                                f"AND CAST(orden_bolsa AS INTEGER) < ?",
                                (tipo, orden_b)
                            ).fetchone()[0]
                            return total_ord + cnt_res

                    if usa_esp_por_fila:
                        # ── Tipo A: una fila por especialidad, orden_bolsa = rank dentro de esa esp ──
                        for row in rows:
                            _, _, orden_b, _, _, cod_esp, esp_nombre, tipo_persona = row
                            if not cod_esp:
                                continue
                            orden_b = int(orden_b) if orden_b is not None else 0

                            cnt_esp = _cnt_adelante_tipo_a(conn, t, cod_esp, orden_b, tipo_persona)
                            pos_esp = cnt_esp + 1

                            por_provincia = []
                            for prov in mis_provincias:
                                cnt_prov = _cnt_adelante_tipo_a(conn, t, cod_esp, orden_b, tipo_persona, prov)
                                por_provincia.append({"codigo": prov, "provincia": PROV_MAP.get(prov, prov), "posicion": cnt_prov + 1})

                            posiciones_esp.append({
                                "especialidad": cod_esp,
                                "nombre_especialidad": esp_nombre or None,
                                "posicion": pos_esp,
                                "por_provincia": por_provincia
                            })

                        # posicion_general = mejor posición entre todas sus especialidades
                        pos_general = min((p["posicion"] for p in posiciones_esp), default=None)

                    else:
                        # ── Tipo B: una fila por persona, especialidades en columna `especialidades` ──
                        row = rows[0]
                        _, _, orden_b, especialidades_str, _, _, _, tipo_persona = row
                        orden_b = int(orden_b) if orden_b is not None else 0

                        pos_general = _cnt_adelante_tipo_b(conn, t, orden_b, tipo_persona) + 1

                        esps = _split_especialidades(especialidades_str or "")
                        for esp in esps:
                            cnt_esp = _cnt_adelante_tipo_b(conn, t, orden_b, tipo_persona, esp=esp)

                            por_provincia = []
                            for prov in mis_provincias:
                                cnt_prov = _cnt_adelante_tipo_b(conn, t, orden_b, tipo_persona, esp=esp, prov=prov)
                                por_provincia.append({"codigo": prov, "provincia": PROV_MAP.get(prov, prov), "posicion": cnt_prov + 1})

                            posiciones_esp.append({"especialidad": esp, "posicion": cnt_esp + 1, "por_provincia": por_provincia})

                    interinos_result.append({
                        "nombre": nombre_found,
                        "cuerpo": cuerpo_code,
                        "posicion_general": pos_general,
                        "provincias_activas": mis_provincias,
                        "posiciones_por_especialidad": posiciones_esp
                    })

            return {
                "fecha": fecha,
                "tabla_usada": "bolsa_inicial",
                "interinos": interinos_result
            }

        else:
            # ── Tabla semanal ───────────────────────────────────────────────────
            tabla = _nombre_tabla_interinos(fecha)

            with sqlite3.connect(DB_PATH) as conn:
                chk = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tabla,)
                ).fetchone()
                if not chk:
                    raise HTTPException(status_code=404, detail=f"No existen datos para la fecha '{fecha}'.")

                rows = conn.execute(
                    f"SELECT nombre, tipo_bolsa, orden_bolsa, codigo_especialidad, COALESCE(provincias,'') "
                    f"FROM {tabla} WHERE nombre LIKE ?",
                    (f"%{nombre_norm}%",)
                ).fetchall()

                if not rows:
                    raise HTTPException(status_code=404, detail="Interino no encontrado en esa fecha.")

                nombre_found = rows[0][0]
                tipo_b  = rows[0][1]
                orden_b = rows[0][2]
                mis_provincias = _parse_provs(rows[0][4] or "")

                pos_general_cnt = conn.execute(f"""
                    SELECT COUNT(DISTINCT nombre) FROM {tabla}
                    WHERE (CAST(tipo_bolsa AS INTEGER) < CAST(? AS INTEGER))
                       OR (CAST(tipo_bolsa AS INTEGER) = CAST(? AS INTEGER) AND CAST(orden_bolsa AS INTEGER) < ?)
                """, (tipo_b, tipo_b, orden_b)).fetchone()[0]
                pos_general = pos_general_cnt + 1

                esps = sorted(set(str(r[3]).zfill(3) for r in rows if r[3]))
                posiciones_esp = []
                for esp in esps:
                    cnt_esp = conn.execute(f"""
                        SELECT COUNT(DISTINCT nombre) FROM {tabla}
                        WHERE codigo_especialidad = ?
                          AND ((CAST(tipo_bolsa AS INTEGER) < CAST(? AS INTEGER))
                            OR (CAST(tipo_bolsa AS INTEGER) = CAST(? AS INTEGER) AND CAST(orden_bolsa AS INTEGER) < ?))
                    """, (esp, tipo_b, tipo_b, orden_b)).fetchone()[0]

                    por_provincia = []
                    for prov in mis_provincias:
                        cnt_prov = conn.execute(f"""
                            SELECT COUNT(DISTINCT nombre) FROM {tabla}
                            WHERE codigo_especialidad = ?
                              AND (',' || COALESCE(provincias,'') || ',' LIKE ?)
                              AND ((CAST(tipo_bolsa AS INTEGER) < CAST(? AS INTEGER))
                                OR (CAST(tipo_bolsa AS INTEGER) = CAST(? AS INTEGER) AND CAST(orden_bolsa AS INTEGER) < ?))
                        """, (esp, f"%,{prov},%", tipo_b, tipo_b, orden_b)).fetchone()[0]
                        por_provincia.append({"codigo": prov, "provincia": PROV_MAP.get(prov, prov), "posicion": cnt_prov + 1})

                    posiciones_esp.append({"especialidad": esp, "posicion": cnt_esp + 1, "por_provincia": por_provincia})

            return {
                "fecha": fecha,
                "tabla_usada": tabla,
                "interinos": [{
                    "nombre": nombre_found,
                    "posicion_general": pos_general,
                    "provincias_activas": mis_provincias,
                    "posiciones_por_especialidad": posiciones_esp
                }]
            }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────

@app.get("/estimacion_adjudicacion")
def estimacion_adjudicacion(
    especialidad: str = Query(..., description="Código de especialidad (ej. 038)"),
    posicion: int = Query(..., description="Posición actual del interino en la lista de disponibles para esa especialidad"),
):
    """
    Calcula la tasa media de adjudicaciones semanales para una especialidad
    y estima en cuántas semanas podría ser llamado el interino según su posición.

    La estimación usa el histórico de adjudicaciones_2025_2026 agrupado por semana ISO.
    """
    try:
        esp_int = int(especialidad)
    except ValueError:
        raise HTTPException(status_code=400, detail="Código de especialidad no válido.")

    # Usar solo la tabla del curso activo
    tabla_activa = f"adjudicaciones_{ANIO_BOLSA}_{int(ANIO_BOLSA) + 1}"

    with sqlite3.connect(DB_PATH) as conn:
        # Verificar que la tabla existe
        existe = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (tabla_activa,)
        ).fetchone()
        if not existe:
            return {
                "sin_datos_curso": True,
                "mensaje": f"Aún no hay adjudicaciones registradas para el curso {ANIO_BOLSA}-{int(ANIO_BOLSA)+1}. "
                           "La estimación estará disponible cuando se publiquen los primeros datos del curso.",
            }

        df = pd.read_sql_query(
            f"SELECT codigo_especialidad, semana FROM {tabla_activa}",
            conn
        )

    if df.empty:
        return {
            "sin_datos_curso": True,
            "mensaje": f"Aún no hay adjudicaciones registradas para el curso {ANIO_BOLSA}-{int(ANIO_BOLSA)+1}. "
                       "La estimación estará disponible cuando se publiquen los primeros datos del curso.",
        }

    # Normalizar código a entero para comparar (la tabla guarda "38", la bolsa usa "038")
    def _to_int(val):
        try:
            return int(str(val).strip())
        except Exception:
            return None

    df["cod_int"] = df["codigo_especialidad"].apply(_to_int)
    df_esp = df[df["cod_int"] == esp_int].dropna(subset=["semana"]).copy()

    if df_esp.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No hay adjudicaciones registradas para la especialidad {especialidad} en el curso actual."
        )

    # Adjudicaciones por semana
    adj_por_semana = df_esp["semana"].value_counts().sort_index()
    total = int(len(df_esp))
    num_semanas = int(len(adj_por_semana))
    media = round(total / num_semanas, 1) if num_semanas > 0 else 0.0
    desv = round(float(adj_por_semana.std()), 1) if num_semanas > 1 else 0.0

    # Estimación
    if media > 0:
        semanas_central = round(posicion / media, 1)
        # Rango usando (media ± desviación), acotado a valores positivos
        tasa_opt = media + desv if desv > 0 else media * 1.3
        tasa_pes = max(1.0, media - desv) if desv > 0 else max(1.0, media * 0.7)

        semanas_opt = round(posicion / tasa_opt, 1)
        semanas_pes = round(posicion / tasa_pes, 1)

        hoy = date.today()
        fecha_central = _fecha_skipping_verano(hoy, semanas_central).isoformat()
        fecha_opt     = _fecha_skipping_verano(hoy, semanas_opt).isoformat()
        fecha_pes     = _fecha_skipping_verano(hoy, semanas_pes).isoformat()
    else:
        semanas_central = semanas_opt = semanas_pes = None
        fecha_central = fecha_opt = fecha_pes = None

    return {
        "especialidad": especialidad,
        "posicion_actual": posicion,
        "estadisticas": {
            "total_adjudicaciones_curso": total,
            "semanas_con_datos": num_semanas,
            "media_semanal": media,
            "desviacion_semanal": desv,
            "detalle_por_semana": adj_por_semana.to_dict(),
        },
        "estimacion": {
            "semanas_estimadas": semanas_central,
            "semanas_optimista": semanas_opt,
            "semanas_pesimista": semanas_pes,
            "fecha_estimada": fecha_central,
            "fecha_optimista": fecha_opt,
            "fecha_pesimista": fecha_pes,
        }
    }


# ─────────────────────────────────────────────
# DIAGNÓSTICO
# ─────────────────────────────────────────────

@app.get("/check_junta")
def check_junta():
    """Endpoint de diagnóstico: comprueba acceso a la web de la Junta de CLM."""
    url = "https://educacion.castillalamancha.es/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
    }
    try:
        resp = _requests.get(url, headers=headers, timeout=15)
        tiene_adjudicacion = "adjudicaci" in resp.text.lower()
        return {
            "status_code": resp.status_code,
            "acceso_ok": resp.status_code == 200,
            "content_length": len(resp.text),
            "detecta_adjudicaciones": tiene_adjudicacion,
            "primeros_200_chars": resp.text[:200],
        }
    except Exception as e:
        return {
            "status_code": None,
            "acceso_ok": False,
            "error": str(e),
        }


# ─────────────────────────────────────────────
# SCRAPER PIPELINE
# ─────────────────────────────────────────────

_scraper_lock = threading.Lock()
_scraper_log  = logging.getLogger("scraper_pipeline")
logging.basicConfig(level=logging.INFO)

DB_BOLSA_PATH = os.getenv("DB_BOLSA_PATH", "Base_Bolsa_Docente.db")


def _ejecutar_pipeline():
    import importlib
    import io
    import json
    import re

    parser_disp = importlib.import_module("2_Parser_Disponibles")
    parser_adj  = importlib.import_module("3_Parser_adjudicaciones")
    cargador    = importlib.import_module("4_Cargador_Semanal")

    _scraper_log.info("▶ Iniciando scraper en memoria...")

    from scraper import obtener_adjudicaciones_portada, extraer_pdfs_pagina, cargar_estado, guardar_estado, descargar_pdf_bytes, BASE_URL

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
                if clave_pdf in estado["pdfs_descargados"]:
                    continue

                resultado = descargar_pdf_bytes(pdf["url"])
                if not resultado:
                    continue

                pdf_bytes, nombre = resultado
                hay_novedades = True
                estado["pdfs_descargados"].append(clave_pdf)

                if not fecha_raw:
                    m = re.search(r'(\d{8})', nombre.replace(' ', ''))
                    if m:
                        s = m.group(1)
                        fecha_raw = f"{s[6:8]}/{s[4:6]}/{s[0:4]}"

                _scraper_log.info(f"  ✓ {nombre}")

                try:
                    if seccion == "disponibles":
                        registros_disp.extend(parser_disp.parse_pdf_bytes(pdf_bytes, nombre))
                    elif seccion == "adjudicados":
                        registros_adj.extend(parser_adj.parse_pdf_bytes(pdf_bytes, nombre))
                except Exception as e:
                    _scraper_log.error(f"  ✗ Error parseando {nombre}: {e}")

    guardar_estado(estado)

    if not hay_novedades:
        _scraper_log.info("✓ Sin novedades esta ejecución.")
        return

    _scraper_log.info(f"  → {len(registros_disp)} disponibles | {len(registros_adj)} adjudicaciones")

    if not fecha_raw:
        _scraper_log.error("✗ No se pudo determinar la fecha.")
        return

    for r in registros_disp:
        if not r.get("fecha"):
            r["fecha"] = fecha_raw
    for r in registros_adj:
        if not r.get("fecha_publicacion"):
            r["fecha_publicacion"] = fecha_raw

    import csv, tempfile
    from pathlib import Path

    CAMPOS_DISP = ["fecha", "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
                   "orden", "dni", "apellidos_nombre", "tipo_bolsa", "orden_bolsa",
                   "provincias", "ingles", "frances", "aleman", "italiano"]
    CAMPOS_ADJ  = ["fecha_publicacion", "fecha_inicio_periodo", "fecha_fin_periodo",
                   "cod_cuerpo", "cuerpo", "cod_especialidad", "especialidad",
                   "cod_centro", "nombre_centro", "localidad", "dni", "apellidos_nombre",
                   "titular", "bolsa", "posicion", "tipo_jornada", "fecha_inicio", "fecha_fin"]

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8', newline='') as f_disp:
        writer = csv.DictWriter(f_disp, fieldnames=CAMPOS_DISP, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(registros_disp)
        path_disp = f_disp.name

    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8', newline='') as f_adj:
        writer = csv.DictWriter(f_adj, fieldnames=CAMPOS_ADJ, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(registros_adj)
        path_adj = f_adj.name

    try:
        _scraper_log.info("▶ Cargando en base de datos...")
        cargador.procesar(Path(path_disp), Path(path_adj), Path(DB_BOLSA_PATH))
        _scraper_log.info("✅ Pipeline completado correctamente.")
    except Exception as e:
        _scraper_log.error(f"✗ Error en cargador: {e}")
    finally:
        Path(path_disp).unlink(missing_ok=True)
        Path(path_adj).unlink(missing_ok=True)


@app.post("/run-scraper")
def run_scraper(
    x_scraper_token: str = Header(..., description="Token secreto de autenticación"),
    force: bool = False,
):
    """
    Lanza el pipeline como proceso independiente (Popen) para evitar
    el timeout de Render en procesos largos.
    Llamar desde GitHub Actions:
      curl -X POST https://api-interinos-2025.onrender.com/run-scraper
           -H "x-scraper-token: TU_TOKEN"
    """
    token_esperado = os.getenv("SCRAPER_TOKEN", "")
    if not token_esperado:
        raise HTTPException(status_code=500, detail="SCRAPER_TOKEN no configurado en el servidor.")
    if x_scraper_token != token_esperado:
        raise HTTPException(status_code=401, detail="Token inválido.")

    if not _scraper_lock.acquire(blocking=False):
        return {
            "status":  "ya_en_ejecucion",
            "mensaje": "El scraper ya está corriendo, espera a que termine."
        }

    try:
        cmd = [sys.executable, "run_pipeline.py"]
        if force:
            cmd.append("--force")
        subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # El lock se liberará cuando el proceso termine
        # (no podemos hacer join sin bloquear, así que lo liberamos tras lanzar)
        _scraper_lock.release()
    except Exception as e:
        _scraper_lock.release()
        raise HTTPException(status_code=500, detail=f"Error lanzando pipeline: {e}")

    return {"status": "iniciado", "mensaje": "Pipeline lanzado como proceso independiente."}


@app.get("/scraper-status")
def scraper_status():
    """Indica si el scraper está corriendo en este momento."""
    en_ejecucion = not _scraper_lock.acquire(blocking=False)
    if not en_ejecucion:
        _scraper_lock.release()
    return {"en_ejecucion": en_ejecucion}


# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)