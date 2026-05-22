# -*- coding: utf-8 -*-
"""
3_Cargar_semana.py

Genera la tabla interinos_{fecha} con los aspirantes disponibles esa semana
que NO han sido adjudicados, y vuelca ambos CSVs en sus tablas operativas.

Lógica:
  1. Carga el CSV de disponibles de la semana.
  2. Carga el CSV de adjudicaciones de la semana.
  3. Vuelca disponibles en disponibles_semanales_2025_2026.
  4. Vuelca adjudicaciones en adjudicaciones_2025_2026.
  5. Elimina de disponibles a los adjudicados (cruce DNI + nombre).
  6. Los restantes se vuelcan en interinos_{fecha}.

Cruce:
  - Principal:  DNI + nombre normalizado exacto.
  - Prefijo:    DNI + nombre del CSV es prefijo del nombre en disponibles
                (cubre truncados y nombres compuestos parciales).
  - Normalización: sin tildes, mayúsculas, espacios alrededor de comas normalizados.

Uso:
    python 3_Cargar_semana.py
    (el script pedirá las rutas por consola)
"""

import sqlite3
import unicodedata
import re
import pandas as pd
from pathlib import Path
from datetime import datetime


# =========================================================
# CONFIGURACIÓN
# =========================================================
DB_NAME   = "Base_Bolsa_Docente.db"
ANIO      = 2025
TABLA_ADJ  = f"adjudicaciones_{ANIO}_{ANIO + 1}"
TABLA_DISP = f"disponibles_semanales_{ANIO}_{ANIO + 1}"

CUERPO_MAP = {
    590: '0590', 591: '0591', 592: '0592', 593: '0593',
    594: '0594', 595: '0595', 596: '0596', 597: '0597', 598: '0598',
}


# =========================================================
# UTILIDADES
# =========================================================
def normalizar_nombre(s):
    if not isinstance(s, str):
        return ''
    s = s.upper().strip()
    # Eliminar sufijos de tipo de bolsa: "(>55)", "(+8)", "(>55 +8)", etc.
    s = re.sub(r'\s*\([^)]*\)\s*$', '', s).strip()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'\s*,\s*', ', ', s)  # "VIDALE , X" -> "VIDALE, X"
    return s


def semana_iso(fecha_str):
    try:
        d = datetime.strptime(fecha_str.strip(), '%d/%m/%Y')
        return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
    except Exception:
        return None


def normalizar_cod_especialidad(cod, cod_cuerpo):
    """
    En adjudicaciones el cod_especialidad viene como '590001' (con prefijo de cuerpo).
    En disponibles viene como '1' (solo número).
    Devuelve siempre el número sin prefijo, como entero.
    """
    try:
        cod_str = str(int(cod))
        cuerpo_str = str(int(cod_cuerpo))
        if cod_str.startswith(cuerpo_str):
            cod_str = cod_str[len(cuerpo_str):]
        return int(cod_str)
    except Exception:
        return None


# =========================================================
# CRUCE: construir sets de exclusión desde adjudicaciones
# =========================================================
def construir_sets_exclusion(df_adj):
    """
    Devuelve:
      - exactos:  {(dni, nombre_norm)}
      - prefijos: {dni -> nombre_norm} para cruce por prefijo
    """
    exactos  = set()
    prefijos = {}

    for _, row in df_adj.iterrows():
        dni    = row['dni']
        nombre = row['apellidos_nombre']
        nombre_limpio = normalizar_nombre(
            nombre.rstrip().rstrip(',') if isinstance(nombre, str) else nombre
        )
        exactos.add((dni, nombre_limpio))
        if len(nombre_limpio) >= 10:
            prefijos[dni] = nombre_limpio

    return exactos, prefijos


# =========================================================
# FILTRADO: eliminar adjudicados de disponibles
# =========================================================
def filtrar_no_adjudicados(df_disp, exactos, prefijos):
    mask = []
    for _, row in df_disp.iterrows():
        dni      = row['dni']
        nombre_n = row['nombre_norm']
        if (dni, nombre_n) in exactos:
            mask.append(True)
            continue
        if dni in prefijos and nombre_n.startswith(prefijos[dni]):
            mask.append(True)
            continue
        mask.append(False)
    return df_disp[~pd.Series(mask, index=df_disp.index)].copy()


