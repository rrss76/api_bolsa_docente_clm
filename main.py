from fastapi import FastAPI, HTTPException, Query, APIRouter, BackgroundTasks, Header
import sqlite3
import pandas as pd
import unicodedata
from typing import Optional
import numpy as np
from datetime import datetime
import re
from fastapi.middleware.cors import CORSMiddleware
from typing import Tuple
import os
import requests as _requests
import subprocess
import threading
import logging

app = FastAPI(title="API Interinos CLM")

DB_PATH = "Base_Bolsa_Docente.db"

# Año de convocatoria activo (se usa para detectar tablas de bolsa)
ANIO_BOLSA = "2025"

# Tabla de disponibles semanales
TABLA_DISPONIBLES_SEMANALES = "disponibles_semanales_2025_2026"

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
            "latest": _env("APP_ANDROID_LATEST", "3.0.2+32"),
            "min":    _env("APP_ANDROID_MIN",    "3.0.2+32"),
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
            "latest": _env("APP_IOS_LATEST", "3.0.4"),
            "min":    _env("APP_IOS_MIN",    "3.0.4"),
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
    tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
    patron = re.compile(rf"^bolsas_{ANIO_BOLSA}_(\d{{3}})$")
    resultado = []
    for nombre in tablas["name"]:
        m = patron.match(nombre)
        if m:
            resultado.append((nombre, m.group(1)))  # (tabla, cuerpo)
    return sorted(resultado, key=lambda x: x[1])


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
def obtener_adjudicaciones(nombre: str):
    """Devuelve todas las adjudicaciones que contienen el nombre indicado (todos los cursos)."""
    with sqlite3.connect(DB_PATH) as conn:
        union_query, _ = _union_adjudicaciones(conn)
        if not union_query:
            raise HTTPException(status_code=404, detail="No se encontraron tablas de adjudicaciones.")
        df = pd.read_sql_query(f"SELECT * FROM ({union_query})", conn)

    df = _add_nombre_normalizado(df)
    nombre_norm = normalizar_nombre(nombre)
    df_filtrado = df[df["nombre_normalizado"].str.contains(nombre_norm, na=False)]

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
            f"SELECT nombre, orden_bolsa, cuerpo FROM ({union});",
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
    return df[["nombre", "orden_bolsa", "cuerpo", "display"]].to_dict(orient="records")


@app.get("/fechas_disponibles")
def fechas_disponibles():
    """
    Devuelve las fechas disponibles:
      - 'inicio': la bolsa inicial (bolsas_2025_597)
      - Fechas YYYY-MM-DD de las tablas interinos_YYYYMMDD
    """
    with sqlite3.connect(DB_PATH) as conn:
        tablas_interinos = _tablas_interinos_disponibles(conn)

    fechas = ["inicio"]
    for _, yyyymmdd in tablas_interinos:
        try:
            dt = datetime.strptime(yyyymmdd, "%Y%m%d")
            fechas.append(dt.strftime("%Y-%m-%d"))
        except ValueError:
            pass

    return sorted(fechas)


