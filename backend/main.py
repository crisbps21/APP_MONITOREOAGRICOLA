import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import traceback
import uuid

from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# COMPATIBILIDAD: algunos Python/OpenSSL no aceptan el parámetro
# 'usedforsecurity' que usa reportlab al generar el PDF
# (TypeError: 'usedforsecurity' is an invalid keyword argument for
# openssl_md5()). Este ajuste debe ir ANTES de importar reportlab.
# ---------------------------------------------------------------------------
import hashlib as _hashlib

_md5_original = _hashlib.md5


def _md5_compatible(*args, **kwargs):
    try:
        return _md5_original(*args, **kwargs)
    except TypeError:
        kwargs.pop("usedforsecurity", None)
        return _md5_original(*args, **kwargs)


_hashlib.md5 = _md5_compatible

import numpy as np
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.figure import Figure
from pydantic import BaseModel
from PIL import Image, ImageDraw, ExifTags
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    CondPageBreak,
    HRFlowable,
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from ultralytics import YOLO

try:
    from matplotlib import colormaps as _colormaps

    def obtener_mapa_colores(nombre):
        return _colormaps[nombre]

except ImportError:  # matplotlib antiguo
    from matplotlib import cm as _cm

    def obtener_mapa_colores(nombre):
        return _cm.get_cmap(nombre)


# =========================================================
# SOPORTE PARA IMÁGENES HEIC / HEIF
# =========================================================

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    print(
        "Aviso: pillow-heif no está instalado. "
        "Las imágenes HEIC/HEIF podrían no abrirse."
    )


# =========================================================
# CONFIGURACIÓN
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "best.pt"
UPLOAD_DIR = BASE_DIR / "imagenes_registradas"
DB_PATH = BASE_DIR / "inspecciones.db"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FORMATOS_PERMITIDOS = {
    "JPEG", "PNG", "WEBP", "BMP", "GIF",
    "TIFF", "HEIF", "HEIC"
}

EXTENSIONES_PERMITIDAS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    ".heic", ".heif", ".tif", ".tiff", ".gif"
}

CONFIANZA_MINIMA = 0.5
TAMANO_IMAGEN = 640

SESION_DIAS = 30
LARGO_MAXIMO_NOTA = 1000


# =========================================================
# APLICACIÓN FASTAPI
# =========================================================

app = FastAPI(
    title="Sistema de Detección de Mosca Pinta",
    description=(
        "API para detectar mosca pinta, guardar inspecciones por lotes, "
        "consultar el historial, generar mapas de calor y reportes PDF."
    ),
    version="4.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# MODELO YOLO
# =========================================================

if not MODEL_PATH.exists():
    raise FileNotFoundError(
        f"No se encontró el modelo YOLO: {MODEL_PATH}"
    )

model = YOLO(str(MODEL_PATH))


# =========================================================
# INTERPRETACIÓN DE FECHAS EXIF
# =========================================================

def interpretar_fecha_exif(valor):
    """
    Convierte una fecha EXIF a formato ISO 8601.
    Acepta cadenas y bytes.
    """

    if valor is None:
        return None

    if isinstance(valor, bytes):
        valor = valor.decode("ascii", errors="ignore")

    valor = str(valor).strip().strip("\x00")

    if not valor:
        return None

    formatos = [
        "%Y:%m:%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ]

    for formato in formatos:
        try:
            fecha = datetime.strptime(valor, formato)
            return fecha.isoformat(timespec="seconds")
        except ValueError:
            continue

    try:
        fecha = datetime.fromisoformat(valor)
        return fecha.isoformat(timespec="seconds")
    except ValueError:
        return None


def normalizar_metadatos(contenido):
    """
    Convierte los metadatos almacenados en JSON
    a un diccionario de Python.
    """

    if isinstance(contenido, dict):
        return contenido

    if isinstance(contenido, bytes):
        try:
            contenido = contenido.decode("utf-8")
        except UnicodeDecodeError:
            return {}

    if isinstance(contenido, str):
        try:
            resultado = json.loads(contenido or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}

        return resultado if isinstance(resultado, dict) else {}

    return {}


def obtener_fecha_captura(metadatos):
    """
    Utiliza exclusivamente la fecha original de captura.
    No utiliza la fecha de digitalización ni la fecha
    de análisis como sustitutos.
    """

    fecha_original = interpretar_fecha_exif(
        metadatos.get("fecha_original")
    )

    if fecha_original:
        return fecha_original, True

    return None, False


# =========================================================
# BASE DE DATOS Y MIGRACIÓN
# =========================================================

def obtener_conexion():
    conexion = sqlite3.connect(DB_PATH)
    conexion.row_factory = sqlite3.Row
    return conexion


def agregar_columna_si_falta(conexion, tabla, columna, definicion):
    columnas = {
        fila["name"]
        for fila in conexion.execute(
            f"PRAGMA table_info({tabla})"
        ).fetchall()
    }

    if columna not in columnas:
        conexion.execute(
            f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"
        )


def inicializar_base_datos():
    """
    Crea las tablas si no existen y migra bases de datos
    anteriores. Conserva los registros existentes.
    """

    with obtener_conexion() as conexion:

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS inspecciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_analisis TEXT NOT NULL,
                nombre_imagen TEXT,
                archivo_guardado TEXT NOT NULL,
                metadatos TEXT NOT NULL,
                cantidad INTEGER NOT NULL,
                confianza_promedio REAL NOT NULL,
                detecciones TEXT NOT NULL,
                fecha_captura TEXT,
                lote_id INTEGER,
                usuario_id INTEGER,
                nota TEXT
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS lotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_analisis TEXT NOT NULL,
                total_imagenes INTEGER NOT NULL,
                total_detecciones INTEGER NOT NULL,
                confianza_promedio REAL NOT NULL,
                usuario_id INTEGER
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre_usuario TEXT NOT NULL UNIQUE,
                nombre_completo TEXT,
                sal TEXT NOT NULL,
                hash_contrasena TEXT NOT NULL,
                creado TEXT NOT NULL
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS registros_pendientes (
                correo TEXT PRIMARY KEY,
                nombre_usuario TEXT NOT NULL,
                nombre_completo TEXT,
                sal TEXT NOT NULL,
                hash_contrasena TEXT NOT NULL,
                codigo_sal TEXT NOT NULL,
                codigo_hash TEXT NOT NULL,
                expira TEXT NOT NULL,
                intentos INTEGER NOT NULL DEFAULT 0,
                ultimo_envio TEXT NOT NULL,
                creado TEXT NOT NULL
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS recuperaciones (
                correo TEXT PRIMARY KEY,
                codigo_sal TEXT NOT NULL,
                codigo_hash TEXT NOT NULL,
                expira TEXT NOT NULL,
                intentos INTEGER NOT NULL DEFAULT 0,
                ultimo_envio TEXT NOT NULL
            )
        """)

        conexion.execute("""
            CREATE TABLE IF NOT EXISTS sesiones (
                token TEXT PRIMARY KEY,
                usuario_id INTEGER NOT NULL,
                creado TEXT NOT NULL,
                expira TEXT NOT NULL
            )
        """)

        # Migraciones de bases de datos anteriores
        agregar_columna_si_falta(
            conexion, "inspecciones", "fecha_captura", "TEXT"
        )
        agregar_columna_si_falta(
            conexion, "inspecciones", "lote_id", "INTEGER"
        )
        agregar_columna_si_falta(
            conexion, "inspecciones", "usuario_id", "INTEGER"
        )
        agregar_columna_si_falta(
            conexion, "inspecciones", "nota", "TEXT"
        )
        agregar_columna_si_falta(
            conexion, "lotes", "usuario_id", "INTEGER"
        )
        agregar_columna_si_falta(
            conexion, "lotes", "nombre", "TEXT"
        )
        agregar_columna_si_falta(
            conexion, "usuarios", "correo", "TEXT"
        )
        conexion.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_correo "
            "ON usuarios (lower(correo)) WHERE correo IS NOT NULL"
        )

        filas = conexion.execute("""
            SELECT id, metadatos, fecha_captura
            FROM inspecciones
        """).fetchall()

        for fila in filas:

            if fila["fecha_captura"]:
                continue

            metadatos = normalizar_metadatos(fila["metadatos"])

            fecha_original = interpretar_fecha_exif(
                metadatos.get("fecha_original")
            )

            if fecha_original:
                metadatos["fecha_captura_disponible"] = True

                conexion.execute("""
                    UPDATE inspecciones
                    SET fecha_captura = ?, metadatos = ?
                    WHERE id = ?
                """, (
                    fecha_original,
                    json.dumps(metadatos, ensure_ascii=False),
                    fila["id"]
                ))

            else:
                metadatos["fecha_captura_disponible"] = False

                conexion.execute("""
                    UPDATE inspecciones
                    SET metadatos = ?
                    WHERE id = ?
                """, (
                    json.dumps(metadatos, ensure_ascii=False),
                    fila["id"]
                ))

        conexion.commit()


inicializar_base_datos()


# =========================================================
# AUTENTICACIÓN (usuarios y sesiones)
# =========================================================

class CredencialesRegistro(BaseModel):
    nombre_usuario: str
    correo: str
    contrasena: str
    nombre_completo: Optional[str] = None


class CredencialesLogin(BaseModel):
    nombre_usuario: str      # también acepta el correo
    contrasena: str


class VerificacionCorreo(BaseModel):
    correo: str
    codigo: str


class ReenvioCodigo(BaseModel):
    correo: str


class SolicitudRecuperacion(BaseModel):
    correo: str


class RestablecerContrasena(BaseModel):
    correo: str
    codigo: str
    contrasena_nueva: str


class CambiarContrasena(BaseModel):
    contrasena_actual: str
    contrasena_nueva: str


def calcular_hash(contrasena: str, sal: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        contrasena.encode("utf-8"),
        sal,
        200_000
    ).hex()


def crear_sesion(conexion, usuario_id: int) -> str:
    token = secrets.token_urlsafe(32)
    ahora = datetime.now()
    expira = ahora + timedelta(days=SESION_DIAS)

    conexion.execute(
        """
        INSERT INTO sesiones (token, usuario_id, creado, expira)
        VALUES (?, ?, ?, ?)
        """,
        (
            token,
            usuario_id,
            ahora.isoformat(timespec="seconds"),
            expira.isoformat(timespec="seconds"),
        )
    )

    return token


def usuario_actual(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
) -> dict:
    """
    Obtiene el usuario de la sesión. Acepta el encabezado
    'Authorization: Bearer <token>' o el parámetro ?token=
    (este último se usa para descargar el PDF).
    """

    valor = None

    if authorization and authorization.lower().startswith("bearer "):
        valor = authorization[7:].strip()
    elif token:
        valor = token.strip()

    if not valor:
        raise HTTPException(
            status_code=401,
            detail="Debes iniciar sesión."
        )

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            """
            SELECT u.id, u.nombre_usuario, u.nombre_completo, s.expira
            FROM sesiones s
            JOIN usuarios u ON u.id = s.usuario_id
            WHERE s.token = ?
            """,
            (valor,)
        ).fetchone()

        if fila is None:
            raise HTTPException(
                status_code=401,
                detail="Sesión no válida."
            )

        if fila["expira"] < datetime.now().isoformat(timespec="seconds"):
            conexion.execute(
                "DELETE FROM sesiones WHERE token = ?", (valor,)
            )
            conexion.commit()

            raise HTTPException(
                status_code=401,
                detail="La sesión expiró."
            )

    return {
        "id": fila["id"],
        "nombre_usuario": fila["nombre_usuario"],
        "nombre_completo": fila["nombre_completo"],
        "token": valor,
    }


def datos_publicos_usuario(usuario: dict) -> dict:
    return {
        "id": usuario["id"],
        "nombre_usuario": usuario["nombre_usuario"],
        "nombre_completo": usuario.get("nombre_completo"),
    }


# ---------------------------------------------------------
# Reglas de usuario, correo y contraseña
# ---------------------------------------------------------

CODIGO_MINUTOS = 10          # vigencia del código de verificación
CODIGO_MAX_INTENTOS = 5      # intentos fallidos permitidos por código
CODIGO_ESPERA_SEGUNDOS = 60  # espera mínima entre envíos

PATRON_USUARIO = re.compile(r"^[a-z0-9._-]{3,30}$")
PATRON_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

CONTRASENAS_COMUNES = {
    "12345678", "123456789", "1234567890", "password", "password1",
    "contrasena", "contraseña", "qwertyui", "qwerty123", "abc12345",
    "11111111", "00000000", "iloveyou", "admin123", "mosca123",
}


def normalizar_correo(valor: str) -> str:
    return (valor or "").strip().lower()


def validar_contrasena(contrasena: str, usuario: str = "", correo: str = ""):
    """Devuelve la lista de requisitos que NO cumple la contraseña."""

    faltan = []

    if len(contrasena) < 8:
        faltan.append("al menos 8 caracteres")
    if len(contrasena) > 72:
        faltan.append("máximo 72 caracteres")
    if not any(c.isupper() for c in contrasena):
        faltan.append("una letra mayúscula")
    if not any(c.islower() for c in contrasena):
        faltan.append("una letra minúscula")
    if not any(c.isdigit() for c in contrasena):
        faltan.append("un número")
    if not any(not c.isalnum() and not c.isspace() for c in contrasena):
        faltan.append("un símbolo (por ejemplo ! @ # $ % &)")
    if any(c.isspace() for c in contrasena):
        faltan.append("sin espacios")

    minuscula = contrasena.lower()

    if minuscula in CONTRASENAS_COMUNES:
        faltan.append("no ser una contraseña muy común")

    if usuario and len(usuario) >= 3 and usuario in minuscula:
        faltan.append("no contener tu nombre de usuario")

    parte_correo = correo.split("@")[0] if correo else ""

    if len(parte_correo) >= 4 and parte_correo in minuscula:
        faltan.append("no contener tu correo")

    return faltan


def exigir_contrasena_valida(contrasena, usuario="", correo=""):
    faltan = validar_contrasena(contrasena, usuario, correo)

    if faltan:
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe tener: " + "; ".join(faltan) + "."
        )


def ocultar_correo(correo: str) -> str:
    """ana.perez@gmail.com -> an***@gmail.com"""

    usuario, _, dominio = correo.partition("@")
    visible = usuario[:2]
    return f"{visible}{'*' * max(1, len(usuario) - 2)}@{dominio}"


# ---------------------------------------------------------
# Envío de correo (SMTP)
# ---------------------------------------------------------
# Configura el envío con variables de entorno o con el archivo
# correo_config.json junto a main.py (ver correo_config.ejemplo.json):
#   MOSCA_SMTP_HOST, MOSCA_SMTP_PORT, MOSCA_SMTP_USER,
#   MOSCA_SMTP_PASSWORD, MOSCA_SMTP_FROM
# Si no hay configuración, el código se imprime en la terminal del
# servidor (modo de pruebas).

ARCHIVO_CONFIG_CORREO = BASE_DIR / "correo_config.json"


def configuracion_correo():
    """Devuelve la configuración SMTP o None si no está configurada."""

    datos = {}

    if ARCHIVO_CONFIG_CORREO.is_file():
        try:
            datos = json.loads(
                ARCHIVO_CONFIG_CORREO.read_text(encoding="utf-8")
            )
        except Exception as error:
            print(f"Aviso: correo_config.json no es válido: {error}")

    def valor(clave_env, clave_json, defecto=""):
        return (
            os.environ.get(clave_env)
            or str(datos.get(clave_json) or "")
            or defecto
        ).strip()

    host = valor("MOSCA_SMTP_HOST", "host")
    usuario = valor("MOSCA_SMTP_USER", "usuario")
    clave = valor("MOSCA_SMTP_PASSWORD", "contrasena").replace(" ", "")

    if not host or not usuario or not clave:
        return None

    try:
        puerto = int(valor("MOSCA_SMTP_PORT", "puerto", "587"))
    except ValueError:
        puerto = 587

    return {
        "host": host,
        "puerto": puerto,
        "usuario": usuario,
        "contrasena": clave,
        "remitente": valor("MOSCA_SMTP_FROM", "remitente", usuario),
    }


def enviar_codigo_por_correo(
    correo: str, codigo: str, nombre: str, motivo: str = "registro"
) -> bool:
    """
    Envía el código. Devuelve True si se envió por correo, o False si el
    servidor no tiene correo configurado (modo de pruebas: se imprime en
    la terminal). Lanza HTTPException si el envío falla.
    """

    config = configuracion_correo()

    if config is None:
        print("=" * 60)
        print("MODO DE PRUEBAS (correo no configurado)")
        print(f"Código ({motivo}) para {correo}: {codigo}")
        print("=" * 60)
        return False

    mensaje = EmailMessage()
    recuperacion = motivo == "recuperacion"
    accion = (
        "recuperar tu contraseña de Mosca Pinta"
        if recuperacion else "activar tu cuenta de Mosca Pinta"
    )
    mensaje["Subject"] = (
        f"Código para recuperar tu contraseña: {codigo}"
        if recuperacion else f"Tu código de verificación: {codigo}"
    )
    mensaje["From"] = config["remitente"]
    mensaje["To"] = correo

    saludo = f"Hola {nombre}," if nombre else "Hola,"

    mensaje.set_content(
        f"{saludo}\n\n"
        f"Tu código para {accion} es: {codigo}\n\n"
        f"Es válido por {CODIGO_MINUTOS} minutos. Si no fuiste tú, "
        f"ignora este mensaje.\n"
    )
    mensaje.add_alternative(
        f"""<div style="font-family:Arial,sans-serif;max-width:420px;margin:auto">
<h2 style="color:#2E7D32">Mosca Pinta</h2>
<p>{escape(saludo)}</p>
<p>Tu código para {escape(accion)} es:</p>
<p style="font-size:34px;letter-spacing:8px;font-weight:bold;
background:#E8F5E9;padding:14px;text-align:center;border-radius:8px">{codigo}</p>
<p style="color:#555">Es válido por {CODIGO_MINUTOS} minutos.
Si no fuiste tú, ignora este mensaje.</p></div>""",
        subtype="html"
    )

    try:
        if config["puerto"] == 465:
            with smtplib.SMTP_SSL(
                config["host"], config["puerto"], timeout=20
            ) as servidor:
                servidor.login(config["usuario"], config["contrasena"])
                servidor.send_message(mensaje)
        else:
            with smtplib.SMTP(
                config["host"], config["puerto"], timeout=20
            ) as servidor:
                servidor.starttls()
                servidor.login(config["usuario"], config["contrasena"])
                servidor.send_message(mensaje)

    except smtplib.SMTPAuthenticationError:
        print("ERROR SMTP: usuario o contraseña de aplicación incorrectos.")
        raise HTTPException(
            status_code=502,
            detail=(
                "El servidor no pudo iniciar sesión en su correo emisor. "
                "Revisa correo_config.json (contraseña de aplicación)."
            )
        )
    except Exception as error:
        print(f"ERROR SMTP: {type(error).__name__}: {error}")
        raise HTTPException(
            status_code=502,
            detail=(
                "No se pudo enviar el correo de verificación. Revisa la "
                "conexión a internet y la configuración del correo."
            )
        )

    return True


def generar_codigo():
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_codigo(codigo: str, sal: str) -> str:
    return hashlib.sha256((sal + codigo).encode("utf-8")).hexdigest()


def segundos_desde(texto_iso: str) -> float:
    try:
        return (datetime.now() - datetime.fromisoformat(texto_iso)).total_seconds()
    except ValueError:
        return 1e9


@app.post("/auth/registro")
def registrar_usuario(datos: CredencialesRegistro):
    """
    Paso 1 del registro: valida los datos y envía un código al correo.
    La cuenta se crea hasta confirmar el código (/auth/verificar).
    """

    nombre = datos.nombre_usuario.strip().lower()
    correo = normalizar_correo(datos.correo)
    nombre_completo = (datos.nombre_completo or "").strip()[:80] or None

    if not PATRON_USUARIO.match(nombre):
        raise HTTPException(
            status_code=400,
            detail=(
                "El usuario debe tener de 3 a 30 caracteres: letras, "
                "números, punto, guion o guion bajo (sin espacios)."
            )
        )

    if not PATRON_CORREO.match(correo) or len(correo) > 120:
        raise HTTPException(
            status_code=400,
            detail="Escribe un correo electrónico válido."
        )

    exigir_contrasena_valida(datos.contrasena, nombre, correo)

    sal = secrets.token_bytes(16)
    hash_contrasena = calcular_hash(datos.contrasena, sal)

    codigo = generar_codigo()
    codigo_sal = secrets.token_hex(8)
    ahora = datetime.now()

    with obtener_conexion() as conexion:
        if conexion.execute(
            "SELECT id FROM usuarios WHERE nombre_usuario = ?", (nombre,)
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Ese nombre de usuario ya está en uso."
            )

        if conexion.execute(
            "SELECT id FROM usuarios WHERE lower(correo) = ?", (correo,)
        ).fetchone():
            raise HTTPException(
                status_code=409,
                detail="Ese correo ya está registrado. Inicia sesión."
            )

        previo = conexion.execute(
            "SELECT ultimo_envio FROM registros_pendientes WHERE correo = ?",
            (correo,)
        ).fetchone()

        if previo and segundos_desde(previo["ultimo_envio"]) < CODIGO_ESPERA_SEGUNDOS:
            raise HTTPException(
                status_code=429,
                detail=(
                    "Ya se envió un código a ese correo. Espera un minuto "
                    "para pedir otro."
                )
            )

        conexion.execute(
            """
            INSERT OR REPLACE INTO registros_pendientes (
                correo, nombre_usuario, nombre_completo, sal,
                hash_contrasena, codigo_sal, codigo_hash, expira,
                intentos, ultimo_envio, creado
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                correo, nombre, nombre_completo, sal.hex(),
                hash_contrasena, codigo_sal,
                hash_codigo(codigo, codigo_sal),
                (ahora + timedelta(minutes=CODIGO_MINUTOS)).isoformat(
                    timespec="seconds"
                ),
                ahora.isoformat(timespec="seconds"),
                ahora.isoformat(timespec="seconds"),
            )
        )
        conexion.commit()

    try:
        enviado = enviar_codigo_por_correo(correo, codigo, nombre_completo or nombre)
    except HTTPException:
        with obtener_conexion() as conexion:
            conexion.execute(
                "DELETE FROM registros_pendientes WHERE correo = ?", (correo,)
            )
            conexion.commit()
        raise

    return {
        "requiere_verificacion": True,
        "correo": correo,
        "correo_oculto": ocultar_correo(correo),
        "minutos": CODIGO_MINUTOS,
        "correo_enviado": enviado,
    }


@app.post("/auth/reenviar-codigo")
def reenviar_codigo(datos: ReenvioCodigo):
    correo = normalizar_correo(datos.correo)

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM registros_pendientes WHERE correo = ?", (correo,)
        ).fetchone()

        if fila is None:
            raise HTTPException(
                status_code=404,
                detail="No hay un registro pendiente para ese correo. Regístrate de nuevo."
            )

        espera = CODIGO_ESPERA_SEGUNDOS - segundos_desde(fila["ultimo_envio"])

        if espera > 0:
            raise HTTPException(
                status_code=429,
                detail=f"Espera {int(espera) + 1} segundos para pedir otro código."
            )

        codigo = generar_codigo()
        codigo_sal = secrets.token_hex(8)
        ahora = datetime.now()

        conexion.execute(
            """
            UPDATE registros_pendientes
            SET codigo_sal = ?, codigo_hash = ?, expira = ?,
                intentos = 0, ultimo_envio = ?
            WHERE correo = ?
            """,
            (
                codigo_sal, hash_codigo(codigo, codigo_sal),
                (ahora + timedelta(minutes=CODIGO_MINUTOS)).isoformat(
                    timespec="seconds"
                ),
                ahora.isoformat(timespec="seconds"),
                correo,
            )
        )
        conexion.commit()

        nombre_mostrar = fila["nombre_completo"] or fila["nombre_usuario"]

    enviado = enviar_codigo_por_correo(correo, codigo, nombre_mostrar)

    return {
        "correo": correo,
        "correo_oculto": ocultar_correo(correo),
        "minutos": CODIGO_MINUTOS,
        "correo_enviado": enviado,
    }


@app.post("/auth/verificar")
def verificar_correo(datos: VerificacionCorreo):
    """Paso 2 del registro: confirma el código y crea la cuenta."""

    correo = normalizar_correo(datos.correo)
    codigo = "".join(c for c in (datos.codigo or "") if c.isdigit())

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM registros_pendientes WHERE correo = ?", (correo,)
        ).fetchone()

        if fila is None:
            raise HTTPException(
                status_code=404,
                detail="No hay un registro pendiente para ese correo. Regístrate de nuevo."
            )

        if fila["expira"] < datetime.now().isoformat(timespec="seconds"):
            raise HTTPException(
                status_code=400,
                detail="El código expiró. Pide uno nuevo."
            )

        if fila["intentos"] >= CODIGO_MAX_INTENTOS:
            raise HTTPException(
                status_code=429,
                detail="Demasiados intentos fallidos. Pide un código nuevo."
            )

        correcto = hmac.compare_digest(
            hash_codigo(codigo, fila["codigo_sal"]), fila["codigo_hash"]
        )

        if not correcto:
            conexion.execute(
                "UPDATE registros_pendientes SET intentos = intentos + 1 "
                "WHERE correo = ?",
                (correo,)
            )
            conexion.commit()

            restantes = CODIGO_MAX_INTENTOS - fila["intentos"] - 1

            raise HTTPException(
                status_code=400,
                detail=(
                    "Código incorrecto."
                    + (
                        f" Te quedan {restantes} intento(s)."
                        if restantes > 0 else " Pide un código nuevo."
                    )
                )
            )

        # Código correcto: se crea la cuenta.
        if conexion.execute(
            "SELECT id FROM usuarios WHERE nombre_usuario = ? "
            "OR lower(correo) = ?",
            (fila["nombre_usuario"], correo)
        ).fetchone():
            conexion.execute(
                "DELETE FROM registros_pendientes WHERE correo = ?", (correo,)
            )
            conexion.commit()
            raise HTTPException(
                status_code=409,
                detail="Ese usuario o correo ya fue registrado. Inicia sesión."
            )

        total_usuarios = conexion.execute(
            "SELECT COUNT(*) AS total FROM usuarios"
        ).fetchone()["total"]

        cursor = conexion.execute(
            """
            INSERT INTO usuarios (
                nombre_usuario, nombre_completo, sal,
                hash_contrasena, creado, correo
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fila["nombre_usuario"],
                fila["nombre_completo"],
                fila["sal"],
                fila["hash_contrasena"],
                datetime.now().isoformat(timespec="seconds"),
                correo,
            )
        )

        usuario_id = cursor.lastrowid

        # El primer usuario hereda los análisis anteriores
        # (hechos antes de existir el inicio de sesión).
        if total_usuarios == 0:
            conexion.execute(
                "UPDATE inspecciones SET usuario_id = ? "
                "WHERE usuario_id IS NULL",
                (usuario_id,)
            )
            conexion.execute(
                "UPDATE lotes SET usuario_id = ? "
                "WHERE usuario_id IS NULL",
                (usuario_id,)
            )

        conexion.execute(
            "DELETE FROM registros_pendientes WHERE correo = ?", (correo,)
        )

        token = crear_sesion(conexion, usuario_id)
        conexion.commit()

    return {
        "token": token,
        "usuario": {
            "id": usuario_id,
            "nombre_usuario": fila["nombre_usuario"],
            "nombre_completo": fila["nombre_completo"],
        },
    }


@app.post("/auth/login")
def iniciar_sesion(datos: CredencialesLogin):
    nombre = datos.nombre_usuario.strip().lower()

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios WHERE nombre_usuario = ? "
            "OR lower(correo) = ?",
            (nombre, nombre)
        ).fetchone()

        valido = False

        if fila is not None:
            hash_calculado = calcular_hash(
                datos.contrasena,
                bytes.fromhex(fila["sal"])
            )
            valido = hmac.compare_digest(
                hash_calculado,
                fila["hash_contrasena"]
            )

        if not valido:
            raise HTTPException(
                status_code=401,
                detail="Usuario o contraseña incorrectos."
            )

        token = crear_sesion(conexion, fila["id"])
        conexion.commit()

    return {
        "token": token,
        "usuario": {
            "id": fila["id"],
            "nombre_usuario": fila["nombre_usuario"],
            "nombre_completo": fila["nombre_completo"],
        },
    }


# ---------------------------------------------------------
# Recuperar y cambiar contraseña
# ---------------------------------------------------------

def cambiar_contrasena_usuario(conexion, usuario_id, contrasena, conservar_token=None):
    sal = secrets.token_bytes(16)

    conexion.execute(
        "UPDATE usuarios SET sal = ?, hash_contrasena = ? WHERE id = ?",
        (sal.hex(), calcular_hash(contrasena, sal), usuario_id)
    )

    # Cierra las demás sesiones: si alguien más la tenía abierta, se sale.
    if conservar_token:
        conexion.execute(
            "DELETE FROM sesiones WHERE usuario_id = ? AND token != ?",
            (usuario_id, conservar_token)
        )
    else:
        conexion.execute(
            "DELETE FROM sesiones WHERE usuario_id = ?", (usuario_id,)
        )


@app.post("/auth/olvide-contrasena")
def olvide_contrasena(datos: SolicitudRecuperacion):
    """
    Envía un código al correo si existe una cuenta con él. La respuesta
    es siempre la misma, para no revelar qué correos están registrados.
    """

    correo = normalizar_correo(datos.correo)

    respuesta = {
        "mensaje": (
            "Si el correo está registrado, te enviamos un código de "
            "recuperación."
        ),
        "minutos": CODIGO_MINUTOS,
        "correo_enviado": True,
    }

    if not PATRON_CORREO.match(correo):
        raise HTTPException(
            status_code=400,
            detail="Escribe un correo electrónico válido."
        )

    with obtener_conexion() as conexion:
        usuario = conexion.execute(
            "SELECT id, nombre_usuario, nombre_completo FROM usuarios "
            "WHERE lower(correo) = ?",
            (correo,)
        ).fetchone()

        if usuario is None:
            return respuesta

        previo = conexion.execute(
            "SELECT ultimo_envio FROM recuperaciones WHERE correo = ?",
            (correo,)
        ).fetchone()

        if previo and segundos_desde(previo["ultimo_envio"]) < CODIGO_ESPERA_SEGUNDOS:
            return respuesta

        codigo = generar_codigo()
        codigo_sal = secrets.token_hex(8)
        ahora = datetime.now()

        conexion.execute(
            """
            INSERT OR REPLACE INTO recuperaciones (
                correo, codigo_sal, codigo_hash, expira, intentos,
                ultimo_envio
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                correo, codigo_sal, hash_codigo(codigo, codigo_sal),
                (ahora + timedelta(minutes=CODIGO_MINUTOS)).isoformat(
                    timespec="seconds"
                ),
                ahora.isoformat(timespec="seconds"),
            )
        )
        conexion.commit()

        nombre = usuario["nombre_completo"] or usuario["nombre_usuario"]

    try:
        respuesta["correo_enviado"] = enviar_codigo_por_correo(
            correo, codigo, nombre, motivo="recuperacion"
        )
    except HTTPException:
        with obtener_conexion() as conexion:
            conexion.execute(
                "DELETE FROM recuperaciones WHERE correo = ?", (correo,)
            )
            conexion.commit()
        raise

    return respuesta