# =========================================================
# VOLCADO: disponibles_semanales
# =========================================================
def volcar_disponibles(conn, df_disp, fecha_raw, semana, fuente):
    filas = []
    for _, row in df_disp.iterrows():
        filas.append({
            'cuerpo':               CUERPO_MAP.get(int(row['cod_cuerpo']), str(row['cod_cuerpo'])),
            'codigo_especialidad':  str(row['cod_especialidad']),
            'especialidad':         row['especialidad'],
            'dni_ofuscado':         row['dni'],
            'nombre':               row['apellidos_nombre'],
            'orden_bolsa':          row.get('orden_bolsa'),
            'fecha':                fecha_raw,   # fecha completa DD/MM/YYYY
            'semana':               semana,       # semana ISO 2025-W36 (para agrupar)
            'fuente_pdf':           fuente,
            'fecha_carga':          datetime.now().isoformat(timespec='seconds'),
        })
    pd.DataFrame(filas).to_sql(TABLA_DISP, conn, if_exists='append', index=False)
    print(f"  ✓ {len(filas)} filas insertadas en {TABLA_DISP}")


# =========================================================
# VOLCADO: adjudicaciones
# =========================================================
def volcar_adjudicaciones(conn, df_adj, semana, fuente):
    filas = []
    for _, row in df_adj.iterrows():
        cod_cuerpo = int(row['cod_cuerpo'])
        filas.append({
            'cuerpo':               CUERPO_MAP.get(cod_cuerpo, str(cod_cuerpo)),
            'codigo_especialidad':  str(normalizar_cod_especialidad(row['cod_especialidad'], cod_cuerpo)),
            'especialidad':         row['especialidad'],
            'dni_ofuscado':         row['dni'],
            'nombre':               row['apellidos_nombre'],
            'tipo':                 'SEMANAL',
            'centro_destino':       row.get('nombre_centro'),
            'localidad':            row.get('localidad'),
            'municipio':            None,
            'semana':               semana,
            'fecha_adjudicacion':   row.get('fecha_publicacion'),
            'fuente_pdf':           fuente,
            'fecha_carga':          datetime.now().isoformat(timespec='seconds'),
        })
    pd.DataFrame(filas).to_sql(TABLA_ADJ, conn, if_exists='append', index=False)
    print(f"  ✓ {len(filas)} filas insertadas en {TABLA_ADJ}")


# =========================================================
# CREAR TABLA interinos_{fecha}
# =========================================================
def crear_tabla_interinos(conn, df_restantes, tabla, fecha_raw, semana, fuente_disp, fuente_adj):
    cursor = conn.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {tabla};")
    cursor.execute(f"""
    CREATE TABLE {tabla} (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha_referencia    TEXT,
        semana              TEXT,
        fuente_disponibles  TEXT,
        fuente_adjudicaciones TEXT,
        fecha_carga         TEXT,
        cuerpo              TEXT,
        tipo_bolsa          TEXT,
        codigo_especialidad TEXT,
        especialidad        TEXT,
        orden               INTEGER,
        orden_bolsa         INTEGER,
        dni_ofuscado        TEXT,
        nombre              TEXT,
        provincias          TEXT,
        ingles              TEXT,
        frances             TEXT,
        aleman              TEXT,
        italiano            TEXT
    );
    """)
    for col in ['dni_ofuscado', 'cuerpo', 'codigo_especialidad', 'orden_bolsa']:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{tabla}_{col} ON {tabla} ({col});")

    ahora  = datetime.now().isoformat(timespec='seconds')
    df_out = df_restantes.drop(columns=['nombre_norm'], errors='ignore').copy()
    df_out['fecha_referencia']      = fecha_raw
    df_out['semana']                = semana
    df_out['fuente_disponibles']    = fuente_disp
    df_out['fuente_adjudicaciones'] = fuente_adj
    df_out['fecha_carga']           = ahora

    # Renombrar columnas del CSV al esquema de la tabla
    df_out = df_out.rename(columns={
        'cod_cuerpo':        'cuerpo',
        'cod_especialidad':  'codigo_especialidad',
        'tipo_bolsa':        'tipo_bolsa',
        'apellidos_nombre':  'nombre',
        'dni':               'dni_ofuscado',
    })

    # Reordenar columnas al esquema de la tabla
    cols = [
        'fecha_referencia', 'semana', 'fuente_disponibles', 'fuente_adjudicaciones',
        'fecha_carga', 'cuerpo', 'tipo_bolsa', 'codigo_especialidad', 'especialidad',
        'orden', 'orden_bolsa', 'dni_ofuscado', 'nombre',
        'provincias', 'ingles', 'frances', 'aleman', 'italiano',
    ]
    df_out = df_out[[c for c in cols if c in df_out.columns]]
    df_out.to_sql(tabla, conn, if_exists='append', index=False)
    print(f"  ✓ {len(df_out)} filas insertadas en {tabla}")