@app.get("/datos_interino")
def datos_interino(nombre: str = Query(..., description="Nombre completo o parcial del interino")):
    """Datos de puntuación e idiomas de un interino en la bolsa inicial."""
    with sqlite3.connect(DB_PATH) as conn:
        union = _union_bolsas(conn)
        df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)

    nombre_busqueda = normalizar_nombre(nombre)
    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    coincidencias = df[df["nombre_normalizado"].str.contains(nombre_busqueda, case=False, na=False)]

    if coincidencias.empty:
        return {"mensaje": "No se encontraron coincidencias."}

    columnas_deseadas = [
        "nombre", "cuerpo", "puntos_total", "puntos_apd1", "puntos_apd2", "puntos_apd3",
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
    Adjudicaciones cuya fecha_adjudicacion cae en el rango indicado (todos los cursos).
    Parámetros en formato YYYY-MM-DD. La BD almacena fechas como DD/MM/YYYY,
    la conversión se hace automáticamente.
    """
    # Convertir YYYY-MM-DD → DD/MM/YYYY (formato de la BD)
    try:
        desde_bd = datetime.strptime(desde, "%Y-%m-%d").strftime("%d/%m/%Y")
        hasta_bd = datetime.strptime(hasta, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")

    with sqlite3.connect(DB_PATH) as conn:
        union_query, _ = _union_adjudicaciones(conn)
        if not union_query:
            raise HTTPException(status_code=404, detail="No se encontraron tablas de adjudicaciones.")
        # Como las fechas son DD/MM/YYYY no son comparables lexicográficamente con BETWEEN,
        # se convierte a formato ISO dentro de SQLite para la comparación.
        df = pd.read_sql_query(
            f"""
            SELECT * FROM ({union_query})
            WHERE substr(fecha_adjudicacion,7,4)||substr(fecha_adjudicacion,4,2)||substr(fecha_adjudicacion,1,2)
                  BETWEEN ? AND ?
            """,
            conn,
            params=(
                desde.replace("-", ""),
                hasta.replace("-", "")
            )
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
def posicion_en_fecha(nombre: str = Query(...), fecha: str = Query(...)):
    """
    Devuelve la posición de un interino en una fecha concreta.

    - fecha='inicio'  → usa la bolsa inicial (bolsas_2025_597)
    - fecha='YYYY-MM-DD' → busca la tabla interinos_YYYYMMDD
    """
    try:
        if fecha.lower() == "inicio":
            es_tabla_bolsa = True
        else:
            tabla = _nombre_tabla_interinos(fecha)
            es_tabla_bolsa = False

        with sqlite3.connect(DB_PATH) as conn:
            if es_tabla_bolsa:
                union = _union_bolsas(conn)
                if not union:
                    raise HTTPException(status_code=404, detail="No se encontraron tablas de bolsa.")
                df = pd.read_sql_query(f"SELECT * FROM ({union})", conn)
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
        df["especialidades"] = df.get("especialidades", pd.Series([""] * len(df))).fillna("")

        # Idiomas: en bolsa inicial es leng_signos, en interinos_YYYYMMDD puede variar
        idiomas_cols = ["aleman", "frances", "ingles", "italiano", "leng_signos"]
        for col in idiomas_cols:
            if col not in df.columns:
                df[col] = ""

        has_provincias = "provincias" in df.columns
        if has_provincias:
            df["provincias"] = df["provincias"].fillna("")
            df["provincias_list"] = df["provincias"].apply(_split_provincias)
        else:
            df["provincias_list"] = [[] for _ in range(len(df))]

        df["especialidades_list"] = df["especialidades"].apply(_split_especialidades)
        df["especialidades_list_full"] = df["especialidades_list"]

        df = df.sort_values(by="orden_bolsa").reset_index(drop=True)
        nombre_norm = normalizar_nombre(nombre)
        coincidencias = df[df["nombre_normalizado"].str.contains(nombre_norm, na=False)]

        if coincidencias.empty:
            raise HTTPException(status_code=404, detail="Interino no encontrado en esa fecha.")

        cols_keep = [
            "nombre", "nombre_normalizado", "orden_bolsa",
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

            esp_list = _split_especialidades(interino.get("especialidades", ""))
            prov_list_interino = interino["provincias_list"] if has_provincias else []

            posiciones_especialidad = []

            for esp in esp_list:
                df_esp = df_exp[df_exp["esp"] == esp].sort_values("orden_bolsa")
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
                        ].sort_values("orden_bolsa")

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
    Devuelve:
      - Tu posición ese día en la lista semanal de disponibles (si estás en ella).
      - Los 'No Disponibles' por delante de ti:
          Estaban en la bolsa inicial (bolsas_2025_597),
          NO están en la lista de disponibles de esa semana (disponibles_semanales_2025_2026),
          y NO aparecen en adjudicaciones_2025_2026.
      - Resumen (conteo) por especialidad de esos no disponibles por delante.
    """
    nombre_norm = normalizar_nombre(nombre)

    # Validar formato fecha
    try:
        datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")

    # La fecha en disponibles_semanales está en formato DD/MM/YYYY
    try:
        dt = datetime.strptime(fecha, "%Y-%m-%d")
        fecha_bd = dt.strftime("%d/%m/%Y")
    except ValueError:
        raise HTTPException(status_code=400, detail="No se pudo convertir la fecha.")

    with sqlite3.connect(DB_PATH) as conn:

        # 1) Verificar que existe esa fecha en disponibles_semanales
        chk = pd.read_sql_query(
            f"SELECT COUNT(*) as cnt FROM {TABLA_DISPONIBLES_SEMANALES} WHERE fecha=?",
            conn, params=[fecha_bd]
        )
        if chk["cnt"].iloc[0] == 0:
            raise HTTPException(status_code=404, detail=f"No hay datos de disponibles para la fecha '{fecha}'.")

        # 2) Cargar bolsa inicial y localizar al usuario
        df_ini = pd.read_sql_query(
            f"SELECT nombre, nombre_normalizado, orden_bolsa, especialidades FROM ({_union_bolsas(conn)})",
            conn
        )
        if df_ini.empty:
            raise HTTPException(status_code=404, detail="La bolsa inicial está vacía.")

        df_ini["orden_bolsa"] = pd.to_numeric(df_ini["orden_bolsa"], errors="coerce")
        cand = df_ini[df_ini["nombre_normalizado"].str.contains(nombre_norm, na=False)]
        if cand.empty:
            raise HTTPException(status_code=404, detail="No se encontró el aspirante en la bolsa inicial.")

        # Si hay homónimos, tomar el de menor orden_bolsa
        cand = cand.sort_values(["orden_bolsa", "nombre_normalizado"]).reset_index(drop=True)
        user_row = cand.iloc[0]
        user_nom_norm    = str(user_row["nombre_normalizado"])
        user_nom_display = str(user_row.get("nombre", user_nom_norm))
        user_orden       = int(user_row["orden_bolsa"]) if pd.notna(user_row["orden_bolsa"]) else None
        user_esps        = str(user_row.get("especialidades", "") or "")

        if user_orden is None:
            raise HTTPException(status_code=400, detail="El aspirante no tiene 'orden_bolsa' válido en la bolsa inicial.")

        # 3) Posición del usuario en los disponibles de esa semana (si está)
        df_sem = pd.read_sql_query(
            f"SELECT nombre, orden_bolsa FROM {TABLA_DISPONIBLES_SEMANALES} WHERE fecha=?",
            conn, params=[fecha_bd]
        )
        df_sem = _add_nombre_normalizado(df_sem)
        df_sem["orden_bolsa"] = pd.to_numeric(df_sem["orden_bolsa"], errors="coerce")
        df_sem = df_sem.dropna(subset=["orden_bolsa"]).sort_values("orden_bolsa").reset_index(drop=True)

        idxs_user = df_sem.index[df_sem["nombre_normalizado"] == user_nom_norm]
        posicion_semana = int(idxs_user[0] + 1) if len(idxs_user) else None

        # 4) Calcular 'No Disponibles' por delante
        #    - En bolsa inicial con orden < user_orden
        #    - NO en disponibles de esa semana
        #    - NO en adjudicaciones (sin importar fecha)
        nombres_disponibles = set(df_sem["nombre_normalizado"].tolist())

        union_adj, cols_adj = _union_adjudicaciones(conn)
        if union_adj and "nombre" in cols_adj:
            df_adj = pd.read_sql_query(f"SELECT nombre FROM ({union_adj})", conn)
        else:
            df_adj = pd.DataFrame(columns=["nombre"])
        df_adj = _add_nombre_normalizado(df_adj)
        nombres_adjudicados = set(df_adj["nombre_normalizado"].tolist())

        df_ini_adelante = df_ini[df_ini["orden_bolsa"] < user_orden].copy()
        df_no_ahead = df_ini_adelante[
            ~df_ini_adelante["nombre_normalizado"].isin(nombres_disponibles) &
            ~df_ini_adelante["nombre_normalizado"].isin(nombres_adjudicados)
        ].sort_values("orden_bolsa").reset_index(drop=True)

        # 5) Resumen por especialidad
        if not df_no_ahead.empty:
            df_no_ahead["especialidades"] = df_no_ahead["especialidades"].fillna("")
            exp_vals = []
            for _, r in df_no_ahead.iterrows():
                for e in _split_especialidades(r["especialidades"]):
                    exp_vals.append(e)
            if exp_vals:
                resumen_especialidad = (
                    pd.Series(exp_vals)
                    .value_counts()
                    .reset_index()
                    .rename(columns={"index": "especialidad", 0: "count"})
                    .to_dict(orient="records")
                )
            else:
                resumen_especialidad = []
        else:
            resumen_especialidad = []

        detalle = df_no_ahead.rename(columns={
            "nombre_normalizado": "nombre_normalizado",
            "orden_bolsa": "orden_bolsa"
        }).to_dict(orient="records")

        return {
            "fecha": fecha,
            "fecha_bd": fecha_bd,
            "usuario": {
                "nombre": user_nom_display,
                "nombre_normalizado": user_nom_norm,
                "orden_bolsa": user_orden,
                "posicion_en_lista_semana": posicion_semana,
                "especialidades": user_esps
            },
            "no_disponibles_por_delante": detalle,
            "resumen": {
                "total_no_disponibles_por_delante": int(len(df_no_ahead)),
                "por_especialidad": resumen_especialidad
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
    background_tasks: BackgroundTasks,
    x_scraper_token: str = Header(..., description="Token secreto de autenticación")
):
    """
    Lanza el scraper + pipeline en background.
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

    def tarea():
        try:
            _ejecutar_pipeline()
        finally:
            _scraper_lock.release()

    background_tasks.add_task(tarea)
    return {"status": "iniciado", "mensaje": "Scraper lanzado en background."}


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