@app.post("/auth/restablecer-contrasena")
def restablecer_contrasena(datos: RestablecerContrasena):
    correo = normalizar_correo(datos.correo)
    codigo = "".join(c for c in (datos.codigo or "") if c.isdigit())

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM recuperaciones WHERE correo = ?", (correo,)
        ).fetchone()

        usuario = conexion.execute(
            "SELECT id, nombre_usuario FROM usuarios WHERE lower(correo) = ?",
            (correo,)
        ).fetchone()

        if fila is None or usuario is None:
            raise HTTPException(
                status_code=400,
                detail="Código incorrecto o vencido. Pide uno nuevo."
            )

        if fila["expira"] < datetime.now().isoformat(timespec="seconds"):
            raise HTTPException(
                status_code=400,
                detail="El código expiró. Pide uno nuevo."
            )

        if fila["intentos"] >= CODIGO_MAX_INTENTOS:
            raise HTTPException(
                status_code=429,
                detail="Demasiados intentos fallidos. Pide un código nuevo."
            )

        if not hmac.compare_digest(
            hash_codigo(codigo, fila["codigo_sal"]), fila["codigo_hash"]
        ):
            conexion.execute(
                "UPDATE recuperaciones SET intentos = intentos + 1 "
                "WHERE correo = ?",
                (correo,)
            )
            conexion.commit()

            restantes = CODIGO_MAX_INTENTOS - fila["intentos"] - 1

            raise HTTPException(
                status_code=400,
                detail=(
                    "Código incorrecto."
                    + (
                        f" Te quedan {restantes} intento(s)."
                        if restantes > 0 else " Pide un código nuevo."
                    )
                )
            )

        exigir_contrasena_valida(
            datos.contrasena_nueva, usuario["nombre_usuario"], correo
        )

        cambiar_contrasena_usuario(
            conexion, usuario["id"], datos.contrasena_nueva
        )
        conexion.execute(
            "DELETE FROM recuperaciones WHERE correo = ?", (correo,)
        )
        conexion.commit()

    return {"mensaje": "Contraseña actualizada. Ya puedes iniciar sesión."}


@app.post("/auth/cambiar-contrasena")
def cambiar_contrasena(
    datos: CambiarContrasena,
    usuario: dict = Depends(usuario_actual)
):
    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM usuarios WHERE id = ?", (usuario["id"],)
        ).fetchone()

        if not hmac.compare_digest(
            calcular_hash(datos.contrasena_actual, bytes.fromhex(fila["sal"])),
            fila["hash_contrasena"]
        ):
            raise HTTPException(
                status_code=400,
                detail="La contraseña actual es incorrecta."
            )

        if datos.contrasena_nueva == datos.contrasena_actual:
            raise HTTPException(
                status_code=400,
                detail="La contraseña nueva debe ser distinta de la actual."
            )

        exigir_contrasena_valida(
            datos.contrasena_nueva, fila["nombre_usuario"],
            fila["correo"] or ""
        )

        cambiar_contrasena_usuario(
            conexion, usuario["id"], datos.contrasena_nueva,
            conservar_token=usuario["token"]
        )
        conexion.commit()

    return {"mensaje": "Contraseña actualizada."}


@app.get("/auth/yo")
def consultar_sesion(usuario: dict = Depends(usuario_actual)):
    return {"usuario": datos_publicos_usuario(usuario)}


@app.post("/auth/logout")
def cerrar_sesion(usuario: dict = Depends(usuario_actual)):
    with obtener_conexion() as conexion:
        conexion.execute(
            "DELETE FROM sesiones WHERE token = ?",
            (usuario["token"],)
        )
        conexion.commit()

    return {"mensaje": "Sesión cerrada."}


# =========================================================
# METADATOS DE IMAGEN Y GPS
# =========================================================

def convertir_a_numero(valor):
    try:
        if hasattr(valor, "numerator") and hasattr(
            valor, "denominator"
        ):
            if valor.denominator == 0:
                return None

            numero = (
                float(valor.numerator)
                / float(valor.denominator)
            )
        else:
            numero = float(valor)

        return numero if math.isfinite(numero) else None

    except (TypeError, ValueError, ZeroDivisionError):
        return None


def convertir_coordenada_gps(valor):
    try:
        if valor is None or len(valor) < 3:
            return None

        grados = convertir_a_numero(valor[0])
        minutos = convertir_a_numero(valor[1])
        segundos = convertir_a_numero(valor[2])

        if (
            grados is None
            or minutos is None
            or segundos is None
        ):
            return None

        return grados + minutos / 60.0 + segundos / 3600.0

    except (TypeError, ValueError, IndexError):
        return None


def etiquetas_exif(imagen):
    """
    Lee las etiquetas EXIF principales y el subdirectorio
    EXIF, donde normalmente se encuentra DateTimeOriginal.
    """

    etiquetas = {}

    try:
        exif = imagen.getexif()
    except Exception as error:
        print(f"No se pudo leer EXIF: {error}")
        return etiquetas

    if not exif:
        return etiquetas

    for clave, valor in exif.items():
        nombre = ExifTags.TAGS.get(clave, str(clave))
        etiquetas[nombre] = valor

    try:
        exif_interno = exif.get_ifd(34665)

        for clave, valor in exif_interno.items():
            nombre = ExifTags.TAGS.get(clave, str(clave))
            etiquetas[nombre] = valor

    except (AttributeError, KeyError, TypeError, ValueError) as error:
        print(f"Aviso al leer subdirectorio EXIF: {error}")

    return etiquetas


