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
import os  # === APP META (NUEVO) ===
import requests as _requests
import subprocess
import threading
import logging

app = FastAPI(title="API Interinos CLM")

DB_PATH = "Base_interinos_2025.db"

# Permitir todas las peticiones desde cualquier origen (útil para pruebas)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === APP META (NUEVO) ===
def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip() or default

def get_app_meta_dict():
    """
    Metadatos de versión que la app consulta al inicio para decidir si
    muestra un banner o fuerza actualización. Se leen de variables
    de entorno para no tocar código en cada release.
    """
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
# === /APP META (NUEVO) ===


def normalizar_nombre(nombre):
    if not nombre:
        return ""
    nfkd = unicodedata.normalize('NFKD', nombre)
    sin_tildes = ''.join([c for c in nfkd if not unicodedata.combining(c)])
    return sin_tildes.upper()


@app.get("/")
def read_root():
    return {"mensaje": "La API está viva!"}


@app.get("/interinos")
def get_nombres_normalizados():
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT DISTINCT nombre FROM disponibles_inicio_curso_597", conn)
    nombres = df["nombre"].dropna().sort_values().tolist()
    return [{"nombre": n} for n in nombres]


@app.get("/adjudicaciones")
def obtener_adjudicaciones(nombre: str):
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM adjudicaciones_total", conn)

    nombre_norm = normalizar_nombre(nombre)
    df_filtrado = df[df["nombre_normalizado"].str.contains(nombre_norm)]

    if df_filtrado.empty:
        return {"adjudicaciones": []}

    return {"adjudicaciones": df_filtrado.to_dict(orient="records")}


@app.get("/buscar_nombre")
def buscar_nombre(query: str = Query(...)):
    qnorm = normalizar_nombre(query)

    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query(
            "SELECT nombre, orden_bolsa FROM disponibles_inicio_curso_597;",
            conn
        )

    df["nombre_normalizado"] = df["nombre"].apply(normalizar_nombre)
    mask = df["nombre_normalizado"].str.contains(qnorm, case=False, na=False)
    df = df[mask].copy()

    df["orden_bolsa"] = pd.to_numeric(df["orden_bolsa"], errors="coerce")
    df = (df
          .sort_values(["nombre_normalizado", "orden_bolsa"])
          .drop_duplicates(subset=["nombre_normalizado", "orden_bolsa"], keep="first")
          .reset_index(drop=True))

    def mk_display(row):
        ob = row["orden_bolsa"]
        return f"{row['nombre']} — #{int(ob)}" if pd.notna(ob) else row["nombre"]

    df["display"] = df.apply(mk_display, axis=1)
    return df[["nombre", "orden_bolsa", "display"]].to_dict(orient="records")


@app.get("/fechas_disponibles")
def fechas_disponibles():
    with sqlite3.connect(DB_PATH) as conn:
        tablas = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table'", conn)
        fechas = []
        for tabla in tablas["name"]:
            match = re.fullmatch(r"disponibles_(\d{4})_(\d{2})_(\d{2})", tabla)
            if match:
                fechas.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
            if tabla == "disponibles_inicio_curso_597":
                fechas.append("inicio")
    return sorted(fechas)


@app.get("/datos_interino")
def datos_interino(nombre: str = Query(..., description="Nombre completo o parcial del interino")):
    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM disponibles_inicio_curso_597", conn)

    nombre_busqueda = normalizar_nombre(nombre)
    coincidencias = df[df["nombre_normalizado"].str.contains(nombre_busqueda, case=False, na=False)]

    if coincidencias.empty:
        return {"mensaje": "No se encontraron coincidencias."}

    columnas_deseadas = [
        "nombre", "puntos_total", "puntos_apd1", "puntos_apd2", "puntos_apd3",
        "especialidades", "aleman", "frances", "ingles", "italiano", "lengua_signos"
    ]
    datos_filtrados = coincidencias[columnas_deseadas].fillna("")
    return {
        "resultados": datos_filtrados.to_dict(orient="records"),
        "total": len(datos_filtrados)
    }


@app.get("/ceses_previstos")
def ceses_previstos(desde: str = Query(...), hasta: str = Query(...)):
    with sqlite3.connect("Base_interinos_2025.db") as conn:
        df = pd.read_sql_query(
            """
            SELECT *
            FROM adjudicaciones_total
            WHERE fecha_fin BETWEEN ? AND ?
            """,
            conn,
            params=(desde, hasta)
        )
        return {
            "total": len(df),
            "ceses": df.to_dict(orient="records")
        }