# =========================================================
# WARNINGS
# =========================================================
def mostrar_y_exportar_warnings(df_adj, df_disp, exactos, prefijos,
                                  fecha_fmt, db_path):
    exactos_disp   = set(zip(df_disp['dni'], df_disp['nombre_norm']))
    dnis_disp      = set(df_disp['dni'])
    dni_a_nombres  = df_disp.groupby('dni')['nombre_norm'].apply(list).to_dict()

    warns_dni_solo = []
    warns_no_enc   = []

    for _, row in df_adj.iterrows():
        dni    = row['dni']
        nombre = row['apellidos_nombre']
        cuerpo = CUERPO_MAP.get(int(row['cod_cuerpo']), str(row['cod_cuerpo']))
        nombre_limpio = normalizar_nombre(
            nombre.rstrip().rstrip(',') if isinstance(nombre, str) else nombre
        )
        k = (dni, nombre_limpio)

        prefijo_ok = (
            len(nombre_limpio) >= 10 and
            any(nb.startswith(nombre_limpio) for nb in dni_a_nombres.get(dni, []))
        )

        if k in exactos_disp or prefijo_ok:
            continue
        elif dni in dnis_disp:
            warns_dni_solo.append((cuerpo, dni, nombre))
        else:
            warns_no_enc.append((cuerpo, dni, nombre))

    total = len(warns_dni_solo) + len(warns_no_enc)

    if total:
        print(f"⚠️  WARNINGS ({total} casos):")
        if warns_dni_solo:
            print(f"\n  DNI_SIN_NOMBRE ({len(warns_dni_solo)}) — DNI en disponibles pero nombre no coincide:")
            for cuerpo, dni, nombre in warns_dni_solo:
                print(f"    [{cuerpo}] {dni}  {nombre}")
        if warns_no_enc:
            print(f"\n  NO_EN_DISPONIBLES ({len(warns_no_enc)}) — adjudicado no aparece en el CSV de disponibles:")
            for cuerpo, dni, nombre in warns_no_enc:
                print(f"    [{cuerpo}] {dni}  {nombre}")
        print()

        txt_path = db_path.parent / f"warnings_{fecha_fmt}.txt"
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(f"WARNINGS DE CARGA SEMANAL — semana {fecha_fmt}\n")
            f.write("=" * 60 + "\n\n")
            if warns_dni_solo:
                f.write(f"DNI_SIN_NOMBRE ({len(warns_dni_solo)}):\n")
                f.write("-" * 60 + "\n")
                for cuerpo, dni, nombre in warns_dni_solo:
                    f.write(f"  [{cuerpo}]  {dni}  {nombre}\n")
                f.write("\n")
            if warns_no_enc:
                f.write(f"NO_EN_DISPONIBLES ({len(warns_no_enc)}):\n")
                f.write("-" * 60 + "\n")
                for cuerpo, dni, nombre in warns_no_enc:
                    f.write(f"  [{cuerpo}]  {dni}  {nombre}\n")
        print(f"📄 Warnings exportados a: {txt_path}\n")
    else:
        print("✅ Sin warnings — todos los adjudicados cruzados correctamente\n")