def extraer_metadatos(imagen, bytes_imagen):
    metadatos = {
        "fecha_original": None,
        "fecha_digitalizacion": None,
        "marca": None,
        "modelo": None,
        "orientacion": None,
        "gps_latitud": None,
        "gps_longitud": None,
        "formato": imagen.format,
        "ancho_px": imagen.width,
        "alto_px": imagen.height,
        "tamano_bytes": len(bytes_imagen),
    }

    etiquetas = etiquetas_exif(imagen)

    if not etiquetas:
        return metadatos

    fecha_original = (
        etiquetas.get("DateTimeOriginal")
        or etiquetas.get("36867")
    )

    fecha_digitalizacion = (
        etiquetas.get("DateTimeDigitized")
        or etiquetas.get("36868")
    )

    metadatos["fecha_original"] = interpretar_fecha_exif(
        fecha_original
    )

    metadatos["fecha_digitalizacion"] = interpretar_fecha_exif(
        fecha_digitalizacion
    )

    metadatos["marca"] = etiquetas.get("Make")
    metadatos["modelo"] = etiquetas.get("Model")
    metadatos["orientacion"] = etiquetas.get("Orientation")

    try:
        exif = imagen.getexif()
        gps_exif = exif.get_ifd(34853)

        gps = {
            ExifTags.GPSTAGS.get(clave, str(clave)): valor
            for clave, valor in gps_exif.items()
        }

        latitud = convertir_coordenada_gps(gps.get("GPSLatitude"))
        longitud = convertir_coordenada_gps(gps.get("GPSLongitude"))

        referencia_latitud = gps.get("GPSLatitudeRef")
        referencia_longitud = gps.get("GPSLongitudeRef")

        if isinstance(referencia_latitud, bytes):
            referencia_latitud = referencia_latitud.decode(
                "ascii", errors="ignore"
            )

        if isinstance(referencia_longitud, bytes):
            referencia_longitud = referencia_longitud.decode(
                "ascii", errors="ignore"
            )

        if latitud is not None:
            if str(referencia_latitud).upper().startswith("S"):
                latitud = -abs(latitud)

            metadatos["gps_latitud"] = latitud

        if longitud is not None:
            if str(referencia_longitud).upper().startswith("W"):
                longitud = -abs(longitud)

            metadatos["gps_longitud"] = longitud

    except (AttributeError, KeyError, TypeError, ValueError) as error:
        print(f"Aviso al leer GPS: {error}")

    return metadatos


def validar_coordenadas(latitud, longitud):
    try:
        latitud = float(latitud)
        longitud = float(longitud)

        return (
            math.isfinite(latitud)
            and math.isfinite(longitud)
            and -90 <= latitud <= 90
            and -180 <= longitud <= 180
        )

    except (TypeError, ValueError):
        return False


# =========================================================
# SERIALIZACIÓN Y CONSULTA DE REGISTROS
# =========================================================

def serializar_registro(fila):
    if fila is None:
        return None

    registro = dict(fila)

    metadatos = normalizar_metadatos(registro.get("metadatos"))

    detecciones = registro.get("detecciones")

    if isinstance(detecciones, str):
        try:
            detecciones = json.loads(detecciones or "[]")
        except (json.JSONDecodeError, TypeError, ValueError):
            detecciones = []

    if not isinstance(detecciones, list):
        detecciones = []

    registro["metadatos"] = metadatos
    registro["detecciones"] = detecciones

    if not registro.get("fecha_captura"):
        registro["fecha_captura"] = interpretar_fecha_exif(
            metadatos.get("fecha_original")
        )

    if "fecha_captura_disponible" not in metadatos:
        metadatos["fecha_captura_disponible"] = bool(
            interpretar_fecha_exif(metadatos.get("fecha_original"))
        )

    return registro


def obtener_registro(registro_id, usuario_id):
    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT * FROM inspecciones WHERE id = ? AND usuario_id = ?",
            (registro_id, usuario_id)
        ).fetchone()

    return serializar_registro(fila) if fila else None


def validar_fecha(valor, nombre):
    if valor is None or str(valor).strip() == "":
        return None

    try:
        datetime.strptime(valor, "%Y-%m-%d")
        return valor
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"'{nombre}' debe tener el formato AAAA-MM-DD."
        )


def validar_rango(desde, hasta):
    desde = validar_fecha(desde, "desde")
    hasta = validar_fecha(hasta, "hasta")

    if desde and hasta and desde > hasta:
        raise HTTPException(
            status_code=400,
            detail="La fecha inicial no puede ser posterior a la final."
        )

    return desde, hasta


def limpiar_nombre_lote(valor):
    """Nombre de lote: una línea, sin caracteres raros, máximo 60."""

    if valor is None:
        return None

    texto = " ".join(str(valor).split())
    texto = "".join(c for c in texto if ord(c) >= 32)

    return texto[:60].strip() or None


def nombres_de_lotes(usuario_id):
    """{lote_id: nombre} de los lotes del usuario que tienen nombre."""

    with obtener_conexion() as conexion:
        filas = conexion.execute(
            "SELECT id, nombre FROM lotes "
            "WHERE usuario_id = ? AND nombre IS NOT NULL AND nombre != ''",
            (usuario_id,)
        ).fetchall()

    return {fila["id"]: fila["nombre"] for fila in filas}


def consultar_registros(
    usuario_id,
    desde=None,
    hasta=None,
    descendente=True
):
    """
    Devuelve los registros del usuario. Si se indica un rango,
    se filtra por la FECHA DE CAPTURA (los que no tienen fecha
    de captura quedan fuera del rango).
    """

    consulta = "SELECT * FROM inspecciones WHERE usuario_id = ?"
    parametros = [usuario_id]

    if desde:
        consulta += " AND substr(fecha_captura, 1, 10) >= ?"
        parametros.append(desde)

    if hasta:
        consulta += " AND substr(fecha_captura, 1, 10) <= ?"
        parametros.append(hasta)

    with obtener_conexion() as conexion:
        filas = conexion.execute(consulta, parametros).fetchall()

    registros = [serializar_registro(fila) for fila in filas]

    nombres = nombres_de_lotes(usuario_id)

    for registro in registros:
        registro["lote_nombre"] = nombres.get(registro.get("lote_id"))

    registros.sort(
        key=lambda r: (r.get("fecha_captura") or "", r.get("id") or 0),
        reverse=descendente
    )

    return registros


def puntos_de_calor(registros):
    """Lista de (latitud, longitud, peso) de los registros con GPS."""

    puntos = []

    for registro in registros:
        metadatos = registro.get("metadatos") or {}
        latitud = metadatos.get("gps_latitud")
        longitud = metadatos.get("gps_longitud")

        if validar_coordenadas(latitud, longitud):
            puntos.append((
                float(latitud),
                float(longitud),
                int(registro.get("cantidad") or 0)
            ))

    return puntos


def texto_seguro(valor, maximo=300):
    """
    Limpia un texto para el PDF: quita caracteres de control y los que
    la fuente estándar del PDF no puede dibujar (emojis, etc.), y lo recorta.
    """

    texto = "" if valor is None else str(valor)

    texto = "".join(
        caracter if (caracter in "\n\t" or ord(caracter) >= 32) else " "
        for caracter in texto
    )

    texto = texto.encode("cp1252", errors="ignore").decode("cp1252")

    if len(texto) > maximo:
        texto = texto[:maximo].rstrip() + "..."

    return texto


def limpiar_nota(nota):
    if nota is None:
        return None

    nota = str(nota).strip()

    if not nota:
        return None

    return nota[:LARGO_MAXIMO_NOTA]


# =========================================================
# RUTA PRINCIPAL
# =========================================================

def datos_de_salud():
    return {
        "mensaje": "API de detección de mosca pinta funcionando.",
        "modelo": MODEL_PATH.name,
        "autenticacion": True,
        "notas": True,
        "mapa_calor": True,
        "reporte_pdf": True,
        "documentacion": "/docs"
    }


@app.get("/salud")
def salud():
    """Comprobación rápida de que el servidor está encendido."""
    return datos_de_salud()


# Si existe la carpeta "web" (la app compilada de Flutter), el servidor la
# publica en "/" y entonces "/" ya no muestra este JSON (ver el final del
# archivo). Si no existe, "/" responde con el estado del servidor.
CARPETA_WEB = BASE_DIR / "web"

if not (CARPETA_WEB / "index.html").is_file():
    @app.get("/")
    def inicio():
        return datos_de_salud()


# Estos archivos no deben quedar guardados en caché: así, cuando actualizas
# la app, el celular recibe la versión nueva.
ARCHIVOS_SIN_CACHE = {
    "/", "/index.html", "/flutter_service_worker.js", "/manifest.json",
    "/version.json", "/flutter_bootstrap.js", "/main.dart.js",
}


@app.middleware("http")
async def sin_cache_para_la_app(request, call_next):
    respuesta = await call_next(request)

    if request.method == "GET" and request.url.path in ARCHIVOS_SIN_CACHE:
        respuesta.headers["Cache-Control"] = "no-cache"

    return respuesta


# =========================================================
# VISTA PREVIA (conversión HEIC -> JPEG)
# =========================================================

@app.post("/vista-previa", dependencies=[Depends(usuario_actual)])
async def vista_previa(imagen: UploadFile = File(...)):
    extension = Path(imagen.filename or "").suffix.lower()

    if extension not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(
            status_code=400,
            detail="El formato de imagen no está permitido."
        )

    contenido = await imagen.read()

    if not contenido:
        raise HTTPException(
            status_code=400,
            detail="El archivo está vacío."
        )

    try:
        with Image.open(io.BytesIO(contenido)) as img:
            img.load()
            img = img.convert("RGB")

            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=90)

        return Response(
            content=buffer.getvalue(),
            media_type="image/jpeg"
        )

    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"No se pudo procesar la imagen: {error}"
        )


# =========================================================
# ANÁLISIS DE UNA IMAGEN (función reutilizable)
# =========================================================

def analizar_imagen(contenido: bytes, nombre_original: str) -> dict:
    extension = Path(nombre_original).suffix.lower()

    if extension not in EXTENSIONES_PERMITIDAS:
        raise HTTPException(
            status_code=400,
            detail="Formato de imagen no permitido."
        )

    if not contenido:
        raise HTTPException(
            status_code=400,
            detail="El archivo está vacío."
        )

    try:
        with Image.open(io.BytesIO(contenido)) as imagen:
            imagen.load()

            if imagen.format not in FORMATOS_PERMITIDOS:
                raise HTTPException(
                    status_code=400,
                    detail="El contenido no es una imagen compatible."
                )

            metadatos = extraer_metadatos(imagen, contenido)
            imagen_rgb = imagen.convert("RGB")

    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=400,
            detail=f"No se pudo abrir la imagen: {error}"
        )

    try:
        resultados = model.predict(
            source=imagen_rgb,
            imgsz=TAMANO_IMAGEN,
            conf=CONFIANZA_MINIMA,
            verbose=False
        )

        detecciones = []

        if resultados[0].boxes is not None:
            for caja in resultados[0].boxes:
                x1, y1, x2, y2 = caja.xyxy[0].tolist()
                detecciones.append({
                    "x1": float(x1),
                    "y1": float(y1),
                    "x2": float(x2),
                    "y2": float(y2),
                    "confianza": float(caja.conf[0].item())
                })

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Error durante la detección con YOLO: {error}"
        )

    cantidad = len(detecciones)
    confianza_promedio = (
        sum(d["confianza"] for d in detecciones) / cantidad
        if cantidad else 0.0
    )

    fecha_captura, fecha_disponible = obtener_fecha_captura(metadatos)
    metadatos["fecha_captura_disponible"] = fecha_disponible

    return {
        "nombre_imagen": nombre_original,
        "extension": extension,
        "fecha_captura": fecha_captura,
        "fecha_captura_disponible": fecha_disponible,
        "cantidad": cantidad,
        "confianza_promedio": confianza_promedio,
        "detecciones": detecciones,
        "metadatos": metadatos,
    }


# =========================================================
# DETECCIÓN POR LOTES (una o varias imágenes)
# =========================================================

@app.post("/detectar-lote")
async def detectar_lote(
    imagenes: List[UploadFile] = File(...),
    nota: Optional[str] = Form(None),
    nombre_lote: Optional[str] = Form(None),
    lote_id: Optional[int] = Form(None),
    usuario: dict = Depends(usuario_actual),
):
    """
    Analiza una o varias imágenes y las guarda en un lote.
    - Sin lote_id: crea un lote nuevo (con nombre_lote opcional).
    - Con lote_id: agrega las imágenes a un lote existente del usuario
      (así la app puede enviar las fotos una por una y mostrar progreso).
    """

    if not imagenes:
        raise HTTPException(
            status_code=400,
            detail="No se recibió ninguna imagen."
        )

    nota = limpiar_nota(nota)
    nombre_lote = limpiar_nombre_lote(nombre_lote)
    lote_existente = lote_id

    fecha_analisis = datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    if lote_existente is not None:
        with obtener_conexion() as conexion:
            fila_lote = conexion.execute(
                "SELECT id, fecha_analisis FROM lotes "
                "WHERE id = ? AND usuario_id = ?",
                (lote_existente, usuario["id"])
            ).fetchone()

        if fila_lote is None:
            raise HTTPException(
                status_code=404,
                detail="No se encontró el lote al que se quieren agregar imágenes."
            )

        fecha_analisis = fila_lote["fecha_analisis"]

    analizadas = []   # (indice, analisis, contenido)
    errores = []

    for indice, archivo in enumerate(imagenes):
        nombre = Path(archivo.filename or "imagen").name
        contenido = await archivo.read()

        try:
            analisis = analizar_imagen(contenido, nombre)
            analizadas.append((indice, analisis, contenido))
        except HTTPException as error:
            errores.append({
                "indice": indice,
                "nombre_imagen": nombre,
                "detalle": str(error.detail)
            })

    if not analizadas:
        raise HTTPException(
            status_code=400,
            detail="Ninguna imagen pudo analizarse: "
                   + "; ".join(e["detalle"] for e in errores)
        )

    total_detecciones = sum(a["cantidad"] for _, a, _ in analizadas)
    suma_confianzas = sum(
        d["confianza"]
        for _, a, _ in analizadas
        for d in a["detecciones"]
    )
    confianza_lote = (
        suma_confianzas / total_detecciones
        if total_detecciones else 0.0
    )

    archivos_guardados = []
    resultados = []

    try:
        with obtener_conexion() as conexion:
            if lote_existente is not None:
                lote_id = lote_existente
            else:
                cursor = conexion.execute(
                    """
                    INSERT INTO lotes (
                        fecha_analisis, total_imagenes,
                        total_detecciones, confianza_promedio,
                        usuario_id, nombre
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fecha_analisis,
                        0,
                        0,
                        0.0,
                        usuario["id"],
                        nombre_lote
                    )
                )
                lote_id = cursor.lastrowid

            for indice, analisis, contenido in analizadas:
                nombre_guardado = (
                    f"{uuid.uuid4().hex}{analisis['extension']}"
                )
                ruta = UPLOAD_DIR / nombre_guardado
                ruta.write_bytes(contenido)
                archivos_guardados.append(ruta)

                cursor = conexion.execute(
                    """
                    INSERT INTO inspecciones (
                        fecha_analisis, fecha_captura, nombre_imagen,
                        archivo_guardado, metadatos, cantidad,
                        confianza_promedio, detecciones, lote_id,
                        usuario_id, nota
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fecha_analisis,
                        analisis["fecha_captura"],
                        analisis["nombre_imagen"],
                        nombre_guardado,
                        json.dumps(
                            analisis["metadatos"], ensure_ascii=False
                        ),
                        analisis["cantidad"],
                        analisis["confianza_promedio"],
                        json.dumps(
                            analisis["detecciones"], ensure_ascii=False
                        ),
                        lote_id,
                        usuario["id"],
                        nota
                    )
                )

                resultados.append({
                    "registro_id": cursor.lastrowid,
                    "indice": indice,
                    "nombre_imagen": analisis["nombre_imagen"],
                    "fecha_captura": analisis["fecha_captura"],
                    "fecha_captura_disponible":
                        analisis["fecha_captura_disponible"],
                    "cantidad": analisis["cantidad"],
                    "confianza_promedio": analisis["confianza_promedio"],
                    "detecciones": analisis["detecciones"],
                    "metadatos": analisis["metadatos"],
                    "nota": nota,
                })

            # Totales del lote calculados con todas sus imágenes.
            totales = conexion.execute(
                """
                SELECT COUNT(*) AS imagenes,
                       COALESCE(SUM(cantidad), 0) AS detecciones,
                       COALESCE(SUM(cantidad * confianza_promedio), 0)
                           AS ponderada
                FROM inspecciones
                WHERE lote_id = ? AND usuario_id = ?
                """,
                (lote_id, usuario["id"])
            ).fetchone()

            total_imagenes_lote = int(totales["imagenes"])
            total_detecciones = int(totales["detecciones"])
            confianza_lote = (
                float(totales["ponderada"]) / total_detecciones
                if total_detecciones else 0.0
            )

            conexion.execute(
                """
                UPDATE lotes
                SET total_imagenes = ?, total_detecciones = ?,
                    confianza_promedio = ?
                WHERE id = ?
                """,
                (
                    total_imagenes_lote, total_detecciones,
                    confianza_lote, lote_id
                )
            )

            nombre_guardado_lote = conexion.execute(
                "SELECT nombre FROM lotes WHERE id = ?", (lote_id,)
            ).fetchone()["nombre"]

            conexion.commit()

    except Exception as error:
        for ruta in archivos_guardados:
            try:
                ruta.unlink(missing_ok=True)
            except OSError:
                pass

        raise HTTPException(
            status_code=500,
            detail=f"No se pudo guardar el lote: {error}"
        )

    return {
        "lote_id": lote_id,
        "lote_nombre": nombre_guardado_lote,
        "fecha_analisis": fecha_analisis,
        "total_imagenes": total_imagenes_lote,
        "total_detecciones": total_detecciones,
        "confianza_promedio": confianza_lote,
        "resultados": resultados,
        "errores": errores,
    }


# =========================================================
# NOMBRES DE LOTE
# =========================================================

@app.get("/lotes")
def listar_lotes(usuario: dict = Depends(usuario_actual)):
    """Lotes del usuario (los más recientes primero) para sugerir nombres."""

    with obtener_conexion() as conexion:
        filas = conexion.execute(
            "SELECT id, nombre, fecha_analisis, total_imagenes, "
            "total_detecciones FROM lotes WHERE usuario_id = ? "
            "ORDER BY id DESC LIMIT 200",
            (usuario["id"],)
        ).fetchall()

    lotes = [dict(f) for f in filas]

    nombres = []
    for lote in lotes:
        nombre = lote.get("nombre")
        if nombre and nombre not in nombres:
            nombres.append(nombre)

    return {"lotes": lotes, "nombres": nombres[:12]}