@app.get("/posicion_inicial")
def posicion_inicial(
    nombre: str = Query(..., description="Parte del nombre del interino")
):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            df = pd.read_sql_query("SELECT * FROM disponibles_inicio_curso_597", conn)

        nombre_busqueda = nombre.strip().upper()

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

            especialidades_str = fila["especialidades"]
            especialidades = [e.strip() for e in especialidades_str.split(",") if e.strip().isdigit()]

            posiciones_especialidad = []

            for esp in especialidades:
                df_esp = df[
                    df["especialidades"].fillna("").str.split(",").apply(lambda x: esp in x)
                ].sort_values(by="orden_bolsa").reset_index(drop=True)

                pos_esp = df_esp[df_esp["nombre_normalizado"] == nombre_normalizado].index
                if not pos_esp.empty:
                    pos_idx = pos_esp[0]

                    personas_antes = df_esp.iloc[:pos_idx]

                    idiomas = ["aleman", "frances", "ingles", "italiano", "lengua_signos"]
                    personas_con_idiomas = {
                        idioma: int(personas_antes[personas_antes[idioma] == "S"].shape[0])
                        for idioma in idiomas
                    }

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


@app.get("/posicion_disponibles")
def posicion_disponibles(nombre: str = Query(..., description="Nombre del interino")):
    nombre = normalizar_nombre(nombre)

    with sqlite3.connect(DB_PATH) as conn:
        df = pd.read_sql_query("SELECT * FROM disponibles_inicio_curso_597", conn)

    df = df[["orden_bolsa", "nombre", "nombre_normalizado", "especialidades", "provincias"]].copy()
    df = df.sort_values(by="orden_bolsa").reset_index(drop=True)

    df_filtrado = df[df["nombre_normalizado"].str.contains(nombre, case=False, na=False)].copy()

    if df_filtrado.empty:
        return {"mensaje": "No se encontraron interinos con ese nombre."}

    resultados = []

    for _, row in df_filtrado.iterrows():
        nombre_interino = row["nombre"]
        nombre_normalizado = row["nombre_normalizado"]
        orden_interino = row["orden_bolsa"]

        especialidades = str(row["especialidades"]).split(",")
        provincias = str(row["provincias"]).split(",")

        posicion_general = df[df["nombre_normalizado"] == nombre_normalizado].index[0] + 1

        posiciones_por_provincia = []
        for provincia in provincias:
            df_prov = df[df["provincias"].str.contains(provincia, na=False)]
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
        df = pd.read_sql_query("SELECT * FROM disponibles_inicio_curso_597", conn)

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
        provincias = str(row["provincias"]).split(",")

        if especialidad not in especialidades:
            continue

        posicion_general = df[df["nombre_normalizado"] == nombre_normalizado].index[0] + 1

        df_esp = df[df["especialidades"].str.contains(especialidad, na=False)]
        df_esp = df_esp.sort_values(by="orden_bolsa").reset_index(drop=True)

        if nombre_normalizado in df_esp["nombre_normalizado"].values:
            posicion_en_especialidad = df_esp[df_esp["nombre_normalizado"] == nombre_normalizado].index[0] + 1
        else:
            posicion_en_especialidad = None

        posiciones_por_provincia = []
        for provincia in provincias:
            df_prov = df_esp[df_esp["provincias"].str.contains(provincia, na=False)]
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