# =========================================================
# PROCESO PRINCIPAL
# =========================================================
def procesar(disp_path, adj_path, db_path):
    print(f"\n{'='*60}")
    print(f"  Disponibles:    {disp_path.name}")
    print(f"  Adjudicaciones: {adj_path.name}")
    print(f"  Base de datos:  {db_path.name}")
    print(f"{'='*60}\n")

    df_disp = pd.read_csv(disp_path)
    df_adj  = pd.read_csv(adj_path)

    fecha_raw = df_disp['fecha'].iloc[0]
    fecha_fmt = datetime.strptime(fecha_raw.strip(), '%d/%m/%Y').strftime('%Y%m%d')
    semana    = semana_iso(fecha_raw)
    tabla_snap = f"interinos_{fecha_fmt}"

    print(f"Fecha: {fecha_raw}  →  semana {semana}  →  tabla '{tabla_snap}'\n")
    print(f"Disponibles:    {len(df_disp)} filas")
    print(f"Adjudicaciones: {len(df_adj)} filas\n")

    # Normalizar nombres en disponibles
    df_disp['nombre_norm'] = df_disp['apellidos_nombre'].apply(normalizar_nombre)

    # Construir sets de exclusión desde adjudicaciones
    exactos, prefijos = construir_sets_exclusion(df_adj)

    # Filtrar no adjudicados
    df_restantes = filtrar_no_adjudicados(df_disp, exactos, prefijos)
    eliminados   = len(df_disp) - len(df_restantes)

    print(f"Resultado:")
    print(f"  Disponibles:      {len(df_disp)}")
    print(f"  Adjudicados:      {eliminados}")
    print(f"  Quedan en espera: {len(df_restantes)}\n")

    # Resumen por cuerpo
    print(f"{'─'*52}")
    print(f"{'CUERPO':<10} {'DISPONIBLES':>12} {'ADJUDIC':>9} {'EN ESPERA':>10}")
    print(f"{'─'*52}")
    for cod in sorted(df_disp['cod_cuerpo'].unique()):
        total_c  = len(df_disp[df_disp['cod_cuerpo'] == cod])
        espera_c = len(df_restantes[df_restantes['cod_cuerpo'] == cod])
        print(f"{CUERPO_MAP.get(int(cod), str(cod)):<10} {total_c:>12} {total_c - espera_c:>9} {espera_c:>10}")
    print(f"{'─'*52}\n")

    # Warnings
    mostrar_y_exportar_warnings(df_adj, df_disp, exactos, prefijos, fecha_fmt, db_path)

    conn = sqlite3.connect(db_path)

    # Volcar disponibles
    print(f"Volcando en '{TABLA_DISP}'...")
    volcar_disponibles(conn, df_disp, fecha_raw, semana, disp_path.name)

    # Volcar adjudicaciones
    print(f"\nVolcando en '{TABLA_ADJ}'...")
    volcar_adjudicaciones(conn, df_adj, semana, adj_path.name)

    # Crear snapshot
    print(f"\nCreando tabla '{tabla_snap}'...")
    crear_tabla_interinos(conn, df_restantes, tabla_snap,
                          fecha_raw, semana, disp_path.name, adj_path.name)

    conn.commit()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  Disponibles:    {TABLA_DISP}")
    print(f"  Adjudicaciones: {TABLA_ADJ}")
    print(f"  En espera:      {tabla_snap}  ({len(df_restantes)} aspirantes)")
    print(f"{'='*60}\n")


# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    print("=== Carga de adjudicación semanal ===\n")

    # Pedir fecha y buscar los CSVs automáticamente
    while True:
        fecha_inp = input("Fecha de la adjudicación (AAAAMMDD, ej: 20250905): ").strip()
        if not re.fullmatch(r'\d{8}', fecha_inp):
            print("  ✗ Formato incorrecto. Usa AAAAMMDD (ej: 20250905).\n")
            continue

        disp_path = Path(f"disponibles_{fecha_inp}.csv")
        adj_path  = Path(f"adjudicaciones_{fecha_inp}.csv")

        encontrados = []
        if disp_path.exists():
            encontrados.append(f"  ✓ Disponibles:    {disp_path}")
        else:
            encontrados.append(f"  ✗ No encontrado:  {disp_path}")

        if adj_path.exists():
            encontrados.append(f"  ✓ Adjudicaciones: {adj_path}")
        else:
            encontrados.append(f"  ✗ No encontrado:  {adj_path}")

        print("\nArchivos detectados:")
        for l in encontrados:
            print(l)

        if disp_path.exists() and adj_path.exists():
            confirmar = input("\n¿Continuar con estos archivos? [S/n]: ").strip().lower()
            if confirmar in ('', 's', 'si', 'sí', 'y', 'yes'):
                break
            else:
                print()
                continue
        else:
            print("  Uno o más archivos no encontrados. Comprueba que están en el directorio actual.\n")

    db_input = input(f"\nRuta a la base de datos [{DB_NAME}]: ").strip().strip('"').strip("'")
    db_path  = Path(db_input) if db_input else Path(DB_NAME)

    if not db_path.exists():
        print(f"  ✗ Base de datos no encontrada: '{db_path}'.")
        exit(1)

    procesar(disp_path, adj_path, db_path)