class NombreLoteEntrada(BaseModel):
    nombre: Optional[str] = None


@app.patch("/lotes/{lote_id}/nombre")
def renombrar_lote(
    lote_id: int,
    datos: NombreLoteEntrada,
    usuario: dict = Depends(usuario_actual)
):
    nombre = limpiar_nombre_lote(datos.nombre)

    with obtener_conexion() as conexion:
        cursor = conexion.execute(
            "UPDATE lotes SET nombre = ? WHERE id = ? AND usuario_id = ?",
            (nombre, lote_id, usuario["id"])
        )

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="No se encontró el lote solicitado."
            )

        conexion.commit()

    return {"lote_id": lote_id, "nombre": nombre}


# =========================================================
# CONSULTAR UN LOTE
# =========================================================

@app.get("/lotes/{lote_id}")
def consultar_lote(
    lote_id: int,
    usuario: dict = Depends(usuario_actual)
):
    with obtener_conexion() as conexion:
        lote = conexion.execute(
            "SELECT * FROM lotes WHERE id = ? AND usuario_id = ?",
            (lote_id, usuario["id"])
        ).fetchone()

        if lote is None:
            raise HTTPException(
                status_code=404,
                detail="No se encontró el lote solicitado."
            )

        filas = conexion.execute(
            "SELECT * FROM inspecciones "
            "WHERE lote_id = ? AND usuario_id = ? ORDER BY id",
            (lote_id, usuario["id"])
        ).fetchall()

    return {
        **dict(lote),
        "lote_nombre": lote["nombre"],
        "registros": [serializar_registro(f) for f in filas]
    }


# =========================================================
# HISTORIAL
# =========================================================

@app.get("/registros")
def listar_registros(usuario: dict = Depends(usuario_actual)):
    registros = consultar_registros(usuario["id"])

    return {
        "total": len(registros),
        "registros": registros
    }


@app.get("/registros/{registro_id}")
def consultar_registro(
    registro_id: int,
    usuario: dict = Depends(usuario_actual)
):
    registro = obtener_registro(registro_id, usuario["id"])

    if registro is None:
        raise HTTPException(
            status_code=404,
            detail="No se encontró la inspección solicitada."
        )

    return registro


# =========================================================
# NOTAS / COMENTARIOS (opcionales)
# =========================================================

class NotaEntrada(BaseModel):
    nota: Optional[str] = None


@app.patch("/registros/{registro_id}/nota")
def actualizar_nota(
    registro_id: int,
    datos: NotaEntrada,
    usuario: dict = Depends(usuario_actual)
):
    nota = limpiar_nota(datos.nota)

    with obtener_conexion() as conexion:
        cursor = conexion.execute(
            "UPDATE inspecciones SET nota = ? "
            "WHERE id = ? AND usuario_id = ?",
            (nota, registro_id, usuario["id"])
        )

        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=404,
                detail="No se encontró la inspección solicitada."
            )

        conexion.commit()

    return {
        "mensaje": "Nota guardada.",
        "registro_id": registro_id,
        "nota": nota
    }


# =========================================================
# ACTUALIZAR UBICACIÓN
# =========================================================

@app.patch("/registros/{registro_id}/ubicacion")
def actualizar_ubicacion(
    registro_id: int,
    latitud: float,
    longitud: float,
    usuario: dict = Depends(usuario_actual)
):
    if not validar_coordenadas(latitud, longitud):
        raise HTTPException(
            status_code=400,
            detail="Las coordenadas GPS no son válidas."
        )

    with obtener_conexion() as conexion:
        fila = conexion.execute(
            "SELECT metadatos FROM inspecciones "
            "WHERE id = ? AND usuario_id = ?",
            (registro_id, usuario["id"])
        ).fetchone()

        if fila is None:
            raise HTTPException(
                status_code=404,
                detail="No se encontró la inspección solicitada."
            )

        metadatos = normalizar_metadatos(fila["metadatos"])

        metadatos["gps_latitud"] = float(latitud)
        metadatos["gps_longitud"] = float(longitud)
        metadatos["ubicacion_manual"] = True

        conexion.execute("""
            UPDATE inspecciones
            SET metadatos = ?
            WHERE id = ?
        """, (
            json.dumps(metadatos, ensure_ascii=False),
            registro_id
        ))

        conexion.commit()

    return {
        "mensaje": "Ubicación actualizada correctamente.",
        "registro_id": registro_id,
        "gps_latitud": float(latitud),
        "gps_longitud": float(longitud)
    }


# =========================================================
# REGISTROS GEOLOCALIZADOS
# =========================================================

@app.get("/registros-geolocalizados")
def listar_registros_geolocalizados(
    usuario: dict = Depends(usuario_actual)
):
    registros = consultar_registros(usuario["id"])
    geolocalizados = []

    for registro in registros:
        metadatos = registro.get("metadatos", {})
        latitud = metadatos.get("gps_latitud")
        longitud = metadatos.get("gps_longitud")

        if validar_coordenadas(latitud, longitud):
            registro["gps_latitud"] = float(latitud)
            registro["gps_longitud"] = float(longitud)
            geolocalizados.append(registro)

    return {
        "total": len(geolocalizados),
        "registros": geolocalizados
    }


# =========================================================
# MAPA DE CALOR
# =========================================================

def suavizar_matriz(matriz, sigma):
    """Desenfoque gaussiano separable (solo numpy)."""

    radio = max(int(3 * sigma), 1)
    x = np.arange(-radio, radio + 1)
    nucleo = np.exp(-(x ** 2) / (2 * sigma ** 2))
    nucleo /= nucleo.sum()

    resultado = np.apply_along_axis(
        lambda fila: np.convolve(fila, nucleo, mode="same"),
        0,
        matriz
    )
    resultado = np.apply_along_axis(
        lambda fila: np.convolve(fila, nucleo, mode="same"),
        1,
        resultado
    )

    return resultado


def figura_a_png(figura) -> bytes:
    buffer = io.BytesIO()
    figura.savefig(buffer, format="png", bbox_inches="tight")
    return buffer.getvalue()


# ---------------------------------------------------------
# MAPA BASE SATELITAL (Esri World Imagery)
# ---------------------------------------------------------
# Al generar el reporte se descargan los mosaicos del mapa base.
# Necesita internet SOLO en ese momento; si falla, el mapa de calor
# se dibuja igual, sin mapa base.

# Imágenes satelitales (mismo servicio que usa la app). Nota: el orden en la
# URL es {z}/{y}/{x}.
URL_MOSAICOS = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
AGENTE_MOSAICOS = "MoscaPintaReporte/1.0 (proyecto escolar)"
ZOOM_MAXIMO_BASE = 17
MAX_MOSAICOS_BASE = 16