@app.get("/posicion_en_fecha")
def posicion_en_fecha(nombre: str = Query(...), fecha: str = Query(...)):
    try:
        if fecha.lower() == "inicio":
            tabla = "disponibles_inicio_curso_597"
        elif fecha.lower() == "admitidos":
            tabla = "admitidos_sin_provincias"
        else:
            try:
                fecha_dt = datetime.strptime(fecha, "%Y-%m-%d")
                tabla = f"disponibles_{fecha_dt.strftime('%Y_%m_%d')}"
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD o 'inicio' o 'admitidos'.")

        with sqlite3.connect(DB_PATH) as conn:
            try:
                df = pd.read_sql_query(f"SELECT * FROM {tabla}", conn)
            except Exception:
                raise HTTPException(status_code=404, detail=f"No se pudo leer la tabla '{tabla}'.")

        if df.empty:
            raise HTTPException(status_code=404, detail=f"La tabla '{tabla}' está vacía o no existe.")
        if "orden_bolsa" not in df.columns or "nombre_normalizado" not in df.columns:
            raise HTTPException(status_code=400, detail=f"La tabla '{tabla}' no tiene columnas clave requeridas.")

        df["orden_bolsa"] = pd.to_numeric(df["orden_bolsa"], errors="coerce")
        df["especialidades"] = df.get("especialidades", "").fillna("")
        for col in ["aleman","frances","ingles","italiano","lengua_signos"]:
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
            "provincias_list", "aleman", "frances", "ingles", "italiano", "lengua_signos"
        ]
        for c in cols_keep:
            if c not in df.columns:
                df[c] = "" if c not in ("orden_bolsa","especialidades_list","especialidades_list_full","provincias_list") else df[c]

        df_exp = df[cols_keep].explode("especialidades_list", ignore_index=True)
        df_exp = df_exp.rename(columns={"especialidades_list": "esp"})
        df_exp = df_exp[df_exp["esp"].notna()]

        def _es_si(series):
            return series.astype(str).str.strip().str.upper().isin(["S","1","TRUE","SI","YES"])

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

                idiomas_cols = ["aleman", "frances", "ingles", "italiano", "lengua_signos"]
                personas_con_idiomas = {
                    idioma: int(personas_antes[_es_si(personas_antes[idioma])].shape[0]) if idioma in personas_antes.columns else 0
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
                        df_esp_prov = df_esp[df_esp["provincias_list"].apply(lambda lst: isinstance(lst, list) and cod in lst)]
                        df_esp_prov = df_esp_prov.sort_values("orden_bolsa")

                        pos_esp_prov = _posicion_en(df_esp_prov, nom_norm_i)
                        total_esp_prov = int(len(df_esp_prov))

                        if pos_esp_prov is not None and pos_esp_prov > 1:
                            personas_antes_prov = df_esp_prov.reset_index(drop=True).iloc[:pos_esp_prov - 1]
                        else:
                            personas_antes_prov = df_esp_prov.iloc[0:0]

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


# === Helper NUEVO para endpoint de no disponibles ===
def _tabla_disponibles_from_date_strict(fecha: str) -> str:
    """
    Convierte 'YYYY-MM-DD' en 'disponibles_YYYY_MM_DD'.
    Este helper SOLO admite fechas semanales (no 'inicio' ni 'admitidos').
    """
    if not isinstance(fecha, str):
        raise HTTPException(status_code=400, detail="La fecha debe ser una cadena 'YYYY-MM-DD'.")
    if fecha.lower() in ("inicio", "admitidos"):
        raise HTTPException(status_code=400, detail="Para este endpoint necesitas una fecha semanal (YYYY-MM-DD).")
    try:
        dt = datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha no válido. Usa YYYY-MM-DD.")
    return f"disponibles_{dt.strftime('%Y_%m_%d')}"


# === NUEVO ENDPOINT: No Disponibles por delante ===
@app.get("/no_disponibles_adelante")
def no_disponibles_adelante(
    nombre: str = Query(..., description="Nombre del aspirante (se normaliza internamente)"),
    fecha: str = Query(..., description="Fecha semanal 'YYYY-MM-DD' de las tablas de disponibles")
):
    """
    Devuelve:
      - Tu posición ese día en la lista semanal (si estás en esa lista).
      - Los 'No Disponibles' por delante de ti:
          Estaban en la lista inicial (disponibles_inicio_curso_597),
          NO están en la lista semanal de esa fecha,
          y NO aparecen en 'adjudicaciones_total' (independientemente de la fecha).
      - Resumen (conteo) por especialidad de esos no disponibles por delante.
    """
    nombre_norm = normalizar_nombre(nombre)
    tabla_semana = _tabla_disponibles_from_date_strict(fecha)  # valida y forma: disponibles_YYYY_MM_DD

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # 1) Verificar tabla semanal
        chk = pd.read_sql_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            conn, params=[tabla_semana]
        )
        if chk.empty:
            raise HTTPException(status_code=404, detail=f"No existe la tabla semanal '{tabla_semana}'.")

        # 2) Cargar lista inicial y localizar al usuario
        df_ini = pd.read_sql_query(
            "SELECT nombre, nombre_normalizado, orden_bolsa, especialidades FROM disponibles_inicio_curso_597",
            conn
        )
        if df_ini.empty:
            raise HTTPException(status_code=404, detail="La tabla 'disponibles_inicio_curso_597' está vacía.")

        df_ini["orden_bolsa"] = pd.to_numeric(df_ini["orden_bolsa"], errors="coerce")
        cand = df_ini[df_ini["nombre_normalizado"].str.contains(nombre_norm, na=False)]
        if cand.empty:
            raise HTTPException(status_code=404, detail="No se encontró el aspirante en la lista inicial.")

        # Si hay homónimos, tomar el de menor orden_bolsa
        cand = cand.sort_values(["orden_bolsa", "nombre_normalizado"]).reset_index(drop=True)
        user_row = cand.iloc[0]
        user_nom_norm = str(user_row["nombre_normalizado"])
        user_nom_display = str(user_row.get("nombre", user_nom_norm))
        user_orden = int(user_row["orden_bolsa"]) if pd.notna(user_row["orden_bolsa"]) else None
        user_esps = str(user_row.get("especialidades", "") or "")

        if user_orden is None:
            raise HTTPException(status_code=400, detail="El aspirante no tiene 'orden_bolsa' válido en la lista inicial.")

        # 3) Posición del usuario en la lista semanal (si está)
        df_sem = pd.read_sql_query(
            f"SELECT nombre_normalizado, orden_bolsa FROM {tabla_semana}",
            conn
        )
        if df_sem.empty:
            raise HTTPException(status_code=404, detail=f"La tabla '{tabla_semana}' está vacía.")

        df_sem["orden_bolsa"] = pd.to_numeric(df_sem["orden_bolsa"], errors="coerce")
        df_sem = df_sem.dropna(subset=["orden_bolsa"]).sort_values("orden_bolsa").reset_index(drop=True)

        idxs_user = df_sem.index[df_sem["nombre_normalizado"] == user_nom_norm]
        posicion_semana = int(idxs_user[0] + 1) if len(idxs_user) else None  # puede no estar esa semana

        # 4) Calcular 'No Disponibles' por delante
        #    NOTA: aquí excluimos a TODOS los que aparecen en adjudicaciones_total, sin importar fechas
        q_no_disp = f"""
            WITH base AS (
                SELECT nombre_normalizado, orden_bolsa, especialidades
                FROM disponibles_inicio_curso_597
            ),
            dispo_sem AS (
                SELECT DISTINCT nombre_normalizado FROM {tabla_semana}
            ),
            adjudicados AS (
                SELECT DISTINCT nombre_normalizado
                FROM adjudicaciones_total
            ),
            no_disponibles AS (
                SELECT b.*
                FROM base b
                WHERE b.nombre_normalizado NOT IN (SELECT nombre_normalizado FROM dispo_sem)
                  AND b.nombre_normalizado NOT IN (SELECT nombre_normalizado FROM adjudicados)
            )
            SELECT *
            FROM no_disponibles
            WHERE CAST(orden_bolsa AS INTEGER) < ?
            ORDER BY CAST(orden_bolsa AS INTEGER) ASC
        """
        df_no_ahead = pd.read_sql_query(q_no_disp, conn, params=[user_orden])

        # 5) Resumen por especialidad
        if not df_no_ahead.empty:
            df_no_ahead["especialidades"] = df_no_ahead["especialidades"].fillna("")
            exp_vals = []
            for _, r in df_no_ahead.iterrows():
                esps = _split_especialidades(r["especialidades"])
                if not esps:
                    continue
                for e in esps:
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

        # 6) Payload
        detalle = df_no_ahead.rename(columns={
            "nombre_normalizado": "nombre",
            "orden_bolsa": "orden_bolsa"
        }).to_dict(orient="records")

        return {
            "fecha": fecha,
            "tabla_semana": tabla_semana,
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

@app.get("/check_junta")
def check_junta():
    """
    Endpoint de diagnóstico temporal.
    Comprueba si este servidor puede acceder a la web de la Junta de CLM.
    Borrar una vez confirmado.
    """
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



# =============================================================
# SCRAPER — ejecuta el pipeline automático desde GitHub Actions
# =============================================================

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
    cargador    = importlib.import_module("3_Cargar_semana")

    _scraper_log.info("▶ Iniciando scraper en memoria...")

    # 1. Detectar adjudicaciones en portada
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

                # Extraer fecha del nombre si no la tenemos
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

    # 2. Normalizar fecha
    if not fecha_raw:
        _scraper_log.error("✗ No se pudo determinar la fecha.")
        return

    for r in registros_disp:
        if not r.get("fecha"):
            r["fecha"] = fecha_raw
    for r in registros_adj:
        if not r.get("fecha_publicacion"):
            r["fecha_publicacion"] = fecha_raw

    # 3. Guardar CSVs temporales y cargar en BD
    import csv, tempfile
    from pathlib import Path

    CAMPOS_DISP = ["fecha","cod_cuerpo","cuerpo","cod_especialidad","especialidad",
                   "orden","dni","apellidos_nombre","tipo_bolsa","orden_bolsa",
                   "provincias","ingles","frances","aleman","italiano"]
    CAMPOS_ADJ  = ["fecha_publicacion","fecha_inicio_periodo","fecha_fin_periodo",
                   "cod_cuerpo","cuerpo","cod_especialidad","especialidad",
                   "cod_centro","nombre_centro","localidad","dni","apellidos_nombre",
                   "titular","bolsa","posicion","tipo_jornada","fecha_inicio","fecha_fin"]

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

# =============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