def mosaico_xy(latitud, longitud, zoom):
    n = 2 ** zoom
    x = (longitud + 180.0) / 360.0 * n
    latitud = max(min(latitud, 85.0511), -85.0511)
    rad = math.radians(latitud)
    y = (1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n
    return x, y


def descargar_mosaico(zoom, x, y):
    import urllib.request

    peticion = urllib.request.Request(
        URL_MOSAICOS.format(z=zoom, x=x, y=y),
        headers={"User-Agent": AGENTE_MOSAICOS}
    )

    with urllib.request.urlopen(peticion, timeout=6) as respuesta:
        datos = respuesta.read()

    with Image.open(io.BytesIO(datos)) as imagen:
        return imagen.convert("RGB")


def obtener_fondo_mapa(lat_min, lat_max, lon_min, lon_max):
    """
    Devuelve un arreglo RGB (recortado exactamente a la zona pedida) con
    el mapa base, o None si no hay internet o falla la descarga.
    """

    try:
        from concurrent.futures import ThreadPoolExecutor

        zoom = ZOOM_MAXIMO_BASE

        while zoom > 3:
            x0, y1 = mosaico_xy(lat_min, lon_min, zoom)
            x1, y0 = mosaico_xy(lat_max, lon_max, zoom)
            columnas = int(math.floor(x1)) - int(math.floor(x0)) + 1
            filas = int(math.floor(y1)) - int(math.floor(y0)) + 1

            if columnas * filas <= MAX_MOSAICOS_BASE:
                break

            zoom -= 1

        x_ini, x_fin = int(math.floor(x0)), int(math.floor(x1))
        y_ini, y_fin = int(math.floor(y0)), int(math.floor(y1))

        pedidos = [
            (x, y)
            for y in range(y_ini, y_fin + 1)
            for x in range(x_ini, x_fin + 1)
        ]

        with ThreadPoolExecutor(max_workers=6) as grupo:
            imagenes = list(grupo.map(
                lambda p: descargar_mosaico(zoom, p[0], p[1]), pedidos
            ))

        lado = 256
        mosaico = Image.new(
            "RGB", ((x_fin - x_ini + 1) * lado, (y_fin - y_ini + 1) * lado)
        )

        for (x, y), imagen in zip(pedidos, imagenes):
            mosaico.paste(imagen, ((x - x_ini) * lado, (y - y_ini) * lado))

        izquierda = int(round((x0 - x_ini) * lado))
        derecha = int(round((x1 - x_ini) * lado))
        arriba = int(round((y0 - y_ini) * lado))
        abajo = int(round((y1 - y_ini) * lado))

        recorte = mosaico.crop((
            izquierda, arriba, max(derecha, izquierda + 1),
            max(abajo, arriba + 1)
        ))

        return np.asarray(recorte)

    except Exception as error:
        print(
            "Aviso: no se pudo descargar el mapa base "
            f"({type(error).__name__}: {error}). "
            "El mapa de calor se dibuja sin mapa base."
        )
        return None


def generar_mapa_calor_png(
    puntos,
    titulo="Mapa de calor de mosca pinta",
    zonas=None
) -> bytes:
    """
    Dibuja un mapa de calor a partir de (latitud, longitud, peso).
    El peso es la cantidad de moscas detectadas. No usa mapa base
    de internet, por lo que funciona sin conexión.
    """

    figura = Figure(figsize=(8, 6), dpi=130)
    eje = figura.add_subplot(111)
    eje.set_title(titulo, fontsize=12)

    if not puntos:
        eje.text(
            0.5, 0.5,
            "No hay fotografías con ubicación GPS\n"
            "en el periodo seleccionado.",
            ha="center", va="center", fontsize=12,
            transform=eje.transAxes
        )
        eje.set_xticks([])
        eje.set_yticks([])
        return figura_a_png(figura)

    latitudes = np.array([p[0] for p in puntos], dtype=float)
    longitudes = np.array([p[1] for p in puntos], dtype=float)
    pesos = np.array([p[2] for p in puntos], dtype=float)

    latitud_media = float(latitudes.mean())
    factor = max(math.cos(math.radians(latitud_media)), 0.01)

    centro_lat = (latitudes.min() + latitudes.max()) / 2
    centro_lon = (longitudes.min() + longitudes.max()) / 2

    # Mínimo de ~220 m de lado para que un solo punto se vea bien.
    amplitud_lat = max(latitudes.max() - latitudes.min(), 0.002) * 1.3
    amplitud_lon = max(
        longitudes.max() - longitudes.min(), 0.002 / factor
    ) * 1.3

    lat_min = centro_lat - amplitud_lat / 2
    lat_max = centro_lat + amplitud_lat / 2
    lon_min = centro_lon - amplitud_lon / 2
    lon_max = centro_lon + amplitud_lon / 2

    filas = 240
    columnas = int(round(filas * (amplitud_lon * factor) / amplitud_lat))
    columnas = max(60, min(columnas, 480))

    matriz, _, _ = np.histogram2d(
        longitudes,
        latitudes,
        bins=[columnas, filas],
        range=[[lon_min, lon_max], [lat_min, lat_max]],
        weights=pesos
    )
    matriz = matriz.T  # (filas, columnas), origen abajo

    matriz = suavizar_matriz(matriz, sigma=max(columnas, filas) * 0.035)

    maximo = float(matriz.max())
    normalizada = matriz / maximo if maximo > 0 else np.zeros_like(matriz)

    mapa_colores = obtener_mapa_colores("jet")
    rgba = mapa_colores(normalizada)
    rgba[..., 3] = np.clip(normalizada * 2.0, 0, 0.85)

    eje.set_facecolor("#EEF2EE")

    fondo = obtener_fondo_mapa(lat_min, lat_max, lon_min, lon_max)

    if fondo is not None:
        eje.imshow(
            fondo,
            extent=[lon_min, lon_max, lat_min, lat_max],
            interpolation="bilinear",
            aspect=1.0 / factor,
            zorder=0
        )
        rgba[..., 3] = np.clip(normalizada * 2.0, 0, 0.7)
        eje.text(
            0.995, 0.005, "Imágenes: Esri, Maxar, Earthstar Geographics",
            ha="right", va="bottom", fontsize=6, zorder=6,
            transform=eje.transAxes,
            bbox=dict(facecolor="white", alpha=0.75, edgecolor="none", pad=1)
        )

    eje.imshow(
        rgba,
        origin="lower",
        extent=[lon_min, lon_max, lat_min, lat_max],
        interpolation="bilinear",
        aspect=1.0 / factor,
        zorder=1
    )

    eje.scatter(
        longitudes,
        latitudes,
        s=24,
        c="white",
        edgecolors="black",
        linewidths=0.8,
        zorder=3,
        label="Punto de inspección"
    )

    # Zonas de mayor (A, rojo) y menor (B, azul) concentración.
    if zonas:
        for lista, color in (
            (zonas.get("altas") or [], "#B71C1C"),
            (zonas.get("bajas") or [], "#0D47A1"),
        ):
            for zona in lista:
                eje.scatter(
                    [zona["lon"]], [zona["lat"]], s=330, facecolors="none",
                    edgecolors=color, linewidths=1.8, zorder=4
                )
                eje.annotate(
                    zona["etiqueta"], (zona["lon"], zona["lat"]),
                    xytext=(9, 9), textcoords="offset points",
                    fontsize=8, fontweight="bold", color=color, zorder=5,
                    bbox=dict(
                        boxstyle="round,pad=0.15", facecolor="white",
                        alpha=0.85, edgecolor=color, linewidth=0.6
                    )
                )

    eje.set_xlim(lon_min, lon_max)
    eje.set_ylim(lat_min, lat_max)
    eje.set_xlabel("Longitud", fontsize=8)
    eje.set_ylabel("Latitud", fontsize=8)
    eje.tick_params(labelsize=7)
    eje.ticklabel_format(useOffset=False, style="plain")
    for etiqueta in eje.get_xticklabels():
        etiqueta.set_rotation(30)
        etiqueta.set_ha("right")
    eje.grid(
        alpha=0.35, linewidth=0.5, zorder=2,
        color="white" if fondo is not None else None
    )
    eje.legend(loc="upper right", fontsize=7, framealpha=0.9)

    barra = figura.colorbar(
        ScalarMappable(norm=Normalize(0, 1), cmap=mapa_colores),
        ax=eje,
        fraction=0.046,
        pad=0.04
    )
    barra.set_ticks([0, 1])
    barra.set_ticklabels(["Baja", "Alta"])
    barra.set_label("Densidad de moscas pintas", fontsize=8)

    if maximo <= 0:
        eje.text(
            0.5, 0.04,
            "Sin moscas detectadas en estos puntos",
            ha="center", fontsize=8, transform=eje.transAxes,
            bbox=dict(facecolor="white", alpha=0.8, edgecolor="none")
        )

    return figura_a_png(figura)


@app.get("/mapa-calor/resumen")
def resumen_mapa_calor(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    usuario: dict = Depends(usuario_actual)
):
    desde, hasta = validar_rango(desde, hasta)
    registros = consultar_registros(usuario["id"], desde, hasta)
    puntos = puntos_de_calor(registros)

    return {
        "registros": len(registros),
        "detecciones": sum(int(r.get("cantidad") or 0) for r in registros),
        "puntos_gps": len(puntos),
        "sin_gps": len(registros) - len(puntos),
    }


@app.get("/mapa-calor/puntos")
def puntos_mapa_calor(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    usuario: dict = Depends(usuario_actual)
):
    """Puntos con GPS para dibujar el mapa interactivo en la app."""

    desde, hasta = validar_rango(desde, hasta)
    registros = consultar_registros(usuario["id"], desde, hasta)

    puntos = []

    for registro in registros:
        metadatos = registro.get("metadatos") or {}
        latitud = metadatos.get("gps_latitud")
        longitud = metadatos.get("gps_longitud")

        if not validar_coordenadas(latitud, longitud):
            continue

        puntos.append({
            "id": registro.get("id"),
            "latitud": float(latitud),
            "longitud": float(longitud),
            "cantidad": int(registro.get("cantidad") or 0),
            "nombre_imagen": registro.get("nombre_imagen"),
            "fecha_captura": registro.get("fecha_captura"),
            "nota": registro.get("nota"),
            "lote_id": registro.get("lote_id"),
            "lote_nombre": registro.get("lote_nombre"),
        })

    return {
        "registros": len(registros),
        "detecciones": sum(int(r.get("cantidad") or 0) for r in registros),
        "puntos_gps": len(puntos),
        "sin_gps": len(registros) - len(puntos),
        "puntos": puntos,
    }


@app.get("/mapa-calor.png")
def mapa_calor_png(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    usuario: dict = Depends(usuario_actual)
):
    desde, hasta = validar_rango(desde, hasta)
    registros = consultar_registros(usuario["id"], desde, hasta)
    puntos = puntos_de_calor(registros)

    png = generar_mapa_calor_png(puntos)

    return Response(content=png, media_type="image/png")


# =========================================================
# REPORTE PDF
# =========================================================

def formatear_fecha_corta(valor):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return str(valor)


# =========================================================
# ANÁLISIS DEL PERIODO (estadísticas, zonas y tendencia)
# =========================================================

COLOR_VERDE = "#2E7D32"
COLOR_VERDE_OSCURO = "#1B5E20"
COLOR_VERDE_CLARO = "#E8F5E9"
COLOR_ROJO = "#C62828"
COLOR_AMBAR = "#F9A825"
COLOR_AZUL = "#1565C0"
COLOR_GRIS = "#5F6B61"
COLOR_NARANJA = "#EF6C00"

# Criterios de referencia internos (NO son umbrales económicos oficiales).
# Nivel según el promedio de ejemplares (moscas) detectados por imagen.
# Son umbrales provisionales; ajustarlos con los resultados reales.
UMBRAL_MODERADO = 1.0     # ejemplares por imagen
UMBRAL_ALTO = 3.0         # a partir de aquí: nivel alto / foco rojo
UMBRAL_CAMBIO_PCT = 25    # variación considerada notable
MIN_IMAGENES_COMPARACION = 5
TAMANO_ZONA_METROS = 150
MAX_ZONAS_ALTAS = 5
MAX_ZONAS_BAJAS = 3
MAX_MAPAS_FECHA = 6
MAX_EVIDENCIAS = 12
MAX_FILAS_DETALLE = 400
MAX_LOTES_GRAFICA = 15


def etiqueta_lote_registro(registro, corto=False):
    nombre = (registro.get("lote_nombre") or "").strip()

    if nombre:
        return texto_seguro(nombre, 18 if corto else 40)

    if registro.get("lote_id") is not None:
        return (
            f"#{registro['lote_id']}" if corto
            else f"Lote #{registro['lote_id']}"
        )

    return "-" if corto else "Sin lote"


def fecha_dia(registro):
    return (registro.get("fecha_captura") or "")[:10]


def parsear_dia(texto):
    try:
        return datetime.strptime(texto, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def variacion_porcentual(actual, anterior):
    if anterior and anterior > 0:
        return (actual - anterior) / anterior * 100.0
    return None


def texto_variacion(valor):
    if valor is None:
        return "No calculable"
    return f"{valor:+.0f} %"


def calcular_zonas(registros, tamano_m=TAMANO_ZONA_METROS):
    """
    Agrupa los puntos con GPS en celdas de ~150 m y devuelve las zonas
    de mayor y menor concentración de detecciones.
    """

    puntos = []

    for registro in registros:
        metadatos = registro.get("metadatos") or {}
        latitud = metadatos.get("gps_latitud")
        longitud = metadatos.get("gps_longitud")

        if validar_coordenadas(latitud, longitud):
            puntos.append((
                float(latitud),
                float(longitud),
                int(registro.get("cantidad") or 0)
            ))

    resultado = {
        "puntos": len(puntos),
        "celdas": 0,
        "total_detecciones": sum(p[2] for p in puntos),
        "altas": [],
        "bajas": [],
        "focos": [],
    }

    if not puntos:
        return resultado

    latitud_ref = sum(p[0] for p in puntos) / len(puntos)
    grados_lat = tamano_m / 111320.0
    grados_lon = tamano_m / (
        111320.0 * max(math.cos(math.radians(latitud_ref)), 0.01)
    )

    celdas = {}

    for latitud, longitud, cantidad in puntos:
        clave = (
            math.floor(latitud / grados_lat),
            math.floor(longitud / grados_lon)
        )
        celda = celdas.setdefault(clave, {
            "imagenes": 0, "detecciones": 0,
            "suma_lat": 0.0, "suma_lon": 0.0
        })
        celda["imagenes"] += 1
        celda["detecciones"] += cantidad
        celda["suma_lat"] += latitud
        celda["suma_lon"] += longitud

    zonas = []

    for celda in celdas.values():
        zonas.append({
            "lat": celda["suma_lat"] / celda["imagenes"],
            "lon": celda["suma_lon"] / celda["imagenes"],
            "imagenes": celda["imagenes"],
            "detecciones": celda["detecciones"],
            "promedio": celda["detecciones"] / celda["imagenes"],
        })

    resultado["celdas"] = len(zonas)
    total = resultado["total_detecciones"]

    for zona in zonas:
        zona["pct_total"] = (
            zona["detecciones"] / total * 100.0 if total else 0.0
        )

    altas = sorted(
        [z for z in zonas if z["detecciones"] > 0],
        key=lambda z: (-z["detecciones"], -z["promedio"])
    )[:MAX_ZONAS_ALTAS]

    for indice, zona in enumerate(altas, start=1):
        zona["etiqueta"] = f"A{indice}"
        zona["nivel"] = (
            "Foco rojo" if zona["promedio"] >= UMBRAL_ALTO
            else "Moderado" if zona["promedio"] >= UMBRAL_MODERADO
            else "Bajo"
        )

    ids_altas = {id(z) for z in altas}

    bajas = sorted(
        [z for z in zonas if id(z) not in ids_altas],
        key=lambda z: (z["promedio"], z["detecciones"], -z["imagenes"])
    )[:MAX_ZONAS_BAJAS]

    for indice, zona in enumerate(bajas, start=1):
        zona["etiqueta"] = f"B{indice}"
        zona["nivel"] = "Sin detecciones" if zona["detecciones"] == 0 else "Bajo"

    resultado["altas"] = altas
    resultado["bajas"] = bajas
    resultado["focos"] = [z for z in altas if z["nivel"] == "Foco rojo"]

    return resultado


def calcular_tendencia(por_dia):
    dias = sorted(d for d in por_dia if d)

    serie = [
        (d, por_dia[d]["detecciones"] / por_dia[d]["imagenes"])
        for d in dias
    ]

    tendencia = {
        "suficiente": len(dias) >= 3,
        "serie": serie,
        "clase": "Datos insuficientes",
        "cambio_pct": None,
        "inicio": None,
        "fin": None,
        "pico": max(serie, key=lambda s: s[1]) if serie else None,
        "mayor_aumento": None,
        "mayor_disminucion": None,
    }

    if len(dias) < 3:
        return tendencia

    mitad = len(dias) // 2

    def promedio_dias(lista):
        imagenes = sum(por_dia[d]["imagenes"] for d in lista)
        detecciones = sum(por_dia[d]["detecciones"] for d in lista)
        return detecciones / imagenes if imagenes else 0.0

    inicio = promedio_dias(dias[:mitad])
    fin = promedio_dias(dias[mitad:])

    tendencia["inicio"] = inicio
    tendencia["fin"] = fin

    if inicio > 0:
        cambio = (fin - inicio) / inicio * 100.0
        tendencia["cambio_pct"] = cambio

        if cambio >= UMBRAL_CAMBIO_PCT:
            tendencia["clase"] = "Al alza"
        elif cambio <= -UMBRAL_CAMBIO_PCT:
            tendencia["clase"] = "A la baja"
        else:
            tendencia["clase"] = "Estable"
    else:
        tendencia["clase"] = "Al alza" if fin > 0 else "Estable"

    for i in range(1, len(serie)):
        delta = serie[i][1] - serie[i - 1][1]
        cambio = (serie[i - 1][0], serie[i][0], delta)

        if delta > 0 and (
            tendencia["mayor_aumento"] is None
            or delta > tendencia["mayor_aumento"][2]
        ):
            tendencia["mayor_aumento"] = cambio

        if delta < 0 and (
            tendencia["mayor_disminucion"] is None
            or delta < tendencia["mayor_disminucion"][2]
        ):
            tendencia["mayor_disminucion"] = cambio

    return tendencia


def analizar_periodo(registros):
    """Calcula todas las estadísticas que usa el reporte."""

    cantidades = [int(r.get("cantidad") or 0) for r in registros]

    n = len(registros)
    total = sum(cantidades)
    con = sum(1 for c in cantidades if c > 0)

    suma_ponderada = sum(
        float(r.get("confianza_promedio") or 0) * int(r.get("cantidad") or 0)
        for r in registros
    )

    por_dia = {}
    por_lote = {}

    for registro, cantidad in zip(registros, cantidades):
        dia = fecha_dia(registro)

        acumulado_dia = por_dia.setdefault(
            dia, {"imagenes": 0, "detecciones": 0, "con": 0}
        )
        acumulado_dia["imagenes"] += 1
        acumulado_dia["detecciones"] += cantidad
        acumulado_dia["con"] += 1 if cantidad > 0 else 0

        lote_id = registro.get("lote_id")
        nombre_lote = (registro.get("lote_nombre") or "").strip() or None

        # Los análisis con el mismo nombre (p. ej. "Parcela Norte")
        # se agrupan como un solo lote aunque se hayan hecho en
        # distintas fechas.
        clave_lote = (
            ("n", nombre_lote.lower()) if nombre_lote else ("i", lote_id)
        )

        lote = por_lote.setdefault(clave_lote, {
            "lote_id": lote_id,
            "nombre": nombre_lote,
            "imagenes": 0,
            "detecciones": 0,
            "con": 0,
            "primera": None,
            "ultima": None,
        })
        lote["imagenes"] += 1
        lote["detecciones"] += cantidad
        lote["con"] += 1 if cantidad > 0 else 0

        if dia:
            if lote["primera"] is None or dia < lote["primera"]:
                lote["primera"] = dia
            if lote["ultima"] is None or dia > lote["ultima"]:
                lote["ultima"] = dia

    lotes = sorted(
        por_lote.values(),
        key=lambda l: (l["primera"] or "9999-99-99", l["lote_id"] or 0)
    )

    for lote in lotes:
        lote["sin_lote"] = lote["nombre"] is None and lote["lote_id"] is None
        lote["etiqueta"] = (
            texto_seguro(lote["nombre"], 40) if lote["nombre"]
            else (
                "Sin lote" if lote["sin_lote"]
                else f"Lote #{lote['lote_id']}"
            )
        )
        lote["corto"] = (
            texto_seguro(lote["nombre"], 14) if lote["nombre"]
            else ("S/L" if lote["sin_lote"] else f"#{lote['lote_id']}")
        )

    for lote in lotes:
        lote["promedio"] = lote["detecciones"] / lote["imagenes"]

    dias = sorted(d for d in por_dia if d)
    primera = dias[0] if dias else None
    ultima = dias[-1] if dias else None

    dias_monitoreo = 0

    if primera and ultima:
        dias_monitoreo = (parsear_dia(ultima) - parsear_dia(primera)).days + 1

    zonas = calcular_zonas(registros)
    pct_con = con / n * 100.0 if n else 0.0

    if total == 0:
        nivel = "Sin detecciones"
    elif total / n >= UMBRAL_ALTO:
        nivel = "Alto"
    elif total / n >= UMBRAL_MODERADO:
        nivel = "Moderado"
    else:
        nivel = "Bajo"

    return {
        "n": n,
        "total": total,
        "con": con,
        "sin": n - con,
        "pct_con": pct_con,
        "promedio": total / n if n else 0.0,
        "maximo": max(cantidades) if cantidades else 0,
        "confianza": suma_ponderada / total if total else None,
        "por_dia": por_dia,
        "lotes": lotes,
        "n_lotes": sum(1 for l in lotes if not l["sin_lote"]),
        "n_sin_lote": sum(l["imagenes"] for l in lotes if l["sin_lote"]),
        "primera": primera,
        "ultima": ultima,
        "dias_monitoreo": dias_monitoreo,
        "dias_con_capturas": len(dias),
        "zonas": zonas,
        "n_gps": zonas["puntos"],
        "pct_gps": zonas["puntos"] / n * 100.0 if n else 0.0,
        "tendencia": calcular_tendencia(por_dia),
        "nivel": nivel,
    }


def comparar_periodos(actual, previo, rango_previo):
    comparacion = {
        "rango": rango_previo,
        "previo": previo,
        "suficiente": False,
        "clase": "Sin datos suficientes",
    }

    if previo is None or rango_previo is None:
        return comparacion

    comparacion["suficiente"] = (
        previo["n"] >= MIN_IMAGENES_COMPARACION
        and actual["n"] >= MIN_IMAGENES_COMPARACION
    )

    comparacion["var_promedio"] = variacion_porcentual(
        actual["promedio"], previo["promedio"]
    )
    comparacion["var_detecciones"] = variacion_porcentual(
        actual["total"], previo["total"]
    )
    comparacion["dif_pct_con"] = actual["pct_con"] - previo["pct_con"]

    if comparacion["suficiente"]:
        variacion = comparacion["var_promedio"]

        if variacion is None:
            comparacion["clase"] = (
                "Incremento" if actual["promedio"] > 0 else "Sin cambio notable"
            )
        elif variacion >= UMBRAL_CAMBIO_PCT:
            comparacion["clase"] = "Incremento"
        elif variacion <= -UMBRAL_CAMBIO_PCT:
            comparacion["clase"] = "Disminución"
        else:
            comparacion["clase"] = "Sin cambio notable"

    return comparacion


def seleccionar_fechas_mapa(registros, maximo=MAX_MAPAS_FECHA):
    """Fechas con puntos GPS para dibujar la distribución por fecha."""

    grupos = {}

    for registro in registros:
        dia = fecha_dia(registro)
        metadatos = registro.get("metadatos") or {}
        latitud = metadatos.get("gps_latitud")
        longitud = metadatos.get("gps_longitud")

        if not dia or not validar_coordenadas(latitud, longitud):
            continue

        cantidad = int(registro.get("cantidad") or 0)

        grupo = grupos.setdefault(
            dia, {"dia": dia, "puntos": [], "imagenes": 0, "detecciones": 0}
        )
        grupo["puntos"].append((float(latitud), float(longitud), cantidad))
        grupo["imagenes"] += 1
        grupo["detecciones"] += cantidad

    lista = list(grupos.values())

    if len(lista) > maximo:
        lista = sorted(lista, key=lambda g: -g["detecciones"])[:maximo]

    return sorted(lista, key=lambda g: g["dia"])


# =========================================================
# RECOMENDACIONES Y CONCLUSIONES (basadas en los datos)
# =========================================================

def generar_recomendaciones(a, comparacion, sin_fecha):
    recomendaciones = []

    def agregar(prioridad, tema, texto):
        recomendaciones.append(
            {"prioridad": prioridad, "tema": tema, "texto": texto}
        )

    if a["n"] == 0:
        agregar(
            "Media", "Nuevos monitoreos",
            "No hay fotografías con fecha de captura dentro del periodo "
            "seleccionado. Realizar nuevas capturas con la cámara de fecha, "
            "hora y ubicación activadas, o ampliar el periodo del reporte."
        )
        return recomendaciones

    zonas = a["zonas"]
    tendencia = a["tendencia"]

    # ---- Zonas prioritarias y monitoreo reforzado ----
    if zonas["altas"]:
        principales = zonas["altas"][:3]
        porcentaje = sum(z["pct_total"] for z in principales)
        listado = "; ".join(
            f"{z['etiqueta']} ({z['lat']:.5f}, {z['lon']:.5f}) con "
            f"{z['detecciones']} detecciones en {z['imagenes']} imagen(es)"
            for z in principales
        )
        prioridad = (
            "Alta" if zonas["focos"] or a["nivel"] in ("Moderado", "Alto")
            else "Media"
        )

        agregar(
            prioridad, "Zonas prioritarias para inspección",
            f"Inspeccionar en campo las zonas con mayor concentración"
            + (
                f" (focos rojos: "
                f"{', '.join(z['etiqueta'] for z in zonas['focos'])})"
                if zonas["focos"] else ""
            )
            + f": {listado}. Reúnen el {porcentaje:.0f} % de las detecciones "
            f"georreferenciadas."
        )
        agregar(
            prioridad, "Monitoreo en zonas de mayor concentración",
            "Reforzar el monitoreo en esas zonas. Como referencia, una "
            "revisión semanal durante la temporada de lluvias, cuando la "
            "plaga suele estar más activa. Confirmar en campo la presencia "
            "de ninfas (espuma en la base de los tallos) y de adultos en el "
            "follaje antes de decidir acciones de manejo."
        )
    elif zonas["puntos"] == 0:
        agregar(
            "Media", "Zonas prioritarias para inspección",
            "Ninguna imagen del periodo tiene ubicación GPS, por lo que no "
            "es posible identificar zonas prioritarias. Activar la ubicación "
            "de la cámara al tomar las fotografías."
        )
    else:
        agregar(
            "Baja", "Monitoreo en zonas de mayor concentración",
            "No hubo detecciones en los puntos georreferenciados. Mantener "
            "el monitoreo regular y vigilar el inicio de la temporada de "
            "lluvias."
        )

    # ---- Incrementos o disminuciones ----
    clase = tendencia["clase"]

    if clase == "Al alza":
        cambio = tendencia["cambio_pct"]
        detalle = (
            f"aumentó {cambio:.0f} %" if cambio is not None
            else "pasó de cero a valores mayores a cero"
        )
        agregar(
            "Alta", "Posibles incrementos de la plaga",
            f"El promedio de detecciones por imagen {detalle} entre la "
            f"primera y la segunda mitad del periodo. Confirmarlo con un "
            f"nuevo muestreo en 7 días y revisar condiciones que favorecen "
            f"a la plaga: humedad alta, lluvias recientes, residuos de "
            f"cosecha y maleza gramínea."
        )
    elif clase == "A la baja":
        agregar(
            "Baja", "Disminución de detecciones",
            "El promedio de detecciones por imagen disminuyó durante el "
            "periodo. No asumir que la plaga está controlada: puede deberse "
            "a diferencias en los puntos o momentos de muestreo. Continuar "
            "con el monitoreo."
        )
    elif clase == "Estable":
        agregar(
            "Baja", "Comportamiento temporal",
            "El promedio de detecciones por imagen se mantuvo estable. "
            "Continuar con el monitoreo periódico para detectar cambios "
            "tempranos."
        )
    else:
        agregar(
            "Media", "Comportamiento temporal",
            "Hay capturas en menos de 3 fechas, por lo que no se puede "
            "evaluar la tendencia. Programar capturas en varias fechas "
            "del periodo."
        )

    if comparacion["suficiente"]:
        if comparacion["clase"] == "Incremento":
            agregar(
                "Alta", "Comparación con el periodo anterior",
                "Las detecciones por imagen aumentaron respecto al periodo "
                "anterior. Priorizar la verificación en campo y acortar el "
                "intervalo entre monitoreos."
            )
        elif comparacion["clase"] == "Disminución":
            agregar(
                "Baja", "Comparación con el periodo anterior",
                "Las detecciones por imagen disminuyeron respecto al periodo "
                "anterior. Verificar que el muestreo haya cubierto las mismas "
                "zonas antes de interpretarlo como mejoría."
            )

    if a["maximo"] >= 5 and a["maximo"] >= 3 * max(a["promedio"], 0.1):
        agregar(
            "Media", "Concentración puntual",
            f"Una imagen reunió {a['maximo']} detecciones, muy por encima "
            f"del promedio. Revisar esa fotografía y su entorno, y descartar "
            f"duplicados o fotografías repetidas del mismo punto."
        )

    # ---- Calidad y cobertura del muestreo ----
    if a["n"] < 10 or a["dias_con_capturas"] < 3:
        agregar(
            "Media", "Cobertura del muestreo",
            f"La muestra es limitada ({a['n']} imágenes en "
            f"{a['dias_con_capturas']} fecha(s)). Ampliar el número de "
            f"puntos y de fechas para obtener conclusiones más confiables."
        )

    if a["n"] > 0 and a["pct_gps"] < 80:
        agregar(
            "Media", "Georreferenciación",
            f"{a['n'] - a['n_gps']} de {a['n']} imágenes no tienen GPS y no "
            f"aparecen en los mapas. Activar la ubicación de la cámara."
        )

    if sin_fecha > 0:
        agregar(
            "Baja", "Fecha de captura",
            f"{sin_fecha} imagen(es) del sistema no tienen fecha de captura "
            f"en sus metadatos y no se incluyen en ningún reporte por "
            f"periodo. Tomar las fotografías con la fecha y hora activas."
        )

    if a["confianza"] is not None and a["confianza"] < 0.65:
        agregar(
            "Media", "Validación de detecciones",
            f"La confianza promedio del modelo es baja "
            f"({a['confianza'] * 100:.0f} %). Revisar las evidencias "
            f"fotográficas (documento de evidencias) y cuidar el enfoque y la iluminación de las fotos."
        )

    # ---- Manejo integrado de plagas (general) ----
    agregar(
        "Media", "Manejo integrado de plagas",
        "Interpretar este reporte como un indicador de presencia, no como el "
        "conteo de la población real. Confirmar con muestreo en campo (por "
        "ejemplo, ninfas por tallo o adultos por golpe de red) y compararlo "
        "con los umbrales económicos locales antes de decidir intervenciones."
    )
    agregar(
        "Baja", "Manejo integrado de plagas",
        "Priorizar medidas preventivas y culturales: manejo de residuos de "
        "cosecha y de maleza hospedera (gramíneas) en y alrededor de las "
        "zonas prioritarias, y conservación de los enemigos naturales."
    )
    agregar(
        "Baja", "Manejo integrado de plagas",
        "Con la información de este reporte no se recomienda un tratamiento "
        "específico. Si el muestreo en campo supera el umbral económico, "
        "consultar a un asesor técnico o al comité local de sanidad vegetal "
        "para elegir un método de control autorizado."
    )

    # ---- Nuevos monitoreos ----
    if zonas["altas"]:
        etiquetas = ", ".join(z["etiqueta"] for z in zonas["altas"][:3])
        agregar(
            "Media", "Sugerencias para nuevos monitoreos",
            f"Repetir el monitoreo en 7 días en las zonas {etiquetas} y en "
            f"14 días en el resto. Fotografiar los mismos puntos, a una "
            f"altura y horario similares, con al menos 5 imágenes por zona."
        )
    else:
        agregar(
            "Baja", "Sugerencias para nuevos monitoreos",
            "Repetir el monitoreo en 14 días fotografiando puntos fijos, a "
            "una altura y horario similares, con al menos 5 imágenes por "
            "zona."
        )

    if zonas["bajas"]:
        agregar(
            "Baja", "Sugerencias para nuevos monitoreos",
            "Incluir las zonas de menor concentración (B) en los siguientes "
            "monitoreos para confirmar que se mantienen con baja presencia."
        )

    orden = {"Alta": 0, "Media": 1, "Baja": 2}
    return sorted(recomendaciones, key=lambda r: orden[r["prioridad"]])


def generar_conclusiones(a, comparacion, recomendaciones, desde, hasta):
    conclusiones = []

    if a["n"] == 0:
        return [
            "No hay fotografías con fecha de captura dentro del periodo "
            "seleccionado, por lo que no se pueden obtener conclusiones."
        ]

    lotes_texto = (
        f"{a['n_lotes']} lote(s)" if a["n_lotes"]
        else "análisis sin lote asignado"
    )

    conclusiones.append(
        f"Entre el {formatear_fecha_corta(desde)} y el "
        f"{formatear_fecha_corta(hasta)} se analizaron {a['n']} imagen(es) "
        f"({lotes_texto}) y se registraron {a['total']} detecciones. "
        f"{a['con']} imágenes ({a['pct_con']:.0f} %) presentaron detecciones, "
        f"con un promedio de {a['promedio']:.2f} ejemplares por imagen. "
        f"Nivel de presencia de referencia: {a['nivel'].lower()}."
    )

    zonas = a["zonas"]

    if zonas["altas"]:
        z = zonas["altas"][0]
        conclusiones.append(
            f"La mayor concentración se ubica en la zona {z['etiqueta']} "
            f"({z['lat']:.5f}, {z['lon']:.5f}), con {z['detecciones']} "
            f"detecciones ({z['pct_total']:.0f} % del total georreferenciado)."
        )

        if zonas["focos"]:
            conclusiones.append(
                f"Se identificaron {len(zonas['focos'])} foco(s) rojo(s) "
                f"(zonas con {UMBRAL_ALTO:g} o más ejemplares por imagen): "
                f"{', '.join(f['etiqueta'] for f in zonas['focos'])}."
            )
    elif zonas["puntos"] == 0:
        conclusiones.append(
            "Ninguna imagen tiene ubicación GPS, por lo que no se pudo "
            "identificar la distribución geográfica."
        )
    else:
        conclusiones.append(
            "No se registraron detecciones en los puntos georreferenciados."
        )

    tendencia = a["tendencia"]

    if tendencia["suficiente"]:
        texto = f"El comportamiento temporal fue {tendencia['clase'].lower()}"

        if tendencia["cambio_pct"] is not None:
            texto += (
                f" ({tendencia['cambio_pct']:+.0f} % en el promedio por "
                f"imagen entre la primera y la segunda mitad del periodo)"
            )

        if tendencia["pico"]:
            texto += (
                f". El mayor promedio por imagen ocurrió el "
                f"{formatear_fecha_corta(tendencia['pico'][0])} "
                f"({tendencia['pico'][1]:.2f})"
            )

        conclusiones.append(texto + ".")
    else:
        conclusiones.append(
            "Hay capturas en menos de 3 fechas, por lo que no se pudo "
            "evaluar la tendencia."
        )

    if comparacion["suficiente"]:
        conclusiones.append(
            f"Respecto al periodo anterior "
            f"({formatear_fecha_corta(comparacion['rango'][0])} al "
            f"{formatear_fecha_corta(comparacion['rango'][1])}): "
            f"{comparacion['clase'].lower()} "
            f"({texto_variacion(comparacion['var_promedio'])} en el promedio "
            f"de detecciones por imagen)."
        )

    prioritarias = [r for r in recomendaciones if r["prioridad"] == "Alta"]
    destacadas = (prioritarias or recomendaciones)[:3]

    conclusiones.append(
        "Recomendaciones principales: "
        + "; ".join(r["tema"].lower() for r in destacadas) + "."
    )

    conclusiones.append(
        "Las detecciones son un indicador de presencia obtenido de "
        "fotografías; no equivalen a la población total real de mosca "
        "pinta. Las decisiones de manejo deben confirmarse con muestreo en "
        "campo."
    )

    return conclusiones


def contar_sin_fecha(usuario_id):
    try:
        with obtener_conexion() as conexion:
            fila = conexion.execute(
                "SELECT COUNT(*) AS total FROM inspecciones "
                "WHERE usuario_id = ? "
                "AND (fecha_captura IS NULL OR fecha_captura = '')",
                (usuario_id,)
            ).fetchone()

        return int(fila["total"])

    except Exception:
        return 0


# =========================================================
# GRÁFICAS DEL REPORTE
# =========================================================

def preparar_eje(eje):
    eje.spines["top"].set_visible(False)
    eje.spines["right"].set_visible(False)
    eje.tick_params(labelsize=8)
    eje.grid(axis="y", alpha=0.3, linewidth=0.6)
    eje.set_axisbelow(True)


def etiqueta_fecha_corta(dia):
    try:
        return datetime.strptime(dia, "%Y-%m-%d").strftime("%d/%m")
    except (TypeError, ValueError):
        return str(dia)


def grafica_resumen_lotes(a):
    figura = Figure(figsize=(7.2, 2.9), dpi=150)

    eje_dona = figura.add_axes([0.0, 0.08, 0.30, 0.78])
    eje_dona.pie(
        [a["con"], a["sin"]],
        colors=[COLOR_NARANJA, "#C8E6C9"],
        startangle=90,
        counterclock=False,
        wedgeprops=dict(width=0.38, edgecolor="white"),
    )
    eje_dona.text(
        0, 0, f"{a['pct_con']:.0f} %\ncon\ndetecciones",
        ha="center", va="center", fontsize=8, fontweight="bold"
    )
    eje_dona.set_title("Imágenes", fontsize=9)
    eje_dona.legend(
        [f"Con detecciones ({a['con']})", f"Sin detecciones ({a['sin']})"],
        loc="upper center", bbox_to_anchor=(0.5, 0.02),
        fontsize=7, frameon=False
    )

    lotes = a["lotes"][-MAX_LOTES_GRAFICA:]

    eje = figura.add_axes([0.42, 0.22, 0.56, 0.62])
    preparar_eje(eje)

    etiquetas = [l["corto"] for l in lotes]
    valores = [l["detecciones"] for l in lotes]

    barras = eje.bar(range(len(lotes)), valores, color=COLOR_VERDE, width=0.65)
    eje.set_xticks(range(len(lotes)))
    eje.set_xticklabels(etiquetas, fontsize=7)
    eje.set_ylabel("Detecciones", fontsize=8)
    eje.set_title("Detecciones por lote", fontsize=9)

    maximo = max(valores) if valores else 0
    eje.set_ylim(0, maximo * 1.2 + 1)

    for barra, valor in zip(barras, valores):
        eje.text(
            barra.get_x() + barra.get_width() / 2, valor + maximo * 0.02,
            str(valor), ha="center", va="bottom", fontsize=7
        )

    return figura_a_png(figura)


def grafica_detecciones_fecha(a):
    serie = a["tendencia"]["serie"]
    dias = [s[0] for s in serie]

    figura = Figure(figsize=(7.2, 3.0), dpi=150)
    eje = figura.add_axes([0.08, 0.2, 0.84, 0.62])
    preparar_eje(eje)

    detecciones = [a["por_dia"][d]["detecciones"] for d in dias]
    promedios = [s[1] for s in serie]
    posiciones = list(range(len(dias)))

    eje.bar(
        posiciones, detecciones, color="#A5D6A7", width=0.65,
        label="Detecciones del día"
    )
    eje.set_ylabel("Detecciones", fontsize=8)
    eje.set_title("Detecciones por fecha de captura", fontsize=9)

    paso = max(1, math.ceil(len(dias) / 14))
    eje.set_xticks(posiciones[::paso])
    eje.set_xticklabels(
        [etiqueta_fecha_corta(d) for d in dias[::paso]],
        fontsize=7, rotation=45, ha="right"
    )

    eje.set_ylim(0, (max(detecciones) if detecciones else 0) * 1.25 + 1)

    eje_prom = eje.twinx()
    eje_prom.plot(
        posiciones, promedios, color=COLOR_ROJO, marker="o",
        markersize=4, linewidth=1.6, label="Promedio por imagen"
    )
    eje_prom.set_ylabel("Promedio por imagen", fontsize=8, color=COLOR_ROJO)
    eje_prom.tick_params(labelsize=8, colors=COLOR_ROJO)
    eje_prom.spines["top"].set_visible(False)
    eje_prom.set_ylim(0, (max(promedios) if promedios else 0) * 1.25 + 0.1)

    manejadores = eje.get_legend_handles_labels()
    manejadores_2 = eje_prom.get_legend_handles_labels()
    eje.legend(
        manejadores[0] + manejadores_2[0],
        manejadores[1] + manejadores_2[1],
        loc="upper left", fontsize=7, frameon=False
    )

    return figura_a_png(figura)


def grafica_comparacion_lotes(a):
    lotes = a["lotes"][-MAX_LOTES_GRAFICA:]

    figura = Figure(figsize=(7.2, 2.6), dpi=150)
    eje = figura.add_axes([0.08, 0.2, 0.9, 0.62])
    preparar_eje(eje)

    valores = [l["promedio"] for l in lotes]
    maximo = max(valores) if valores else 0

    colores = [
        COLOR_ROJO if (v == maximo and maximo > 0) else COLOR_VERDE
        for v in valores
    ]

    barras = eje.bar(range(len(lotes)), valores, color=colores, width=0.65)
    eje.axhline(
        a["promedio"], color=COLOR_GRIS, linestyle="--", linewidth=1,
        label=f"Promedio del periodo ({a['promedio']:.2f})"
    )

    eje.set_xticks(range(len(lotes)))
    eje.set_xticklabels(
        [l["corto"] for l in lotes],
        fontsize=7
    )
    eje.set_ylabel("Detecciones por imagen", fontsize=8)
    eje.set_title("Comparación entre lotes (promedio por imagen)", fontsize=9)
    eje.set_ylim(0, maximo * 1.25 + 0.1)
    eje.legend(loc="upper right", fontsize=7, frameon=False)

    for barra, valor in zip(barras, valores):
        eje.text(
            barra.get_x() + barra.get_width() / 2, valor + maximo * 0.02,
            f"{valor:.2f}", ha="center", va="bottom", fontsize=7
        )

    return figura_a_png(figura)


def generar_mapas_por_fecha_png(grupos):
    """
    Un mapa de calor pequeño por fecha, todos con la misma extensión y la
    misma escala de color para poder comparar la concentración.
    """

    todos = [p for g in grupos for p in g["puntos"]]

    latitudes = np.array([p[0] for p in todos], dtype=float)
    longitudes = np.array([p[1] for p in todos], dtype=float)

    factor = max(math.cos(math.radians(float(latitudes.mean()))), 0.01)

    centro_lat = (latitudes.min() + latitudes.max()) / 2
    centro_lon = (longitudes.min() + longitudes.max()) / 2

    amplitud_lat = max(latitudes.max() - latitudes.min(), 0.002) * 1.3
    amplitud_lon = max(
        longitudes.max() - longitudes.min(), 0.002 / factor
    ) * 1.3

    lat_min = centro_lat - amplitud_lat / 2
    lat_max = centro_lat + amplitud_lat / 2
    lon_min = centro_lon - amplitud_lon / 2
    lon_max = centro_lon + amplitud_lon / 2

    filas_malla = 100
    columnas_malla = int(round(filas_malla * (amplitud_lon * factor) / amplitud_lat))
    columnas_malla = max(40, min(columnas_malla, 220))

    matrices = []

    for grupo in grupos:
        lat = np.array([p[0] for p in grupo["puntos"]], dtype=float)
        lon = np.array([p[1] for p in grupo["puntos"]], dtype=float)
        peso = np.array([p[2] for p in grupo["puntos"]], dtype=float)

        matriz, _, _ = np.histogram2d(
            lon, lat,
            bins=[columnas_malla, filas_malla],
            range=[[lon_min, lon_max], [lat_min, lat_max]],
            weights=peso
        )

        matrices.append(
            suavizar_matriz(
                matriz.T, sigma=max(columnas_malla, filas_malla) * 0.05
            )
        )

    maximo_global = max(float(m.max()) for m in matrices)
    mapa_colores = obtener_mapa_colores("jet")

    fondo = obtener_fondo_mapa(lat_min, lat_max, lon_min, lon_max)

    n = len(grupos)
    columnas_fig = 3 if n >= 3 else n
    filas_fig = math.ceil(n / columnas_fig)

    figura = Figure(figsize=(7.2, 2.35 * filas_fig + 0.5), dpi=140)
    ejes = figura.subplots(filas_fig, columnas_fig, squeeze=False)
    figura.subplots_adjust(
        left=0.02, right=0.9, top=0.86, bottom=0.03,
        wspace=0.08, hspace=0.38
    )

    for indice, eje in enumerate(ejes.ravel()):
        if indice >= n:
            eje.set_visible(False)
            continue

        grupo = grupos[indice]
        matriz = matrices[indice]

        normalizada = (
            matriz / maximo_global if maximo_global > 0
            else np.zeros_like(matriz)
        )

        rgba = mapa_colores(normalizada)
        rgba[..., 3] = np.clip(
            normalizada * 2.0, 0, 0.7 if fondo is not None else 0.85
        )

        eje.set_facecolor("#EEF2EE")

        if fondo is not None:
            eje.imshow(
                fondo, extent=[lon_min, lon_max, lat_min, lat_max],
                interpolation="bilinear", aspect=1.0 / factor, zorder=0
            )

        eje.imshow(
            rgba, origin="lower",
            extent=[lon_min, lon_max, lat_min, lat_max],
            interpolation="bilinear", aspect=1.0 / factor, zorder=1
        )
        eje.scatter(
            [p[1] for p in grupo["puntos"]],
            [p[0] for p in grupo["puntos"]],
            s=14, c="white", edgecolors="black", linewidths=0.6, zorder=3
        )
        eje.set_xlim(lon_min, lon_max)
        eje.set_ylim(lat_min, lat_max)
        eje.set_xticks([])
        eje.set_yticks([])
        eje.set_title(
            f"{formatear_fecha_corta(grupo['dia'])}\n"
            f"{grupo['detecciones']} detecciones, "
            f"{grupo['imagenes']} imagen(es)",
            fontsize=7.5
        )

    eje_barra = figura.add_axes([0.93, 0.15, 0.015, 0.62])
    barra = figura.colorbar(
        ScalarMappable(norm=Normalize(0, 1), cmap=mapa_colores),
        cax=eje_barra
    )
    barra.set_ticks([0, 1])
    barra.set_ticklabels(["Baja", "Alta"])
    barra.ax.tick_params(labelsize=7)

    return figura_a_png(figura)


# =========================================================
# EVIDENCIAS FOTOGRÁFICAS
# =========================================================

def miniatura_evidencia(registro, lado=700):
    """
    Devuelve (jpeg, ancho, alto) con los recuadros de detección dibujados,
    o None si la imagen original no está disponible.
    """

    nombre = str(registro.get("archivo_guardado") or "")
    ruta = UPLOAD_DIR / nombre

    if not nombre or not ruta.is_file():
        return None

    try:
        with Image.open(ruta) as imagen:
            orientacion = 1

            try:
                orientacion = int(imagen.getexif().get(274, 1) or 1)
            except Exception:
                pass

            imagen.load()
            ancho, alto = imagen.size
            rgb = imagen.convert("RGB")

        escala = min(1.0, lado / float(max(ancho, alto)))

        if escala < 1.0:
            rgb = rgb.resize(
                (max(1, int(ancho * escala)), max(1, int(alto * escala))),
                Image.LANCZOS
            )

        dibujo = ImageDraw.Draw(rgb)
        grosor = max(2, int(max(rgb.size) / 200))

        for deteccion in registro.get("detecciones") or []:
            try:
                x1, y1, x2, y2 = (
                    float(deteccion[clave]) * escala
                    for clave in ("x1", "y1", "x2", "y2")
                )
            except (KeyError, TypeError, ValueError):
                continue

            dibujo.rectangle(
                [x1, y1, x2, y2], outline=(255, 235, 59), width=grosor
            )

        transposiciones = getattr(Image, "Transpose", Image)
        operaciones = {
            2: transposiciones.FLIP_LEFT_RIGHT,
            3: transposiciones.ROTATE_180,
            4: transposiciones.FLIP_TOP_BOTTOM,
            5: transposiciones.TRANSPOSE,
            6: transposiciones.ROTATE_270,
            7: transposiciones.TRANSVERSE,
            8: transposiciones.ROTATE_90,
        }

        if orientacion in operaciones:
            rgb = rgb.transpose(operaciones[orientacion])

        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=78)

        return buffer.getvalue(), rgb.size[0], rgb.size[1]

    except Exception as error:
        print(f"Aviso: no se pudo preparar la evidencia {nombre}: {error}")
        return None


# =========================================================
# CONSTRUCCIÓN DEL PDF
# =========================================================

def construir_pdf(
    usuario,
    registros,
    desde,
    hasta,
    mapa_png,
    total_puntos,
    analisis=None,
    registros_previos=None,
    rango_previo=None,
    sin_fecha=0,
    parte="informe",
):
    """parte='informe': secciones 1 a 6. parte='evidencias': documento aparte."""

    es_evidencias = parte == "evidencias"
    buffer = io.BytesIO()

    verde = colors.HexColor(COLOR_VERDE)
    verde_oscuro = colors.HexColor(COLOR_VERDE_OSCURO)
    verde_claro = colors.HexColor(COLOR_VERDE_CLARO)
    gris = colors.HexColor(COLOR_GRIS)
    linea = colors.HexColor("#CFD8CF")

    ancho_util = 17 * cm

    nombre_periodo = (
        f"{formatear_fecha_corta(desde)} al {formatear_fecha_corta(hasta)}"
    )

    titulo_doc = (
        "Evidencias del monitoreo de mosca pinta" if es_evidencias
        else "Reporte de monitoreo de mosca pinta"
    )

    def pie_de_pagina(lienzo, doc):
        lienzo.saveState()
        lienzo.setStrokeColor(verde)
        lienzo.setLineWidth(2)
        lienzo.line(2 * cm, A4[1] - 1.2 * cm, A4[0] - 2 * cm, A4[1] - 1.2 * cm)
        lienzo.setStrokeColor(linea)
        lienzo.setLineWidth(0.5)
        lienzo.line(2 * cm, 1.5 * cm, A4[0] - 2 * cm, 1.5 * cm)
        lienzo.setFont("Helvetica", 8)
        lienzo.setFillColor(gris)
        lienzo.drawString(
            2 * cm, 1.0 * cm,
            f"{titulo_doc}  |  Periodo: {nombre_periodo}"
        )
        lienzo.drawRightString(
            A4[0] - 2 * cm, 1.0 * cm, f"Página {doc.page}"
        )
        lienzo.restoreState()

    documento = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.9 * cm,
        bottomMargin=2.0 * cm,
        title=titulo_doc,
        author=usuario["nombre_usuario"],
    )

    estilos = getSampleStyleSheet()
    normal = ParagraphStyle(
        "TextoReporte", parent=estilos["Normal"], fontSize=9, leading=12
    )
    e_h1 = ParagraphStyle(
        "H1Reporte", parent=estilos["Heading1"], fontSize=14, leading=17,
        textColor=verde_oscuro, spaceBefore=0, spaceAfter=0
    )
    e_h2 = ParagraphStyle(
        "H2Reporte", parent=estilos["Heading2"], fontSize=10.5, leading=13,
        textColor=colors.HexColor("#37474F"), spaceBefore=8, spaceAfter=4, keepWithNext=1
    )
    e_celda = ParagraphStyle(
        "CeldaReporte", parent=normal, fontSize=8, leading=10,
        wordWrap="CJK"
    )
    e_celda_centro = ParagraphStyle(
        "CeldaCentro", parent=e_celda, alignment=1
    )
    e_nota = ParagraphStyle(
        "NotaReporte", parent=normal, fontSize=8, leading=10, textColor=gris
    )
    e_kpi = ParagraphStyle(
        "KpiReporte", parent=normal, fontSize=20, leading=24, alignment=1,
        fontName="Helvetica-Bold"
    )
    e_kpi_et = ParagraphStyle(
        "KpiEtiqueta", parent=normal, fontSize=8, leading=10, alignment=1,
        textColor=gris
    )
    e_portada = ParagraphStyle(
        "Portada", parent=normal, fontSize=21, leading=25,
        textColor=colors.white, fontName="Helvetica-Bold"
    )
    e_portada_2 = ParagraphStyle(
        "PortadaSub", parent=normal, fontSize=10, leading=13,
        textColor=colors.HexColor("#C8E6C9")
    )
    e_vineta = ParagraphStyle(
        "VinetaReporte", parent=normal, leftIndent=12, bulletIndent=2,
        spaceAfter=4
    )

    def seccion(numero, titulo):
        return [
            CondPageBreak(9.5 * cm),
            Spacer(1, 10),
            Paragraph(f"{numero}. {escape(titulo)}", e_h1),
            HRFlowable(
                width="100%", thickness=1.2, color=verde,
                spaceBefore=2, spaceAfter=6
            ),
        ]

    def tabla_pares(filas, anchos=(4.3, 4.2, 4.3, 4.2)):
        """Tabla de 4 columnas: etiqueta, valor, etiqueta, valor."""

        datos = [
            [
                Paragraph(f"<b>{escape(f[0])}</b>", e_celda),
                Paragraph(escape(f[1]), e_celda),
                Paragraph(f"<b>{escape(f[2])}</b>", e_celda) if f[2] else "",
                Paragraph(escape(f[3]), e_celda) if f[3] else "",
            ]
            for f in filas
        ]

        tabla = Table(datos, colWidths=[w * cm for w in anchos])
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, -1), verde_claro),
            ("BACKGROUND", (2, 0), (2, -1), verde_claro),
            ("GRID", (0, 0), (-1, -1), 0.4, linea),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return tabla

    def tabla_datos(encabezados, filas, anchos, centrar_desde=1):
        datos = [[
            Paragraph(
                f'<font color="white"><b>{escape(h)}</b></font>', e_celda_centro
            )
            for h in encabezados
        ]] + filas

        tabla = Table(datos, colWidths=[w * cm for w in anchos], repeatRows=1)
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), verde),
            ("GRID", (0, 0), (-1, -1), 0.4, linea),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (centrar_desde, 1), (-1, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#F5F9F5")]),
        ]))
        return tabla

    def celda(texto, centrado=True):
        return Paragraph(
            escape(str(texto)), e_celda_centro if centrado else e_celda
        )

    def indicadores(items):
        celdas = [[
            [
                Paragraph(f'<font color="{color}">{escape(valor)}</font>', e_kpi),
                Paragraph(escape(etiqueta), e_kpi_et),
            ]
            for valor, etiqueta, color in items
        ]]

        tabla = Table(
            celdas, colWidths=[ancho_util / len(items)] * len(items)
        )
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F8F4")),
            ("LINEABOVE", (0, 0), (-1, 0), 3, verde),
            ("LINEBEFORE", (1, 0), (-1, 0), 4, colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ]))
        return tabla

    def imagen_png(png, ancho_cm=17.0, alto_max_cm=12.0):
        with Image.open(io.BytesIO(png)) as imagen_base:
            ancho_px, alto_px = imagen_base.size

        ancho = ancho_cm * cm
        alto = ancho * alto_px / ancho_px
        maximo = alto_max_cm * cm

        if alto > maximo:
            ancho = ancho * maximo / alto
            alto = maximo

        return RLImage(io.BytesIO(png), width=ancho, height=alto)

    def caja_nota(texto, fondo="#FFF8E1", borde=COLOR_AMBAR):
        tabla = Table([[Paragraph(texto, e_celda)]], colWidths=[ancho_util])
        tabla.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(fondo)),
            ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(borde)),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ]))
        return tabla

    def seguro(funcion, *argumentos):
        """Si una gráfica falla, el reporte se genera igual sin ella."""

        try:
            return funcion(*argumentos)
        except Exception:
            print(f"Aviso: no se pudo generar {funcion.__name__}:")
            traceback.print_exc()
            return None

    # -----------------------------------------------------------------
    # Datos
    # -----------------------------------------------------------------

    a = analisis or analizar_periodo(registros)

    previo = (
        analizar_periodo(registros_previos)
        if registros_previos is not None else None
    )
    comparacion = comparar_periodos(a, previo, rango_previo)

    recomendaciones = generar_recomendaciones(a, comparacion, sin_fecha)
    conclusiones = generar_conclusiones(
        a, comparacion, recomendaciones, desde, hasta
    )

    nombre = texto_seguro(
        usuario.get("nombre_completo") or usuario["nombre_usuario"], 80
    ) or usuario["nombre_usuario"]

    def bloque_evidencias(numero):
        """Galería y tabla de resultados por imagen (documento de evidencias)."""

        salida = list(seccion(numero, "Evidencias"))
        salida.append(Paragraph("Fotografías analizadas", e_h2))

        candidatas = sorted(
            registros, key=lambda r: -int(r.get("cantidad") or 0)
        )[:MAX_EVIDENCIAS]

        celdas_galeria = []

        for registro in candidatas:
            cantidad = int(registro.get("cantidad") or 0)
            metadatos = registro.get("metadatos") or {}
            latitud = metadatos.get("gps_latitud")
            longitud = metadatos.get("gps_longitud")

            ubicacion = (
                f"{float(latitud):.5f}, {float(longitud):.5f}"
                if validar_coordenadas(latitud, longitud) else "Sin GPS"
            )
            lote = etiqueta_lote_registro(registro)
            fecha_hora = (registro.get("fecha_captura") or "").replace("T", " ")[:16]

            pie = Paragraph(
                f"<b>{escape(texto_seguro(registro.get('nombre_imagen'), 40))}</b>"
                f"<br/>Captura: {escape(fecha_hora)}"
                f"<br/>Detecciones: <b>{cantidad}</b>  |  {escape(lote)}"
                f"<br/>GPS: {escape(ubicacion)}",
                ParagraphStyle(
                    "PieEvidencia", parent=e_celda, fontSize=7, leading=9
                )
            )

            miniatura = miniatura_evidencia(registro)

            if miniatura:
                jpeg, ancho_px, alto_px = miniatura
                ancho = 5.0 * cm
                alto = ancho * alto_px / ancho_px

                if alto > 4.0 * cm:
                    ancho = ancho * (4.0 * cm) / alto
                    alto = 4.0 * cm

                contenido = [
                    RLImage(io.BytesIO(jpeg), width=ancho, height=alto), pie
                ]
            else:
                contenido = [
                    Paragraph("<i>Imagen no disponible</i>", e_celda), pie
                ]

            celdas_galeria.append(contenido)

        if celdas_galeria:
            filas_galeria = []

            for inicio in range(0, len(celdas_galeria), 3):
                fila = celdas_galeria[inicio:inicio + 3]
                fila += [""] * (3 - len(fila))
                filas_galeria.append(fila)

            tabla_galeria = Table(
                filas_galeria, colWidths=[ancho_util / 3] * 3
            )
            tabla_galeria.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, linea),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            salida.append(tabla_galeria)
            salida.append(Paragraph(
                f"Se muestran las {len(celdas_galeria)} imágenes con más "
                f"detecciones; los recuadros amarillos son las detecciones del "
                f"modelo. Todas las imágenes del periodo aparecen en la tabla "
                f"siguiente.",
                e_nota
            ))

        salida.append(Paragraph("Resultados de detección por imagen", e_h2))

        filas_detalle = []

        for registro in registros[:MAX_FILAS_DETALLE]:
            metadatos = registro.get("metadatos") or {}
            latitud = metadatos.get("gps_latitud")
            longitud = metadatos.get("gps_longitud")

            coordenadas = (
                f"{float(latitud):.5f}, {float(longitud):.5f}"
                if validar_coordenadas(latitud, longitud) else "Sin GPS"
            )

            cantidad = int(registro.get("cantidad") or 0)
            confianza_registro = (
                f"{float(registro.get('confianza_promedio') or 0) * 100:.0f} %"
                if cantidad else "-"
            )
            lote = etiqueta_lote_registro(registro, corto=True)
            fecha_hora = (registro.get("fecha_captura") or "").replace("T", " ")[:16]

            nota_texto = escape(
                texto_seguro(registro.get("nota"), 300) or "-"
            ).replace("\n", "<br/>")

            filas_detalle.append([
                Paragraph(escape(fecha_hora), e_celda),
                Paragraph(escape(texto_seguro(registro.get("nombre_imagen"), 80)), e_celda),
                celda(lote), celda(cantidad), celda(confianza_registro),
                Paragraph(escape(coordenadas), e_celda),
                Paragraph(nota_texto, e_celda),
            ])

        salida.append(tabla_datos(
            ["Fecha de captura", "Imagen", "Lote", "Moscas", "Confianza",
             "Ubicación", "Nota"],
            filas_detalle, [2.5, 2.8, 2.0, 1.7, 1.9, 3.0, 3.1],
            centrar_desde=2
        ))

        if len(registros) > MAX_FILAS_DETALLE:
            salida.append(Paragraph(
                f"Se muestran las primeras {MAX_FILAS_DETALLE} de "
                f"{len(registros)} imágenes del periodo.",
                e_nota
            ))

        return salida

    # -----------------------------------------------------------------
    # Encabezado
    # -----------------------------------------------------------------

    portada = Table(
        [
            [Paragraph(titulo_doc, e_portada)],
            [Paragraph(
                f"Cultivo de caña de azúcar  |  Periodo de captura: "
                f"{nombre_periodo}",
                e_portada_2
            )],
        ],
        colWidths=[ancho_util]
    )
    portada.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), verde_oscuro),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING", (0, 0), (-1, 0), 14),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 14),
    ]))

    elementos = [portada, Spacer(1, 12)]

    # -----------------------------------------------------------------
    # Documento aparte: evidencias
    # -----------------------------------------------------------------

    if es_evidencias:
        elementos += seccion(1, "Información general")
        elementos.append(tabla_pares([
            ("Fecha inicial", formatear_fecha_corta(desde),
             "Fecha final", formatear_fecha_corta(hasta)),
            ("Imágenes analizadas", str(a["n"]),
             "Detecciones totales", str(a["total"])),
            ("Imágenes con detecciones", str(a["con"]),
             "Imágenes con GPS", f"{a['n_gps']} ({a['pct_gps']:.0f} %)"),
            ("Fecha de generación", datetime.now().strftime("%d/%m/%Y %H:%M"),
             "Generado por", nombre),
        ]))
        elementos.append(Paragraph(
            "Este documento complementa el reporte de monitoreo del mismo "
            "periodo y reúne las fotografías analizadas con sus resultados.",
            e_nota
        ))

        if a["n"] == 0:
            elementos.append(Spacer(1, 14))
            elementos.append(Paragraph(
                "No hay fotografías con fecha de captura dentro de este "
                "periodo.", normal
            ))
        else:
            elementos += bloque_evidencias(2)

        documento.build(
            elementos, onFirstPage=pie_de_pagina, onLaterPages=pie_de_pagina
        )
        return buffer.getvalue()

    # -----------------------------------------------------------------
    # 1. Información general del periodo
    # -----------------------------------------------------------------

    elementos += seccion(1, "Información general del periodo")

    if a["primera"]:
        periodo_monitoreo = (
            f"{formatear_fecha_corta(a['primera'])} al "
            f"{formatear_fecha_corta(a['ultima'])}"
        )
        detalle_monitoreo = (
            f"{a['dias_monitoreo']} día(s), con capturas en "
            f"{a['dias_con_capturas']}"
        )
    else:
        periodo_monitoreo = "Sin capturas"
        detalle_monitoreo = "-"

    lotes_texto = str(a["n_lotes"])

    if a["n_sin_lote"]:
        lotes_texto += f" (+ {a['n_sin_lote']} imagen(es) sin lote)"

    elementos.append(tabla_pares([
        ("Fecha inicial", formatear_fecha_corta(desde),
         "Fecha final", formatear_fecha_corta(hasta)),
        ("Periodo de monitoreo", periodo_monitoreo,
         "Duración", detalle_monitoreo),
        ("Imágenes analizadas", str(a["n"]),
         "Lotes involucrados", lotes_texto),
        ("Fecha de generación", datetime.now().strftime("%d/%m/%Y %H:%M"),
         "Generado por", nombre),
        ("Imágenes con GPS", f"{a['n_gps']} ({a['pct_gps']:.0f} %)",
         "Imágenes sin fecha*", str(sin_fecha)),
    ]))
    elementos.append(Paragraph(
        "El periodo se filtra por la fecha original de captura (metadatos "
        "de la fotografía), no por la fecha de subida al sistema. "
        "*Imágenes del sistema sin fecha de captura: no se incluyen en "
        "ningún reporte por periodo.",
        e_nota
    ))

    if a["n"] == 0:
        elementos.append(Spacer(1, 14))
        elementos.append(Paragraph(
            "No hay fotografías con fecha de captura dentro de este periodo.",
            normal
        ))

        elementos += seccion(5, "Estrategias de mitigación y recomendaciones")
        elementos.append(Paragraph(escape(recomendaciones[0]["texto"]), normal))

        documento.build(
            elementos, onFirstPage=pie_de_pagina, onLaterPages=pie_de_pagina
        )
        return buffer.getvalue()

    # -----------------------------------------------------------------
    # 2. Resumen estadístico
    # -----------------------------------------------------------------

    elementos += seccion(2, "Resumen estadístico")

    elementos.append(indicadores([
        (str(a["n"]), "Imágenes analizadas", COLOR_VERDE_OSCURO),
        (str(a["total"]), "Moscas pintas detectadas", COLOR_ROJO),
        (f"{a['pct_con']:.0f} %", "Imágenes con detecciones", COLOR_NARANJA),
        (f"{a['promedio']:.2f}", "Detecciones por imagen", COLOR_AZUL),
    ]))
    elementos.append(Spacer(1, 8))

    confianza_texto = (
        f"{a['confianza'] * 100:.1f} %" if a["confianza"] is not None
        else "No disponible"
    )

    elementos.append(tabla_pares([
        ("Total de detecciones", str(a["total"]),
         "Promedio por imagen", f"{a['promedio']:.2f}"),
        ("Imágenes con detecciones", f"{a['con']} ({a['pct_con']:.0f} %)",
         "Máximo en una imagen", str(a["maximo"])),
        ("Imágenes sin detecciones",
         f"{a['sin']} ({100 - a['pct_con']:.0f} %)",
         "Confianza promedio", confianza_texto),
    ]))
    elementos.append(Spacer(1, 6))
    elementos.append(Paragraph(
        f"Nivel de presencia de referencia: <b>{escape(a['nivel'])}</b> "
        f"(según el promedio de ejemplares por imagen: bajo, menos de "
        f"{UMBRAL_MODERADO:g}; moderado, de {UMBRAL_MODERADO:g} a menos de "
        f"{UMBRAL_ALTO:g}; alto, {UMBRAL_ALTO:g} o más). Es un criterio "
        f"interno y provisional de este sistema, no un umbral económico "
        f"oficial, y solo es comparable si las fotografías se toman en "
        f"condiciones similares.",
        e_nota
    ))

    elementos.append(Paragraph("Distribución de detecciones por lote", e_h2))

    grafica = seguro(grafica_resumen_lotes, a)
    if grafica:
        elementos.append(imagen_png(grafica, 17, 8))

    filas_lotes = []

    for lote in a["lotes"][-40:]:
        if lote["primera"]:
            captura = (
                f"{formatear_fecha_corta(lote['primera'])} - "
                f"{formatear_fecha_corta(lote['ultima'])}"
            )
        else:
            captura = "Sin fecha"

        filas_lotes.append([
            celda(lote["etiqueta"], False), celda(captura),
            celda(lote["imagenes"]), celda(lote["con"]),
            celda(lote["detecciones"]), celda(f"{lote['promedio']:.2f}"),
        ])

    elementos.append(Spacer(1, 4))
    elementos.append(tabla_datos(
        ["Lote", "Fechas de captura", "Imágenes", "Con detecc.",
         "Detecciones", "Prom./imagen"],
        filas_lotes, [3.0, 4.6, 2.0, 2.4, 2.4, 2.6]
    ))
    elementos.append(Paragraph(
        "Solo se cuentan las imágenes de cada lote cuya fecha de captura "
        "cae dentro del periodo. Un reporte puede incluir varios lotes.",
        e_nota
    ))
    elementos.append(Spacer(1, 6))
    elementos.append(caja_nota(
        "<b>Importante:</b> una detección es un objeto identificado por el "
        "modelo en una fotografía. El número de detecciones es un indicador "
        "de presencia y no equivale a la población total real de mosca "
        "pinta en el cultivo."
    ))

    # -----------------------------------------------------------------
    # 3. Distribución geográfica
    # -----------------------------------------------------------------

    elementos += seccion(3, "Distribución geográfica")
    elementos.append(Paragraph("Mapa de calor acumulado del periodo", e_h2))
    elementos.append(imagen_png(mapa_png, 17, 12.5))
    elementos.append(Paragraph(
        "Las zonas rojas concentran más detecciones. Los círculos blancos "
        "son los puntos donde se realizaron los análisis (con GPS). "
        "Los anillos rojos A1, A2... marcan las zonas de mayor "
        "concentración y los azules B1, B2... las de menor concentración. "
        "Mapa base: imágenes satelitales de Esri, descargadas al generar el reporte "
        "(requiere internet en ese momento; sin conexión el mapa se "
        "muestra sin mapa base).",
        e_nota
    ))

    zonas = a["zonas"]

    if zonas["puntos"] == 0:
        elementos.append(Spacer(1, 6))
        elementos.append(caja_nota(
            "Ninguna imagen del periodo tiene ubicación GPS, por lo que no "
            "se pueden identificar zonas."
        ))
    else:
        def filas_zonas(lista):
            return [[
                celda(z["etiqueta"]),
                celda(f"{z['lat']:.5f}, {z['lon']:.5f}"),
                celda(z["imagenes"]), celda(z["detecciones"]),
                celda(f"{z['pct_total']:.0f} %"),
                celda(f"{z['promedio']:.2f}"), celda(z["nivel"]),
            ] for z in lista]

        encabezados_zonas = [
            "Zona", "Coordenadas", "Imágenes", "Detecciones",
            "% del total", "Prom./imagen", "Nivel"
        ]
        anchos_zonas = [1.4, 4.2, 2.0, 2.3, 2.3, 2.5, 2.3]

        elementos.append(Paragraph(
            "Zonas con mayor concentración de detecciones", e_h2
        ))

        if zonas["altas"]:
            elementos.append(tabla_datos(
                encabezados_zonas, filas_zonas(zonas["altas"]), anchos_zonas
            ))
        else:
            elementos.append(Paragraph(
                "No hubo detecciones en los puntos con GPS.", normal
            ))

        elementos.append(Paragraph(
            "Zonas con menor concentración de detecciones", e_h2
        ))

        if zonas["bajas"]:
            elementos.append(tabla_datos(
                encabezados_zonas, filas_zonas(zonas["bajas"]), anchos_zonas
            ))
        else:
            elementos.append(Paragraph(
                "No hay otras zonas para comparar: todos los puntos quedaron "
                "agrupados en las zonas de mayor concentración.",
                normal
            ))

        elementos.append(Paragraph(
            f"Las zonas son celdas aproximadas de {TAMANO_ZONA_METROS} m. "
            f"El nivel de cada zona depende de su promedio de ejemplares por "
            f"imagen: foco rojo desde {UMBRAL_ALTO:g}, moderado desde "
            f"{UMBRAL_MODERADO:g}, bajo por debajo de ese valor.",
            e_nota
        ))

    elementos.append(Paragraph(
        "Distribución geográfica por fecha de captura", e_h2
    ))

    grupos_fecha = seleccionar_fechas_mapa(registros)

    if len(grupos_fecha) >= 2:
        mapas_fecha = seguro(generar_mapas_por_fecha_png, grupos_fecha)

        if mapas_fecha:
            elementos.append(imagen_png(mapas_fecha, 17, 13))
            elementos.append(Paragraph(
                "Todos los mapas usan la misma extensión y la misma escala "
                "de color, por lo que se pueden comparar entre fechas. "
                + (
                    f"Se muestran las {len(grupos_fecha)} fechas con más "
                    f"detecciones."
                    if len(grupos_fecha) == MAX_MAPAS_FECHA else
                    "Se muestran todas las fechas con ubicación GPS."
                ),
                e_nota
            ))
    else:
        elementos.append(Paragraph(
            "Se necesitan capturas con GPS en al menos 2 fechas para "
            "comparar la distribución. En este periodo, el mapa acumulado "
            "representa la única fecha disponible.",
            normal
        ))

    # -----------------------------------------------------------------
    # 4. Comportamiento temporal
    # -----------------------------------------------------------------

    elementos += seccion(4, "Comportamiento temporal")

    grafica = seguro(grafica_detecciones_fecha, a)
    if grafica:
        elementos.append(imagen_png(grafica, 17, 8))

    tendencia = a["tendencia"]

    colores_tendencia = {
        "Al alza": COLOR_ROJO, "A la baja": COLOR_VERDE,
        "Estable": COLOR_AZUL, "Datos insuficientes": COLOR_GRIS,
    }

    elementos.append(Paragraph("Aumentos y disminuciones", e_h2))

    def texto_cambio(cambio):
        if not cambio:
            return "No se observó"
        return (
            f"{formatear_fecha_corta(cambio[0])} a "
            f"{formatear_fecha_corta(cambio[1])} ({cambio[2]:+.2f} por imagen)"
        )

    filas_tendencia = [
        (
            "Tendencia general", tendencia["clase"],
            "Variación (inicio vs. final)",
            texto_variacion(tendencia["cambio_pct"])
            if tendencia["suficiente"] else "Datos insuficientes"
        ),
        (
            "Mayor promedio por imagen",
            (
                f"{formatear_fecha_corta(tendencia['pico'][0])} "
                f"({tendencia['pico'][1]:.2f})"
            ) if tendencia["pico"] else "-",
            "Mayor aumento entre capturas",
            texto_cambio(tendencia["mayor_aumento"])
            if tendencia["suficiente"] else "Datos insuficientes",
        ),
        (
            "Mayor disminución entre capturas",
            texto_cambio(tendencia["mayor_disminucion"])
            if tendencia["suficiente"] else "Datos insuficientes",
            "Días con capturas", str(a["dias_con_capturas"]),
        ),
    ]

    tabla_tendencia = tabla_pares(filas_tendencia)
    elementos.append(tabla_tendencia)
    elementos.append(Paragraph(
        "La tendencia compara el promedio de detecciones por imagen de la "
        f"primera mitad del periodo contra la segunda; se considera "
        f"aumento o disminución una variación de {UMBRAL_CAMBIO_PCT} % o "
        f"más. Requiere capturas en al menos 3 fechas. "
        f"Tendencia detectada: <font color=\"{colores_tendencia.get(tendencia['clase'], COLOR_GRIS)}\">"
        f"<b>{escape(tendencia['clase'])}</b></font>.",
        e_nota
    ))

    elementos.append(Paragraph("Comparación entre lotes", e_h2))

    if len(a["lotes"]) >= 2:
        grafica = seguro(grafica_comparacion_lotes, a)
        if grafica:
            elementos.append(imagen_png(grafica, 17, 7.5))
            mayor = max(a["lotes"], key=lambda l: l["promedio"])
            elementos.append(Paragraph(
                f"El lote con mayor promedio de detecciones por imagen fue "
                f"{escape(mayor['etiqueta'])} ({mayor['promedio']:.2f}). "
                f"Se usa el promedio por imagen para comparar lotes de "
                f"distinto tamaño.",
                e_nota
            ))
    else:
        elementos.append(Paragraph(
            "El periodo incluye un solo lote, por lo que no hay otro "
            "contra el cual compararlo.",
            normal
        ))

    elementos.append(Paragraph("Comparación con el periodo anterior", e_h2))

    if previo is not None and rango_previo:
        rango_texto = (
            f"{formatear_fecha_corta(rango_previo[0])} al "
            f"{formatear_fecha_corta(rango_previo[1])}"
        )

        if comparacion["suficiente"]:
            elementos.append(tabla_datos(
                ["Indicador", "Periodo actual", f"Periodo anterior",
                 "Variación"],
                [
                    [celda("Imágenes analizadas", False), celda(a["n"]),
                     celda(previo["n"]), celda("-")],
                    [celda("Detecciones totales", False), celda(a["total"]),
                     celda(previo["total"]),
                     celda(texto_variacion(comparacion["var_detecciones"]))],
                    [celda("Detecciones por imagen", False),
                     celda(f"{a['promedio']:.2f}"),
                     celda(f"{previo['promedio']:.2f}"),
                     celda(texto_variacion(comparacion["var_promedio"]))],
                    [celda("Imágenes con detecciones", False),
                     celda(f"{a['pct_con']:.0f} %"),
                     celda(f"{previo['pct_con']:.0f} %"),
                     celda(f"{comparacion['dif_pct_con']:+.0f} pts")],
                ],
                [5.6, 3.8, 3.8, 3.8]
            ))
            elementos.append(Paragraph(
                f"Periodo anterior de igual duración: {rango_texto}. "
                f"Resultado: <b>{escape(comparacion['clase'])}</b>.",
                e_nota
            ))
        else:
            elementos.append(Paragraph(
                f"No hay registros suficientes para comparar con el periodo "
                f"anterior ({rango_texto}): se requieren al menos "
                f"{MIN_IMAGENES_COMPARACION} imágenes en cada periodo "
                f"(actual: {a['n']}, anterior: {previo['n']}).",
                normal
            ))
    else:
        elementos.append(Paragraph(
            "No hay registros del periodo anterior para comparar.", normal
        ))

    filas_dia = []

    for dia in sorted(d for d in a["por_dia"] if d)[:62]:
        datos_dia = a["por_dia"][dia]
        filas_dia.append([
            celda(formatear_fecha_corta(dia)),
            celda(datos_dia["imagenes"]), celda(datos_dia["con"]),
            celda(datos_dia["detecciones"]),
            celda(f"{datos_dia['detecciones'] / datos_dia['imagenes']:.2f}"),
        ])

    elementos.append(Paragraph("Detecciones por día", e_h2))
    elementos.append(tabla_datos(
        ["Fecha de captura", "Imágenes", "Con detecc.", "Detecciones",
         "Prom./imagen"],
        filas_dia, [4.2, 3.0, 3.2, 3.2, 3.4], centrar_desde=0
    ))

    # -----------------------------------------------------------------
    # 5. Estrategias de mitigación y recomendaciones
    # -----------------------------------------------------------------

    elementos += seccion(5, "Estrategias de mitigación y recomendaciones")
    elementos.append(Paragraph(
        "Las recomendaciones se generan a partir de los resultados de este "
        "periodo y de criterios generales de manejo integrado de plagas. "
        "Son orientativas y no sustituyen la verificación en campo ni la "
        "opinión de un asesor técnico.",
        e_nota
    ))
    elementos.append(Spacer(1, 6))

    colores_prioridad = {
        "Alta": (COLOR_ROJO, "white"),
        "Media": (COLOR_AMBAR, "#212121"),
        "Baja": (COLOR_VERDE, "white"),
    }

    filas_rec = []
    estilos_rec = [
        ("GRID", (0, 0), (-1, -1), 0.4, linea),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]

    for indice, rec in enumerate(recomendaciones):
        fondo, texto_color = colores_prioridad[rec["prioridad"]]

        filas_rec.append([
            Paragraph(
                f'<font color="{texto_color}"><b>'
                f'{escape(rec["prioridad"].upper())}</b></font>',
                e_celda_centro
            ),
            Paragraph(f"<b>{escape(rec['tema'])}</b>", e_celda),
            Paragraph(escape(rec["texto"]), e_celda),
        ])
        estilos_rec.append(
            ("BACKGROUND", (0, indice), (0, indice), colors.HexColor(fondo))
        )

    tabla_rec = Table(
        filas_rec, colWidths=[1.8 * cm, 4.0 * cm, 11.2 * cm]
    )
    tabla_rec.setStyle(TableStyle(estilos_rec))
    elementos.append(tabla_rec)

    # -----------------------------------------------------------------
    # 6. Conclusiones
    # -----------------------------------------------------------------

    elementos += seccion(6, "Conclusiones")

    for conclusion in conclusiones:
        elementos.append(
            Paragraph(escape(conclusion), e_vineta, bulletText="•")
        )

    elementos.append(Spacer(1, 8))
    elementos.append(Paragraph(
        "Las fotografías analizadas y el detalle de cada imagen se entregan "
        "en el documento de evidencias del mismo periodo.",
        e_nota
    ))

    documento.build(
        elementos, onFirstPage=pie_de_pagina, onLaterPages=pie_de_pagina
    )

    return buffer.getvalue()


@app.get("/reporte-pdf")
def reporte_pdf(
    desde: str,
    hasta: str,
    parte: str = "informe",
    usuario: dict = Depends(usuario_actual)
):
    desde, hasta = validar_rango(desde, hasta)

    if not desde or not hasta:
        raise HTTPException(
            status_code=400,
            detail="Debes indicar las fechas 'desde' y 'hasta'."
        )

    if parte not in ("informe", "evidencias"):
        raise HTTPException(
            status_code=400,
            detail="El parámetro 'parte' debe ser 'informe' o 'evidencias'."
        )

    try:
        registros = consultar_registros(
            usuario["id"], desde, hasta, descendente=False
        )

        if parte == "evidencias":
            pdf = construir_pdf(
                usuario, registros, desde, hasta, None, 0,
                analisis=analizar_periodo(registros),
                parte="evidencias",
            )
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="evidencias_mosca_pinta_'
                        f'{desde}_{hasta}.pdf"'
                    )
                }
            )

        puntos = puntos_de_calor(registros)
        analisis = analizar_periodo(registros)

        titulo_mapa = (
            f"Mapa de calor acumulado: {formatear_fecha_corta(desde)} "
            f"al {formatear_fecha_corta(hasta)}"
        )

        try:
            mapa_png = generar_mapa_calor_png(
                puntos, titulo=titulo_mapa, zonas=analisis["zonas"]
            )
        except Exception:
            # Si el dibujo del mapa falla, el reporte se genera igual.
            print("Aviso: no se pudo dibujar el mapa de calor del PDF:")
            traceback.print_exc()
            mapa_png = generar_mapa_calor_png([], titulo=titulo_mapa)

        # Periodo anterior de igual duración (si hay registros).
        registros_previos = None
        rango_previo = None

        try:
            d_desde = parsear_dia(desde)
            d_hasta = parsear_dia(hasta)
            duracion = (d_hasta - d_desde).days + 1
            previo_hasta = d_desde - timedelta(days=1)
            previo_desde = previo_hasta - timedelta(days=duracion - 1)
            rango_previo = (
                previo_desde.strftime("%Y-%m-%d"),
                previo_hasta.strftime("%Y-%m-%d"),
            )
            registros_previos = consultar_registros(
                usuario["id"], rango_previo[0], rango_previo[1],
                descendente=False
            )
        except Exception:
            print("Aviso: no se pudo consultar el periodo anterior:")
            traceback.print_exc()
            registros_previos = None
            rango_previo = None

        pdf = construir_pdf(
            usuario, registros, desde, hasta, mapa_png, len(puntos),
            analisis=analisis,
            registros_previos=registros_previos,
            rango_previo=rango_previo,
            sin_fecha=contar_sin_fecha(usuario["id"]),
        )

    except HTTPException:
        raise

    except Exception as error:
        # El error completo queda en la terminal del servidor.
        print("ERROR al generar el reporte PDF:")
        traceback.print_exc()

        raise HTTPException(
            status_code=500,
            detail=(
                "No se pudo generar el reporte: "
                f"{type(error).__name__}: {error}"
            )
        )

    nombre_archivo = f"reporte_mosca_pinta_{desde}_{hasta}.pdf"

    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{nombre_archivo}"'
        }
    )


# =========================================================
# APP WEB (PWA) SERVIDA POR EL MISMO SERVIDOR
# =========================================================
# Debe quedar al FINAL del archivo, después de todas las rutas de la API.
# Copia el contenido de la carpeta build/web de Flutter dentro de una
# carpeta llamada "web", junto a main.py, y reinicia el servidor.

if (CARPETA_WEB / "index.html").is_file():
    app.mount(
        "/",
        StaticFiles(directory=str(CARPETA_WEB), html=True),
        name="app_web"
    )
    print(f"App web publicada desde: {CARPETA_WEB}")
else:
    print(
        "Aviso: no hay carpeta 'web' junto a main.py; "
        "solo se publica la API."
    )
